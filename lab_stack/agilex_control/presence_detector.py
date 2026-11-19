"""Read-only CPU person/palm observations for offline replay.

No camera, robot, motion, network or storage API is used here. A negative result
is only a detector observation, never a claim that a physical space is clear.
Postprocessing is adapted from the Apache-2.0 OpenCV Zoo implementations;
upstream sources, licenses, commits and exact asset hashes are in the task's
official-assets manifest. Person scores are suppressed separately from other
COCO classes. Palm NMS receives xywh as required by OpenCV, not xyxy.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import threading
import time


DEFAULT_MODEL_ROOT = Path(r"D:/AgileX-Models/2026-10-18-10-02-presence-detection")
ASSETS = {
    "person": ("object_detection_nanodet_2022nov.onnx", 3800954,
               "4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186"),
    "palm": ("palm_detection_mediapipe_2023feb.onnx", 3905734,
             "78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c"),
}


def xyxy_to_xywh(boxes):
    """Return a copy; never change boxes that will be displayed or audited."""
    result = boxes.copy()
    result[:, 2:4] -= result[:, 0:2]
    return result


def _threshold(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 1:
        raise ValueError(name + " must be finite and strictly between zero and one")
    return float(value)


def _checked_asset(root, key):
    name, size, sha = ASSETS[key]
    path = root / name
    if not path.is_file() or path.stat().st_size != size:
        raise ValueError("Missing or wrong-sized official " + key + " asset")
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise ValueError("Official " + key + " asset SHA-256 mismatch")
    return path


def _palm_anchors(np):
    # MediaPipe short-range detector: stride-8 two anchors, stride-16 six.
    rows = []
    for cells, repeats in ((24, 2), (12, 6)):
        for y in range(cells):
            for x in range(cells):
                rows.extend([((x + .5) / cells, (y + .5) / cells)] * repeats)
    return np.asarray(rows, dtype=np.float32)


def _letterbox(image, size, cv):
    """Symmetric black padding, without cropping any source-image region."""
    height, width = image.shape[:2]
    ratio = min(size / height, size / width)
    new_w, new_h = max(1, int(width * ratio)), max(1, int(height * ratio))
    resized = cv.resize(image, (new_w, new_h), interpolation=cv.INTER_AREA)
    left, top = (size - new_w) // 2, (size - new_h) // 2
    padded = cv.copyMakeBorder(resized, top, size - new_h - top,
                              left, size - new_w - left, cv.BORDER_CONSTANT, value=(0, 0, 0))
    return padded, (left, top, new_w / width, new_h / height)


def _unletterbox(boxes, transform, image_shape, np):
    left, top, scale_x, scale_y = transform
    restored = boxes.copy().astype(np.float64)
    restored[:, (0, 2)] = (restored[:, (0, 2)] - left) / scale_x
    restored[:, (1, 3)] = (restored[:, (1, 3)] - top) / scale_y
    restored[:, (0, 2)] = np.clip(restored[:, (0, 2)], 0, image_shape[1])
    restored[:, (1, 3)] = np.clip(restored[:, (1, 3)], 0, image_shape[0])
    return restored


def _nms(boxes, scores, threshold, iou, cv, np):
    if not len(boxes):
        return np.empty((0,), dtype=np.int64)
    if not np.isfinite(boxes).all() or not np.isfinite(scores).all():
        raise ValueError("Non-finite detector output")
    widths = xyxy_to_xywh(boxes)
    if (widths[:, 2:4] < 0).any():
        raise ValueError("Inverted detector box")
    indices = cv.dnn.NMSBoxes(widths.tolist(), scores.tolist(), threshold, iou)
    return np.asarray(indices, dtype=np.int64).reshape(-1)


def _records(boxes, scores, kind):
    return [{"kind": kind, "bbox_xyxy": [round(float(v), 3) for v in box],
             "score": float(score)} for box, score in zip(boxes, scores)
            if box[2] > box[0] and box[3] > box[1]]


class OpenCVPresenceDetector:
    """Two independent full-frame detectors; either can report presence.

    Confidence defaults are frozen public OpenCV demo defaults, not fitted to
    any reference labels. OpenCV/NumPy imports occur only on construction.
    """
    def __init__(self, model_root=DEFAULT_MODEL_ROOT, *, person_threshold=.35,
                 palm_threshold=.5, person_nms=.6, palm_nms=.3):
        self.person_threshold = _threshold(person_threshold, "person_threshold")
        self.palm_threshold = _threshold(palm_threshold, "palm_threshold")
        self.person_nms = _threshold(person_nms, "person_nms")
        self.palm_nms = _threshold(palm_nms, "palm_nms")
        root = Path(model_root).resolve(strict=True)
        paths = {kind: _checked_asset(root, kind) for kind in ASSETS}
        import cv2
        import numpy
        self.cv, self.np = cv2, numpy
        self._lock = threading.Lock()
        self._nets = {}
        for kind, path in paths.items():
            network = cv2.dnn.readNet(str(path))
            network.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            network.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            self._nets[kind] = network
        self._palm_anchors = _palm_anchors(numpy)
        self._mean = numpy.asarray([103.53, 116.28, 123.675], dtype=numpy.float32)
        self._std = numpy.asarray([57.375, 57.12, 58.395], dtype=numpy.float32)
        self.model_info = {
            "backend": "OpenCV DNN CPU", "opencv": cv2.__version__, "numpy": numpy.__version__,
            "person": "NanoDet-m-plus-1.5x_416 (COCO person column 0)",
            "hand": "MediaPipe Palm detector 192; no landmark acceptance filter",
            "person_threshold": self.person_threshold, "palm_threshold": self.palm_threshold,
            "person_nms": self.person_nms, "palm_nms": self.palm_nms,
            "roi": "entire source frame", "license": "Apache-2.0", "safety_certified": False,
            "assets": {key: {"filename": value[0], "bytes": value[1], "sha256": value[2]}
                       for key, value in ASSETS.items()},
        }

    def _person(self, image):
        cv, np = self.cv, self.np
        padded, transform = _letterbox(image, 416, cv)
        rgb = cv.cvtColor(padded, cv.COLOR_BGR2RGB).astype(np.float32)
        blob = cv.dnn.blobFromImage((rgb - self._mean) / self._std)
        net = self._nets["person"]
        net.setInput(blob)
        outputs = net.forward(net.getUnconnectedOutLayersNames())
        if len(outputs) != 6:
            raise ValueError("Unexpected NanoDet output count")
        all_boxes, all_scores = [], []
        for level, stride in enumerate((8, 16, 32)):
            scores = np.asarray(outputs[level * 2])[0]
            distances = np.asarray(outputs[level * 2 + 1])[0]
            cells = 416 // stride
            if scores.shape != (cells * cells, 80) or distances.shape != (cells * cells, 32):
                raise ValueError("Unexpected NanoDet output shape")
            if not np.isfinite(scores).all() or not np.isfinite(distances).all():
                raise ValueError("Non-finite NanoDet output")
            person_scores = scores[:, 0]
            selected = np.flatnonzero(person_scores >= self.person_threshold)
            if not len(selected):
                continue
            # Keep the person probability even when another COCO class wins.
            logits = distances[selected].reshape(-1, 4, 8)
            exponent = np.exp(logits - logits.max(axis=2, keepdims=True))
            distribution = exponent / exponent.sum(axis=2, keepdims=True)
            offset = (distribution * np.arange(8)).sum(axis=2) * stride
            x, y = selected % cells * stride + .5 * (stride - 1), selected // cells * stride + .5 * (stride - 1)
            boxes = np.column_stack((x - offset[:, 0], y - offset[:, 1], x + offset[:, 2], y + offset[:, 3]))
            all_boxes.append(boxes)
            all_scores.append(person_scores[selected])
        if not all_boxes:
            return []
        boxes, scores = np.concatenate(all_boxes), np.concatenate(all_scores)
        keep = _nms(boxes, scores, self.person_threshold, self.person_nms, cv, np)
        restored = _unletterbox(boxes[keep], transform, image.shape, np)
        return _records(restored, scores[keep], "person")

    def _palm(self, image):
        cv, np = self.cv, self.np
        padded, transform = _letterbox(image, 192, cv)
        rgb = cv.cvtColor(padded, cv.COLOR_BGR2RGB).astype(np.float32) / 255.0
        net = self._nets["palm"]
        net.setInput(rgb[None])
        outputs = net.forward(net.getUnconnectedOutLayersNames())
        if len(outputs) != 2 or outputs[0].shape != (1, 2016, 18) or outputs[1].shape != (1, 2016, 1):
            raise ValueError("Unexpected palm output shape")
        if not all(np.isfinite(value).all() for value in outputs):
            raise ValueError("Non-finite palm output")
        logits = outputs[1][0, :, 0].astype(np.float64)
        scores = 1 / (1 + np.exp(-np.clip(logits, -80, 80)))
        selected = np.flatnonzero(scores >= self.palm_threshold)
        if not len(selected):
            return []
        delta = outputs[0][0, selected, :4]
        center = delta[:, :2] + self._palm_anchors[selected] * 192
        boxes = np.column_stack((center - delta[:, 2:4] / 2, center + delta[:, 2:4] / 2))
        scores = scores[selected]
        keep = _nms(boxes, scores, self.palm_threshold, self.palm_nms, cv, np)
        restored = _unletterbox(boxes[keep], transform, image.shape, np)
        raw_boxes = boxes[keep].astype(np.float64).copy()
        landmarks = outputs[0][0, selected[keep], 4:].reshape(-1, 7, 2).astype(np.float64)
        landmarks += self._palm_anchors[selected[keep], None, :] * 192
        left, top, scale_x, scale_y = transform
        raw_boxes[:, (0, 2)] = (raw_boxes[:, (0, 2)] - left) / scale_x
        raw_boxes[:, (1, 3)] = (raw_boxes[:, (1, 3)] - top) / scale_y
        landmarks[:, :, 0] = (landmarks[:, :, 0] - left) / scale_x
        landmarks[:, :, 1] = (landmarks[:, :, 1] - top) / scale_y
        rows = []
        for box, raw_box, score, points in zip(restored, raw_boxes, scores[keep], landmarks):
            records = _records([box], [score], "palm")
            if records:
                records[0]['palm_landmarks_xy'] = points.tolist()
                records[0]['palm_bbox_xyxy_unclipped'] = raw_box.tolist()
                rows.append(records[0])
        return rows

    def infer(self, image):
        """Return pixels, scores and timing; any processing error is invalid.

        The caller owns timestamps, frame freshness, continuity and coverage
        checks. Neither a clear frame nor this function grants robot motion.
        """
        started = time.perf_counter()
        result = {"valid": False, "error": None, "persons": [], "hands": [],
                  "present": None, "timing_ms": {}, "source": "offline_image",
                  "safety_certified": False}
        try:
            np = self.np
            if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
                raise ValueError("Expected uint8 HxWx3 BGR image")
            if min(image.shape[:2]) < 2 or max(image.shape[:2]) > 16384:
                raise ValueError("Image dimensions outside supported range")
            result["source_image_size"] = [int(image.shape[1]), int(image.shape[0])]
            with self._lock:
                tick = time.perf_counter()
                result["persons"] = self._person(image)
                result["timing_ms"]["person_ms"] = (time.perf_counter() - tick) * 1000
                tick = time.perf_counter()
                result["hands"] = self._palm(image)
                result["timing_ms"]["palm_ms"] = (time.perf_counter() - tick) * 1000
            result["present"] = bool(result["persons"] or result["hands"])
            result["valid"] = True
        except Exception as exc:
            result["error"] = type(exc).__name__ + ": " + str(exc)
        result["timing_ms"]["total_ms"] = (time.perf_counter() - started) * 1000
        return result


PresenceDetector = OpenCVPresenceDetector
