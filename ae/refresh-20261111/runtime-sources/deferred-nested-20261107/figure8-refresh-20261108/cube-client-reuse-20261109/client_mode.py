"""Process-local client-context reuse, with spans; never edits installed SDK."""
from pathlib import Path
import time,threading,ssl,json,hashlib,importlib.metadata
import httpx
import cubesandbox.sandbox as sandbox_mod
import cubesandbox._commands as commands_mod
from cubesandbox._transport import IPOverrideTransport
MODE='original';_context=None;_lock=threading.Lock();events=[];contexts=[]
original_build=sandbox_mod.build_client
original_run=commands_mod.Commands.run

def set_mode(mode):
 global MODE
 assert mode in ('original','reuse');MODE=mode

def _get_context():
 global _context
 with _lock:
  if _context is None:
   t=time.perf_counter();epoch=time.time();_context=httpx._config.create_ssl_context(verify=True,trust_env=True)
   assert _context.verify_mode==ssl.CERT_REQUIRED and _context.check_hostname
   contexts.append({'epoch':epoch,'ms':(time.perf_counter()-t)*1000,'verify_mode':int(_context.verify_mode),'check_hostname':_context.check_hostname,'cert_store_stats':_context.cert_store_stats(),'verify_flags':int(_context.verify_flags)})
  return _context

def measured_build(config):
 t=time.perf_counter();epoch=time.time();mode=MODE
 try:
  if mode=='original':return original_build(config)
  context=_get_context()
  assert config.proxy_node_ip,'This experiment requires the original explicit HTTP proxy path'
  transport=IPOverrideTransport(config.proxy_node_ip,config.proxy_port,ssl_context=context)
  return httpx.Client(transport=transport,timeout=httpx.Timeout(connect=config.request_timeout,read=None,write=30,pool=30),follow_redirects=True)
 finally:events.append({'type':'build_client','mode':mode,'thread':threading.get_ident(),'started_epoch':epoch,'finished_epoch':time.time(),'ms':(time.perf_counter()-t)*1000})

def measured_run(self,*args,**kwargs):
 t=time.perf_counter();epoch=time.time();mode=MODE;ok=False
 try:
  result=original_run(self,*args,**kwargs);ok=True;return result
 finally:events.append({'type':'command','mode':mode,'thread':threading.get_ident(),'sandbox_id':self._sandbox.sandbox_id,'started_epoch':epoch,'finished_epoch':time.time(),'ms':(time.perf_counter()-t)*1000,'ok':ok})

def install():
 assert sandbox_mod.build_client is original_build and commands_mod.Commands.run is original_run
 sandbox_mod.build_client=measured_build;commands_mod.Commands.run=measured_run

def fact():
 sdk=Path('/mnt/disk2/dyp/cube-l1/sdk-runtime')
 return {'mode':MODE,'implementation':'process-local function wrapper; existing SDK files unmodified','shared_contexts_created':len(contexts),'contexts':contexts,'python_dependencies':{n:importlib.metadata.version(n) for n in ['httpx','httpcore','requests']},'sdk_files':{n:hashlib.sha256((sdk/n).read_bytes()).hexdigest() for n in ['cubesandbox/_commands.py','cubesandbox/_transport.py','cubesandbox/sandbox.py','pyproject.toml']}}
