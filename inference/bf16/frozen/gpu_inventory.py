"""Read-only GPU owner classification against one measured graphical executable.

No query output is filtered or falsified. Unknown/inaccessible/reused owners
block a new model. The exact allowed daemon was inspected on 2026-10-19.
"""
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

GRAPHICAL_EXE = '/usr/libexec/gnome-remote-desktop-daemon'
GRAPHICAL_SHA = '7f6b73dfa687851f319abf99678f8f6b666128fddd9b9fb6b2fb301696145e4f'
GRAPHICAL_UIDS = [1000, 1000, 1000, 1000]


def owner(pid):
    proc = Path('/proc') / str(pid)
    start = proc.joinpath('stat').read_text().split(') ', 1)[1].split()[19]
    executable = Path(os.readlink(proc / 'exe'))
    uids = next(line.split()[1:] for line in proc.joinpath('status').read_text().splitlines() if line.startswith('Uid:'))
    result = {'pid': pid, 'start_ticks': start, 'exe': str(executable),
              'exe_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(), 'uids': [int(v) for v in uids]}
    if proc.joinpath('stat').read_text().split(') ', 1)[1].split()[19] != start:
        raise RuntimeError('PID changed during GPU identity read')
    return result


def inventory(*, query=None, read_owner=None):
    read_owner = owner if read_owner is None else read_owner
    if query is None:
        query = lambda: subprocess.check_output([
            'nvidia-smi', '--query-compute-apps=pid,process_name,used_gpu_memory',
            '--format=csv,noheader,nounits'], text=True, timeout=10)
    result = {'schema': 'tiger-fp32-gpu-owner-inventory-v1',
              'at_utc': datetime.now(timezone.utc).isoformat(), 'raw_nvidia_compute_apps': None,
              'allowed_graphical_rule': {'exe': GRAPHICAL_EXE, 'exe_sha256': GRAPHICAL_SHA, 'uids': GRAPHICAL_UIDS},
              'processes': [], 'unknown_compute_owners': []}
    try:
        raw = query()
        if not isinstance(raw, str):
            raise ValueError('GPU query did not return text')
    except Exception as exc:
        result.update(query_error=type(exc).__name__ + ': GPU process query failed', new_model_allowed=False)
        result['unknown_compute_owners'].append({'identity_error': 'GPU owner inventory unavailable'})
        return result
    result['raw_nvidia_compute_apps'] = raw
    for values in csv.reader(raw.splitlines()):
        if not values:
            continue
        entry = {'query_row': values, 'allowed_graphical_owner': False}
        try:
            if len(values) != 3:
                raise ValueError('Unexpected nvidia query columns')
            pid = int(values[0].strip())
            if pid <= 0:
                raise ValueError('Invalid query PID')
            entry.update(read_owner(pid))
            if entry.get('pid') != pid:
                raise ValueError('Owner reader returned another PID')
            entry['allowed_graphical_owner'] = (entry.get('exe') == GRAPHICAL_EXE and
                entry.get('exe_sha256') == GRAPHICAL_SHA and entry.get('uids') == GRAPHICAL_UIDS)
        except Exception as exc:
            entry['identity_error'] = type(exc).__name__ + ': GPU owner identity could not be verified'
        result['processes'].append(entry)
        if not entry['allowed_graphical_owner']:
            result['unknown_compute_owners'].append(entry)
    result['new_model_allowed'] = not result['unknown_compute_owners']
    return result


def require_available(receipt_path):
    value = inventory()
    receipt_path = Path(receipt_path)
    with receipt_path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    if not value['new_model_allowed']:
        raise RuntimeError('Unknown GPU compute owners exist and are preserved; inspect ' + str(receipt_path))
    return value
