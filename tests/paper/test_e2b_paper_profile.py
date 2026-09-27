"""Strict paper-nested admission and planning, without starting a guest."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from ae.scripts import e2b_paper_profile as profile
from ae.scripts import hosted_launcher as hosted
from ae.scripts import run_review as review
from ae.repro import catalog
from ae import reproduce


class ProfileTests(unittest.TestCase):
    def args(self, extra=()):
        return review.parser().parse_args(['--experiment', 'table-02-e2b',
                '--e2b-profile', 'paper-nested', '--config', str(profile.ROOT_CONFIG), *extra])

    def test_explicit_fixed_profile_is_accepted(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            profile.validate(self.args())

    def test_rejects_every_scope_or_placement_override(self):
        variants = [
            ['--all'], ['--test'], ['--available'], ['--list'], ['--group', 'table-02'],
            ['--experiment', 'table-02-cube'], ['--experiment', 'table-02-e2b'],
            ['--limit', '8'], ['--max-events', '185'], ['--resume', '/tmp/prior'],
            ['--reuse-completed-from', '/tmp/prior'], ['--no-pin'], ['--numa-node', '1'],
            ['--cpus', '28-31'], ['--cube-profile', 'paper-disk'],
            ['--experiment-config', 'table-02-e2b=/tmp/other'],
            ['--config', '/tmp/other'], ['--analyze-existing', '/tmp/prior'],
            ['--execute-plan', '/tmp/plan'], ['--probe-plan', '/tmp/plan'],
        ]
        with mock.patch.dict(os.environ, {}, clear=True):
            for flags in variants:
                with self.subTest(flags=flags), self.assertRaises((ValueError, SystemExit)):
                    profile.validate(self.args(flags))

    def test_rejects_environment_overrides(self):
        for key in ('AE_CPUS', 'AE_NUMA_NODE', 'AE_CONFIG'):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: 'injected'}, clear=True), self.assertRaises(ValueError):
                profile.validate(self.args())

    def test_missing_explicit_selection_rejected(self):
        args = self.args()
        args.experiment = None
        with self.assertRaises(ValueError):
            profile.validate(args)

    def test_paper_callbacks_fail_closed_and_preserve_not_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'plan.json'
            plan = {'review_config': '/config', 'review_output': str(root/'suite'),
                    'jobs': [{'key': 'one', 'experiment': 'table-02-e2b'},
                             {'key': 'two', 'experiment': 'table-02-e2b'}], 'workers': 1}
            path.write_text(json.dumps(plan))
            config = profile.effective({}, profile.PROFILE)
            before = mock.Mock(side_effect=ValueError('fresh proof invalid'))
            after = mock.Mock()
            with mock.patch.object(review, 'load_config', return_value=config), mock.patch.object(review, 'repository_state', return_value={}), mock.patch.object(review, 'host_state', return_value={}), mock.patch.object(review, 'from_environment', return_value={}), mock.patch.object(review, 'make_output_accessible'), mock.patch.object(review, 'execute_review_job') as execute:
                code = review.execute_plan(path, paper_context_ready=True, paper_before_job=before, paper_after_job=after)
            self.assertEqual(code, 1)
            execute.assert_not_called(); after.assert_not_called()
            suite = json.loads((root/'suite/suite.json').read_text())
            self.assertEqual([j['status'] for j in suite['jobs']], ['failed', 'not-run'])
            self.assertIn('fresh proof invalid', suite['jobs'][0]['paper_context_error'])

    def test_paper_after_callback_failure_stops_next_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'plan.json'
            plan = {'review_config': '/config', 'review_output': str(root/'suite'),
                    'jobs': [{'key': 'one', 'experiment': 'table-02-e2b'},
                             {'key': 'two', 'experiment': 'table-02-e2b'}], 'workers': 1}
            path.write_text(json.dumps(plan))
            config = profile.effective({}, profile.PROFILE)
            before = mock.Mock(); after = mock.Mock(side_effect=ValueError('guest SHA changed'))
            with mock.patch.object(review, 'load_config', return_value=config), mock.patch.object(review, 'repository_state', return_value={}), mock.patch.object(review, 'host_state', return_value={}), mock.patch.object(review, 'from_environment', return_value={}), mock.patch.object(review, 'make_output_accessible'), mock.patch.object(review, 'execute_review_job', return_value={'status': 'ok'}) as execute:
                code = review.execute_plan(path, paper_context_ready=True, paper_before_job=before, paper_after_job=after)
            self.assertEqual(code, 1)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(before.call_count, 1)
            suite = json.loads((root/'suite/suite.json').read_text())
            self.assertEqual([j['status'] for j in suite['jobs']], ['failed', 'not-run'])

    def test_callbacks_cannot_be_injected_into_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'plan.json'
            path.write_text(json.dumps({'review_config': '/config', 'review_output': str(root/'suite')}))
            with mock.patch.object(review, 'load_config', return_value={}), self.assertRaises(ValueError):
                review.execute_plan(path, paper_context_ready=True, paper_before_job=mock.Mock())
            self.assertFalse((root/'suite').exists())

    def test_default_is_unchanged_and_copied(self):
        config = {'measurement': {'cpus': '52-55'}, 'e2b': {'from_build': 'old'}, 'baseline_storage': 'tmpfs'}
        value = profile.effective(config, None)
        self.assertEqual(value, config)
        value['e2b']['from_build'] = 'new'
        self.assertEqual(config['e2b']['from_build'], 'old')

    def test_effective_removes_parent_and_guest_injection(self):
        config = {'e2b': {'from_build': 'old', 'ssh_host': 'attacker', 'storage': '/other',
                          'warm_action_worker': True, 'execution': 'local'}}
        value = profile.effective(config, profile.PROFILE)
        profile.validate_effective(value)
        self.assertNotIn('from_build', value['e2b'])
        self.assertNotIn('ssh_host', value['e2b'])
        self.assertFalse(value['e2b']['warm_action_worker'])
        self.assertTrue(value['e2b']['fresh_base_per_input'])
        self.assertEqual(value['measurement'], {'pin': True, 'numa_node': 1, 'cpus': '28-31'})

    def test_effective_fixed_resources_cannot_be_changed(self):
        for key, bad in [('vcpus', 4), ('mem_mib', 4096), ('fresh_base_per_input', False),
                         ('warm_action_worker', True), ('paper_manifest', '/tmp/bad'),
                         ('execution', 'ssh'), ('ssh_host', 'injected'), ('vcpus', True)]:
            value = profile.effective({}, profile.PROFILE)
            value['e2b'][key] = bad
            with self.subTest(key=key), self.assertRaises(ValueError):
                profile.validate_effective(value)

    def test_hosted_selection_is_strict(self):
        base = ['--checkout', '/repo', '--experiment', 'table-02-e2b', '--e2b-profile', profile.PROFILE]
        args = hosted.parse_arguments(base)
        self.assertEqual(args.e2b_profile, profile.PROFILE)
        for flags in (['--limit', '8'], ['--resume', '/tmp/x'], ['--reuse-completed-from', '/tmp/x'],
                      ['--experiment', 'table-02-cube'], ['--cube-profile', 'paper-disk'],
                      ['--test'], ['--group', 'table-02'], ['--cpus', '28-31'],
                      ['--config', '/tmp/other'], ['--e2b-profile', profile.PROFILE]):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                hosted.parse_arguments([*base, *flags])

    def test_hosted_forwards_only_profile_flag(self):
        args = hosted.parse_arguments(['--checkout', '/repo', '--experiment', 'table-02-e2b',
                                      '--e2b-profile', profile.PROFILE])
        command = hosted.command_line({'python': Path('/python'), 'runtime_root': Path('/repo'),
                                       'config': profile.ROOT_CONFIG}, args, Path('/result'))
        self.assertEqual(command[command.index('--e2b-profile') + 1], profile.PROFILE)
        self.assertNotIn('--cpus', command)

    def test_wrapper_runs_before_suite_directory_creation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = root / 'config.json'
            config.write_text(json.dumps(profile.effective({}, profile.PROFILE)))
            plan = root / 'plan.json'
            output = root / 'not-created'
            plan.write_text(json.dumps({'review_config': str(config), 'review_output': str(output)}))
            suite = SimpleNamespace(run=mock.Mock(return_value=73))
            with mock.patch.dict('sys.modules', {'ae.scripts.e2b_paper_suite': suite}):
                self.assertEqual(review.execute_plan(plan), 73)
            suite.run.assert_called_once_with(plan)
            self.assertFalse(output.exists())


class InputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input_root = self.root / 'inputs'
        self.manifest_path = self.root / 'manifest.json'
        self.cohort = []
        self.rows = []
        for index, row in enumerate(profile.COHORT):
            name, _, _, commit, expansions, actions = row
            directory = self.input_root / 'ms' / name
            directory.mkdir(parents=True)
            raw = json.dumps({'instance': name, 'index': index}).encode()
            rtt = b'{"dur_s": 1}\n'
            trace = directory / 'trajectory.json'; trace.write_bytes(raw)
            rtt_path = directory / 'ms_trace.jsonl'; rtt_path.write_bytes(rtt)
            sha = lambda content: hashlib.sha256(content).hexdigest()
            self.cohort.append((name, sha(raw), sha(rtt), commit, expansions, actions))
            self.rows.append(dict(instance=name, repository_commit=commit, expansions=expansions,
                    actions=actions, trajectory=dict(path=str(trace), sha256=sha(raw), bytes=len(raw)),
                    rtt=dict(path=str(rtt_path), sha256=sha(rtt), bytes=len(rtt))))
        self.contract = self.input_root / 'e2b-paper-185-input-action-contract.json'
        self.contract.write_text(json.dumps(dict(n_inputs=8, n_observed_expansions=227,
                     n_measured_checkpoint_restore_pairs=185, inputs=[{'instance': r[0]} for r in self.cohort])))
        self.contract_sha = hashlib.sha256(self.contract.read_bytes()).hexdigest()
        self.manifest = dict(schema_version=1, profile=profile.PROFILE, inputs=self.rows,
                         contract=dict(path=str(self.contract), sha256=self.contract_sha))
        self.manifest_path.write_text(json.dumps(self.manifest))
        for name, value in [('INPUT_ROOT', self.input_root), ('MANIFEST', self.manifest_path),
                            ('COHORT', tuple(self.cohort)), ('CONTRACT_SHA256', self.contract_sha)]:
            patch = mock.patch.object(profile, name, value)
            patch.start(); self.addCleanup(patch.stop)
        self.config = profile.effective({}, profile.PROFILE)

    @contextlib.contextmanager
    def fixture_reads(self):
        # Only manifest validation tests replace ownership admission; separate
        # tests below exercise real file/symlink/mode/uid gates.
        with mock.patch.object(profile, '_root_read', side_effect=lambda p: Path(p).read_bytes()):
            yield

    def test_all_eight_inputs_and_exact_order_are_bound(self):
        with self.fixture_reads():
            proof = profile.verify_inputs(self.config)
        self.assertEqual(len(proof['inputs']), 8)
        self.assertEqual(proof['observed_expansions'], 227)
        self.assertEqual(proof['measured_actions'], 185)

    def test_wrong_hash_order_commit_and_coverage_are_rejected(self):
        variants = []
        for key, value in [('repository_commit', '0' * 40), ('actions', 197), ('instance', 'other')]:
            m = copy.deepcopy(self.manifest);m['inputs'][0][key] = value;variants.append(m)
        m = copy.deepcopy(self.manifest);m['inputs'].reverse();variants.append(m)
        m = copy.deepcopy(self.manifest);m['inputs'][0]['rtt']['sha256'] = '0' * 64;variants.append(m)
        m = copy.deepcopy(self.manifest);m['inputs'][0]['trajectory']['bytes'] += 1;variants.append(m)
        m = copy.deepcopy(self.manifest);m['contract']['sha256'] = '0' * 64;variants.append(m)
        for m in variants:
            self.manifest_path.write_text(json.dumps(m))
            with self.subTest(manifest=m), self.fixture_reads(), self.assertRaises(ValueError):
                profile.verify_inputs(self.config)

    def test_changed_recording_bytes_are_rejected(self):
        Path(self.rows[0]['trajectory']['path']).write_bytes(b'tampered')
        with self.fixture_reads(), self.assertRaises(ValueError):
            profile.verify_inputs(self.config)

    def test_planner_uses_old_inputs_and_explicit_base_commit(self):
        with self.fixture_reads():
            jobs = catalog.build_jobs(['table-02-e2b'], self.config, Path('/config'), Path('/result'))
        self.assertEqual(len(jobs), 8)
        for job, row in zip(jobs, self.rows):
            cmd = job['command']
            self.assertEqual(cmd[cmd.index('--trace') + 1], row['trajectory']['path'])
            self.assertEqual(cmd[cmd.index('--repository-commit') + 1], row['repository_commit'])
            self.assertEqual(job['paper_contract']['expected_actions'], row['actions'])
            self.assertNotIn('--limit', cmd)

    def test_planner_rejects_mixed_or_limited_profile(self):
        for selected, limit, events in [(['table-02-e2b', 'table-02-cube'], None, None),
                                         (['table-02-e2b'], 8, None), (['table-02-e2b'], None, 185)]:
            with self.subTest(selected=selected, limit=limit, events=events), self.assertRaises(ValueError):
                catalog.build_jobs(selected, self.config, Path('/config'), Path('/result'), limit, events)

    def test_doctor_never_probes_pending_guest_or_parent(self):
        config = dict(self.config, payload=str(self.root), moatless_venv=str(self.root))
        with self.fixture_reads(), mock.patch.object(socket, 'create_connection', side_effect=AssertionError('no network')):
            result = reproduce.doctor(config, ['table-02-e2b'])
        names = [c['name'] for c in result['checks']]
        self.assertIn('e2b.paper static frozen input contract', names)
        self.assertNotIn('e2b.from_build', names)
        self.assertNotIn('e2b.ssh_host', names)

    def test_root_read_rejects_symlink_and_writable_file(self):
        p = self.root / 'plain';p.write_text('x');p.chmod(0o666)
        with self.assertRaises(ValueError):
            profile._root_read(p)
        link = self.root / 'link';link.symlink_to(p)
        with self.assertRaises(ValueError):
            profile._root_read(link)

    def test_root_read_rejects_non_root_owner_even_when_readonly(self):
        p = self.root / 'plain';p.write_text('x');p.chmod(0o444)
        actual = p.stat()
        bad = SimpleNamespace(**{k:getattr(actual,k) for k in
              ('st_mode','st_uid','st_nlink','st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')})
        bad.st_uid = 12345
        with mock.patch.object(profile.os, 'fstat', return_value=bad), self.assertRaises(ValueError):
            profile._root_read(p)


if __name__ == '__main__':
    unittest.main()
