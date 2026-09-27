"""Cube reconstruction entry contracts without starting services."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ae.scripts import hosted_launcher as hosted, run_review as review
from ae.scripts import cube_paper_profile as profile
from ae.scripts.run_pinned_measurement import frequency_cpu_selection

FLAGS = ['--experiment', 'table-02-cube', '--cube-profile', 'paper-disk']
SOURCE = dict(source_commit='a'*40, source_sha256='b'*64)

class CubePaperEntryTests(unittest.TestCase):
    def test_daemon_frequency_scope_cannot_escape_node_or_available_cpus(self):
        command=set(range(48,52));node=set(range(48,72));available=set(range(96))
        self.assertEqual(frequency_cpu_selection(command,None,node,available),command)
        self.assertEqual(frequency_cpu_selection(command,'48-71',node,available),node)
        for value,allowed in [('48-71,144-167',available),('49-71',available),('48-71',command)]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                frequency_cpu_selection(command,value,node,allowed)

    def test_hosted_forwarding_and_selected_destination(self):
        args = hosted.parse_arguments(['--checkout', '/repo', *FLAGS])
        policy = dict(python=Path('/python'), runtime_root=Path('/repo'), config=Path('/fixed.json'))
        command = hosted.command_line(policy, args, Path('/out'))
        self.assertEqual(command[command.index('--cube-profile')+1], 'paper-disk')
        self.assertNotIn('--numa-node', command)
        self.assertEqual(hosted.default_result(policy, args).parent, Path('selected'))

    def test_rejects_mixed_or_truncated_hosted_entry(self):
        variants = [[], ['--group','cube'], ['--experiment','table-02-e2b'],
                    ['--test'], ['--list'], ['--all'], ['--limit','1'],
                    ['--max-events','3'], ['--numa-node','2','--cpus','48-51'],
                    ['--reuse-completed-from','/old'], ['--resume','/old']]
        for flags in variants:
            command = ['--checkout','/repo','--cube-profile','paper-disk']
            if flags: command += ['--experiment','table-02-cube',*flags]
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                hosted.parse_arguments(command)

    def test_direct_entry_rejects_profile_escape_before_loading_config(self):
        for flags in [['--no-pin'], ['--available'], ['--analyze-existing','/unread'],
                      ['--experiment-config','table-02-cube=/unread'], ['--probe-plan','/unread']]:
            with self.subTest(flags=flags), patch.object(review,'load_config',side_effect=AssertionError('must reject first')), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                review.main([*FLAGS,*flags])

    def test_environment_cannot_override_controller(self):
        for key,value in [('AE_CPUS','52-55'),('AE_NUMA_NODE','1')]:
            with patch.dict(os.environ,{key:value}), self.assertRaisesRegex(ValueError,'cannot be overridden'):
                profile.validate(review.parser().parse_args(FLAGS))

    def test_effective_config_isolated_and_keeps_template_and_scopes(self):
        old=dict(baseline_storage='tmpfs', cube=dict(manage_memory_service=True, memory_manifest='/old'),
                 measurement=dict(pin=True,numa_node=1,cpus='28-31'))
        before=json.dumps(old,sort_keys=True)
        fresh=profile.effective(old,'paper-disk')
        self.assertEqual(json.dumps(old,sort_keys=True),before)
        self.assertEqual(fresh['baseline_storage'],'disk')
        self.assertFalse(fresh['cube']['manage_memory_service'])
        self.assertNotIn('memory_manifest',fresh['cube'])
        self.assertEqual(fresh['measurement']['cpus'],'48-51')
        self.assertEqual(fresh['cube']['service_cpus'],'48-71')
        self.assertEqual(fresh['measurement']['policy_cpus'],fresh['cube']['service_cpus'])
        self.assertEqual(profile.effective(old,None),old)

    def test_profile_rejects_resume_before_reading_old_results(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            review.main([*FLAGS,'--resume','/unread'])

    def test_reconstruction_excludes_quick_but_default_does_not(self):
        from ae.repro.result_storage import run_lock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with run_lock(root/'.quick-run.lock'):
                with self.assertRaises(ValueError):
                    with profile.preparation_lease(root,'paper-disk'): self.fail('must not start')
                with profile.preparation_lease(root,None): pass
            with profile.preparation_lease(root,'paper-disk'):
                with self.assertRaises(ValueError):
                    with run_lock(root/'.quick-run.lock'): self.fail('quick must wait')
            with run_lock(root/'.quick-run.lock'): pass

    def test_disk_manifest_pid_is_not_resume_measurement_identity(self):
        a=profile.effective({},'paper-disk');b=profile.effective({},'paper-disk')
        a['cube']['disk_manifest']='/old/pid-proof';b['cube']['disk_manifest']='/new/pid-proof'
        self.assertEqual(review.config_identity(a),review.config_identity(b))
        b['cube']['service_cpus']='48-51'
        self.assertNotEqual(review.config_identity(a),review.config_identity(b))

if __name__ == '__main__': unittest.main()
