"""Independent CPU TimeQP environment; does not load or alter a policy."""
from pathlib import Path
import json,os,socket,subprocess,time
ROOT=Path('/opt/cobot-workbench/lab-packages/2026-10-23-12-12-FP32-V2-NOSEND')
BASE='/home/robot/deployment-tools/python311-uv-2026-10-16/python/cpython-3.11.17-linux-x86_64-gnu/bin/python3.11'
assert socket.gethostname()=='user-MS-7E06'
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()=='ffaf2833-bb6b-4680-ab7f-22e2d6d91c14'
assert ROOT.parent.is_dir()
ROOT.mkdir(exist_ok=False)
for folder in ('environment','receipts','tmp','src','scripts','vendor'):(ROOT/folder).mkdir()
env=dict(os.environ,TMPDIR=str(ROOT/'tmp'),PYTHONDONTWRITEBYTECODE='1',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
def run(name,cmd):
    began=time.monotonic()
    with (ROOT/'receipts'/(name+'.log')).open('w') as log:
        result=subprocess.run(cmd,env=env,stdout=log,stderr=subprocess.STDOUT)
    print(json.dumps({'phase':name,'returncode':result.returncode,'seconds':time.monotonic()-began}),flush=True)
    if result.returncode:raise RuntimeError(name+' failed')
python=str(ROOT/'environment/bin/python')
run('01-private-venv',[BASE,'-m','venv','--copies','--without-pip',str(ROOT/'environment')])
run('02-private-pip',[python,'-m','ensurepip'])
run('03-private-timeqp-dependencies',[python,'-m','pip','install','--no-cache-dir','numpy==1.26.4','scipy==1.14.1','osqp==1.0.4'])
run('04-private-freeze',[python,'-m','pip','freeze'])
(ROOT/'receipts/environment-ready.json').write_text(json.dumps({'ready':True,'python':python,'policy_loaded':False,'GPU_calls':0},indent=2))
print(json.dumps({'ready':True,'root':str(ROOT)}),flush=True)
