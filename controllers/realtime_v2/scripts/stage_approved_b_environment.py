"""Copy a verified private CPU environment onto the current verified robot USB.

No device/model imports, installs, or production writes. New directory only.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT.parent / '2026-10-24-15-35-V2-LONGWINDOW-WEB'
assert ROOT.name == '2026-10-24-16-00-V2-STORAGE-WEB'
assert socket.gethostname() == 'agliex-NUC14RVK-B'
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip() == 'bfb34bd1-6604-4add-badf-92bd0e646a08'
mount = json.loads(subprocess.check_output(
    ['findmnt', '-J', '-T', str(ROOT), '-o', 'TARGET,UUID,OPTIONS,MAJ:MIN'], text=True))['filesystems'][0]
assert mount['target'] == str(ROOT.parent) and mount['uuid'] == '6745-2301'
assert mount['maj:min'] == '8:1' and 'rw' in mount['options'].split(',')
assert shutil.disk_usage(ROOT).free > 4 * 1024**3
assert not (ROOT/'environment').exists()
started = time.monotonic()
for name in ('environment', 'deps/acados', 'build/mpc'):
    source, target = BASE/name, ROOT/name
    assert source.is_dir() and not target.exists()
    shutil.copytree(source, target, symlinks=False)
report = {'status': 'INDEPENDENT_CPU_ENVIRONMENT_COPIED', 'base': str(BASE), 'root': str(ROOT),
          'mount': mount, 'elapsed_s': time.monotonic()-started,
          'source_environment_modified': False, 'production_environment_modified': False,
          'model_calls': 0, 'hardware_calls': 0}
(ROOT/'receipts/environment-source-copy.json').write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps(report), flush=True)
