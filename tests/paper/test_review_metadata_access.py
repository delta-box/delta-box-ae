"""Review metadata and readable config evidence must reflect the selected scope."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ae.scripts import run_review as review


class ReviewMetadataAccessTests(unittest.TestCase):
    def runner(self, directory, flags=(), config=None):
        root = Path(directory)
        source = root / 'config.json'
        source.write_text(json.dumps(config or {'measurement': {'pin': False},
                                                'review': {'validation_max_jobs': 10}}))
        output = root / 'output'
        output.mkdir()
        args = review.parser().parse_args(['--config', str(source), *flags])
        with patch.object(review, 'current_source', return_value={}):
            return review.Review(args, review.load_config(source), output)

    def test_bounded_cpu_runs_are_not_quick_checks(self):
        for flags, purpose, limit, events in [
            (['--group', 'cpu', '--cpu-parallel', '--limit', '3'], 'ae-cohorts', 3, None),
            (['--group', 'cpu'], 'ae-cohorts', 10, None),
            (['--available', '--limit', '3'], 'available-cohorts', 3, None),
            (['--limit', '3', '--max-events', '7'], 'ae-cohorts', 3, 7),
        ]:
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as tmp:
                runner = self.runner(tmp, flags)
                runner.save()
                record = json.loads((runner.output / 'review.json').read_text())
                self.assertEqual(record['run_purpose'], purpose)
                self.assertEqual((record['input_limit'], record['event_limit']), (limit, events))
                self.assertEqual(record['experiments'], runner.experiments)

    def test_only_explicit_quick_check_uses_quick_check_label(self):
        for flag in ('--test', '--smoke'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                runner = self.runner(tmp, [flag])
                self.assertEqual(runner.record['run_purpose'], 'quick-check')
                self.assertEqual(runner.experiments, ['table-02-deltabox'])
                self.assertEqual((runner.record['input_limit'], runner.record['event_limit']), (1, 3))
                self.assertEqual(runner.limits, ['--limit', '1', '--max-events', '3'])

    def test_configs_are_readable_without_exposing_execution_credentials(self):
        for secrets in (False, True):
            with self.subTest(secrets=secrets), tempfile.TemporaryDirectory() as tmp:
                config = {'measurement': {'pin': False}, 'timeout': 10}
                if secrets:
                    config['credentials'] = {'api_key': 'fixture-api-key', 'nested': [{'password': 'fixture-password'}]}
                runner = self.runner(tmp, ['--experiment', 'table-02-deltabox', '--limit', '3'], config)
                captured = []
                def doctor(name, command, *args, **kwargs):
                    path = Path(command[command.index('--config') + 1])
                    captured.append((path, json.loads(path.read_text())))
                    raise RuntimeError('stop-before-doctor-execution')
                with patch.object(runner, 'step', side_effect=doctor):
                    with self.assertRaisesRegex(RuntimeError, 'stop-before-doctor-execution'):
                        runner.run_experiment('table-02-deltabox')
                row = runner.record['coverage'][0]
                raw = Path(row['config']); public = Path(row['public_config']['path'])
                self.assertEqual(captured[0][0], raw)
                self.assertEqual(row['effective_config'], review.file_record(raw))
                self.assertEqual(row['public_config'], review.file_record(public))
                self.assertEqual(json.loads(public.read_text()), review.public_config(captured[0][1]))
                self.assertEqual(public.stat().st_mode & 0o777, 0o644)
                if secrets:
                    self.assertEqual(raw.stat().st_mode & 0o777, 0o600)
                    self.assertNotEqual(raw, public)
                    self.assertEqual(captured[0][1]['credentials']['api_key'], 'fixture-api-key')
                    self.assertNotIn('fixture-api-key', public.read_text())
                    self.assertNotIn('fixture-password', public.read_text())
                else:
                    self.assertEqual(raw, public)
                with patch.dict(os.environ, {'AE_HOSTED_CALLER_UID': '7001'}):
                    review.make_output_accessible(runner.output)
                self.assertEqual(public.stat().st_mode & 0o777, 0o644)
                if secrets:
                    self.assertEqual(raw.stat().st_mode & 0o777, 0o600)

    def test_failed_serialization_keeps_previous_file_and_no_temporary_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'effective.json'
            path.write_text('previous evidence')
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                review.write_effective_config(path, {'value': float('nan')})
            self.assertEqual(path.read_text(), 'previous evidence')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(tmp).iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
