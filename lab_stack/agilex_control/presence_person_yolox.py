"""Read-only YOLOX-S COCO person observation for CPU offline comparison.

Adapted from official Apache-2.0 OpenCV Zoo YOLOX demo; exact sources and
license are retained beside the frozen D: model. This module detects people,
not isolated hands, and never supplies a robot motion permission.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import threading
import time

from .presence_detector import DEFAULT_MODEL_ROOT, _nms, _records, _threshold


MODEL_NAME = 'object_detection_yolox_2022nov.onnx'
MODEL_BYTES = 35858002
MODEL_SHA256 = 'c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063'


class OpenCVYoloXPersonDetector:
    """Frozen official demo confidence=.5 and NMS=.5, no label tuning."""
    def __init__(self, model_root=DEFAULT_MODEL_ROOT, *, person_threshold=.5, nms_threshold=.5):
        self.person_threshold = _threshold(person_threshold, 'person_threshold')
        self.nms_threshold = _threshold(nms_threshold, 'nms_threshold')
        path = Path(model_root).resolve(strict=True) / MODEL_NAME
        if not path.is_file() or path.stat().st_size != MODEL_BYTES:
            raise ValueError('Missing or wrong-sized official YOLOX asset')
        if hashlib.sha256(path.read_bytes()).hexdigest() != MODEL_SHA256:
            raise ValueError('Official YOLOX asset SHA-256 mismatch')
        import cv2
        import numpy
        self.cv, self.np = cv2, numpy
        self._lock = threading.Lock()
        self.net = cv2.dnn.readNet(str(path))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.model_info = {
            'backend': 'OpenCV DNN CPU', 'opencv': cv2.__version__, 'numpy': numpy.__version__,
            'person': 'YOLOX-S COCO person column 0', 'hands_supported': False,
            'person_threshold': self.person_threshold, 'nms_threshold': self.nms_threshold,
            'preprocessing': 'RGB float32 0..255, 640 top-left letterbox with 114 padding',
            'roi': 'entire source frame', 'license': 'Apache-2.0', 'safety_certified': False,
            'asset': {'filename': MODEL_NAME, 'bytes': MODEL_BYTES, 'sha256': MODEL_SHA256},
        }

    def _infer_person(self, image):
        cv, np = self.cv, self.np
        h, w = image.shape[:2]
        ratio = min(640 / h, 640 / w)
        resized = cv.resize(image, (max(1, int(w * ratio)), max(1, int(h * ratio))),
                            interpolation=cv.INTER_LINEAR)
        padded = np.full((640, 640, 3), 114, np.float32)
        padded[:resized.shape[0], :resized.shape[1]] = cv.cvtColor(resized, cv.COLOR_BGR2RGB)
        self.net.setInput(padded.transpose(2, 0, 1)[None])
        outputs = self.net.forward(self.net.getUnconnectedOutLayersNames())
        if len(outputs) != 1 or outputs[0].shape != (1, 8400, 85):
            raise ValueError('Unexpected YOLOX output shape')
        raw = outputs[0][0]
        if not np.isfinite(raw).all():
            raise ValueError('Non-finite YOLOX output')
        scores = raw[:, 4] * raw[:, 5]
        selected = np.flatnonzero(scores >= self.person_threshold)
        if not len(selected):
            return []
        grids, scales = [], []
        for stride in (8, 16, 32):
            cells = 640 // stride
            xx, yy = np.meshgrid(np.arange(cells), np.arange(cells))
            grids.append(np.stack((xx, yy), axis=-1).reshape(-1, 2))
            scales.append(np.full((cells * cells, 1), stride))
        grid, stride = np.concatenate(grids)[selected], np.concatenate(scales)[selected]
        centers = (raw[selected, :2] + grid) * stride
        log_wh = raw[selected, 2:4]
        if (np.abs(log_wh) > 80).any():
            raise ValueError('YOLOX size outside numeric range')
        wh = np.exp(log_wh.astype(np.float64)) * stride
        boxes = np.column_stack((centers - wh / 2, centers + wh / 2))
        keep = _nms(boxes, scores[selected], self.person_threshold, self.nms_threshold, cv, np)
        boxes = boxes[keep] / ratio
        boxes[:, (0, 2)] = np.clip(boxes[:, (0, 2)], 0, w)
        boxes[:, (1, 3)] = np.clip(boxes[:, (1, 3)], 0, h)
        return _records(boxes, scores[selected][keep], 'person')

    def infer(self, image):
        started = time.perf_counter()
        result = {'valid': False, 'error': None, 'persons': [], 'hands': [], 'present': None,
                  'hands_supported': False, 'source': 'offline_image', 'safety_certified': False,
                  'timing_ms': {}}
        try:
            np = self.np
            if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
                raise ValueError('Expected uint8 HxWx3 BGR image')
            if min(image.shape[:2]) < 2 or max(image.shape[:2]) > 16384:
                raise ValueError('Image dimensions outside supported range')
            result['source_image_size'] = [int(image.shape[1]), int(image.shape[0])]
            with self._lock:
                result['persons'] = self._infer_person(image)
            result['valid'] = True
            result['present'] = bool(result['persons'])
        except Exception as exc:
            result['error'] = type(exc).__name__ + ': ' + str(exc)
        elapsed = (time.perf_counter() - started) * 1000
        result['timing_ms'] = {'person_ms': elapsed, 'total_ms': elapsed}
        return result
