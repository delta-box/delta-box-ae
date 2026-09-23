"""Restricted hosted AE execution: caller, source, environment, and result paths."""
import contextlib
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('hosted_launcher', ROOT / 'ae/scripts/hosted_launcher.py')
hosted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosted)
REVIEWER = SimpleNamespace(pw_name='atc-ae', pw_uid=7001, pw_gid=7001)
ROOT_USER = SimpleNamespace(pw_name='root', pw_uid=0, pw_gid=0)
MAINTAINER = SimpleNamespace(pw_name='dyp', pw_uid=1010, pw_gid=1011)
OTHER_USER = SimpleNamespace(pw_name='other', pw_uid=7002, pw_gid=7002)


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runtime = self.root / 'runtime'
        (self.runtime / 'ae/scripts').mkdir(parents=True)
        (self.runtime / 'ae/scripts/run_review.py').write_text('# fixed runtime\n')
        (self.runtime / 'release').mkdir()
        (self.runtime / 'release/candidate-lock.json').write_text(json.dumps({'source_commit': 'a' * 40}))
        self.python = self.root / 'venv/bin/python'
        self.python.parent.mkdir(parents=True)
        self.python.write_text('# fixed python\n')
        (self.python.parent.parent / 'pyvenv.cfg').write_text('include-system-site-packages = false\n')
        self.output = self.root / 'results'
        self.output.mkdir()
        self.config = self.root / 'review.json'
        self.config.write_text('{}')
        self.environment = self.root / 'environment.json'
        self.environment.write_text(json.dumps({'E2B_TEMPLATE': 'prepared', 'E2B_API_KEY': 'fixed-secret'}))
        self.policy = dict(runtime_root=self.runtime, python=self.python, config=self.config,
                           environment_file=self.environment, output_root=self.output,
                           allowed_user=REVIEWER.pw_name, lock_file=self.root / 'ae.lock')
        self.policy_path = self.root / 'launcher.json'
        self.policy_path.write_text(json.dumps({key: str(value) for key, value in self.policy.items()}))
        self.untrusted = set()
        self.owners = {}
        self.users = [ROOT_USER, MAINTAINER, REVIEWER, OTHER_USER]
        self.groups = {0: [], MAINTAINER.pw_gid: [], REVIEWER.pw_gid: [], OTHER_USER.pw_gid: []}

    def test_default_results_group_full_run_and_checks_under_the_same_version(self):
        with self.owned_fixture():
            full = hosted.default_result(self.policy, hosted.parse_arguments(['--checkout', str(self.runtime)]))
            quick_check = hosted.default_result(self.policy, hosted.parse_arguments(['--checkout', str(self.runtime), '--test']))
        self.assertEqual(full, Path('aaaaaaaaaaaa/full'))
        self.assertEqual(quick_check, Path('aaaaaaaaaaaa/checks/quick-check'))
        self.assertEqual(full.parts[0], quick_check.parts[0])

    def test_default_result_rejects_path_content_in_source_identity(self):
        (self.runtime / 'release/candidate-lock.json').write_text(json.dumps({'source_commit': '../outside'}))
        with self.owned_fixture(), self.assertRaisesRegex(ValueError, 'full source commit'):
            hosted.default_result(self.policy, hosted.parse_arguments(['--checkout', str(self.runtime)]))

    def maintainer_runtime(self):
        author = self.root / 'dyp'
        author.mkdir(mode=0o755)
        self.runtime = self.runtime.rename(author / 'runtime')
        environment = self.python.parent.parent.rename(self.runtime / '.venv')
        self.python = environment / 'bin/python'
        results = self.runtime / 'ae/results'
        results.mkdir()
        self.output = self.output.rename(results / 'hosted')
        for path in [author, self.runtime, *self.runtime.rglob('*')]:
            self.owners[path] = (MAINTAINER.pw_uid, MAINTAINER.pw_gid)
        self.owners[self.output] = (0, 0)
        self.runtime.chmod(0o775)
        self.policy.update(runtime_root=self.runtime, python=self.python,
                           output_root=self.output, trusted_maintainer=MAINTAINER.pw_name)
        self.policy_path.write_text(json.dumps({key: str(value) for key, value in self.policy.items()}))
        return author

    @contextlib.contextmanager
    def owned_fixture(self):
        # Tests create real files as the developer, then supply their deployment
        # ownership. Actual file modes, link types and directory contents remain.
        lstat, fstat = Path.lstat, os.fstat
        def root_metadata(info, *, outside=False, owner=(0, 0), untrusted=False):
            values = list(info)
            values[4] = REVIEWER.pw_uid if untrusted else owner[0]
            values[5] = owner[1]
            if outside and stat.S_ISDIR(info.st_mode):
                values[0] = stat.S_IFDIR | 0o755
            return os.stat_result(values)
        def owned_stat(path):
            return root_metadata(lstat(path), outside=not path.is_relative_to(self.root),
                                 owner=self.owners.get(path, (0, 0)), untrusted=path in self.untrusted)
        with patch.object(Path, 'lstat', owned_stat),\
             patch.object(hosted.os, 'getxattr', side_effect=OSError(errno.ENODATA, 'No ACL'), create=True),\
             patch.object(hosted.os, 'fstat', side_effect=lambda fd: root_metadata(fstat(fd))):
            yield

    @contextlib.contextmanager
    def launcher(self, extra_env=None):
        env = {'SUDO_UID': str(REVIEWER.pw_uid), **(extra_env or {})}
        with self.owned_fixture(),\
             patch.object(hosted, 'POLICY_PATH', self.policy_path),\
             patch.object(hosted.os, 'geteuid', return_value=0),\
             patch.object(hosted.os, 'getuid', return_value=0),\
             patch.object(hosted.pwd, 'getpwnam', side_effect=lambda name: next(user for user in self.users if user.pw_name == name)),\
             patch.object(hosted.pwd, 'getpwuid', side_effect=lambda uid: ROOT_USER if uid == 0 else REVIEWER),\
             patch.object(hosted.pwd, 'getpwall', side_effect=lambda: self.users),\
             patch.object(hosted.grp, 'getgrgid', side_effect=lambda gid: SimpleNamespace(gr_gid=gid, gr_mem=self.groups[gid])),\
             patch.dict(hosted.os.environ, env, clear=True),\
             patch.object(hosted.os, 'chdir'), patch.object(hosted.os, 'umask'),\
             patch.object(hosted.os, 'execve') as execute,\
             contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            yield execute, stdout, stderr

    def test_fixed_command_discards_caller_configuration_and_loader_environment(self):
        poison = {'PYTHONPATH': '/attacker', 'PYTHONHOME': '/attacker', 'GIT_CONFIG_COUNT': '1',
                  'GIT_CONFIG_KEY_0': 'core.sshCommand', 'GIT_CONFIG_VALUE_0': 'attacker',
                  'SUDO_GID': str(REVIEWER.pw_gid), 'SUDO_USER': 'forged',
                  'HOME': '/attacker', 'LD_PRELOAD': '/attacker.so', 'BASH_ENV': '/attacker',
                  'AE_CONFIG': '/attacker.json', 'DELTABOX_RELEASE_LOCK': '/attacker.json',
                  'TMPDIR': '/attacker', 'TMP': '/attacker', 'TEMP': '/attacker',
                  'AE_HOSTED_CALLER_UID': '0', 'E2B_API_KEY': 'caller-secret'}
        with self.launcher(poison) as (execute, stdout, _):
            code = hosted.main(['--checkout', str(self.runtime), '--output', 'formal'])
        self.assertEqual(code, 0)
        binary, command, environment = execute.call_args.args
        self.assertEqual(binary, str(self.python))
        self.assertEqual(command, [str(self.python), '-I', str(self.runtime / 'ae/scripts/run_review.py'),
                                   '--config', str(self.config), '--output', str(self.output / 'formal')])
        self.assertEqual(environment['HOME'], '/root')
        self.assertEqual(environment['AE_HOSTED_CALLER_UID'], str(REVIEWER.pw_uid))
        self.assertEqual(environment['E2B_API_KEY'], 'fixed-secret')
        self.assertFalse(any(key.startswith(('SUDO_', 'PYTHON', 'GIT_CONFIG')) for key in environment))
        for key in ('LD_PRELOAD', 'BASH_ENV', 'AE_CONFIG', 'DELTABOX_RELEASE_LOCK', 'TMPDIR', 'TMP', 'TEMP'):
            self.assertNotIn(key, environment)
        self.assertNotIn('secret', stdout.getvalue())
        audit = json.loads((self.output / '.launcher-audit.jsonl').read_text())
        self.assertEqual(audit['caller_uid'], REVIEWER.pw_uid)
        self.assertEqual(audit['command'], command)

    def test_refuses_untrusted_user_and_missing_root_privilege(self):
        with self.launcher({'SUDO_UID': '7002'}) as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime)]), 2)
            execute.assert_not_called()
            self.assertIn('not authorized', stderr.getvalue())
        for uid in ('-1', 'not-a-uid', '７００１'):
            with self.subTest(uid=uid), self.launcher({'SUDO_UID': uid}) as (execute, _, _):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime)]), 2)
                execute.assert_not_called()
        with self.launcher() as (execute, _, _), patch.object(hosted.os, 'geteuid', return_value=REVIEWER.pw_uid):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime)]), 2)
            execute.assert_not_called()

    def test_root_can_use_the_same_restricted_interface(self):
        with self.launcher({'SUDO_UID': '0'}) as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 0)
        self.assertEqual(execute.call_args.args[2]['AE_HOSTED_CALLER_UID'], '0')
        self.assertNotIn('--output', execute.call_args.args[1])

    def test_whitelist_rejects_privilege_expansion_flags_and_abbreviations(self):
        bad = (['--available'], ['--config', '/other'], ['--conf', '/other'],
               ['--experiment-config', 'correctness=/other'], ['--runtime-repo', '/other'],
               ['--execute-plan', '/other'], ['--probe-plan', '/other'], ['--publish-output', '/other'],
               ['--analyze-existing', '/other'], ['--no-pin'], ['--cpus', '0'], ['--numa-node', '0'],
               ['--temporary-root', '/other'],
               ['--experiment', 'invented'], ['--group', 'invented'], ['--limit', '0'],
               ['--test', '--limit', '1'], ['--all', '--experiment', 'correctness'],
               ['--checkout', '/other'], ['--list', '--output', 'other'])
        for flags in bad:
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    hosted.parse_arguments(['--checkout', str(self.runtime), *flags])

    def test_legacy_check_option_is_supported_but_hidden(self):
        new = hosted.parse_arguments(['--checkout', str(self.runtime), '--test'])
        old = hosted.parse_arguments(['--checkout', str(self.runtime), '--smoke'])
        self.assertEqual(vars(new), vars(old))

    def test_test_alias_uses_the_fixed_minimum_check(self):
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--test']), 0)
        command = execute.call_args.args[1]
        self.assertIn('--test', command)
        self.assertNotIn('--smoke', command)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            hosted.parse_arguments(['--checkout', str(self.runtime), '--test', '--limit', '1'])

    def test_permitted_subset_and_limits_are_forwarded_as_arguments(self):
        flags = ['--group', 'deltabox', '--experiment', 'correctness', '--limit', '1', '--max-events', '3']
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), *flags]), 0)
        command = execute.call_args.args[1]
        self.assertIn('--group', command)
        self.assertEqual(command[command.index('--limit') + 1], '1')
        self.assertEqual(command[command.index('--max-events') + 1], '3')
        self.assertTrue(Path(command[command.index('--output') + 1]).is_relative_to(self.output))

    def test_refuses_checkout_or_root_configuration_written_by_a_reviewer(self):
        for path in (self.policy_path, self.config, self.environment, self.python,
                     self.runtime / 'ae/scripts/run_review.py'):
            with self.subTest(path=path):
                self.untrusted.add(path)
                with self.launcher() as (execute, _, _):
                    self.assertEqual(hosted.main(['--checkout', str(self.runtime)]), 2)
                    execute.assert_not_called()
                self.untrusted.clear()
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.root)]), 2)
            execute.assert_not_called()

    def test_refuses_writable_code_or_virtualenv_packages(self):
        for path in (self.runtime / 'ae/scripts/run_review.py', self.python.parent.parent / 'sitecustomize.py'):
            path.write_text('# writable injection\n')
            path.chmod(0o666)
            with self.subTest(path=path), self.launcher() as (execute, _, _):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime)]), 2)
                execute.assert_not_called()
            path.chmod(0o644)

    def test_explicit_maintainer_owns_parent_source_and_venv_with_a_private_writable_group(self):
        author = self.maintainer_runtime()
        (self.runtime / 'ae/scripts/run_review.py').chmod(0o664)
        self.python.chmod(0o775)
        with self.launcher() as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 0, stderr.getvalue())
        self.assertEqual(author.stat().st_mode & 0o777, 0o755)
        self.assertEqual(execute.call_args.args[0], str(self.python))
        self.assertEqual(execute.call_args.args[2]['AE_HOSTED_CALLER_UID'], str(REVIEWER.pw_uid))

    def test_old_policy_still_refuses_a_maintainer_owned_runtime(self):
        self.maintainer_runtime()
        del self.policy['trusted_maintainer']
        self.policy_path.write_text(json.dumps({key: str(value) for key, value in self.policy.items()}))
        with self.launcher() as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
            execute.assert_not_called()
        self.assertIn('owned by root', stderr.getvalue())

    def test_maintainer_policy_does_not_trust_reviewers_other_owners_or_world_write(self):
        author = self.maintainer_runtime()
        source = self.runtime / 'ae/scripts/run_review.py'
        for path in (author, self.runtime, source, self.python):
            expected = self.owners[path]
            for user in (REVIEWER, OTHER_USER):
                self.owners[path] = (user.pw_uid, user.pw_gid)
                with self.subTest(path=path, user=user.pw_name), self.launcher() as (execute, _, _):
                    self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
                    execute.assert_not_called()
            self.owners[path] = expected
            mode = path.stat().st_mode & 0o777
            path.chmod(mode | 0o002)
            with self.subTest(path=path, world_writable=True), self.launcher() as (execute, _, _):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
                execute.assert_not_called()
            path.chmod(mode)

    def test_writable_group_checks_supplementary_and_primary_members(self):
        self.maintainer_runtime()
        for user in (REVIEWER, OTHER_USER):
            self.groups[MAINTAINER.pw_gid] = [user.pw_name]
            with self.subTest(member=user.pw_name, kind='supplementary'), self.launcher() as (execute, _, _):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
                execute.assert_not_called()
        self.groups[MAINTAINER.pw_gid] = []
        self.users.append(SimpleNamespace(pw_name='primary-only', pw_uid=OTHER_USER.pw_uid, pw_gid=MAINTAINER.pw_gid))
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
            execute.assert_not_called()
        self.users.pop()
        self.groups[MAINTAINER.pw_gid] = ['root', 'dyp']
        with self.launcher() as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 0, stderr.getvalue())

    def test_private_group_cannot_hide_a_reviewer_write_acl(self):
        self.maintainer_runtime()
        for uid, expected in ((REVIEWER.pw_uid, 2), (OTHER_USER.pw_uid, 2), (MAINTAINER.pw_uid, 0)):
            entries = [(0x01, 7, 0xffffffff), (0x02, 6, uid), (0x04, 5, 0xffffffff),
                       (0x10, 7, 0xffffffff), (0x20, 5, 0xffffffff)]
            acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)
            def access_acl(path, name, **kwargs):
                if path == self.runtime and name == 'system.posix_acl_access':
                    return acl
                raise OSError(errno.ENODATA, 'No ACL')
            with self.subTest(uid=uid), self.launcher() as (execute, _, stderr),\
                 patch.object(hosted.os, 'getxattr', side_effect=access_acl):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), expected, stderr.getvalue())
                self.assertEqual(execute.called, expected == 0)

    def test_read_only_result_parent_cannot_grant_reviewer_write_through_default_acl(self):
        self.maintainer_runtime()
        entries = [(0x01, 7, 0xffffffff), (0x02, 7, REVIEWER.pw_uid), (0x04, 5, 0xffffffff),
                   (0x10, 7, 0xffffffff), (0x20, 5, 0xffffffff)]
        acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)
        def inherited_acl(path, name, **kwargs):
            if path == self.output and name == 'system.posix_acl_default':
                return acl
            raise OSError(errno.ENODATA, 'No ACL')
        self.assertEqual(self.output.stat().st_mode & 0o022, 0)
        with self.launcher() as (execute, _, stderr),\
             patch.object(hosted.os, 'getxattr', side_effect=inherited_acl):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
            execute.assert_not_called()
            self.assertIn('ACL is writable', stderr.getvalue())

    def test_maintainer_cannot_own_policy_private_environment_or_fixed_config(self):
        self.maintainer_runtime()
        for path in (self.policy_path, self.environment, self.config):
            self.owners[path] = (MAINTAINER.pw_uid, MAINTAINER.pw_gid)
            with self.subTest(path=path), self.launcher() as (execute, _, stderr):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
                execute.assert_not_called()
                self.assertIn('owned by root', stderr.getvalue())
            self.owners.pop(path)

    def test_policy_does_not_promote_the_reviewer_to_maintainer(self):
        self.policy['trusted_maintainer'] = REVIEWER.pw_name
        self.policy_path.write_text(json.dumps({key: str(value) for key, value in self.policy.items()}))
        with self.launcher() as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
            execute.assert_not_called()
            self.assertIn('reviewer cannot', stderr.getvalue())

    def developer_runtime(self):
        self.maintainer_runtime()
        self.policy['trusted_developer'] = REVIEWER.pw_name
        self.policy_path.write_text(json.dumps({key: str(value) for key, value in self.policy.items()}))

    def test_explicit_developer_can_own_source_and_inherit_workspace_write_acl(self):
        self.developer_runtime()
        source = self.runtime / 'ae/scripts/run_review.py'
        self.owners[source] = (REVIEWER.pw_uid, REVIEWER.pw_gid)
        entries = [(0x01, 7, 0xffffffff), (0x02, 7, REVIEWER.pw_uid), (0x04, 5, 0xffffffff),
                   (0x10, 7, 0xffffffff), (0x20, 5, 0xffffffff)]
        acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)
        def developer_acl(path, name, **kwargs):
            if (path == self.runtime and name == 'system.posix_acl_access') or (
                    path == self.output and name == 'system.posix_acl_default'):
                return acl
            raise OSError(errno.ENODATA, 'No ACL')
        with self.launcher() as (execute, _, stderr), patch.object(hosted.os, 'getxattr', side_effect=developer_acl):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 0, stderr.getvalue())
            execute.assert_called_once()

    def test_developer_policy_does_not_trust_other_accounts(self):
        self.developer_runtime()
        self.policy['trusted_developer'] = OTHER_USER.pw_name
        self.policy_path.write_text(json.dumps({key: str(value) for key, value in self.policy.items()}))
        with self.launcher() as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
            execute.assert_not_called()
            self.assertIn('explicitly allowed caller', stderr.getvalue())

    def test_developer_cannot_own_privileged_control_files(self):
        self.developer_runtime()
        for path in (self.policy_path, self.environment, self.config):
            self.owners[path] = (REVIEWER.pw_uid, REVIEWER.pw_gid)
            with self.subTest(path=path), self.launcher() as (execute, _, stderr):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
                execute.assert_not_called()
                self.assertIn('owned by root', stderr.getvalue())
            self.owners.pop(path)

    def test_developer_result_roots_and_manifest_files_still_require_root_owner(self):
        self.developer_runtime()
        manifest = self.output / 'review.json'
        manifest.write_text('{}')
        with self.launcher():
            trust = hosted.runtime_trust(self.policy)
            for path in (self.output, manifest):
                self.owners[path] = (REVIEWER.pw_uid, REVIEWER.pw_gid)
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'owned by root'):
                    hosted.trusted_path(path, directory=path == self.output, trust=trust, root_leaf=True)
                self.owners[path] = (0, 0)

    def test_caller_environment_cannot_enable_developer_trust(self):
        self.maintainer_runtime()
        self.owners[self.runtime / 'ae/scripts/run_review.py'] = (REVIEWER.pw_uid, REVIEWER.pw_gid)
        with self.launcher({'AE_TRUSTED_DEVELOPER': REVIEWER.pw_name}) as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
            execute.assert_not_called()

    def test_runtime_local_venv_allows_its_trusted_system_python_link(self):
        self.maintainer_runtime()
        system_python = self.root / 'system-python'
        system_python.write_text('# fixed system interpreter\n')
        self.python.unlink()
        self.python.symlink_to(system_python)
        with self.launcher() as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 0, stderr.getvalue())
        self.assertEqual(execute.call_args.args[0], str(self.python))
        packages = self.root / 'external-packages'
        packages.mkdir()
        bad = packages / 'untrusted.py'
        bad.write_text('# injected dependency\n')
        self.untrusted.add(bad)
        (self.python.parent.parent / 'packages').symlink_to(packages)
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
            execute.assert_not_called()

    def test_result_and_work_input_links_are_data_but_source_links_stay_strict(self):
        self.maintainer_runtime()
        fixed = self.root / 'fixed-inputs'
        fixed.mkdir()
        self.owners[fixed] = (MAINTAINER.pw_uid, MAINTAINER.pw_gid)
        (fixed / 'input.json').write_text('{}')
        for directory in (self.runtime / 'ae/work', self.output / 'prior/payload'):
            directory.mkdir(parents=True)
            (directory / 'inputs').symlink_to(fixed)
        with self.launcher() as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 0, stderr.getvalue())
        self.untrusted.add(fixed)
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
            execute.assert_not_called()
        self.untrusted.clear()
        source_link = self.runtime / 'ae/scripts/injection'
        for target in (fixed, self.runtime / 'ae/work', self.output / 'prior'):
            source_link.symlink_to(target)
            with self.subTest(target=target), self.launcher() as (execute, _, _):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
                execute.assert_not_called()
            source_link.unlink()

    def test_result_and_work_roots_cannot_be_external_directory_links(self):
        self.maintainer_runtime()
        (self.runtime / 'ae/work').symlink_to(self.root)
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
            execute.assert_not_called()

    def test_materialized_paper_and_trace_data_can_reference_imported_work_objects(self):
        self.maintainer_runtime()
        ae = self.runtime / 'ae'
        objects = ae / 'work/paper-data/objects'
        objects.mkdir(parents=True)
        raw = b'{"fixed": "paper input"}'
        digest = hashlib.sha256(raw).hexdigest()
        (objects / digest).write_bytes(raw)
        (ae / 'traces').mkdir()
        (ae / 'traces/objects').symlink_to(objects)
        source = importlib.util.spec_from_file_location('hosted_paper_data_fixture', ROOT / 'ae/scripts/paper_data.py')
        paper_data = importlib.util.module_from_spec(source)
        source.loader.exec_module(paper_data)
        rows = [dict(target='paper/figure-01/data/fixed.json', sha256=digest)]
        paper_data.materialize(ae, rows)
        paper_data.verify(ae, rows, {digest: len(raw)})
        with self.launcher() as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 0, stderr.getvalue())
        source_link = ae / 'repro/imported.py'
        source_link.parent.mkdir()
        source_link.symlink_to(ae / rows[0]['target'])
        with self.launcher() as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
            execute.assert_not_called()
            self.assertIn('source link enters generated data', stderr.getvalue())

    def test_temporary_directory_is_fixed_by_root_policy_inside_runtime_work(self):
        self.maintainer_runtime()
        temporary = self.runtime / 'ae/work/hosted/tmp'
        temporary.mkdir(parents=True)
        self.policy['temporary_root'] = temporary
        self.policy_path.write_text(json.dumps({key: str(value) for key, value in self.policy.items()}))
        with self.launcher({'TMPDIR': '/caller', 'TMP': '/caller', 'TEMP': '/caller'}) as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 0, stderr.getvalue())
        environment = execute.call_args.args[2]
        self.assertEqual(environment['TMPDIR'], str(temporary))
        self.assertNotIn('TMP', environment)
        self.assertNotIn('TEMP', environment)

    def test_temporary_directory_rejects_escape_links_and_nonroot_or_writable_leaf(self):
        self.maintainer_runtime()
        temporary = self.runtime / 'ae/work/hosted/tmp'
        temporary.mkdir(parents=True)
        alias = temporary.parent / 'alias'
        alias.symlink_to(self.root)
        for path in (self.root, alias):
            self.policy['temporary_root'] = path
            self.policy_path.write_text(json.dumps({key: str(value) for key, value in self.policy.items()}))
            with self.subTest(path=path), self.launcher() as (execute, _, _):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
                execute.assert_not_called()
        alias.unlink()
        self.policy['temporary_root'] = temporary
        self.policy_path.write_text(json.dumps({key: str(value) for key, value in self.policy.items()}))
        for owner, mode in ((MAINTAINER.pw_uid, 0o755), (REVIEWER.pw_uid, 0o755), (0, 0o777)):
            self.owners[temporary] = (owner, 0)
            temporary.chmod(mode)
            with self.subTest(owner=owner, mode=mode), self.launcher() as (execute, _, _):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
                execute.assert_not_called()

    def test_refuses_code_link_outside_fixed_runtime(self):
        (self.root / 'outside.py').write_text('# outside checkout\n')
        (self.runtime / 'injection.py').symlink_to(self.root / 'outside.py')
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime)]), 2)
            execute.assert_not_called()

    def test_python_symlink_keeps_the_virtualenv_entrypoint_and_checks_external_packages(self):
        base = self.root / 'system-python'
        base.write_text('# root-owned interpreter\n')
        self.python.unlink()
        self.python.symlink_to(base)
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 0)
        self.assertEqual(execute.call_args.args[0], str(self.python))
        packages = self.root / 'external-packages'
        packages.mkdir()
        injected = packages / 'injected.py'
        injected.write_text('# reviewer can edit this\n')
        self.untrusted.add(injected)
        (self.python.parent.parent / 'packages').symlink_to(packages, target_is_directory=True)
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 2)
            execute.assert_not_called()

    def test_api_environment_cannot_override_execution_settings(self):
        for key, value in (('PATH', '/attacker'), ('PYTHONPATH', '/attacker'),
                           ('SUDO_UID', '7001'), ('E2B_API_KEY', 'line1\nline2'), ('E2B_TEMPLATE', 2)):
            self.environment.write_text(json.dumps({key: value}))
            with self.subTest(key=key, value=value), self.launcher() as (execute, _, _):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime)]), 2)
                execute.assert_not_called()

    def test_output_rejects_escape_symlinks_and_reuse(self):
        (self.output / 'alias').symlink_to(self.root, target_is_directory=True)
        (self.output / 'existing').mkdir()
        for value in ('../outside', str(self.root / 'outside'), str(self.output), 'alias/new', 'existing'):
            with self.subTest(value=value), self.launcher() as (execute, _, _):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--output', value]), 2)
                execute.assert_not_called()
        self.assertFalse((self.root / 'new').exists())

    def test_resume_accepts_only_root_owned_immutable_metadata(self):
        result = self.output / 'prior'
        result.mkdir()
        (result / 'review.json').write_text('{}')
        (result / 'SUMMARY.md').write_text('# prior\n')
        plan = result / 'plan.json'
        plan.write_text('{}')
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--resume', 'prior']), 0)
        self.assertEqual(execute.call_args.args[1][-2:], ['--resume', str(result)])
        for owner, mode in ((REVIEWER.pw_uid, 0o644), (0, 0o666)):
            if owner:
                self.untrusted.add(plan)
            plan.chmod(mode)
            with self.subTest(owner=owner, mode=mode), self.launcher() as (execute, _, _):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--resume', 'prior']), 2)
                execute.assert_not_called()
            self.untrusted.clear()
        plan.chmod(0o644)
        (result / 'review.json').unlink()
        (result / 'review.json').symlink_to(self.config)
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--resume', 'prior']), 2)
            execute.assert_not_called()

    def test_maintainer_ancestors_do_not_relax_resume_or_audit_control_ownership(self):
        self.maintainer_runtime()
        result = self.output / 'prior'
        result.mkdir()
        for name in ('review.json', 'SUMMARY.md', 'plan.json', 'suite.json', 'run.json'):
            (result / name).write_text('{}')
        plans = result / 'plans/attempt-001'
        plans.mkdir(parents=True)
        (plans / 'correctness.json').write_text('{}')
        payload = result / 'payload'
        payload.mkdir()
        fixed = self.root / 'fixed-inputs'
        fixed.mkdir()
        (payload / 'inputs').symlink_to(fixed)
        with self.launcher() as (execute, _, stderr):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--resume', 'prior']), 0, stderr.getvalue())
        for path in (result / 'review.json', result / 'plan.json', result / 'suite.json',
                     result / 'run.json', plans / 'correctness.json', self.output / '.launcher-audit.jsonl'):
            self.owners[path] = (MAINTAINER.pw_uid, MAINTAINER.pw_gid)
            with self.subTest(path=path), self.launcher() as (execute, _, _):
                self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--resume', 'prior']), 2)
                execute.assert_not_called()
            self.owners.pop(path)
        (plans / 'correctness.json').unlink()
        (plans / 'correctness.json').symlink_to(self.config)
        with self.launcher() as (execute, _, _):
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--resume', 'prior']), 2)
            execute.assert_not_called()

    def test_lock_refuses_a_second_run_until_the_first_releases_it(self):
        with self.owned_fixture():
            first = hosted.acquire_lock(self.policy['lock_file'])
            try:
                with self.assertRaisesRegex(ValueError, 'Another hosted AE run'):
                    hosted.acquire_lock(self.policy['lock_file'])
            finally:
                os.close(first)
            second = hosted.acquire_lock(self.policy['lock_file'])
            os.close(second)

    def test_lock_is_still_held_and_inheritable_at_exec(self):
        observed = []
        with self.launcher() as (execute, _, _),\
             patch.object(hosted.os, 'set_inheritable', wraps=os.set_inheritable) as inherit:
            def inspect_exec(*_):
                fd = inherit.call_args.args[0]
                observed.append(os.get_inheritable(fd))
                with self.assertRaisesRegex(ValueError, 'Another hosted AE run'):
                    hosted.acquire_lock(self.policy['lock_file'])
            execute.side_effect = inspect_exec
            self.assertEqual(hosted.main(['--checkout', str(self.runtime), '--list']), 0)
        self.assertEqual(observed, [True])

    def test_sticky_lock_parent_is_safe_but_writable_nonsticky_parent_is_refused(self):
        directory = self.root / 'locks'
        directory.mkdir(mode=0o1777)
        directory.chmod(0o1777)
        with self.owned_fixture():
            fd = hosted.acquire_lock(directory / 'ae.lock')
            os.close(fd)
            directory.chmod(0o777)
            with self.assertRaisesRegex(ValueError, 'writable'):
                hosted.acquire_lock(directory / 'ae.lock')

    def test_selector_whitelist_matches_the_runtime_catalog(self):
        source = importlib.util.spec_from_file_location('review_launcher_catalog', ROOT / 'ae/scripts/run_review.py')
        review = importlib.util.module_from_spec(source)
        source.loader.exec_module(review)
        self.assertEqual(set(hosted.EXPERIMENTS), set(review.EXPERIMENTS))
        self.assertEqual(set(hosted.GROUPS), set(review.GROUPS))

    @unittest.skipIf(os.geteuid() == 0, 'wrapper delegates only for non-root callers')
    def test_shell_wrapper_delegates_before_loading_caller_python_or_runtime_override(self):
        binary = self.root / 'bin'
        binary.mkdir()
        sudo = binary / 'sudo'
        sudo.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        sudo.chmod(0o755)
        env = {**os.environ, 'PATH': str(binary) + ':' + os.environ.get('PATH', ''),
               'AE_HOSTED_LAUNCHER': '/usr/local/sbin/deltabox-ae-run', 'AE_PYTHON': '/must-not-run'}
        flags = ['--runtime-repo', '/other', '--test']
        result = subprocess.run(['bash', str(ROOT / 'ae/run_all.sh'), *flags], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['-n', '--', env['AE_HOSTED_LAUNCHER'],
                                                    '--checkout', str(ROOT), *flags])


if __name__ == '__main__':
    unittest.main()
