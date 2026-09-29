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
