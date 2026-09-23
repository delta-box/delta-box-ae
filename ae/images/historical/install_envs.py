#!/usr/bin/env python3
"""
install_envs.py — Install SWE-bench Verified conda environments.
Run inside the VM or chroot:
    python3 install_envs.py env_specs.json [--repos sympy,django,...]

Creates:
  /repos/{name}/            — full git clone of each repository
  /testbed/{name}__{ver}/   — reflink working copy per (repo, version)
  conda env {name}__{ver}   — conda environment per (repo, version)
"""

import json, subprocess, os, sys, re, time
from pathlib import Path
from collections import defaultdict

REPOS_DIR = Path("/repos")
TESTBED_DIR = Path("/testbed")
CONDA = "/opt/miniconda3/bin/conda"

# Install order: lightweight first → heaviest last.
# If disk runs out, we'll have maximized instance coverage.
REPO_PRIORITY = [
    "sympy/sympy",               # 75 inst, 12 env, pure Python
    "django/django",             # 231 inst, 9 env, lightweight
    "pytest-dev/pytest",         # 19 inst, 10 env, pure Python
    "pylint-dev/pylint",         # 10 inst, 5 env, pure Python
    "psf/requests",              # 8 inst, 7 env, pure Python
    "pallets/flask",             # 1 inst, 1 env, pure Python
    "sphinx-doc/sphinx",         # 44 inst, 15 env, needs graphviz
    "pydata/xarray",             # 22 inst, 4 env, numpy/pandas
    "astropy/astropy",           # 22 inst, 6 env, compiled ext
    "mwaskom/seaborn",           # 2 inst, 1 env, needs matplotlib
    "scikit-learn/scikit-learn", # 32 inst, 4 env, C compilation
    "matplotlib/matplotlib",     # 34 inst, 6 env, system libs
]


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def run(cmd, cwd=None, check=True, timeout=900):
    """Run shell command."""
    short = cmd if len(cmd) <= 120 else cmd[:117] + "..."
    log(f"  $ {short}")
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log(f"  TIMEOUT ({timeout}s)")
        if check:
            raise
        return subprocess.CompletedProcess(cmd, 1, "timeout", "")

    if r.returncode != 0:
        tail = r.stdout[-500:] if r.stdout else "(no output)"
        if check:
            log(f"  FAILED (rc={r.returncode}):\n{tail}")
            raise RuntimeError(f"Command failed (rc={r.returncode}): {cmd}")
        else:
            log(f"  warn rc={r.returncode}")
    return r


def disk_free_gb():
    st = os.statvfs("/")
    return (st.f_bavail * st.f_frsize) / (1024 ** 3)


def disk_info():
    r = subprocess.run("df -h /", shell=True, capture_output=True, text=True)
    lines = r.stdout.strip().split("\n")
    return lines[-1] if lines else "?"


# ── repo management ─────────────────────────────────────────

def clone_repo(repo_url):
    """Clone GitHub repo once into /repos/{name}/."""
    name = repo_url.split("/")[1]
    dst = REPOS_DIR / name
    if dst.exists():
        log(f"  repo {name}/ exists — git fetch")
        run("git fetch --all -q", cwd=dst, check=False, timeout=120)
        return dst
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    log(f"  cloning {repo_url} …")
    run(f"git clone -q https://github.com/{repo_url}.git {dst}", timeout=600)
    return dst


def make_workcopy(repo_path, name, version, env_commit):
    """Reflink-copy the repo into /testbed/{name}__{version}/ and check out."""
    tag = f"{name}__{version}"
    dst = TESTBED_DIR / tag
    if dst.exists():
        log(f"  workcopy {tag} exists → checkout {env_commit[:10]}")
        run(f"git checkout -f {env_commit}", cwd=dst, check=False)
        run("git clean -fdx", cwd=dst, check=False, timeout=60)
        return dst
    TESTBED_DIR.mkdir(parents=True, exist_ok=True)
    # prefer reflink; fall back to regular copy
    r = run(f"cp --reflink=always -a {repo_path} {dst}", check=False)
    if r.returncode != 0:
        log("  reflink failed, plain copy")
        run(f"cp -a {repo_path} {dst}")
    run(f"git checkout -f {env_commit}", cwd=dst)
    run("git clean -fdx", cwd=dst, check=False, timeout=60)
    return dst


# ── conda helpers ───────────────────────────────────────────

_env_cache = None

def env_exists(env_name):
    global _env_cache
    if _env_cache is None:
        r = run(f"{CONDA} env list --json", check=False)
        if r.returncode == 0:
            _env_cache = {Path(p).name for p in json.loads(r.stdout).get("envs", [])}
        else:
            _env_cache = set()
    return env_name in _env_cache


def create_env(env_name, python_ver):
    if env_exists(env_name):
        log(f"  conda env {env_name} exists")
        return
    log(f"  conda create {env_name}  python={python_ver}")
    run(f"{CONDA} create -n {env_name} python={python_ver} -y -q", timeout=300)
    _env_cache.add(env_name)


# ── installation logic ──────────────────────────────────────

def strip_texlive(cmd):
    """Remove texlive* from apt-get to save ~500 MB."""
    if "texlive" not in cmd:
        return cmd
    for pkg in ("texlive-latex-extra", "texlive-fonts-recommended",
                "texlive-xetex", "texlive-luatex", "texlive"):
        cmd = re.sub(r"\b" + re.escape(pkg) + r"\b", "", cmd)
    return re.sub(r"  +", " ", cmd)


def install_env(env_name, spec, work_dir):
    """Run the full install sequence for one (repo, version)."""
    cr = f"{CONDA} run -n {env_name}"
    repo = spec["repo"]

    # 1. pre-install (sed patches, apt packages, downloads …)
    for cmd in spec.get("pre_install", []):
        if repo == "matplotlib/matplotlib":
            cmd = strip_texlive(cmd)
        run(cmd, cwd=work_dir, check=False, timeout=300)

    # 2. 'packages' field — several formats
    pkgs = spec.get("packages", "")
    if pkgs:
        if pkgs.strip() == "requirements.txt":
            rf = work_dir / "requirements.txt"
            if rf.exists():
                run(f"{cr} python -m pip install -r {rf}",
                    cwd=work_dir, check=False, timeout=600)
        elif pkgs.strip() == "environment.yml":
            if not spec.get("no_use_env"):
                yf = work_dir / "environment.yml"
                if yf.exists():
                    run(f"{CONDA} env update -n {env_name} -f {yf}",
                        cwd=work_dir, check=False, timeout=600)
        else:
            # space-separated spec like "'numpy==1.19' scipy pytest"
            clean = pkgs.replace("'", '"')
            run(f"{cr} python -m pip install {clean}",
                cwd=work_dir, check=False, timeout=600)

    # 3. pip_packages (pinned)
    pip_pkgs = spec.get("pip_packages", [])
    if pip_pkgs:
        joined = " ".join(f'"{p}"' for p in pip_pkgs)
        run(f"{cr} python -m pip install {joined}",
            cwd=work_dir, check=False, timeout=600)

    # 4. main install (usually  pip install -e .)
    inst = spec.get("install", "")
    if inst:
        # Some install commands need compilation → longer timeout
        tout = 1200 if repo in ("scikit-learn/scikit-learn",
                                 "matplotlib/matplotlib",
                                 "astropy/astropy") else 600
        run(f"{cr} {inst}", cwd=work_dir, check=True, timeout=tout)


# ── main ────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 install_envs.py env_specs.json "
              "[--repos django,sympy,...]")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        specs = json.load(f)

    # optional repo filter
    selected = None
    if "--repos" in sys.argv:
        idx = sys.argv.index("--repos")
        selected = set(sys.argv[idx + 1].split(","))

    # group by repo
    repo_specs = defaultdict(list)
    for spec in specs.values():
        repo_specs[spec["repo"]].append(spec)

    # order
    ordered = [r for r in REPO_PRIORITY if r in repo_specs]
    for r in repo_specs:
        if r not in ordered:
            ordered.append(r)
    if selected:
        ordered = [r for r in ordered
                   if r.split("/")[1] in selected or r in selected]

    total = sum(len(repo_specs[r]) for r in ordered)
    log(f"Plan: {total} envs across {len(ordered)} repos")
    log(f"Disk: {disk_info()}  ({disk_free_gb():.1f} GB free)")

    ok = 0
    fail = 0
    skipped = 0

    for repo in ordered:
        versions = repo_specs[repo]
        name = repo.split("/")[1]

        log(f"\n{'=' * 60}")
        log(f"REPO: {repo}  ({len(versions)} versions)")
        log(f"{'=' * 60}")

        free = disk_free_gb()
        if free < 2.0:
            log(f"STOP — only {free:.1f} GB free")
            skipped += sum(len(repo_specs[r]) for r in ordered[ordered.index(repo):])
            break

        try:
            repo_path = clone_repo(repo)
        except Exception as e:
            log(f"CLONE FAILED {repo}: {e}")
            fail += len(versions)
            continue

        for spec in sorted(versions, key=lambda s: s["version"]):
            ver = spec["version"]
            env_name = f"{name}__{ver}"
            log(f"\n--- {env_name}  Python {spec['python']} ---")

            free = disk_free_gb()
            if free < 1.5:
                log(f"SKIP {env_name}: {free:.1f} GB free")
                skipped += 1
                continue

            try:
                wd = make_workcopy(repo_path, name, ver, spec["env_commit"])
                create_env(env_name, spec["python"])
                install_env(env_name, spec, wd)
                log(f"  ✓ {env_name}")
                ok += 1
            except Exception as e:
                log(f"  ✗ {env_name}: {e}")
                fail += 1

        # house-keeping after each repo
        run(f"{CONDA} clean --all -y -q", check=False, timeout=120)
        run("rm -rf /root/.cache/pip /tmp/pip-*", check=False)
        log(f"After {name}: {disk_info()}")

    # final cleanup
    log(f"\n{'=' * 60}")
    log("Final cleanup …")
    run(f"{CONDA} clean --all -y -q", check=False)
    run("apt-get clean 2>/dev/null", check=False)
    run("rm -rf /root/.cache /tmp/pip-* /tmp/*.tgz", check=False)

    log(f"\nDone: {ok} ok, {fail} failed, {skipped} skipped")
    log(f"Disk: {disk_info()}")


if __name__ == "__main__":
    main()
