"""Bounded validation retains complete inputs and permits only isolated VM jobs."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ae.scripts import run_review as review
from ae.scripts import hosted_launcher as hosted
from ae.repro import catalog

class ValidationLimitTests(unittest.TestCase):
    def test_configured_default_is_small_and_not_latest_full_output(self):
        args=review.parser().parse_args([])
        review.apply_validation_defaults(args,{'review':{'validation_max_jobs':10}})
        self.assertEqual(args.limit,10)
        self.assertFalse(review.complete_selection(args))
        self.assertEqual(review.default_output(args).parent.name,'selected')
    def test_explicit_oversize_is_rejected_and_existing_custom_config_is_unchanged(self):
        args=review.parser().parse_args(['--limit','44'])
        review.apply_validation_defaults(args,{})
        self.assertEqual(args.limit,44)
        with self.assertRaisesRegex(ValueError,'cap'):
            review.apply_validation_defaults(args,{'review':{'validation_max_jobs':10}})
    def test_limit_is_not_an_event_prefix_and_all_only_selects_groups(self):
        args=review.parser().parse_args(['--all'])
        review.apply_validation_defaults(args,{'review':{'validation_max_jobs':10}})
        self.assertEqual(args.limit,10)
        self.assertIsNone(args.max_events)
    def test_adaptive_and_filesystem_arms_fit_the_total_job_budget(self):
        flags=review.bounded_plan_limits('figure-06-adaptive',{},['--limit','10'],10)
        self.assertEqual(flags,['--limit','5'])
        self.assertEqual(review.bounded_plan_limits('figure-09',{},['--limit','10'],10),['--limit','3'])
        config=dict(images_dir='/images',kernel='/kernel',base_xfs='/base',vcpus=4,mem_mib=8192,timeout=600,checkpoint_profile='async-incremental')
        cohort=[dict(instance='django__django-'+str(14000+i),local='paper/fixture/trajectory.json') for i in range(12)]
        with patch.object(catalog,'cohort',return_value=cohort),patch.object(catalog,'replay_wait_budget',return_value={'recorded_wait_s':0,'legacy_timing_policy':'paper-zero'}):
            jobs=catalog.build_jobs(['figure-06-adaptive'],config,Path('/config'),Path('/out'),limit=5)
        self.assertEqual(len(jobs),10)
        self.assertEqual(sum('--adaptive' in j['command'] for j in jobs),5)
        self.assertTrue(all('--max-events' not in j['command'] for j in jobs))
    def test_arm_selection_can_expand_without_hiding_real_configuration_changes(self):
        first={'checkpoint_profile':'async-incremental','mem_mib':16384,'figure06_adaptive_arms':['adaptive']}
        expanded={**first,'figure06_adaptive_arms':['standard','adaptive']}
        self.assertEqual(review.config_identity(first),review.config_identity(expanded))
        self.assertNotEqual(review.config_identity(first),review.config_identity({**expanded,'mem_mib':8192}))
        self.assertEqual(review.bounded_plan_limits('figure-06-adaptive',first,['--limit','10'],10),['--limit','10'])

    def test_gpu_only_group_does_not_receive_a_cpu_input_limit(self):
        args=review.parser().parse_args(['--group','gpu'])
        review.apply_validation_defaults(args,{'review':{'validation_max_jobs':10}})
        self.assertIsNone(args.limit)
    def test_isolated_validation_cannot_omit_the_total_job_cap(self):
        args=review.parser().parse_args(['--isolated-validation','--experiment','figure-06-adaptive','--limit','10','--numa-node','0','--cpus','0-3','--output','/unused'])
        with self.assertRaises(ValueError):review.isolated_validation_output(args,{})

    def test_public_adaptive_profile_keeps_the_validated_guest_memory(self):
        config=json.loads((Path(review.__file__).resolve().parents[1]/'configs/spr4numa-review.json').read_text())
        effective=review.deep_merge(config,config['review']['experiment_overrides']['figure-06-adaptive'])
        self.assertEqual(effective['mem_mib'],16384)
        self.assertEqual(effective['checkpoint_profile'],'async-incremental')

    def test_pilot_stays_one_input_in_each_arm(self):
        self.assertEqual(review.bounded_plan_limits('figure-06-adaptive',{},['--limit','1'],10),['--limit','1'])
    def test_hosted_default_uses_selected_output_when_configured_small(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'config.json';config.write_text(json.dumps({'review':{'validation_max_jobs':10}}))
            args=hosted.parse_arguments(['--checkout','/repo'])
            self.assertEqual(hosted.default_result({'config':config},args).parent,Path('selected'))
    def test_isolated_scope_is_explicit_and_cannot_move_shared_baseline_services(self):
        flags=['--checkout','/repo','--isolated-validation','--experiment','figure-06-adaptive','--limit','1','--numa-node','0','--cpus','0-3','--output','selected/pilot']
        args=hosted.parse_arguments(flags)
        command=hosted.command_line(dict(python=Path('/python'),runtime_root=Path('/repo'),config=Path('/config')),args,Path('/result'))
        self.assertIn('--isolated-validation',command)
        for name in ['table-02-cube','table-02-e2b','table-02-criu','figure-08-gpu']:
            other=flags.copy();other[other.index('figure-06-adaptive')]=name
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):hosted.parse_arguments(other)
    def test_isolated_scope_uses_shared_rotation_barrier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'ae/results/pilot';out.parent.mkdir(parents=True)
            flags=['--isolated-validation','--experiment','figure-06-adaptive','--limit','1','--numa-node','0','--cpus','0-3','--output',str(out)]
            with patch.object(review,'REPO',root),patch.object(review,'load_config',return_value={'review':{'validation_max_jobs':10}}),patch.object(review,'run_selected',return_value=0) as run,patch.object(review,'run_lock',side_effect=lambda *a,**k:contextlib.nullcontext()) as locks:
                self.assertEqual(review.main(flags),0)
                self.assertEqual(locks.call_args_list[0].kwargs,{'shared':True})
                self.assertEqual(locks.call_count,1)
                run.assert_called_once()

if __name__=='__main__':unittest.main()
