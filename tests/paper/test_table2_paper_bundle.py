"""Original Table 2 import is source-locked and cannot overwrite later inputs."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from ae.repro import analysis
from ae.scripts import paper_data


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Table2PaperBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / "paper/table-02"
        self.directory.mkdir(parents=True)
        self.records = {}
        self.rows = []
        self.locked = []
        for i in range(2):
            instance, run_id = f"django__django-{i}", f"replay-original-{i}"
            name = instance + "." + run_id + ".results.jsonl"
            events = [dict(kind="ckpt", ckpt_wall_ms=i+2),
                      dict(kind="restore", restore_wall_ms=1, restore_critical_ms=1),
                      dict(kind="run_summary", error_n=0, worker_exec_bad_n=0, instance=instance)]
            raw = "".join(json.dumps(e)+"\n" for e in events).encode()
            self.records[name] = raw
            self.locked.append(dict(instance=instance, run_id=run_id, raw_sha256=sha(raw), raw_bytes=len(raw),
                                    checkpoint_events=1, restore_events=1))
            self.rows.append(dict(instance=instance, run_id=run_id, sha256=sha(raw), bytes=len(raw),
                             target="paper/table-02/data/records/deltabox-paper/results/"+name,
                             member="table2-paper-source/records/"+name+".gz"))
        self.lock_path = self.directory / "paper-source-lock.json"
        self.lock_path.write_text(json.dumps(dict(schema_version=1, dataset_id="fixture-original",
                                        expected_counts=dict(runs=2), runs=self.locked)))
        self.index = dict(format=1, dataset_id="fixture-original", repository_path="datasets/original.tar.gz", files=self.rows)
        self.index_path = self.directory / "paper-source-bundle.json"
        self.bundle = self.root / self.index["repository_path"]
        self.bundle.parent.mkdir()
        self.write_bundle()
        (self.directory / "cohort-deltabox.csv").write_text("instance,n_ckpt,n_restore\n" +
                   "".join(row["instance"]+",1,1\n" for row in self.rows))
        # A later batch has the same instances and event counts but different runs/values.
        self.later = self.directory / "data/records/deltabox-fast/results"
        self.later.mkdir(parents=True)
        for name,raw in self.records.items():
            (self.later / name.replace("original", "later")).write_bytes(raw.replace(b'"ckpt_wall_ms": 2', b'"ckpt_wall_ms": 9'))
        self.later_before = {p.name:p.read_bytes() for p in self.later.iterdir()}

    def write_bundle(self):
        with tarfile.open(self.bundle, "w:gz") as tar:
            for row in self.rows:
                raw = gzip.compress(self.records[Path(row["target"]).name])
                row.update(gzip_sha256=sha(raw), gzip_bytes=len(raw))
                member = tarfile.TarInfo(row["member"])
                member.size = len(raw)
                tar.addfile(member, io.BytesIO(raw))
        self.index.update(sha256=sha(self.bundle.read_bytes()), compressed_bytes=self.bundle.stat().st_size)
        self.write_index()

    def write_index(self):
        self.index_path.write_text(json.dumps(self.index))

    def test_import_then_verify_is_idempotent_and_preserves_later_batch(self):
        first = paper_data.table2_paper_source(self.root, install=True)
        self.assertEqual(first["verified_objects"], 2)
        self.assertEqual(first, paper_data.table2_paper_source(self.root, install=True))
        self.assertEqual(first, paper_data.table2_paper_source(self.root))
        self.assertEqual(self.later_before, {p.name:p.read_bytes() for p in self.later.iterdir()})
        for row in self.rows:
            self.assertTrue((self.root/row["target"]).is_symlink())
            self.assertEqual((self.root/row["target"]).read_bytes(), self.records[Path(row["target"]).name])

    def test_replaced_bundle_is_rejected(self):
        with self.bundle.open("ab") as f:
            f.write(b"changed")
        with self.assertRaisesRegex(RuntimeError, "bundle SHA-256/size"):
            paper_data.table2_paper_source(self.root, install=True)

    def test_rehashed_index_cannot_override_fixed_lock(self):
        row = self.rows[0]
        self.records[Path(row["target"]).name] += b"\n"
        row.update(sha256=sha(self.records[Path(row["target"]).name]), bytes=len(self.records[Path(row["target"]).name]))
        self.write_bundle()
        with self.assertRaisesRegex(RuntimeError, "fixed run/content lock"):
            paper_data.table2_paper_source(self.root, install=True)

    def test_rehashed_bundle_with_wrong_raw_bytes_is_rejected(self):
        self.records[Path(self.rows[0]["target"]).name] += b"\n"
        self.write_bundle()
        with self.assertRaisesRegex(RuntimeError, "raw content differs"):
            paper_data.table2_paper_source(self.root, install=True)

    def test_unsafe_target_is_rejected(self):
        self.rows[0]["target"] = "paper/table-02/data/../../outside"
        self.write_index()
        with self.assertRaisesRegex(RuntimeError, "fixed run/content lock"):
            paper_data.table2_paper_source(self.root, install=True)

    def test_existing_wrong_regular_file_is_not_overwritten(self):
        path = self.root / self.rows[0]["target"]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"existing data")
        with self.assertRaisesRegex(RuntimeError, "blocks materialization"):
            paper_data.table2_paper_source(self.root, install=True)
        self.assertEqual(path.read_bytes(), b"existing data")

    def test_existing_corrupt_object_is_not_overwritten(self):
        path = self.root / "traces/objects" / self.rows[0]["sha256"]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"existing corrupt object")
        with self.assertRaisesRegex(RuntimeError, "Existing Table 2 object differs"):
            paper_data.table2_paper_source(self.root, install=True)
        self.assertEqual(path.read_bytes(), b"existing corrupt object")

    def test_default_uses_original_and_fresh_selection_keeps_later(self):
        paper_data.table2_paper_source(self.root, install=True)
        ev = analysis.Evidence(self.root/"paper", "archived")
        selected, selection = analysis.select_delta(ev)
        with patch.object(analysis, "TABLE2_PAPER_SOURCE_LOCK", self.lock_path):
            identity = analysis.archived_table2_source_identity(ev, selected)
        self.assertTrue(identity["paper_source_match"])
        self.assertEqual(selection["complete_runs"], 2)
        self.assertTrue(all("deltabox-paper/" in row["path"] for row in selected))
        self.assertTrue(all(ev.sources[row["path"]]["manifest_verified"] for row in selected))
        fresh, _ = analysis.select_delta(analysis.Evidence(self.root/"paper", "fresh"))
        self.assertTrue(all("deltabox-fast/" in row["path"] for row in fresh))
        other = self.root / "paper/table-03"
        other.mkdir()
        (other/"cohort-deltabox.csv").write_bytes((self.directory/"cohort-deltabox.csv").read_bytes())
        (other/"data").symlink_to(self.directory/"data", target_is_directory=True)
        table3, _ = analysis.select_delta(ev, "table-03")
        self.assertTrue(all("deltabox-fast/" in row["path"] for row in table3))

    def test_missing_original_does_not_fall_back_to_same_count_later(self):
        ev = analysis.Evidence(self.root/"paper", "archived")
        with self.assertRaisesRegex(ValueError, "Missing original Table 2 paper records"):
            analysis.select_delta(ev)

    def test_default_rejects_later_even_if_index_and_counts_are_replaced(self):
        paper_data.table2_paper_source(self.root, install=True)
        first = self.root / self.rows[0]["target"]
        first.unlink()
        wrong = next(iter(self.later_before.values()))
        first.write_bytes(wrong)
        self.rows[0]["sha256"] = sha(wrong)
        self.write_index()
        ev = analysis.Evidence(self.root/"paper", "archived")
        with patch.object(analysis, "TABLE2_PAPER_SOURCE_LOCK", self.lock_path):
            with self.assertRaisesRegex(ValueError, "does not match the original paper run/content lock"):
                analysis.table2(ev)


if __name__ == "__main__":
    unittest.main()
