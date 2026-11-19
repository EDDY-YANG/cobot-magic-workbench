"""One whole-frame StartLux presence candidate for explicitly offline replay.

No camera, robot, SSH, disk writes, action authority, labels or temporal state.
The existing loopback model service is inspected once on construction. Requests
are serialized per instance; that is not a system-wide GPU ownership lock.
"""
from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.request import Request

from .presence_detector import _threshold
from .semantic_observer import StartLuxHTTPClient, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES


EXPECTED_MODEL = 'StartLux-Decision-v1.1-4B-Instant-Q8_0'
PROMPT_SHA256 = '178ea049314c0a8f35d41fa8013d9fcb9f479a1e3f1022b8a4d7132ec2db01a4'
DEFAULT_PROMPT_PATH = (Path(__file__).resolve().parents[2] / '2026-10-18-10-02-人员暂停离线验证'
                       / '2026-10-18-10-02-startlux-presence-frozen-prompt.json')
ENDPOINT = 'http://127.0.0.1:8090/v1/decisions'


def _load_prompt(path):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != PROMPT_SHA256:
        raise ValueError('Frozen StartLux presence prompt SHA-256 mismatch')
    value = json.loads(raw)
    if value.get('expected_model') != EXPECTED_MODEL or value.get('endpoint') != ENDPOINT:
        raise ValueError('Frozen loopback model identity differs')
    if set(value.get('questions', {})) != {'human_presence'} or set(
            value['questions']['human_presence'].get('criteria', {})) != {'present', 'absent', 'uncertain'}:
        raise ValueError('Frozen presence question contract differs')
    return value


class StartLuxPresenceDetector:
    """VLM classification without invented localization boxes.

    Winning probability >= .9 and None probability < .2 are engineering replay
    acceptance thresholds, not calibrated protective limits. Everything else
    is invalid/uncertain and must not count as a clear observation.
    """
    def __init__(self, prompt_path=DEFAULT_PROMPT_PATH, *, timeout_s=10.,
                 min_winning_probability=.9, max_none_probability=.2, verify_service=True):
        self.min_winning_probability = _threshold(min_winning_probability, 'min_winning_probability')
        self.max_none_probability = _threshold(max_none_probability, 'max_none_probability')
        if type(verify_service) is not bool:
            raise ValueError('verify_service must be an explicit boolean')
        self._config = _load_prompt(prompt_path)
        self._client = StartLuxHTTPClient(endpoint=ENDPOINT, expected_model=EXPECTED_MODEL, timeout_s=timeout_s)
        self._client.questions = deepcopy(self._config['questions'])
        self._lock = threading.Lock()
        import cv2
        import numpy
        self.cv, self.np = cv2, numpy
        self.profile = 'startlux_single_image'
        self.model_info = {
            'profile': self.profile, 'model': EXPECTED_MODEL, 'expected_model': EXPECTED_MODEL,
            'backend': 'existing loopback StartLux HTTP / llama.cpp', 'endpoint': ENDPOINT,
            'opencv': cv2.__version__, 'numpy': numpy.__version__,
            'prompt_path': str(Path(prompt_path)), 'prompt_sha256': PROMPT_SHA256,
            'input': 'one whole current image, PNG preserving source decoded pixels; no history/joints/labels/IDs',
            'outputs': ['present', 'absent', 'uncertain'], 'localization_supported': False,
            'min_winning_probability': self.min_winning_probability, 'max_none_probability': self.max_none_probability,
            'threshold_status': 'engineering replay parameters; not calibrated protective limits',
            'roi': 'entire source frame', 'safety_certified': False, 'motion_authority': False,
            'person_palm_timing_fields': 'zero means these local detectors were not executed',
            'service_verified_on_construction': False,
        }
        if verify_service:
            self.model_info['service_snapshot'] = self._inspect_idle_service()
            self.model_info['service_verified_on_construction'] = True

    def _json_request(self, request):
        with self._client._opener.open(request, timeout=self._client.timeout_s) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError('StartLux JSON response exceeds bounded size')
        return json.loads(raw.decode('utf-8'))

    def _inspect_idle_service(self):
        health = self._json_request(Request('http://127.0.0.1:8090/health', method='GET'))
        if (not isinstance(health, dict) or health.get('model') != EXPECTED_MODEL or health.get('status') != 'ok'
                or health.get('kind') != 'instant' or health.get('images') is not True
                or health.get('prompt_attention') != 'bidirectional'):
            raise ValueError('Expected image-capable bidirectional Instant model health was not verified')
        slots = self._json_request(Request('http://127.0.0.1:8091/slots', method='GET'))
        if not isinstance(slots, list) or not slots or any(slot.get('is_processing') is not False for slot in slots):
            raise ValueError('Existing model slot is busy or idle state unverified')
        return {'health': health, 'slots': [{'id': slot.get('id'), 'is_processing': slot['is_processing']} for slot in slots]}

    def infer(self, image):
        started = time.perf_counter()
        result = {'valid': False, 'error': None, 'present': None, 'semantic_presence': 'uncertain',
                  'persons': [], 'hands': [], 'localization_supported': False,
                  'source': 'offline_image', 'profile': self.profile, 'safety_certified': False,
                  'motion_authority': False, 'raw_response': None, 'validated': None,
                  'timing_ms': {'person_ms': 0., 'palm_ms': 0., 'encode_ms': 0., 'client_ms': 0.}}
        try:
            np = self.np
            if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3
                    or image.shape[2] != 3 or min(image.shape[:2]) < 2 or max(image.shape[:2]) > 16384):
                raise ValueError('Expected uint8 HxWx3 BGR source image')
            result['source_image_size'] = [int(image.shape[1]), int(image.shape[0])]
            with self._lock:
                tick = time.perf_counter()
                ok, encoded = self.cv.imencode('.png', image)
                if not ok:
                    raise ValueError('Whole source frame lossless PNG encoding failed')
                encoded = encoded.tobytes()
                result['timing_ms']['encode_ms'] = (time.perf_counter() - tick) * 1000
                result['request_input'] = {'image_count': 1, 'input_png_bytes': len(encoded),
                                           'input_png_sha256': hashlib.sha256(encoded).hexdigest(),
                                           'prompt_sha256': PROMPT_SHA256}
                payload = {'state': self._config['state'], 'questions': deepcopy(self._config['questions']),
                           'images': ['data:image/png;base64,' + base64.b64encode(encoded).decode('ascii')],
                           **self._config['controls']}
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
                if len(body) > MAX_REQUEST_BYTES:
                    raise ValueError('Whole-image request exceeds bounded size')
                request = Request(ENDPOINT, data=body, headers={'Content-Type': 'application/json',
                                                               'Accept': 'application/json'}, method='POST')
                tick = time.perf_counter()
                try:
                    result['raw_response'] = self._json_request(request)
                finally:
                    result['timing_ms']['client_ms'] = (time.perf_counter() - tick) * 1000
                result['validated'] = self._client.validate_response(result['raw_response'])
            accepted = result['validated']['answers']['human_presence']
            choice = accepted['choice']
            winning = max(accepted['probabilities'].values())
            result.update(model=result['validated']['model'], raw_semantic_choice=choice,
                          probabilities=deepcopy(accepted['probabilities']), confidence=accepted['confidence'],
                          none_probability=accepted['none_probability'], winning_probability=winning)
            if (choice not in ('present', 'absent') or accepted['none_probability'] >= self.max_none_probability
                    or winning < self.min_winning_probability):
                result['error'] = 'semantic_uncertain: candidate, winning probability or None score did not pass replay acceptance'
            else:
                result.update(valid=True, semantic_presence=choice, present=(choice == 'present'))
        except Exception as exc:
            result['error'] = type(exc).__name__ + ': ' + str(exc)
        result['timing_ms']['total_ms'] = (time.perf_counter() - started) * 1000
        return result
