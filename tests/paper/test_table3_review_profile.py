"""The Table3 slow plan must reach the existing lazy runtime protocol.
These tests build commands only; they never launch a VM or benchmark.
"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from ae.repro import catalog
from ae.repro.common import load_config, configured_path
from ae.scripts.run_review import deep_merge
from common.runtime_profile import checkpoint_environment

ROOT=Path(__file__).resolve().parents[2]
SHA='61b978b4c7c03577e9a122e5d23845950304f84abec93ef95ef54a917d494940'
class Table3ReviewProfile(unittest.TestCase):
 def setUp(self):
  self.config=load_config(ROOT/'ae/configs/spr4numa-review.json')
 def effective(self,name):
  return deep_merge(self.config,self.config.get('review',{}).get('experiment_overrides',{}).get(name,{}))
 def test_public_slow_plan_reaches_lazy_incremental_with_same_pinned_criu(self):
  cfg=self.effective('table-03-slow')
  cohort=[dict(instance='django__django-'+str(14000+i),local='paper/fixture/trajectory.json') for i in range(12)]
  with patch.object(catalog,'cohort',return_value=cohort),patch.object(catalog,'replay_wait_budget',return_value={'recorded_wait_s':0}):
   jobs=catalog.build_jobs(['table-03-slow'],cfg,Path('/config.json'),Path('/result'))
  self.assertEqual(len(jobs),12)
  for job in jobs:
   command=job['command'];profile=command[command.index('--checkpoint-profile')+1];mode=command[command.index('--mode')+1]
   env=checkpoint_environment(profile,mode)
   self.assertEqual(mode,'slow');self.assertEqual(env['DELTABOX_CRIU_LAZY_RESTORE'],'1')
   self.assertEqual(env['DELTABOX_ASYNC_INCREMENTAL_DUMP'],'1');self.assertEqual(env['DELTABOX_ASYNC_TEMPLATE_FULL_DUMP'],'0')
   binary=command[command.index('--criu-dump-binary')+1]
   self.assertEqual(Path(binary).resolve(),ROOT/'ae/work/runtime-deps/criu'/SHA/'criu')
   self.assertEqual(configured_path(cfg,'criu_bin'),configured_path(cfg,'criu_dump_binary'))
 def test_slow_override_does_not_change_fast_or_criu_baseline(self):
  original=copy.deepcopy(self.config)
  slow=self.effective('table-03-slow');fast=self.effective('table-02-deltabox');criu=self.effective('table-02-criu')
  self.assertEqual(self.config,original)
  self.assertEqual(fast['checkpoint_profile'],'async-incremental')
  self.assertEqual(checkpoint_environment(fast['checkpoint_profile'],'fast')['DELTABOX_CRIU_LAZY_RESTORE'],'0')
  self.assertEqual(criu['criu_bin'],self.config['criu_bin'])
  self.assertNotEqual(slow['criu_dump_binary'],fast['criu_dump_binary'])
 def test_slow_keeps_existing_resources_and_timing_settings(self):
  slow=self.effective('table-03-slow')
  for field in ['vcpus','mem_mib','prewarm_policy','timeout']:
   self.assertEqual(slow[field],self.config[field])
  self.assertEqual(slow['checkpoint_profile'],'async-incremental-lazy')
  self.assertEqual(slow['measurement'],self.config['measurement'])
  self.assertNotIn('measurement',self.config['review']['experiment_overrides']['table-03-slow'])

 def test_every_default_experiment_inherits_the_selected_runtime_placement(self):
  self.config['measurement']={'pin':True,'numa_node':5,'cpus':'100-103'}
  for name in self.config['review']['experiment_overrides']:
   with self.subTest(experiment=name):
    self.assertEqual(self.effective(name)['measurement'],self.config['measurement'])

class RuntimePlacement(unittest.TestCase):
 def test_missing_pinned_placement_has_no_hardcoded_node_or_cpu_fallback(self):
  from ae.scripts import run_review as review
  with patch.dict('os.environ',{},clear=True):
   with self.assertRaisesRegex(ValueError,'measurement.numa_node'):
    review.measurement_placement(review.parser().parse_args([]),{})
 def test_explicit_placement_and_unpinned_configs_are_supported(self):
  from ae.scripts import run_review as review
  with patch.dict('os.environ',{},clear=True):
   args=review.parser().parse_args(['--numa-node','5','--cpus','100-103'])
   self.assertEqual(review.measurement_placement(args,{}),dict(node=5,cpus='100-103'))
   args=review.parser().parse_args(['--no-pin'])
   self.assertEqual(review.measurement_placement(args,{}),dict(node=None,cpus=None))

if __name__=='__main__':unittest.main()
