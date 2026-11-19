"""Clone only the independent V2 environment; copy exact reviewed SDK files.
No SDK import, CAN socket, enable, mode change, model or motion operation.
"""
from pathlib import Path
import hashlib,importlib.metadata as metadata,json,os,shutil,socket,subprocess,sys
ROOT=Path('/media/robot/RECORDING_USB/2026-10-23-13-12-FP32-V2-APPLY40')
BASE=ROOT.parent/'2026-10-23-12-12-FP32-V2-NOSEND'
assert socket.gethostname()=='agliex-NUC14RVK-B'
m=json.loads(subprocess.check_output(['findmnt','-J','-T',str(ROOT.parent),'-o','TARGET,UUID,OPTIONS,MAJ:MIN'],text=True))['filesystems'][0]
assert m['target']==str(ROOT.parent) and m['uuid']=='6745-2301' and m['maj:min']=='8:17' and 'rw' in m['options'].split(',')
ROOT.mkdir(exist_ok=False)
for name in ('src','vendor','scripts','tests','pc','receipts','runs','build','deps','tmp'):(ROOT/name).mkdir()
shutil.copytree(BASE/'environment',ROOT/'environment',symlinks=False)
shutil.copytree(BASE/'deps/acados',ROOT/'deps/acados',symlinks=False)
source=Path('/home/agliex/miniconda3/envs/tiger-lerobot-piper-20261005/lib/python3.12/site-packages')
dest=ROOT/'environment/lib/python3.12/site-packages'
copied={}
for pattern in ('piper_sdk','piper_sdk-*.dist-info','can','python_can-*.dist-info',
                'typing_extensions.py','typing_extensions-*.dist-info','serial','pyserial-*.dist-info'):
    matches=list(source.glob(pattern));assert len(matches)==1,(pattern,matches)
    p=matches[0];target=dest/p.name
    if target.exists():raise RuntimeError('Refuse to overwrite private dependency: '+str(target))
    if p.is_dir():shutil.copytree(p,target,symlinks=False)
    else:shutil.copy2(p,target)
    for f in ([p] if p.is_file() else p.rglob('*')):
        if f.is_file():
            relative=f.relative_to(source);a=hashlib.sha256(f.read_bytes()).hexdigest()
            assert hashlib.sha256((dest/relative).read_bytes()).hexdigest()==a
            copied[str(relative)]=a
(ROOT/'receipts/environment-source-copy.json').write_text(json.dumps({'source':str(BASE),
    'SDK_source':str(source),'copied_dependency_hashes':copied,'motion_started':False,
    'model_calls':0,'new_environment':str(ROOT/'environment')},indent=2))
print(json.dumps({'prepared':str(ROOT),'SDK_copied_files':len(copied),'robot_commands':0}),flush=True)
