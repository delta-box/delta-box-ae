"""The review presenter must never substitute archive values or mix populations."""
import base64
import copy
import importlib.util
import json
import re
from pathlib import Path
import tempfile
import unittest

from ae.scripts.build_review_comparison import (
    BOUNDARIES_ZH, ITEMS, PAPER_SHA256, brief_population, build, coverage_lines, digest, missing_details, sample_lines,
    sample_records, verify_inputs,
)

PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGAAAAABJRU5ErkJggg==")


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class ReviewComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paper = self.root / "paper"
        self.paper.mkdir()
        artifacts = []
        for key in ITEMS:
            path = self.paper / (key + ".png")
            path.write_bytes(PIXEL)
            artifacts.append(dict(file=path.name, sha256=digest(path), pdf_page=2))
        write(self.paper / "manifest.json", dict(source_sha256=PAPER_SHA256, artifacts=artifacts))
        self.coverage = self.root / "review.json"
        write(self.coverage, dict(schema_version=2, release=dict(source_commit="a" * 40, source_sha256="b" * 64),
                                 coverage=[dict(experiment="table-02-deltabox", status="partial", planned_jobs=12,
                                                successful_jobs=1, failed_jobs=1, reasons=["Remaining inputs unavailable"])]))
        self.summary = self.root / "analysis/summary.json"
        self.plots = self.root / "plots/plots.json"
        self.plots.parent.mkdir()
        self.picture = self.plots.parent / "table-02.png"
        self.picture.write_bytes(PIXEL)
        self.document = dict(schema_version=1, source="fresh", experiments={"table-02": dict(
            metrics=[dict(backend="deltabox", group="All", metric="checkpoint_ms", value=8, n=29,
                          unit="ms", plot_group="profile-a", cohort="cohort-a"),
                     dict(backend="deltabox", group="Tools/Small", metric="checkpoint_ms", value=8, n=29,
                          unit="ms", plot_group="profile-a", cohort="cohort-a")], series=[])} )
        self.write_summary()

    def tearDown(self):
        self.temp.cleanup()

    def write_summary(self):
        write(self.summary, self.document)
        write(self.plots, dict(schema_version=1, source="fresh", input_sha256=digest(self.summary),
                              artifacts=[dict(experiment="table-02", population="profile-a",
                                              path="/previous/server/table-02.png", sha256=digest(self.picture))]))

    def assert_bilingual_pages(self, output, manifest):
        english = (output / "README.md").read_text()
        chinese = (output / "README-zh.md").read_text()
        self.assertIn("[简体中文](README-zh.md)", english)
        self.assertIn("[English](README.md)", chinese)
        self.assertTrue(english.startswith("# This run's AE results"))
        self.assertTrue(chinese.startswith("# 本次 AE 结果"))
        self.assertEqual(set(BOUNDARIES_ZH), set(ITEMS))
        for item in manifest["items"]:
            self.assertIn("## " + item["paper_item"], english)
            self.assertIn("## " + item["paper_item"], chinese)
            self.assertIn(item["timing_boundary"], english)
            self.assertIn(BOUNDARIES_ZH[item["experiment"]], chinese)
        targets = []
        for text in (english, chinese):
            links = re.findall(r'\]\(([^)]+)\)', text)
            for link in links:
                self.assertTrue((output / link).is_file(), link)
            targets.append([link for link in links if not link.endswith(".md")])
        self.assertEqual(targets[0], targets[1])
        expected = [artifact["path"] for item in manifest["items"] for artifact in item["artifacts"]
                    if artifact["kind"] == "comparison" or artifact["path"].endswith("-ae.png")]
        self.assertCountEqual(targets[0], expected)
        self.assertEqual(english.count("No measured plot is available"),
                         sum(item["status"] == "unavailable" for item in manifest["items"]))
        self.assertEqual(chinese.count("本项没有可用的本次测量图"),
                         sum(item["status"] == "unavailable" for item in manifest["items"]))

    def verify(self):
        return verify_inputs(self.summary, self.plots, self.coverage, self.paper)

    def test_relocated_plot_is_verified_by_its_recorded_bytes(self):
        result = self.verify()
        self.assertEqual(result[1][0]["resolved_path"], str(self.picture.resolve()))

    def test_rejects_archived_summary_even_with_valid_hash(self):
        self.document["source"] = "archived"
        self.write_summary()
        with self.assertRaisesRegex(ValueError, "source=fresh"):
            self.verify()

    def test_rejects_changed_summary_and_plot(self):
        self.summary.write_text(self.summary.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.verify()
        self.write_summary()
        self.picture.write_bytes(PIXEL + b"changed")
        with self.assertRaisesRegex(ValueError, "changed after rendering"):
            self.verify()

    def test_rejects_unrelated_population_and_changed_paper(self):
        manifest = json.loads(self.plots.read_text())
        manifest["artifacts"][0]["population"] = "other-run"
        write(self.plots, manifest)
        with self.assertRaisesRegex(ValueError, "population is absent"):
            self.verify()
        self.write_summary()
        (self.paper / "figure-06.png").write_bytes(PIXEL + b"changed")
        with self.assertRaisesRegex(ValueError, "changed original"):
            self.verify()

    def test_sample_counts_preserve_population_and_overlapping_denominators(self):
        result = copy.deepcopy(self.document["experiments"]["table-02"])
        result["metrics"].append(dict(plot_group="other-profile", n=1000000))
        records = sample_records("table-02", result, "profile-a")
        self.assertEqual([row["n"] for row in records], [29, 29])
        labels = sample_lines("table-02", records)
        self.assertEqual(len(labels), 2)
        self.assertTrue(all("n=29" in line for line in labels))
        self.assertFalse(any("58" in line or "1000000" in line for line in labels))

    def test_paper_layout_retains_each_population_and_rejects_missing_or_forged_groups(self):
        rows = self.document['experiments']['table-02']['metrics']
        rows.append(dict(rows[0], backend='cube', plot_group='profile-b', n=17))
        self.write_summary()
        manifest = json.loads(self.plots.read_text())
        item = manifest['artifacts'][0]
        item.update(layout='paper', population=None, populations=['profile-a', 'profile-b'])
        write(self.plots, manifest)
        self.verify()
        samples = sample_records('table-02', self.document['experiments']['table-02'], item['populations'])
        self.assertEqual([row['n'] for row in samples], [29, 29, 17])
        self.assertEqual({row['plot_group'] for row in samples}, {'profile-a', 'profile-b'})
        for groups in (['profile-a'], ['profile-a', 'other'], ['profile-a', 'profile-a'], []):
            item['populations'] = groups
            write(self.plots, manifest)
            with self.assertRaises(ValueError):
                self.verify()

    @unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'Plotting extra is not installed')
    def test_paper_layout_image_does_not_contain_a_population_dashboard(self):
        from PIL import Image
        manifest = json.loads(self.plots.read_text())
        manifest['artifacts'][0].update(layout='paper', population=None, populations=['profile-a'])
        write(self.plots, manifest)
        output = self.root / 'paper-comparison'
        result = build(self.summary, self.plots, coverage_path=self.coverage,
                       output=output, paper_dir=self.paper)
        self.assert_bilingual_pages(output, result)
        item = result['items'][0]
        self.assertEqual(item['layout'], 'paper')
        self.assertEqual(item['populations'][0]['populations'], ['profile-a'])
        self.assertEqual(item['populations'][0]['samples'][0]['n'], 29)
        with Image.open(output / 'table-02-ae.png') as picture:
            self.assertEqual(picture.size, (1, 1))

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Plotting extra is not installed")
    def test_gpu_supplement_is_verified_and_copied_into_both_language_pages(self):
        folder = self.root / "gpu"
        folder.mkdir()
        artifacts = []
        for extension in (".png", ".pdf"):
            path = folder / ("figure-08b" + extension)
            path.write_bytes(PIXEL if extension == ".png" else b"PDF fixture, copy only")
            artifacts.append(dict(path=str(path), sha256=digest(path), bytes=path.stat().st_size))
        source = self.root / "figure08.json"
        supplement = dict(schema_version=1, release=json.loads(self.coverage.read_text())['release'], panels=[
            dict(experiment="figure-08-gpu", title="Figure 8(b)", status="ok", reasons=[], artifacts=artifacts),
            dict(experiment="figure-08-theory", title="Figure 8(c)", status="failed",
                 reasons=["Missing CPU measurements"], artifacts=[])])
        write(source, supplement)
        output = self.root / "with-gpu"
        manifest = build(coverage_path=self.coverage, output=output, paper_dir=self.paper, figure08_path=source)
        for name in ("README.md", "README-zh.md"):
            text = (output / name).read_text()
            self.assertIn("### Figure 8(b)", text)
            self.assertIn("### Figure 8(c)", text)
            self.assertIn("(figure-08b.png)", text)
            self.assertIn("Missing CPU measurements", text)
            for link in re.findall(r'\]\(([^)]+)\)', text):
                self.assertTrue((output / link).is_file(), link)
        self.assertEqual((output / "figure-08b.png").read_bytes(), PIXEL)
        self.assertEqual(manifest["figure08"]["panels"][0]["artifacts"][0]["path"], "figure-08b.png")
        other = copy.deepcopy(supplement)
        other["release"]["source_sha256"] = "c" * 64
        write(source, other)
        with self.assertRaisesRegex(ValueError, "another measurement source"):
            build(coverage_path=self.coverage, output=self.root / "wrong-source", paper_dir=self.paper,
                  figure08_path=source)
        self.assertFalse((self.root / "wrong-source").exists())
        write(source, supplement)
        (folder / "figure-08b.png").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Changed Figure 8"):
            build(coverage_path=self.coverage, output=self.root / "rejected", paper_dir=self.paper,
                  figure08_path=source)
        self.assertFalse((self.root / "rejected").exists())

    def test_composite_and_legacy_images_cannot_repeat_the_same_population(self):
        manifest = json.loads(self.plots.read_text())
        legacy = dict(manifest['artifacts'][0])
        manifest['artifacts'][0].update(layout='paper', population=None, populations=['profile-a'])
        manifest['artifacts'].append(legacy)
        write(self.plots, manifest)
        with self.assertRaisesRegex(ValueError, 'Duplicate plot population'):
            self.verify()

    def test_coverage_is_independent_from_measurement_count(self):
        row = json.loads(self.coverage.read_text())["coverage"][0]
        text = " ".join(coverage_lines([row]))
        self.assertIn("planned=12", text)
        self.assertIn("passed=1", text)
        self.assertIn("failed=1", text)

    def test_chinese_execution_status_preserves_counts_and_raw_diagnostics(self):
        rows = [dict(experiment="figure-06-memory", status="partial",
                     planned_jobs=4, successful_jobs=2, failed_jobs=["one", "two"],
                     reasons=["original diagnostic"],
                     unavailable_arms=[dict(arm="warm")])]
        text = "\n".join(coverage_lines(rows, language="zh"))
        self.assertIn("部分完成（partial）", text)
        self.assertIn("计划=4, 成功=2, 失败=2", text)
        self.assertIn("original diagnostic", text)
        self.assertIn("不可用实验臂 warm: 详见运行记录", text)
        self.assertIn("没有对应的运行记录", coverage_lines([], language="zh")[0])

    def test_complete_panels_are_not_presented_as_missing(self):
        self.assertEqual(missing_details(dict(panels={"filesystem": {"status": "analyzed"}, "memory": {"status": "analyzed"}})), {})
        self.assertEqual(missing_details(dict(panels={"a": {"status": "unavailable"}, "b": {"status": "analyzed"}})),
                         {"panels": {"a": {"status": "unavailable"}}})
        label = brief_population('experiment="figure-02-filesystem";mode=null;run_purpose="quick-check"')
        self.assertIn("figure-02-filesystem", label)
        self.assertNotIn("mode=null", label)

    def test_per_arm_counts_remain_identifiable(self):
        labels = sample_lines("figure-06", [dict(row_type="metrics", arm=arm, metric="checkpoint_ms", n=2)
                                            for arm in ("standard", "adaptive")])
        self.assertEqual(labels, ["adaptive/checkpoint_ms: n=2", "standard/checkpoint_ms: n=2"])

    def test_explicit_unavailable_arm_is_visible_even_if_remaining_jobs_pass(self):
        lines = coverage_lines([dict(experiment="figure-06-memory", status="ok", successful_jobs=3,
                                    unavailable_arms=[dict(arm="warm", reason="Disabled pending correctness fix")])])
        self.assertIn("Unavailable arm warm", " ".join(lines))
        self.assertIn("Disabled pending correctness fix", " ".join(lines))

    def test_analysis_and_plots_must_be_paired(self):
        with self.assertRaisesRegex(ValueError, "supplied together"):
            verify_inputs(self.summary, None, self.coverage, self.paper)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Plotting extra is not installed")
    def test_all_missing_items_are_explicit_and_keep_originals(self):
        output = self.root / "comparison"
        result = build(coverage_path=self.coverage, output=output, paper_dir=self.paper)
        self.assert_bilingual_pages(output, result)
        self.assertEqual(result["source"], "no-fresh-results")
        self.assertEqual(len(result["items"]), 7)
        self.assertTrue(all(item["status"] == "unavailable" and not item["populations"] for item in result["items"]))
        self.assertEqual(len(list(output.glob("*.png"))), 14)
        self.assertTrue((output / "figure-08-cpu-ae.png").exists())

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Plotting extra is not installed")
    def test_render_records_measured_and_missing_items_without_backfill(self):
        output = self.root / "comparison"
        result = build(self.summary, self.plots, coverage_path=self.coverage, output=output, paper_dir=self.paper)
        self.assert_bilingual_pages(output, result)
        measured = [row for row in result["items"] if row["status"] == "fresh-results"]
        self.assertEqual([row["experiment"] for row in measured], ["table-02"])
        self.assertEqual(measured[0]["populations"][0]["samples"][0]["n"], 29)
        self.assertEqual(result["release"]["source_commit"], "a" * 40)


if __name__ == "__main__":
    unittest.main()
