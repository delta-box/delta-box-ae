import os,sys,json
from pathlib import Path
P=Path(__file__).resolve().parent
H=Path('/mnt/disk2/dyp/ae-hosted-20260922/e2b')
name=sys.argv[1];assert name in ['api','orchestrator']
cfg=json.loads((H/'config/services.json').read_text())[name]
env=os.environ.copy();env.update(cfg['env']);os.chdir(H)
if name=='orchestrator':env['ENVD_TIMEOUT']='60s'
args=list(cfg['args']);args[0]=str(P/'bin'/name)
os.execvpe(args[0],args,env)
