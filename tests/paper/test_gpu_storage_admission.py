"""Storage relocation preserves reservations and keeps JIT writes off full disks."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ae.scripts import figure08_remote as remote
from ae.runners import gpu_timing


class StorageTests(unittest.TestCase):
    def good(self):
        return dict(requested='/work/run', existing_parent='/work',
                    available_bytes=40*1024**3, available_inodes=100000, writable=True)

    def test_admission_rejects_block_inode_or_permission_shortage(self):
        for field, value in [('available_bytes', 0), ('available_inodes', 0), ('writable', False)]:
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'storage reserve'):
                remote.storage_admission(dict(self.good(), **{field: value}))
        record = remote.storage_admission(self.good())
        self.assertEqual(record['required_free_bytes'], 10*1024**3)

    def test_low_storage_fails_before_snapshot_upload_or_mkdir(self):
        with tempfile.TemporaryDirectory() as d:
            response = subprocess.CompletedProcess([], 0, json.dumps(dict(self.good(), available_bytes=0)))
            with patch.object(remote, 'ssh', return_value=response) as ssh, patch.object(remote, 'snapshot') as snapshot:
                result = remote.run_auto(Path(d)/'run')
            self.assertEqual(result['successful_cases'], 0)
            self.assertIn('storage reserve', result['reason'])
            snapshot.assert_not_called()
            self.assertEqual(ssh.call_count, 1)
            self.assertIn('-c', ssh.call_args.args[1])

    def test_probe_uses_existing_parent_and_user_available_capacity(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            response = subprocess.run([remote.sys.executable, '-c', remote.STORAGE_PROBE,
                                       str(root/'missing/run')], check=True, capture_output=True, text=True)
            record = json.loads(response.stdout)
            fs = os.statvfs(root)
            self.assertEqual(record['available_inodes'], fs.f_favail)
            self.assertEqual(record['existing_parent'], str(root.resolve()))
            self.assertFalse((root/'missing').exists())

    def test_runtime_cache_and_temp_environment_propagates_and_restores_on_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.dict(os.environ, {'TMPDIR': '/old/tmp', 'FLASHINFER_WORKSPACE_BASE': '/full/cache'}):
                before = os.environ.copy()
                old_temp = tempfile.tempdir
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    with remote.runtime_environment(root) as values:
                        self.assertEqual(values['PYTHONDONTWRITEBYTECODE'], '1')
                        for key, value in values.items():
                            self.assertEqual(os.environ[key], value)
                            if key != 'PYTHONDONTWRITEBYTECODE':
                                self.assertTrue(Path(value).is_relative_to(root.resolve()))
                                self.assertTrue(Path(value).is_dir())
                        self.assertEqual(tempfile.gettempdir(), values['TMPDIR'])
                        child = remote.protocol.child_environment(
                            {'devices': ['GPU-0'], 'generation_python': '/python/bin/python'},
                            {'num_gpus': 1, 'phase': 'generation', 'case_id': 'generation-B1'},
                            device_indices=['0'])
                        self.assertEqual(child['FLASHINFER_WORKSPACE_BASE'], values['FLASHINFER_WORKSPACE_BASE'])
                        self.assertEqual(child['TORCHINDUCTOR_CACHE_DIR'], values['TORCHINDUCTOR_CACHE_DIR'])
                        raise RuntimeError('injected')
                self.assertEqual(os.environ, before)
                self.assertEqual(tempfile.tempdir, old_temp)

    def test_relocated_workspace_respects_original_locked_inode(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            locks = root/'original/locks'
            locks.mkdir(parents=True)
            lockpath = locks/'GPU-0.lock'
            lockpath.touch()
            original = (lockpath.stat().st_dev, lockpath.stat().st_ino)
            run = root/'new-storage/run'
            run.mkdir(parents=True)
            config = remote.load_settings(remote.DEFAULT_CONFIG)
            config.update(remote_root=str(root/'new-storage'), lock_root=str(locks))
            remote.write_json(run/'remote-config.json', config)
            remote.write_json(run/'source.json', {'files': {}})
            with lockpath.open('a') as held:
                remote.fcntl.flock(held, remote.fcntl.LOCK_EX)
                with patch.object(remote, 'local_storage_admission', return_value=self.good()), \
                     patch.object(remote, 'probe', return_value={'idle': [{'index':0,'uuid':'GPU-0'}]}), \
                     patch.object(gpu_timing, 'run_suite') as measure:
                    result = remote.remote_run(run)
                measure.assert_not_called()
            self.assertEqual(result['selected'], [])
            self.assertEqual((lockpath.stat().st_dev, lockpath.stat().st_ino), original)
            self.assertFalse((root/'new-storage/locks').exists())

    def test_remote_capacity_is_rechecked_before_model_preflight(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            remote.write_json(root/'remote-config.json', remote.load_settings(remote.DEFAULT_CONFIG))
            remote.write_json(root/'source.json', {'files': {}})
            with patch.object(remote, 'local_storage_admission', side_effect=RuntimeError('disk became full')), \
                 patch.object(gpu_timing, 'check_resources') as check, patch.object(remote, 'probe') as probe:
                result=remote.remote_run(root)
            self.assertIn('disk became full', result['reason'])
            check.assert_not_called()
            probe.assert_not_called()

    def test_deployed_path_fits_real_unix_socket_and_overlong_root_fails(self):
        import socket
        configured = Path(remote.load_settings(remote.DEFAULT_CONFIG)['remote_root'])
        deployed = configured / ('run-' + 'a'*32)
        self.assertLessEqual(len(os.fsencode(deployed)) + 37, 107)
        with tempfile.TemporaryDirectory() as d:
            # Exercise the exact maximum-length endpoint, not only string arithmetic.
            root = Path(d) / ('a' * (70 - len(os.fsencode(d)) - 1))
            root.mkdir()
            with remote.runtime_environment(root) as values:
                address = values['VLLM_RPC_BASE_PATH'] + '/' + 'b'*36
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.bind(address)
                Path(address).unlink()
            with self.assertRaisesRegex(ValueError, 'too long'):
                with remote.runtime_environment(root / 'too-long'):
                    self.fail('worker must not start with an invalid socket path')

    def test_relative_lock_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            config=remote.load_settings(remote.DEFAULT_CONFIG)
            config['lock_root']='new-relative-locks'
            path=Path(d)/'config.json'
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, 'lock_root'):
                remote.load_settings(path)

if __name__ == '__main__':
    unittest.main()
