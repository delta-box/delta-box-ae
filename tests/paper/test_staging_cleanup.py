"""Reclaim only private staging after measurement; keep verifiable evidence."""
import hashlib
import contextlib
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae'))
from repro.common import file_record, write_json
from repro.staging_cleanup import cleanup_reconstructable_staging, verify_artifacts


class StagingCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.run = self.base / 'run'
        self.repo = self.run / 'payload/repos/swe-bench_owner__project-1'
        self.repo.mkdir(parents=True)
        (self.repo / 'tracked.py').write_text('recorded pristine checkout\n')
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / 'keep.txt').write_text('shared source stays intact')
        self.nltk = self.run / 'nltk_data'
        self.nltk.mkdir()
        nltk_file = self.nltk / 'english.pickle'
        nltk_file.write_bytes(b'real fixture data')
        (self.source / 'english.pickle').write_bytes(nltk_file.read_bytes())
        history = self.run / 'payload/moatless-det-src/moatless'
        history.mkdir(parents=True)
        (history / 'message_history.py').write_text('private adapted source')
        self.table = history / '_replay_history_order.json'
        self.table.write_text('{"orders": []}')
        self.measurement = self.run / 'measurements.json'
        self.measurement.write_text('{"latency_ms": 1.25}')
        self.provenance = dict(instance='owner__project-1', commit='a' * 40,
                               source=str(self.source), checkout='fresh git clone, detached trace commit')
        write_json(self.run / 'repository.json', self.provenance)
        self.config = dict(status='ok', analysis_mode='fresh-measurement', experiment='table-02-replay',
                           instance='owner__project-1', artifacts=[self.artifact(self.measurement), self.artifact(self.table)],
                           local_dependencies={'nltk_data': dict(source=str(self.source), staged=str(self.nltk),
                                                                files=[file_record(nltk_file)])})
        self.save()

    def artifact(self, path):
        return dict(file_record(path), path=str(path.relative_to(self.run)))

    def save(self):
        write_json(self.run / 'run.json', self.config)

    def test_removes_only_reconstructable_copies_and_preserves_fresh_analysis(self):
        before = (self.run / 'run.json').read_bytes()
        result = cleanup_reconstructable_staging(self.run)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['removed'], ['payload/repos', 'nltk_data'])
        self.assertFalse(self.repo.parent.exists())
        self.assertFalse(self.nltk.exists())
        self.assertEqual((self.run / 'run.json').read_bytes(), before)
        self.assertTrue(self.table.is_file())
        self.assertTrue((self.table.parent / 'message_history.py').is_file())
        self.assertEqual((self.source / 'keep.txt').read_text(), 'shared source stays intact')
        self.assertEqual(len(verify_artifacts(self.run).artifacts), 2)
        report = json.loads((self.run / 'staging-cleanup.json').read_text())
        self.assertEqual(report['producer_manifest']['sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(report['directories'][0]['reconstruction']['commit'], 'a' * 40)
        self.assertGreater(report['removed_allocated_bytes'], 0)

    def test_failed_producer_is_kept_for_debugging(self):
        self.config['status'] = 'failed'
        self.save()
        with self.assertRaisesRegex(ValueError, 'successful fresh'):
            cleanup_reconstructable_staging(self.run)
        self.assertTrue(self.repo.exists())
        self.assertTrue(self.nltk.exists())

    def test_artifact_inside_a_cleanup_target_rejects_the_whole_cleanup(self):
        self.config['artifacts'].append(self.artifact(self.repo / 'tracked.py'))
        self.save()
        with self.assertRaisesRegex(ValueError, 'contains a measured artifact'):
            cleanup_reconstructable_staging(self.run)
        self.assertTrue(self.repo.exists())
        self.assertTrue(self.nltk.exists())

    def test_changed_measurement_hash_prevents_removal(self):
        self.measurement.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            cleanup_reconstructable_staging(self.run)
        self.assertTrue(self.repo.exists())

    def test_symlink_target_or_parent_never_reaches_shared_data(self):
        original = self.run / 'payload'
        moved = self.base / 'preserved-payload'
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'not a symlink'):
            cleanup_reconstructable_staging(self.run)
        self.assertTrue((moved / 'repos/swe-bench_owner__project-1/tracked.py').is_file())
        self.assertTrue(self.nltk.exists())

    def test_nested_checkout_symlink_is_unlinked_without_following_external_data(self):
        (self.repo / 'external').symlink_to(self.source, target_is_directory=True)
        cleanup_reconstructable_staging(self.run)
        self.assertTrue((self.source / 'keep.txt').is_file())

    def test_missing_reconstruction_provenance_preserves_both_directories(self):
        self.config['local_dependencies'] = {}
        self.save()
        with self.assertRaisesRegex(ValueError, 'reconstruction provenance'):
            cleanup_reconstructable_staging(self.run)
        self.assertTrue(self.repo.exists())
        self.assertTrue(self.nltk.exists())

    def test_changed_reconstruction_source_prevents_deletion(self):
        (self.source / 'english.pickle').write_bytes(b'changed cache')
        with self.assertRaisesRegex(ValueError, 'reconstruction source differs'):
            cleanup_reconstructable_staging(self.run)
        self.assertTrue(self.repo.exists())
        self.assertTrue(self.nltk.exists())

    def test_partial_cleanup_failure_records_completed_and_remaining_paths(self):
        remove = shutil.rmtree
        def fail_second(path):
            if Path(path).name == 'nltk_data':
                raise PermissionError('fixture directory cannot be removed')
            remove(path)
        with patch('repro.staging_cleanup.shutil.rmtree', side_effect=fail_second) as patched:
            patched.avoids_symlink_attacks = True
            with self.assertRaisesRegex(PermissionError, 'fixture directory'):
                cleanup_reconstructable_staging(self.run)
        report = json.loads((self.run / 'staging-cleanup.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual([row['status'] for row in report['directories']], ['removed', 'pending'])
        self.assertTrue(self.nltk.exists())
        self.assertEqual(len(verify_artifacts(self.run).artifacts), 2)


class E2BStagingCleanupTests(unittest.TestCase):
    BASE = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    ANCESTOR = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    CHILDREN = ('11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222')
    OTHER = '33333333-3333-4333-8333-333333333333'

    def setUp(self):
        from ae.runners.e2b_environment import SNAPSHOT_FILES, _parent_dependencies
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.runtime = self.base / 'runtime'
        self.storage = self.runtime / 'ae/work/e2b-storage'
        self.run = self.runtime / 'ae/results/hosted/job'
        self.run.mkdir(parents=True)
        self.owners = {}
        self.children = [self.storage / 'templates' / build for build in self.CHILDREN]
        for build in (self.BASE, self.ANCESTOR, *self.CHILDREN, self.OTHER):
            directory = self.storage / 'templates' / build
            directory.mkdir(parents=True)
            for name in SNAPSHOT_FILES:
                (directory / name).write_bytes((build + ':' + name).encode())
        self.parent_file = self.storage / 'templates' / self.ANCESTOR / 'memfile'
        self.parent_path = self.storage / 'parent-isolation.json'
        rows = []
        for build in (self.BASE, self.ANCESTOR):
            for name in SNAPSHOT_FILES:
                path = self.storage / 'templates' / build / name
                rows.append(dict(relative=str(path.relative_to(self.storage)), bytes=path.stat().st_size,
                                 sha256=file_record(path)['sha256']))
        headers = {build: [dict(build=build, file=name,
                    referenced_builds={self.ANCESTOR: 1} if build == self.BASE else {})
                    for name in ('memfile.header', 'rootfs.ext4.header')]
                    for build in (self.BASE, self.ANCESTOR)}
        self.parents = dict(schema_version=1, status='verified', independent_copies=True, source_unchanged=True,
                            destination=str(self.storage), base_build=self.BASE, headers=headers, build_count=2,
                            files=rows, total_bytes=sum(row['bytes'] for row in rows),
                            snapshot_bytes=sum(row['bytes'] for row in rows))
        write_json(self.parent_path, self.parents)
        self.fixed = dict(e2b=dict(execution='local', storage=str(self.storage), from_build=self.BASE,
                                  parent_manifest=str(self.parent_path)))
        self.config_path = self.base / 'fixed.json'
        write_json(self.config_path, self.fixed)
        self.policy_path = self.base / 'launcher.json'
        self.policy = dict(runtime_root=str(self.runtime), output_root=str(self.run.parent),
                           python=str(self.runtime / '.venv/bin/python'), config=str(self.config_path),
                           environment_file=str(self.base / 'private-environment.json'),
                           lock_file=str(self.base / 'launcher.lock'), allowed_user='fixture-reviewer',
                           trusted_maintainer='fixture-author')
        write_json(self.policy_path, self.policy)
        self.pilot_path = self.run / 'driver/results/fixture/pilot_result.json'
        self.pilot = dict(ok=True, instance='owner__project-1', n_e2b_steps=1,
                          create=dict(ok=True, build_id=self.BASE, reused=True),
                          root_setup=dict(ok=True, to_build=self.CHILDREN[0], from_build=''),
                          iterations=[dict(ok=True, e2b_steps=[dict(ok=True, to_build=self.CHILDREN[1], from_build='')])])
        self.measurement = self.run / 'measurement.json'
        self.measurement.write_text('{"checkpoint_ms": 12.5}\n')
        self.log = self.run / 'stdout.log'
        self.log.write_text('original measurement log\n')
        self.producer = dict(status='ok', analysis_mode='fresh-measurement', experiment='table-02-e2b',
                             backend='e2b', instance='owner__project-1', config=self.fixed,
                             e2b_environment=dict(execution='local', storage=str(self.storage),
                                 parent_manifest=file_record(self.parent_path), snapshot_inputs_unchanged=True,
                                 snapshot_dependencies=_parent_dependencies(self.parents, self.storage, self.BASE)))
        self.save()

    def save(self):
        write_json(self.pilot_path, self.pilot)
        self.producer['result'] = dict(file_record(self.pilot_path), path=str(self.pilot_path.relative_to(self.run)))
        self.producer['artifacts'] = [self.producer['result'],
                                     dict(file_record(self.measurement), path='measurement.json')]
        write_json(self.run / 'run.json', self.producer)

    @contextlib.contextmanager
    def owned_fixture(self, hosted_context=True):
        from ae.scripts import hosted_launcher as hosted
        real_lstat = Path.lstat
        def metadata(path):
            original = real_lstat(path)
            values = {name: getattr(original, name) for name in dir(original) if name.startswith('st_')}
            values['st_uid'], values['st_gid'] = self.owners.get(path, (0, 0))
            if not path.is_relative_to(self.base) and stat.S_ISDIR(original.st_mode):
                values['st_mode'] = stat.S_IFDIR | 0o755
            return SimpleNamespace(**values)
        accounts = {'fixture-author': SimpleNamespace(pw_uid=1010),
                    'fixture-reviewer': SimpleNamespace(pw_uid=1012)}
        with patch.object(Path, 'lstat', metadata), patch.object(hosted, 'POLICY_PATH', self.policy_path), \
             patch.object(hosted.pwd, 'getpwnam', side_effect=accounts.__getitem__), \
             patch.dict(os.environ, {'AE_HOSTED_CALLER_UID': '1012'} if hosted_context else {}, clear=True), \
             patch.object(hosted.os, 'getxattr', side_effect=OSError(errno.ENODATA, 'No ACL'), create=True):
            yield

    def cleanup(self, hosted_context=True):
        with self.owned_fixture(hosted_context):
            return cleanup_reconstructable_staging(self.run)

    def assert_children_kept(self):
        self.assertTrue(all(path.is_dir() for path in self.children))
        self.assertTrue((self.storage / 'templates' / self.BASE).is_dir())
        self.assertTrue((self.storage / 'templates' / self.ANCESTOR).is_dir())

    def test_removes_exact_owned_children_and_preserves_evidence_parents_and_siblings(self):
        original = {path: path.read_bytes() for path in (self.parent_path, self.parent_file,
                    self.run / 'run.json', self.pilot_path, self.measurement, self.log)}
        self.owners[self.storage] = (1010, 0)
        result = self.cleanup()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['removed'], [str(path) for path in self.children])
        self.assertEqual(result['absent'], [])
        self.assertFalse(any(path.exists() for path in self.children))
        self.assertTrue((self.storage / 'templates' / self.OTHER).is_dir())
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(len(verify_artifacts(self.run).artifacts), 2)
        report = json.loads((self.run / 'staging-cleanup.json').read_text())
        self.assertEqual(report['e2b']['protected_builds'], sorted([self.BASE, self.ANCESTOR]))
        self.assertEqual(report['e2b']['parent_verification'], 'unchanged after cleanup')
        self.assertGreater(report['removed_allocated_bytes'], 0)
        self.assertEqual(report['absent_allocated_bytes'], 0)

    def test_already_absent_child_is_recorded_without_claiming_reclaimed_bytes(self):
        shutil.rmtree(self.children[0])
        result = self.cleanup()
        self.assertEqual(result['absent'], [str(self.children[0])])
        self.assertEqual(result['removed'], [str(self.children[1])])
        self.assertEqual(result['absent_allocated_bytes'], 0)

    def test_failed_producer_is_never_cleaned(self):
        self.producer['status'] = 'failed'
        self.save()
        with self.assertRaisesRegex(ValueError, 'successful fresh'):
            self.cleanup()
        self.assert_children_kept()

    def test_remote_e2b_does_not_enter_child_cleanup(self):
        self.producer['config']['e2b']['execution'] = 'ssh'
        self.producer['e2b_environment']['execution'] = 'ssh'
        self.save()
        self.assertEqual(self.cleanup()['status'], 'not-applicable')
        self.assert_children_kept()

    def test_nonhosted_local_e2b_keeps_original_staging_cleanup_without_root_policy(self):
        local = self.run / 'payload/repos/swe-bench_owner__project-1'
        local.mkdir(parents=True)
        (local / 'file.txt').write_text('reconstructable checkout')
        source = self.base / 'reconstruction-source'
        source.mkdir()
        write_json(self.run / 'repository.json', dict(instance=self.producer['instance'],
                   commit='a' * 40, source=str(source)))
        with patch('ae.scripts.hosted_launcher.load_policy', side_effect=AssertionError('Nonhosted cleanup must not read root policy')):
            result = self.cleanup(hosted_context=False)
        self.assertEqual(result['removed'], ['payload/repos'])
        self.assertFalse(local.parent.exists())
        self.assert_children_kept()
        self.assertNotIn('e2b', json.loads((self.run / 'staging-cleanup.json').read_text()))

    def test_tampered_pilot_sha_is_rejected_before_any_removal(self):
        self.pilot_path.write_text('{"ok":true}')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.cleanup()
        self.assert_children_kept()

    def test_pilot_must_also_be_bound_in_artifacts(self):
        self.producer['artifacts'] = self.producer['artifacts'][1:]
        write_json(self.run / 'run.json', self.producer)
        with self.assertRaisesRegex(ValueError, 'SHA-bound'):
            self.cleanup()
        self.assert_children_kept()

    def test_illegal_and_protected_ids_are_rejected_before_local_staging_removal(self):
        local = self.run / 'payload/repos'
        local.mkdir(parents=True)
        for value in ('../escape', 'not-a-uuid', self.BASE, self.ANCESTOR):
            with self.subTest(value=value):
                self.pilot['root_setup']['to_build'] = value
                self.save()
                with self.assertRaises(ValueError):
                    self.cleanup()
                self.assert_children_kept()
                self.assertTrue(local.is_dir())

    def test_reused_root_and_duplicate_child_claims_are_rejected(self):
        self.pilot['root_setup']['reused'] = True
        self.save()
        with self.assertRaisesRegex(ValueError, 'reused'):
            self.cleanup()
        self.pilot['root_setup'].pop('reused')
        self.pilot['iterations'][0]['e2b_steps'][0]['to_build'] = self.CHILDREN[0]
        self.save()
        with self.assertRaisesRegex(ValueError, 'repeats'):
            self.cleanup()
        self.assert_children_kept()

    def test_fixed_configuration_and_central_storage_boundaries_are_required(self):
        changed = dict(self.fixed, e2b=dict(self.fixed['e2b'], storage='/outside/storage'))
        write_json(self.config_path, changed)
        with self.assertRaisesRegex(ValueError, 'fixed configuration'):
            self.cleanup()
        write_json(self.config_path, self.fixed)
        self.policy['runtime_root'] = str(self.base / 'different-runtime')
        write_json(self.policy_path, self.policy)
        with self.assertRaisesRegex(ValueError, 'runtime ae/work'):
            self.cleanup()
        self.assert_children_kept()

    def test_parent_input_and_parent_manifest_changes_are_rejected(self):
        original = self.parent_file.read_bytes()
        self.parent_file.write_bytes(b'changed parent')
        with self.assertRaisesRegex(ValueError, 'snapshot input changed'):
            self.cleanup()
        self.parent_file.write_bytes(original)
        self.parent_path.write_text(self.parent_path.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'parent manifest identity'):
            self.cleanup()
        self.assert_children_kept()

    def test_links_mounts_untrusted_ownership_and_world_write_are_refused(self):
        child = self.children[0]
        self.owners[child] = (1012, 1013)
        with self.assertRaisesRegex(ValueError, 'owned by'):
            self.cleanup()
        self.owners.clear()
        child.chmod(0o777)
        with self.assertRaisesRegex(ValueError, 'writable'):
            self.cleanup()
        child.chmod(0o755)
        with patch('repro.staging_cleanup.os.path.ismount', side_effect=lambda path: Path(path) == child):
            with self.assertRaisesRegex(ValueError, 'mounted'):
                self.cleanup()
        memory = child / 'memfile'
        memory.unlink()
        memory.symlink_to(self.measurement)
        with self.assertRaisesRegex(ValueError, 'Symbolic links'):
            self.cleanup()
        self.assert_children_kept()

    def test_hardlinked_child_file_is_refused(self):
        memory = self.children[0] / 'memfile'
        memory.unlink()
        os.link(self.measurement, memory)
        with self.assertRaisesRegex(ValueError, 'independent regular file'):
            self.cleanup()
        self.assert_children_kept()

    def test_unknown_child_contents_are_refused(self):
        (self.children[0] / 'unrelated.txt').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'unexpected or incomplete'):
            self.cleanup()
        self.assert_children_kept()

    def test_replaced_child_is_rejected_before_deletion(self):
        real_write = write_json
        replaced = self.base / 'original-child'
        changed = False
        def replace_after_plan(path, value):
            nonlocal changed
            real_write(path, value)
            if Path(path).name == 'staging-cleanup.json' and not changed:
                changed = True
                self.children[0].rename(replaced)
                shutil.copytree(replaced, self.children[0])
        with patch('repro.staging_cleanup.write_json', side_effect=replace_after_plan):
            with self.assertRaisesRegex(ValueError, 'changed after cleanup validation'):
                self.cleanup()
        self.assert_children_kept()
        self.assertTrue(replaced.is_dir())

    def test_parent_is_reverified_after_removal(self):
        remove = shutil.rmtree
        def tamper_after_remove(path):
            remove(path)
            self.parent_file.write_text('concurrent parent change')
        with patch('repro.staging_cleanup.shutil.rmtree', side_effect=tamper_after_remove) as patched:
            patched.avoids_symlink_attacks = True
            with self.assertRaisesRegex(ValueError, 'snapshot input changed'):
                self.cleanup()
        report = json.loads((self.run / 'staging-cleanup.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertGreater(report['removed_allocated_bytes'], 0)
        self.assertNotIn('parent_verification', report['e2b'])


if __name__ == '__main__':
    unittest.main()
