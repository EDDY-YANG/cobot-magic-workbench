"""Optional CPU hand-shape confirmation of full-frame palm candidates.

ROI rotation and landmark restoration are adapted from OpenCV Zoo MPHandPose,
Apache-2.0, official OpenCV HF commit 4b2a0b446e5cf2f11fb6b2c7251091c035d2c1f7:
https://huggingface.co/opencv/handpose_estimation_mediapipe/blob/4b2a0b446e5cf2f11fb6b2c7251091c035d2c1f7/mp_handpose.py
Original code and license are preserved beside the pinned D model asset.

This is an experimental two-stage detector. Low hand-presence scores can reject
real hands, including occluded or unusual hands. Raw palm candidates and every
confirmation score remain in the result. A negative result never establishes a
safe physical workspace. Any processing error is invalid, never a clear result.
The estimated world landmarks are model outputs, not measured metric geometry.
No robot, camera, network or filesystem write occurs in this module.
"""
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import threading
import time


DEFAULT_MODEL_ROOT = Path("D:/AgileX-Models/2026-10-18-10-02-presence-detection")
MODEL_NAME = "handpose_estimation_mediapipe_2023feb.onnx"
MODEL_BYTES = 4_099_621
MODEL_SHA256 = "db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c"


def _checked_model(root):
    path = Path(root) / MODEL_NAME
    if not path.is_file() or path.stat().st_size != MODEL_BYTES:
        raise ValueError("Missing or wrong-sized official hand-pose model")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != MODEL_SHA256:
            raise ValueError("Official hand-pose model SHA-256 differs")
    return path


def _threshold(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value < 1):
        raise ValueError("hand_presence_threshold must be finite and strictly between 0 and 1")
    return float(value)


class OpenCVHandShapeVerifier:
    """Use official 224px MediaPipe hand presence and 21-landmark outputs.

    ``verify(image, palms)`` requires original uint8 BGR plus palm candidates with
    bbox_xyxy, score and seven source-pixel palm_landmarks_xy. Optional
    palm_bbox_xyxy_unclipped preserves official expand-before-clip preprocessing.
    The model's published demo threshold 0.8 is the default; it is not a calibrated
    protective threshold. The returned hands list contains only confirmations;
    raw_palms and unconfirmed_hands preserve all rejected candidates.
    """

    def __init__(self, model_root=DEFAULT_MODEL_ROOT, *, hand_presence_threshold=.8):
        self.threshold = _threshold(hand_presence_threshold)
        path = _checked_model(model_root)
        import cv2
        import numpy
        self.cv, self.np = cv2, numpy
        self._lock = threading.Lock()
        self._net = cv2.dnn.readNet(str(path))
        self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.model_info = {
            "model": MODEL_NAME, "bytes": MODEL_BYTES, "sha256": MODEL_SHA256,
            "backend": "OpenCV DNN CPU", "opencv": cv2.__version__,
            "hand_presence_threshold": self.threshold,
            "threshold_source": "official MPHandPose published default; not calibrated on Piper labels",
            "input": "224x224 RGB NHWC float32 /255, official palm-landmark-rotated ROI",
            "outputs": ["21 image landmarks", "hand presence score", "handedness score", "21 estimated world landmarks"],
            "license": "Apache-2.0", "safety_certified": False,
            "limitations": ["a second-stage rejection may increase hand false negatives",
                            "world landmarks are monocular model estimates, not measured 3D geometry"],
        }

    def _crop_and_pad(self, image, palm_bbox, for_rotation=False):
        """Faithful official box shift/expansion/clipping and square black pad."""
        np, cv = self.np, self.cv
        box = np.asarray(palm_bbox, dtype=np.float64).copy()
        if box.shape != (2, 2) or not np.isfinite(box).all() or (box[1] <= box[0]).any():
            raise ValueError("Degenerate palm ROI")
        wh = box[1] - box[0]
        box += np.asarray([0., 0.] if for_rotation else [0., -.4]) * wh
        center = box.sum(axis=0) / 2
        half = (box[1] - box[0]) * (4 if for_rotation else 3) / 2
        box = np.asarray([center - half, center + half]).astype(np.int32)
        box[:, 0] = np.clip(box[:, 0], 0, image.shape[1])
        box[:, 1] = np.clip(box[:, 1], 0, image.shape[0])
        crop = image[box[0, 1]:box[1, 1], box[0, 0]:box[1, 0], :]
        if min(crop.shape[:2]) < 1:
            raise ValueError("Empty clipped palm ROI")
        side = int(np.linalg.norm(crop.shape[:2]) if for_rotation else max(crop.shape[:2]))
        pad_h, pad_w = side - crop.shape[0], side - crop.shape[1]
        left, top = pad_w // 2, pad_h // 2
        crop = cv.copyMakeBorder(crop, top, pad_h - top, left, pad_w - left,
                                cv.BORDER_CONSTANT, None, (0, 0, 0))
        return crop, box, box[0] - [left, top]

    def _preprocess(self, image, candidate):
        np, cv = self.np, self.cv
        points = np.asarray(candidate.get("palm_landmarks_xy"), dtype=np.float64)
        if points.shape != (7, 2) or not np.isfinite(points).all():
            raise ValueError("Seven finite source-pixel palm landmarks required")
        display = np.asarray(candidate.get("bbox_xyxy"), dtype=np.float64)
        if display.shape != (4,) or not np.isfinite(display).all() or (display[2:] <= display[:2]).any():
            raise ValueError("Finite nondegenerate palm display bbox required")
        box = np.asarray(candidate.get("palm_bbox_xyxy_unclipped", display), dtype=np.float64)
        if box.shape != (4,):
            raise ValueError("Palm ROI bbox requires four source-pixel coordinates")
        roi, box, bias = self._crop_and_pad(image, box.reshape(2, 2), True)
        roi = cv.cvtColor(roi, cv.COLOR_BGR2RGB)
        box = box - bias
        points = points - bias
        p1, p2 = points[0], points[2]
        radians = np.pi / 2 - np.arctan2(-(p2[1] - p1[1]), p2[0] - p1[0])
        radians -= 2 * np.pi * np.floor((radians + np.pi) / (2 * np.pi))
        angle = np.rad2deg(radians)
        rotation = cv.getRotationMatrix2D(box.sum(axis=0) / 2, angle, 1.0)
        rotated = cv.warpAffine(roi, rotation, (roi.shape[1], roi.shape[0]))
        homogeneous = np.c_[points, np.ones(7)]
        rotated_points = np.asarray([homogeneous @ rotation[0], homogeneous @ rotation[1]])
        rotated_box = np.asarray([rotated_points.min(axis=1), rotated_points.max(axis=1)])
        crop, rotated_box, _ = self._crop_and_pad(rotated, rotated_box)
        blob = cv.resize(crop, (224, 224), interpolation=cv.INTER_AREA).astype(np.float32) / 255.
        return blob[None], {"rotated_palm_bbox": rotated_box, "rotation_deg": float(angle),
                            "rotation_matrix": rotation, "pad_bias": bias,
                            "unclipped_palm_bbox_available": "palm_bbox_xyxy_unclipped" in candidate}

    def _decode(self, outputs, context):
        np, cv = self.np, self.cv
        if not isinstance(outputs, (list, tuple)) or len(outputs) != 4:
            raise ValueError("Unexpected hand-pose output count")
        expected = ((1, 63), (1, 1), (1, 1), (1, 63))
        arrays = [np.asarray(value) for value in outputs]
        if any(value.shape != shape for value, shape in zip(arrays, expected)):
            raise ValueError("Unexpected hand-pose output shape")
        if not all(np.isfinite(value).all() for value in arrays):
            raise ValueError("Non-finite hand-pose output")
        points, confidence, handedness, world = arrays
        confidence, handedness = float(confidence[0, 0]), float(handedness[0, 0])
        if not 0 <= confidence <= 1 or not 0 <= handedness <= 1:
            raise ValueError("Hand-pose probability outside zero to one")
        points, world = points[0].reshape(21, 3).copy(), world[0].reshape(21, 3).copy()
        box, angle = context["rotated_palm_bbox"], context["rotation_deg"]
        scale = max((box[1] - box[0]) / np.asarray([224, 224]))
        points[:, :2] = (points[:, :2] - np.asarray([112, 112])) * scale
        points[:, 2] *= scale
        rotation_zero = cv.getRotationMatrix2D((0, 0), angle, 1.0)
        rotated_xy = points[:, :2] @ rotation_zero[:, :2]
        rotated_world = np.c_[world[:, :2] @ rotation_zero[:, :2], world[:, 2]]
        inverse = cv.invertAffineTransform(context["rotation_matrix"])
        center = np.append(box.sum(axis=0) / 2, 1)
        original_center = inverse @ center
        points[:, :2] = rotated_xy + original_center + context["pad_bias"]
        hand_box = np.asarray([points[:, :2].min(axis=0), points[:, :2].max(axis=0)])
        hand_box += np.asarray([0, -.1]) * (hand_box[1] - hand_box[0])
        center = hand_box.sum(axis=0) / 2
        half = (hand_box[1] - hand_box[0]) * 1.65 / 2
        hand_box = np.asarray([center - half, center + half])
        return {"hand_presence_score": confidence, "handedness_right_score": handedness,
                "landmarks_21_pixel": points.tolist(), "landmarks_21_world_estimated": rotated_world.tolist(),
                "hand_bbox_xyxy_unclipped": hand_box.reshape(4).tolist()}

    def verify(self, image, palm_candidates):
        """Return confirmed/unconfirmed candidates; errors cannot become clear."""
        started = time.perf_counter()
        result = {"valid": False, "error": None, "present": None, "hands": [],
                  "raw_palms": deepcopy(palm_candidates), "unconfirmed_hands": [],
                  "diagnostics": [], "timing_ms": {}, "source": "offline_image",
                  "safety_certified": False, "motion_authority": False}
        try:
            np = self.np
            if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3
                    or image.shape[2] != 3 or min(image.shape[:2]) < 2 or max(image.shape[:2]) > 16384):
                raise ValueError("Expected uint8 HxWx3 BGR source image")
            if not isinstance(palm_candidates, list):
                raise ValueError("Palm candidates must be an explicit list")
            with self._lock:
                for index, candidate in enumerate(palm_candidates):
                    if not isinstance(candidate, dict):
                        raise ValueError("Palm candidate must be a mapping")
                    score = candidate.get("score")
                    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
                        raise ValueError("Finite raw palm probability required")
                    step = time.perf_counter()
                    blob, context = self._preprocess(image, candidate)
                    preprocess_ms = (time.perf_counter() - step) * 1000
                    step = time.perf_counter()
                    self._net.setInput(blob)
                    outputs = self._net.forward(self._net.getUnconnectedOutLayersNames())
                    forward_ms = (time.perf_counter() - step) * 1000
                    decoded = self._decode(outputs, context)
                    confirmed = decoded["hand_presence_score"] >= self.threshold
                    item = {**deepcopy(candidate), "palm_index": index, "palm_score": score,
                            "hand_shape_confirmed": confirmed, **decoded}
                    result["diagnostics"].append({**deepcopy(item), "rotation_deg": context["rotation_deg"],
                        "rotated_palm_bbox": context["rotated_palm_bbox"].tolist(),
                        "unclipped_palm_bbox_available": context["unclipped_palm_bbox_available"],
                        "preprocess_ms": preprocess_ms, "forward_ms": forward_ms})
                    if confirmed:
                        item["kind"] = "verified_hand"
                        item["score"] = decoded["hand_presence_score"]
                        result["hands"].append(item)
                    else:
                        result["unconfirmed_hands"].append(item)
            result["valid"] = True
            result["present"] = bool(result["hands"])
        except Exception as exc:
            result["error"] = type(exc).__name__ + ": " + str(exc)
        result["timing_ms"]["total_ms"] = (time.perf_counter() - started) * 1000
        result["timing_ms"]["preprocess_ms"] = sum(row["preprocess_ms"] for row in result["diagnostics"])
        result["timing_ms"]["forward_ms"] = sum(row["forward_ms"] for row in result["diagnostics"])
        return result


HandShapeVerifier = OpenCVHandShapeVerifier
