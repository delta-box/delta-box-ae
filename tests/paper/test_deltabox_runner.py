from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "ae/runners/deltabox"
sys.path.insert(0, str(RUNNER))
sys.path.insert(0, str(RUNNER / "guest"))
import host_execution
import provenance
import run_batch
import run_instance
from make_schedule import make_schedule, validate_schedule
from measurements import timed_api_call, settle_dumps
from entry import verify_sources

INSTANCE = "django__django-14997"
COMMIT = "a" * 40


def trace(path):
    path.mkdir(parents=True)
    children = [
        {"node_id": 1, "action_steps": [{"action": {"action_args_class": "StringReplaceArgs",
          "path": "module.py", "old_str": "old", "new_str": "new"}}]},
        {"node_id": 2, "action_steps": [{"action": {"action_args_class": "ViewCodeArgs",
          "files": [{"file_path": "module.py"}]}}]},
    ]
    (path / "trajectory.json").write_text(json.dumps({"repository": {"commit": COMMIT},
                                                       "root": {"node_id": 0, "children": children}}))
    (path / "ms_trace.jsonl").write_text('{"dur_s":0.1}\n{"dur_s":0.3}\n')


def options(root, extra=()):
    for name in ("kernel", "base", "data"):
        (root / name).write_bytes(name.encode())
    trace(root / "trace")
    return run_instance.parse_args([
        "--instance", INSTANCE, "--trace-dir", str(root / "trace"),
        "--kernel", str(root / "kernel"), "--base-xfs", str(root / "base"),
        "--data-xfs", str(root / "data"), "--out", str(root / "output"), "--dry-run", *extra,
    ])


def good_rows(events):
    rows = []
    for index, event in enumerate(events):
        row = {"kind": event["type"], "ev_i": index, "agent_mode": "real", "require_real_agent": True}
        if event["type"] == "ckpt":
            row.update(schedule_ckpt_id=event["ckpt_id"], checkpoint_api_wall_ms=1,
                       ckpt_wall_ms=2, checkpoint_sync_no_dump_ms=0.5,
                       worker_exec={"ok": True}, worker_index_status={"ok": True})
        else:
            row.update(schedule_target_id=event["restore_to_ckpt_id"], restore_api_wall_ms=3,
                       restore_critical_ms=0, worker_index_status_after_restore={"matches_target_ckpt": True})
        rows.append(row)
    rows.append({"kind": "run_summary", "error_n": 0, "worker_exec_bad_n": 0,
                 "worker_index_loaded": True, "worker_exec_required": True})
    return rows


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


class DeltaBoxScheduleTests(unittest.TestCase):
    def test_original_branch_order_rtt_and_all_standard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace(root / "trace")
            metadata = make_schedule(root / "trace", INSTANCE, root / "events.jsonl")
            events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
            self.assertEqual([e["type"] for e in events], ["ckpt", "ckpt", "restore", "ckpt"])
            self.assertEqual(events[2]["restore_to_ckpt_id"], events[0]["ckpt_id"])
            self.assertEqual([e["latency_ms"] for e in events if e["type"] == "ckpt"], [100, 300, 200])
            self.assertEqual({e["strategy"] for e in events if e["type"] == "ckpt"}, {"standard"})
            self.assertEqual(metadata["conversion_policy"], "all-standard")
            make_schedule(root / "trace", INSTANCE, root / "adaptive.jsonl", adaptive=True)
            adaptive = [json.loads(line) for line in (root / "adaptive.jsonl").read_text().splitlines()]
            self.assertIn("lightweight", [e.get("strategy") for e in adaptive])
            with self.assertRaises(ValueError):
                validate_schedule(adaptive)

    def test_missing_target_and_unsupported_action_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing checkpoint"):
            validate_schedule([{"type": "restore", "restore_to_ckpt_id": "absent"}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace(root / "trace")
            path = root / "trace/trajectory.json"
            path.write_text(path.read_text().replace("StringReplaceArgs", "UnknownArgs"))
            with self.assertRaisesRegex(ValueError, "unsupported recorded action"):
                make_schedule(root / "trace", INSTANCE, root / "events.jsonl")


class DeltaBoxProvenanceTests(unittest.TestCase):
    def test_bundle_injects_exact_current_runtime_and_pycriu(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "guest.tar"
            manifest = provenance.build_guest_archive(ROOT, RUNNER / "guest", archive)
            with tarfile.open(archive) as bundle:
                for name in ("sandbox_controller.py", "template_fork.py", "root_overlay.py"):
                    expected = ROOT / "backends/deltabox/gsd" / name
                    self.assertEqual(bundle.extractfile(name).read(), expected.read_bytes())
                    self.assertEqual(manifest["files"][name]["sha256"], hashlib.sha256(expected.read_bytes()).hexdigest())
                self.assertEqual(bundle.extractfile("pycriu/__init__.py").read(), (ROOT / "pycriu/__init__.py").read_bytes())
                extracted = Path(tmp) / "app"
                bundle.extractall(extracted)
            verify_sources(extracted)
            (extracted / "sandbox_controller.py").write_text("stale controller")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                verify_sources(extracted)

    def test_helper_cannot_shadow_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            guest = Path(tmp)
            (guest / "sandbox_controller.py").write_text("old controller")
            with self.assertRaisesRegex(RuntimeError, "shadow"):
                provenance.guest_sources(ROOT, guest)

    def test_image_hash_cache_invalidates_same_size_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, cache = root / "disk.xfs", root / "hashes.json"
            image.write_bytes(b"original")
            first = provenance.cached_digest(image, cache)
            image.write_bytes(b"modified")
            second = provenance.cached_digest(image, cache)
            self.assertNotEqual(first["sha256"], second["sha256"])

    def test_image_hash_cache_reuses_only_hashes_started_after_timestamp_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            image, cache = Path(tmp) / 'disk.xfs', Path(tmp) / 'hashes.json'
            image.write_bytes(b'original')
            identity = provenance.signature(image)
            stable_time = max(identity['mtime_ns'], identity['ctime_ns']) + 2 * provenance._RACY_STAT_NS
            with patch.object(provenance.time, 'time_ns', return_value=stable_time):
                first = provenance.cached_digest(image, cache)
                with patch.object(provenance, 'file_digest', side_effect=AssertionError('must reuse stable hash')):
                    self.assertEqual(provenance.cached_digest(image, cache), first)
            self.assertEqual(set(first), set(identity) | {'sha256'}, 'cache bookkeeping must stay out of image manifests')

    def test_same_stat_rewrite_cannot_become_trusted_when_cached_hash_gets_older(self):
        with tempfile.TemporaryDirectory() as tmp:
            image, cache = Path(tmp) / 'disk.xfs', Path(tmp) / 'hashes.json'
            image.write_bytes(b'original')
            identity = provenance.signature(image)
            stamp = max(identity['mtime_ns'], identity['ctime_ns'])
            with patch.object(provenance.time, 'time_ns', return_value=stamp):
                first = provenance.cached_digest(image, cache)
            image.write_bytes(b'modified')
            # Force the real Linux same-tick collision deterministically. It
            # must still be detected if the caller comes back much later.
            with patch.object(provenance, 'signature', return_value=identity),\
                 patch.object(provenance.time, 'time_ns', return_value=stamp + 10 * provenance._RACY_STAT_NS):
                second = provenance.cached_digest(image, cache)
            self.assertEqual(second['sha256'], hashlib.sha256(b'modified').hexdigest())
            self.assertNotEqual(first['sha256'], second['sha256'])

    def test_legacy_cache_without_hash_start_proof_is_recomputed(self):
        with tempfile.TemporaryDirectory() as tmp:
            image, cache = Path(tmp) / 'disk.xfs', Path(tmp) / 'hashes.json'
            image.write_bytes(b'original')
            original = provenance.file_digest(image)
            cache.write_text(json.dumps({str(image.resolve()): original}))
            with patch.object(provenance, 'file_digest', wraps=provenance.file_digest) as hash_file:
                self.assertEqual(provenance.cached_digest(image, cache), original)
                hash_file.assert_called_once_with(image)


class DeltaBoxRunnerTests(unittest.TestCase):
    def test_prewarm_ablation_is_explicit_and_separately_grouped(self):
        from ae.repro.analysis import fresh_labels
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = options(root, ("--prewarm-policy", "read"))
            original = run_instance.prepare_single_run(args)
            self.assertTrue(original.config['prewarm_requested'])
            args.out = root / 'prewarm-off'
            args.prewarm_policy = 'off'
            disabled = run_instance.prepare_single_run(args)
            self.assertFalse(disabled.config['prewarm_requested'])
            self.assertIn('--no-prewarm', disabled.config['guest_flags'])
            old = dict(original.config)
            old.pop('prewarm_requested')
            self.assertEqual(fresh_labels(old)['cohort'], fresh_labels(original.config)['cohort'])
            for field in ('cohort', 'plot_group'):
                self.assertNotEqual(fresh_labels(original.config)[field], fresh_labels(disabled.config)[field])
            args.out = root / 'cooperative-memory-policy'
            args.memory_policy = 'warm'
            warm = run_instance.prepare_single_run(args)
            self.assertEqual(warm.config['prewarm_mode'], 'populate-write-self')
            self.assertEqual(warm.config['prewarm_execution'], 'agent-cooperative')
            self.assertTrue(warm.config['prewarm_requested'])
            self.assertIn('--no-prewarm', warm.config['guest_flags'])
            self.assertEqual(warm.config['guest_env']['DELTABOX_PAPER_COOPERATIVE_PREWARM'], '1')
            self.assertFalse(warm.config['durable_dump_enabled'])
            self.assertFalse(warm.config['incremental_dump_enabled'])
            self.assertNotEqual(fresh_labels(warm.config)['cohort'], fresh_labels(disabled.config)['cohort'])

    def test_cooperative_warm_rejects_async_and_disable_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = options(root, ('--memory-policy', 'warm'))
            args.checkpoint_profile = 'async-incremental'
            args.criu_dump_binary = root / 'kernel'
            with self.assertRaisesRegex(ValueError, 'async-incremental'):
                run_instance.prepare_single_run(args)
            args.checkpoint_profile = 'runtime-default'
            policy = root / 'disabled.json'
            policy.write_text('{"DELTABOX_DISABLE_PREWARM": "1"}')
            args.guest_env_json = policy
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                run_instance.prepare_single_run(args)

    def test_write_prewarm_rejected_and_default_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = options(root)
            spec = run_instance.prepare_single_run(args)
            self.assertFalse(spec.config['prewarm_requested'])
            self.assertEqual(spec.config['prewarm_mode'], 'off')
            args.out = root / 'unsafe'
            args.prewarm_policy = 'historical'
            with self.assertRaisesRegex(ValueError, 'Unsafe write-prewarm'):
                run_instance.prepare_single_run(args)

    def test_prewarm_disable_override_cannot_mislabel_a_read_run(self):
        from ae.repro.analysis import fresh_labels
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = options(root, ("--prewarm-policy", "read"))
            policy = root / "disabled.json"
            policy.write_text('{"DELTABOX_DISABLE_PREWARM": "1"}')
            args.guest_env_json = policy
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                run_instance.prepare_single_run(args)
        self.assertEqual(fresh_labels({'prewarm_requested': True,
            'guest_env': {'DELTABOX_DISABLE_PREWARM': '1'}})['prewarm_mode'], 'off')

    def test_cooperative_journal_failure_cannot_mark_measurements_successful(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = run_instance.prepare_single_run(options(Path(tmp), ('--memory-policy', 'warm')))
            machine = SimpleNamespace(ssh_ready=True)
            with patch.object(run_instance, 'collect_jsonl'),\
                 patch.object(run_instance, 'collect_dmesg'),\
                 patch.object(run_instance, 'collect_diagnostics', side_effect=RuntimeError('warm evidence failed')):
                with self.assertRaisesRegex(RuntimeError, 'warm evidence failed'):
                    with run_instance.collect_results_on_exit(machine, spec):
                        pass

    def test_dry_run_manifest_prefix_flags_and_isolated_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = options(Path(tmp), ("--mode", "slow", "--max-events", "3"))
            spec = run_instance.prepare_single_run(args)
            self.assertEqual(spec.config["run_purpose"], "quick-check")
            self.assertEqual((spec.config["n_ckpt"], spec.config["n_restore"]), (2, 1))
            self.assertTrue(spec.config["incremental_dump_enabled"])
            self.assertEqual(spec.config["guest_env"]["DELTABOX_FORCE_CRIU_RESTORE"], "1")
            self.assertEqual(len(spec.config["images"]), 3)
            self.assertEqual(len(spec.config["inputs"]), 2)
            command = host_execution.build_instance_command(spec, host_execution.Lane(0))
            self.assertEqual(command[:5], ["unshare", "--mount", "--net", "--propagation", "private"])
            self.assertEqual(command[-2:], ["--_run-config", str(spec.config_path)])
            one = run_instance.vm_options(spec, Path(tmp) / "db-paper-one")
            two = run_instance.vm_options(spec, Path(tmp) / "db-paper-two")
            self.assertNotEqual(one.socket, two.socket)
            self.assertNotEqual(one.tap, two.tap)
            self.assertTrue(one.no_nat)

    def test_protected_environment_and_valid_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = root / "policy.json"
            args = options(root, ("--adaptive", "--checkpoint-profile", "runtime-default", "--memory-policy", "gc"))
            spec = run_instance.prepare_single_run(args)
            self.assertFalse(spec.config["incremental_dump_enabled"])
            self.assertFalse(spec.config["durable_dump_enabled"])
            self.assertEqual(spec.config["memory_policy"], "gc")
            self.assertIn("--enable-adaptive", spec.config["guest_flags"])
            self.assertEqual(spec.config["guest_env"]["DELTABOX_MEMCURVE"], "1")
            self.assertIn("--no-prewarm", spec.config["guest_flags"])
            for index, key in enumerate(("DELTABOX_REQUIRE_REAL_AGENT", "DELTABOX_MEMCURVE", "DELTABOX_FORK_ONLY_MEMCURVE", "DELTABOX_PAPER_MEMORY_POLICY")):
                policy.write_text(json.dumps({key: "0"}))
                args.guest_env_json = policy
                args.out = root / f"rejected-{index}"
                with self.assertRaisesRegex(ValueError, "protected"):
                    run_instance.prepare_single_run(args)

    def test_complete_results_and_invalid_indices_or_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = run_instance.prepare_single_run(options(Path(tmp)))
            events = [json.loads(line) for line in spec.schedule_path.read_text().splitlines()]
            original = good_rows(events)
            write_jsonl(spec.results_path, original)
            run_instance.validate_results(spec.results_path, spec.schedule_path)
            for mutate in (lambda rows: rows.pop(0),
                           lambda rows: rows[0].update(ev_i=99),
                           lambda rows: rows[0].update(checkpoint_api_wall_ms=float("nan")),
                           lambda rows: rows[0].update(worker_exec={"ok": False}),
                           lambda rows: rows[-1].update(error_n=1)):
                rows = json.loads(json.dumps(original))
                mutate(rows)
                write_jsonl(spec.results_path, rows)
                with self.assertRaises(ValueError):
                    run_instance.validate_results(spec.results_path, spec.schedule_path)

    def test_guest_failure_collects_results_and_stops_vm(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = run_instance.prepare_single_run(options(Path(tmp)))
            machine = SimpleNamespace(ssh_ready=True)
            stopped = []
            @contextlib.contextmanager
            def managed(_):
                try:
                    yield machine
                finally:
                    stopped.append(True)
            with patch.object(run_instance, "managed_vm", managed),\
                 patch.object(run_instance, "upload_inputs"),\
                 patch.object(run_instance, "execute_replay", side_effect=RuntimeError("guest failed")),\
                 patch.object(run_instance, "collect_jsonl", side_effect=RuntimeError("collection failed")),\
                 patch.object(run_instance, "collect_dmesg") as dmesg,\
                 patch.object(run_instance, "collect_diagnostics") as diagnostics:
                with self.assertRaisesRegex(RuntimeError, "guest failed"):
                    run_instance.run_guest(spec.config_path)
                dmesg.assert_called_once()
                diagnostics.assert_called_once_with(machine, spec)
            self.assertEqual(stopped, [True])
            self.assertIn("collection failed", (spec.output_dir / "host_errors.json").read_text())

    def test_startup_failure_stops_only_the_created_vm_and_tap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = run_instance.prepare_single_run(options(root))
            args = run_instance.vm_options(spec, root / "runtime")
            args.run_rootfs.parent.mkdir()
            process = Mock(pid=321)
            process.poll.return_value = None
            with patch.object(run_instance.vm, "require_root"),\
                 patch.object(run_instance.vm, "check_prereqs"),\
                 patch.object(run_instance.vm, "prepare_rootfs"),\
                 patch.object(run_instance.vm, "inject_ssh_key"),\
                 patch.object(run_instance.vm, "setup_tap"),\
                 patch.object(run_instance.vm, "route_guest_to_tap"),\
                 patch.object(run_instance.vm.subprocess, "Popen", return_value=process),\
                 patch.object(run_instance.vm.time, "sleep"),\
                 patch.object(run_instance.vm, "fc_put", side_effect=RuntimeError("boot config failed")),\
                 patch.object(run_instance.vm, "run") as command:
                with self.assertRaisesRegex(RuntimeError, "boot config failed"):
                    run_instance.vm.start_vm(args)
                run_instance.vm.stop_vm(args, None)
                process.terminate.assert_called_once()
                command.assert_called_once_with(["ip", "link", "del", args.tap], check=False)

    def test_failed_child_never_marks_run_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = run_instance.prepare_single_run(options(Path(tmp)))
            process = Mock(pid=123)
            process.poll.return_value = 17
            job = host_execution.RunningJob(spec, host_execution.Lane(0), process, io.StringIO())
            with patch.object(host_execution, "kill_group"):
                with self.assertRaisesRegex(RuntimeError, "rc=17"):
                    host_execution.finish_job(job)
            self.assertEqual(json.loads(spec.config_path.read_text())["status"], "failed")

    def test_batch_both_modes_builds_separate_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            single = options(root)
            args = run_batch.parse_args(["--instance", INSTANCE, "--inputs-root", str(root),
                 "--data-xfs", str(single.data_xfs), "--kernel", str(single.kernel),
                 "--base-xfs", str(single.base_xfs), "--out", str(root / "batch"), "--dry-run"])
            (root / "trace").rename(root / INSTANCE)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_batch.run_batch(args), 0)
            for mode in ("fast", "slow"):
                path = root / "batch" / mode / INSTANCE / "run.json"
                self.assertEqual(json.loads(path.read_text())["mode"], mode)


class DeltaBoxMeasurementsTests(unittest.TestCase):
    def test_api_timer_wraps_full_call_and_does_not_use_runtime_claim(self):
        result = {"restore_api_wall_ms": 99999}
        with patch("measurements.time.perf_counter_ns", side_effect=[10_000_000, 12_500_000]):
            observed, duration = timed_api_call(lambda argument: argument, result)
        self.assertIs(observed, result)
        self.assertEqual(duration, 2.5)
        with self.assertRaisesRegex(RuntimeError, "api failure"):
            timed_api_call(Mock(side_effect=RuntimeError("api failure")))

    def test_terminal_async_failure_is_recorded(self):
        ok, bad = Future(), Future()
        ok.set_result(0)
        bad.set_exception(RuntimeError("last dump failed"))
        rows = settle_dumps([("one", ok, {"dump_size_bytes": 4}), ("two", bad, {})])
        self.assertTrue(rows[0]["ok"])
        self.assertFalse(rows[1]["ok"])
        self.assertEqual(rows[1]["msg"], "last dump failed")


if __name__ == "__main__":
    unittest.main()
