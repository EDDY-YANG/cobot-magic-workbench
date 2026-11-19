"""Copy the verified private V2 CPU environment; no package or hardware calls."""
from pathlib import Path
import json,subprocess,shutil,hashlib,socket
ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT.parent/'2026-10-23-13-12-FP32-V2-APPLY40'
assert ROOT.name=='2026-10-23-14-50-V2-STARTUP-FIX'
assert socket.gethostname()=='agliex-NUC14RVK-B'
m=json.loads(subprocess.check_output(['findmnt','-J','-T',str(ROOT),'-o','TARGET,UUID,OPTIONS,MAJ:MIN'],text=True))['filesystems'][0]
assert m['target']==str(ROOT.parent) and m['uuid']=='6745-2301' and m['maj:min']=='8:17' and 'rw' in m['options'].split(',')
for name in ('environment','deps/acados','build/mpc'):
    target=ROOT/name
    assert not target.exists(),str(target)
    shutil.copytree(BASE/name,target,symlinks=False)
report={'scope':'COPY_VERIFIED_PRIVATE_CPU_RUNTIME_ONLY','base':str(BASE),'target':str(ROOT),
    'base_environment_receipt_sha256':hashlib.sha256((BASE/'receipts/environment-source-copy.json').read_bytes()).hexdigest(),
    'source_environment_modified':False,'production_environment_modified':False,'model_calls':0,'hardware_calls':0}
(ROOT/'receipts/environment-source-copy.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
