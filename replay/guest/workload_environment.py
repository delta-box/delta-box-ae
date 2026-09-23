"""Prepare explicitly identified workload environments before checkpoint timing.

The two entries below repair previously observed environment failures. They are
metadata, not test-result overrides. Their source is spr4numa's swe-bench.json
(SHA256 7303cc5795e3707162f9b0ffcc5694f3fd67e20bd9d514cfdce63146fdebc196).
Other workloads retain their existing environment until separately validated.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time


WORKLOADS = {
    "django__django-14672": {
        "version": "4.0", "repository": "django",
        "base_commit": "00ea883ef56fb5e092cbe4a6f7ff2e7470886ac4",
        "imports": ["pytest", "asgiref", "sqlparse", "pytz", "django.test"],
        "test_runner": "django",
        "build_extensions": False,
    },
    "astropy__astropy-14182": {
        "version": "5.1", "repository": "astropy",
        "base_commit": "a5917978be39d13cd90b517e1de4e7a539ffaa48",
        "imports": ["pytest", "numpy", "setuptools_scm", "Cython", "extension_helpers"],
        "build_extensions": True,
    },
}


def workload_for(instance_id: str, base_commit: str) -> dict | None:
    profile = WORKLOADS.get(instance_id)
    if profile is None:
        return None
    if base_commit != profile["base_commit"]:
        raise ValueError(f"workload metadata commit mismatch for {instance_id}")
    return dict(profile)


def prepare_workload(profile: dict, root: str = "/testbed",
                     environments: str = "/opt/miniconda3/envs",
                     evidence: str = "/tmp/workload-environment.json") -> dict:
    """Use preinstalled dependencies; never pip-install or download in a run."""
    python = Path(environments) / (profile["repository"] + "__" + profile["version"]) / "bin/python"
    if not python.is_file():
        raise RuntimeError(f"missing workload interpreter: {python}")
    env = dict(os.environ, PYTHONNOUSERSITE="1")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    probe = (
        "import importlib,sys,json,importlib.metadata as m; "
        "[importlib.import_module(x) for x in json.loads(sys.argv[1])]; "
        "print(json.dumps({'python':sys.version,'executable':sys.executable,"
        "'packages':sorted((d.metadata['Name'],d.version) for d in m.distributions())}))"
    )
    started = time.monotonic()
    record = {"profile": profile, "status": "preparing",
              "timing": "before agent start and all checkpoint/restore timing",
              "network": "offline; preinstalled dependencies only"}
    target = Path(evidence)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        record["environment"] = json.loads(subprocess.check_output(
            [str(python), "-c", probe, json.dumps(profile["imports"])],
            cwd=root, env=env, text=True, timeout=60))
        if profile.get("test_runner") == "django":
            command = [str(python), "-c",
                "import sys,os; sys.path.insert(0, 'tests'); "
                "os.environ['DJANGO_SETTINGS_MODULE']='test_sqlite'; "
                "from runtests import setup_run_tests,teardown_run_tests; "
                "labels,state=setup_run_tests(0,None,None,['many_to_many']); "
                "print('Django native runner ready:', labels); teardown_run_tests(state)"]
            record["test_runner_probe"] = command
            record["test_runner_probe_output"] = subprocess.check_output(
                command, cwd=root, env=env, text=True, stderr=subprocess.STDOUT, timeout=60)
        if profile["build_extensions"]:
            command = [str(python), "setup.py", "build_ext", "--inplace", "-j", "2"]
            record["build_command"] = command
            # git clean has already removed artifacts from the image's different
            # setup commit. Build only from the exact recorded source checkout.
            with target.with_suffix(".build.log").open("w") as log:
                subprocess.run(command, cwd=root, env=env, stdout=log,
                               stderr=subprocess.STDOUT, check=True, timeout=1200)
        record["status"] = "ready"
    except BaseException as error:
        record.update(status="failed", error=str(error))
        raise
    finally:
        record["preparation_s"] = time.monotonic() - started
        target.write_text(json.dumps(record, indent=2) + "\n")
    os.environ["AGENT_WORKER_PYTHON"] = str(python)
    os.environ["AGENT_WORKER_TEST_RUNNER"] = profile.get("test_runner", "pytest")
    return record
