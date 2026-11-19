# Offline checks from the VS Code repository terminal:
# python -X utf8 -B -m unittest discover -s tests -p 'test_2026_10_09_perception.py' -v
"""Read-only, optional grasp perception over the shared real camera frames.

No capture device, CAN, model download, file writes, or movement is opened here.
OpenCV reports color candidates in image pixels, not metric grasp poses. SAM2
and a separately trained HTTP classifier are optional evidence providers. All
dependencies beyond the standard library are lazy. Inference is not training.
"""
from copy import deepcopy
import base64
import importlib.util
import json
import math
from pathlib import Path
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


SCHEMA = "tiger-perception-v1"
SAM_CONFIGS = {
    "tiny": "configs/sam2.1/sam2.1_hiera_t.yaml",
    "small": "configs/sam2.1/sam2.1_hiera_s.yaml",
}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _rgb(value):
    import numpy as np
    if not isinstance(value, np.ndarray) or value.dtype != np.uint8 or value.ndim != 3 or value.shape[2] != 3:
        raise ValueError("Expected real uint8 RGB HxWx3 array")
    if min(value.shape[:2]) < 2 or value.shape[0] * value.shape[1] > 4096 * 2160:
        raise ValueError("Unsupported image dimensions")
    return value


def _unknown(reason):
    return {"status": "unknown", "reason": str(reason)[:600], "metric_pose": None,
            "physical_task_success": "unknown", "commands_sent": 0}


class AlignmentObserver:
    """Red HSV contour geometry; image center is never relabeled fingertip center."""
    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})
        self.min_area = float(self.cfg.get("min_area_fraction", .0015))
        self.max_area = float(self.cfg.get("max_area_fraction", .6))
        self.ambiguity = float(self.cfg.get("ambiguity_ratio", .35))
        if not 0 < self.min_area < self.max_area <= 1 or not 0 < self.ambiguity <= 1:
            raise ValueError("Invalid contour area/ambiguity bounds")

    def calibration(self, role, width, height):
        cal = self.cfg.get("calibrations", {}).get(role)
        if cal is None:
            return None
        if not isinstance(cal, dict) or not isinstance(cal.get("calibration_id"), str) or not cal["calibration_id"].strip():
            raise ValueError("Explicit calibration_id required for expected center or fingertip ROI")
        if cal.get("image_size") != [width, height]:
            raise ValueError("Calibration resolution differs from current source image")
        point = cal.get("expected_center_px")
        if point is not None and (not isinstance(point, (list, tuple)) or len(point) != 2 or
                                  not all(_finite(v) for v in point) or
                                  not 0 <= point[0] < width or not 0 <= point[1] < height):
            raise ValueError("Invalid calibrated expected image center")
        rois = cal.get("fingertip_rois_px", [])
        if not isinstance(rois, list) or len(rois) > 2:
            raise ValueError("At most two explicit fingertip ROIs are supported")
        for box in rois:
            if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(_finite(v) for v in box):
                raise ValueError("Invalid fingertip ROI")
            x, y, w, h = box
            if not (w > 0 and h > 0 and 0 <= x < x + w <= width and 0 <= y < y + h <= height):
                raise ValueError("Fingertip ROI outside source image")
        return deepcopy(cal)

    def observe(self, image, role="right", mask=None):
        import cv2
        import numpy as np
        image = _rgb(image)
        height, width = image.shape[:2]
        cal = self.calibration(role, width, height)
        if mask is None:
            hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
            mask = cv2.bitwise_or(cv2.inRange(hsv, (0, 100, 60), (10, 255, 255)),
                                 cv2.inRange(hsv, (170, 100, 60), (179, 255, 255)))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        else:
            if not isinstance(mask, np.ndarray) or mask.shape != (height, width):
                raise ValueError("Segmentation mask must match real image resolution")
            mask = (mask.astype(bool).astype(np.uint8) * 255)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        regions = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if not self.min_area <= area / (width * height) <= self.max_area:
                continue
            moments = cv2.moments(contour)
            if moments["m00"] <= 0:
                continue
            center = [float(moments["m10"] / moments["m00"]), float(moments["m01"] / moments["m00"])]
            rect = cv2.minAreaRect(contour)
            corners = cv2.boxPoints(rect).astype(float).tolist()
            regions.append({"centroid_px": center, "area_px2": area,
                "bbox_px": list(map(int, cv2.boundingRect(contour))), "rect_corners_px": corners,
                "rect_size_px": list(map(float, rect[1])), "rect_angle_deg": float(rect[2]),
                "image_center_error_px": [center[0] - (width - 1) / 2, center[1] - (height - 1) / 2],
                "calibrated_center_error_px": None if not cal or cal.get("expected_center_px") is None else
                    [center[i] - cal["expected_center_px"][i] for i in range(2)]})
        regions.sort(key=lambda item: item["area_px2"], reverse=True)
        ambiguous = len(regions) > 1 and regions[1]["area_px2"] >= regions[0]["area_px2"] * self.ambiguity
        return {"status": "ambiguous" if ambiguous else "candidate" if regions else "missing",
            "method": "opencv_contours_minAreaRect", "coordinate_frame": "source_image_pixels",
            "image_size": [width, height], "regions": regions[:4], "candidate_count": len(regions),
            "selected": regions[0] if regions and not ambiguous else None,
            "calibration": cal, "fingertips_detected": False, "metric_pose": None,
            "semantic_identity_verified": False, "physical_task_success": "unknown", "commands_sent": 0,
            "limitations": ["Image center is not the gripper center.",
                "A minAreaRect angle is a 2D contour convention, not a 3D grasp yaw.",
                "Configured fingertip ROIs do not detect fingertips or prove alignment."]}


class SAM2ImageProvider:
    """Official image predictor, initialized once by the optional background worker.

CUDA free-memory admission is not a hard per-model memory limit. Use a dedicated
perception process/environment and measure coexistence with the VLA explicitly.
"""
    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})
        self.predictor = None
        self.error = None
        self._init_attempted = False

    def capability(self):
        variant = self.cfg.get("variant", "tiny")
        checkpoint = self.cfg.get("checkpoint")
        config = self.cfg.get("config", SAM_CONFIGS.get(variant))
        reason = None
        if variant not in SAM_CONFIGS or config != SAM_CONFIGS.get(variant):
            reason = "Only the selected official SAM2.1 tiny/small config is accepted"
        elif not isinstance(checkpoint, str) or not Path(checkpoint).is_file():
            reason = "Existing local checkpoint required; no automatic download"
        elif importlib.util.find_spec("sam2") is None or importlib.util.find_spec("torch") is None:
            reason = "Optional official sam2/torch dependencies unavailable"
        if self.error:
            reason = self.error
        return {"provider": "sam2", "available": reason is None, "loaded": self.predictor is not None,
            "reason": reason, "variant": variant, "config": config, "checkpoint": checkpoint,
            "device": self.cfg.get("device", "cpu"), "automatic_download": False,
            "memory_guard": "CUDA free-memory admission only; no hard allocation guarantee",
            "writes_robot_commands": False}

    def _initialize(self):
        if self.predictor is not None:
            return
        if self._init_attempted:
            raise RuntimeError(self.error or "SAM2 initialization did not succeed")
        self._init_attempted = True
        try:
            cap = self.capability()
            if not cap["available"]:
                raise RuntimeError(cap["reason"])
            import torch
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor
            device = self.cfg.get("device", "cpu")
            if device != "cpu" and device != "cuda:0":
                raise ValueError("Device must be cpu or cuda:0")
            if device.startswith("cuda"):
                budget = self.cfg.get("memory_budget_mb")
                if self.cfg.get("gpu_coexistence_reviewed") is not True or not _finite(budget) or budget <= 0:
                    raise ValueError("CUDA requires explicit memory budget and reviewed VLA coexistence")
                if not torch.cuda.is_available():
                    raise RuntimeError("CUDA unavailable")
                free, _ = torch.cuda.mem_get_info(0)
                if free < budget * 1024 * 1024:
                    raise RuntimeError("Insufficient free CUDA memory for configured admission budget")
            model = build_sam2(cap["config"], cap["checkpoint"], device=device, mode="eval")
            self.predictor = SAM2ImagePredictor(model)
        except Exception as exc:
            self.error = repr(exc)
            raise

    def observe(self, image, role, geometry):
        import numpy as np
        image = _rgb(image)
        prompt = self.cfg.get("prompts", {}).get(role)
        if not isinstance(prompt, dict) or not isinstance(prompt.get("box_px"), (list, tuple)):
            return _unknown("SAM2 needs an explicit object box prompt; no invented object identity")
        box = prompt["box_px"]
        h, w = image.shape[:2]
        if prompt.get("image_size") != [w, h] or len(box) != 4 or not all(_finite(v) for v in box):
            return _unknown("SAM2 prompt resolution/coordinates invalid")
        if not 0 <= box[0] < box[2] <= w or not 0 <= box[1] < box[3] <= h:
            return _unknown("SAM2 prompt box outside source image")
        self._initialize()
        import torch
        with torch.inference_mode():
            self.predictor.set_image(image)
            masks, scores, _ = self.predictor.predict(box=np.asarray(box, dtype=np.float32), multimask_output=True)
        scores = np.asarray(scores)
        if scores.ndim != 1 or not scores.size or not np.isfinite(scores).all():
            raise RuntimeError("SAM2 returned invalid mask scores")
        idx = int(np.argmax(scores))
        result = geometry.observe(image, role, mask=np.asarray(masks[idx]))
        result.update(method="official_sam2_image_predictor", mask_score=float(scores[idx]),
            mask_score_is_grasp_probability=False, prompt=deepcopy(prompt),
            model_id="sam2.1-" + self.cfg.get("variant", "tiny"))
        return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GraspClassifierClient:
    """Read-only inference HTTP client; not a bundled HIL-SERL trained classifier.

The service receives real PNG frames. Probabilities are evidence only. Validation
provenance is pinned by configuration; a claimed validation status is not a proof
that this robot or scene was validated. No response can authorize CAN motion.
"""
    request_schema = "tiger-grasp-classifier-request-v1"
    response_schema = "tiger-grasp-classifier-v1"
    probability_labels = {"grasped", "empty", "uncertain"}

    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})
        self.timeout = float(self.cfg.get("timeout_s", .5))
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 5:
            raise ValueError("Classifier timeout must be in (0,5] seconds")
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def capability(self):
        url = self.cfg.get("url")
        parsed = urlparse(url or "")
        reason = None
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            reason = "Explicit credential-free HTTP classifier URL required"
        elif not self.cfg.get("model_id") or not self.cfg.get("validation_id"):
            reason = "Expected model_id and validation_id must be pinned"
        return {"provider": "http_grasp_classifier", "configured": reason is None,
                "service_verified": False, "reason": reason, "timeout_s": self.timeout,
                "response_schema": self.response_schema,
                "writes_robot_commands": False, "bundled_pretrained_classifier": False}

    def validate_response(self, data, request):
        expected = {"schema", "request_id", "model_id", "source_stamps", "probabilities", "validation"}
        if not isinstance(data, dict) or set(data) != expected or data.get("schema") != self.response_schema:
            raise ValueError("Classifier response schema mismatch")
        if data["request_id"] != request["request_id"] or data["model_id"] != self.cfg["model_id"]:
            raise ValueError("Classifier request/model identity mismatch")
        if data["source_stamps"] != request["source_stamps"]:
            raise ValueError("Classifier did not identify the actual source frames")
        probabilities = data["probabilities"]
        labels = self.probability_labels
        if not isinstance(probabilities, dict) or set(probabilities) != labels or any(
                not _finite(v) or not 0 <= v <= 1 for v in probabilities.values()) or abs(sum(probabilities.values()) - 1) > .001:
            raise ValueError("Classifier probabilities must be finite normalized " + "/".join(sorted(labels)))
        validation = data["validation"]
        keys = {"id", "status", "dataset_id", "split", "metrics", "artifact_sha256"}
        if not isinstance(validation, dict) or set(validation) != keys or validation["id"] != self.cfg["validation_id"]:
            raise ValueError("Classifier validation provenance mismatch")
        if validation["status"] not in ("validated", "unvalidated") or not all(
                isinstance(validation[k], str) and validation[k].strip() for k in ("dataset_id", "split")):
            raise ValueError("Classifier validation provenance incomplete")
        digest = validation["artifact_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Classifier validation artifact SHA256 required")
        pinned_digest = self.cfg.get("validation_artifact_sha256")
        if pinned_digest is not None and digest != pinned_digest:
            raise ValueError("Classifier validation artifact differs from pinned SHA256")
        metrics = validation["metrics"]
        if not isinstance(metrics, dict) or not metrics or any(not isinstance(k, str) or not _finite(v) for k, v in metrics.items()):
            raise ValueError("Classifier numeric validation metrics required")
        return {"status": "evidence" if validation["status"] == "validated" else "unvalidated_evidence",
                "response": deepcopy(data), "physical_task_success": "unknown", "commands_sent": 0,
                "robot_validation_confirmed_by_client": False}

    def infer(self, frames, source_stamps, request_id):
        cap = self.capability()
        if not cap["configured"]:
            return _unknown(cap["reason"])
        try:
            import cv2
            images = {}
            for role, frame in frames.items():
                ok, buffer = cv2.imencode(".png", cv2.cvtColor(_rgb(frame["rgb"]), cv2.COLOR_RGB2BGR))
                if not ok:
                    raise ValueError("Real image PNG encoding failed")
                images[role] = base64.b64encode(buffer.tobytes()).decode("ascii")
            body = {"schema": self.request_schema, "request_id": request_id,
                    "source_stamps": source_stamps, "images_png_base64": images}
            payload = json.dumps(body, allow_nan=False).encode("utf-8")
            if len(payload) > 24 * 1024 * 1024:
                raise ValueError("Classifier request exceeds explicit bounded payload limit")
            req = Request(self.cfg["url"], data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with self._opener.open(req, timeout=self.timeout) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("Classifier reply too large")
            data = json.loads(raw)
            return self.validate_response(data, body)
        except Exception as exc:
            return _unknown("Classifier unavailable or invalid: " + repr(exc))


class PlacementClassifierClient(GraspClassifierClient):
    """Independent placed head evidence; never relabel held probability as placed."""
    request_schema = "tiger-placement-classifier-request-v1"
    response_schema = "tiger-placement-classifier-v1"
    probability_labels = {"placed", "not_placed", "uncertain"}

    def capability(self):
        result = super().capability()
        result["provider"] = "http_placement_classifier"
        return result


class PerceptionSupervisor:
    """Bounded latest-only optional worker; update never executes neural inference.

Call update outside CAN/inference loops (recommended <=2 Hz). The synchronous
OpenCV path reads only the supplied images. Neural work has one in-flight and
one pending snapshot, dropping superseded *perception requests*, not recordings.
"""
    def __init__(self, cfg=None):
        source = cfg or {}
        self.cfg = dict(source.get("perception", source))
        self.enabled = self.cfg.get("enabled", True) is True
        self.roles = self.cfg.get("roles", ["front", "right"])
        if not isinstance(self.roles, list) or not self.roles or len(self.roles) > 3 or len(set(self.roles)) != len(self.roles) or any(
                r not in ("front", "right", "left") for r in self.roles):
            raise ValueError("Perception roles must be unique configured camera roles")
        self.provider = self.cfg.get("provider", "opencv")
        if self.provider not in ("opencv", "sam2"):
            raise ValueError("Unknown perception provider")
        self.max_age = float(self.cfg.get("frame_max_age_s", .5))
        self.result_age = float(self.cfg.get("result_max_age_s", 1.))
        if not math.isfinite(self.max_age) or not math.isfinite(self.result_age) or min(self.max_age, self.result_age) <= 0:
            raise ValueError("Perception age limits must be finite positive seconds")
        self.geometry = AlignmentObserver(self.cfg.get("alignment"))
        self.sam = SAM2ImageProvider(self.cfg.get("sam2")) if self.provider == "sam2" else None
        ccfg = self.cfg.get("classifier", {})
        self.classifier = GraspClassifierClient(ccfg) if ccfg.get("enabled") is True else None
        pcfg = self.cfg.get("placement", {})
        self.placement = PlacementClassifierClient(pcfg) if pcfg.get("enabled") is True else None
        if (self.classifier is not None or self.placement is not None) and set(self.roles) != {"front", "right"}:
            raise ValueError("HIL classifiers require exactly front/right camera roles")
        self._condition = threading.Condition()
        self._state = self._base()
        self._neural = None
        self._pending = None
        self._thread = None
        self._closed = False
        self._sequence = 0
        self._superseded = 0
        self._task_session_id = None
        self._context_generation = 0

    def set_task_context(self, session_id):
        """Invalidate old visual requests on a new task; never change a device."""
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 160:
            raise ValueError("Perception task context requires a bounded session identity")
        with self._condition:
            if session_id != self._task_session_id:
                self._task_session_id = session_id
                self._context_generation += 1
                self._pending = None
                self._neural = None

    def capabilities(self):
        return {"schema": SCHEMA, "enabled": self.enabled, "geometry_provider": self.provider,
            "opencv_available": importlib.util.find_spec("cv2") is not None and importlib.util.find_spec("numpy") is not None,
            "sam2": None if self.sam is None else self.sam.capability(),
            "classifier": None if self.classifier is None else self.classifier.capability(),
            "placement": None if self.placement is None else self.placement.capability(),
            "neural_execution": "dedicated background thread; one running and one pending snapshot",
            "metric_control_available": False, "automatic_download": False, "commands_sent": 0}

    def _base(self):
        return {"schema": SCHEMA, "status": "unknown", "fresh": False, "age_s": None,
            "updated_perf": None, "source_stamps": {}, "cameras": {}, "arm": {},
            "metric_pose": None, "physical_task_success": "unknown", "commands_sent": 0,
            "alignment_units": "pixels", "neural": None, "reason": "No observations received"}

    def update(self, frames, arm, now_perf=None):
        now = time.perf_counter() if now_perf is None else now_perf
        if not _finite(now):
            raise ValueError("now_perf must be finite")
        state = self._base()
        state["updated_perf"] = now
        valid = {}
        if not self.enabled or self._closed:
            state.update(status="disabled", reason="Perception disabled or closed")
        else:
            for role in self.roles:
                frame = (frames or {}).get(role) or {}
                stamp, wall = frame.get("stamp_perf"), frame.get("wall_ns")
                if not _finite(stamp) or not 0 <= now - stamp <= self.max_age or not isinstance(wall, int) or isinstance(wall, bool) or wall <= 0:
                    state["cameras"][role] = _unknown("Missing, stale, future or invalid source timestamp")
                    continue
                try:
                    image = _rgb(frame.get("rgb"))
                    state["cameras"][role] = self.geometry.observe(image, role)
                    state["source_stamps"][role] = {"stamp_perf": stamp, "wall_ns": wall}
                    valid[role] = {"rgb": image, "stamp_perf": stamp, "wall_ns": wall}
                except Exception as exc:
                    state["cameras"][role] = _unknown(repr(exc))
            all_valid = len(valid) == len(self.roles)
            state.update(status="observing" if all_valid else "unknown", fresh=all_valid,
                         reason=None if all_valid else "One or more configured images unavailable")
            # Actual arm is diagnostic only; no inferred pose or state substitution.
            arm = arm or {}
            q, grip = arm.get("q_deg"), arm.get("grip_m")
            q = list(q) if isinstance(q, (list, tuple)) and len(q) == 6 and all(_finite(v) for v in q) else None
            state["arm"] = {"q_deg": q, "grip_m": grip if _finite(grip) else None,
                "feedback_fresh": arm.get("fresh", arm.get("feedback_fresh")) is True}
        with self._condition:
            self._sequence += 1
            state["sequence"] = self._sequence
            self._state = state
            if state["fresh"] and (self.sam is not None or self.classifier is not None or self.placement is not None):
                if self._pending is not None:
                    self._superseded += 1
                self._pending = (self._sequence, now, deepcopy(state["source_stamps"]), self._task_session_id, self._context_generation,
                                 {role: dict(frame, rgb=frame["rgb"].copy()) for role, frame in valid.items()})
                if self._thread is None:
                    self._thread = threading.Thread(target=self._work, name="read-only-perception", daemon=True)
                    self._thread.start()
                self._condition.notify_all()
        return self.snapshot(now)

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                sequence, observed, stamps, task_session_id, context_generation, frames = self._pending
                self._pending = None
            result = {"sequence": sequence, "observed_perf": observed, "source_stamps": stamps,
                      "task_session_id": task_session_id,
                      "context_generation": context_generation,
                      "sam2": {}, "classifier": None, "placement": None, "commands_sent": 0}
            if self.sam is not None:
                for role, frame in frames.items():
                    try:
                        result["sam2"][role] = self.sam.observe(frame["rgb"], role, self.geometry)
                    except Exception as exc:
                        result["sam2"][role] = _unknown(repr(exc))
            if self.classifier is not None:
                result["classifier"] = self.classifier.infer(frames, stamps, "perception-%s-%s" % (sequence, observed))
            if self.placement is not None:
                result["placement"] = self.placement.infer(frames, stamps, "placement-%s-%s" % (sequence, observed))
            result["completed_perf"] = time.perf_counter()
            with self._condition:
                if (not self._closed and task_session_id == self._task_session_id
                        and context_generation == self._context_generation):
                    self._neural = result

    def snapshot(self, now_perf=None):
        now = time.perf_counter() if now_perf is None else now_perf
        if not _finite(now):
            raise ValueError("now_perf must be finite")
        with self._condition:
            state = deepcopy(self._state)
            state["neural"] = deepcopy(self._neural)
            state["superseded_perception_requests"] = self._superseded
            state["worker_running"] = self._thread is not None and self._thread.is_alive()
        age = None if state["updated_perf"] is None else now - state["updated_perf"]
        state["age_s"] = age
        state["freshness_limit_s"] = self.result_age
        state["frame_max_age_s"] = self.max_age
        if state["status"] != "disabled" and (age is None or age < 0 or age > self.result_age or any(
                not 0 <= now - stamp["stamp_perf"] <= self.max_age for stamp in state["source_stamps"].values())):
            state.update(fresh=False, status="unknown", reason="Source image or observation result expired")
        neural = state["neural"]
        if neural is not None:
            neural["age_s"] = now - neural["observed_perf"]
            neural["source_image_age_s"] = max((now - stamp["stamp_perf"]
                for stamp in neural["source_stamps"].values()), default=None)
            neural["valid_until_perf"] = min([neural["observed_perf"] + self.result_age] +
                [stamp["stamp_perf"] + self.max_age for stamp in neural["source_stamps"].values()])
            neural["fresh"] = 0 <= neural["age_s"] <= self.result_age and all(
                0 <= now - stamp["stamp_perf"] <= self.max_age for stamp in neural["source_stamps"].values())
            neural["matches_latest_source"] = neural["source_stamps"] == state["source_stamps"]
            # A completed asynchronous response belongs to its own original RGB
            # request, not to the newer camera frame. Validate that binding while
            # retaining the same source-frame TTL; this grants no motion authority.
            responses = [value.get("response") for key in ("classifier", "placement")
                         for value in [neural.get(key)]
                         if isinstance(value, dict) and value.get("status") == "evidence"]
            neural["matches_request_source"] = bool(responses) and all(
                isinstance(body, dict) and body.get("source_stamps") == neural["source_stamps"]
                for body in responses)
        return state

    def close(self, timeout=2):
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return {"stopped": self._thread is None or not self._thread.is_alive(), "commands_sent": 0}
