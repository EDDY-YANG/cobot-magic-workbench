"""PC replay workspace for the optional semantic observer. No robot transport.

Only the reviewed catalog supplies images. Browser input selects catalog IDs,
never filesystem paths, model URLs, control commands, or generated evidence.
"""
from copy import deepcopy
import ctypes
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .semantic_observer import INPUT_PROFILES, SemanticObserver, StartLuxHTTPClient
from .semantic_replay import ReplayCatalog, SOURCE_SERIAL, _volume_identity
from .temporal_semantic import TEMPORAL_PROFILES


def _utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def verify_output_volume(path, expected_serial):
    """A missing/replaced USB never falls back to a PC directory."""
    path = Path(path).resolve()
    expected_serial = str(expected_serial).upper()
    if len(expected_serial) != 8 or any(c not in '0123456789ABCDEF' for c in expected_serial):
        raise ValueError('An explicit eight-digit output volume serial is required')
    if os.name != 'nt' or not path.drive or path == Path(path.anchor):
        raise ValueError('This release requires the explicitly selected Windows USB volume')
    serial = ctypes.c_uint32()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(path.anchor), None, 0, ctypes.byref(serial), None, None, None, 0)
    if not ok or f'{serial.value:08X}' != expected_serial:
        raise ValueError('Selected output USB is missing or has the wrong volume serial')
    return path


def verify_source_volume(catalog):
    """Refresh the source catalog's own serial/filesystem contract, without writes."""
    root = catalog._source_root
    identity = _volume_identity(root)
    if identity.get('serial') != SOURCE_SERIAL or str(identity.get('filesystem', '')).lower() != 'exfat':
        raise ValueError('Reviewed source USB identity changed')
    catalog._guard()
    return {'root': str(root), **identity}


def _input_metadata(episode_id, index, sample=None):
    """Retain real input identity/feedback; never images or human outcome labels."""
    value = {'source': 'replay', 'episode_id': episode_id, 'index': index}
    if sample is not None:
        for key in ('task_id', 'session_id', 'request_id', 'frame_id', 'stamps', 'image_roles'):
            value[key] = deepcopy(sample.get(key))
        actual = sample.get('state', {}).get('right', {})
        if 'temporal' in sample.get('state', {}):
            value['temporal_actual_features'] = deepcopy(sample['state']['temporal'])
        value['right_actual_feedback'] = {key: deepcopy(actual[key]) for key in (
            'q_deg', 'grip_m', 'feedback_fresh', 'age_joint_s', 'age_gripper_s', 'age_status_s',
            'source_timing') if key in actual}
    return value


class SemanticWorkspace:
    def __init__(self, config, *, catalog=None, client=None):
        self.config = deepcopy(config)
        self.input_profile = config.get('input_profile', 'full_provenance_v1')
        if self.input_profile not in INPUT_PROFILES:
            raise ValueError('Invalid semantic input_profile')
        if client is not None and hasattr(client, 'profile') and client.profile != self.input_profile:
            raise ValueError('Injected client profile differs from the workspace configuration')
        self.catalog = catalog if catalog is not None else ReplayCatalog(
            config['source_review_path'], expected_review_sha256=config['source_review_sha256'])
        if client is None:
            common=dict(endpoint=config['endpoint'], timeout_s=config.get('timeout_s',60),expected_model=config.get('expected_model'))
            if self.input_profile=='focused_dual_history_v2':
                from .focused_client import FocusedReplayClient
                client=FocusedReplayClient(self.catalog,**common)
            else:
                client=StartLuxHTTPClient(input_profile=self.input_profile,**common)
        self.observer = SemanticObserver(client=client,
            max_age_s=config.get('processing_budget_s', 90))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._run = None
        self._results = []
        self._latest = None
        self._error = None
        self._saved = None
        self._failed_save_path = None
        self._closed = False
        self._catalog_metadata = self.catalog.list()
        self.presentation = None
        if config.get('presentation_path'):
            from .semantic_presentation import SemanticPresentation
            self.presentation = SemanticPresentation(config['presentation_path'], config['presentation_sha256'],
                                                      config['output_root'], config['output_volume_serial'])

    def snapshot(self, include_results=True):
        with self._lock:
            catalog_error = None
            if not self._closed:
                try:
                    self._catalog_metadata = self.catalog.list()
                except Exception as exc:
                    # Still expose the failed run, cancellation and in-memory evidence if USB disappears.
                    catalog_error = type(exc).__name__ + ': ' + str(exc)
            value = {'schema': 'tiger-semantic-workspace-v1', 'source': 'replay',
                     'input_profile': self.input_profile,
                     'catalog': deepcopy(self._catalog_metadata), 'catalog_available': catalog_error is None,
                     'catalog_error': catalog_error, 'closed': self._closed, 'run': deepcopy(self._run),
                     'latest_sample': deepcopy(self._latest), 'error': self._error or catalog_error,
                     'receipt_path': self._saved, 'result_count': len(self._results),
                     'failed_receipt_path': self._failed_save_path,
                     'physical_task_result': 'unverified', 'motion_authority': False,
                     'commands_sent': 0, 'live_observation_connected': False,
                     'observer': self.observer.snapshot()}
            if include_results:
                value['results'] = deepcopy(self._results)
            return value

    def start(self, params):
        if not isinstance(params, dict) or set(params) - {'episode_id', 'index', 'all'}:
            raise ValueError('Replay accepts only episode_id, index and all')
        all_samples = params.get('all', False)
        index = params.get('index', 0)
        if type(all_samples) is not bool or type(index) is not int or index < 0:
            raise ValueError('Invalid replay sample selection')
        with self._lock:
            if self._closed:
                raise ValueError('Replay workspace is closed')
            if self._thread is not None and self._thread.is_alive():
                raise ValueError('A replay is already running; cancel or wait for it')
            observed = self.observer.snapshot()
            if observed['inflight'] is not None or observed['pending'] is not None:
                raise ValueError('The previous model request is still exiting; wait for its timeout or completion')
            if self._run is not None and self._run.get('save_status') == 'failed':
                try:
                    self._save()
                except Exception as exc:
                    raise ValueError('Preserve the previous unsaved replay receipt before starting again: ' + str(exc)) from exc
            source_identity = verify_source_volume(self.catalog)
            episode = next((row for row in self.catalog.list() if row['episode_id'] == params.get('episode_id')), None)
            if episode is None:
                raise ValueError('Choose one of the reviewed recordings')
            count = episode.get('observation_count')
            if type(count) is not int or not 0 < count <= 600 or index >= count:
                raise ValueError('Replay sample index is outside the reviewed observation_count')
            output = verify_output_volume(self.config['output_root'], self.config['output_volume_serial'])
            for entry in self.catalog._episodes.values():
                raw = Path(entry['root']).resolve()
                if output == raw or raw in output.parents:
                    raise ValueError('Replay receipts must not be written inside an original recording directory')
            # "Whole recording" always means [0, count), independent of the preview selection.
            first = 0 if all_samples else index
            run_id = uuid.uuid4().hex
            self._stop = threading.Event()
            self._results, self._latest, self._error, self._saved = [], None, None, None
            self._failed_save_path = None
            self.observer.set_context('replay', episode['task'], run_id)
            self._run = {'id': run_id, 'episode_id': episode['episode_id'], 'status': 'running',
                         'all': all_samples, 'requested_index': index, 'first_index': first,
                         'target_samples': count if all_samples else 1, 'observation_count': count,
                         'started_at': _utc(), 'completed_samples': 0, 'attempted_samples': 0,
                         'model_candidate_samples': 0, 'save_status': 'pending',
                         'input_profile': self.input_profile,
                         'request_input_summary': {'records_with_text_counts': 0, 'payload_text_json_chars_min': None,
                                                   'payload_text_json_chars_max': None, 'request_image_count_values': []},
                         'source_volume': source_identity,
                         'output_volume': {'root': str(output), 'serial': self.config['output_volume_serial']}}
            self._thread = threading.Thread(target=self._work,
                args=(episode['episode_id'], first, count, all_samples, run_id, self._stop),
                daemon=True, name='semantic-reviewed-replay')
            self._thread.start()
            return deepcopy(self._run)

    def cancel(self):
        with self._lock:
            if self._closed:
                return {'cancel_requested': False, 'status': 'closed', 'commands_sent': 0}
            self._stop.set()
            if self._run and self._run['status'] == 'running':
                self._run['status'] = 'cancelling'
            # Invalidate any request still inside the HTTP client's timeout.
            self.observer.set_context('replay', 'cancelled', uuid.uuid4().hex)
            return {'cancel_requested': True, 'commands_sent': 0}

    def _work(self, episode_id, index, count, all_samples, run_id, stop):
        status = 'completed'
        attempt = None
        try:
            while not stop.is_set() and index < count:
                attempt = {**_input_metadata(episode_id, index), 'started_at': _utc(), 'result': None,
                           'input_profile': self.input_profile, 'payload_text_json_chars': None, 'request_image_count': None}
                started = time.monotonic()
                with self._lock:
                    self._run['attempted_samples'] += 1
                verify_source_volume(self.catalog)
                sample = (self.catalog.temporal_observation(episode_id, index) if self.input_profile in TEMPORAL_PROFILES or self.input_profile=='focused_dual_history_v2'
                          else self.catalog.observation(episode_id, index))
                if self.input_profile=='focused_dual_history_v2':
                    sample['stamps']['focused_replay']={'episode_id':episode_id,'index':index}
                # Every HTTP response is bound to this workspace run and this sample.
                sample['session_id'] = run_id
                sample['request_id'] = run_id + '-' + str(index)
                attempt.update(_input_metadata(episode_id, index, sample))
                with self._lock:
                    if stop.is_set() or self._closed:
                        status = 'cancelled'
                        break
                    self._latest = _input_metadata(episode_id, index, sample)
                    # cancel cannot invalidate context between this setup and submission.
                    self.observer.set_context('replay', sample['task_id'], run_id)
                    submission = self.observer.submit(sample)
                if submission.get('accepted') is False:
                    raise RuntimeError('Observer rejected the reviewed sample: ' + str(submission.get('status')))
                deadline = started + self.config.get('processing_budget_s', 90) + 5
                result = None
                while not stop.wait(.05):
                    observed = self.observer.snapshot().get('result')
                    if observed and observed.get('request_id') == sample['request_id']:
                        result = observed
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError('Replay request exceeded the local processing deadline')
                if stop.is_set():
                    status = 'cancelled'
                    attempt['error'] = 'Cancelled while waiting for the model; late answers are discarded'
                    break
                if result is None:
                    raise RuntimeError('Replay produced no matching result')
                attempt.update(completed_at=_utc(), end_to_end_ms=(time.monotonic() - started) * 1000,
                               result=result, request_input=deepcopy(result.get('request_input')))
                request_input = result.get('request_input') or {}
                attempt['payload_text_json_chars'] = request_input.get('payload_text_json_chars')
                attempt['request_image_count'] = request_input.get('request_image_count')
                with self._lock:
                    self._results.append(attempt)
                    attempt = None
                    self._run['completed_samples'] += 1
                    if result.get('status') == 'model_candidate':
                        self._run['model_candidate_samples'] += 1
                    summary = self._run['request_input_summary']
                    chars, image_count = request_input.get('payload_text_json_chars'), request_input.get('request_image_count')
                    if type(chars) is int and chars >= 0:
                        summary['records_with_text_counts'] += 1
                        summary['payload_text_json_chars_min'] = chars if summary['payload_text_json_chars_min'] is None else min(chars, summary['payload_text_json_chars_min'])
                        summary['payload_text_json_chars_max'] = chars if summary['payload_text_json_chars_max'] is None else max(chars, summary['payload_text_json_chars_max'])
                    if type(image_count) is int and image_count not in summary['request_image_count_values']:
                        summary['request_image_count_values'].append(image_count)
                if result.get('status') != 'model_candidate':
                    raise RuntimeError('Model request did not validate: ' + str(result.get('error') or result.get('status')))
                if not all_samples:
                    break
                index += 1
            if stop.is_set():
                status = 'cancelled'
        except Exception as exc:
            status = 'failed'
            with self._lock:
                self._error = type(exc).__name__ + ': ' + str(exc)
                if attempt is not None:
                    attempt['error'] = self._error
                if not self._closed:
                    self.observer.set_context('replay', 'failed', run_id + '-failed')
        finally:
            with self._lock:
                if attempt is not None:
                    attempt.update(completed_at=_utc(), end_to_end_ms=(time.monotonic() - started) * 1000)
                    self._results.append(attempt)
                self._run.update(status=status, finished_at=_utc())
            try:
                self._save()
            except Exception as exc:
                with self._lock:
                    self._run['save_status'] = 'failed'
                    self._error = (self._error + '; ' if self._error else '') + 'USB save failed: ' + str(exc)

    def _save(self):
        root = verify_output_volume(self.config['output_root'], self.config['output_volume_serial'])
        with self._lock:
            saved_run = deepcopy(self._run)
            saved_run['save_status'] = 'saved'
            document = {'schema': 'tiger-semantic-replay-receipt-v1', 'run': deepcopy(self._run),
                        'input_profile': self.input_profile,
                        'request_input_summary': deepcopy(self._run['request_input_summary']),
                        'source_review_sha256': self.config['source_review_sha256'],
                        'model_endpoint': self.config['endpoint'], 'error': self._error,
                        'physical_task_result': 'unverified', 'commands_sent': 0,
                        'results': deepcopy(self._results)}
            document['run'] = saved_run
        root.mkdir(parents=True, exist_ok=True)
        filename = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d-%H-%M-%S-%f')
        destination = root / (filename + '-semantic-replay-' + document['run']['id'][:8] + '.json')
        temporary = destination.with_suffix('.json.partial')
        payload = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        with self._lock:
            self._failed_save_path = str(temporary)
        verify_output_volume(root, self.config['output_volume_serial'])
        with temporary.open('xb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        verify_output_volume(root, self.config['output_volume_serial'])
        os.replace(temporary, destination)
        with self._lock:
            self._failed_save_path = str(destination)
        if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(payload).digest():
            raise OSError('Saved replay receipt failed SHA-256 readback')
        with self._lock:
            self._saved = str(destination)
            self._failed_save_path = None
            self._run['save_status'] = 'saved'

    def image(self, episode_id, index, role):
        return self.catalog.image_bytes(episode_id, index, role)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self.cancel()
            self._closed = True
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=.5)
        self.observer.close()
