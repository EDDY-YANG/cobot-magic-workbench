# 完整单行命令：Windows PC PowerShell；无需预定义变量，复制时去掉行首 #。
# 按需选择一条命令，不要把所有示例一起执行。
# # 采集模块，经统一入口调用；会打开相机，不发机械臂指令。
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' hub-start
# # 仅在需要接管已识别的旧只读相机生产者时使用：
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' hub-start --takeover-existing
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' hub-stop
# 本轮仅补充命令注释；未运行测试、SSH、模型、相机或机械臂。
"""The sole camera owner: LeRobot capture -> Rerun, local inference, USB review.

HTTP endpoints are loopback only and never issue robot commands. Read-only CAN
observers share receive traffic; the hub has no SDK instance or CAN send call.
"""
import base64
from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import hashlib
import json
import os
from pathlib import Path
import signal
import threading
import time
from urllib.parse import parse_qs, urlparse
from .storage import Ownership, verify_usb, now, Session
from .recording import Recorder
from .sensors import CanObserver
from .stage_observer import StageObserver
from .recovery import RecoverySupervisor
from .task_state import TaskStateMachine, validate_operator_review, validate_execution_event
from .perception import PerceptionSupervisor
from .visual_servo import PointIBVS
from .time_contract import capture_clock_domain, capture_host_clock_anchor
from .runtime_alignment import RuntimeAlignment

STORAGE_RPC_PREFIX = '/storage-bound-v1'


def hub_storage_binding(cfg):
    """The selected recorder owner, not the display name of a drive."""
    selected = cfg.get('usb_selection')
    if selected is None:
        return None
    if selected.get('schema') != 'tiger-usb-selection-v1':
        raise RuntimeError('Invalid recorder storage selection')
    return {'schema':'tiger-hub-storage-binding-v1', 'mountpoint':cfg['usb'],
            'uuid':str(cfg['usb_uuid']).upper(), 'runtime_bundle':cfg['remote_bundle'],
            'device':selected['device'], 'major_minor':selected['major_minor'],
            'physical_usb_path':selected['physical_usb_path']}


def storage_binding_token(binding):
    return hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',', ':'),
                                    ensure_ascii=False).encode('utf-8')).hexdigest()


def validate_hub_storage(cfg, response):
    expected = hub_storage_binding(cfg)
    if expected is not None and (not isinstance(response,dict) or response.get('storage_binding') != expected):
        raise RuntimeError('Existing Hub belongs to another or unverified USB; stop its owner before switching')
    return response


def storage_bound_route(cfg, path, supplied_token):
    """Reject a wrong drive before any record/task mutation is dispatched."""
    binding = hub_storage_binding(cfg)
    if binding is None:
        if path.startswith(STORAGE_RPC_PREFIX + '/'):
            raise RuntimeError('This legacy Hub does not implement selected-USB recording')
        return path
    if not path.startswith(STORAGE_RPC_PREFIX + '/') or supplied_token != storage_binding_token(binding):
        raise RuntimeError('Recording request does not match this Hub USB owner')
    return path[len(STORAGE_RPC_PREFIX):]


def resize_pad_rgb(rgb, size=224):
    """Match the step4000 training reader's RGB resize and centered black pad.

    Keep the original frame for recording/preview. Only the policy image uses
    this transform: 640x480 -> 224x168 with 28 black rows above and below.
    Rounding and interpolation match 2026-10-02-14-58-right-candidate-dataset.py.
    """
    import cv2
    import numpy as np
    if (not isinstance(rgb, np.ndarray) or rgb.ndim != 3 or rgb.shape[2] != 3
            or rgb.dtype != np.uint8 or min(rgb.shape[:2]) <= 0):
        raise ValueError("Policy image must be nonempty HWC uint8 RGB")
    if type(size) is not int or size <= 0:
        raise ValueError("Policy image size must be a positive integer")
    h, w = rgb.shape[:2]
    scale = min(size / h, size / w)
    nh, nw = round(h * scale), round(w * scale)
    if min(nh, nw) <= 0:
        raise ValueError("Image aspect ratio cannot fit the policy image grid")
    resized = cv2.resize(rgb, (nw, nh),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    out = np.zeros((size, size, 3), dtype=np.uint8)
    y, x = (size - nh) // 2, (size - nw) // 2
    out[y:y + nh, x:x + nw] = resized
    return out


def verify_camera(serial):
    candidates = list(Path("/dev/v4l/by-id").glob(f"*{serial}*video-index0"))
    if len(candidates) != 1:
        raise RuntimeError("Camera serial not uniquely identified: " + serial)
    device = candidates[0].resolve(strict=True)
    sys_device = (Path("/sys/class/video4linux") / device.name / "device").resolve(strict=True)
    for item in (sys_device, *sys_device.parents):
        if (item / "serial").is_file():
            if (item / "serial").read_text().strip() != serial:
                raise RuntimeError("Camera USB identity mismatch")
            return str(candidates[0])
    raise RuntimeError("Camera serial unavailable in sysfs")


class CameraUnavailable(RuntimeError):
    pass


def capture_device(serial):
    try:
        return verify_camera(serial)
    except (OSError, RuntimeError) as exc:
        raise CameraUnavailable(str(exc)) from exc


class Camera:
    def __init__(self, role, serial, startup_lock, reopen_guard=None):
        self.role, self.serial, self.startup_lock = role, serial, startup_lock
        self.lock, self.stop_event = threading.Lock(), threading.Event()
        self.latest = None
        self.error = None
        self.reopen_guard = reopen_guard
        self.recovery_state = None
        self.reconnect_attempts = 0
        self.opening_capture = threading.Event()
        self.profile = None
        self.count = 0
        self.clock_domain = None
        self.publication_callback = None
        self.alignment_publication_error = None
        self.thread = threading.Thread(target=self.run, name="unified-camera-" + role, daemon=True)

    def run(self):
        retry = False
        while not self.stop_event.is_set():
            first_frame = self.count
            try:
                self._capture(retry)
            except Exception as exc:
                with self.lock:
                    self.error = repr(exc)
                    self.latest = None
                    self.profile = None
                    # A successful reopen must deliver approximately two seconds of
                    # real frames before a later outage receives a fresh retry.
                    can_retry = (self.reopen_guard is not None
                                 and isinstance(exc, (CameraUnavailable, TimeoutError, OSError))
                                 and (not retry or self.count - first_frame >= 60)
                                 and not self.stop_event.is_set())
                    self.recovery_state = "waiting_for_device" if can_retry else "failed"
                if can_retry:
                    retry = True
                    continue
            return

    @contextmanager
    def _reopen(self):
        # Keep this camera's existing worker alive while USB is absent. Verify
        # twice across the settle interval and acquire the idle guard only then.
        while not self.stop_event.wait(.75):
            try:
                capture_device(self.serial)
            except CameraUnavailable:
                continue
            if self.stop_event.wait(.75):
                break
            try:
                capture_device(self.serial)
            except CameraUnavailable:
                continue
            with self.reopen_guard() as idle:
                if not idle:
                    continue
                with self.lock:
                    self.recovery_state = "opening"
                    self.reconnect_attempts += 1
                yield
                return
        yield

    def _capture(self, retry):
        camera = None
        try:
            import cv2
            from lerobot.cameras.opencv.camera_opencv import OpenCVCamera
            from lerobot.cameras.opencv.configuration_opencv import ColorMode, OpenCVCameraConfig
            with self._reopen() if retry else nullcontext():
                with self.startup_lock:
                    if self.stop_event.is_set():
                        return
                    device = capture_device(self.serial)
                    config = OpenCVCameraConfig(index_or_path=device, fps=30, width=640, height=480,
                        color_mode=ColorMode.RGB, backend=cv2.CAP_V4L2, fourcc="MJPG", warmup_s=6)
                    camera = OpenCVCamera(config)
                    self.opening_capture.set()
                    try:
                        if self.stop_event.is_set():
                            return
                        camera.connect(warmup=True)
                    finally:
                        self.opening_capture.clear()
            cap = camera.videocapture
            self.profile = {"width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), "fps": cap.get(cv2.CAP_PROP_FPS),
                "backend": cap.getBackendName(), "fourcc": "".join(chr((int(cap.get(cv2.CAP_PROP_FOURCC)) >> (8*i)) & 255) for i in range(4))}
            if (self.profile["width"], self.profile["height"], self.profile["backend"], self.profile["fourcc"]) != (640, 480, "V4L2", "MJPG") or abs(self.profile["fps"] - 30) > .01:
                raise RuntimeError("Actual camera profile differs from requested: " + repr(self.profile))
            previous, check_at = None, 0
            last_frame_at = time.monotonic()
            while not self.stop_event.is_set():
                if time.monotonic() > check_at:
                    capture_device(self.serial)
                    check_at = time.monotonic() + 2
                if camera.thread is None or not camera.thread.is_alive():
                    raise CameraUnavailable("LeRobot camera read thread exited")
                with camera.frame_lock:
                    frame, stamp = camera.latest_frame, camera.latest_timestamp
                    image = None if frame is None else frame.copy()
                if image is not None and stamp != previous:
                    # Bind all clock estimates to the same pre-codec anchor.
                    # JPEG duration must not be added to a source wall stamp.
                    source_timing = capture_host_clock_anchor().map_perf_seconds(stamp)
                    age = source_timing["source_age_at_anchor_ns"] / 1e9
                    if age < 0 or age > .5 or image.shape != (480, 640, 3) or str(image.dtype) != "uint8":
                        raise RuntimeError("Camera produced stale or malformed real frame")
                    ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(image, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if not ok:
                        raise RuntimeError("JPEG encode failed")
                    with self.lock:
                        available_ns = time.monotonic_ns()
                        domain = deepcopy(self.clock_domain)
                        source_timing.update(
                            available_monotonic_ns=available_ns,
                            clock_domain=domain,
                            clock_domain_identity_verified=bool(domain and domain.get("identity_verified")),
                            sample_id=(f"{domain['session_id']}:camera:{self.role}:{self.count + 1}"
                                       if domain else None),
                            frame_id=self.count + 1, device_serial=self.serial,
                            mapping_valid=True,
                            provenance_complete=bool(domain and domain.get("identity_verified")),
                            mapping_error_ns=None)
                        self.latest = {"rgb": image, "jpeg": jpeg.tobytes(), "stamp_perf": stamp,
                                       "wall_ns": source_timing["source_wall_ns_estimate"],
                                       "source_timing": source_timing, "frame_id": self.count + 1,
                                       "available_monotonic_ns": available_ns}
                        self.count += 1
                        self.error = None
                        self.recovery_state = None
                        published = self.latest
                    if self.publication_callback is not None:
                        try:
                            self.publication_callback(self.role, published)
                        except Exception as exc:
                            self.alignment_publication_error = repr(exc)
                    previous = stamp
                    last_frame_at = time.monotonic()
                elif self.reopen_guard is not None and time.monotonic() - last_frame_at > 2:
                    raise CameraUnavailable("Front camera stopped producing new frames")
                self.stop_event.wait(.005)
        finally:
            if camera is not None and (camera.is_connected or camera.thread is not None):
                try:
                    camera.disconnect()
                except Exception as exc:
                    raise RuntimeError("Camera close failed; refusing to reopen " + self.role) from exc

    def status(self):
        with self.lock:
            age = None if self.latest is None else time.perf_counter() - self.latest["stamp_perf"]
            return {"serial": self.serial, "profile": self.profile, "frames": self.count,
                    "age_s": age, "fresh": self.thread.is_alive() and self.error is None and age is not None and 0 <= age <= .5,
                    "error": self.error, "thread_alive": self.thread.is_alive(),
                    "recovery_state": self.recovery_state, "reconnect_attempts": self.reconnect_attempts,
                    "alignment_publication_error": self.alignment_publication_error}

    def frame(self):
        with self.lock:
            if self.latest is None or not self.status_unlocked_fresh():
                return None
            result = dict(self.latest)
            if "source_timing" in result:
                result["source_timing"] = deepcopy(result["source_timing"])
            return result

    def status_unlocked_fresh(self):
        return self.thread.is_alive() and self.error is None and self.latest is not None and 0 <= time.perf_counter() - self.latest["stamp_perf"] <= .5

    def close(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(15 if self.opening_capture.is_set() else 4)
            if self.thread.is_alive():
                raise RuntimeError('Camera capture thread still stopping: ' + self.role)


class SensorHub:
    def __init__(self, cfg):
        self.cfg = cfg
        from .capture_profile import contract
        self.capture_contract = contract(cfg)
        self.clock_domain = None
        self.alignment = None
        self.stop_event = threading.Event()
        startup = threading.Lock()
        self.cameras = {role: Camera(role, cfg['cameras'][role], startup,
                                     self._front_reopen_guard if role == 'front' else None)
                        for role in self.capture_contract['required_camera_roles']}
        self.observers = {role: CanObserver(**entry) for role, entry in cfg["can"].items()}
        self.observer_start_errors = {}
        self.recorder = Recorder(cfg, self.record_state, self.record_frames)
        self.record_request_lock = threading.RLock()
        self.preview_error = None
        self.stage_observer = StageObserver(cfg)
        self.stage_observer_error = None
        self.recovery = RecoverySupervisor(cfg)
        self.recovery_context_lock = threading.Lock()
        self.recovery_session = None
        self.recovery_lease_until = 0.
        self.task_state = TaskStateMachine(cfg)
        self.perception = PerceptionSupervisor(cfg)
        self.perception_error = None
        self.visual_servo = PointIBVS(cfg)
        self.task_event_lock = threading.RLock()
        self.task_event_sequences = {}
        self.task_lease_until = 0.

    @contextmanager
    def _front_reopen_guard(self):
        """Retain the existing device owner and reserve the idle writer slots."""
        if not self.record_request_lock.acquire(blocking=False):
            yield False
            return
        try:
            with ExitStack() as locks:
                recording = self.recorder.status()
                idle = (recording.get('active') is False and recording.get('saving') is False
                        and self.task_snapshot().get('active') is False
                        and self.recovery_snapshot().get('deployment_active') is False
                        and not self.stop_event.is_set())
                if idle:
                    interfaces = sorted({entry['interface'] for entry in self.cfg['can'].values()})
                    try:
                        for interface in interfaces:
                            locks.enter_context(Ownership('writer-' + interface))
                    except RuntimeError:
                        idle = False
                if idle:
                    verify_usb(self.cfg)
                yield idle
        finally:
            self.record_request_lock.release()

    def visual_servo_snapshot(self):
        servo = getattr(self, "visual_servo", None)
        if servo is None:
            return {"schema": "tiger-visual-servo-v1", "status": "blocked", "motion_authorized": False,
                    "reason": "视觉伺服尚未初始化", "commands_sent": 0}
        return {**servo.snapshot(), "capabilities": servo.capabilities()}

    def task_snapshot(self):
        # __new__ test fixtures and old clients may not have the new observer.
        tracker = getattr(self, "task_state", None)
        if tracker is None:
            return {"schema": "tiger-task-state-v1", "fresh": False,
                    "phase": "unknown", "session_id": None, "results": {}, "commands_sent": 0}
        lock = getattr(self, "task_event_lock", None)
        if lock is not None and lock.acquire(blocking=False):
            try:
                self._expire_task_owner()
            finally:
                lock.release()
        result = tracker.snapshot()
        result["owner_lease"] = {"valid_until_monotonic": getattr(self, "task_lease_until", 0.),
            "metadata_only": True, "motion_stop_confirmed": False}
        if result.get("active") and time.monotonic() > getattr(self, "task_lease_until", 0.):
            result.update(fresh=False, owner_confirmation="expired")
        return result

    def _expire_task_owner(self):
        if (self.task_state.snapshot().get("active") and
                time.monotonic() > getattr(self, "task_lease_until", 0.)):
            self.task_state.lose_owner("任务心跳已过期；仅观测会话失效，不能确认控制程序或机械臂已停止")

    def task_heartbeat(self, data):
        if not hasattr(self, "task_event_lock"):
            self.task_event_lock = threading.RLock()
            self.task_event_sequences = {}
            self.task_lease_until = 0.
        with self.task_event_lock:
            self._expire_task_owner()
            current = self.task_state.snapshot()
            if current.get("active") and current.get("session_id") == data.get("session_id"):
                self.task_lease_until = time.monotonic() + 5.
                return {"renewed": True, "session_id": data["session_id"], "commands_sent": 0}
            return {"renewed": False, "reason": "No matching live metadata owner", "commands_sent": 0}

    def perception_snapshot(self):
        observer = getattr(self, "perception", None)
        result = observer.snapshot() if observer is not None else {"schema": "tiger-perception-v1", "fresh": False,
            "reason": "感知模块尚未初始化", "metric_pose": None, "commands_sent": 0}
        if observer is not None:
            result["capabilities"] = observer.capabilities()
        if getattr(self, "perception_error", None):
            result.update(fresh=False, observer_error=self.perception_error)
        return result

    def task_event(self, data):
        if not hasattr(self, "task_event_lock"):
            self.task_event_lock = threading.RLock()
            self.task_event_sequences = {}
            self.task_lease_until = 0.
        with self.task_event_lock:
            return self._task_event_locked(data)

    def _task_event_locked(self, data):
        tracker = self.task_state
        self._expire_task_owner()
        sid = data.get("session_id")
        event, receipt = validate_execution_event(data.get("event"), data.get("receipt", {}))
        sequence = data.get("sequence")
        if sequence is not None:
            if type(sequence) is not int or sequence < 1:
                raise ValueError("Task event sequence must be a positive integer")
            if sequence <= self.task_event_sequences.get(sid, 0):
                return {**tracker.snapshot(), "ignored_event": "out_of_order_or_duplicate"}
        if event in ("deployment_started", "prepare_started"):
            current = tracker.snapshot()
            if (current.get("session_id") == sid and not current.get("active") and
                    (current.get("results", {}).get("execution", {}).get("status") != "unverified" or
                     current.get("results", {}).get("execution", {}).get("provenance") == "owner_lease_expired")):
                raise ValueError("Ended task session cannot be restarted; use a new deployment identity")
            tracker.set_context(sid, True)
            perception = getattr(self, "perception", None)
            if perception is not None:
                perception.set_task_context(sid)
        result = tracker.record_execution_event(sid, event, receipt)
        if result.get("active"):
            self.task_lease_until = time.monotonic() + 5.
        if (event in ("fault", "cancelled", "recording_completed", "recording_failed") or
                event == "recording_pending" and data.get("receipt", {}).get("motion_closed") is True):
            result = tracker.set_context(sid, False)
        if sequence is not None:
            self.task_event_sequences[sid] = sequence
            if len(self.task_event_sequences) > 32:
                del self.task_event_sequences[next(iter(self.task_event_sequences))]
        return result

    def task_review(self, data):
        # This is an operator annotation, never a synthetic sensor receipt.
        reviewer, refs = validate_operator_review(data.get("verdict"), data.get("reviewer"), data.get("evidence_refs"))
        if not hasattr(self, "task_event_lock"):
            self.task_event_lock = threading.RLock()
            self.task_event_sequences = {}
        with self.task_event_lock:
            if data.get("session_id") != self.task_state.snapshot().get("session_id"):
                raise ValueError("Task identity changed before review")
            # Commit memory only after the actual USB annotation has been saved.
            session = Session(self.cfg, "task-outcome-review")
            session.finish("OPERATOR_REVIEW_RECORDED_NO_MOTION", session_id=data["session_id"],
                verdict=data["verdict"], reviewer=reviewer, evidence_refs=refs, provenance="operator_review")
            result = self.task_state.review_outcome(data["session_id"], data["verdict"], reviewer, refs)
        return {"task_state": result, "report": str(session.path / "operation.json"), "commands_sent": 0}

    def recovery_context(self, data, heartbeat=False):
        """Metadata only. A five-second lease prevents stale 'running' on crash."""
        sid = data.get("session_id")
        if not isinstance(sid, str) or not 1 <= len(sid) <= 160:
            raise ValueError("Recovery context requires a deployment session ID")
        with self.recovery_context_lock:
            if self.recovery_session is not None and time.monotonic() > self.recovery_lease_until:
                self.recovery.set_context({"deployment_active": False})
                self.recovery_session = None
            if heartbeat:
                if self.recovery_session == sid:
                    self.recovery_lease_until = time.monotonic() + 5.
            else:
                active = data.get("deployment_active")
                if type(active) is not bool:
                    raise ValueError("deployment_active must be boolean")
                if self.recovery_session is not None and self.recovery_session != sid:
                    raise ValueError("A different deployment owns the recovery context")
                self.recovery_session = sid if active else None
                self.recovery_lease_until = time.monotonic() + 5. if active else 0.
                self.recovery.set_context({"session_id": sid, "deployment_active": active,
                                           "front_camera_stationary_verified": False})
            return self.recovery.snapshot()

    def recovery_snapshot(self):
        with self.recovery_context_lock:
            if self.recovery_session is not None and time.monotonic() > self.recovery_lease_until:
                self.recovery.set_context({"deployment_active": False})
                self.recovery_session = None
            return self.recovery.snapshot()

    def stage_snapshot(self):
        result = self.stage_observer.snapshot()
        result["sample_rate_max_hz"] = 2
        result["worker_error"] = self.stage_observer_error
        if self.stage_observer_error:
            result.update(fresh=False, phase="unknown", label="无法确认阶段",
                          evidence=["观察线程异常：" + self.stage_observer_error])
        return result

    def _observe_stage_once(self):
        """Reuse existing frames/feedback, with no device open or CAN send."""
        frames = {role: self.cameras[role].frame() for role in ("front", "right")}
        arm = self.observers["right"].control_snapshot()
        self.stage_observer.update(frames, arm, now_perf=time.perf_counter())
        if getattr(self, "perception", None) is not None:
            try:
                self.perception.update(frames, arm, now_perf=time.perf_counter())
                self.perception_error = None
            except Exception as exc:
                self.perception_error = repr(exc)
        if getattr(self, "task_state", None) is not None:
            self.task_snapshot()  # Metadata lease, independent of healthy sensor frames.
            self.task_state.update(self.stage_snapshot(), arm, self.perception_snapshot(), now_perf=time.perf_counter())
        self.recovery_snapshot()  # Expire abandoned session metadata before observing.
        self.recovery.observe(self.stage_snapshot(), arm, now_perf=time.perf_counter())

    def _observe_stage(self):
        # Geometry/image submissions stay at 2Hz. A lightweight metadata poll
        # consumes each completed neural result inside its original image TTL.
        next_image = 0.
        consumed = None
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= next_image:
                    self._observe_stage_once()
                    self.stage_observer_error = None
                    next_image = time.monotonic() + .5
                observer = getattr(self, "perception", None)
                perception = observer.snapshot() if observer is not None else {}
                neural = perception.get("neural")
                key = (neural.get("task_session_id"), neural.get("sequence")) if isinstance(neural, dict) else None
                if key is not None and key != consumed:
                    self.task_snapshot()
                    # This RGB-only candidate retains its own source stamps. The
                    # current arm snapshot gates freshness; it is not a joint
                    # input to that earlier neural request or a servo Jacobian.
                    updated = self.task_state.update(self.stage_snapshot(), self.observers["right"].control_snapshot(),
                                                     perception, now_perf=time.perf_counter())
                    if updated.get("model_stage", {}).get("fresh") is True or neural.get("fresh") is not True:
                        consumed = key
            except Exception as exc:
                self.stage_observer_error = repr(exc)
            if self.stop_event.wait(.05):
                break

    def status(self, compact=False):
        cfg = getattr(self, 'cfg', {})
        arms = {}
        for role, observer in self.observers.items():
            state = observer.control_snapshot() if compact else observer.snapshot()
            if compact:
                state.pop("raw", None)
                state["external_commands"] = {cid: value for cid, value in state["external_commands"].items() if value["count"]}
            arms[role] = state
        proc_stat = Path('/proc/self/stat').read_text() if os.name == 'posix' else None
        producer = {"schema": "tiger-hub-producer-identity-v1", "pid": os.getpid(),
                    "proc_start_ticks": proc_stat[proc_stat.rfind(')') + 2:].split()[19] if proc_stat else None}
        return {"schema": "tiger-unified-sensor-hub-v1", "at": now(), "CAN_send_calls": 0,
                "producer": producer,
                **({'storage_binding':hub_storage_binding(cfg)} if cfg.get('usb_selection') else {}),
                "observer_start_errors": dict(getattr(self, 'observer_start_errors', {})),
                "cameras": {key: obj.status() for key, obj in self.cameras.items()},
                "arms": arms,
                "stage_observer": self.stage_snapshot(),
                "recovery": self.recovery_snapshot(),
                "task_state": self.task_snapshot(),
                "perception": self.perception_snapshot(),
                "visual_servo": self.visual_servo_snapshot(),
                "recording": self.recorder.status(), "preview_error": self.preview_error,
                "recording_contract":{"collection_metadata_version":1,"expected_stop_path_supported":True,
                    "capture_contract": deepcopy(getattr(self, 'capture_contract', None))},
                "camera_capture_requested_hz": 30, "rerun_image_display_max_hz": 5,
                "single_camera_owner": True}

    def start_recording(self, data):
        """Bind a batch identity BEFORE capture, and replay a lost response safely."""
        allowed={'task','batch_id','trial_id','category','notes'}
        if not isinstance(data,dict) or set(data)-allowed:
            raise ValueError('Unsupported recording request fields')
        with self.record_request_lock:
            if 'batch_id' not in data:
                if set(data)-{'task'}:
                    raise ValueError('Batch metadata requires a batch ID')
                return self.recorder.start(data.get('task','unspecified'))
            from .collection import CollectionStore
            store=CollectionStore(self.cfg)
            metadata=store.prepare_start(data['batch_id'],data.get('trial_id'),data.get('category'),data.get('notes',''))
            if data.get('task') != metadata['task']:
                raise ValueError('Task differs from frozen batch task')
            existing=store.trial_status(metadata).get('episode_path')
            if existing:
                current=self.recorder.status()
                if current.get('path')==existing:
                    return {**current,'start_idempotent_replay':True}
                report=json.loads((Path(existing)/'operation.json').read_text(encoding='utf-8'))
                if report.get('collection_v1')!=metadata or report.get('raw_storage')!=existing:
                    raise RuntimeError('Existing trial receipt identity changed; no second capture')
                from .capture_profile import receipt_contract
                frozen_capture = receipt_contract(self.cfg, report)
                return {'path':existing,'episode_id':Path(existing).name,'active':False,
                    'status':report.get('status'),'collection_v1':metadata,
                    'capture_profile': frozen_capture['profile'], 'capture_contract': frozen_capture,
                    'finalized':store._finalized(report),
                    'samples':report.get('samples'),'elapsed_s':report.get('elapsed_s'),
                    'start_idempotent_replay':True,'quality_summary':report.get('quality_summary')}
            try:
                return self.recorder.start(metadata['task'],collection_v1=metadata,
                    on_created=lambda path:store.bind_episode(metadata,path))
            except Exception as exc:
                try:
                    store.failed_start(metadata,exc)
                except Exception as bookkeeping_error:
                    exc.add_note('Collection failure event could not be saved: '+repr(bookkeeping_error))
                raise

    def record_frames(self):
        result = {}
        for role, camera in self.cameras.items():
            frame = camera.frame()
            result[role] = None if frame is None else {
                key: deepcopy(frame[key]) if key == "source_timing" else frame[key]
                for key in ("wall_ns", "jpeg", "source_timing", "frame_id", "available_monotonic_ns")
                if key in frame}
        return result

    def record_state(self):
        """Compact real state; full gap/counter diagnostics remain in /status."""
        keys = ("q_deg", "grip_m", "age_joint_s", "age_status_s", "age_gripper_s", "age_motor_s",
                "status_hex", "motor_flags", "enabled", "feedback_fresh", "action_targets",
                "socket_rx_overflow_packets", "error_frames", "timestamp_failures",
                "observer_running", "observer_error", "stale_after_s")
        stage = self.stage_snapshot()
        # Reuse the existing buffered recorder. Do not create a second log writer.
        stage_record = {key: stage[key] for key in ("schema", "phase", "label", "task_result",
                        "source", "fresh", "age_s", "sample_count", "source_timestamps")}
        recovery = self.recovery_snapshot()
        task = self.task_snapshot()
        perception = self.perception_snapshot()
        result = {"arms": {role: {**{key: value[key] for key in keys},
                                 **({"source_timing": deepcopy(value["source_timing"])}
                                    if "source_timing" in value else {})} for role, obj in self.observers.items()
                         for value in [obj.control_snapshot()]},
                "stage_observer": stage_record,
                "recovery": {key: recovery.get(key) for key in ("schema", "state", "candidate", "mode",
                    "deployment_active", "fresh", "age_s", "retry_count", "commands_sent")},
                "task_state": {key: task.get(key) for key in ("schema", "session_id", "revision", "phase", "results", "fresh", "age_s", "perception")},
                "perception": self._compact_perception(perception),
                "camera_status": {role: obj.status() for role, obj in self.cameras.items()}}
        if getattr(self, "clock_domain", None) is not None:
            result["clock_domain"] = deepcopy(self.clock_domain)
        return result

    @staticmethod
    def _compact_perception(perception):
        """Small provenance/probability fields only; no masks or image copies."""
        result = {key: perception.get(key) for key in
                  ("schema", "status", "fresh", "age_s", "sequence", "source_stamps")}
        neural = perception.get("neural")
        if isinstance(neural, dict):
            result["neural"] = {key: neural.get(key) for key in
                ("sequence", "task_session_id", "context_generation", "source_stamps", "age_s", "source_image_age_s", "fresh",
                 "matches_request_source", "matches_latest_source")}
            for head in ("classifier", "placement"):
                output = neural.get(head)
                if not isinstance(output, dict):
                    result["neural"][head] = None
                    continue
                body = output.get("response")
                body = body if isinstance(body, dict) else {}
                result["neural"][head] = {"status": output.get("status"),
                    "model_id": body.get("model_id"), "probabilities": body.get("probabilities"),
                    "validation": body.get("validation"), "source_stamps": body.get("source_stamps")}
        return result

    def observation(self, alignment=None, *, wait_ms=None, deadline_monotonic_ns=None):
        return self.observation_for_mode(alignment, wait_ms=wait_ms, deadline_monotonic_ns=deadline_monotonic_ns)

    def observation_for_mode(self, alignment=None, *, wait_ms=None, deadline_monotonic_ns=None):
        """An explicit query override allows read-only A/B without config edits."""
        mode = (getattr(self, 'cfg', {}).get('observation_alignment_mode', 'legacy')
                if alignment is None else alignment)
        if mode == 'legacy':
            if wait_ms not in (None, 0) or deadline_monotonic_ns is not None:
                raise ValueError('Bounded requery is only supported for query_grid')
            return self._legacy_observation()
        if mode == 'query_grid':
            return self._aligned_observation(wait_ms=wait_ms, deadline_monotonic_ns=deadline_monotonic_ns)
        raise ValueError('observation_alignment_mode must be legacy or query_grid')

    def alignment_shadow(self, *, wait_ms=None, deadline_monotonic_ns=None):
        alignment = getattr(self, 'alignment', None)
        if alignment is None:
            return {'schema': 'tiger-runtime-alignment-v1', 'ready': False,
                    'failures': [{'code': 'RUNTIME_NOT_STARTED', 'streams': [],
                                  'detail': 'No capture runtime exists'}],
                    'physical_deployment_ready': False, 'diagnostics': None}
        budget = getattr(self, 'cfg', {}).get('alignment_wait_ms', 0) if wait_ms is None else wait_ms
        _samples, receipt = alignment.select('shadow', wait_ms=budget, deadline_monotonic_ns=deadline_monotonic_ns)
        return {**receipt, 'diagnostics': alignment.diagnostics()}

    def _aligned_observation(self, *, wait_ms=None, deadline_monotonic_ns=None):
        import cv2
        import numpy as np
        alignment = getattr(self, 'alignment', None)
        if alignment is None:
            raise RuntimeError('query_grid alignment runtime is unavailable')
        if self.clock_domain != alignment.domain_record:
            raise RuntimeError('Capture clock domain changed after alignment startup')
        # Preserve the existing live-source health guards in addition to the
        # tighter candidate timing contract. These latest values are health
        # evidence only; the policy state below is decoded from selected bytes.
        state = self.observers['right'].snapshot()
        if not state['fresh']:
            raise RuntimeError('Right joint feedback is stale')
        if state.get('source_callback_error'):
            raise RuntimeError('Right feedback alignment publication failed: ' + state['source_callback_error'])
        for role in ('front', 'right'):
            camera_status = self.cameras[role].status()
            if not camera_status.get('fresh') or camera_status.get('alignment_publication_error'):
                raise RuntimeError('Model camera stale/missing or alignment publication failed: ' + role)
        budget = getattr(self, 'cfg', {}).get('alignment_wait_ms', 0) if wait_ms is None else wait_ms
        selected, receipt = alignment.select('observation', wait_ms=budget, deadline_monotonic_ns=deadline_monotonic_ns)
        if not receipt['ready']:
            failure = RuntimeError('query_grid selection failed: ' + ','.join(item['code'] for item in receipt['failures']))
            failure.alignment_query_evidence = receipt
            raise failure
        query_evidence = {'query_retry': deepcopy(receipt['query_retry']), 'attempts': deepcopy(receipt['attempts'])}
        compact_receipt = {key: value for key, value in receipt.items() if key not in ('attempts', 'latest_sources')}
        vector = alignment.state_vector(selected)
        frames, stamps, clocks, ages = {}, {}, {}, {}
        for role in ('front', 'right'):
            sample = selected[role]
            original = np.frombuffer(sample.payload.rgb_bytes, dtype=np.uint8).reshape(sample.payload.shape)
            rgb = resize_pad_rgb(original)
            ok, png = cv2.imencode('.png', cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            if not ok:
                raise RuntimeError('PNG encode failed')
            frames[role + '_png224_b64'] = base64.b64encode(png.tobytes()).decode('ascii')
            stamps[role] = sample.payload.wall_ns
            clocks[role] = sample.source_ns / 1e9
            ages[role] = receipt['selection_age_ns'][role] / 1e9
        packet_ready = time.monotonic()
        packet_ns = round(packet_ready * 1e9)
        stamps['timing_v1'] = {
            'schema': 'tiger-observation-timing-v1', 'clock_domain': deepcopy(self.clock_domain),
            'selection_mode': 'query_grid', 'alignment_applied': True,
            'selection_anchor_monotonic_ns': receipt['anchor_ns'],
            'observation_acquired_monotonic_ns': receipt['anchor_ns'],
            'packet_ready_monotonic_ns': packet_ns,
            'packet_ready_clock_api': 'time.monotonic_seconds_converted_to_ns',
            'sources': alignment.compact_sources(selected), 'selection_receipt': compact_receipt}
        return {**frames, 'state_right_rad_m': vector, 'source_timestamps': stamps,
                'observation_monotonic': packet_ready,
                'observation_acquired_monotonic': receipt['anchor_ns'] / 1e9,
                'source_frame_monotonic': clocks, 'source_frame_age_s': ages,
                'feedback': state, 'wall_ns': time.time_ns(), 'alignment_query_evidence': query_evidence}

    def _legacy_observation(self):
        import cv2
        import math
        acquired_at = time.monotonic()
        acquired_ns = time.monotonic_ns()
        state = self.observers["right"].snapshot()
        if not state["fresh"]:
            raise RuntimeError("Right joint feedback is stale")
        if type(state.get('grip_m')) not in (int,float) or not math.isfinite(state['grip_m']):
            raise RuntimeError('Model requires measured gripper linear width; angle/unknown mode is unsupported')
        frames = {}
        stamps = {}
        source_clocks = {}
        source_ages = {}
        source_metadata = deepcopy(state.get("source_timing", {}))
        for role in ("front", "right"):
            frame = self.cameras[role].frame()
            if frame is None:
                raise RuntimeError("Model camera stale/missing: " + role)
            # Explicit source time, separate from the existing RTC queue clock.
            # Map perf_counter to monotonic without assuming identical origins.
            timing = frame.get("source_timing")
            source_metadata[role] = deepcopy(timing) if isinstance(timing, dict) else None
            cached_source_ns = timing.get("source_monotonic_ns_estimate") if isinstance(timing, dict) else None
            stamp = frame.get("stamp_perf")
            source_age = ((time.monotonic_ns() - cached_source_ns) / 1e9 if type(cached_source_ns) is int
                          else None if stamp is None else time.perf_counter() - stamp)
            source_ages[role] = source_age
            source_clocks[role] = (cached_source_ns / 1e9 if type(cached_source_ns) is int
                                  else None if source_age is None else time.monotonic() - source_age)
            # Preserve the training image geometry before lossless PNG transport.
            rgb = resize_pad_rgb(frame["rgb"])
            ok, png = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            if not ok:
                raise RuntimeError("PNG encode failed")
            frames[role + "_png224_b64"] = base64.b64encode(png.tobytes()).decode("ascii")
            stamps[role] = frame["wall_ns"]
        packet_ready = time.monotonic()
        packet_ready_ns = round(packet_ready * 1e9)
        domain = getattr(self, "clock_domain", None)
        if domain is not None:
            stamps["timing_v1"] = {
                "schema": "tiger-observation-timing-v1", "clock_domain": deepcopy(domain),
                "selection_mode": "independent_latest", "alignment_applied": False,
                "selection_anchor_monotonic_ns": None,
                "observation_acquired_monotonic_ns": acquired_ns,
                "packet_ready_monotonic_ns": packet_ready_ns,
                "packet_ready_clock_api": "time.monotonic_seconds_converted_to_ns",
                "sources": {key: source_metadata.get(key) for key in
                            ("front", "right", "q12", "q34", "q56", "gripper")}}
        return {**frames, "state_right_rad_m": [math.radians(x) for x in state["q_deg"]] + [state["grip_m"]],
                "source_timestamps": stamps, "observation_monotonic": packet_ready,
                "observation_acquired_monotonic": acquired_at,
                "source_frame_monotonic": source_clocks, "source_frame_age_s": source_ages,
                "feedback": state, "wall_ns": time.time_ns()}

    def _preview(self):
        rr = None
        try:
            import rerun as rr
            import rerun.blueprint as rrb
            rr.init("tiger_agilex_unified", spawn=False, strict=True)
            rr.connect_grpc(self.cfg["rerun_proxy"])
            images = [rrb.Spatial2DView(name=role + " RGB", origin="/cameras/" + role) for role in self.cameras]
            rr.send_blueprint(rrb.Blueprint(rrb.Vertical(rrb.Horizontal(*images),
                rrb.Horizontal(*[rrb.TimeSeriesView(name=role + " joints (deg)", origin="/arms/" + role) for role in self.observers]),
                rrb.TextDocumentView(name="Health / recording", origin="/status")), auto_layout=False, auto_views=False))
            tick = 0
            while not self.stop_event.wait(.1):
                rr.set_time("sensor_wall_time", timestamp=time.time())
                if tick % 2 == 0:
                    for role, camera in self.cameras.items():
                        frame = camera.frame()
                        rr.log("cameras/" + role, rr.Clear(recursive=True) if frame is None else rr.Image(frame["rgb"]).compress(jpeg_quality=75))
                for role, observer in self.observers.items():
                    state = observer.control_snapshot()
                    if state["fresh"]:
                        for axis, angle in enumerate(state["q_deg"], 1):
                            rr.log(f"arms/{role}/J{axis}", rr.Scalars(angle))
                        rr.log(f"arms/{role}/gripper_m", rr.Scalars(state["grip_m"]))
                if tick % 10 == 0:
                    rr.log("status", rr.TextDocument(json.dumps(self.status(), indent=2)))
                tick += 1
        except Exception as exc:
            self.preview_error = repr(exc)
        finally:
            if rr is not None:
                try:
                    rr.disconnect()
                except Exception as exc:
                    self.preview_error = (self.preview_error + "; " if self.preview_error else "") + "Cleanup: " + repr(exc)

    def _ingest_can_source(self, role, cid, payload, source_timing):
        """Publish one original receipt to recorder and existing alignment.

        The recorder copies the integer kernel timestamp, not the legacy
        floating-point monotonic estimate. No additional CAN reader is opened.
        """
        self.recorder.enqueue_can_source(role, cid, payload, source_timing)
        if role == 'right':
            self.alignment.ingest_can(cid, payload, source_timing)

    def _start_observers(self):
        """Keep excluded observers visible; their absence cannot invent feedback."""
        started = []
        for role, observer in self.observers.items():
            try:
                observer.start(callback=None,
                    source_callback=lambda cid, payload, evidence, role=role:
                        self._ingest_can_source(role, cid, payload, evidence))
                started.append(observer)
            except Exception as exc:
                self.observer_start_errors[role] = repr(exc)
                cleanup = [*started, observer] if role in self.capture_contract['required_arm_roles'] else [observer]
                cleanup_failed = False
                for owned in cleanup:
                    try:
                        owned.stop()
                    except Exception as close_error:
                        cleanup_failed = True
                        exc.add_note('Owned observer cleanup failed: ' + repr(close_error))
                if role in self.capture_contract['required_arm_roles'] or cleanup_failed:
                    raise

    def run(self):
        verify_usb(self.cfg)
        for name, version in (("lerobot", "0.5.2"), ("rerun-sdk", "0.26.2")):
            if importlib.metadata.version(name) != version:
                raise RuntimeError("Wrong capture package version: " + name)
        session = Session(self.cfg, "sensor-hub")
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass  # No per-frame/access debug disk logging.

            def reply(self, code, payload, mime="application/json"):
                if isinstance(payload,dict) and hub.cfg.get('usb_selection'):
                    payload = {**payload,'storage_binding':hub_storage_binding(hub.cfg)}
                if not isinstance(payload, bytes):
                    payload = json.dumps(payload, allow_nan=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                try:
                    request = urlparse(self.path)
                    route = request.path
                    if route == "/status":
                        return self.reply(200, hub.status(compact=parse_qs(request.query).get("compact") == ["1"]))
                    if route == "/observation/fp32":
                        from .hub_fp32 import build_snapshot
                        return self.reply(200, build_snapshot(hub))
                    if route == "/recording/fp32-binding":
                        from .hub_fp32 import recording_binding
                        return self.reply(200, recording_binding(hub))
                    if route == "/observation":
                        query = parse_qs(request.query)
                        choices = query.get('alignment', [])
                        if len(choices) > 1:
                            raise ValueError('Only one observation alignment override is allowed')
                        options = self.alignment_options(query)
                        return self.reply(200, hub.observation_for_mode(choices[0] if choices else None, **options))
                    if route == '/alignment/shadow':
                        return self.reply(200, hub.alignment_shadow(**self.alignment_options(parse_qs(request.query))))
                    if route.startswith("/image/"):
                        camera = hub.cameras[route.removeprefix("/image/")]
                        frame = camera.frame()
                        if frame is None:
                            raise RuntimeError("Real frame not fresh")
                        return self.reply(200, frame["jpeg"], "image/jpeg")
                    self.reply(404, {"error": "Unknown sensor route"})
                except Exception as exc:
                    payload = {"error": repr(exc)}
                    if hasattr(exc, 'alignment_query_evidence'):
                        payload['alignment_query_evidence'] = exc.alignment_query_evidence
                    self.reply(503, payload)

            @staticmethod
            def alignment_options(query):
                options = {}
                for key, converter in (('wait_ms', float), ('deadline_monotonic_ns', int)):
                    values = query.get(key, [])
                    if len(values) > 1:
                        raise ValueError('Only one ' + key + ' is allowed')
                    if values:
                        options[key] = converter(values[0])
                return options

            def do_POST(self):
                try:
                    if self.headers.get("X-Tiger-Local") != "1":
                        raise RuntimeError("Explicit local capture client required")
                    self.path = storage_bound_route(hub.cfg,self.path,self.headers.get('X-Tiger-Storage-Binding'))
                    count = int(self.headers.get("Content-Length", 0))
                    if not 0 <= count <= 4096:
                        raise ValueError("Request too large")
                    data = json.loads(self.rfile.read(count) or b"{}")
                    if self.path in ("/recovery/context", "/recovery/heartbeat"):
                        return self.reply(200, hub.recovery_context(data, heartbeat=self.path.endswith("heartbeat")))
                    if self.path == "/task/event":
                        return self.reply(200, hub.task_event(data))
                    if self.path == "/task/heartbeat":
                        return self.reply(200, hub.task_heartbeat(data))
                    if self.path == "/task/review":
                        return self.reply(200, hub.task_review(data))
                    if self.path == "/record/start":
                        return self.reply(200, hub.start_recording(data))
                    if self.path == "/record/stop":
                        return self.reply(200, hub.recorder.stop(**({'expected_path':data['expected_path']} if 'expected_path' in data else {})))
                    self.reply(404, {"error": "No robot-control route exists here"})
                except Exception as exc:
                    self.reply(409, {"error": repr(exc)})

        server = preview = stage_worker = None
        old, attempted_cameras, cleanup_errors = {}, [], []
        observers_attempted, primary = False, None
        try:
            # Reserve the sole-owner endpoint before opening any sensor source.
            server = ThreadingHTTPServer(("127.0.0.1", self.cfg["hub_port"]), Handler)
            server.timeout = .25
            self.clock_domain = capture_clock_domain()
            for producer in (*self.cameras.values(), *self.observers.values()):
                producer.clock_domain = deepcopy(self.clock_domain)
            self.alignment = RuntimeAlignment(self.clock_domain, self.cfg.get('observation_alignment'))
            for role, camera in self.cameras.items():
                if role in ('front', 'right'):
                    camera.publication_callback = self.alignment.ingest_camera
            session.report["clock_domain"] = deepcopy(self.clock_domain)
            session.save()
            observers_attempted = True
            self._start_observers()
            session.report['observer_start_errors'] = dict(self.observer_start_errors)
            for camera in self.cameras.values():
                attempted_cameras.append(camera)
                camera.thread.start()
            preview = threading.Thread(target=self._preview, name="rerun-publish", daemon=True)
            preview.start()
            stage_worker = threading.Thread(target=self._observe_stage, name="read-only-stage-observer", daemon=True)
            stage_worker.start()
            for sig in (signal.SIGINT, signal.SIGTERM):
                old[sig] = signal.signal(sig, lambda *_: self.stop_event.set())
            while not self.stop_event.is_set():
                server.handle_request()
        except BaseException as exc:
            primary = exc
            raise
        finally:
            self.stop_event.set()

            def cleanup(label, operation):
                try:
                    operation()
                except BaseException as exc:
                    cleanup_errors.append((label, exc))

            cleanup('recorder stop', self.recorder.stop)
            for camera in attempted_cameras:
                cleanup('camera close ' + camera.role, camera.close)
            if observers_attempted:
                for observer in self.observers.values():
                    cleanup('observer stop ' + observer.interface, observer.stop)
            def join_worker(worker):
                worker.join(3)
                if worker.is_alive():
                    raise RuntimeError('Hub worker still stopping: ' + worker.name)

            for worker in (preview, stage_worker):
                if worker is not None and worker.ident is not None:
                    cleanup('worker join ' + worker.name, lambda worker=worker: join_worker(worker))
            cleanup('perception close', self.perception.close)
            if server is not None:
                cleanup('server close', server.server_close)
            for sig, handler in old.items():
                cleanup('signal restore ' + str(sig), lambda sig=sig, handler=handler: signal.signal(sig, handler))
            sensors = None
            try:
                sensors = self.status()
            except BaseException as exc:
                cleanup_errors.append(('final status snapshot', exc))
            failure = primary or (cleanup_errors[0][1] if cleanup_errors else None)
            cleanup('session finalize', lambda: session.finish(
                'SENSOR_HUB_FAILED' if failure is not None else 'SENSOR_HUB_STOPPED',
                sensors=sensors, failure=repr(failure) if failure is not None else None,
                cleanup_errors=[{'step': label, 'error': repr(exc)} for label, exc in cleanup_errors]))
            if primary is not None:
                for label, exc in cleanup_errors:
                    primary.add_note(label + ': ' + repr(exc))
            elif cleanup_errors:
                raise cleanup_errors[0][1]


def rpc(cfg, path, data=None, timeout=20):
    import urllib.request
    binding = hub_storage_binding(cfg)
    headers={"Content-Type": "application/json", "X-Tiger-Local": "1"}
    if binding is not None:
        headers['X-Tiger-Storage-Binding'] = storage_binding_token(binding)
        if data is not None:
            # Old Hubs do not recognize this route, so a late/foreign old Hub
            # cannot silently start recording to the former USB.
            path = STORAGE_RPC_PREFIX + path
    url = f"http://127.0.0.1:{cfg['hub_port']}" + path
    request = urllib.request.Request(url, data=None if data is None else json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8'),
        headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return validate_hub_storage(cfg,json.load(response))
