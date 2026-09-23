"""Equation 1 and Figure 8 plotting remain usable without GPU packages."""
import copy
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ae/repro/gpu_occupation.py"


def theory_inputs():
    return dict(
        schema_version=1, kind="gpu-occupation-inputs", source_kind="assumed",
        model_label="test-model", batches=[16, 64], backends=["deltabox", "e2b"],
        gpu_timings=[dict(n=n, t_gen_s=1., t_train_s=2., generation_gpus=1,
                          training_gpus=4, source={"record": f"gpu-{n}"})
                     for n in (16, 64)],
        sandbox_timings=[dict(backend=backend, n=n, t_sandbox_s=1.,
                              estimated=backend == "e2b" and n == 64,
                              source={"record": f"{backend}-{n}"})
                         for backend in ("deltabox", "e2b") for n in (16, 64)],
        provenance={"experiment": "hand-computed"}, notes=["Test fixture."],
    )


def timing_suite(batches=(16, 64)):
    cases = []
    for n in batches:
        for phase, value in (("generation", 1.), ("training", 2.)):
            cases.append(dict(
                case_id=f"{phase}-B{n}", phase=phase, batch=n,
                num_gpus=4 if phase == "training" and n >= 16 else 1,
                method="fixture", status="ok",
                timing_s=dict(n=3, mean=value, median=value / 2,
                              p95=value * 1.5, min=value / 4, max=value * 2),
                result=dict(path=f"{phase}-B{n}/result.json", sha256="a" * 64, bytes=123),
            ))
    return dict(schema_version=1, kind="gpu-timing-suite", status="ok",
                source_kind="fresh", model_label="test-model",
                protocol={"prompt_mode": "paper-template"}, cases=cases)


def fanout_summary():
    series = [dict(panel="a", backend=backend, x=n, y=1000., unit="ms",
                   estimated=False, cohort=backend + "-cohort", plot_group="same-run",
                   population={"purpose": "fixture"})
              for backend in ("deltabox", "e2b") for n in (16, 64)]
    # A primitive time must never replace the end-to-end fanout time.
    series.append(dict(panel="primitive", backend="deltabox", x=16, y=999999., unit="ms"))
    return dict(schema_version=1, source="fresh",
                experiments={"figure-08": {"status": "analyzed", "series": series}})


class OccupationCliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, "-S", str(SCRIPT), *map(str, args)],
                              cwd=ROOT, text=True, capture_output=True, timeout=20)

    def test_cpu_cli_writes_hand_computed_model_and_hashed_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "inputs.json"
            source.write_text(json.dumps(theory_inputs()))
            output = base / "result"
            process = self.invoke("--input", source, "--output", output)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads((output / "occupation.json").read_text())
            self.assertEqual(result["kind"], "gpu-occupation-model")
            self.assertEqual(result["rows"][0]["occupation"], .75)
            self.assertEqual(result["rows"][0]["staleness_versions"], 1.)
            self.assertEqual(result["input_files"][0]["sha256"],
                             hashlib.sha256(source.read_bytes()).hexdigest())
            with (output / "metrics.csv").open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 4)
            self.assertEqual(float(rows[0]["occupation_pct"]), 75.)
            self.assertFalse((output / "plots").exists())

    def test_cli_rejects_invalid_inputs_before_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "inputs.json"
            data = theory_inputs()
            data["sandbox_timings"].pop()
            source.write_text(json.dumps(data))
            output = base / "result"
            process = self.invoke("--input", source, "--output", output)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("Missing", process.stderr)
            self.assertFalse(output.exists())

    def test_existing_output_and_dangling_symlink_are_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "inputs.json"
            source.write_text(json.dumps(theory_inputs()))
            output = base / "result"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("original")
            process = self.invoke("--input", source, "--output", output)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(sentinel.read_text(), "original")
            self.assertEqual(list(output.iterdir()), [sentinel])
            dangling = base / "dangling"
            dangling.symlink_to(base / "absent")
            process = self.invoke("--input", source, "--output", dangling)
            self.assertNotEqual(process.returncode, 0)
            self.assertTrue(dangling.is_symlink())
            self.assertFalse((base / "absent").exists())

    def test_fresh_file_import_retains_both_file_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            suite_path, fanout_path = base / "suite.json", base / "summary.json"
            suite_path.write_text(json.dumps(timing_suite()))
            fanout_path.write_text(json.dumps(fanout_summary()))
            output = base / "result"
            process = self.invoke("--gpu-results", suite_path, "--fanout-summary", fanout_path,
                                  "--output", output)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads((output / "occupation.json").read_text())
            self.assertEqual(result["source_kind"], "fresh")
            self.assertEqual({r["sha256"] for r in result["input_files"]},
                             {hashlib.sha256(p.read_bytes()).hexdigest() for p in (suite_path, fanout_path)})

    def test_cli_requires_an_explicit_complete_input_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result"
            for args in ([], ["--gpu-results", "suite.json"],
                         ["--input", "inputs.json", "--fanout-summary", "summary.json"],
                         ["--input", "inputs.json", "--gpu-results", "suite.json"]):
                with self.subTest(args=args):
                    self.assertNotEqual(self.invoke(*args, "--output", output).returncode, 0)
                    self.assertFalse(output.exists())

    def test_missing_optional_plotting_package_does_not_claim_output(self):
        # -S deliberately hides installed extras, even on a plotting workstation.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "inputs.json"
            source.write_text(json.dumps(theory_inputs()))
            output = base / "result"
            process = self.invoke("--input", source, "--output", output, "--plot")
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("optional", process.stderr)
            self.assertFalse(output.exists())


class EquationTests(unittest.TestCase):
    def setUp(self):
        from ae.repro.gpu_occupation import calculate_occupation
        self.calculate = calculate_occupation

    def test_hand_calculation_retains_estimates_and_provenance(self):
        inputs = theory_inputs()
        original = copy.deepcopy(inputs)
        result = self.calculate(inputs)
        self.assertEqual(inputs, original)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source_kind"], "assumed")
        self.assertEqual(result["provenance"], inputs["provenance"])
        self.assertEqual(result["inputs"], inputs)
        self.assertEqual(result["rows"][0]["occupation"], .75)
        self.assertEqual(result["rows"][0]["occupation_pct"], 75.)
        self.assertEqual(result["rows"][0]["staleness_versions"], 1.)
        estimated = [row for row in result["rows"] if row["estimated"]]
        self.assertEqual([(r["backend"], r["n"]) for r in estimated], [("e2b", 64)])
        self.assertEqual(estimated[0]["source"]["sandbox"], inputs["sandbox_timings"][-1]["source"])
        self.assertEqual(estimated[0]["source"]["gpu"], inputs["gpu_timings"][-1]["source"])
        self.assertIn("time fraction", " ".join(result["assumptions"]))
        self.assertIn("GPU-count", " ".join(result["assumptions"]))
        json.dumps(result, allow_nan=False)

    def test_default_batches_and_bounds_do_not_weight_gpu_counts(self):
        inputs = theory_inputs()
        del inputs["batches"]
        inputs["sandbox_timings"][0]["t_sandbox_s"] = 0.
        inputs["gpu_timings"][0]["t_gen_s"] = 0.
        result = self.calculate(inputs)
        self.assertEqual(result["batches"], [16, 64])
        self.assertEqual(result["rows"][0]["occupation"], 1.)
        self.assertEqual(result["rows"][0]["staleness_versions"], 0.)
        inputs["gpu_timings"][0]["training_gpus"] = 8
        self.assertEqual(self.calculate(inputs)["rows"][0]["occupation"], 1.)
        inputs["sandbox_timings"][0]["t_sandbox_s"] = 1e9
        ratio = self.calculate(inputs)["rows"][0]["occupation"]
        self.assertGreater(ratio, 0.)
        self.assertLess(ratio, 1.)

    def test_rejects_invalid_timing_values(self):
        for section, field in (("gpu_timings", "t_gen_s"), ("gpu_timings", "t_train_s"),
                               ("sandbox_timings", "t_sandbox_s")):
            for value in (-1, float("nan"), float("inf"), -float("inf"), True, "1", None):
                with self.subTest(section=section, field=field, value=value):
                    data = theory_inputs()
                    data[section][0][field] = value
                    with self.assertRaises(ValueError):
                        self.calculate(data)
        data = theory_inputs()
        data["gpu_timings"][0]["t_train_s"] = 0
        with self.assertRaisesRegex(ValueError, "t_train_s"):
            self.calculate(data)

    def test_rejects_missing_and_duplicate_points(self):
        for section in ("gpu_timings", "sandbox_timings"):
            for duplicate in (False, True):
                with self.subTest(section=section, duplicate=duplicate):
                    data = theory_inputs()
                    if duplicate:
                        data[section].append(copy.deepcopy(data[section][0]))
                    else:
                        data[section].pop()
                    with self.assertRaisesRegex(ValueError, "Duplicate" if duplicate else "Missing"):
                        self.calculate(data)

    def test_rejects_invalid_schema_dimensions_and_estimate_flags(self):
        for field, value in (("schema_version", True), ("schema_version", 2), ("kind", "wrong"),
                             ("source_kind", "measured-utilization"), ("model_label", " "),
                             ("batches", [True, 64]), ("batches", [16, 16]), ("batches", []),
                             ("backends", ["deltabox", "deltabox"]), ("backends", [])):
            with self.subTest(field=field, value=value):
                data = theory_inputs()
                data[field] = value
                with self.assertRaises(ValueError):
                    self.calculate(data)
        for section, field, value in (("gpu_timings", "n", True),
                                      ("gpu_timings", "generation_gpus", 0),
                                      ("gpu_timings", "training_gpus", 4.),
                                      ("sandbox_timings", "estimated", 1)):
            data = theory_inputs()
            data[section][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.calculate(data)
        data = theory_inputs()
        del data["sandbox_timings"][0]["estimated"]
        with self.assertRaises(ValueError):
            self.calculate(data)

    def test_large_finite_inputs_do_not_overflow_the_time_fraction(self):
        data = theory_inputs()
        data["gpu_timings"][0].update(t_gen_s=1e308, t_train_s=1e308)
        data["sandbox_timings"][0]["t_sandbox_s"] = 1e308
        row = self.calculate(data)["rows"][0]
        self.assertAlmostEqual(row["occupation"], 2 / 3)
        self.assertEqual(row["staleness_versions"], 2.)


class MeasurementImportTests(unittest.TestCase):
    def setUp(self):
        from ae.repro.gpu_occupation import calculate_occupation, inputs_from_measurements
        self.import_inputs = inputs_from_measurements
        self.calculate = calculate_occupation

    def test_imports_means_milliseconds_and_population_provenance(self):
        suite, summary = timing_suite(), fanout_summary()
        inputs = self.import_inputs(suite, summary)
        self.assertEqual(inputs["batches"], [16, 64])
        self.assertEqual(inputs["source_kind"], "fresh")
        self.assertEqual(inputs["gpu_timings"][0]["t_gen_s"], 1.)
        self.assertEqual(inputs["gpu_timings"][0]["t_train_s"], 2.)
        self.assertEqual(inputs["sandbox_timings"][0]["t_sandbox_s"], 1.)
        source = inputs["sandbox_timings"][0]["source"]
        self.assertEqual(source["series"]["cohort"], "deltabox-cohort")
        self.assertEqual(self.calculate(inputs)["rows"][0]["occupation"], .75)

    def test_historical_fanout_and_fresh_gpu_are_labeled_mixed(self):
        summary = fanout_summary()
        summary["source"] = "archived"
        summary["experiments"]["figure-08"]["series"][3]["estimated"] = True
        result = self.calculate(self.import_inputs(timing_suite(), summary))
        self.assertEqual(result["source_kind"], "mixed")
        self.assertTrue(next(r for r in result["rows"] if r["backend"] == "e2b" and r["n"] == 64)["estimated"])

    def test_rejects_failed_missing_and_conflicting_gpu_cases(self):
        for mutation in (lambda s: s.update(status="failed"),
                         lambda s: s["cases"][0].update(status="failed"),
                         lambda s: s["cases"].pop(),
                         lambda s: s["cases"].append(copy.deepcopy(s["cases"][0])),
                         lambda s: s["cases"][0]["timing_s"].update(mean=float("nan")),
                         lambda s: s["cases"][0]["timing_s"].update(mean=True),
                         lambda s: s["cases"][0]["result"].update(sha256="invalid")):
            suite = timing_suite()
            mutation(suite)
            with self.subTest(suite=suite), self.assertRaises(ValueError):
                self.import_inputs(suite, fanout_summary())

    def test_rejects_missing_bad_unit_duplicate_or_conflicting_fanout(self):
        for mutation in (lambda rows: rows.pop(0),
                         lambda rows: rows[0].update(unit="s"),
                         lambda rows: rows[0].update(y=-1),
                         lambda rows: rows.append(copy.deepcopy(rows[0])),
                         lambda rows: rows[1].update(cohort="different-N64-population"),
                         lambda rows: rows[1].update(population={"purpose": "other"}),
                         lambda rows: rows[2].update(plot_group="different-source")):
            summary = fanout_summary()
            mutation(summary["experiments"]["figure-08"]["series"])
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                self.import_inputs(timing_suite(), summary)

    def test_extra_gpu_batches_are_not_required_for_theory(self):
        inputs = self.import_inputs(timing_suite(), fanout_summary())
        self.assertEqual(len(inputs["gpu_timings"]), 2)
        inputs = self.import_inputs(timing_suite((1, 4, 16, 64)), fanout_summary())
        self.assertEqual(len(inputs["gpu_timings"]), 2)

    def test_unknown_fanout_provenance_cannot_become_a_fresh_input_claim(self):
        summary = fanout_summary()
        del summary["source"]
        inputs = self.import_inputs(timing_suite(), summary)
        self.assertEqual(inputs["source_kind"], "mixed")
        self.assertEqual(inputs["provenance"]["fanout_summary"]["source_kind"], "assumed")
        summary["source"] = ["fresh"]
        with self.assertRaises(ValueError):
            self.import_inputs(timing_suite(), summary)


class DependencyTests(unittest.TestCase):
    def test_imports_have_no_optional_or_gpu_dependencies(self):
        code = (
            "import sys; import ae.repro.gpu_occupation; import ae.repro.figure08_plots; "
            "assert not any(name.split('.')[0] in "
            "{'torch','vllm','transformers','peft','matplotlib','numpy'} for name in sys.modules)"
        )
        process = subprocess.run([sys.executable, "-S", "-c", code], cwd=ROOT,
                                 text=True, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Plotting extra is not installed")
class PlotTests(unittest.TestCase):
    def test_partial_gpu_timing_plot_reports_missing_cases_and_uses_means(self):
        from ae.repro.figure08_plots import plot_gpu_timing
        suite = timing_suite((1,))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "timing"
            metadata = plot_gpu_timing(suite, output)
            self.assertEqual(metadata["status"], "partial")
            self.assertEqual(len(metadata["missing_cases"]), 6)
            self.assertEqual(metadata["slots"][0]["mean_s"], 1.)
            self.assertIsNone(metadata["slots"][1]["mean_s"])
            self.assertIn("gpu_count_labels", metadata)
            self.assertEqual(metadata["gpu_count_labels"],
                             ["Generation GPUs: B1 = 1.", "Training GPUs: B1 = 1."])
            self.assertIn("Partial suite", metadata["footer"])
            self.assertIn("generation B64", metadata["footer"])
            for label in metadata["gpu_count_labels"]:
                self.assertIn(label, metadata["footer"])
            for name in ("figure-08b.png", "figure-08b.pdf"):
                self.assertGreater((output / name).stat().st_size, 1000)
            with self.assertRaises(FileExistsError):
                plot_gpu_timing(suite, output)

    def test_timing_gpu_count_labels_follow_custom_case_records(self):
        from ae.repro.figure08_plots import plot_gpu_timing
        suite = timing_suite((1, 4, 16, 64))
        for case in suite["cases"]:
            if case["phase"] == "generation" and case["batch"] == 64:
                case["num_gpus"] = 3
            elif case["phase"] == "training" and case["batch"] >= 16:
                case["num_gpus"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            metadata = plot_gpu_timing(suite, Path(tmp) / "timing")
            self.assertIn("gpu_count_labels", metadata)
            self.assertEqual(metadata["gpu_count_labels"], [
                "Generation GPUs: B1/4/16 = 1; B64 = 3.",
                "Training GPUs: B1/4 = 1; B16/64 = 2.",
            ])
            for label in metadata["gpu_count_labels"]:
                self.assertIn(label, metadata["footer"])
            self.assertNotIn("Partial suite", metadata["footer"])

    def test_theory_plot_keeps_canonical_order_and_marks_estimates(self):
        from ae.repro.figure08_plots import plot_gpu_occupation
        from ae.repro.gpu_occupation import calculate_occupation
        inputs = theory_inputs()
        inputs["backends"].reverse()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "theory"
            metadata = plot_gpu_occupation(calculate_occupation(inputs), output)
            self.assertEqual(metadata["backends"], ["deltabox", "e2b"])
            self.assertEqual(metadata["estimated_points"], [{"backend": "e2b", "n": 64}])
            self.assertIn("Assumed-input model", metadata["provenance_label"])
            for name in ("figure-08c.png", "figure-08c.pdf"):
                self.assertGreater((output / name).stat().st_size, 1000)

    def test_invalid_plot_inputs_do_not_create_output(self):
        from ae.repro.figure08_plots import plot_gpu_timing, plot_gpu_occupation
        from ae.repro.gpu_occupation import calculate_occupation
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "invalid"
            suite = timing_suite()
            suite["cases"][0]["timing_s"]["mean"] = float("inf")
            with self.assertRaises(ValueError):
                plot_gpu_timing(suite, output)
            self.assertFalse(output.exists())
            result = calculate_occupation(theory_inputs())
            result["rows"][0]["occupation_pct"] = float("nan")
            with self.assertRaises(ValueError):
                plot_gpu_occupation(result, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
