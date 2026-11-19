"""One new USB-scoped environment. No hardware/model imports or commands."""
from pathlib import Path
import hashlib,json,os,socket,subprocess,sys,time

ROOT=Path('/media/robot/RECORDING_USB/2026-10-23-12-12-FP32-V2-NOSEND')
MOUNT=ROOT.parent
BASE='/home/agliex/miniconda3/envs/tiger-lerobot-piper-20261005/bin/python'
PRODUCTION=MOUNT/'2026-10-06-14-05-AGILEX-UNIFIED-CONTROL'

def check():
    assert socket.gethostname()=='agliex-NUC14RVK-B'
    assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()=='13b1d802-3319-40e6-8062-19cf0190d6f4'
    mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(MOUNT),'-o','TARGET,SOURCE,FSTYPE,UUID,OPTIONS,MAJ:MIN'],text=True))['filesystems'][0]
    assert mount['target']==str(MOUNT) and mount['uuid']=='6745-2301' and 'rw' in mount['options'].split(',')
    assert mount['maj:min']=='8:17' and mount['fstype']=='exfat'
    return mount

mount=check()
ROOT.mkdir(exist_ok=False)
for name in ('receipts','tmp','environment','src','vendor','scripts','tests','pc','runs','build'):(ROOT/name).mkdir()
(ROOT/'environment/lib64').mkdir()
env=dict(os.environ,TMPDIR=str(ROOT/'tmp'),PIP_CACHE_DIR=str(ROOT/'tmp/pip-cache'),PYTHONDONTWRITEBYTECODE='1')
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
protected={str(p):sha(p) for p in sorted((PRODUCTION/'agilex_control').rglob('*.py'))}
config=PRODUCTION/'2026-10-06-14-05-config.json';protected[str(config)]=sha(config)
(ROOT/'receipts/production-before.json').write_text(json.dumps({'mount':mount,'files':protected},indent=2))

def run(name,args):
    check();began=time.monotonic()
    with (ROOT/'receipts'/(name+'.log')).open('w') as log:
        result=subprocess.run(args,env=env,stdout=log,stderr=subprocess.STDOUT)
    print(json.dumps({'phase':name,'returncode':result.returncode,'seconds':time.monotonic()-began}),flush=True)
    if result.returncode:raise RuntimeError(name+' failed; original log retained')

python=str(ROOT/'environment/bin/python')
run('01-private-venv',[BASE,'-m','venv','--copies','--without-pip',str(ROOT/'environment')])
run('02-private-pip',[python,'-m','ensurepip'])
run('03-private-dependencies',[python,'-m','pip','install','--no-cache-dir','numpy==1.26.4','scipy==1.14.1',
    'osqp==1.0.4','casadi==3.7.2','jinja2==3.1.6','matplotlib==3.9.2','Cython','Deprecated'])
for path,h in protected.items():assert sha(Path(path))==h, 'Production changed during environment setup: '+path
run('04-private-freeze',[python,'-m','pip','freeze'])
check()
(ROOT/'receipts/environment-ready.json').write_text(json.dumps({'python':python,'original_sources_unchanged':True,
    'production_files':len(protected),'model_calls':0,'hardware_calls':0,'robot_commands':0},indent=2))
print(json.dumps({'ready':True,'root':str(ROOT),'python':python}),flush=True)
