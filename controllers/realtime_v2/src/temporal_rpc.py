"""CPU-only official TimeQP boundary. No model/Hub/actuator operations.

The 4090 worker owns only an immutable mathematical transformation. The client
retains RTC conditions, observation clock, queue revision/cut and publication.
Existing H50 model wire fields and endpoints are untouched.
"""
from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import math
from pathlib import Path
import socket
import threading
import time
import uuid

import numpy as np

try:
    from .manual_timeqp import TARGET_CONTRACT, validate_target_scale, local_scale_stats
except ImportError:
    from manual_timeqp import TARGET_CONTRACT, validate_target_scale, local_scale_stats

ROOT = Path(__file__).resolve().parents[1]
HOST, PORT = '127.0.0.1', 18970
SCHEMA = 'tiger-v2-timeqp-cpu-v3-seven-gears'
OFFICIAL_SHA = '63028ebfecb8093a22f5c4b78a7d2f8ebd64850eed31c518cb2ea89b7ecdcb81'
MAX_BYTES = 64 * 1024


class TemporalRPCError(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise TemporalRPCError(message)


def target_scale(value):
    try:
        return validate_target_scale(value)
    except ValueError as exc:
        raise TemporalRPCError(str(exc)) from exc


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(raw):
    require(0 < len(raw) <= MAX_BYTES, 'TimeQP JSON size outside bound')
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, 'Duplicate JSON field')
            out[key] = value
        return out
    def invalid(_):
        raise TemporalRPCError('Nonfinite JSON value')
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    require(isinstance(value, dict), 'TimeQP JSON object required')
    return value


def config(kind):
    require(kind in ('native51', 'rolling38', 'rolling35'), 'Only native51/rolling38/rolling35 are supported')
    n = 51 if kind == 'native51' else 50
    return dict(dt_ref=1/18, dt_min=(1/18)*(.010/.018), dt_max=(1/18)*(.020/.018),
                lambda_acc=10., lambda_time=1., stride=n, optim_dims=list(range(7)),
                v_max=None, lambda_v=10., horizon=n, logging=False)


def matrix(value, kind):
    require(kind in ('native51', 'rolling38', 'rolling35'), 'Unsupported TimeQP matrix kind')
    n = {'native51':51, 'rolling38':38, 'rolling35':35}[kind]
    require(isinstance(value, list) and len(value) == n and all(
        isinstance(row, list) and len(row) == 7 and all(
            type(x) in (int, float) and math.isfinite(x) and abs(x) <= np.finfo(np.float32).max
            for x in row) for row in value), 'Finite exact TimeQP waypoint shape required')
    return np.asarray(value, dtype=np.float32)


def load_original():
    path = ROOT/'vendor/frozen/optimizer.py'
    require(hashlib.sha256(path.read_bytes()).hexdigest() == OFFICIAL_SHA, 'Official TimeQP source SHA changed')
    spec = importlib.util.spec_from_file_location('isolated_timeqp_original', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TimeParameterizationMPC


@lru_cache(maxsize=1)
def implementation_sha256():
    return digest({name: hashlib.sha256((ROOT/'src'/name).read_bytes()).hexdigest()
                   for name in ('temporal_rpc.py', 'cached_timeqp.py', 'manual_timeqp.py')})


class TimeQPService:
    """One in-flight CPU solve; official equations and fresh default OSQP setup."""
    def __init__(self):
        from .cached_timeqp import CachedTimeQP
        self.optimizer_class = load_original()
        self.optimizers = {kind: CachedTimeQP(self.optimizer_class, config(kind), reuse_workspace=False)
                           for kind in ('native51','rolling38','rolling35')}
        self.manual_optimizers = OrderedDict()
        self.implementation = implementation_sha256()
        self.instance_id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.closing = False

    def health(self):
        return {'schema': SCHEMA, 'ready': not self.closing, 'instance_id': self.instance_id,
                'official_sha256': OFFICIAL_SHA,
                'implementation_sha256': self.implementation,
                'implementation_mode': 'SPARSE_CSC_FRESH_OSQP_DEFAULT_SETTINGS',
                'osqp_workspace_reuse': False, 'warm_iterate_reuse': False,
                'target_contract': dict(TARGET_CONTRACT), 'target_contract_sha256': digest(TARGET_CONTRACT),
                'config_sha256': {kind: digest(config(kind)) for kind in ('native51', 'rolling38', 'rolling35')},
                'model_calls': 0, 'hardware_calls': 0, 'role': 'STATELESS_CPU_TIMEQP_ONLY'}

    def solve(self, request):
        fields = {'schema', 'instance_id', 'request_id', 'kind', 'config_sha256',
                  'waypoints', 'input_sha256', 'target_scale', 'target_contract_sha256'}
        require(isinstance(request, dict) and set(request) == fields, 'Unexpected TimeQP request fields')
        require(request['schema'] == SCHEMA and request['instance_id'] == self.instance_id,
                'TimeQP schema/server instance mismatch')
        rid = request['request_id']
        require(isinstance(rid, str) and len(rid) == 32 and all(c in '0123456789abcdef' for c in rid),
                'TimeQP UUID request identity required')
        kind = request['kind']; cfg = config(kind)
        scale = target_scale(request['target_scale'])
        require(request['target_contract_sha256'] == digest(TARGET_CONTRACT), 'TimeQP target contract differs')
        require(request['config_sha256'] == digest(cfg), 'Frozen TimeQP configuration differs')
        waypoints = matrix(request['waypoints'], kind)
        require(request['input_sha256'] == digest(request['waypoints']), 'Waypoint SHA mismatch')
        require(not self.closing, 'TimeQP worker is closing')
        require(self.lock.acquire(blocking=False), 'Only one TimeQP solve may be in flight')
        try:
            require(not self.closing, 'TimeQP worker is closing')
            if scale == 1.0:
                optimizer = self.optimizers[kind]
            else:
                # Arbitrary user inputs cannot grow this cache without bound.
                # No workspace can inherit a different target's q vector.
                from .cached_timeqp import CachedTimeQP
                key = (kind, scale)
                if key not in self.manual_optimizers:
                    self.manual_optimizers[key] = CachedTimeQP(
                        self.optimizer_class, cfg, reuse_workspace=False, target_scale=scale)
                self.manual_optimizers.move_to_end(key)
                while len(self.manual_optimizers) > 12:
                    self.manual_optimizers.popitem(last=False)
                optimizer = self.manual_optimizers[key]
            began = time.perf_counter_ns()
            output = matrix(optimizer.optimize(waypoints.tolist()), kind)
            compute_ms = (time.perf_counter_ns()-began)/1e6
            require(optimizer.last_map is not None, 'Official source time map missing')
            source_times, dt_traj = optimizer.last_map
            n = len(output)
            require(source_times.shape == (n,) and dt_traj.shape == (n,)
                    and np.isfinite(source_times).all() and np.isfinite(dt_traj).all()
                    and source_times[0] == 0 and np.all(np.diff(source_times) > 0)
                    and np.all(dt_traj > 0), 'Invalid official source time map')
            response = {'schema': SCHEMA, 'instance_id': self.instance_id,
                'request_id': rid, 'request_sha256': digest(request), 'kind': kind,
                'input_sha256': request['input_sha256'], 'config_sha256': digest(cfg),
                'target_scale': scale, 'target_contract_sha256': digest(TARGET_CONTRACT),
                'local_scale_stats': local_scale_stats(source_times, cfg['dt_ref']),
                'official_sha256': OFFICIAL_SHA, 'output': output.tolist(),
                'implementation_sha256': self.implementation,
                'source_times': source_times.tolist(), 'dt_traj': dt_traj.tolist(),
                'server_compute_ms': compute_ms, 'server_qp_timing': dict(optimizer.last_timing),
                'model_calls': 0, 'hardware_calls': 0}
            response['output_sha256'] = digest({k: response[k] for k in ('output', 'source_times', 'dt_traj')})
            require(len(canonical(response)) <= MAX_BYTES, 'TimeQP response exceeds byte limit')
            return response
        finally:
            self.lock.release()


class TimeQPHTTP:
    """Fixed localhost endpoint; whole-call watchdog owns its own socket FD."""
    def __init__(self, timeout_s=.25):
        require(type(timeout_s) in (int, float) and 0 < timeout_s <= 1., 'TimeQP timeout must be within 1 second')
        self.timeout_s = timeout_s

    def request(self, method, path, value=None):
        require((method, path) in (('GET', '/health'), ('POST', '/timeqp')), 'TimeQP route not permitted')
        body = None if value is None else canonical(value)
        require(body is None or len(body) <= MAX_BYTES, 'TimeQP request exceeds byte limit')
        conn = http.client.HTTPConnection(HOST, PORT, timeout=self.timeout_s)
        duplicate = response = timer = None
        socket_lock = threading.Lock()
        expired = threading.Event()
        deadline = time.monotonic()+self.timeout_s
        def expire():
            expired.set()
            with socket_lock:
                if duplicate is not None:
                    try:
                        duplicate.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        try:
            conn.connect(); duplicate = conn.sock.dup()
            left = deadline-time.monotonic()
            require(left > 0, 'TimeQP connect exceeded deadline')
            timer = threading.Timer(left, expire); timer.daemon = True; timer.start()
            headers = {'Accept': 'application/json', 'Connection': 'close'}
            if body is not None:
                headers.update({'Content-Type': 'application/json', 'Content-Length': str(len(body))})
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            length = response.getheader('Content-Length', '')
            require(length.isdecimal() and 0 < int(length) <= MAX_BYTES
                    and not response.getheader('Transfer-Encoding'), 'Bounded TimeQP response required')
            require(response.getheader('Content-Type', '').split(';')[0] == 'application/json', 'TimeQP JSON required')
            left_bytes, raw = int(length), bytearray()
            while left_bytes:
                require(not expired.is_set() and time.monotonic() < deadline, 'TimeQP whole-call timeout')
                chunk = response.read1(min(8192, left_bytes))
                require(bool(chunk), 'Truncated TimeQP response')
                raw.extend(chunk); left_bytes -= len(chunk)
            require(not expired.is_set() and time.monotonic() <= deadline, 'TimeQP whole-call timeout')
            decoded = strict_json(raw)
            require(response.status == 200, 'TimeQP rejected request; HTTP '+str(response.status))
            return decoded
        finally:
            if timer is not None:
                timer.cancel()
            with socket_lock:
                if duplicate is not None:
                    duplicate.close()
            if response is not None:
                response.close()
            conn.close()


class RemoteTimeParameterizationMPC:
    """Drop-in optimize/solve facade; no local solve or fallback exists here.

    NativeBootstrap wraps .solve to observe source_times. Preserve that API.
    A transport injection is for explicit CPU fixtures only; production uses
    127.0.0.1:18970 through the separately owned SSH forwarding channel.
    """
    def __init__(self, dt_ref=1/18, dt_min=(1/18)*(.010/.018), dt_max=(1/18)*(.020/.018),
                 lambda_acc=10., lambda_time=1., stride=50, optim_dims=None, v_max=None,
                 lambda_v=10., horizon=50, logging=False, *, transport=None,
                 expected_instance_id=None, timeout_s=.25, target_scale=1.0):
        self.kind = 'native51' if horizon == 51 else 'rolling38'
        got = dict(dt_ref=dt_ref, dt_min=dt_min, dt_max=dt_max, lambda_acc=lambda_acc,
            lambda_time=lambda_time, stride=stride, optim_dims=list(range(7)) if optim_dims is None else optim_dims,
            v_max=v_max, lambda_v=lambda_v, horizon=horizon, logging=logging)
        require(got == config(self.kind), 'Only frozen 18Hz TimeQP settings are accepted')
        self.H = horizon
        self.optim_dims = list(range(7))
        self.transport = transport or TimeQPHTTP(timeout_s)
        self.expected_instance_id = expected_instance_id
        self.last_receipt = None
        self._target_lock = threading.Lock()
        self._solve_lock = threading.Lock()
        self.set_target_scale(target_scale)

    def set_target_scale(self, value):
        """Set the next solve's preference; an in-flight solve stays frozen.

        The runtime must freeze this at its packet boundary, including RTC
        context. This setter alone does not admit or rewrite a runtime queue.
        """
        value = target_scale(value)
        with self._target_lock:
            self._target_scale = value

    @property
    def target_scale(self):
        with self._target_lock:
            return self._target_scale

    def solve(self, waypoints, st_roll, end_roll):
        require(self._solve_lock.acquire(blocking=False), 'One proxy cannot run concurrent TimeQP solves')
        try:
            # Freeze before any health request or network wait. A concurrent
            # human update is reserved for the next packet/solve.
            scale = self.target_scale
            return self._solve_frozen(waypoints, st_roll, end_roll, scale)
        finally:
            self._solve_lock.release()

    def _solve_frozen(self, waypoints, st_roll, end_roll, scale):
        self.last_receipt = None
        require(st_roll == 0 and end_roll == self.H, 'Only existing whole-window solve API is supported')
        if self.H == 50:
            self.kind = 'rolling35' if len(waypoints) == 35 else 'rolling38'
        points = matrix(np.asarray(waypoints, dtype=np.float32).tolist(), self.kind).tolist()
        health = self.transport.request('GET', '/health')
        require(health.get('schema') == SCHEMA and health.get('ready') is True
                and health.get('official_sha256') == OFFICIAL_SHA
                and health.get('implementation_sha256') == implementation_sha256()
                and health.get('target_contract_sha256') == digest(TARGET_CONTRACT)
                and health.get('target_contract') == TARGET_CONTRACT
                and health.get('config_sha256', {}).get(self.kind) == digest(config(self.kind)),
                'TimeQP service source/config unavailable or different')
        instance = health.get('instance_id')
        require(isinstance(instance, str) and len(instance) == 32, 'TimeQP instance identity missing')
        if self.expected_instance_id is None:
            self.expected_instance_id = instance
        require(instance == self.expected_instance_id, 'TimeQP service restarted; reject old run')
        request = {'schema': SCHEMA, 'instance_id': instance, 'request_id': uuid.uuid4().hex,
            'kind': self.kind, 'config_sha256': digest(config(self.kind)), 'waypoints': points,
            'target_scale': scale, 'target_contract_sha256': digest(TARGET_CONTRACT),
            'input_sha256': digest(points)}
        began = time.perf_counter_ns()
        response = self.transport.request('POST', '/timeqp', request)
        elapsed_ms = (time.perf_counter_ns()-began)/1e6
        for key in ('schema', 'instance_id', 'request_id', 'kind', 'config_sha256', 'input_sha256',
                    'target_scale', 'target_contract_sha256'):
            require(response.get(key) == request[key], 'TimeQP response binding mismatch: '+key)
        require(type(response.get('target_scale')) in (int, float), 'TimeQP target scale type mismatch')
        require(response.get('request_sha256') == digest(request) and response.get('official_sha256') == OFFICIAL_SHA,
                'TimeQP request/source digest mismatch')
        require(response.get('implementation_sha256') == health.get('implementation_sha256'),
                'TimeQP implementation fingerprint mismatch')
        output = matrix(response.get('output'), self.kind)
        source_times = np.asarray(response.get('source_times'), dtype=np.float64)
        dt_traj = np.asarray(response.get('dt_traj'), dtype=np.float64)
        n = len(points)
        require(source_times.shape == (n,) and dt_traj.shape == (n,)
                and np.isfinite(source_times).all() and np.isfinite(dt_traj).all()
                and source_times[0] == 0 and np.all(np.diff(source_times) > 0) and np.all(dt_traj > 0),
                'Invalid TimeQP time map')
        require(response.get('output_sha256') == digest({k: response[k] for k in ('output', 'source_times', 'dt_traj')}),
                'TimeQP output digest mismatch')
        stats = local_scale_stats(source_times)
        require(response.get('local_scale_stats') == stats, 'TimeQP local scale statistics differ from time map')
        require(type(response.get('server_compute_ms')) in (int, float)
                and math.isfinite(response['server_compute_ms']) and response['server_compute_ms'] >= 0,
                'TimeQP compute timing missing')
        self.last_receipt = {k: response[k] for k in ('schema', 'instance_id', 'request_id', 'request_sha256',
            'kind', 'input_sha256', 'config_sha256', 'official_sha256', 'output_sha256', 'server_compute_ms',
            'implementation_sha256', 'server_qp_timing', 'target_scale', 'target_contract_sha256',
            'local_scale_stats')}
        self.last_receipt.update(client_post_round_trip_ms=elapsed_ms, no_local_fallback=True,
                                 model_interface_changed=False)
        return output, source_times, dt_traj

    def optimize(self, action_list):
        kind = ('rolling35' if isinstance(action_list, list) and len(action_list) == 35 else 'rolling38') if self.H == 50 else 'native51'
        points = matrix(action_list, kind)
        output, _, _ = self.solve(points[:, self.optim_dims], 0, self.H)
        return output.astype(np.float32).tolist()


def make_server(service, *, port=PORT, test_fixture=False):
    require(port == PORT or (test_fixture is True and port == 0), 'Production TimeQP port is fixed18970')
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        def log_message(self, *_):
            pass
        def setup(self):
            super().setup(); self.connection.settimeout(1.)
        def reply(self, status, value):
            raw = canonical(value)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Connection', 'close'); self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def do_GET(self):
            self.reply(200, service.health()) if self.path == '/health' else self.reply(404, {'error': 'route_not_allowed'})
        def do_POST(self):
            if self.path != '/timeqp':
                self.reply(404, {'error': 'route_not_allowed'}); return
            try:
                lengths = self.headers.get_all('Content-Length', [])
                require(len(lengths) == 1 and lengths[0].isdecimal() and 0 < int(lengths[0]) <= MAX_BYTES,
                        'Bounded content length required')
                require(not self.headers.get('Transfer-Encoding') and self.headers.get_content_type() == 'application/json',
                        'Nonchunked JSON required')
                count = int(lengths[0]); raw = bytearray(); deadline = time.monotonic()+1.
                while len(raw) < count:
                    remaining = deadline-time.monotonic()
                    require(remaining > 0, 'TimeQP body timeout')
                    self.connection.settimeout(remaining)
                    part = self.rfile.read1(min(8192, count-len(raw)))
                    require(bool(part), 'Truncated TimeQP request'); raw.extend(part)
                self.reply(200, service.solve(strict_json(raw)))
            except Exception as exc:
                self.reply(400, {'error': type(exc).__name__, 'detail': str(exc)[:180]})
        def unsupported(self):
            self.reply(405, {'error': 'method_not_allowed'})
        do_DELETE = do_PUT = do_PATCH = do_CONNECT = do_TRACE = do_OPTIONS = do_HEAD = unsupported
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    return server
