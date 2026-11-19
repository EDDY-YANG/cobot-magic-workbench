"""Local FP32 model selection, single GPU worker, and deployment leases.

Only model computation is supervised. No robot/control/SDK code is imported.
CPU tests inject an explicitly fake backend; they cannot validate real hardware.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

HOST = '127.0.0.1'
MANAGER_PORT = 18882
WORKER_PORT = 18884
STEPS = (2000, 5000, 10000, 15000, 20000)
LEASE_TTL = 2.0
ACCEPTANCE_SEMANTICS = {'timing_basis': 'packet_ready_to_validated_ram_commit_ready', 'storage_semantic_version': 'tiger-h50-bounded-ram-async-usb-finalflush-v1', 'execution_contract_version': 'tiger-h50-playback18-r14-d600-native-uncapped-velocity-v3', 'native_training_hz': 30, 'execution_label_hz': 18, 'replan_steps': 14, 'reply_deadline_ms': 600, 'acceptance_p95_ms': 550, 'acceptance_max_ms': 600, 'startup_semantic_version': 'tiger-h50-native-bootstrap-then-rtc-v1', 'no_send_semantic_version': 'tiger-h50-diagnostic-reference-physical-hold-v1', 'can_recording_timestamp_semantic_version': 'tiger-can-kernel-wall-ns-v1'}

ACCEPTANCE_P95_MS, ACCEPTANCE_MAX_MS = 550, 600
MAX_BODY = 8 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
JOB = Path('/opt/cobot-workbench/lab-packages/2026-10-19-18-12-H50-Model-Supervisor')
ARCHIVE = Path('/data/4090-1tb/RobotData/02_Models/2026-10-18_Pi05_FP32_FullFineTune/Complete_Archive_Package')
PYTHON = '/home/robot/openpi-workspace/pi05-215abfb217db/env/bin/python'
NORM_SHA = '22704c461f2485b8f15ff3b0aa99f4491e04c97735d53d1ca2e70f45dda85126'
TOKEN_SHA = '8986bb4f423f07f8c7f70d0dbe3526fb2316056c17bae71b1ea975e77a168fc6'
COMMIT = '215abfb217dbac7d5f1273282331b9b1866c0479'
SUPERVISOR_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
sys.path.insert(0, str(Path(__file__).resolve().parent))


class APIError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


def require(condition, code, message, status=409):
    if not condition:
        raise APIError(code, message, status)


def step_value(value):
    require(type(value) is int and value in STEPS, 'INVALID_STEP', 'Expected step 2000, 5000, 10000, 15000, or 20000', 400)
    return value


def session_value(value):
    require(isinstance(value, str) and 0 < len(value) <= 200 and value.strip() == value and '\x00' not in value,
            'INVALID_SESSION', 'An explicit nonempty session_id of at most 200 characters is required', 400)
    return value


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def health_validated(value, step):
    require(isinstance(value, dict) and value.get('ready') is True and value.get('local_only') is True,
            'WORKER_NOT_READY', 'Worker is not ready/local-only', 503)
    contract, policy = value.get('contract'), value.get('policy')
    expected = {'schema': 'tiger-pi05-fp32-local-v1', 'checkpoint_step': step,
                'precision': 'float32', 'model_compute_dtype': 'float32', 'matmul_precision': 'highest',
                'tf32_override': '0', 'horizon': 50, 'internal_action_dim': 32, 'physical_action_dim': 7,
                'units': ['rad'] * 6 + ['m'], 'action_basis': 'absolute', 'sampling_steps': 10,
                'seed': 42, 'batch_size': 1, 'robot_commands': 0, 'precision_audit_passed': True,
                'source_commit': COMMIT, 'normalization_sha256': NORM_SHA, 'tokenizer_sha256': TOKEN_SHA}
    require(isinstance(policy, dict) and all(type(policy.get(k)) is type(v) and policy[k] == v for k, v in expected.items()),
            'WORKER_CONTRACT_MISMATCH', 'Worker policy identity/FP32 contract differs from the selected candidate', 503)
    expected_contract = {k: 'float32' for k in ('parameter_dtype', 'compute_dtype', 'state_dtype', 'action_dtype')}
    expected_contract['action_shape'] = [50, 7]
    require(isinstance(contract, dict) and all(contract.get(k) == v for k, v in expected_contract.items()),
            'WORKER_CONTRACT_MISMATCH', 'Worker transport FP32 contract is invalid', 503)
    rtc, binding = value.get('rtc'), value.get('acceptance_binding')
    require(isinstance(rtc, dict) and rtc.get('schema') == 'tiger-fp32-h50-rtc-v1'
            and rtc.get('enabled') is True and rtc.get('precision_audit_passed') is True
            and rtc.get('freeze_steps') == 12 and rtc.get('previous_valid_steps') == 35
            and rtc.get('execution_label_hz') == 18 and rtc.get('native_training_hz') == 30
            and rtc.get('beta') == 5,
            'RTC_CONTRACT_MISMATCH', 'Worker lacks the separately audited native32 FP32 RTC contract', 503)
    require(isinstance(binding, dict) and isinstance(binding.get('identity'), dict)
            and binding.get('semantics') == ACCEPTANCE_SEMANTICS
            and isinstance(binding.get('contract_fingerprint'), str) and len(binding['contract_fingerprint']) == 64
            and value.get('contract_fingerprint') == binding['contract_fingerprint']
            and binding['identity'].get('supervisor_sha256') == SUPERVISOR_SHA256
            and binding['identity'].get('normalization_sha256') == NORM_SHA
            and binding['identity'].get('tokenizer_sha256') == TOKEN_SHA
            and binding['identity'].get('source_commit') == COMMIT,
            'RTC_BINDING_MISMATCH', 'Worker contract fingerprint or loaded supervisor code binding differs', 503)
    for name, key in (('implementation_sha256', 'rtc_implementation_sha256'),
                      ('precision_audit_sha256', 'rtc_precision_audit_sha256')):
        require(isinstance(rtc.get(name), str) and len(rtc[name]) == 64 and rtc[name] == binding['identity'].get(key),
                'RTC_BINDING_MISMATCH', 'RTC implementation/audit binding is invalid', 503)
    return deepcopy(value)


def acceptance_receipt(path, step, binding):
    """Read externally produced real acceptance only; never manufacture PASS."""
    path = Path(path)
    if not path.exists():
        return {'schema': 'tiger-fp32-h50-rtc-acceptance-v1', 'step': step, 'status': 'NOT_RUN'}
    try:
        require(not path.is_symlink() and path.stat().st_size <= MAX_RESPONSE,
                'INVALID_ACCEPTANCE', 'Acceptance receipt is redirected or oversized')
        value = json.loads(path.read_text(encoding='utf-8'),
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
        require(value.get('schema') == 'tiger-fp32-h50-rtc-acceptance-v1' and type(value.get('step')) is int and value['step'] == step,
                'INVALID_ACCEPTANCE', 'Acceptance schema/step differs')
        if not isinstance(binding, dict):
            return {'schema': value['schema'], 'step': step, 'status': 'UNVERIFIED_CURRENT_MODEL',
                    'declared_status': value.get('status'), 'reason': 'Select and verify this step before binding acceptance'}
        if (value.get('identity') != binding.get('identity')
                or value.get('contract_fingerprint') != binding.get('contract_fingerprint')
                or value.get('semantics') != binding.get('semantics')
                or binding.get('semantics') != ACCEPTANCE_SEMANTICS):
            return {'schema': value['schema'], 'step': step, 'status': 'STALE',
                    'reason': 'Live code, RTC IR, checkpoint, normalization, tokenizer or timing/storage semantics changed'}
        requirements = {'warmup_calls': 3, 'measured_calls': 100, 'p95_budget_ms': ACCEPTANCE_P95_MS,
                        'max_budget_ms': ACCEPTANCE_MAX_MS, 'whole_gpu_budget_mib': 22528}
        require(value.get('requirements') == requirements, 'INVALID_ACCEPTANCE', 'Approved acceptance thresholds changed')
        results = value.get('results')
        require(isinstance(results, dict) and results.get('mode') == 'native32_bootstrap_then_rtc'
                and results.get('timing_basis') == ACCEPTANCE_SEMANTICS['timing_basis']
                and type(results.get('warmup_calls')) is int and results['warmup_calls'] == 3
                and type(results.get('measured_calls')) is int and results['measured_calls'] == 100
                and type(results.get('vjp_enabled_calls')) is int and results['vjp_enabled_calls'] == 100
                and type(results.get('native_bootstrap_calls')) is int and results['native_bootstrap_calls'] == 1
                and type(results.get('rtc_continuation_calls')) is int and results['rtc_continuation_calls'] == 102
                and results.get('bootstrap_accepted_before_deadline') is True
                and results.get('diagnostic_reference_chain_validated') is True,
                'INVALID_ACCEPTANCE', 'Real warmup/measurement/VJP sample counts do not match the approved contract')
        for key in ('all_actions_float32_50x7', 'zero_weight_same_rng_noise_equivalent',
                    'robot_chain_audits_passed', 'usb_finalflush_validated'):
            require(results.get(key) is True, 'ACCEPTANCE_FAILED', 'Required real result failed or is missing: ' + key)
        latency = results.get('latency_ms', {})
        p95, maximum, gpu = latency.get('p95'), latency.get('max'), results.get('whole_gpu_peak_mib')
        require(all(type(number) in (int, float) and math.isfinite(number) and number >= 0 for number in (p95, maximum, gpu))
                and p95 <= maximum and p95 <= ACCEPTANCE_P95_MS and maximum <= ACCEPTANCE_MAX_MS and gpu <= 22528,
                'ACCEPTANCE_FAILED', 'Actual end-to-end latency or whole-GPU memory exceeds the approved budget')
        require(value.get('status') == 'PASS' and isinstance(value.get('evidence'), dict) and bool(value['evidence']),
                'ACCEPTANCE_FAILED', 'External real acceptance did not pass or lacks evidence references')
        return deepcopy(value)
    except Exception as exc:
        return {'schema': 'tiger-fp32-h50-rtc-acceptance-v1', 'step': step, 'status': 'FAIL',
                'reason': getattr(exc, 'code', 'INVALID_ACCEPTANCE'), 'receipt': str(path)}


def http_json(port, method, route, payload=None, *, timeout=2.0):
    """Bounded loopback exchange with an absolute socket deadline."""
    connection = http.client.HTTPConnection(HOST, port, timeout=timeout)
    timer, deadline_socket, response = None, None, None
    try:
        connection.connect()
        deadline_socket = connection.sock.dup()
        def expire():
            try:
                deadline_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        data = None if payload is None else json_bytes(payload)
        headers = {'Connection': 'close'}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        connection.request(method, route, data, headers)
        response = connection.getresponse()
        lengths = response.headers.get_all('Content-Length', [])
        require(len(lengths) == 1 and lengths[0].isascii() and lengths[0].isdecimal() and
                len(lengths[0]) <= 8 and 0 < int(lengths[0]) <= MAX_RESPONSE and
                response.getheader('Transfer-Encoding') is None,
                'INVALID_WORKER_RESPONSE', 'Worker response framing is missing or unbounded', 502)
        length = int(lengths[0])
        raw = response.read(length)
        require(len(raw) == length, 'INVALID_WORKER_RESPONSE', 'Worker returned an incomplete response', 502)
        body = json.loads(raw.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
        require(isinstance(body, dict), 'INVALID_WORKER_RESPONSE', 'Worker response must be a JSON object', 502)
        if response.status != 200:
            # Never echo worker errors that might contain an observation/prompt.
            raise APIError('WORKER_HTTP_ERROR', 'Worker rejected the request (HTTP %d)' % response.status, response.status)
        return body
    finally:
        if timer is not None:
            timer.cancel()
        if response is not None:
            response.close()
        connection.close()
        if deadline_socket is not None:
            deadline_socket.close()


def process_identity(pid):
    proc = Path('/proc') / str(pid)
    stat = proc.joinpath('stat').read_text().split(') ', 1)[1].split()
    command = proc.joinpath('cmdline').read_bytes()
    return {'pid': pid, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'start_ticks': stat[19], 'cmdline_sha256': hashlib.sha256(command).hexdigest()}


def process_birth(pid):
    fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(') ', 1)[1].split()
    return {'pid': pid, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'start_ticks': fields[19], 'ppid': int(fields[1])}


def wait_expected_identity(process, argv, *, timeout=5.0, birth_reader=None,
                           command_reader=None, identity_reader=None, now=None, pause=None):
    """Freeze only the same direct child's stable identity AFTER exact exec argv.

    An empty/inherited/transitional cmdline is not a usable worker identity.
    No signal is sent on timeout, early exit or identity uncertainty. Failure
    carries the original birth and last observations for the launch receipt.
    """
    birth_reader = birth_reader or process_birth
    command_reader = command_reader or (lambda pid: (Path('/proc') / str(pid) / 'cmdline').read_bytes())
    identity_reader = identity_reader or process_identity
    now, pause = now or time.monotonic, pause or time.sleep
    expected = b'\x00'.join(os.fsencode(arg) for arg in argv) + b'\x00'
    expected_sha = hashlib.sha256(expected).hexdigest()
    evidence = {'pid': process.pid, 'expected_parent_pid': os.getpid(), 'expected_argv': list(argv),
                'expected_cmdline_sha256': expected_sha, 'birth': None, 'last_observed_identity': None,
                'last_observed_cmdline_sha256': None, 'attempts': 0}
    deadline = now() + timeout
    def fail(code, message):
        error = APIError(code, message, 503)
        evidence['child_exit_code'] = process.poll()
        error.launch_evidence = deepcopy(evidence)
        raise error
    while True:
        if process.poll() is not None:
            fail('WORKER_EARLY_EXIT', 'Child exited before exact worker exec identity was confirmed')
        evidence['attempts'] += 1
        try:
            birth = birth_reader(process.pid)
            if evidence['birth'] is None:evidence['birth'] = deepcopy(birth)
            if birth != evidence['birth'] or birth.get('ppid') != os.getpid():
                fail('WORKER_IDENTITY_CHANGED', 'Child birth or direct parent changed before identity freeze; preserve process')
            command = command_reader(process.pid)
            evidence['last_observed_cmdline_sha256'] = hashlib.sha256(command).hexdigest()
            identity = identity_reader(process.pid)
            evidence['last_observed_identity'] = deepcopy(identity)
            projection = {key: birth[key] for key in ('pid', 'boot_id', 'start_ticks')}
            if any(identity.get(key) != value for key, value in projection.items()):
                fail('WORKER_IDENTITY_CHANGED', 'Child identity no longer matches original birth; preserve process')
            if command == expected and identity.get('cmdline_sha256') == expected_sha:
                repeated_command = command_reader(process.pid)
                repeated = identity_reader(process.pid)
                repeated_birth = birth_reader(process.pid)
                evidence['last_observed_identity'] = deepcopy(repeated)
                if repeated_birth != birth or any(repeated.get(key) != value for key, value in projection.items()):
                    fail('WORKER_IDENTITY_CHANGED', 'Child birth changed during exact exec confirmation; preserve process')
                if repeated_command == expected and repeated == identity and process.poll() is None:
                    evidence['status'] = 'EXACT_EXEC_IDENTITY_CONFIRMED'
                    return identity, evidence
        except (OSError, ValueError, IndexError) as exc:
            evidence['last_read_error'] = type(exc).__name__
        if now() >= deadline:
            fail('WORKER_EXEC_NOT_CONFIRMED', 'Exact worker argv and stable identity were not confirmed; review launch-attempt receipt')
        pause(.01)


def save_launch_attempt(path, value):
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(json_bytes(value));stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class Worker:
    process: object
    identity: dict
    step: int
    log: str


class ProductionBackend:
    def __init__(self, job=JOB):
        self.job = Path(job)

    def catalog(self):
        manifest = json.loads((ARCHIVE / 'download-manifest.json').read_text(encoding='utf-8'))
        expected = {r['relative']: r['sha256'] for r in manifest['files']}
        rows = []
        for step in STEPS:
            path = ARCHIVE / 'models' / ('update-%08d' % step)
            row = {'step': step, 'checkpoint': str(path.resolve()), 'present': (path / 'params').is_dir(), 'available': False,
                   'normalization_sha256': NORM_SHA, 'tokenizer_sha256': TOKEN_SHA, 'source_commit': COMMIT}
            try:
                for relative in ('candidate.json', 'assets/tiger_piper_right_strict95/norm_stats.json'):
                    file = path / relative
                    digest = hashlib.sha256(file.read_bytes()).hexdigest()
                    require(digest == expected[str(file.relative_to(ARCHIVE))], 'ARCHIVE_MISMATCH', 'Candidate metadata differs from archive')
                    if relative.endswith('norm_stats.json'):
                        require(digest == NORM_SHA, 'ARCHIVE_MISMATCH', 'Candidate normalization differs from frozen FP32 contract')
                candidate = json.loads((path / 'candidate.json').read_text())
                require(candidate['completed_updates'] == step and candidate['parameter_dtype'] == 'float32',
                        'ARCHIVE_MISMATCH', 'Candidate metadata is not the requested FP32 step')
                require((path / 'params').is_dir(), 'ARCHIVE_MISSING', 'Candidate params directory missing')
                row.update(available=True, error=None)
            except Exception as exc:
                row['error'] = type(exc).__name__ + ': candidate files unavailable or identity check failed'
            rows.append(row)
        return rows

    def launch(self, step):
        from gpu_inventory import require_available
        gpu_receipt = self.job / 'receipts' / ('before-worker-%d-%s-GPU-owners.json' % (step, uuid.uuid4().hex))
        require_available(gpu_receipt)
        from launch_supervisor import check_port_free
        check_port_free(WORKER_PORT)
        log = self.job / 'receipts' / ('worker-%d-%s.log' % (step, uuid.uuid4().hex))
        argv = [PYTHON, '-I', '-B', str(self.job / 'scripts' / 'worker_entry.py'), '--step', str(step), '--port', str(WORKER_PORT)]
        with log.open('xb', buffering=0) as stream:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=True, cwd=self.job,
                                       env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'TIGER_SUPERVISOR_SHA256': SUPERVISOR_SHA256})
        attempt_path = log.with_suffix('.launch-attempt.json')
        attempt = {'status': 'WAITING_EXPECTED_EXEC', 'pid': process.pid, 'step': step,
                   'expected_argv': argv, 'log': str(log), 'expected_parent_pid': os.getpid()}
        try:
            save_launch_attempt(attempt_path, attempt)
            identity, evidence = wait_expected_identity(process, argv)
            save_launch_attempt(attempt_path, {**attempt, **evidence, 'identity': identity})
        except Exception as exc:
            attempt.update(getattr(exc, 'launch_evidence', {}))
            attempt.update(status='IDENTITY_NOT_FROZEN_PRESERVE_CHILD', error_code=getattr(exc, 'code', type(exc).__name__),
                           child_exit_code=process.poll(), automatic_signal_sent=False)
            save_launch_attempt(attempt_path, attempt)
            raise
        return Worker(process, identity, step, str(log))

    def alive(self, worker):
        return worker.process.poll() is None

    def _verify(self, worker):
        require(self.alive(worker) and process_identity(worker.identity['pid']) == worker.identity,
                'WORKER_IDENTITY_CHANGED', 'Tracked PID/start ticks/command identity changed; preserve unknown process', 503)

    def _verify_listener(self, worker):
        self._verify(worker)
        inodes = set()
        for fd in (Path('/proc') / str(worker.identity['pid']) / 'fd').iterdir():
            try:
                value = os.readlink(fd)
            except FileNotFoundError:
                continue
            if value.startswith('socket:[') and value.endswith(']'):
                inodes.add(value[8:-1])
        found = False
        for row in Path('/proc/net/tcp').read_text().splitlines()[1:]:
            fields = row.split()
            if fields[1] == '0100007F:%04X' % WORKER_PORT and fields[3] == '0A' and fields[9] in inodes:
                found = True
        require(found, 'WORKER_NOT_LISTENING', 'Tracked worker has not bound its own loopback listener', 503)

    def health(self, worker):
        self._verify_listener(worker)
        return http_json(WORKER_PORT, 'GET', '/health')

    def infer(self, worker, payload):
        self._verify_listener(worker)
        return http_json(WORKER_PORT, 'POST', '/infer', payload, timeout=15.0)

    def infer_rtc(self, worker, payload):
        self._verify_listener(worker)
        return http_json(WORKER_PORT, 'POST', '/infer_rtc', payload, timeout=15.0)

    def stop(self, worker):
        if not self.alive(worker):
            return {'exit_code': worker.process.returncode, 'already_exited': True}
        self._verify(worker)
        os.kill(worker.identity['pid'], signal.SIGTERM)
        forced = False
        try:
            worker.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self._verify(worker)
            os.kill(worker.identity['pid'], signal.SIGKILL)
            worker.process.wait(timeout=10)
            forced = True
        return {'exit_code': worker.process.returncode, 'forced_owned_sigkill': forced}


class Supervisor:
    def __init__(self, backend, *, state_file=None, now=time.monotonic, startup_timeout=120.0, telemetry=None):
        self.backend, self.now = backend, now
        self.state_file = Path(state_file) if state_file is not None else None
        self.startup_timeout = startup_timeout
        self.lock = threading.RLock()
        self.state = 'IDLE'
        self.worker = None
        self.desired_step = None
        self.selected_step = None
        self.selection_id = None
        self.error = None
        self.inflight = False
        self.lease = None
        self.thread = None
        self.closing = False
        self.current_health = None
        self.telemetry = telemetry
        self.rtc_observations = deque(maxlen=4096)
        self.rtc_attempts = deque(maxlen=4096)
        self.completed_leases = {}

    def _expire(self):
        if self.lease is not None and self.now() >= self.lease['expires']:
            lease = self.lease
            self.lease = None
            self._complete_lease(lease, 'EXPIRED')

    def _complete_lease(self, lease, end_status):
        try:
            if self.telemetry is not None and lease.get('gpu_marker') is not None:
                gpu = self.telemetry.finish(lease['gpu_marker'], lease['session_id'])
            else:
                gpu = {'status': 'UNAVAILABLE', 'session_id': lease['session_id'], 'reason': 'Whole-device NVML was not configured'}
        except Exception as exc:
            gpu = {'status': 'FAIL', 'session_id': lease['session_id'],
                   'reason': 'Whole-device evidence save failed: ' + type(exc).__name__}
        self.completed_leases[lease['session_id']] = {'gpu_telemetry': gpu, 'step': lease['step'],
                                                      'trial_index': lease['trial_index'], 'end_status': end_status}
        return gpu

    def _status(self):
        self._expire()
        lease = {'active': False, 'session_id': None, 'step': None, 'expires_in_s': None}
        if self.lease is not None:
            lease = {k: self.lease[k] for k in ('session_id', 'step', 'trial_index', 'trial_seed', 'last_request_index')}
            lease.update(active=True, expires_in_s=max(0., self.lease['expires'] - self.now()))
        worker = None if self.worker is None else {**deepcopy(self.worker.identity), 'step': self.worker.step, 'log': self.worker.log}
        return {'schema': 'tiger-fp32-model-supervisor-v1', 'status': self.state, 'selected_step': self.selected_step,
                'desired_step': self.desired_step, 'selection_id': self.selection_id, 'busy': self.inflight,
                'lease': lease, 'worker': worker, 'error': deepcopy(self.error),
                'gpu_telemetry': self.telemetry.status() if self.telemetry is not None else {'status': 'UNAVAILABLE'}}

    def status(self):
        with self.lock:
            if self.worker is not None and self.state == 'READY' and not self.backend.alive(self.worker):
                self.state, self.selected_step = 'FAILED', None
                self.error = {'code': 'WORKER_EXITED', 'message': 'Tracked worker exited; child log is preserved'}
                self.lease = None
            return self._status()

    def _save(self):
        if self.state_file is None:
            return
        value = self._status()  # Token is intentionally never persisted.
        value['at_utc'] = datetime.now(timezone.utc).isoformat()
        temporary = self.state_file.with_name(self.state_file.name + '.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_file)

    def catalog(self):
        rows = self.backend.catalog()
        health = self.health()
        status = self.status()
        for row in rows:
            row['ready'] = health.get('ready') is True and row.get('step') == status['selected_step']
        return {'schema': 'tiger-fp32-model-catalog-v1', 'steps': list(STEPS), 'candidates': rows,
                'selected_step': status['selected_step']}

    def health(self):
        with self.lock:
            status = self.status()
            if status['status'] != 'READY':
                return {'ready': False, 'local_only': True, 'manager': status}
            worker, step = self.worker, self.selected_step
        try:
            body = health_validated(self.backend.health(worker), step)
        except Exception:
            return {'ready': False, 'local_only': True, 'manager': self.status(),
                    'error': 'Worker health identity or availability check failed'}
        with self.lock:
            if self.worker is not worker or self.state != 'READY':
                return {'ready': False, 'local_only': True, 'manager': self._status()}
            body['manager'] = self._status()
            return body

    def acceptance(self, step):
        step_value(step)
        body = self.health()
        binding = body.get('acceptance_binding') if body.get('ready') is True and body.get('policy', {}).get('checkpoint_step') == step else None
        job = Path(getattr(self.backend, 'job', JOB))
        return acceptance_receipt(job / 'receipts' / ('acceptance-step-%d.json' % step), step, binding)

    def submit_acceptance(self, body, session_id, token):
        """Publish trusted robot metadata; derive budget results from real calls/NVML."""
        with self.lock:
            self._lease_match(session_id, token)
            require(not self.inflight and self.state == 'READY', 'DEPLOYMENT_BUSY', 'Cannot publish during inference or selection')
            step = step_value(body.get('step'))
            require(self.selected_step == step == self.lease['step'], 'MODEL_CHANGED', 'Acceptance step differs from leased model')
            binding = self.current_health['acceptance_binding']
            require(body.get('schema') == 'tiger-fp32-h50-rtc-acceptance-v1' and body.get('identity') == binding['identity']
                    and body.get('semantics') == binding['semantics'] == ACCEPTANCE_SEMANTICS
                    and body.get('contract_fingerprint') == binding['contract_fingerprint'],
                    'ACCEPTANCE_BINDING_MISMATCH', 'Acceptance code/RTC IR/normalization/model binding differs')
            def no_secret(value):
                if isinstance(value, str):
                    return value != self.lease['token']
                if isinstance(value, dict):
                    return all(k.lower().replace('-', '_') not in
                               ('token', 'lease_token', 'leasetoken', 'deployment_lease', 'x_tiger_deployment_lease')
                               and no_secret(v) for k, v in value.items())
                return all(no_secret(v) for v in value) if isinstance(value, list) else True
            require(no_secret(body), 'SECRET_IN_METADATA', 'Lease secrets cannot be persisted in acceptance metadata', 400)
            source = session_value(body.get('source_run_session_id'))
            trial = body.get('source_trial_index')
            require(type(trial) is int and trial >= 0, 'INVALID_TRIAL', 'Explicit original source_trial_index required', 400)
            metrics = body.get('metrics', {})
            require(body.get('results', {}).get('timing_basis') == ACCEPTANCE_SEMANTICS['timing_basis'],
                    'INVALID_TIMING_SEMANTICS', 'Actual latency basis must be validated RAM commit-ready', 400)
            latencies, indices, warmup = (metrics.get(key) for key in ('raw_latencies_ms', 'rtc_call_request_indices', 'warmup_request_indices'))
            require(isinstance(latencies, list) and len(latencies) == 100 and
                    all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in latencies),
                    'INVALID_METRICS', 'Exactly100 actual finite end-to-end latency samples required', 400)
            require(isinstance(indices, list) and len(indices) == 100 and all(type(v) is int and v >= 0 for v in indices)
                    and indices == sorted(set(indices)) and isinstance(warmup, list) and len(warmup) == 3
                    and all(type(v) is int and v >= 0 for v in warmup) and warmup == sorted(set(warmup))
                    and max(warmup) < min(indices), 'INVALID_METRICS', 'Unique ordered100 RTC indices and preceding3 warmup indices required', 400)
            require(warmup == [0,1,2] and indices == list(range(3,103)), 'INVALID_METRICS',
                    'Native bootstrap0, warmups1/2 and contiguous100 RTC calls are required', 400)
            live = health_validated(self.backend.health(self.worker), step)
            require(live['acceptance_binding'] == binding, 'MODEL_CHANGED', 'Live worker binding changed before acceptance publication')
            zero = live.get('zero_weight_equivalence', {})
            zero_ok = (zero.get('pass') is True and zero.get('absolute_tolerance') == 0
                       and zero.get('relative_tolerance') == 0 and zero.get('max_absolute_difference') == 0
                       and zero.get('implementation_sha256') == live['rtc']['implementation_sha256'])
            available = [row for row in self.rtc_observations if row['session_id'] == source and row['trial_index'] == trial
                         and row['step'] == step and row['contract_fingerprint'] == binding['contract_fingerprint']]
            selected = [row for row in available if row['request_index'] in set(indices)]
            initial = [row for row in available if row['request_index'] in set(warmup)]
            attempts = [row for row in self.rtc_attempts if row['session_id'] == source and row['trial_index'] == trial
                        and row['step'] == step and row['contract_fingerprint'] == binding['contract_fingerprint']]
            call_ok = (len(available) == 103 and len(attempts) == 103 and all(row['status'] == 'SUCCESS' for row in attempts)
                       and len(selected) == 100 and len({row['request_index'] for row in selected}) == 100
                       and all(row['rtc_enabled'] is True for row in selected)
                       and len(initial) == 3 and len({row['request_index'] for row in initial}) == 3
                       and all(row['rtc_enabled'] is (row['request_index'] != 0) for row in available))
            bootstrap = [row for row in initial if row['request_index'] == 0]
            bootstrap_wall_ok = (len(bootstrap) == 1 and bootstrap[0]['rtc_enabled'] is False
                                 and type(bootstrap[0].get('worker_wall_ms')) in (int, float)
                                 and math.isfinite(bootstrap[0]['worker_wall_ms'])
                                 and 0 <= bootstrap[0]['worker_wall_ms'] <= ACCEPTANCE_MAX_MS)
            call_ok = call_ok and bootstrap_wall_ok
            wall = {row['request_index']: row['worker_wall_ms'] for row in selected}
            call_ok = call_ok and all(index in wall and latency >= wall[index] for index, latency in zip(indices, latencies))
            gpu = deepcopy(self.completed_leases.get(source, {}).get('gpu_telemetry', {'status': 'UNAVAILABLE'}))
            gpu_ok = (gpu.get('status') == 'PASS' and gpu.get('session_id') == source
                      and self.completed_leases.get(source, {}).get('end_status') == 'RELEASED')
            peak = None
            if gpu_ok:
                try:
                    require(hashlib.sha256(Path(gpu['csv_file']).read_bytes()).hexdigest() == gpu['csv_sha256']
                            and hashlib.sha256(Path(gpu['json_file']).read_bytes()).hexdigest() == gpu['json_sha256'],
                            'TELEMETRY_CHANGED', 'Server GPU evidence changed')
                    peak = max(gpu['lease_peak_mib'], gpu['model_peak_mib'])
                except Exception:
                    gpu_ok = False
            ordered = sorted(latencies)
            position = .95 * (len(ordered) - 1)
            lower = int(position)
            p95 = ordered[lower] + (ordered[min(lower + 1, 99)] - ordered[lower]) * (position - lower)
            maximum = max(latencies)
            robot_ok = (body.get('results', {}).get('robot_chain_audits_passed') is True
                        and body.get('results', {}).get('bootstrap_accepted_before_deadline') is True
                        and body.get('results', {}).get('diagnostic_reference_chain_validated') is True)
            usb_ok = body.get('results', {}).get('usb_finalflush_validated') is True
            reasons = []
            for good, reason in ((call_ok, 'Manager does not prove the exact100 successful enabled RTC calls and3 warmups'),
                                 (zero_ok, 'Current worker exact zero-weight equivalence proof is absent/failed'),
                                 (gpu_ok, 'Original source-run whole-device NVML evidence is unavailable/failed'),
                                 (robot_ok, 'Trusted robot USB/source audits did not pass'),
                                 (usb_ok, 'Final asynchronous USB flush and recorded-file readback audits did not pass'),
                                 (p95 <= ACCEPTANCE_P95_MS and maximum <= ACCEPTANCE_MAX_MS,
                                  'Actual packet-ready-to-validated-RAM-commit-ready latency exceeded budget'),
                                 (gpu_ok and type(peak) in (int, float) and math.isfinite(peak) and peak <= 22528,
                                  'Whole-device sampled peak exceeded22528MiB or is unknown')):
                if not good:
                    reasons.append(reason)
            receipt = deepcopy(body)
            receipt.update(status='FAIL' if reasons else 'PASS', at_utc=datetime.now(timezone.utc).isoformat(),
                           requirements={'warmup_calls': 3, 'measured_calls': 100, 'p95_budget_ms': ACCEPTANCE_P95_MS,
                                         'max_budget_ms': ACCEPTANCE_MAX_MS, 'whole_gpu_budget_mib': 22528},
                           submitted_results=deepcopy(body.get('results')), failure_reasons=reasons,
                           zero_weight_worker_proof=deepcopy(zero), gpu_telemetry=gpu,
                           manager_call_evidence=deepcopy(initial + selected),
                           results={'mode': 'native32_bootstrap_then_rtc', 'warmup_calls': 3, 'measured_calls': 100,
                                    'timing_basis': ACCEPTANCE_SEMANTICS['timing_basis'],
                                    'usb_finalflush_validated': usb_ok,
                                    'vjp_enabled_calls': sum(row['rtc_enabled'] is True for row in selected),
                                    'native_bootstrap_calls': sum(row['rtc_enabled'] is False for row in available),
                                    'rtc_continuation_calls': sum(row['rtc_enabled'] is True for row in available),
                                    'bootstrap_accepted_before_deadline': body.get('results',{}).get('bootstrap_accepted_before_deadline') is True,
                                    'diagnostic_reference_chain_validated': body.get('results',{}).get('diagnostic_reference_chain_validated') is True,
                                    'latency_ms': {'p95': p95, 'max': maximum}, 'whole_gpu_peak_mib': peak,
                                    'all_actions_float32_50x7': call_ok, 'zero_weight_same_rng_noise_equivalent': zero_ok,
                                    'robot_chain_audits_passed': robot_ok})
            directory = Path(getattr(self.backend, 'job', JOB)) / 'receipts'
            target = directory / ('acceptance-step-%d.json' % step)
            if target.exists():
                previous = target.read_bytes()
                previous_history = directory / ('acceptance-step-%d-history-%s.json' % (step, hashlib.sha256(previous).hexdigest()))
                if not previous_history.exists():
                    with previous_history.open('xb') as stream:
                        stream.write(previous)
                        stream.flush()
                        os.fsync(stream.fileno())
            history = directory / ('acceptance-step-%d-%s.json' % (step, uuid.uuid4().hex))
            with history.open('xb') as stream:
                stream.write(json_bytes(receipt))
                stream.flush()
                os.fsync(stream.fileno())
            self._lease_match(session_id, token)
            temporary = directory / ('.acceptance-%s.tmp' % uuid.uuid4().hex)
            try:
                with temporary.open('xb') as stream:
                    stream.write(json_bytes(receipt))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return {'status': receipt['status'], 'step': step, 'receipt': str(target), 'history': str(history),
                    'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'failure_reasons': reasons,
                    'results': receipt['results'], 'contract_fingerprint': binding['contract_fingerprint']}

    def select(self, step):
        step_value(step)
        with self.lock:
            self._expire()
            require(not self.closing and self.state != 'SELECTING', 'SELECTION_BUSY', 'Model selection is already in progress')
            require(not self.inflight and self.lease is None, 'DEPLOYMENT_BUSY', 'Inference or deployment lease is active; model switch rejected')
            if self.state == 'READY' and self.selected_step == step and self.worker is not None and self.backend.alive(self.worker):
                return 200, self._status()
            row = next((r for r in self.backend.catalog() if r.get('step') == step), None)
            require(row is not None and row.get('available') is True, 'CANDIDATE_UNAVAILABLE', 'Requested archived candidate is unavailable', 503)
            self.state, self.desired_step, self.selected_step = 'SELECTING', step, None
            self.current_health = None
            self.selection_id, self.error = uuid.uuid4().hex, None
            self._save()
            self.thread = threading.Thread(target=self._switch, args=(step,), daemon=True, name='owned-fp32-worker-switch')
            self.thread.start()
            return 202, self._status()

    def _switch(self, step):
        try:
            with self.lock:
                previous = self.worker
            if previous is not None:
                self.backend.stop(previous)
                with self.lock:
                    self.worker = None
                    self._save()
            with self.lock:
                require(not self.closing, 'SUPERVISOR_CLOSING', 'Supervisor is closing')
            if self.telemetry is not None:
                self.telemetry.begin_model(step)
            worker = self.backend.launch(step)
            with self.lock:
                self.worker = worker
                self._save()
            deadline = self.now() + self.startup_timeout
            while self.now() < deadline:
                with self.lock:
                    require(not self.closing, 'SUPERVISOR_CLOSING', 'Supervisor is closing')
                require(self.backend.alive(worker), 'WORKER_EARLY_EXIT', 'Worker exited while loading; saved child log retains details', 503)
                try:
                    current = health_validated(self.backend.health(worker), step)
                    with self.lock:
                        self.selected_step, self.state = step, 'READY'
                        self.current_health = current
                        self._save()
                    return
                except (OSError, ValueError, APIError):
                    time.sleep(.1)
            raise APIError('WORKER_STARTUP_TIMEOUT', 'Worker did not expose a verified FP32 health within startup deadline', 503)
        except Exception as exc:
            with self.lock:
                failed_worker = self.worker
            stop_failure = False
            if failed_worker is not None:
                try:
                    self.backend.stop(failed_worker)
                except Exception:
                    stop_failure = True
            with self.lock:
                self.state, self.selected_step = 'FAILED', None
                self.error = {'code': getattr(exc, 'code', 'WORKER_SELECTION_FAILED'),
                              'message': 'Worker selection failed; inspect tracked worker log and saved state',
                              'exception_type': type(exc).__name__, 'owned_stop_failed': stop_failure}
                self._save()

    def acquire(self, session_id, step, trial_index=0):
        session_value(session_id)
        step_value(step)
        require(type(trial_index) is int and 0 <= trial_index <= 1_000_000,
                'INVALID_TRIAL', 'trial_index must be a zero-based integer in0..1000000', 400)
        with self.lock:
            self._expire()
            require(self.state == 'READY' and self.selected_step == step and not self.closing,
                    'MODEL_NOT_READY', 'Requested candidate is not ready', 503)
            require(self.lease is None and not self.inflight, 'DEPLOYMENT_BUSY', 'A lease or inference is already active')
            require(self.worker is not None and self.backend.alive(self.worker), 'MODEL_NOT_READY', 'Worker is not alive', 503)
            token = secrets.token_urlsafe(32)
            self.lease = {'session_id': session_id, 'step': step, 'token': token, 'expires': self.now() + LEASE_TTL,
                          'trial_index': trial_index, 'trial_seed': 42 + trial_index, 'last_request_index': -1,
                          'contract_fingerprint': self.current_health['acceptance_binding']['contract_fingerprint']}
            self.lease['gpu_marker'] = self.telemetry.mark() if self.telemetry is not None else None
            return {'session_id': session_id, 'step': step, 'lease_token': token, 'ttl_s': LEASE_TTL,
                    'heartbeat_interval_s': .5, 'trial_index': trial_index, 'trial_seed': 42 + trial_index,
                    'contract_fingerprint': self.lease['contract_fingerprint']}

    def _lease_match(self, session_id, token):
        self._expire()
        require(self.lease is not None, 'LEASE_EXPIRED', 'Deployment lease expired or absent')
        require(isinstance(session_id, str) and isinstance(token, str) and self.lease['session_id'] == session_id and
                secrets.compare_digest(self.lease['token'], token), 'LEASE_MISMATCH', 'Deployment lease identity does not match', 403)

    def heartbeat(self, session_id, token):
        with self.lock:
            self._lease_match(session_id, token)
            require(self.state == 'READY' and self.selected_step == self.lease['step'] and not self.closing,
                    'MODEL_NOT_READY', 'Leased model is unavailable', 503)
            require(self.worker is not None and self.backend.alive(self.worker),
                    'MODEL_NOT_READY', 'Leased worker exited', 503)
            self.lease['expires'] = self.now() + LEASE_TTL
            return {'ok': True, 'active': True, 'step': self.lease['step'], 'ttl_s': LEASE_TTL}

    def release(self, session_id, token):
        with self.lock:
            self._lease_match(session_id, token)
            require(not self.inflight, 'INFERENCE_BUSY', 'Wait for current inference before releasing the lease')
            gpu = self._complete_lease(self.lease, 'RELEASED')
            self.lease = None
            return {'ok': True, 'active': False, 'gpu_telemetry': gpu}

    def infer(self, payload, session_id=None, token=None, *, rtc=False):
        expected_fields = {'state', 'front_jpeg_base64', 'right_jpeg_base64', 'prompt'} | ({'rtc'} if rtc else set())
        require(isinstance(payload, dict) and set(payload) == expected_fields,
                'INVALID_INPUT', 'Expected original observation fields and an explicit rtc object only on infer_rtc', 400)
        rtc_request = None
        if rtc:
            from rtc_core import validate_rtc
            try:
                rtc_request = validate_rtc(payload['rtc'])
            except ValueError:
                raise APIError('INVALID_RTC', 'RTC request changed approved parameters or lacks a real valid prefix', 400)
        with self.lock:
            self._expire()
            require(self.state == 'READY' and self.worker is not None and not self.closing,
                    'MODEL_NOT_READY', 'No selected ready worker', 503)
            require(not self.inflight, 'INFERENCE_BUSY', 'Only one inference may be in flight')
            using_lease = session_id is not None or token is not None
            require(not rtc or using_lease, 'LEASE_REQUIRED', 'Explicit deployment/measurement lease required for RTC', 403)
            if using_lease:
                self._lease_match(session_id, token)
            else:
                require(self.lease is None, 'DEPLOYMENT_BUSY', 'Unleased shadow inference rejected during deployment')
            if rtc:
                require(rtc_request['trial_index'] == self.lease['trial_index'] and
                        rtc_request['request_index'] > self.lease['last_request_index'],
                        'RTC_SEQUENCE_MISMATCH', 'RTC trial differs or request index repeats/reverses the leased sequence')
                self.lease['last_request_index'] = rtc_request['request_index']
                fingerprint = self.lease['contract_fingerprint']
                observed_attempt = {'session_id': session_id, 'step': self.selected_step,
                    'trial_index': rtc_request['trial_index'], 'request_index': rtc_request['request_index'],
                    'contract_fingerprint': fingerprint, 'status': 'INFLIGHT'}
                self.rtc_attempts.append(observed_attempt)
            worker, step = self.worker, self.selected_step
            self.inflight = True
        try:
            body = (self.backend.infer_rtc if rtc else self.backend.infer)(worker, payload)
            actions = body.get('actions') if isinstance(body, dict) else None
            require(isinstance(body, dict) and body.get('action_dtype') == 'float32' and body.get('action_shape') == [50, 7]
                    and isinstance(actions, list) and len(actions) == 50
                    and all(isinstance(row, list) and len(row) == 7 and
                            all(type(value) in (int, float) and math.isfinite(value) and abs(value) <= 3.4028234663852886e38
                                for value in row) for row in actions)
                    and type(body.get('inference_wall_ms')) in (int, float) and math.isfinite(body['inference_wall_ms'])
                    and body['inference_wall_ms'] >= 0,
                    'INVALID_WORKER_RESPONSE', 'Worker output violated the finite FP32 H50x7 contract', 502)
            with self.lock:
                if using_lease:
                    self._lease_match(session_id, token)
                require(self.worker is worker and self.state == 'READY' and self.selected_step == step,
                        'MODEL_CHANGED', 'Model changed during inference; response rejected')
                if rtc:
                    returned = body.get('rtc', {})
                    require(body.get('contract_fingerprint') == fingerprint and
                            returned.get('schema') == 'tiger-fp32-h50-rtc-v1' and
                            returned.get('execution_label_hz') == 18 and returned.get('native_training_hz') == 30 and
                            returned.get('freeze_steps') == 12 and returned.get('beta') == 5 and
                            returned.get('trial_index') == rtc_request['trial_index'] and
                            returned.get('request_index') == rtc_request['request_index'] and
                            returned.get('trial_seed') == rtc_request['trial_seed'] and
                            returned.get('enabled') is rtc_request['enabled'] and
                            returned.get('previous_valid_steps') == rtc_request['previous_valid_steps'],
                            'RTC_RESPONSE_MISMATCH', 'Worker RTC contract/trial/prefix echo differs from leased request', 502)
                    self.rtc_observations.append({'session_id': session_id, 'step': step,
                        'trial_index': rtc_request['trial_index'], 'request_index': rtc_request['request_index'],
                        'rtc_enabled': rtc_request['enabled'], 'contract_fingerprint': fingerprint,
                        'worker_wall_ms': body.get('inference_wall_ms'),
                        'response_sha256': hashlib.sha256(json_bytes(body)).hexdigest()})
                    observed_attempt['status'] = 'SUCCESS'
            return body
        finally:
            with self.lock:
                if rtc and observed_attempt['status'] == 'INFLIGHT':
                    observed_attempt['status'] = 'FAILED'
                self.inflight = False

    def close(self):
        with self.lock:
            self.closing = True
            if self.lease is not None:
                self._complete_lease(self.lease, 'SUPERVISOR_CLOSING')
            self.lease = None
            thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=35)
            require(not thread.is_alive(), 'SWITCH_STILL_STOPPING', 'Worker switch is still stopping; preserve tracked process')
        with self.lock:
            worker = self.worker
        if worker is not None:
            self.backend.stop(worker)
        if self.telemetry is not None:
            self.telemetry.close()


class SupervisorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, supervisor, port=MANAGER_PORT):
        self.supervisor = supervisor
        super().__init__((HOST, port), Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    def log_message(self, *_):
        pass
    def setup(self):
        super().setup()
        self.connection.settimeout(5)
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    def reply(self, status, body):
        data = json_bytes(body)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(data)
            self.wfile.flush()
            if status >= 400:
                self.connection.shutdown(socket.SHUT_WR)
                deadline = time.monotonic() + .25
                remaining = MAX_BODY
                while remaining:
                    seconds = deadline - time.monotonic()
                    if seconds <= 0:
                        break
                    self.connection.settimeout(seconds)
                    raw = self.rfile.read1(min(65536, remaining))
                    if not raw:
                        break
                    remaining -= len(raw)
        except OSError:
            pass
    def body(self):
        lengths = self.headers.get_all('Content-Length', [])
        require(self.headers.get('Transfer-Encoding') is None and len(lengths) == 1 and
                lengths[0].isascii() and lengths[0].isdecimal(), 'INVALID_BODY', 'One bounded Content-Length required', 400)
        text = lengths[0].lstrip('0') or '0'
        require(len(text) <= len(str(MAX_BODY)) and (len(text) < len(str(MAX_BODY)) or text <= str(MAX_BODY)),
                'BODY_TOO_LARGE', 'Request exceeds 8 MiB', 413)
        length = int(text)
        require(length > 0 and len(self.headers.get_all('Content-Type', [])) == 1 and
                self.headers.get_content_type() == 'application/json',
                'INVALID_BODY', 'A nonempty application/json body is required', 400)
        blocks, remaining = [], length
        deadline = time.monotonic() + 5
        while remaining:
            seconds = deadline - time.monotonic()
            require(seconds > 0, 'BODY_TIMEOUT', 'Request body deadline exceeded', 400)
            self.connection.settimeout(seconds)
            try:
                block = self.rfile.read1(min(65536, remaining))
            except OSError:
                raise APIError('BODY_TIMEOUT', 'Request body read failed or exceeded deadline', 400)
            if not block:
                break
            blocks.append(block)
            remaining -= len(block)
        raw = b''.join(blocks)
        require(len(raw) == length, 'INVALID_BODY', 'Incomplete request body', 400)
        def pairs(items):
            result = {}
            for key, value in items:
                require(key not in result, 'INVALID_BODY', 'Duplicate JSON field', 400)
                result[key] = value
            return result
        try:
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
        except (UnicodeError, ValueError, RecursionError):
            raise APIError('INVALID_BODY', 'Invalid finite UTF-8 JSON body', 400)
        require(isinstance(value, dict), 'INVALID_BODY', 'JSON object required', 400)
        return value
    def dispatch(self, method):
        try:
            api, path = self.server.supervisor, self.path
            if method == 'GET':
                if path == '/catalog':
                    return self.reply(200, api.catalog())
                if path == '/status':
                    return self.reply(200, api.status())
                if path == '/telemetry':
                    return self.reply(200, api.telemetry.status() if api.telemetry is not None else {'status': 'UNAVAILABLE'})
                if path == '/health':
                    return self.reply(200, api.health())
                parsed = urlparse(path)
                if parsed.path == '/acceptance':
                    query = parse_qs(parsed.query)
                    require(set(query) == {'step'} and len(query['step']) == 1 and len(query['step'][0]) <= 5 and query['step'][0].isascii()
                            and query['step'][0].isdecimal(), 'INVALID_STEP', 'One explicit acceptance step required', 400)
                    return self.reply(200, api.acceptance(step_value(int(query['step'][0]))))
                raise APIError('UNKNOWN_ENDPOINT', 'Unknown endpoint', 404)
            body = self.body()
            if path == '/select':
                require(set(body) == {'step'}, 'INVALID_INPUT', 'Only step is accepted', 400)
                status, result = api.select(body['step'])
                return self.reply(status, result)
            if path == '/acceptance/submit':
                return self.reply(200, api.submit_acceptance(body, self.headers.get('X-Tiger-Deployment-Session'),
                                                           self.headers.get('X-Tiger-Deployment-Lease')))
            if path == '/lease/acquire':
                require(set(body) in ({'session_id', 'step'}, {'session_id', 'step', 'trial_index'}),
                        'INVALID_INPUT', 'session_id, step and optional zero-based trial_index required', 400)
                return self.reply(200, api.acquire(body['session_id'], body['step'], body.get('trial_index', 0)))
            if path in ('/lease/heartbeat', '/lease/release'):
                require(set(body) == {'session_id', 'lease_token'}, 'INVALID_INPUT', 'session_id and lease_token required', 400)
                result = (api.heartbeat if path.endswith('heartbeat') else api.release)(body['session_id'], body['lease_token'])
                return self.reply(200, result)
            if path in ('/infer', '/infer_rtc'):
                return self.reply(200, api.infer(body, self.headers.get('X-Tiger-Deployment-Session'),
                                                self.headers.get('X-Tiger-Deployment-Lease'), rtc=path == '/infer_rtc'))
            raise APIError('UNKNOWN_ENDPOINT', 'Unknown endpoint', 404)
        except APIError as exc:
            self.reply(exc.status, {'error': {'code': exc.code, 'message': str(exc)}})
        except Exception as exc:
            self.reply(502, {'error': {'code': 'SUPERVISOR_FAILURE', 'message': 'Model supervisor operation failed',
                                     'exception_type': type(exc).__name__}})
    def do_GET(self):
        self.dispatch('GET')
    def do_POST(self):
        self.dispatch('POST')
    def unsupported(self):
        self.reply(405, {'error': {'code': 'METHOD_NOT_ALLOWED', 'message': 'Only the documented GET and POST endpoints are supported'}})
    do_PUT = unsupported
    do_PATCH = unsupported
    do_DELETE = unsupported
    do_CONNECT = unsupported
    do_OPTIONS = unsupported
    do_TRACE = unsupported


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--initial-step', type=int, choices=STEPS)
    args = parser.parse_args()
    import fcntl
    require(socket.gethostname() == 'user-MS-7E06', 'WRONG_HOST', 'Wrong host')
    mount = json.loads(subprocess.check_output(['findmnt', '-J', '-T', str(JOB), '-o', 'TARGET,UUID'], text=True))['filesystems'][0]
    require(mount['target'] == '/data/4090-1tb' and mount['uuid'] == 'e76043b6-4ab6-42a5-8b06-4963ee92a567',
            'WRONG_STORAGE', 'Wrong supervisor data filesystem')
    lock = (JOB / 'manager.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    from gpu_inventory import require_available
    require_available(JOB / 'receipts' / ('before-manager-main-%s-GPU-owners.json' % uuid.uuid4().hex))
    state_file = JOB / 'receipts' / 'supervisor-state.json'
    if state_file.exists():
        old = json.loads(state_file.read_text())
        worker = old.get('worker')
        require(worker is None or not (Path('/proc') / str(worker.get('pid'))).exists(),
                'OLD_WORKER_REQUIRES_REVIEW', 'Previous tracked worker PID still exists; review its bound identity before restart')
    from gpu_telemetry import Telemetry
    telemetry = Telemetry(JOB / 'receipts').start()
    api = Supervisor(ProductionBackend(), state_file=state_file, telemetry=telemetry)
    server = SupervisorHTTPServer(api)  # Existing service must be explicitly stopped first.
    (JOB / 'receipts' / 'manager-instance.json').write_text(json.dumps(process_identity(os.getpid()), indent=2) + '\n')
    if args.initial_step is not None:
        api.select(args.initial_step)
    def stop(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        server.server_close()
        api.close()


if __name__ == '__main__':
    main()
