"""Small post-run USB indexes; original recordings and receipts stay in place."""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

INDEX_FOLDER = '2026-10-24-16-03-V2部署记录'
SUBFOLDERS = {'run': '01-真实运行索引', 'acceptance': '02-NO_SEND验收'}


def index_paths(cfg):
    root = Path(cfg['usb']) / INDEX_FOLDER
    return {'root': str(root), **{key: str(root / value) for key, value in SUBFOLDERS.items()}}


def write_deployment_index(cfg, run_dir, *, action, requested_window_s, step, prompt,
                           web_status, web_error=None, child_returncode=None):
    """Called after child exit, never from a control/sender loop.

    Imports the canonical held-fd journal lazily. No copying, renaming, cleanup,
    model access or hardware access occurs. Exceptions remain caller-visible.
    """
    if action not in SUBFOLDERS:
        raise ValueError('Deployment index only supports completed run/acceptance actions')
    if (type(requested_window_s) not in (int, float) or not math.isfinite(requested_window_s)
            or not 1 <= requested_window_s <= 1800):
        raise ValueError('Finite requested window required')
    usb = Path(cfg['usb']).resolve(strict=True)
    run = Path(run_dir).resolve(strict=True)
    destination = Path(index_paths(cfg)[action])
    if (not run.is_relative_to(usb) or not destination.is_dir() or destination.is_symlink()
            or not destination.resolve(strict=True).is_relative_to(usb)):
        raise ValueError('Existing controller run/index directory on selected USB required')
    raw = (run / 'operation.json').read_bytes()
    operation = json.loads(raw.decode('utf-8-sig'))
    status = operation.get('status')
    if not isinstance(status, str) or status in ('STARTED', 'RUNNING', ''):
        raise ValueError('Final operation receipt required before indexing')
    evidence = operation.get('evidence') or {}
    recording = operation.get('recording') or {}
    episode = recording.get('owned_path') or (operation.get('recording_ready') or {}).get('owned_path')
    manifest = run / 'evidence-manifest.json'
    complete = (web_status in ('V2_WEB_WINDOW_COMPLETED', 'V2_WEB_NO_SEND_VALIDATED')
                and child_returncode == 0 and evidence.get('complete') is True)
    errors = [str(value) for value in
              [web_error, operation.get('error'), operation.get('finalization_error'), operation.get('evidence_error')]
              if value]
    errors.extend(str(value) for key in ('errors', 'monitor_errors') for value in (operation.get(key) or []))
    record = {
        'schema': 'tiger-v2-deployment-index-v1', 'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'action': action, 'run_id': run.name, 'requested_window_s': float(requested_window_s),
        'precision': 'FP32', 'step': step, 'prompt': prompt,
        'status': status, 'web_status': web_status, 'child_returncode': child_returncode,
        'completed_window': complete, 'physical_motion_requested': action == 'run',
        'actual_sent_groups': (operation.get('execution') or {}).get('actual_send_groups',
                            (operation.get('writer_close') or {}).get('completed_groups')),
        'physical_task_success': 'NOT_VERIFIED' if action == 'run' else 'NOT_RUN',
        'controller_run_path': str(run), 'operation_path': str(run / 'operation.json'),
        'operation_sha256': hashlib.sha256(raw).hexdigest(), 'hub_episode_path': episode,
        'evidence_manifest': str(manifest) if manifest.is_file() else None,
        'evidence_complete': evidence.get('complete'), 'recording_complete': recording.get('completed'),
        'error': errors[0] if errors else None, 'errors': list(dict.fromkeys(errors)),
        'original_data_copied_or_moved': False,
        'meaning': 'Navigation index only; original operation/evidence/episode remain authoritative.'}
    target = destination / (run.name + '.json')
    if target.exists():
        raise FileExistsError('Deployment index already exists; original index retained')
    from agilex_control.h50_journal import H50USBJournal
    journal = H50USBJournal(cfg, destination)
    try:
        journal.verify()
        record['storage_binding'] = dict(journal.storage_binding)
        journal.write_json(target, record)
        journal.verify()
    finally:
        journal.close()
    return {'saved': True, 'path': str(target), 'run_id': run.name,
            'operation_sha256': record['operation_sha256'], 'original_data_copied_or_moved': False}
