#!/usr/bin/env python3
"""measure_motiv_totals.py -- REMOTE (dyp) measurement for the motivation figure
(fig_motiv_combo, panel a) "total state" bars, over the SAME 43
moatless-swe-search instances panel (b) uses.

Panel (a) contrasts the per-step Delta (a MEAN: FS 1.4 KB over writing steps,
Mem 7.1 KB context/step) against the TOTAL state a full-duplication C/R copies
every checkpoint. To keep the ratio same-statistic, the totals are reported as
MEANS too (median printed alongside as a skew sanity-check).

    FS  total = repo source bytes (tracked source at base_commit; == the old
                "CodeIndex source" notion). Workload property -- the DeltaBox
                slim optimization is memory-only, so native == ours here.
    Mem total = NATIVE moatless worker RSS. The full worker loads the CodeIndex
                (faiss / llama_index / numpy embeddings) IN-PROCESS -> ~450-500
                MB (smaps_profile: django-11211 446 MB, sympy-13437 502 MB at
                after_init). This is NOT the ~45 MB slim worker, which is OUR
                optimization (CodeIndex moved to an external sidecar) and must
                not appear in a system-agnostic motivation.

This 1 GB dev box can do neither (no repos, no worker). Run on dyp, then paste
the "FIGURE VALUES" back to finalize plot_fig_motiv_combo.py.

What it does
------------
1. FS  : instance -> (repo, base_commit) from the committed SWE-bench Verified
         parquet (or --map JSON); bare-clone each unique repo once into --cache;
         sum `git ls-tree -r --long <base_commit>` blob sizes = tracked source
         bytes. No checkout, no Docker. -> mean/median over the 43.

2. RSS : NATIVE (non-slim) worker RSS via finalbench/deltabox_std/
         profile_worker_smaps.py (run WITHOUT --slim). Per instance it writes
         results/smaps_profile_<inst>_<ts>/smaps_after_init.json etc.; we take
         the steady-state RSS (max over after_init / after_step_*). With
         --run-rss this script drives those runs for any missing instance;
         otherwise it just parses whatever is already in --smaps-results.

Usage (on dyp, repo root):
    # generate native RSS profiles for the 43 (heavy; ~minutes each), then aggregate:
    python3 finalcode/finaltest/measure_motiv_totals.py \
        --cache /mnt/disk2/dyp/_repo_cache \
        --smaps-results finalbench/deltabox_std/results --run-rss
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics as st
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # repo root (.../d-overlayfs)

TRAJ_GLOB = os.path.join(ROOT, "traces/swe-search/qwen3-coder-30b-p-eagle-ms",
                         "mcts-iter30", "*")
PARQUET_GLOB = os.path.join(ROOT, "codescout-full/data/*/swe_bench_verified-*.parquet")
SMAPS_SCRIPT = os.path.join(ROOT, "finalbench/deltabox_std/profile_worker_smaps.py")


# ----------------------------------------------------------------------------- helpers
def instances_43():
    return sorted(os.path.basename(p.rstrip("/")) for p in glob.glob(TRAJ_GLOB)
                  if os.path.isdir(p))


def stats(vals):
    vals = sorted(vals)
    return dict(n=len(vals), mean=st.mean(vals), median=st.median(vals),
                lo=min(vals), hi=max(vals))


def load_repo_map(map_path=None):
    """instance_id -> (repo, base_commit)."""
    if map_path:
        d = json.load(open(map_path))
        return {k: (v["repo"], v["base_commit"]) for k, v in d.items()}
    pq = sorted(glob.glob(PARQUET_GLOB))
    if pq:
        try:
            import pandas as pd
            rows = pd.read_parquet(pq[0])[["instance_id", "repo", "base_commit"]]
            return {r["instance_id"]: (r["repo"], r["base_commit"])
                    for r in rows.to_dict("records")}
        except Exception:
            try:
                import pyarrow.parquet as paq
                t = paq.read_table(pq[0], columns=["instance_id", "repo", "base_commit"])
                cols = {c: t.column(c).to_pylist() for c in t.column_names}
                return {cols["instance_id"][i]: (cols["repo"][i], cols["base_commit"][i])
                        for i in range(t.num_rows)}
            except Exception as e:
                print(f"  (parquet unreadable: {e})", file=sys.stderr)
    try:
        from datasets import load_dataset
        ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
        return {r["instance_id"]: (r["repo"], r["base_commit"]) for r in ds}
    except Exception as e:
        sys.exit(f"could not resolve instance->repo map ({e}); pass --map JSON.")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ----------------------------------------------------------------------------- FS
def ensure_repo(repo, cache):
    path = os.path.join(cache, repo.replace("/", "__") + ".git")
    if not os.path.isdir(path):
        os.makedirs(cache, exist_ok=True)
        r = run(["git", "clone", "--bare", "--quiet",
                 f"https://github.com/{repo}.git", path])
        if r.returncode != 0:
            print(f"  clone FAIL {repo}: {r.stderr.strip()[:150]}", file=sys.stderr)
            return None
    return path


def tracked_source_bytes(repo_path, commit):
    r = run(["git", "-C", repo_path, "ls-tree", "-r", "--long", commit])
    if r.returncode != 0:
        run(["git", "-C", repo_path, "fetch", "--quiet", "origin", commit])
        r = run(["git", "-C", repo_path, "ls-tree", "-r", "--long", commit])
        if r.returncode != 0:
            return None
    total = 0
    for line in r.stdout.splitlines():
        parts = line.split("\t", 1)[0].split()      # <mode> blob <sha> <size>
        if len(parts) >= 4 and parts[1] == "blob" and parts[3].isdigit():
            total += int(parts[3])
    return total


def du_bytes(path, exclude_git=True):
    """Real on-disk size of an existing checkout (ground truth)."""
    if not os.path.isdir(path):
        return None
    cmd = ["du", "-sb"] + (["--exclude=.git"] if exclude_git else []) + [path]
    r = run(cmd)
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.split()[0])
    except (ValueError, IndexError):
        return None


def measure_fs(insts, repo_map, cache, repos_dir):
    """PRIMARY: du the real checkout dyp actually runs on (repos_dir/
    swe-bench_<inst>), .git excluded = the source tree the agent edits.
    FALLBACK: tracked source bytes at base_commit via a bare clone."""
    print(f"\n=== FS: real checkout size (du, .git excluded) over 43 ===")
    print(f"    repos_dir = {repos_dir}")
    per = {}
    for inst in insts:
        path = os.path.join(repos_dir, f"swe-bench_{inst}")
        nb = du_bytes(path, exclude_git=True)
        src = "du"
        if nb is None and inst in repo_map:           # fallback: git ls-tree
            repo, commit = repo_map[inst]
            rp = ensure_repo(repo, cache)
            nb = tracked_source_bytes(rp, commit) if rp else None
            src = "git"
        if nb is None:
            print(f"  {inst:<36} NO CHECKOUT / UNRESOLVED"); continue
        per[inst] = nb
        print(f"  {inst:<36} {nb/1e6:8.2f} MB  ({src})")
    if per:
        s = stats(list(per.values()))
        print(f"\n  FS total {s['n']}/{len(insts)}: mean={s['mean']/1e6:.1f} MB  "
              f"median={s['median']/1e6:.1f} MB  range={s['lo']/1e6:.1f}-{s['hi']/1e6:.1f} MB")
    return per


# ----------------------------------------------------------------------------- RSS (native)
def _profile_rss_mb(run_dir):
    """Steady-state native RSS for one profile run dir = max RSS over the
    after_init / after_step_* smaps phases (boot_ready excludes the CodeIndex)."""
    best = None
    for jf in glob.glob(os.path.join(run_dir, "smaps_*.json")):
        lab = os.path.basename(jf)[len("smaps_"):-len(".json")]
        if lab == "boot_ready":
            continue
        try:
            rss = json.load(open(jf)).get("total_kb", {}).get("Rss", 0) / 1024.0
        except Exception:
            continue
        if rss and (best is None or rss > best):
            best = rss
    return best


def _instance_of(run_dir):
    m = os.path.join(run_dir, "meta.json")
    if os.path.exists(m):
        try:
            inst = json.load(open(m)).get("instance")
            if inst:
                return inst, json.load(open(m)).get("slim", False)
        except Exception:
            pass
    return None, None


def measure_rss(insts, results_dir, run_rss, steps):
    print(f"\n=== RSS: NATIVE (non-slim) moatless worker, steady-state ===")
    run_dirs = sorted(d for d in glob.glob(os.path.join(results_dir, "smaps_profile_*"))
                      if os.path.isdir(d))
    if run_rss:
        have = set()
        for d in run_dirs:
            inst, slim = _instance_of(d)
            if inst and not slim:
                have.add(inst)
        for inst in insts:
            if inst in have:
                continue
            print(f"  [run] profile_worker_smaps.py --instance {inst} (non-slim)")
            r = run([sys.executable, SMAPS_SCRIPT, "--instance", inst,
                     "--steps", str(steps)])
            if r.returncode != 0:
                print(f"    FAILED: {r.stderr.strip()[-200:]}", file=sys.stderr)

    per = {}
    for d in sorted(glob.glob(os.path.join(results_dir, "smaps_profile_*"))):
        if not os.path.isdir(d):           # skip smaps_profile_summary.json etc.
            continue
        inst, slim = _instance_of(d)
        if not inst or slim or inst not in insts:
            continue
        rss = _profile_rss_mb(d)
        if rss:
            per[inst] = max(per.get(inst, 0), rss)     # keep the largest sample
    for inst in sorted(per):
        print(f"  {inst:<36} {per[inst]:7.1f} MB")
    miss = [i for i in insts if i not in per]
    if per:
        s = stats(list(per.values()))
        print(f"\n  RSS total {s['n']}/{len(insts)}: mean={s['mean']:.1f} MB  "
              f"median={s['median']:.1f} MB  range={s['lo']:.1f}-{s['hi']:.1f} MB")
    print(f"  missing native RSS ({len(miss)}): {miss}")
    if miss:
        print("  -> re-run with --run-rss (drives profile_worker_smaps.py, non-slim).")
    return per


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(ROOT, "_repo_cache"))
    ap.add_argument("--map", default=None)
    ap.add_argument("--repos-dir",
                    default=os.path.join(os.environ.get("SPR_PAYLOAD",
                                         "/mnt/disk2/dyp/spr_payload"), "repos"),
                    help="dir holding swe-bench_<inst> checkouts (du'd for FS)")
    ap.add_argument("--smaps-results",
                    default=os.path.join(ROOT, "finalbench/deltabox_std/results"))
    ap.add_argument("--run-rss", action="store_true",
                    help="drive profile_worker_smaps.py (non-slim) for missing instances")
    ap.add_argument("--steps", type=int, default=2)
    ap.add_argument("--out", default=os.path.join(HERE, "motiv_totals.json"))
    a = ap.parse_args()

    insts = instances_43()
    print(f"43-trajectory instances: {len(insts)}")
    repo_map = load_repo_map(a.map) if (a.map or not os.path.isdir(a.repos_dir)) else {}
    fs = measure_fs(insts, repo_map, a.cache, a.repos_dir)
    rss = measure_rss(insts, a.smaps_results, a.run_rss, a.steps)

    out = {"instances": insts, "fs_bytes": fs, "rss_mb": rss}
    if fs:
        s = stats(list(fs.values()))
        out["fs_mean_mb"] = s["mean"] / 1e6
        out["fs_median_mb"] = s["median"] / 1e6
        out["fs_n"] = s["n"]
    if rss:
        s = stats(list(rss.values()))
        out["rss_mean_mb"] = s["mean"]
        out["rss_median_mb"] = s["median"]
        out["rss_n"] = s["n"]

    print("\n" + "=" * 64)
    print("FIGURE VALUES (MEAN -> consumed directly by plot_fig_motiv_combo.py):")
    if "fs_mean_mb" in out:
        print(f"  Filesystem total = {out['fs_mean_mb']:.1f} MB   "
              f"(mean, n={out['fs_n']}/43; median {out['fs_median_mb']:.1f})")
    if "rss_mean_mb" in out:
        print(f"  Memory     total = {out['rss_mean_mb']:.1f} MB   "
              f"(mean, n={out['rss_n']}/43; median {out['rss_median_mb']:.1f})")
    print("=" * 64)

    json.dump(out, open(a.out, "w"), indent=1, default=str)
    # also drop a copy next to the figure so the plot picks it up automatically
    figdir = os.path.join(ROOT, "rollbackable-sandbox-paper", "figs")
    if os.path.isdir(figdir):
        json.dump(out, open(os.path.join(figdir, "motiv_totals.json"), "w"),
                  indent=1, default=str)
    print(f"wrote {a.out} (+ figs/motiv_totals.json)")


if __name__ == "__main__":
    main()
