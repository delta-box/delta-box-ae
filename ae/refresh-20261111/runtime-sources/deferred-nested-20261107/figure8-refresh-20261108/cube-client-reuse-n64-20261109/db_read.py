import pathlib,re,subprocess,json,sys
def query(sql):
    text=pathlib.Path('/usr/local/services/cubetoolbox/CubeMaster/conf.yaml').read_text()
    block=text.split('ossdb_config:')[1].split('\n\n')[0]
    val=lambda k:re.search(r'^\s*'+k+r':\s*"?([^"\n]+)',block,re.M).group(1)
    defaults='[client]\nuser='+val('user')+'\npassword='+val('pwd')+'\n'
    r=subprocess.run(['docker','exec','-i','cube-sandbox-mysql','mysql','--defaults-extra-file=/dev/stdin','--batch','--raw','--database='+val('db_name'),'-e',sql],input=defaults,text=True,capture_output=True)
    if r.returncode:raise RuntimeError(r.stderr)
    return r.stdout
if __name__=='__main__':print(query(sys.argv[1]))
