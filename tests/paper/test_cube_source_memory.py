import unittest
from ae.runners import cube_fanout_audit as audit
class MemoryResponseTests(unittest.TestCase):
 def check(self,text,**kw):
  self.assertTrue(hasattr(audit,'validate_memory_response'),'Source checksum must be checked before snapshot')
  return audit.validate_memory_response(text,token='source',expected_bytes=8192,min_requests=1,**kw)
 def test_source_response_validated(self):
  self.assertEqual(self.check('OK token=source bytes=8192 checksum=1 requests=1 pid=14')['checksum'],'1')
 def test_corrupt_source_rejected(self):
  with self.assertRaisesRegex(ValueError,'checksum'):self.check('OK token=source bytes=8192 checksum=0 requests=1 pid=14')
 def test_identity_size_counter_and_missing_fields_rejected(self):
  for text in ['OK token=other bytes=8192 checksum=1 requests=1 pid=14','OK token=source bytes=4096 checksum=1 requests=1 pid=14','OK token=source bytes=8192 checksum=1 requests=0 pid=14','OK token=source']:
   with self.subTest(text=text),self.assertRaises(ValueError):self.check(text)


 def test_current_generated_source_and_child_commands_bind_expected_token(self):
  from ae.vendor.finalbench.official_sandbox_fork import bench_official_fork as driver
  token='generated-cube-child'
  self.assertEqual(audit.expected_memory_token(driver.start_mem_server_shell(mem_mib=64,token=token),child=False),token)
  self.assertEqual(audit.expected_memory_token(driver.verify_mem_server_shell(mem_mib=64,token=token),child=True),token)
 def test_child_requires_one_literal_assignment_and_never_uses_reply_or_source_assertion(self):
  for command in ["assert 'OK token=source' in out", "expected_token = danger()", "expected_token = 123", "expected_token = 'a'\nexpected_token = 'b'", "expected_token = 'a b'"]:
   with self.subTest(command=command),self.assertRaises(ValueError):
    audit.expected_memory_token(command,child=True)
 def test_source_requires_unambiguous_assertion(self):
  for command in ["", "assert 'OK token=a' in out; assert 'OK token=b' in out"]:
   with self.subTest(command=command),self.assertRaises(ValueError):
    audit.expected_memory_token(command,child=False)

 def test_bad_original_source_warmup_stops_before_clone(self):
  import importlib.util,json,tempfile,types
  from pathlib import Path
  from unittest.mock import patch
  cloned=[]
  ready=types.ModuleType('ae.runners.cube_envd_ready')
  ready.command_when_ready=lambda sb,cmd,timeout,run,record:run(sb,cmd,timeout)
  class Sandbox:
   sandbox_id='source-id'
   @classmethod
   def create(cls,*a,**k):return cls()
   @classmethod
   def delete_snapshot(cls,*a,**k):pass
   def create_snapshot(self,*a,**k):cloned.append(True)
   def kill(self):pass
  sdk=types.ModuleType('cubesandbox');sdk.Sandbox=Sandbox
  exceptions=types.ModuleType('cubesandbox._exceptions')
  exceptions.SandboxNotFoundError=type('SandboxNotFoundError',(Exception,),{})
  exceptions.TemplateNotFoundError=type('TemplateNotFoundError',(Exception,),{})
  def load(driver):
   driver._ae_interrupted=lambda *a:None
   driver.parse_forks=lambda s:[1]
   driver.cube_run_shell=lambda *a:'OK token=test bytes=1048576 checksum=0 requests=1 pid=14'
   def bench(settings,forks):
    driver.cube_run_shell(Sandbox(),"s.sendall(b'warmup\\n'); assert 'OK token=test' in out",30)
    cloned.append(True)
    return [{'success':True,'forks':1,'children':[]}]
   driver.bench_cube=bench
  spec=importlib.util.spec_from_loader('official_cube_probe_driver',loader=None)
  spec.loader=types.SimpleNamespace(exec_module=load,create_module=lambda spec:None)
  with tempfile.TemporaryDirectory() as directory:
   args=types.SimpleNamespace(self_check=False,out=Path(directory)/'fanout.json',forks='1',mem_mib=1,
       cube_api_url='http://unused',cube_template='test',cube_proxy_node_ip='127.0.0.1',
       timeout=30,request_timeout=30,exec_timeout=30)
   with patch.dict('sys.modules',{'cubesandbox':sdk,'cubesandbox._exceptions':exceptions,'ae.runners.cube_envd_ready':ready}),\
        patch.object(audit.importlib.util,'spec_from_file_location',return_value=spec),\
        patch.object(audit.signal,'signal'):
    with self.assertRaisesRegex(ValueError,'checksum'):
     audit.run_benchmark(args,types.SimpleNamespace(error=lambda e:None),{})
   self.assertEqual(cloned,[])
   report=json.loads((Path(directory)/'cube-audit.json').read_text())
   self.assertFalse(report['source_memory_checks'][0]['ok'])
   self.assertFalse(report['ok'])

 def test_real_generated_child_through_audit_wrapper_keeps_numeric_checks(self):
  import importlib.util,json,tempfile,types
  from pathlib import Path
  from unittest.mock import patch
  from ae.vendor.finalbench.official_sandbox_fork import bench_official_fork as actual
  checksum=actual.expected_memory_state(1)['checksum']
  for label,byte_count,child_checksum in [('valid',1048576,checksum),('bytes',8192,checksum),('checksum',1048576,0)]:
   with self.subTest(label=label),tempfile.TemporaryDirectory() as directory:
    ready=types.ModuleType('ae.runners.cube_envd_ready')
    ready.command_when_ready=lambda sb,cmd,timeout,run,record:run(sb,cmd,timeout)
    class Sandbox:
     def __init__(self,name):self.sandbox_id=name
     @classmethod
     def create(cls,*a,**k):return cls('source')
     @classmethod
     def delete_snapshot(cls,*a,**k):pass
     def create_snapshot(self,*a,**k):pass
     def kill(self):pass
    sdk=types.ModuleType('cubesandbox');sdk.Sandbox=Sandbox
    exceptions=types.ModuleType('cubesandbox._exceptions')
    exceptions.SandboxNotFoundError=type('SandboxNotFoundError',(Exception,),{})
    exceptions.TemplateNotFoundError=type('TemplateNotFoundError',(Exception,),{})
    def load(driver):
     driver._ae_interrupted=lambda *a:None
     driver.parse_forks=lambda s:[1]
     def reply(sb,command,timeout):
      if "s.sendall(b'warmup" in command:
       return f'OK token=test bytes=1048576 checksum={checksum} requests=1 pid=14\n'
      return f'OK token=test bytes={byte_count} checksum={child_checksum} requests=2 pid=14\n'
     driver.cube_run_shell=reply
     def bench(settings,forks):
      driver.cube_run_shell(Sandbox('source'),actual.start_mem_server_shell(mem_mib=1,token='test'),30)
      evidence=actual.verify_child_memory(driver.cube_run_shell,Sandbox('child'),token='test',mem_mib=1,timeout=30)
      return [{'success':True,'forks':1,'children':[{'sandbox_id':'child','memory_validation':evidence}]}]
     driver.bench_cube=bench
    spec=importlib.util.spec_from_loader('official_cube_probe_driver',loader=None)
    spec.loader=types.SimpleNamespace(exec_module=load,create_module=lambda spec:None)
    args=types.SimpleNamespace(self_check=False,out=Path(directory)/'fanout.json',forks='1',mem_mib=1,
        cube_api_url='http://unused',cube_template='test',cube_proxy_node_ip='127.0.0.1',
        timeout=30,request_timeout=30,exec_timeout=30)
    with patch.dict('sys.modules',{'cubesandbox':sdk,'cubesandbox._exceptions':exceptions,'ae.runners.cube_envd_ready':ready}),\
         patch.object(audit.importlib.util,'spec_from_file_location',return_value=spec),\
         patch.object(audit.signal,'signal'):
     if label=='valid':
      self.assertEqual(audit.run_benchmark(args,types.SimpleNamespace(error=lambda e:None),{}),0)
     else:
      with self.assertRaisesRegex(ValueError,'byte count' if label=='bytes' else 'checksum'):
       audit.run_benchmark(args,types.SimpleNamespace(error=lambda e:None),{})
    report=json.loads((Path(directory)/'cube-audit.json').read_text())
    self.assertEqual(report['ok'],label=='valid')
    self.assertEqual(len(report['memory_checks']),1 if label=='valid' else 0)
