"""Event-driven USB collection bookkeeping, independent of immutable raw episodes.

Native saving, human acceptance and training export are separate claims. Listing
reads only explicitly bound receipts; it neither searches all runs nor trusts a
web job cache. Importing this module opens no device, socket or storage.
"""
from contextlib import contextmanager
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid

from .storage import CHINA, Ownership, now, verify_usb, validate_episode_label
from .capture_profile import contract, profile_name, receipt_contract, PROFILES, ALL

CATEGORIES = frozenset({'A', 'B', 'C', 'D', 'random', 'recovery', 'test'})
BATCH_SCHEMA = 'tiger-collection-batch-v1'
METADATA_SCHEMA = 'tiger-collection-metadata-v1'
LIST_SCHEMA = 'tiger-collection-list-v1'
REVIEW_SCHEMA = 'tiger-collection-review-v1'
_BATCH = re.compile(r'^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-batch-[0-9a-f]{32}$')
_TRIAL = re.compile(r'^[0-9a-f]{32}$')
_EPISODE = re.compile(r'^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-episode-'
                      r'(?:(?P<label>[0-9]{6,7}-(?:A|B|C|D|random|recovery|test))-)?[0-9a-f]{8}$')
_LOCKS, _LOCKS_LOCK = {}, threading.Lock()
_METADATA_FIELDS = {'schema', 'batch_id', 'trial_id', 'ordinal', 'category',
                    'operator', 'task', 'target_count', 'goal_kind', 'notes'}
_REVIEW_FIELDS = {'schema', 'at', 'review_id', 'batch_id', 'trial_id', 'episode_id',
                  'path', 'receipt_sha256', 'reviewer', 'verdict', 'note', 'raw_modified',
                  'training_export_verified'}


def _text(value, label, maximum, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or not empty and not value.strip()):
        raise ValueError(f'{label} must be text of at most {maximum} characters without controls')
    return value.strip()


def _positive(value, label, maximum=10000):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f'{label} must be an integer in 1..{maximum}')
    return value


def _identifier(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f'Invalid {label}')
    return value


def _match_episode_name(path, metadata):
    """Legacy IDs remain valid; numbered IDs must match frozen metadata."""
    match = _EPISODE.fullmatch(path.name)
    if match is None:
        raise ValueError('Invalid episode ID')
    if match['label'] is not None:
        ordinal, category = validate_episode_label(match['label'])
        if ordinal != metadata['ordinal'] or category != metadata['category']:
            raise ValueError('Numbered episode ID differs from frozen collection metadata')


def validate_metadata(value):
    """Validate strict capture metadata without touching hardware or storage."""
    if not isinstance(value, dict) or set(value) not in (_METADATA_FIELDS, _METADATA_FIELDS | {'capture_profile'}):
        raise ValueError('Invalid collection metadata fields')
    if 'capture_profile' in value and (not isinstance(value['capture_profile'], str)
                                      or value['capture_profile'] not in PROFILES):
        raise ValueError('Unknown frozen capture profile')
    if value['schema'] != METADATA_SCHEMA or value['goal_kind'] != 'human_accepted':
        raise ValueError('Invalid collection schema or goal kind')
    _identifier(value['batch_id'], _BATCH, 'batch ID')
    _identifier(value['trial_id'], _TRIAL, 'trial ID')
    _positive(value['ordinal'], 'ordinal', 1000000)
    _positive(value['target_count'], 'target_count')
    if value['category'] not in CATEGORIES:
        raise ValueError('Invalid collection category')
    for field, size in (('operator', 80), ('task', 240), ('notes', 512)):
        if _text(value[field], field, size, empty=field == 'notes') != value[field]:
            raise ValueError('Collection metadata must contain canonical trimmed text')
    return copy.deepcopy(value)


def _summary(value, maximum=128):
    if value is None:
        return None
    return ''.join(char if ord(char) >= 32 and ord(char) != 127 else ' '
                   for char in str(value))[:maximum]


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')


def _json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON key in receipt')
        value[key] = item
    return value


def _json_constant(_):
    raise ValueError('Nonfinite JSON value in receipt')


def _read_json(path, maximum=16 * 1024 * 1024):
    with path.open('rb') as handle:
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError('Oversized JSON receipt')
    value = json.loads(data.decode('utf-8'), parse_constant=_json_constant, object_pairs_hook=_json_pairs)
    if not isinstance(value, dict):
        raise ValueError('JSON receipt must be an object')
    return value, hashlib.sha256(data).hexdigest()


class CollectionStore:
    """USB-only manifests and separate review sidecars; operations are explicit."""
    def __init__(self, cfg):
        self.cfg = cfg
        self._root()
        for key in ('cameras', 'can'):
            roles = cfg.get(key)
            if not isinstance(roles, dict) or not 1 <= len(roles) <= 8:
                raise ValueError('Collection requires bounded nonempty configured camera/CAN roles')
            for role in roles:
                _text(role, 'sensor role', 32)

    def _root(self):
        usb = Path(verify_usb(self.cfg)).resolve()
        bundle = Path(self.cfg['remote_bundle']).resolve()
        if not bundle.is_relative_to(usb) or bundle == usb:
            raise ValueError('Collection bundle must be inside the verified USB')
        if len(str(bundle).encode('utf-8')) > 512:
            raise ValueError('Collection bundle path exceeds receipt limit')
        root = (bundle / 'collections').resolve()
        if not root.is_relative_to(bundle):
            raise ValueError('Collection directory escapes configured bundle')
        return root

    @contextmanager
    def _owned(self, batch_id=None):
        root = self._root()
        key = str(root) + '/' + (batch_id or 'index')
        with _LOCKS_LOCK:
            lock = _LOCKS.setdefault(key, threading.RLock())
        with lock, Ownership('collection-' + (batch_id[-32:] if batch_id else 'index')):
            yield root

    def _batch_path(self, batch_id):
        _identifier(batch_id, _BATCH, 'batch ID')
        root = self._root()
        path = (root / batch_id).resolve()
        if path.parent != root:
            raise ValueError('Batch path escapes collection root')
        return path

    def _atomic(self, path, value, *, replace=True):
        root = self._root()
        path = Path(path)
        if not path.resolve().is_relative_to(root):
            raise ValueError('Collection write escapes verified USB collection root')
        if not replace and path.exists():
            raise FileExistsError('Immutable collection sidecar already exists')
        data = _json_bytes(value)
        temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
        try:
            with temporary.open('xb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._root()  # Recheck mounted identity before replacing the manifest.
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self, batch_id):
        path = self._batch_path(batch_id)
        value, _ = _read_json(path / 'batch.json')
        if (value.get('schema') != BATCH_SCHEMA or value.get('batch_id') != batch_id
                or value.get('goal_kind') != 'human_accepted'
                or not isinstance(value.get('attempts'), list)):
            raise ValueError('Invalid collection manifest')
        _text(value.get('task'), 'task', 240)
        _text(value.get('title'), 'title', 120)
        _text(value.get('operator'), 'operator', 80)
        _positive(value.get('target_count'), 'target_count')
        name = value.get('capture_profile', ALL)
        if not isinstance(name, str) or name not in PROFILES:
            raise ValueError('Unknown batch capture profile')
        if 'capture_profile' in value or 'capture_contract' in value:
            if ('capture_profile' not in value
                    or value.get('capture_contract') != contract(self.cfg, name)):
                raise ValueError('Frozen batch capture contract is missing or differs from registered devices')
        for name in ('reviews', 'events'):
            if (path / name).resolve() != path / name:
                raise ValueError('Batch sidecar directory redirects outside its canonical path')
        seen, paths = set(), set()
        runs = self._runs_path()
        for ordinal, attempt in enumerate(value['attempts'], 1):
            if not isinstance(attempt, dict):
                raise ValueError('Invalid collection attempt')
            metadata = validate_metadata(attempt.get('metadata'))
            if metadata.get('capture_profile', ALL) != value.get('capture_profile', ALL):
                raise ValueError('Trial capture profile differs from its batch')
            if (metadata['trial_id'] in seen or metadata['ordinal'] != ordinal
                    or any(metadata[key] != value[key] for key in
                           ('batch_id', 'task', 'operator', 'target_count', 'goal_kind'))):
                raise ValueError('Inconsistent or duplicate collection assignment')
            seen.add(metadata['trial_id'])
            if attempt.get('episode_path'):
                episode = self._episode_path(attempt['episode_path'], resolve=False, runs=runs)
                _match_episode_name(episode, metadata)
                if str(episode) in paths:
                    raise ValueError('Episode bound to multiple trials')
                paths.add(str(episode))
        return path, value

    def _runs_path(self):
        runs = (Path(self.cfg['remote_bundle']).resolve() / 'runs').resolve()
        # The configured runs directory itself must not redirect outside USB.
        if not runs.is_relative_to(Path(self.cfg['remote_bundle']).resolve()):
            raise ValueError('Runs directory escapes bundle')
        return runs

    def _episode_path(self, value, *, resolve=True, runs=None):
        if not isinstance(value, str) or len(value.encode('utf-8')) > 768:
            raise ValueError('Invalid episode path')
        runs = self._runs_path() if runs is None else runs
        supplied = Path(value)
        if not supplied.is_absolute() or '..' in supplied.parts:
            raise ValueError('Episode path must be absolute without traversal')
        path = supplied.resolve() if resolve else supplied
        if path.parent != runs or str(path) != str(supplied):
            raise ValueError('Episode must be a direct canonical child of runs')
        _identifier(path.name, _EPISODE, 'episode ID')
        label = _EPISODE.fullmatch(path.name)['label']
        if label is not None:
            validate_episode_label(label)
        if resolve and (path / 'operation.json').resolve() != path / 'operation.json':
            raise ValueError('Episode receipt escapes episode directory')
        return path

    def create(self, task, title, operator, target_count=100):
        task, title, operator = (_text(task, 'task', 240), _text(title, 'title', 120),
                                  _text(operator, 'operator', 80))
        _positive(target_count, 'target_count')
        with self._owned() as root:
            batch_id = dt.datetime.now(CHINA).strftime('%Y-%m-%d-%H-%M') + '-batch-' + uuid.uuid4().hex
            path = root / batch_id
            path.mkdir(parents=True, exist_ok=False)
            (path / 'reviews').mkdir()
            (path / 'events').mkdir()
            value = {'schema': BATCH_SCHEMA, 'batch_id': batch_id, 'created_at': now(),
                     'task': task, 'title': title, 'operator': operator, 'target_count': target_count,
                     'goal_kind': 'human_accepted', 'attempts': []}
            if 'capture_profile' in self.cfg:
                value['capture_profile'] = profile_name(self.cfg)
                value['capture_contract'] = contract(self.cfg)
            self._atomic(path / 'batch.json', value)
            return copy.deepcopy(value)

    def prepare_start(self, batch_id, trial_id, category, notes=''):
        _identifier(trial_id, _TRIAL, 'trial ID')
        if category not in CATEGORIES:
            raise ValueError('Invalid collection category')
        notes = _text(notes, 'notes', 512, empty=True)
        with self._owned(batch_id):
            path, batch = self._load(batch_id)
            if batch.get('capture_profile', ALL) != profile_name(self.cfg):
                raise ValueError('Batch capture profile differs from current service; select a matching batch')
            for attempt in batch['attempts']:
                metadata = attempt['metadata']
                if metadata['trial_id'] == trial_id:
                    if metadata['category'] != category or metadata['notes'] != notes:
                        raise ValueError('Trial ID is already reserved with different metadata')
                    return copy.deepcopy(metadata)
            metadata = {'schema': METADATA_SCHEMA, 'batch_id': batch_id, 'trial_id': trial_id,
                        'ordinal': len(batch['attempts']) + 1, 'category': category,
                        'operator': batch['operator'], 'task': batch['task'],
                        'target_count': batch['target_count'], 'goal_kind': 'human_accepted', 'notes': notes}
            if 'capture_profile' in batch:
                metadata['capture_profile'] = batch['capture_profile']
            validate_metadata(metadata)
            batch['attempts'].append({'metadata': metadata, 'reserved_at': now(), 'episode_path': None})
            self._atomic(path / 'batch.json', batch)
            return copy.deepcopy(metadata)

    def _assignment(self, batch, metadata):
        metadata = validate_metadata(metadata)
        for attempt in batch['attempts']:
            if attempt['metadata']['trial_id'] == metadata['trial_id']:
                if attempt['metadata'] != metadata:
                    raise ValueError('Capture metadata differs from frozen trial assignment')
                return attempt
        raise ValueError('Trial has not been reserved')

    def bind_episode(self, metadata, path):
        metadata = validate_metadata(metadata)
        with self._owned(metadata['batch_id']):
            directory, batch = self._load(metadata['batch_id'])
            attempt = self._assignment(batch, metadata)
            episode = self._episode_path(str(path))
            _match_episode_name(episode, metadata)
            if attempt.get('episode_path'):
                if attempt['episode_path'] != str(episode):
                    raise ValueError('Trial is already bound to another episode')
                return {'metadata': copy.deepcopy(metadata), 'episode_id': episode.name, 'path': str(episode)}
            if any(item.get('episode_path') == str(episode) for item in batch['attempts']):
                raise ValueError('Episode is already assigned to another trial')
            report, _ = _read_json(episode / 'operation.json')
            self._match_receipt(report, metadata, episode)
            attempt.update(episode_path=str(episode), bound_at=now())
            self._atomic(directory / 'batch.json', batch)
            return {'metadata': copy.deepcopy(metadata), 'episode_id': episode.name, 'path': str(episode)}

    def failed_start(self, metadata, error):
        metadata = validate_metadata(metadata)
        message = str(error)[:8192]
        with self._owned(metadata['batch_id']):
            path, batch = self._load(metadata['batch_id'])
            attempt = self._assignment(batch, metadata)
            event_id = uuid.uuid4().hex
            event = {'schema': 'tiger-collection-start-failure-v1', 'at': now(),
                     'metadata': metadata, 'error': message}
            self._atomic(path / 'events' / (event_id + '.json'), event, replace=False)
            attempt['start_failure'] = {'event': event_id, 'at': event['at'], 'error': _summary(message)}
            self._atomic(path / 'batch.json', batch)
            return event

    def trial_status(self, metadata):
        """Resolve an idempotent start retry without launching a second recorder."""
        metadata = validate_metadata(metadata)
        with self._owned(metadata['batch_id']):
            _, batch = self._load(metadata['batch_id'])
            attempt = self._assignment(batch, metadata)
            return copy.deepcopy(attempt)

    def retry_metadata(self, batch_id, trial_id):
        """Recover complete frozen fields, never a shortened list-note summary."""
        _identifier(trial_id, _TRIAL, 'trial ID')
        with self._owned(batch_id):
            _, batch = self._load(batch_id)
            attempt = next((item for item in batch['attempts']
                            if item['metadata']['trial_id'] == trial_id), None)
            if attempt is None:
                raise ValueError('No reserved trial matches retry identity')
            return validate_metadata(attempt['metadata'])

    @staticmethod
    def _match_receipt(report, metadata, path=None):
        if (report.get('operation') != 'episode' or report.get('format') != 'tiger-native-sensors-v1'
                or report.get('collection_v1') != metadata or report.get('task') != metadata['task']):
            raise ValueError('Native receipt does not match frozen collection metadata')
        if path is not None:
            _match_episode_name(path, metadata)
            if (_EPISODE.fullmatch(path.name)['label'] is not None
                    and report.get('raw_storage') != str(path)):
                raise ValueError('Numbered episode receipt differs from canonical raw storage path')

    def _receipt(self, attempt):
        # A corrupt/replaced episode directory is one bad row, not a reason to
        # hide all other bound episodes. Never follow a rejected redirected path.
        safe_path = self._episode_path(attempt['episode_path'], resolve=False)
        _match_episode_name(safe_path, attempt['metadata'])
        try:
            path = self._episode_path(attempt['episode_path'])
        except Exception as exc:
            return safe_path, None, None, _summary(exc)
        try:
            report, digest = _read_json(path / 'operation.json')
            self._match_receipt(report, attempt['metadata'], path)
            return path, report, digest, None
        except Exception as exc:
            try:
                with (path / 'operation.json').open('rb') as handle:
                    data = handle.read(16 * 1024 * 1024 + 1)
                digest = hashlib.sha256(data).hexdigest() if len(data) <= 16 * 1024 * 1024 else None
            except OSError:
                digest = None
            return path, None, digest, _summary(exc)

    @staticmethod
    def _finalized(report):
        try:
            end = dt.datetime.fromisoformat(report.get('ended_at', ''))
            return report.get('status') in ('RECORDED_NATIVE_REVIEW_REQUIRES_QC', 'RECORDING_FAILED') and end.tzinfo is not None
        except (ValueError, TypeError):
            return False

    def _health(self, report):
        if report is None:
            return False, False, 'Native receipt missing, corrupt or mismatched'
        terminal = report.get('status') == 'RECORDED_NATIVE_REVIEW_REQUIRES_QC' and self._finalized(report)
        counts_ok = type(report.get('samples')) is int and 0 < report['samples'] <= 2**63-1
        try:
            frozen = receipt_contract(self.cfg, report)
        except (ValueError, TypeError, KeyError):
            return False, False, 'Recording capture contract is missing or differs from frozen profile/devices'
        for field, roles in (('camera_frame_counts', frozen['required_camera_roles']),
                             ('raw_CAN_counts', frozen['required_arm_roles'])):
            counts = report.get(field)
            counts_ok = counts_ok and isinstance(counts, dict) and all(
                type(counts.get(role)) is int and 0 < counts[role] <= 2**63-1 for role in roles)
            if report.get('capture_contract') is not None:
                counts_ok = counts_ok and isinstance(counts, dict) and set(counts) == set(roles)
        saved = (terminal and counts_ok and report.get('failure') is None
                 and type(report.get('queue_overflows')) is int and report['queue_overflows'] == 0)
        if not saved:
            return False, False, 'Native episode is active, failed, incomplete or not safely finalized'
        quality = report.get('quality_summary')
        if (not isinstance(quality, dict) or quality.get('schema') != 'tiger-recording-quality-v1'
                or any(type(quality.get(key)) is not int or quality[key] < 0 for key in
                       ('gripper_fault_samples', 'gripper_unknown_samples'))
                or type(quality.get('coordinate_pending')) is not bool):
            return True, False, 'Recording quality is unknown; human acceptance remains pending'
        if quality['gripper_fault_samples'] or quality['gripper_unknown_samples'] or quality['coordinate_pending']:
            return True, False, 'Recording includes gripper faults, unknown feedback or coordinate review pending'
        if 'joint_fault_messages' in quality and (type(quality['joint_fault_messages']) is not int
                                                  or quality['joint_fault_messages'] != 0):
            return True, False, 'Recording includes joint faults or joint fault accounting is unknown'
        if 'arm_error_messages' in quality and (type(quality['arm_error_messages']) is not int
                                                or quality['arm_error_messages'] != 0):
            return True, False, 'Recording includes controller errors or controller error accounting is unknown'
        return True, True, None

    def _review_value(self, directory, attempt, digest):
        selected = attempt.get('review')
        if not selected:
            return None, None
        try:
            filename = selected['file']
            if not re.fullmatch(r'[0-9a-f]{32}\.json', filename):
                raise ValueError('Invalid review sidecar name')
            path = directory / 'reviews' / filename
            if path.resolve().parent != (directory / 'reviews').resolve():
                raise ValueError('Review path escapes batch')
            value, _ = _read_json(path, 65536)
            if (set(value) != _REVIEW_FIELDS or value.get('schema') != REVIEW_SCHEMA
                    or value.get('batch_id') != attempt['metadata']['batch_id']
                    or value.get('trial_id') != attempt['metadata']['trial_id']
                    or value.get('episode_id') != Path(attempt['episode_path']).name
                    or value.get('path') != attempt['episode_path']
                    or value.get('review_id') + '.json' != filename
                    or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                    or value.get('receipt_sha256') != digest
                    or value.get('verdict') not in ('accepted', 'rejected')
                    or value.get('raw_modified') is not False or value.get('training_export_verified') is not False):
                raise ValueError('Review is stale or does not match current native receipt SHA')
            for key, maximum in (('reviewer', 80), ('note', 512)):
                if _text(value[key], key, maximum, empty=key == 'note') != value[key]:
                    raise ValueError('Review text is not canonical')
            if dt.datetime.fromisoformat(value['at']).tzinfo is None:
                raise ValueError('Review time lacks timezone')
            return value, None
        except Exception as exc:
            return None, _summary(exc)

    def list(self, batch_id, page=1, page_size=10):
        _positive(page, 'page', 1000000)
        _positive(page_size, 'page_size', 10)
        with self._owned(batch_id):
            directory, batch = self._load(batch_id)
            batch_capture = batch.get('capture_contract', contract(self.cfg, ALL))
            counts = dict.fromkeys(('native_saved', 'recording_failed', 'human_accepted', 'human_rejected', 'human_unreviewed'), 0)
            counts.update(reserved=0, active=0, corrupt_receipts=0)
            rows = []
            first, last = (page - 1) * page_size, page * page_size
            for index, attempt in enumerate(batch['attempts']):
                meta = attempt['metadata']
                row = {'id': meta['trial_id'], 'episode_id': None, 'trial_id': meta['trial_id'],
                       'path': None, 'ordinal': meta['ordinal'], 'task': _summary(meta['task']),
                       'capture_profile': meta.get('capture_profile', ALL),
                       'category': meta['category'], 'notes': _summary(meta['notes']), 'status': 'RESERVED',
                       'samples': None, 'camera_frame_counts': {}, 'raw_CAN_counts': {}, 'failure': None,
                       'review': None, 'finalized': False, 'started_at': None, 'ended_at': None,
                       'accept_allowed': False, 'accept_blocked_reason': 'No bound native episode'}
                if attempt.get('episode_path'):
                    path, report, digest, error = self._receipt(attempt)
                    saved, acceptable, reason = self._health(report)
                    review, review_error = self._review_value(directory, attempt, digest)
                    # A once accepted review does not remain accepted if quality or raw evidence changes.
                    if review and review['verdict'] == 'accepted' and not acceptable:
                        review_error, review = 'Current native quality does not support acceptance', None
                    counts['native_saved'] += int(saved)
                    failed = error is not None or report is not None and report.get('status') == 'RECORDING_FAILED'
                    counts['recording_failed'] += int(failed)
                    counts['corrupt_receipts'] += int(error is not None)
                    counts['active'] += int(report is not None and not report.get('ended_at') and not failed)
                    counts['human_' + (review['verdict'] if review else 'unreviewed')] += 1
                    row.update(id=path.name, episode_id=path.name, path=str(path),
                               status=_summary(report.get('status'), 64) if report else 'RECEIPT_CORRUPT',
                               failure=error or _summary(report.get('failure')) if report else error,
                               accept_allowed=acceptable, accept_blocked_reason=reason,
                               receipt_sha256=digest, review_error=review_error)
                    if attempt.get('start_failure'):
                        row['start_event_error'] = _summary(attempt['start_failure'].get('error'))
                    if report:
                        def bounded_counts(field, roles):
                            values = report.get(field)
                            values = values if isinstance(values, dict) else {}
                            return {role: values.get(role) if type(values.get(role)) is int
                                    and 0 <= values[role] <= 2**63-1 else None for role in roles}
                        row.update(samples=report.get('samples') if type(report.get('samples')) is int
                                   and 0 <= report['samples'] <= 2**63-1 else None,
                                   camera_frame_counts=bounded_counts('camera_frame_counts', batch_capture['required_camera_roles']),
                                   raw_CAN_counts=bounded_counts('raw_CAN_counts', batch_capture['required_arm_roles']),
                                   finalized=self._finalized(report), started_at=_summary(report.get('started_at'), 64),
                                   ended_at=_summary(report.get('ended_at'), 64))
                    if review:
                        row['review'] = {key: _summary(review.get(key)) for key in ('verdict', 'reviewer', 'note', 'at')}
                elif attempt.get('start_failure'):
                    counts['recording_failed'] += 1
                    row.update(status='START_FAILED', failure=attempt['start_failure'].get('error'))
                else:
                    counts['reserved'] += 1
                if first <= index < last:
                    rows.append(row)
            result = {'schema': LIST_SCHEMA, 'batch': {key: batch[key] for key in
                      ('batch_id', 'title', 'task', 'operator', 'target_count', 'goal_kind', 'created_at')},
                      'counts': counts, 'episodes': rows,
                      'pagination': {'page': page, 'page_size': page_size, 'total_items': len(batch['attempts']),
                                     'total_pages': (len(batch['attempts']) + page_size - 1) // page_size},
                      'training_export_verified': False}
            result['batch']['capture_profile'] = batch.get('capture_profile', ALL)
            result['batch']['capture_contract'] = copy.deepcopy(batch_capture)
            def output_bytes():
                return len((json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8'))
            if output_bytes() > 32768:
                for row in rows:
                    for key in ('task', 'notes', 'failure', 'start_event_error', 'accept_blocked_reason', 'review_error'):
                        if key in row:
                            row[key] = _summary(row[key], 64)
                    if row['review']:
                        for key in ('reviewer', 'note'):
                            row['review'][key] = _summary(row['review'][key], 64)
                result['summary_truncated'] = True
            if output_bytes() > 32768:
                raise ValueError('Collection list exceeds bounded 32 KiB receipt; inspect USB details')
            return result

    def review(self, batch_id, episode_id, reviewer, verdict, note):
        _identifier(episode_id, _EPISODE, 'episode ID')
        reviewer = _text(reviewer, 'reviewer', 80)
        note = _text(note, 'review note', 512, empty=True)
        if verdict not in ('accepted', 'rejected'):
            raise ValueError('Review verdict must be accepted or rejected')
        with self._owned(batch_id):
            directory, batch = self._load(batch_id)
            attempt = next((item for item in batch['attempts'] if item.get('episode_path')
                            and Path(item['episode_path']).name == episode_id), None)
            if attempt is None:
                raise ValueError('Episode is not assigned to this batch')
            path, report, digest, error = self._receipt(attempt)
            _, acceptable, reason = self._health(report)
            if verdict == 'accepted' and not acceptable:
                raise ValueError(reason)
            if digest is None:
                raise ValueError('Review requires a readable receipt SHA, including when rejecting')
            old, _ = self._review_value(directory, attempt, digest)
            if old and all(old[key] == value for key, value in
                           (('verdict', verdict), ('reviewer', reviewer), ('note', note))):
                return copy.deepcopy(old)
            review_id = uuid.uuid4().hex
            value = {'schema': REVIEW_SCHEMA, 'at': now(), 'review_id': review_id,
                     'batch_id': batch_id, 'trial_id': attempt['metadata']['trial_id'],
                     'episode_id': episode_id, 'path': str(path), 'receipt_sha256': digest,
                     'reviewer': reviewer, 'verdict': verdict, 'note': note,
                     'raw_modified': False, 'training_export_verified': False}
            self._atomic(directory / 'reviews' / (review_id + '.json'), value, replace=False)
            # Detect a concurrent recorder finalization before publishing the review index.
            _, _, current_digest, _ = self._receipt(attempt)
            if current_digest != digest:
                raise ValueError('Native receipt changed during review; saved sidecar is not selected')
            attempt['review'] = {'file': review_id + '.json', 'at': value['at']}
            self._atomic(directory / 'batch.json', batch)
            return value

    def require_accepted_episode(self, batch_id, episode_id):
        """Export prerequisite for new collection episodes, not export success.

        Recheck current native quality and the selected human sidecar SHA. Read
        only this bound episode; no raw file or review marker is modified.
        """
        _identifier(episode_id, _EPISODE, 'episode ID')
        with self._owned(batch_id):
            directory, batch = self._load(batch_id)
            attempt = next((item for item in batch['attempts'] if item.get('episode_path')
                            and Path(item['episode_path']).name == episode_id), None)
            if attempt is None:
                raise ValueError('Episode is not assigned to this batch')
            path, report, digest, error = self._receipt(attempt)
            _, acceptable, reason = self._health(report)
            if not acceptable:
                raise ValueError(reason)
            selected, review_error = self._review_value(directory, attempt, digest)
            if selected is None or selected['verdict'] != 'accepted':
                raise ValueError('Episode requires a current SHA-bound human accepted review: '
                                 + (review_error or 'not accepted'))
            return {'schema': 'tiger-collection-accepted-episode-v1', 'episode_id': episode_id,
                    'path': str(path), 'metadata': copy.deepcopy(attempt['metadata']),
                    'receipt_sha256': digest, 'status': report['status'], 'ended_at': report['ended_at'],
                    'finalized': True, 'accept_allowed': True, 'review': copy.deepcopy(selected),
                    'raw_modified': False, 'training_export_verified': False}

    def lookup_episode_binding(self, episode_id, path):
        """Low-frequency export lookup when raw metadata may have been removed.

        Search collection manifests only. A damaged/unknown manifest is not
        evidence of absence and cannot silently turn an episode into legacy.
        """
        _identifier(episode_id, _EPISODE, 'episode ID')
        episode = self._episode_path(str(path))
        if episode.name != episode_id:
            raise ValueError('Episode ID differs from canonical episode path')
        with self._owned() as root:
            if not root.exists():
                if _EPISODE.fullmatch(episode_id)['label'] is not None:
                    raise ValueError('Numbered episode requires frozen collection binding')
                return None
            found = None
            for directory in sorted(root.iterdir()):
                if not directory.is_dir() or not _BATCH.fullmatch(directory.name):
                    raise ValueError('Unknown collection entry; legacy status cannot be assumed')
                _, batch = self._load(directory.name)
                for attempt in batch['attempts']:
                    if attempt.get('episode_path') == str(episode):
                        if found is not None:
                            raise ValueError('Episode has multiple collection bindings')
                        found = {'episode_id': episode_id, 'path': str(episode),
                                 'metadata': copy.deepcopy(attempt['metadata'])}
            if found is None and _EPISODE.fullmatch(episode_id)['label'] is not None:
                raise ValueError('Numbered episode requires frozen collection binding')
            return found
