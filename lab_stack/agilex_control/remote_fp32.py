"""H50 FP32 shadow transport. No camera reader, CAN import, or command sender.

Run on the robot host through its existing CLI owner. The 4090 endpoint must
remain behind the owner's verified SSH forward on 127.0.0.1. HTTP health is an
application contract, not a substitute for authenticated SSH peer identity.
Candidate module only: integration/real recording acceptance is owner work.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import urlsplit
import uuid

UNITS = ['rad'] * 6 + ['m']
SHAPE = [50, 7]
F32_MAX = 3.4028234663852886e38
MAX_REQUEST = 8 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
EXPECTED_POLICY = {
    'schema': 'tiger-pi05-fp32-local-v1', 'checkpoint_step': 2000,
    'precision': 'float32', 'model_compute_dtype': 'float32',
    'matmul_precision': 'highest', 'tf32_override': '0', 'horizon': 50,
    'internal_action_dim': 32, 'physical_action_dim': 7, 'units': UNITS,
    'action_basis': 'absolute', 'sampling_steps': 10, 'seed': 42,
    'batch_size': 1, 'robot_commands': 0, 'precision_audit_passed': True,
    'source_commit': '215abfb217dbac7d5f1273282331b9b1866c0479',
    'normalization_sha256': '22704c461f2485b8f15ff3b0aa99f4491e04c97735d53d1ca2e70f45dda85126',
    'tokenizer_sha256': '8986bb4f423f07f8c7f70d0dbe3526fb2316056c17bae71b1ea975e77a168fc6',
}


class ShadowError(RuntimeError):
    """Rejected or failed shadow attempt; never an authorization to dispatch."""


def _require(condition, message):
    if not condition:
        raise ShadowError(message)


def _number(value, label, *, nonnegative=False):
    _require(type(value) in (int, float) and math.isfinite(value), label + ': finite number required')
    _require(abs(value) <= F32_MAX, label + ': outside float32 range')
    if nonnegative:
        _require(value >= 0, label + ': negative value')
    return value


def _ns(value, label):
    _require(type(value) is int and value > 0, label + ': positive integer nanoseconds required')
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, 'Duplicate JSON key: ' + key)
        result[key] = value
    return result


def _bad_constant(value):
    raise ShadowError('Nonfinite JSON constant: ' + value)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _json_copy(value):
    return json.loads(_dump(value), object_pairs_hook=_pairs, parse_constant=_bad_constant)


def _fingerprint(value):
    return hashlib.sha256(_dump(value)).hexdigest()


def _same_clock_domain(actual, expected):
    _require(isinstance(actual, dict) and isinstance(expected, dict), 'Missing clock domain')
    for domain in (actual, expected):
        _require(domain.get('identity_verified') is True and domain.get('clock_name') == 'monotonic_ns',
                 'Unverified local monotonic clock identity')
        _require(all(isinstance(domain.get(k), str) and domain[k] for k in ('host_id', 'boot_id')),
                 'Clock host/boot identity missing')
    _require(all(actual[k] == expected[k] for k in ('host_id', 'boot_id')), 'Clock host/boot mismatch')
    # Producer session_id identifies a producer lifetime; two local producers
    # can share host/boot monotonic time without sharing a session_id.


def _source_time(timing, local_clock_domain, label):
    _require(isinstance(timing, dict) and timing.get('mapping_valid') is True and
             timing.get('provenance_complete') is True, label + ': invalid source provenance')
    _same_clock_domain(timing.get('clock_domain'), local_clock_domain)
    return _ns(timing.get('source_monotonic_ns_estimate'), label + ' source')


def validate_recording(binding, journal):
    _require(isinstance(binding, dict), 'Missing recording binding')
    recording, storage = binding.get('recording'), binding.get('storage')
    _require(isinstance(recording, dict) and recording.get('active') is True, 'Recorder is not active')
    _require(recording.get('episode_path') == str(journal.episode_path), 'Recording episode changed')
    _require(recording.get('errors') == [], 'Recorder reports errors or lacks error status')
    _require(type(recording.get('queue_overflows')) is int and recording['queue_overflows'] == 0,
             'Recorder queue overflow or missing queue-overflow status')
    # The recorder observes its own queue, not camera hardware exposures.
    # Zero queue overflows cannot establish zero physical camera frame drops.
    _require('physical_frame_drop_count' in recording and recording['physical_frame_drop_count'] is None,
             'Physical camera frame-drop count must remain explicitly unknown')
    _require(isinstance(storage, dict) and storage == journal.storage_binding, 'Selected USB binding changed')
    journal.verify()


def validate_snapshot(snapshot, prompt, journal, local_clock_domain, *, now_ns=None):
    """Validate host-software receipt ages, never claim exposure synchronization."""
    snapshot = _json_copy(snapshot)
    _require(snapshot.get('schema') == 'tiger-fp32-shadow-observation-v1', 'Wrong observation schema')
    _require(snapshot.get('selection_mode') == 'independent_latest' and
             snapshot.get('hardware_synchronized') is False, 'Unsupported/false synchronization claim')
    _same_clock_domain(snapshot.get('clock_domain'), local_clock_domain)
    validate_recording(snapshot, journal)
    _require(isinstance(prompt, str) and 0 < len(prompt.strip()) <= 4096 and '\x00' not in prompt,
             'Explicit nonempty task prompt required')
    now_ns = time.monotonic_ns() if now_ns is None else _ns(now_ns, 'now')
    ready = _ns(snapshot.get('packet_ready_monotonic_ns'), 'packet_ready')
    _require(0 <= now_ns - ready <= 200_000_000, 'Snapshot packet stale or in future')
    state = snapshot.get('state_right_rad_m')
    _require(isinstance(state, list) and len(state) == 7 and snapshot.get('state_units') == UNITS,
             'Expected actual right arm 6 radian joints + gripper meters')
    for value in state:
        _number(value, 'state')
    right = snapshot.get('right_state')
    _require(isinstance(right, dict) and isinstance(right.get('source_timing'), dict), 'Right feedback timing missing')
    state_source = _ns(right.get('source_monotonic_ns'), 'right source')
    _require(0 <= now_ns - state_source <= 200_000_000 and state_source <= ready,
             'Right feedback stale or in future')
    _require(right['source_timing'].get('source_monotonic_ns_estimate') == state_source,
             'Right feedback timing reference mismatch')
    _source_time(right['source_timing'], local_clock_domain, 'right aggregate')
    components = right['source_timing'].get('components')
    _require(isinstance(components, dict) and set(components) == {'q12', 'q34', 'q56', 'gripper'},
             'All four actual right feedback source streams are required')
    component_times = [_source_time(timing, local_clock_domain, name) for name, timing in components.items()]
    _require(state_source == min(component_times) and max(component_times) <= ready,
             'Right aggregate must refer to oldest required actual feedback')
    frames = snapshot.get('frames')
    _require(isinstance(frames, dict) and set(frames) == {'front', 'right'}, 'Exactly front/right JPEGs required')
    source_times = list(component_times)
    image_fields = {}
    ages = {'right_state_age_s': (now_ns - state_source) / 1e9}
    for role, frame in frames.items():
        _require(isinstance(frame, dict), 'Invalid frame')
        frame_id = frame.get('frame_id')
        _require((type(frame_id) is int and frame_id >= 0) or
                 (isinstance(frame_id, str) and bool(frame_id)), role + ': frame_id missing')
        source = _ns(frame.get('source_monotonic_ns'), role + ' source')
        available = _ns(frame.get('available_monotonic_ns'), role + ' available')
        _ns(frame.get('wall_ns'), role + ' wall')
        _require(source <= available <= ready <= now_ns and now_ns - source <= 500_000_000,
                 role + ': camera stale/future or invalid timestamp order')
        timing = frame.get('source_timing')
        _require(isinstance(timing, dict) and timing.get('source_monotonic_ns_estimate') == source,
                 role + ': source timing reference mismatch')
        _source_time(timing, local_clock_domain, role)
        _require(timing.get('frame_id') == frame_id and timing.get('available_monotonic_ns') == available,
                 role + ': frame metadata reference mismatch')
        encoded = frame.get('jpeg_base64')
        _require(isinstance(encoded, str) and 0 < len(encoded) <= MAX_REQUEST, role + ': invalid JPEG base64')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ShadowError(role + ': invalid JPEG base64') from exc
        _require(raw.startswith(b'\xff\xd8') and raw.endswith(b'\xff\xd9'), role + ': JPEG markers absent')
        _require(hashlib.sha256(raw).hexdigest() == frame.get('sha256'), role + ': JPEG SHA mismatch')
        image_fields[role + '_jpeg_base64'] = encoded
        ages[role + '_age_s'] = (now_ns - source) / 1e9
        source_times.append(source)
    ages['host_software_source_skew_s'] = (max(source_times) - min(source_times)) / 1e9
    ages['hardware_synchronized'] = False
    ages['limitation'] = 'Independent latest host software estimates; no device exposure/generation synchronization.'
    payload = dict(state=state, prompt=prompt, **image_fields)
    _require(len(_dump(payload)) <= MAX_REQUEST, 'Model request exceeds 8 MiB')
    return snapshot, payload, ages


def validate_health(health, expected_step=2000):
    _require(type(expected_step) is int and expected_step in (2000, 5000, 10000, 15000, 20000),
             'Unsupported explicit checkpoint step')
    _require(isinstance(health, dict) and health.get('ready') is True and health.get('local_only') is True,
             'FP32 server not ready/local-only')
    contract = health.get('contract')
    expected_contract = {k: 'float32' for k in ('parameter_dtype', 'compute_dtype', 'state_dtype', 'action_dtype')}
    expected_contract['action_shape'] = SHAPE
    _require(isinstance(contract, dict) and all(contract.get(k) == v for k, v in expected_contract.items()),
             'FP32 transport contract mismatch')
    policy = health.get('policy')
    _require(isinstance(policy, dict), 'Policy metadata missing')
    for key, value in {**EXPECTED_POLICY, 'checkpoint_step': expected_step}.items():
        _require(type(policy.get(key)) is type(value) and policy[key] == value, 'Policy mismatch: ' + key)
    return _fingerprint({'contract': contract, 'policy': policy})


def validate_result(result):
    _require(isinstance(result, dict) and result.get('action_dtype') == 'float32' and
             result.get('action_shape') == SHAPE, 'Expected FP32 H50x7 response')
    actions = result.get('actions')
    _require(isinstance(actions, list) and len(actions) == 50, 'Expected exactly 50 actions')
    for row in actions:
        _require(isinstance(row, list) and len(row) == 7, 'Expected 7 physical values per step')
        for value in row:
            _number(value, 'action')
    _number(result.get('inference_wall_ms'), 'server inference_wall_ms', nonnegative=True)
    for key, value in result.get('timing', {}).items():
        _number(value, 'server timing.' + key, nonnegative=True)
    return result


def _storage_hooks():
    # Imported lazily: importing this module never opens devices or creates data.
    from agilex_control.storage import verify_usb, write_json
    from agilex_control.usb_storage import open_usb_directory
    return verify_usb, write_json, open_usb_directory


class USBShadowJournal:
    """Only append new per-attempt records inside the active native USB episode.

    The existing storage owner must supply a frozen cfg/usb_selection and an
    already created episode. All real writes use its descriptor-anchored APIs.
    There is no arbitrary output directory or internal-disk fallback.
    """
    def __init__(self, cfg, expected_episode_path):
        self.cfg = copy.deepcopy(cfg)
        _require(bool(self.cfg.get('usb_selection')), 'Frozen USB selection required')
        self.episode_path = Path(expected_episode_path)
        root = Path(self.cfg['usb'])
        _require(root.is_absolute() and self.episode_path.is_absolute(), 'Absolute USB/episode paths required')
        _require('..' not in self.episode_path.parts and self.episode_path != root and
                 self.episode_path.is_relative_to(root), 'Episode must be strictly inside selected USB')
        self.storage_binding = {'mountpoint': str(root), 'uuid': self.cfg['usb_uuid'],
                                'major_minor': self.cfg['usb_selection']['major_minor']}
        self._verify_usb, self._write_json, self._open_dir = _storage_hooks()
        self.verify()

    def verify(self):
        root = self._verify_usb(self.cfg)
        _require(Path(root) == Path(self.cfg['usb']), 'Storage guard returned wrong root')
        fd = self._open_dir(self.episode_path, self.cfg)
        os.close(fd)

    def begin(self, request_id, value):
        _require(isinstance(request_id, str) and re.fullmatch('[0-9a-f]{32}', request_id) is not None,
                 'Attempt ID must be a generated lowercase UUID hex')
        self.verify()
        stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M-%S')
        directory = self.episode_path / ('%s-fp32-shadow-%s' % (stamp, request_id))
        fd = self._open_dir(directory, self.cfg, create=True, exclusive_final=True)
        os.close(fd)
        self._write_json(directory / 'input.json', value, self.cfg)
        return directory

    def finish(self, directory, value):
        self.verify()
        directory = Path(directory)
        _require(directory.parent == self.episode_path and directory.name.endswith(value['request_id']),
                 'Attempt directory binding mismatch')
        self._write_json(directory / 'result.json', value, self.cfg)


class FP32ShadowClient:
    """One in-flight attempt, no retries, fresh connection for each HTTP call."""
    def __init__(self, base_url='http://127.0.0.1:18883', *, timeout_s=3.0):
        parsed = urlsplit(base_url)
        _require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and
                 parsed.username is None and parsed.password is None and
                 parsed.path in ('', '/') and not parsed.query and not parsed.fragment,
                 'Only an explicit local IPv4 SSH-forward HTTP endpoint is allowed')
        self.port = parsed.port
        _require(type(self.port) is int and 1 <= self.port <= 65535, 'Explicit local forward port required')
        _number(timeout_s, 'timeout', nonnegative=True)
        _require(0 < timeout_s <= 15, 'Timeout must be in (0,15] seconds')
        self.timeout_s = timeout_s
        self._lock = threading.Lock()

    decision = 'SHADOW_NOT_DISPATCHED'
    success_status = 'SHADOW_INFERENCE_RECORDED'
    result_schema = 'tiger-fp32-shadow-result-v1'

    def _validate_health(self, health):
        return validate_health(health)

    def _request_headers(self, method, path):
        return {}

    def _request(self, method, path, payload=None, *, request_id=None, expected_statuses=(200,)):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=self.timeout_s)
        body = None if payload is None else _dump(payload)
        _require(body is None or len(body) <= MAX_REQUEST, 'Model request exceeds 8 MiB')
        headers = {'Accept': 'application/json', 'Connection': 'close'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if request_id:
            headers['X-Tiger-Shadow-Request-ID'] = request_id
        headers.update(self._request_headers(method, path))
        response, timer, deadline_socket = None, None, None
        deadline_lock = threading.Lock()
        try:
            deadline = time.monotonic() + self.timeout_s
            connection.connect()
            # HTTP/1.0 may close the HTTPConnection's socket when the response
            # takes ownership, and close its final file reference at body EOF.
            # Own one duplicate for the watchdog; never retune that closed
            # original socket after reading response headers/body.
            deadline_socket = connection.sock.dup()
            remaining = deadline - time.monotonic()
            _require(remaining > 0, 'HTTP connect exceeded timeout')
            # A socket timeout alone is per recv: a slow header could trickle
            # indefinitely inside http.client.getresponse(). Shutdown only this
            # attempt's socket at its absolute deadline, including header reads.
            def expire():
                # Serialize shutdown/close so a timer cannot race descriptor
                # reuse. This duplicate belongs only to the current exchange.
                with deadline_lock:
                    try:
                        deadline_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            connection.request(method, path, body=body, headers=headers)
            remaining = deadline - time.monotonic()
            _require(remaining > 0, 'HTTP attempt exceeded timeout')
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            response = connection.getresponse()
            length = response.getheader('Content-Length', '')
            _require(length.isdecimal() and 0 < int(length) <= MAX_RESPONSE and
                     response.getheader('Transfer-Encoding') is None, 'Invalid/bounded Content-Length required')
            expected_length = int(length)
            blocks, count = [], 0
            while count < expected_length:
                remaining = deadline - time.monotonic()
                _require(remaining > 0, 'HTTP response exceeded timeout')
                # The original response I/O keeps its pre-header socket
                # timeout. The duplicate watchdog enforces the whole deadline,
                # even if a peer trickles bytes before each per-recv timeout.
                block = response.read1(min(65536, expected_length - count))
                if not block:
                    break
                blocks.append(block)
                count += len(block)
            raw = b''.join(blocks)
            _require(time.monotonic() <= deadline, 'HTTP response exceeded timeout')
            _require(len(raw) == int(length), 'Truncated HTTP response')
            _require(len(raw) <= MAX_RESPONSE, 'Response exceeds size bound')
            _require(response.status in expected_statuses, 'FP32 HTTP status %d' % response.status)
            _require(response.getheader('Content-Type', '').split(';')[0].lower() == 'application/json',
                     'FP32 response is not JSON')
            return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_bad_constant)
        finally:
            if timer is not None:
                timer.cancel()
            with deadline_lock:
                if deadline_socket is not None:
                    deadline_socket.close()
            if response is not None:
                response.close()
            connection.close()

    def check_health(self):
        """Preflight only; no capture, inference, journal creation or retry."""
        _require(self._lock.acquire(blocking=False), 'Another shadow attempt is in flight')
        try:
            health = self._request('GET', '/health')
            return {'health': health, 'policy_fingerprint': self._validate_health(health)}
        finally:
            self._lock.release()

    def run_once(self, snapshot, prompt, journal, *, local_clock_domain, recording_guard):
        _require(callable(recording_guard), 'A current recording/storage guard is required')
        _require(self._lock.acquire(blocking=False), 'Another shadow attempt is in flight')
        request_id = uuid.uuid4().hex
        directory = None
        start_ns = time.monotonic_ns()
        result = {'schema': self.result_schema, 'request_id': request_id,
                  'decision': self.decision, 'module_dispatched_commands': 0,
                  'status': 'FAILED', 'client_started_monotonic_ns': start_ns,
                  'request_binding': 'Client single-inflight HTTP exchange; server does not echo request_id.',
                  'scope': 'No CAN sender exists here; this is not a global CAN-traffic assertion.'}
        try:
            snapshot = _json_copy(snapshot)
            validate_recording(snapshot, journal)
            validate_recording(recording_guard(), journal)
            # Save the attempted observation before any HTTP inference. A failed
            # validation stays a failed receipt; no retry masks the original.
            directory = journal.begin(request_id, {'request_id': request_id, 'snapshot': snapshot,
                                      'prompt': prompt, 'decision': self.decision,
                                      'module_dispatched_commands': 0})
            result['phase'] = 'INPUT_SAVED'
            health_before = self._request('GET', '/health')
            fingerprint = self._validate_health(health_before)
            result.update(health_before=health_before, policy_fingerprint=fingerprint, phase='HEALTH_VERIFIED')
            # This check is immediately before POST so health latency and durable
            # input writes cannot silently consume the observation freshness budget.
            frozen, payload, age = validate_snapshot(snapshot, prompt, journal, local_clock_domain)
            sent_ns = time.monotonic_ns()
            result.update(observation_timing=age, client_sent_monotonic_ns=sent_ns,
                          snapshot_sha256=_fingerprint(frozen), request_payload_sha256=_fingerprint(payload),
                          phase='INFER_STARTED')
            response = self._request('POST', '/infer', payload, request_id=request_id)
            received_ns = time.monotonic_ns()
            # Preserve the returned payload even if its shape/dtype/health fails.
            result.update(response=response, observation_timing=age,
                          client_sent_monotonic_ns=sent_ns, client_received_monotonic_ns=received_ns,
                          client_round_trip_ms=(received_ns - sent_ns) / 1e6,
                          snapshot_sha256=_fingerprint(frozen), request_payload_sha256=_fingerprint(payload),
                          health_before=health_before, policy_fingerprint=fingerprint, phase='INFER_RESPONSE_RECEIVED')
            validate_result(response)
            health_after = self._request('GET', '/health')
            result['health_after'] = health_after
            _require(self._validate_health(health_after) == fingerprint, 'Policy changed during shadow attempt')
            validate_recording(recording_guard(), journal)
            result['status'] = self.success_status
            result['phase'] = 'COMPLETE'
            result['observation_at_return'] = 'Not dispatchable; no freshness/safety authorization for motion.'
        except Exception as exc:
            result['error'] = {'type': type(exc).__name__, 'message': str(exc)}
            raise
        finally:
            result['client_finished_monotonic_ns'] = time.monotonic_ns()
            try:
                if directory is not None:
                    journal.finish(directory, result)
            finally:
                self._lock.release()
        return result
