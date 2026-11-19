"""Inspect or explicitly stop the exact tracked manager; never kill a PID guess."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket

JOB = Path('/opt/cobot-workbench/lab-packages/2026-10-19-18-12-H50-Model-Supervisor')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('status', 'stop'))
    args = parser.parse_args()
    if socket.gethostname() != 'user-MS-7E06':
        raise RuntimeError('Wrong host')
    spec = importlib.util.spec_from_file_location('local_fp32_supervisor_control', JOB / 'scripts/model_supervisor.py')
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    saved = json.loads((JOB / 'receipts/manager-instance.json').read_text())
    if not (Path('/proc') / str(saved['pid'])).exists():
        print(json.dumps({'status': 'MANAGER_NOT_RUNNING', 'pid': saved['pid']}))
        return
    if module.process_identity(saved['pid']) != saved:
        raise RuntimeError('Manager identity changed; preserve unknown PID')
    command = (Path('/proc') / str(saved['pid']) / 'cmdline').read_bytes().split(b'\x00')
    if str(JOB / 'scripts/model_supervisor.py').encode() not in command:
        raise RuntimeError('Tracked command is not the manager entry')
    status = module.http_json(18882, 'GET', '/status')
    if status.get('schema') != 'tiger-fp32-model-supervisor-v1':
        raise RuntimeError('Manager protocol mismatch')
    if args.action == 'stop':
        if status.get('busy') is not False or status.get('lease', {}).get('active') is not False:
            raise RuntimeError('Active inference/deployment lease; explicit idle cleanup required')
        if module.process_identity(saved['pid']) != saved:
            raise RuntimeError('Manager identity changed before signal')
        os.kill(saved['pid'], signal.SIGTERM)
        print(json.dumps({'status': 'OWNED_MANAGER_SIGTERM_SENT', 'pid': saved['pid']}))
    else:
        print(json.dumps(status))


if __name__ == '__main__':
    main()
