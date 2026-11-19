"""Explicit root-owner launch entry; does not stop an existing service."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid

JOB = Path('/opt/cobot-workbench/lab-packages/2026-10-19-18-12-H50-Model-Supervisor')
PYTHON = '/home/robot/openpi-workspace/pi05-215abfb217db/env/bin/python'


def check_port_free(port):
    with socket.socket() as probe:
        # TIME_WAIT from the stopped server is reusable; a live listener is not.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', port))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--step', type=int, choices=(2000, 5000, 10000, 15000, 20000), default=5000)
    args = parser.parse_args()
    if socket.gethostname() != 'user-MS-7E06':
        raise RuntimeError('Wrong host')
    mount = json.loads(subprocess.check_output(['findmnt', '-J', '-T', str(JOB), '-o', 'TARGET,UUID'], text=True))['filesystems'][0]
    if mount['target'] != '/data/4090-1tb' or mount['uuid'] != 'e76043b6-4ab6-42a5-8b06-4963ee92a567':
        raise RuntimeError('Wrong data filesystem')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gpu_inventory import require_available
    gpu_receipt = JOB / 'receipts' / ('before-manager-%s-GPU-owners.json' % uuid.uuid4().hex)
    require_available(gpu_receipt)
    for port in (18882, 18884):
        check_port_free(port)
    suffix = datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M-%S') + '-' + uuid.uuid4().hex[:8]
    log = JOB / 'receipts' / ('manager-launch-' + suffix + '.log')
    argv = [PYTHON, '-I', '-B', str(JOB / 'scripts/model_supervisor.py'), '--initial-step', str(args.step)]
    with log.open('xb', buffering=0) as stream:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                 start_new_session=True, cwd=JOB, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    value = {'status': 'MANAGER_STARTING_NOT_YET_READY', 'pid': child.pid, 'argv': argv, 'log': str(log),
             'requested_step': args.step, 'service': 'http://127.0.0.1:18882',
             'manager_source_sha256': hashlib.sha256((JOB / 'scripts/model_supervisor.py').read_bytes()).hexdigest()}
    (JOB / 'receipts' / ('manager-launch-' + suffix + '.json')).write_text(json.dumps(value, indent=2) + '\n')
    print(json.dumps(value))


if __name__ == '__main__':
    main()
