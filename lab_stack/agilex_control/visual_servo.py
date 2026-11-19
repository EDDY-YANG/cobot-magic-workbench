# Pure calculation checks (repository terminal; no hardware or ViSP required):
# python -X utf8 -B -m unittest discover -s tests -p 'test_2026_10_09_visual_servo.py' -v
"""Calibrated point IBVS shadow candidates; never an executable robot driver.

Eye-in-hand convention: s_dot = L(s,Z) camera_twist, ordered [v, omega].
T_camera_tool maps tool-frame points to camera coordinates, in meters. The
supplied body tool Jacobian maps joint rad/s to tool-origin linear/angular speed.
ViSP API source checked:
https://github.com/lagadic/visp/blob/master/modules/python/examples/ibvs-four-points.py
(FeaturePoint.buildFrom, Servo.EYEINHAND_CAMERA, CURRENT, PSEUDO_INVERSE).
No calibration values, desired corners, depth or Jacobians are fabricated.
"""
from copy import deepcopy
import importlib.util
import math
import time


SCHEMA = "tiger-visual-servo-v1"
CALIBRATION_SCHEMA = "tiger-visual-servo-calibration-v1"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _array(value, shape, label):
    import numpy as np
    array = np.asarray(value, dtype=float)
    if array.shape != shape or not np.isfinite(array).all():
        raise ValueError(label + " has invalid shape or nonfinite values")
    return array


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate_calibration(certificate, now_unix_s, max_reprojection_error_px=2.):
    """Validate an explicit document; cannot certify its physical measurements."""
    import numpy as np
    if not isinstance(certificate, dict) or certificate.get("schema") != CALIBRATION_SCHEMA:
        raise ValueError("Explicit visual-servo calibration certificate required")
    cert = deepcopy(certificate)
    for key in ("id", "robot_id", "camera_serial", "camera_role", "tcp_id", "jacobian_source_id", "desired_features_id"):
        if not isinstance(cert.get(key), str) or not cert[key].strip():
            raise ValueError("Calibration field required: " + key)
    if cert.get("mount") != "eye_in_hand" or cert.get("jacobian_convention") != "body_tool_linear_angular":
        raise ValueError("Only explicit eye-in-hand/body-tool Jacobian convention supported")
    issued, expires = cert.get("issued_at_unix_s"), cert.get("valid_until_unix_s")
    if not _number(now_unix_s) or not _number(issued) or not _number(expires) or not issued <= now_unix_s < expires:
        raise ValueError("Calibration expired, future-dated or invalid")
    reprojection = cert.get("reprojection_rmse_px")
    if not _number(reprojection) or not 0 <= reprojection <= max_reprojection_error_px:
        raise ValueError("Calibration reprojection error exceeds configured acceptance bound")
    if not _hash(cert.get("report_sha256")) or not _hash(cert.get("robot_model_sha256")):
        raise ValueError("Calibration report and robot model SHA256 required")
    size = cert.get("image_size")
    if not isinstance(size, list) or len(size) != 2 or any(type(v) is not int or v <= 1 for v in size):
        raise ValueError("Calibration source image_size required")
    K = _array(cert.get("K"), (3, 3), "camera K")
    if K[0, 0] <= 0 or K[1, 1] <= 0 or not np.allclose(K[2], [0, 0, 1], atol=1e-12) or abs(K[0, 1]) > 1e-12 or abs(K[1, 0]) > 1e-12:
        raise ValueError("Pinhole intrinsics must have positive focal lengths and zero skew")
    if cert.get("distortion_model") != "opencv_pinhole":
        raise ValueError("Explicit opencv_pinhole distortion model required")
    distortion = np.asarray(cert.get("distortion"), dtype=float)
    if distortion.ndim != 1 or distortion.size not in (4, 5, 8) or not np.isfinite(distortion).all():
        raise ValueError("Explicit 4/5/8 coefficient distortion vector required")
    transform = _array(cert.get("T_camera_tool"), (4, 4), "T_camera_tool")
    rotation = transform[:3, :3]
    if not np.allclose(transform[3], [0, 0, 0, 1], atol=1e-9) or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6) or abs(np.linalg.det(rotation) - 1) > 1e-6:
        raise ValueError("T_camera_tool must be a rigid SE3 transform in meters")
    ids = cert.get("point_ids")
    if not isinstance(ids, list) or not 4 <= len(ids) <= 32 or any(not isinstance(v, str) or not v for v in ids) or len(ids) != len(set(ids)):
        raise ValueError("Four to 32 unique corresponding feature IDs required")
    desired = _array(cert.get("desired_uv_px"), (len(ids), 2), "calibrated desired corners")
    if np.any(desired < 0) or np.any(desired[:, 0] >= size[0]) or np.any(desired[:, 1] >= size[1]):
        raise ValueError("Calibrated desired points outside image")
    limits = _array(cert.get("joint_limits_rad"), (6, 2), "verified joint command limits")
    if np.any(limits[:, 0] >= limits[:, 1]):
        raise ValueError("Joint command limits are empty or reversed")
    sources = cert.get("depth_source_ids")
    if not isinstance(sources, list) or not sources or any(not isinstance(v, str) or not v for v in sources):
        raise ValueError("Calibrated admissible depth sources required")
    return cert


def normalize_points(uv, K, distortion):
    """Undistort real pixel features; output normalized pinhole coordinates."""
    import numpy as np
    uv = np.asarray(uv, dtype=float)
    if np.any(np.asarray(distortion) != 0):
        import cv2
        return cv2.undistortPoints(uv.reshape(-1, 1, 2), np.asarray(K, dtype=float),
                                   np.asarray(distortion, dtype=float)).reshape(-1, 2)
    camera = np.asarray(K, dtype=float)
    return (uv - camera[:2, 2]) / [camera[0, 0], camera[1, 1]]


def point_interaction_matrix(normalized_xy, depths_m):
    """Standard IBVS point Jacobian for a camera moving relative to static points."""
    import numpy as np
    rows = []
    for (x, y), z in zip(normalized_xy, depths_m):
        if not _number(float(z)) or z <= 0:
            raise ValueError("Positive finite metric depth required")
        rows.extend([[-1 / z, 0, x / z, x * y, -(1 + x * x), y],
                     [0, -1 / z, y / z, 1 + y * y, -x * y, -x]])
    return np.asarray(rows, dtype=float)


def camera_tool_adjoint(transform):
    """Map body tool-origin [v,omega] to camera-origin [v,omega]."""
    import numpy as np
    t = np.asarray(transform, dtype=float)
    x, y, z = t[:3, 3]
    skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    rotation = t[:3, :3]
    return np.block([[rotation, skew @ rotation], [np.zeros((3, 3)), rotation]])


class PointIBVS:
    """Pure shadow planner with explicit calibration and source identity checks."""
    def __init__(self, cfg=None):
        source = cfg or {}
        self.cfg = dict(source.get("visual_servo", source))
        self.backend = self.cfg.get("backend", "opencv_numpy")
        if self.backend not in ("opencv_numpy", "visp"):
            raise ValueError("Unknown IBVS backend")
        self.certificate = deepcopy(self.cfg.get("calibration"))
        self.gain = float(self.cfg.get("gain", .3))
        self.damping = float(self.cfg.get("damping", .0001))
        self.max_error_px = float(self.cfg.get("max_error_px", 40.))
        self.max_age = float(self.cfg.get("max_source_age_s", .2))
        self.max_skew = float(self.cfg.get("max_source_skew_s", .05))
        self.condition_limit = float(self.cfg.get("condition_limit", 10000.))
        self.max_reprojection = float(self.cfg.get("max_reprojection_error_px", 2.))
        self.speed = float(self.cfg.get("speed_deg_s", 30.))
        self.accel = float(self.cfg.get("accel_deg_s2", 50.))
        self.dt = float(self.cfg.get("dt_s", .02))
        values = (self.gain, self.damping, self.max_error_px, self.max_age, self.max_skew,
                  self.condition_limit, self.max_reprojection, self.speed, self.accel, self.dt)
        if any(not math.isfinite(v) or v <= 0 for v in values) or self.speed > 30 or self.accel > 50 or abs(self.dt - .02) > 1e-12:
            raise ValueError("Invalid IBVS parameters; max30deg/s,50deg/s2 and20ms reference required")
        self._last = self._blocked("No calibrated observation has been evaluated")

    def _blocked(self, reason):
        return {"schema": SCHEMA, "status": "blocked", "backend": self.backend,
                "reason": str(reason)[:700], "candidate": None, "commands_sent": 0,
                "physical_validation": "unverified", "motion_authorized": False}

    def capabilities(self, now_unix_s=None):
        now = time.time() if now_unix_s is None else now_unix_s
        reason = None
        try:
            validate_calibration(self.certificate, now, self.max_reprojection)
        except Exception as exc:
            reason = repr(exc)
        dependency_available = importlib.util.find_spec("numpy") is not None
        if reason is None and any(value != 0 for value in self.certificate["distortion"]):
            dependency_available = dependency_available and importlib.util.find_spec("cv2") is not None
        if self.backend == "visp":
            dependency_available = dependency_available and importlib.util.find_spec("visp") is not None
        return {"schema": SCHEMA, "backend": self.backend, "dependency_available": dependency_available,
                "calibration_document_valid": reason is None, "calibration_reason": reason,
                "available": dependency_available and reason is None,
                "reason": "Selected ViSP dependency unavailable; no NumPy fallback" if self.backend == "visp" and not dependency_available else reason,
                "shadow_only": True, "motion_authorized": False, "commands_sent": 0,
                "backend_runtime_verified": False,
                "speed_deg_s": self.speed, "accel_deg_s2": self.accel, "dt_s": self.dt}

    def _condition(self, matrix, label):
        import numpy as np
        singular = np.linalg.svd(matrix, compute_uv=False)
        if len(singular) < 6 or singular[-1] <= 1e-10 or singular[0] / singular[-1] > self.condition_limit:
            raise ValueError(label + " is rank deficient or ill-conditioned")
        return float(singular[0] / singular[-1])

    def _camera_twist(self, xy, desired, depths, interaction, error):
        import numpy as np
        if self.backend == "opencv_numpy":
            return -self.gain * np.linalg.solve(interaction.T @ interaction + self.damping**2 * np.eye(6), interaction.T @ error)
        # Real official ViSP bindings only. No fake adapter or fallback is used.
        from visp.visual_features import FeaturePoint
        from visp.vs import Servo
        task = Servo()
        task.setServo(Servo.EYEINHAND_CAMERA)
        task.setInteractionMatrixType(Servo.CURRENT, Servo.PSEUDO_INVERSE)
        task.setLambda(self.gain)
        current_features, desired_features = [], []
        for point, goal, z in zip(xy, desired, depths):
            current, target = FeaturePoint(), FeaturePoint()
            current.buildFrom(float(point[0]), float(point[1]), float(z))
            # CURRENT interaction does not use target Z; preserve positive depth.
            target.buildFrom(float(goal[0]), float(goal[1]), float(z))
            current_features.append(current)
            desired_features.append(target)
            task.addFeature(current, target)
        return np.asarray(task.computeControlLaw(), dtype=float).reshape(6)

    def compute(self, current_uv_px, desired_uv_px, depths_m, jacobian_tool, q_rad,
                previous_qdot_rad_s, source, *, now_perf=None, now_unix_s=None):
        """Return a candidate or explicit blocked result; send no command.

source pins camera/robot/model/feature/depth/Jacobian identities and timestamps.
previous_qdot_rad_s must be the coordinator's actual previous commanded reference,
not reset to zero each tick. q_rad is a fresh actual or explicitly tracked anchor.
"""
        import numpy as np
        now = time.perf_counter() if now_perf is None else now_perf
        wall = time.time() if now_unix_s is None else now_unix_s
        try:
            if not _number(now):
                raise ValueError("Finite current monotonic clock required")
            cert = validate_calibration(self.certificate, wall, self.max_reprojection)
            if self.backend == "visp" and importlib.util.find_spec("visp") is None:
                raise RuntimeError("ViSP Python bindings unavailable; no backend substitution")
            if not isinstance(source, dict):
                raise ValueError("Explicit observation/source provenance required")
            for key in ("robot_id", "camera_serial", "camera_role", "tcp_id", "jacobian_source_id", "robot_model_sha256", "desired_features_id"):
                if source.get(key) != cert[key]:
                    raise ValueError("Source/calibration identity mismatch: " + key)
            if source.get("calibration_id") != cert["id"] or source.get("image_size") != cert["image_size"] or source.get("point_ids") != cert["point_ids"]:
                raise ValueError("Calibration, image dimensions or point correspondence mismatch")
            if source.get("positive_depth_verified") is not True or source.get("depth_source_id") not in cert["depth_source_ids"]:
                raise ValueError("Positive depth must come from an explicitly verified calibrated source")
            stamps = [source.get(key) for key in ("image_stamp_perf", "depth_stamp_perf", "joint_stamp_perf", "jacobian_stamp_perf")]
            if any(not _number(stamp) or not 0 <= now - stamp <= self.max_age for stamp in stamps) or max(stamps) - min(stamps) > self.max_skew:
                raise ValueError("Observation, depth, joints or Jacobian stale/future/skewed")
            n = len(cert["point_ids"])
            current = _array(current_uv_px, (n, 2), "current corresponding points")
            desired = _array(desired_uv_px, (n, 2), "desired corresponding points")
            if not np.allclose(desired, cert["desired_uv_px"], rtol=0, atol=1e-6):
                raise ValueError("Desired features must be the explicit calibrated corners, not image center")
            width, height = cert["image_size"]
            if np.any(current < 0) or np.any(current[:, 0] >= width) or np.any(current[:, 1] >= height):
                raise ValueError("Current feature outside source image")
            depths = _array(depths_m, (n,), "metric depth")
            if np.any(depths <= 0):
                raise ValueError("Positive metric depth required")
            pixel_error = float(np.max(np.linalg.norm(current - desired, axis=1)))
            if pixel_error > self.max_error_px:
                raise ValueError("Pixel error exceeds the local-servo capture range")
            jacobian = _array(jacobian_tool, (6, 6), "body tool Jacobian")
            q = _array(q_rad, (6,), "joint anchor")
            prev = _array(previous_qdot_rad_s, (6,), "previous commanded velocity")
            source_q = _array(source.get("jacobian_q_rad"), (6,), "Jacobian computation joints")
            if not np.allclose(source_q, q, rtol=0, atol=1e-9):
                raise ValueError("Jacobian was not computed for the supplied joint anchor")
            limits = np.asarray(cert["joint_limits_rad"])
            if np.any(q < limits[:, 0]) or np.any(q > limits[:, 1]) or np.any(np.abs(prev) > math.radians(self.speed) + 1e-12):
                raise ValueError("Joint anchor or previous velocity outside allowed bounds")
            xy = normalize_points(current, cert["K"], cert["distortion"])
            target_xy = normalize_points(desired, cert["K"], cert["distortion"])
            interaction = point_interaction_matrix(xy, depths)
            camera_jacobian = camera_tool_adjoint(cert["T_camera_tool"]) @ jacobian
            conditions = {"interaction": self._condition(interaction, "Point interaction matrix"),
                          "camera_jacobian": self._condition(camera_jacobian, "Camera Jacobian")}
            error = (xy - target_xy).reshape(-1)
            requested_twist = self._camera_twist(xy, target_xy, depths, interaction, error)
            if not np.isfinite(requested_twist).all():
                raise ValueError("Backend returned nonfinite camera twist")
            requested_qdot = np.linalg.solve(camera_jacobian.T @ camera_jacobian + self.damping**2 * np.eye(6),
                                              camera_jacobian.T @ requested_twist)
            velocity = math.radians(self.speed)
            acceleration_step = math.radians(self.accel) * self.dt
            qdot = np.clip(requested_qdot, -velocity, velocity)
            qdot = np.clip(qdot, prev - acceleration_step, prev + acceleration_step)
            q_next = q + self.dt * qdot
            if np.any(q_next < limits[:, 0]) or np.any(q_next > limits[:, 1]):
                raise ValueError("Candidate would cross a real command bound")
            applied_twist = camera_jacobian @ qdot
            predicted_error = error + self.dt * interaction @ applied_twist
            if np.linalg.norm(error) > 1e-10 and np.linalg.norm(predicted_error) >= np.linalg.norm(error):
                raise ValueError("Limited candidate does not reduce the local linearized image error")
            self._last = {"schema": SCHEMA, "status": "shadow_candidate", "backend": self.backend,
                "calibration_id": cert["id"], "source": deepcopy(source), "created_perf": now,
                "valid_until_perf": now + self.dt, "commands_sent": 0, "motion_authorized": False,
                "physical_validation": "unverified", "condition_numbers": conditions,
                "max_pixel_error": pixel_error, "linearized_error_norm_before": float(np.linalg.norm(error)),
                "linearized_error_norm_after": float(np.linalg.norm(predicted_error)),
                "candidate": {"requested_camera_twist_m_s_rad_s": requested_twist.tolist(),
                    "camera_twist_m_s_rad_s": applied_twist.tolist(), "qdot_rad_s": qdot.tolist(),
                    "joint_reference_rad": q_next.tolist(), "dt_s": self.dt,
                    "speed_cap_deg_s": self.speed, "acceleration_cap_deg_s2": self.accel},
                "limitations": ["Candidate only; no CAN, IK reachability or collision authorization.",
                    "Certificate syntax/provenance validated; physical calibration is not certified by this code.",
                    "Linearized reduction is not a measured improvement or grasp confirmation."]}
        except Exception as exc:
            self._last = self._blocked(repr(exc))
        return deepcopy(self._last)

    def snapshot(self, now_perf=None):
        result = deepcopy(self._last)
        if result.get("status") == "shadow_candidate":
            now = time.perf_counter() if now_perf is None else now_perf
            result["fresh"] = _number(now) and result["created_perf"] <= now <= result["valid_until_perf"]
            if not result["fresh"]:
                result.update(status="expired", candidate=None)
        else:
            result["fresh"] = False
        return result
