"""Exercise private rootfs loop ownership and cleanup without mounting anything."""
import ast
import argparse
from contextlib import contextmanager, ExitStack
import gc
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("rootfs_vm", ROOT / "ae/runners/vm.py")
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)


class RootfsMountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "rootfs.xfs"
        self.image.write_bytes(b"XFSB" + bytes(64))
        self.key = self.root / "key.pub"
        self.key.write_text("ssh-ed25519 fixture\n")
        self.args = SimpleNamespace(run_rootfs=self.image, log=self.root / "firecracker.log",
                                    ssh_pubkey=self.key, socket=self.root / "fc.socket")
        self.attached = False
        self.mounted = False
        self.loop = "/dev/loop23"
        self.device = "7:23"
        self.mount_point = None
        self.calls = []
        self.mount_fail = self.partial_mount_fail = self.umount_fail = self.detach_fail = False
        self.binding_changed = self.foreign_mount = self.detach_residual = False
        self.detach_delay = 0
        self.detach_requested = self.detach_foreign = False
        self.key_observed = None
        original_stat = Path.stat
        original_mkdtemp = tempfile.mkdtemp

        def path_stat(path, *args, **kwargs):
            if str(path) == self.loop:
                return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(7, 23))
            return original_stat(path, *args, **kwargs)

        self.stack = [patch.object(vm, "run", side_effect=self.fake_run),
                      patch.object(vm, "_rootfs_mounts", side_effect=self.mounts),
                      patch.object(Path, "stat", path_stat),
                      patch.object(vm.tempfile, "mkdtemp", side_effect=lambda **kw:
                                   original_mkdtemp(dir=self.root, **kw)),
                      patch.dict(os.environ, {}, clear=True)]
        for context in self.stack:
            context.start()
            self.addCleanup(context.stop)

    def mounts(self):
        return [(str(self.mount_point), "0:44" if self.foreign_mount else self.device)] if self.mounted else []

    def fake_run(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[:3] == ["losetup", "--find", "--show"]:
            self.attached = True
            return subprocess.CompletedProcess(cmd, 0, self.loop + "\n", "")
        if cmd[:3] == ["losetup", "--json", "--list"]:
            if self.detach_requested:
                if self.detach_foreign:
                    self.binding_changed = True
                elif self.detach_delay:
                    self.detach_delay -= 1
                elif not self.detach_residual:
                    self.attached = False
            image_stat = self.image.stat()
            record = dict(name=self.loop)
            record["back-ino"] = image_stat.st_ino + int(self.binding_changed) if self.attached else None
            record["back-maj:min"] = (f" {os.major(image_stat.st_dev)}:{os.minor(image_stat.st_dev)} "
                                      if self.attached else None)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(dict(loopdevices=[record])), "")
        if cmd[0] == "mount":
            self.mount_point = Path(cmd[-1])
            self.mounted = not self.mount_fail or self.partial_mount_fail
            if self.mounted:
                (self.mount_point / "root").mkdir(exist_ok=True)
            if self.mount_fail:
                raise subprocess.CalledProcessError(32, cmd, stderr="duplicate UUID")
        elif cmd[0] == "umount":
            if self.umount_fail:
                raise subprocess.CalledProcessError(32, cmd, stderr="busy")
            auth = self.mount_point / "root/.ssh/authorized_keys"
            if auth.exists():
                self.key_observed = (auth.read_text(), stat.S_IMODE(auth.stat().st_mode),
                                     stat.S_IMODE(auth.parent.stat().st_mode))
            self.mounted = False
            for child in self.mount_point.iterdir():
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        elif cmd[:2] == ["losetup", "--detach"]:
            if self.detach_fail:
                raise subprocess.CalledProcessError(1, cmd)
            self.detach_requested = True
            self.attached = self.detach_residual or bool(self.detach_delay) or self.detach_foreign
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def assert_clean(self):
        self.assertFalse(self.attached)
        self.assertFalse(self.mounted)
        self.assertFalse(self.mount_point.exists())
        self.assertFalse(hasattr(self.args, "_rootfs_cleanup_error"))

    def receipt(self):
        return json.loads(Path(self.args._rootfs_cleanup_error["receipt"]).read_text())

    def test_xfs_same_uuid_clones_use_nouuid_and_owned_explicit_loop(self):
        with vm.patch_rootfs(self.args):
            self.assertTrue(self.mounted)
        mount = next(cmd for cmd in self.calls if cmd[0] == "mount")
        self.assertEqual(mount[1:-1], ["-t", "xfs", "-o", "nouuid", self.loop])
        self.assert_clean()

    def test_non_xfs_does_not_receive_xfs_options(self):
        self.image.write_bytes(b"ext4" + bytes(64))
        with vm.patch_rootfs(self.args):
            pass
        mount = next(cmd for cmd in self.calls if cmd[0] == "mount")
        self.assertEqual(mount[1:-1], [self.loop])
        self.assert_clean()

    def test_inject_preserves_key_text_and_permissions(self):
        vm.inject_ssh_key(self.args)
        self.assertEqual(self.key_observed, (self.key.read_text(), 0o600, 0o700))
        self.assert_clean()

    def test_mount_failure_detaches_loop_and_removes_directory(self):
        self.mount_fail = True
        with self.assertRaises(subprocess.CalledProcessError):
            with vm.patch_rootfs(self.args):
                self.fail("mount must fail before patch")
        self.assertNotIn("umount", [cmd[0] for cmd in self.calls])
        self.assert_clean()

    def test_partial_mount_failure_still_unmounts_owned_mount(self):
        self.mount_fail = self.partial_mount_fail = True
        with self.assertRaises(subprocess.CalledProcessError):
            with vm.patch_rootfs(self.args):
                pass
        self.assertIn("umount", [cmd[0] for cmd in self.calls])
        self.assert_clean()

    def test_patch_failure_preserves_primary_after_successful_cleanup(self):
        with self.assertRaisesRegex(ValueError, "patch failed"):
            with vm.patch_rootfs(self.args):
                raise ValueError("patch failed")
        self.assert_clean()

    def test_umount_failure_retains_image_loop_directory_and_both_errors(self):
        self.umount_fail = True
        with self.assertRaisesRegex(RuntimeError, "resources retained"):
            with vm.patch_rootfs(self.args):
                raise ValueError("patch failed")
        self.assertTrue(self.attached and self.mounted and self.mount_point.exists())
        self.assertNotIn(["losetup", "--detach", self.loop], self.calls)
        receipt = self.receipt()
        self.assertEqual(receipt["image_inode"], self.image.stat().st_ino)
        self.assertEqual(receipt["loop"], self.loop)
        self.assertIn("patch failed", receipt["original_error"])
        self.assertIn("CalledProcessError", receipt["cleanup_error"])
        with self.assertRaisesRegex(RuntimeError, "retain runtime image"):
            vm.stop_vm(self.args, None)
        self.assertTrue(self.image.exists())

    def test_foreign_loop_rebinding_is_not_unmounted_or_detached(self):
        with self.assertRaisesRegex(RuntimeError, "resources retained"):
            with vm.patch_rootfs(self.args):
                self.binding_changed = True
        self.assertNotIn("umount", [cmd[0] for cmd in self.calls])
        self.assertNotIn(["losetup", "--detach", self.loop], self.calls)
        self.assertIn("identity changed", self.receipt()["cleanup_error"])

    def test_unknown_mount_is_not_unmounted_or_detached(self):
        with self.assertRaisesRegex(RuntimeError, "resources retained"):
            with vm.patch_rootfs(self.args):
                self.foreign_mount = True
        self.assertNotIn("umount", [cmd[0] for cmd in self.calls])
        self.assertNotIn(["losetup", "--detach", self.loop], self.calls)

    def test_detach_failure_retains_loop_and_receipt(self):
        self.detach_fail = True
        with self.assertRaisesRegex(RuntimeError, "resources retained"):
            with vm.patch_rootfs(self.args):
                pass
        self.assertTrue(self.attached)
        self.assertFalse(self.mounted)
        self.assertTrue(self.mount_point.exists())
        self.assertTrue(Path(self.receipt()["rootfs"]).exists())

    def test_lazy_detach_persistent_binding_times_out_and_retains_evidence(self):
        self.detach_residual = True
        clock = [0.0]
        with patch.object(vm.time, "monotonic", side_effect=lambda: clock[0]),\
             patch.object(vm.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaisesRegex(RuntimeError, "resources retained"):
                with vm.patch_rootfs(self.args):
                    pass
        self.assertIn("remains attached", self.receipt()["cleanup_error"])
        self.assertIn("timeout", self.receipt()["cleanup_error"])
        self.assertAlmostEqual(clock[0], 2.0)
        self.assertTrue(self.attached and self.mount_point.exists() and self.image.exists())

    def test_lazy_detach_delayed_disappearance_succeeds_without_global_settle(self):
        self.detach_delay = 3
        with patch.object(vm.time, "sleep") as sleep:
            with vm.patch_rootfs(self.args):
                pass
        self.assertEqual(sleep.call_count, 3)
        self.assertTrue(all(call.args == (0.02,) for call in sleep.call_args_list))
        self.assertEqual(self.calls.count(["losetup", "--detach", self.loop]), 1)
        self.assertNotIn("udevadm", [cmd[0] for cmd in self.calls])
        self.assert_clean()

    def test_lazy_detach_foreign_rebinding_immediately_rejects_without_more_cleanup(self):
        self.detach_foreign = True
        with patch.object(vm.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "resources retained"):
                with vm.patch_rootfs(self.args):
                    pass
        sleep.assert_not_called()
        self.assertEqual(self.calls.count(["losetup", "--detach", self.loop]), 1)
        self.assertIn("identity changed", self.receipt()["cleanup_error"])
        self.assertTrue(self.attached and self.mount_point.exists() and self.image.exists())

    def test_hosted_cleanup_failure_creates_exclusive_global_recovery_gate(self):
        self.umount_fail = True
        (self.root / "ae/work").mkdir(parents=True)
        with patch.object(vm, "REPO_ROOT", self.root), patch.dict(os.environ, {"AE_HOSTED_CALLER_UID": "1012"}):
            with self.assertRaises(RuntimeError):
                with vm.patch_rootfs(self.args):
                    pass
        guard = self.root / "ae/work/CPU_SERVICE_RECOVERY_REQUIRED.json"
        record = json.loads(guard.read_text())
        self.assertEqual(record["loop"], self.loop)
        self.assertEqual(record["receipt"], self.receipt()["receipt"])
        self.assertEqual(stat.S_IMODE(guard.stat().st_mode), 0o600)

    def test_hosted_existing_recovery_gate_is_not_overwritten(self):
        self.umount_fail = True
        (self.root / "ae/work").mkdir(parents=True)
        guard = self.root / "ae/work/CPU_SERVICE_RECOVERY_REQUIRED.json"
        guard.write_text('{"reason":"previous"}\n')
        original = guard.stat().st_ino, guard.read_bytes()
        with patch.object(vm, "REPO_ROOT", self.root), patch.dict(os.environ, {"AE_HOSTED_CALLER_UID": "1012"}):
            with self.assertRaises(RuntimeError):
                with vm.patch_rootfs(self.args):
                    pass
        self.assertEqual((guard.stat().st_ino, guard.read_bytes()), original)


class RuntimeRetentionTests(unittest.TestCase):
    def test_runtime_directory_is_retained_after_rootfs_cleanup_failure(self):
        source = ast.parse((ROOT / "replay/run_instance.py").read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                        and node.name == "remove_runtime_directory")
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "run_instance.py", "exec"), namespace)
        with tempfile.TemporaryDirectory() as root:
            temporary = tempfile.TemporaryDirectory(dir=root)
            runtime = Path(temporary.name)
            image = runtime / "rootfs.xfs"
            image.write_text("retained inode")
            machine = SimpleNamespace(args=SimpleNamespace(_rootfs_cleanup_error={"loop": "/dev/loop23"}))
            try:
                namespace[function.name](temporary, machine)
                self.fail("retention must report the cleanup failure")
            except RuntimeError as error:
                self.assertIn("retain runtime directory", str(error))
            reference = weakref.ref(temporary)
            del temporary
            gc.collect()
            self.assertIsNone(reference())
            self.assertTrue(runtime.is_dir())
            self.assertEqual(image.read_text(), "retained inode")
            temporary = tempfile.TemporaryDirectory(dir=root)
            normal_runtime = Path(temporary.name)
            del machine.args._rootfs_cleanup_error
            namespace[function.name](temporary, machine)
            self.assertFalse(normal_runtime.exists())


def compile_functions(path, names, namespace):
    source = ast.parse(path.read_text())
    functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == len(names)
    deferred = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[deferred, *functions], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)


class OuterContextRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.images = []
        self.cleanup_errors = []
        self.failed_cleanup = True

        def start(args):
            args.run_rootfs.write_text("owned image remains")
            self.images.append(args.run_rootfs)
            if self.failed_cleanup:
                args._rootfs_cleanup_error = dict(loop="/dev/loop23")
                raise RuntimeError("simulated rootfs umount failure")
            return None

        self.vm = SimpleNamespace(start_vm=start, stop_vm=vm.stop_vm, GUEST_IP=vm.GUEST_IP,
                                  ssh_opts=lambda: [])
        self.subprocess = SimpleNamespace(run=lambda cmd, **kw: self.calls.append(cmd))
        self.namespace = dict(contextmanager=contextmanager, ExitStack=ExitStack, Path=Path, argparse=argparse,
                              tempfile=tempfile, SimpleNamespace=SimpleNamespace,
                              subprocess=self.subprocess, vm=self.vm, json=json,
                              capture_binding=lambda *args: None, capture_exit_maps=lambda *args: None,
                              record_host_error=lambda out, phase, error: self.cleanup_errors.append(phase))

    def configuration(self, **extra):
        return dict(kernel=str(self.root / "kernel"), base_xfs=str(self.root / "base.xfs"),
                    data_xfs=str(self.root / "data.xfs"), vcpus=4, mem_mib=128,
                    ssh_timeout=1, work_dir=str(self.root), **extra)

    def compile_managed_vm(self):
        host = ROOT / "replay/host_execution.py"
        if not host.exists():
            host = ROOT / "validation/reference/host_execution.py"
        compile_functions(host, {"cleanup_actions"}, self.namespace)
        compile_functions(ROOT / "replay/run_instance.py",
                          {"vm_options", "remove_runtime_directory", "managed_vm"}, self.namespace)

    def test_actual_managed_vm_outer_context_retains_failed_image_after_gc(self):
        self.compile_managed_vm()
        spec = SimpleNamespace(config=self.configuration(), output_dir=self.root)
        try:
            with self.namespace["managed_vm"](spec):
                self.fail("failed VM startup must not yield")
        except RuntimeError as error:
            self.assertIn("simulated rootfs", str(error))
        gc.collect()
        self.assertEqual(self.images[0].read_text(), "owned image remains")
        self.assertIn("stop VM", self.cleanup_errors)
        self.assertIn("remove runtime directory", self.cleanup_errors)

    def test_actual_managed_vm_normal_context_still_removes_owned_runtime(self):
        self.failed_cleanup = False
        self.compile_managed_vm()
        spec = SimpleNamespace(config=self.configuration(), output_dir=self.root)
        with self.namespace["managed_vm"](spec) as machine:
            runtime = machine.runtime
            self.assertTrue(self.images[0].exists())
        self.assertFalse(runtime.exists())
        self.assertFalse(self.cleanup_errors)

    def test_actual_guest_run_retains_rootfs_and_ram_mount_for_all_vm_experiments(self):
        self.namespace.update(AE_ROOT=self.root, require_memory_workdir=lambda *a, **k: {},
                              write_json=lambda path, data: path.write_text(json.dumps(data)))
        compile_functions(ROOT / "ae/runners/vm_experiment.py",
                          {"runtime_directory", "guest_run"}, self.namespace)
        (self.root / "base.xfs").write_text("base")
        for experiment in ("figure-08-deltabox", "figure-09", "correctness"):
            with self.subTest(experiment=experiment):
                self.calls.clear()
                configuration = self.configuration(experiment=experiment)
                if experiment != "figure-08-deltabox":
                    del configuration["work_dir"]
                path = self.root / "config.json"
                path.write_text(json.dumps(configuration))
                try:
                    self.namespace["guest_run"](path)
                    self.fail("failed VM startup must not complete")
                except RuntimeError as error:
                    self.assertIn("retain runtime", str(error))
                gc.collect()
                self.assertEqual(self.images[-1].read_text(), "owned image remains")
                self.assertNotIn("umount", [cmd[0] for cmd in self.calls])


if __name__ == "__main__":
    unittest.main()
