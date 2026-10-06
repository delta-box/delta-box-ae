"""Cross-repository coordination contracts, without hosted services or hardware."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from ae.repro.coordination import coordination_root


ENVIRONMENT_KEY = "AE_HOSTED_COORDINATION_ROOT"
RECOVERY_MARKERS = (
    "CPU_SERVICE_RECOVERY_REQUIRED.json",
    "CPU_BACKGROUND_TRANSACTION.json",
    "E2B_SERVICE_RECOVERY_REQUIRED.json",
)


class HostedCoordinationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ae-coordination-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.old_repo = self.directory / "public-repo"
        self.new_repo = self.directory / "a12-repo"
        self.shared_root = self.directory / "shared-coordination"
        for path in (self.old_repo, self.new_repo, self.shared_root):
            path.mkdir()
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(ENVIRONMENT_KEY, None)

    def run_child(self, code, repository):
        return subprocess.run(
            [sys.executable, "-c", code, str(REPO), str(repository)],
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            close_fds=True,
            timeout=10,
            check=False,
        )

    def test_default_preserves_each_repository_work_directory(self):
        self.assertEqual(coordination_root(self.old_repo), self.old_repo / "ae/work")
        self.assertEqual(coordination_root(self.new_repo), self.new_repo / "ae/work")
        self.assertNotEqual(
            coordination_root(self.old_repo), coordination_root(self.new_repo)
        )

    def test_absolute_override_is_shared_by_distinct_repositories(self):
        os.environ[ENVIRONMENT_KEY] = str(self.shared_root)
        self.assertEqual(coordination_root(self.old_repo), self.shared_root)
        self.assertEqual(coordination_root(self.new_repo), self.shared_root)

    def test_relative_overrides_are_rejected(self):
        for value in ("relative", "ae/work", ".", "../coordination"):
            with self.subTest(value=value):
                os.environ[ENVIRONMENT_KEY] = value
                with self.assertRaises(ValueError):
                    coordination_root(self.new_repo)

    def test_absolute_overrides_containing_parent_traversal_are_rejected(self):
        for value in (
            str(self.shared_root) + "/../other",
            str(self.shared_root) + "/child/../../other",
            str(self.shared_root) + "/..",
        ):
            with self.subTest(value=value):
                os.environ[ENVIRONMENT_KEY] = value
                with self.assertRaises(ValueError):
                    coordination_root(self.new_repo)

    def test_results_gate_excludes_an_independently_opened_child_process_fd(self):
        os.environ[ENVIRONMENT_KEY] = str(self.shared_root)
        gate = coordination_root(self.old_repo) / ".results.lock"
        child_code = """
import fcntl
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from ae.repro.coordination import coordination_root
gate = coordination_root(Path(sys.argv[2])) / ".results.lock"
with gate.open("a+") as stream:
    info = os.fstat(stream.fileno())
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        status = "blocked"
    else:
        status = "acquired"
    print(json.dumps({"status": status, "pid": os.getpid(),
                      "device": info.st_dev, "inode": info.st_ino}))
"""
        with gate.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            owner = os.fstat(stream.fileno())
            blocked = self.run_child(child_code, self.new_repo)
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            observation = json.loads(blocked.stdout)
            self.assertEqual(observation["status"], "blocked")
            self.assertNotEqual(observation["pid"], os.getpid())
            self.assertEqual(
                (observation["device"], observation["inode"]),
                (owner.st_dev, owner.st_ino),
            )

        acquired = self.run_child(child_code, self.new_repo)
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        observation = json.loads(acquired.stdout)
        self.assertEqual(observation["status"], "acquired")
        self.assertEqual(
            (observation["device"], observation["inode"]),
            (owner.st_dev, owner.st_ino),
        )

    def test_recovery_markers_are_visible_from_either_repository(self):
        os.environ[ENVIRONMENT_KEY] = str(self.shared_root)
        for writer_repo, reader_repo in (
            (self.old_repo, self.new_repo),
            (self.new_repo, self.old_repo),
        ):
            for marker in RECOVERY_MARKERS:
                with self.subTest(writer=writer_repo.name, marker=marker):
                    payload = {"writer": writer_repo.name, "marker": marker}
                    written = coordination_root(writer_repo) / marker
                    written.write_text(json.dumps(payload), encoding="utf-8")
                    observed = coordination_root(reader_repo) / marker
                    self.assertTrue(written.samefile(observed))
                    self.assertEqual(
                        json.loads(observed.read_text(encoding="utf-8")), payload
                    )
                    observed.unlink()
                    self.assertFalse(written.exists())

    def test_failed_process_leaves_recovery_markers_for_the_other_repository(self):
        os.environ[ENVIRONMENT_KEY] = str(self.shared_root)
        child_code = """
import json
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from ae.repro.coordination import coordination_root
root = coordination_root(Path(sys.argv[2]))
for marker in (
    "CPU_SERVICE_RECOVERY_REQUIRED.json",
    "CPU_BACKGROUND_TRANSACTION.json",
    "E2B_SERVICE_RECOVERY_REQUIRED.json",
):
    (root / marker).write_text(
        json.dumps({"status": "recovery-required", "marker": marker}),
        encoding="utf-8",
    )
raise RuntimeError("simulated failure after persisting recovery evidence")
"""
        failed = self.run_child(child_code, self.new_repo)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("simulated failure after persisting recovery evidence", failed.stderr)
        for marker in RECOVERY_MARKERS:
            with self.subTest(marker=marker):
                observed = coordination_root(self.old_repo) / marker
                self.assertEqual(
                    json.loads(observed.read_text(encoding="utf-8")),
                    {"status": "recovery-required", "marker": marker},
                )


if __name__ == "__main__":
    unittest.main()
