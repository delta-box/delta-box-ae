"""Check coupled filesystem and process rollback with the shipped DeltaBox runtime."""
import os
from pathlib import Path
import sys
import time
import tempfile

sys.path.insert(0, "/repo")
from backends.deltabox.gsd.root_sandbox import RootOverlaySandbox

sandbox = RootOverlaySandbox("/mnt/base", tempfile.mkdtemp(prefix="deltabox-check-", dir="/dev/shm"))
sandbox.mount()
try:
    sandbox.launch()
    def run(command):
        response = sandbox.exec(command, timeout=10)
        if not response.startswith("rc=0\n"):
            raise RuntimeError(response)
        return response.split("\n", 1)[1].rstrip("\n")

    run("echo saved >/branch-state")
    before = sandbox.heartbeat()
    sandbox.checkpoint("parent")
    run("echo changed >/branch-state && touch /child-only")
    time.sleep(1.5)
    advanced = sandbox.heartbeat()
    if not advanced > before:
        raise RuntimeError("Worker counter did not advance after checkpoint")
    sandbox.restore("parent")
    observed = sandbox.heartbeat()
    contents = run("cat /branch-state && test ! -e /child-only")
    time.sleep(.6)
    resumed = sandbox.heartbeat()
    if contents != "saved" or not before <= observed < advanced or not observed < resumed:
        raise RuntimeError(f"Rollback mismatch: {contents}, counters {before}, {observed}, {advanced}, {resumed}")
    print(f"PASS coupled rollback: saved file restored, child file absent, worker counter {advanced} -> {observed} -> {resumed}")
finally:
    for logfile in Path(sandbox.img_root).rglob("*.log"):
        lines = logfile.read_text(errors="replace").splitlines()
        for line in lines:
            if "Error" in line or "finished successfully" in line:
                print(line)
    sandbox.teardown()
