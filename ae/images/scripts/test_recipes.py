#!/usr/bin/env python3
"""Non-privileged guardrail/provenance checks; Linux fs quick-check is separate."""
import hashlib
import csv
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from build_xfs import new_image, xfs_fstab, GROUPS, DEFAULT_GROUPS

ROOT = Path(__file__).resolve().parents[1]


class RecipeChecks(unittest.TestCase):
    def test_historical_ext4_root_entry_is_replaced_for_xfs_guest(self):
        result = xfs_fstab(['# retained comment', '/dev/vda / ext4 defaults 0 1',
                            'UUID=old /mnt/data xfs defaults 0 0',
                            '/dev/vdb /mnt/swe_env xfs defaults 0 0',
                            'tmpfs /scratch tmpfs defaults 0 0'])
        self.assertNotIn('ext4', result)
        self.assertNotIn('UUID=old', result)
        self.assertNotIn('/mnt/swe_env', result)
        self.assertIn('# retained comment', result)
        self.assertIn('tmpfs /scratch tmpfs defaults 0 0', result)
        self.assertEqual(result.count('/dev/vda / xfs'), 1)
        self.assertEqual(result.count('/dev/vdb /mnt/data'), 1)
        self.assertEqual(xfs_fstab(result.splitlines()), result)

    def test_default_groups_cover_bound_deltabox_cohorts(self):
        for rel in ['table-02/cohort-deltabox.csv', 'figure-06/cohort-memory.csv',
                    'figure-06/cohort-adaptive.csv', 'figure-08/cohort-fork-primitive.csv']:
            with (ROOT.parent / 'paper' / rel).open() as f:
                rows = list(csv.DictReader(f))
            self.assertTrue(rows)
            for row in rows:
                repo = row['instance'].split('__', 1)[1].rsplit('-', 1)[0]
                matches = [g for g, (_, repos) in GROUPS.items() if repo in repos]
                self.assertEqual(len(matches), 1)
                self.assertIn(matches[0], DEFAULT_GROUPS)

    def test_existing_file_is_never_formatted(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'keep.xfs'
            target.write_bytes(b'existing-data')
            with patch('build_xfs.run') as command:
                with self.assertRaises(FileExistsError):
                    new_image(target, 512)
                command.assert_not_called()
            self.assertEqual(target.read_bytes(), b'existing-data')

    def test_existing_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'existing'
            target.write_bytes(b'keep')
            link = Path(tmp) / 'image.xfs'
            link.symlink_to(target)
            with patch('build_xfs.run') as command:
                with self.assertRaises(FileExistsError):
                    new_image(link, 512)
                command.assert_not_called()
            self.assertEqual(target.read_bytes(), b'keep')

    def test_invalid_size_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'bad.xfs'
            with self.assertRaises(ValueError):
                new_image(target, 0)
            self.assertFalse(target.exists())

    def test_recovered_sources_match_remote_hashes(self):
        for item in json.loads((ROOT / 'provenance.json').read_text()):
            if item['mode'] != 'verbatim':
                continue
            self.assertEqual(hashlib.sha256((ROOT / item['stored']).read_bytes()).hexdigest(), item['source_sha256'])

    def test_env_specs_have_one_group_and_a_commit(self):
        specs = json.loads((ROOT / 'historical/env_specs.json').read_text())
        self.assertEqual(len(specs), 80)
        for spec in specs.values():
            repo = spec['repo'].split('/')[1]
            self.assertEqual(sum(repo in x[1] for x in GROUPS.values()), 1)
            self.assertRegex(spec['env_commit'], r'^[a-f0-9]{40}$')

    def test_figure9_bindings_match_publication_inputs(self):
        rows = json.loads((ROOT / 'configs/figure09-oci-inputs.json').read_text())
        self.assertEqual(len(rows), 185)
        self.assertEqual(len({r['image'] for r in rows}), 136)
        manifest = {r['target']: r for r in map(json.loads, (ROOT.parent / 'paper/figure-09/files.jsonl').read_text().splitlines())}
        for row in rows:
            self.assertEqual(row['trace_sha256'], manifest[row['trace']]['sha256'])
            self.assertRegex(row['image'], r'^swebench/sweb\.eval\.x86_64\.')

    def test_unknown_figure9_instance_is_an_error(self):
        result = subprocess.run(['python3', str(ROOT / 'scripts/pull_figure09.py'), '--instance', 'missing'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
