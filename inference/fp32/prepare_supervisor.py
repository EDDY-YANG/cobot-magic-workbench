"""Explicit root-owner setup only. Creates isolated metadata paths, no GPU/run."""
from pathlib import Path
import hashlib
import json
import socket
import subprocess

OLD = Path('/opt/cobot-workbench/lab-packages/2026-10-19-15-38-Pi05-FP32-Deployment')
NEW = Path('/opt/cobot-workbench/lab-packages/2026-10-19-18-12-H50-Model-Supervisor')


def main():
    if socket.gethostname() != 'user-MS-7E06':
        raise RuntimeError('Wrong host')
    mount = json.loads(subprocess.check_output(['findmnt', '-J', '-T', str(OLD), '-o', 'TARGET,UUID'], text=True))['filesystems'][0]
    if mount != {'target': '/data/4090-1tb', 'uuid': 'e76043b6-4ab6-42a5-8b06-4963ee92a567'}:
        raise RuntimeError('Wrong data filesystem')
    for name in ('scripts', 'receipts', 'outputs', 'cache'):
        (NEW / name).mkdir(parents=True, exist_ok=True)
    source = NEW / 'source'
    if source.exists() or source.is_symlink():
        if source.resolve(strict=True) != (OLD / 'source').resolve(strict=True):
            raise RuntimeError('Existing source pointer differs; preserve it')
    else:
        source.symlink_to(OLD / 'source', target_is_directory=True)
    name = '2026-10-19-15-38-setup.json'
    original = (OLD / 'receipts' / name).read_bytes()
    target = NEW / 'receipts' / name
    if target.exists() and target.read_bytes() != original:
        raise RuntimeError('Existing historical input setup differs; preserve it')
    if not target.exists():
        target.write_bytes(original)
    print(json.dumps({'status': 'PREPARED_NO_GPU_STARTED', 'job': str(NEW), 'source': str(source.resolve()),
                      'historical_setup_sha256': hashlib.sha256(original).hexdigest(), 'robot_commands': 0}))


if __name__ == '__main__':
    main()
