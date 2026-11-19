"""Private worker HTTP: native /infer plus separately audited /infer_rtc."""
from copy import deepcopy
import hashlib
from pathlib import Path
import signal
import socket
import threading
import time

import fp32_http as native_http
from model_supervisor import Handler as JSONBodyReader, APIError, COMMIT, NORM_SHA, TOKEN_SHA, ACCEPTANCE_SEMANTICS
from rtc_core import canonical_sha

LOADER_SHA = 'a1024dc360b3e79bcfde21a33f2badbf5bbdd114d2a8379dbd943a999e882ba4'


def acceptance_binding(policy):
    identity = {
        'normalization_sha256': policy.metadata['normalization_sha256'],
        'tokenizer_sha256': policy.metadata['tokenizer_sha256'], 'source_commit': policy.metadata['source_commit'],
        'rtc_implementation_sha256': policy.rtc['implementation_sha256'],
        'rtc_precision_audit_sha256': policy.rtc['precision_audit_sha256'],
        **policy.source_identity,
    }
    native = native_http._health(policy)
    fingerprint = canonical_sha({'step': policy.metadata['checkpoint_step'], 'identity': identity,
                                 'contract': native['contract'], 'policy': native['policy'], 'rtc': policy.rtc,
                                 'semantics': ACCEPTANCE_SEMANTICS})
    return {'contract_fingerprint': fingerprint, 'identity': identity, 'semantics': deepcopy(ACCEPTANCE_SEMANTICS)}


def health(policy):
    value = native_http._health(policy)
    value.update(rtc=deepcopy(policy.rtc), acceptance_binding=acceptance_binding(policy),
                 zero_weight_equivalence=deepcopy(policy.zero_weight_equivalence))
    value['contract_fingerprint'] = value['acceptance_binding']['contract_fingerprint']
    return value


class RTCHandler(native_http.FP32RequestHandler):
    def setup(self):
        super().setup()  # Preserve frozen framing, buffered streams and15s timeout.
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def do_GET(self):
        if self.path == '/health':
            return self._reply(200, health(self.server.policy))
        return super().do_GET()

    def do_POST(self):
        if self.path != '/infer_rtc':
            return super().do_POST()
        try:
            body = JSONBodyReader.body(self)
            if set(body) != {'state', 'front_jpeg_base64', 'right_jpeg_base64', 'prompt', 'rtc'}:
                raise ValueError('Original four observation fields plus explicit rtc required')
            rtc = body.pop('rtc')
            observation = native_http.decode_observation(body)
            observation['rtc'] = rtc
            from rtc_core import validate_rtc
            validate_rtc(rtc)
        except (APIError, native_http.RequestError, ValueError) as exc:
            return self._reply(getattr(exc, 'status', 400), {'error': str(exc)})
        started = time.perf_counter()
        try:
            result = self.server.policy.infer_rtc(observation)
            response = native_http.encode_result(result, (time.perf_counter() - started) * 1000)
            response['rtc'] = result['rtc']
            response['contract_fingerprint'] = acceptance_binding(self.server.policy)['contract_fingerprint']
            return self._reply(200, response)
        except Exception as exc:
            return self._reply(500, {'error': 'RTC inference failed; no precision fallback or action dispatch',
                                     'exception_type': type(exc).__name__})


def serve(policy, port=18884, ready_file=None):
    server = native_http.FP32HTTPServer(policy, port)
    server.RequestHandlerClass = RTCHandler
    status = health(policy)
    status.update(host='127.0.0.1', port=server.server_address[1])
    ready_path = Path(ready_file) if ready_file is not None else None
    previous = None
    def stop(signum, frame):
        raise KeyboardInterrupt
    try:
        if ready_path is not None:
            native_http._write_ready(ready_path, status)
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, stop)
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        if ready_path is not None:
            native_http._write_ready(ready_path, {**status, 'ready': False})
