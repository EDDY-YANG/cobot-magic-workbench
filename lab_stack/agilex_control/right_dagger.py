# Copyright (c) 2026 Yang Taige
# SPDX-License-Identifier: MIT
"""Right-side scheme-one takeover using independent CAN branches and existing writers."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import threading
import time
import urllib.request
import uuid

from .capture_profile import RIGHT, contract
from .hardware import (DOCUMENTED_COMMAND_LIMITS_DEG, HardwareBlocked, PiperAdapter,
                       require_feedback, require_gripper_feedback, validate_target_deg)
from .sensors import CanObserver, adapter_identity
from .recording_lifecycle import preserving_rpc, stop_owned_recording
from .storage import Ownership, Session, stop_requested, verify_usb
from .tx_evidence import TxEvidenceWriter

SCHEMA = "right-dagger-scheme1-v1"
SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
JOINT_COMMANDS = (0x155, 0x156, 0x157)
ID_PATTERN = re.compile(r"[a-f0-9]{12,32}")
FINISHED = {"idle", "saved", "ended", "fault"}
MODEL_PHASES = {"policy", "waiting_policy"}
LOOP_PERIOD_S = .005


def reserved_can_roles(cfg):
    """Keep a borrowed adapter out of its former arm's live control view."""
    profile = cfg.get("right_dagger") or {}
    serial = (profile.get("rear") or {}).get("expected_serial")
    if profile.get("enabled") is not True or not serial:
        return set()
    return {role for role, entry in cfg.get("can", {}).items()
            if role != "right" and entry.get("expected_serial") == serial}


def finite_vector(value, count):
    return (isinstance(value, (list, tuple)) and len(value) == count
            and all(type(x) in (int, float) and math.isfinite(x) for x in value))


def command_input(observer, after, local_origin, now=None, previous_joint_stamps=None):
    """Read coherent post-transition command frames with kernel-derived receipt times."""
    now = time.monotonic() if now is None else now
    result = {}
    frames = [observer.latest_frame(cid) for cid in JOINT_COMMANDS]
    if (all(f is not None and after < f[0] <= now and now - f[0] <= .2 for f in frames)
            and max(f[0] for f in frames) - min(f[0] for f in frames) <= .05
            and all(observer.latest_origin(cid) is local_origin for cid in JOINT_COMMANDS)
            and (previous_joint_stamps is None or
                 all(f[0] > old for f, old in zip(frames, previous_joint_stamps)))):
        import struct
        result["q_deg"] = [v / 1000 for f in frames for v in struct.unpack(">ii", f[1])]
        result["joint_stamp"] = min(f[0] for f in frames)
        result["joint_stamps"] = [f[0] for f in frames]
    gripper = observer.latest_frame(0x159)
    if (gripper and after < gripper[0] <= now and now - gripper[0] <= .2
            and observer.latest_origin(0x159) is local_origin
            and len(gripper[1]) == 8 and gripper[1][6] in (0, 1, 2, 3) and gripper[1][7] == 0):
        result["grip_m"] = int.from_bytes(gripper[1][:4], "big", signed=True) / 1e6
        result["grip_stamp"] = gripper[0]
    return result


def aligned(rear, front, joint_tolerance=2., gripper_tolerance=.002):
    if not finite_vector(rear.get("q_deg"), 6) or not finite_vector(front.get("q_deg"), 6):
        raise HardwareBlocked("Both arms need measured joint positions")
    if not finite_vector([rear.get("grip_m"), front.get("grip_m")], 2):
        raise HardwareBlocked("Both arms need measured gripper openings")
    q_error = max(abs(a-b) for a, b in zip(rear["q_deg"], front["q_deg"]))
    g_error = abs(rear["grip_m"]-front["grip_m"])
    if q_error > joint_tolerance or g_error > gripper_tolerance:
        raise HardwareBlocked(f"Right pair is not aligned: {q_error:.3f} deg / {g_error*1000:.2f} mm")
    return {"joint_error_deg": q_error, "gripper_error_m": g_error}


def validate_record_context(cfg, data):
    if set(data) != {"task", "dagger_context"} or not isinstance(data["task"], str):
        raise ValueError("Invalid right DAgger recording request")
    context = data["dagger_context"]
    fields = {"schema", "policy_job_id", "control_session", "expert_log", "side"}
    if (not isinstance(context, dict) or set(context) != fields or context["schema"] != SCHEMA
            or context["side"] != "right"
            or any(not isinstance(context[k], str) or not ID_PATTERN.fullmatch(context[k])
                   for k in ("policy_job_id", "control_session"))):
        raise ValueError("Invalid correction identity")
    if contract(cfg)["profile"] != RIGHT:
        raise ValueError("Right DAgger requires the right-only front/right camera capture profile")
    root = verify_usb(cfg)
    log = Path(context["expert_log"])
    if not log.is_absolute() or not log.resolve().is_relative_to(root.resolve()) or not log.is_file():
        raise ValueError("Expert evidence is missing from the verified USB")
    owner = json.loads((log.parent / "operation.json").read_text())
    if (owner.get("schema") != SCHEMA or owner.get("control_session") != context["control_session"]
            or owner.get("policy_job_id") != context["policy_job_id"]):
        raise ValueError("Expert evidence belongs to another control or policy session")
    return deepcopy(context)


class RightDaggerService:
    """One serialized device owner. Construction and status contain no CAN commands."""

    def __init__(self, cfg, front_observer, capture_state=None):
        self.cfg = deepcopy(cfg)
        self.front_observer = front_observer
        self.capture_state = capture_state
        self.profile = deepcopy(cfg.get("right_dagger") or {})
        self.enabled = self.profile.get("enabled") is True
        self.lock = threading.RLock()
        self.commands = queue.Queue(maxsize=8)
        self.closing = threading.Event()
        self.state = {"schema": SCHEMA, "phase": "idle", "enabled": self.enabled,
                      "binding": None, "policy_job_id": None, "error": None,
                      "source_sha256": SOURCE_SHA, "hardware_validated": False}
        self.rear = None
        self.thread = None
        self.resources = None
        self.front_lock = None
        self.heartbeats = set()
        self.rpc_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dagger-capture-rpc')
        self.rear_writer = self.front_writer = None
        self.session = self.log = None
        self.record_path = None
        self.after = 0.
        self.last_input = {}
        self.last_command = {}
        self.recent_events = deque(maxlen=64)
        self.identity_checked_at = 0.
        self.identities = {}
        if self.enabled:
            self._validate_profile()
            self.identities = self._identities()
            self.rear = CanObserver(**self.profile["rear"]).start()
            self.thread = threading.Thread(target=self._run, name="right-dagger-owner", daemon=True)
            self.thread.start()

    def _validate_profile(self):
        rear = self.profile.get("rear", {})
        front = self.cfg["can"]["right"]
        if (set(rear) - {"interface", "expected_serial", "stale_after"}
                or not rear.get("expected_serial") or not rear.get("interface")
                or rear["interface"] == front["interface"]
                or rear["expected_serial"] == front["expected_serial"]):
            raise ValueError("Two explicitly identified independent CAN adapters are required")
        if self.profile.get("physical_isolation_confirmed") is not True:
            raise ValueError("Independent right-arm wiring must be confirmed in the installation")
        for key, default, low, high in (("joint_alignment_deg", 2., .1, 2.),
                                      ("gripper_alignment_m", .002, .0001, .002),
                                      ("tracking_error_deg", 10., 1., 15.),
                                      ("manual_speed_deg_s", 30., 1., 60.),
                                      ("manual_gripper_speed_m_s", .04, .001, .1)):
            value = self.profile.get(key, default)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("Unsupported DAgger engineering bound: " + key)
            self.profile[key] = float(value)
        if contract(self.cfg)["profile"] != RIGHT:
            raise ValueError("Right-only capture profile required")

    def _identities(self):
        result = {}
        for side, item in (("rear", self.profile["rear"]), ("front", self.cfg["can"]["right"])):
            result[side] = adapter_identity(item["interface"], item["expected_serial"])
        return result

    def _front(self):
        observer = self.front_observer()
        if not isinstance(observer, CanObserver):
            raise HardwareBlocked("Existing right-arm observer is unavailable")
        return observer

    def _check(self):
        if self.closing.is_set() or stop_requested(self.cfg):
            raise HardwareBlocked("STOP or connection shutdown requested")
        now = time.monotonic()
        if now - self.identity_checked_at >= .25:
            if self._identities() != self.identities:
                raise HardwareBlocked("Physical CAN identity changed")
            self.identity_checked_at = now
        if self.log and self.log.status().get("writer_error"):
            raise HardwareBlocked("Expert USB evidence writer failed")

    def _heartbeat(self):
        for side in tuple(self.heartbeats):
            writer = self.front_writer if side == "front" else self.rear_writer
            if writer is None:
                continue
            writer.feedback(require_can=True)
            writer.keep_control_mode()

    def _hub(self, path="/status?compact=1", payload=None, *, maintain=True, timeout=8):
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.cfg['hub_port']}" + path,
                                     data=data, headers={"Content-Type": "application/json"})
        def request():
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        future = self.rpc_executor.submit(request)
        heartbeat_error = None
        while True:
            try:
                result = future.result(timeout=.005)
                break
            except TimeoutError:
                if future.done():
                    raise
                if maintain and heartbeat_error is None:
                    try:
                        self._heartbeat()
                    except Exception as exc:
                        heartbeat_error = exc
        if path == "/record/start" and result.get("right_dagger") == payload.get("dagger_context") and result.get("path"):
            self.record_path = result["path"]
        if heartbeat_error:
            raise heartbeat_error
        return result

    def _set(self, phase, **fields):
        with self.lock:
            self.state.update(phase=phase, **fields)
        if self.session:
            self.session.report.update(phase=phase, **deepcopy(fields))
            try:
                self.session.save()
            except Exception as exc:
                with self.lock:
                    self.state["receipt_error"] = str(exc)
                if phase != "fault":
                    raise

    def snapshot(self):
        with self.lock:
            result = deepcopy(self.state)
        result["arms"] = {}
        for name, observer in (("rear", self.rear), ("front", self.front_observer())):
            if isinstance(observer, CanObserver):
                snap = observer.control_snapshot()
                result["arms"][name] = {k: snap.get(k) for k in
                    ("q_deg", "grip_m", "status_hex", "fresh", "feedback_fresh", "age_joint_s",
                     "age_gripper_s", "enabled", "identity", "observer_error")}
            else:
                result["arms"][name] = {}
        result["engineering_bounds"] = {k: self.profile.get(k) for k in
            ("joint_alignment_deg", "gripper_alignment_m", "tracking_error_deg",
             "manual_speed_deg_s", "manual_gripper_speed_m_s")}
        return result

    def request(self, action, binding=None, **data):
        allowed = {"prepare", "bind_policy", "intervene", "policy_finished", "finish", "resume"}
        if not self.enabled or action not in allowed:
            raise HardwareBlocked("Right-side DAgger is not configured")
        with self.lock:
            phase = self.state["phase"]
            if action == "prepare":
                if set(data) != {"onsite_confirmed"} or data["onsite_confirmed"] is not True:
                    raise ValueError("Preparation button must confirm supported, clear independent right-arm wiring")
                if phase == "preparing":
                    return self.snapshot()
                if phase not in FINISHED:
                    raise HardwareBlocked("Finish the current right-side session first")
                self.state.update(binding=uuid.uuid4().hex, phase="preparing", error=None,
                                  policy_job_id=None, episode=None, last_episode=None,
                                  human_inputs=0, native_record_saved=False)
            else:
                if binding != self.state["binding"]:
                    raise HardwareBlocked("Right-side control session changed")
                if action == "bind_policy":
                    if set(data) != {"job_id", "task"}:
                        raise ValueError("Invalid policy request")
                    if phase == "policy" and self.state["policy_job_id"] == data["job_id"] and self.state["task"] == data["task"]:
                        return self.snapshot()
                    if phase != "ready":
                        raise HardwareBlocked("Prepare the right pair before launching this model task")
                    if not ID_PATTERN.fullmatch(str(data["job_id"])) or not isinstance(data["task"], str):
                        raise ValueError("Invalid policy binding")
                    self._require_aligned()
                    self.session.report.update(policy_job_id=data["job_id"], task=data["task"])
                    self.session.report.setdefault("policy_history", []).append({
                        "job_id": data["job_id"], "task": data["task"], "bound_monotonic_s": time.monotonic()})
                    self.session.save()
                    self.state.update(phase="policy", policy_job_id=data["job_id"], task=data["task"])
                    self.after = time.monotonic()
                    self.last_input = {}
                    return self.snapshot()
                if action == "intervene":
                    if (set(data) != {"job_id"} or not isinstance(data["job_id"], str)
                            or not ID_PATTERN.fullmatch(data["job_id"])
                            or data["job_id"] != self.state["policy_job_id"]):
                        raise HardwareBlocked("Intervention belongs to another model task")
                    if phase in ("waiting_policy", "taking_control", "human"):
                        return self.snapshot()
                    if phase == "ready":
                        self.state["phase"] = "taking_control"
                        self.commands.put_nowait(("policy_finished", {}))
                        return self.snapshot()
                    if phase != "policy":
                        raise HardwareBlocked("No matching model task to interrupt")
                    self.state["phase"] = "waiting_policy"
                    return self.snapshot()
                if action == "policy_finished":
                    if set(data) != {"job_id"} or data["job_id"] != self.state["policy_job_id"]:
                        raise HardwareBlocked("A different model task finished")
                    if phase == "policy":
                        self.state["phase"] = "ready"
                        self.after = time.monotonic()
                        return self.snapshot()
                    if phase != "waiting_policy":
                        return self.snapshot()
                    self.state["phase"] = "taking_control"
                elif action in ("finish", "resume"):
                    if set(data) != {"job_id"} or data["job_id"] != self.state["policy_job_id"]:
                        raise HardwareBlocked("Finish request belongs to another model task")
                    if phase in ("finishing", "restoring", "saved", "ended"):
                        return self.snapshot()
                    if action == "resume" and phase != "human":
                        raise HardwareBlocked("Human correction is not active")
                    if phase not in ("ready", "human", "fault"):
                        raise HardwareBlocked("Wait for the current handover")
                    self.state["phase"] = "restoring" if action == "resume" else "finishing"
            self.commands.put_nowait((action, deepcopy(data)))
        return self.snapshot()

    def _parameters(self, side):
        entry = self.profile["rear"] if side == "rear" else self.cfg["can"]["right"]
        return {"interface": entry["interface"], "expected_serial": entry["expected_serial"],
                "sdk_version": "0.6.2", "leader_isolated": True, "max_feedback_age_s": .2,
                "native_speed_percent": self.cfg["native_speed_percent"],
                "gripper_bounds_m": self.cfg["gripper_bounds_m"],
                "gripper_effort_mNm": self.cfg["gripper_effort_mNm"],
                "command_limits_deg": DOCUMENTED_COMMAND_LIMITS_DEG}

    def _adapter(self, side, **scope):
        observer = self.rear if side == "rear" else self._front()
        adapter = PiperAdapter(self._parameters(side), observer.control_snapshot,
                               self.recent_events.append, send_guard=self._check)
        adapter.connect_for_apply(**scope)
        return adapter

    def _role(self, role):
        self.heartbeats.discard("rear")
        with self.lock:
            self.state["rear_role_restored"] = False
        if self.rear_writer:
            self.rear_writer.close()
            self.rear_writer = None
        adapter = self._adapter("rear", role_only=True)
        try:
            acknowledgement = adapter.configure_role(role, self.rear.latest_frame, companion_tick=self._heartbeat)
            self.session.event("right_rear_role", **acknowledgement)
            return acknowledgement["ack_monotonic_s"]
        finally:
            adapter.close()

    def _feedback(self, side, enabled=True, can=True):
        observer = self.rear if side == "rear" else self._front()
        self._check()
        return require_feedback(observer.control_snapshot(), require_enabled=enabled, require_can=can)

    def _wait_rear_feedback(self, after):
        deadline = time.monotonic() + 3
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = self._feedback("rear", enabled=False, can=False)
                raw = value.get("raw", {})
                if all(raw.get(hex(cid), {}).get("monotonic_s", 0) > after
                       for cid in (0x2A1, 0x2A5, 0x2A6, 0x2A7, 0x2A8)):
                    return value
            except HardwareBlocked as exc:
                last_error = str(exc)
            self._heartbeat()
            time.sleep(.005)
        raise HardwareBlocked("No fresh follower feedback after role switch: " + str(last_error))

    def _motion_writer(self, side):
        observer = self.rear if side == "rear" else self._front()
        query = self._adapter(side, query_only=True)
        try:
            reply = query.query_limits(observer.latest_frame, companion_tick=self._heartbeat)
        finally:
            query.close()
        bounds = []
        for native, documented in zip(reply["limits"], DOCUMENTED_COMMAND_LIMITS_DEG):
            low, high = max(native["minimum_deg"], documented[0]), min(native["maximum_deg"], documented[1])
            if low >= high:
                raise HardwareBlocked("Controller/documented command bounds do not overlap")
            bounds.append([low, high])
        adapter = self._adapter(side)
        adapter.cfg["command_limits_deg"] = bounds
        self.session.event("command_limits", side=side, reply=reply, bounds=bounds)
        return adapter

    def _require_aligned(self):
        rear = self._feedback("rear")
        front = self._feedback("front", enabled=False, can=False)
        require_gripper_feedback(rear, require_enabled=True)
        require_gripper_feedback(front, require_enabled=False)
        return aligned(rear, front, self.profile["joint_alignment_deg"], self.profile["gripper_alignment_m"])

    def _stationary(self):
        first = self._feedback("front", enabled=False, can=False)["q_deg"]
        deadline = time.monotonic() + .3
        while time.monotonic() < deadline:
            current = self._feedback("front", enabled=False, can=False)["q_deg"]
            if max(abs(a-b) for a,b in zip(first, current)) > .5:
                raise HardwareBlocked("Right front arm is not stationary")
            self._heartbeat()
            time.sleep(.005)

    def _prepare(self):
        self._release()
        self._check()
        verify_usb(self.cfg)
        self._stationary()
        hub = self._hub()
        if hub.get("recording", {}).get("active") or hub.get("recording", {}).get("saving") or hub.get("task_state", {}).get("active"):
            raise HardwareBlocked("Existing capture/task owner is active")
        self.resources = ExitStack()
        self.resources.enter_context(Ownership("writer-" + self.profile["rear"]["interface"]))
        self.session = Session(self.cfg, "right-dagger")
        self.session.report.update(schema=SCHEMA, control_session=self.state["binding"],
                                   source_sha256=SOURCE_SHA, pair=self.identities,
                                   engineering_bounds=deepcopy(self.profile), hardware_validation=False)
        self.session.save()
        self.log = TxEvidenceWriter(self.cfg, self.session.path / "expert-input.ndjson")
        self.log.start()
        if not self.log.status()["opened"]:
            raise HardwareBlocked("Expert USB evidence could not be opened")
        changed = self._role("follower")
        self._wait_rear_feedback(changed)
        self.rear_writer = self._motion_writer("rear")
        self.rear_writer.handoff_can_movej(companion_tick=self._heartbeat)
        measured = self.rear_writer.prime_measured_hold()
        self.rear_writer.enable(companion_tick=self._heartbeat)
        self.rear_writer.send_gripper_m(measured["grip_m"], require_enabled=False)
        self.heartbeats.add("rear")
        target = self._feedback("front", enabled=False, can=False)
        validate_target_deg(target["q_deg"], self.rear_writer.cfg["command_limits_deg"])
        self.rear_writer.execute_pose(target["q_deg"], path_reviewed=True,
            speed_deg_s=min(10., self.cfg["pose_speed_deg_s"]), transmit_hz=200.,
            require_existing_can=True, cancel=lambda: self.closing.is_set() or stop_requested(self.cfg),
            companion_tick=lambda: self._stationary_target(target))
        self._align_gripper(target["grip_m"])
        self._require_aligned()
        self.record_path = None
        self.last_input = {}
        self._set("ready", error=None, episode=None, saved=False, rear_role_restored=True,
                  receipt=str(self.session.path / "operation.json"))

    def _stationary_target(self, target):
        current = self._feedback("front", enabled=False, can=False)
        if max(abs(a-b) for a,b in zip(current["q_deg"], target["q_deg"])) > .5:
            raise HardwareBlocked("Front arm moved while preparing the rear arm")
        return True

    def _align_gripper(self, desired):
        current = self._feedback("rear")["grip_m"]
        if not finite_vector([desired, current], 2):
            raise HardwareBlocked("Fresh measured gripper positions required")
        lo, hi = self.cfg["gripper_bounds_m"]
        if not lo <= desired <= hi:
            raise HardwareBlocked("Front gripper position exceeds rear command bounds")
        started = time.monotonic()
        duration = max(.2, abs(desired-current) / .02)
        while time.monotonic() - started <= duration + 2.:
            elapsed = time.monotonic() - started
            command = current + (desired-current) * min(1., elapsed / duration)
            self.rear_writer.send_gripper_m(command, require_enabled=False)
            actual = self._feedback("rear")["grip_m"]
            if elapsed >= duration and abs(actual-desired) <= self.profile["gripper_alignment_m"]:
                return
            self._heartbeat()
            time.sleep(.005)
        raise HardwareBlocked("Rear gripper did not settle at the front opening")

    def _enter_human(self):
        self._check()
        hub = self._hub()
        recording = hub.get("recording", {})
        if recording.get("active") or recording.get("saving") or hub.get("task_state", {}).get("active"):
            raise HardwareBlocked("Original model recording/task has not finished")
        self.front_lock = Ownership("writer-" + self.cfg["can"]["right"]["interface"])
        self.front_lock.__enter__()
        self._stationary()
        self._require_aligned()
        self.front_writer = self._motion_writer("front")
        self.front_writer.handoff_can_movej(companion_tick=self._heartbeat)
        self._feedback("front")
        self.heartbeats.add("front")
        require_gripper_feedback(self._front().control_snapshot(), require_enabled=True)
        before = self._require_aligned()
        self.after = self._role("leader")
        self.session.event("human_handover", policy_job_id=self.state["policy_job_id"], alignment=before)
        context = {"schema": SCHEMA, "policy_job_id": self.state["policy_job_id"],
                   "control_session": self.state["binding"], "expert_log": str(self.log.path), "side": "right"}
        try:
            result = self._hub("/record/start", {"task": self.state["task"], "dagger_context": context})
        except Exception:
            result = self._hub(maintain=False).get("recording", {})
            if result.get("right_dagger") != context:
                raise
        if result.get("right_dagger") == context and result.get("path"):
            self.record_path = result["path"]
        if result.get("active") is not True or result.get("right_dagger") != context or not result.get("path"):
            raise HardwareBlocked("Correction recording start is unconfirmed")
        self.last_input = {}
        actual = self._feedback("front")
        self.last_command = {"q_deg": actual["q_deg"], "grip_m": actual["grip_m"],
                             "joint_stamp": time.monotonic(), "grip_stamp": time.monotonic()}
        self._set("human", episode=self.record_path, error=None, human_inputs=0)

    def _bounded_human_input(self, sample, now):
        actual = self._feedback("front")
        if "q_deg" in sample:
            delta = max(abs(a-b) for a,b in zip(sample["q_deg"], self.last_command["q_deg"]))
            dt = max(.005, min(.1, now-self.last_command["joint_stamp"]))
            allowed = (self.profile["manual_speed_deg_s"]*dt + .1 if "joint_stamp" in self.last_input
                       else self.profile["joint_alignment_deg"])
            if delta > allowed:
                raise HardwareBlocked("Human joint target rate exceeds the configured limit")
            if max(abs(a-b) for a,b in zip(sample["q_deg"], actual["q_deg"])) > self.profile["tracking_error_deg"]:
                raise HardwareBlocked("Human target is too far from actual front feedback")
        if "grip_m" in sample:
            dt = max(.005, min(.1, now-self.last_command["grip_stamp"]))
            allowed = (self.profile["manual_gripper_speed_m_s"]*dt + .0002 if "grip_stamp" in self.last_input
                       else self.profile["gripper_alignment_m"])
            if abs(sample["grip_m"]-self.last_command["grip_m"]) > allowed:
                raise HardwareBlocked("Human gripper target rate exceeds the configured limit")

    def _tick(self):
        with self.lock:
            phase = self.state["phase"]
        if phase in FINISHED:
            return
        self._check()
        if phase not in MODEL_PHASES | {"human"}:
            if phase == "ready":
                self._heartbeat()
            return
        human = phase == "human"
        if human:
            capture = self.capture_state() if self.capture_state else {}
            recording = capture.get("recording") or {}
            context = recording.get("right_dagger") or {}
            if (capture.get("api_reachable") is not True or capture.get("age_s", 999) > .5
                    or recording.get("active") is not True or recording.get("error")
                    or recording.get("path") != self.record_path
                    or context.get("control_session") != self.state["binding"]):
                raise HardwareBlocked("Correction producer is stale, failed, or belongs to another session")
        source = self.rear if human else self._front()
        sample = command_input(source, self.after, local_origin=not human,
                               previous_joint_stamps=self.last_input.get("joint_stamps"))
        if sample.get("joint_stamp", 0) <= self.last_input.get("joint_stamp", 0):
            sample.pop("q_deg", None)
            sample.pop("joint_stamp", None)
            sample.pop("joint_stamps", None)
        if sample.get("grip_stamp", 0) <= self.last_input.get("grip_stamp", 0):
            sample.pop("grip_m", None)
            sample.pop("grip_stamp", None)
        if not sample:
            self._heartbeat()
            return
        now = time.monotonic()
        target = self.front_writer if human else self.rear_writer
        if human:
            require_gripper_feedback(self._front().control_snapshot(), require_enabled=True)
            self._bounded_human_input(sample, now)
        else:
            rear = self._feedback("rear")
            front = self._feedback("front")
            if max(abs(a-b) for a,b in zip(rear["q_deg"], front["q_deg"])) > self.profile["tracking_error_deg"]:
                raise HardwareBlocked("Rear synchronization exceeded tracking bound")
        intent = {"kind": "expert_input" if human else "policy_mirror",
                  "policy_job_id": self.state["policy_job_id"], "monotonic_s": now,
                  "source_interface": source.interface, "local_origin": not human, **sample}
        if not self.log.enqueue(intent):
            raise HardwareBlocked("Expert evidence queue is full")
        if "q_deg" in sample:
            target.send_joint_target_deg(sample["q_deg"])
            self.last_command.update(q_deg=sample["q_deg"], joint_stamp=now)
        if "grip_m" in sample:
            if "q_deg" not in sample:
                target.keep_control_mode()
            target.send_gripper_m(sample["grip_m"], require_enabled=True)
            self.last_command.update(grip_m=sample["grip_m"], grip_stamp=now)
        self.last_input.update(sample)
        if not self.log.enqueue({"kind": "input_applied", "source_stamps":
                {k: sample[k] for k in ("joint_stamp", "grip_stamp") if k in sample},
                "monotonic_s": time.monotonic(), "policy_job_id": self.state["policy_job_id"]}):
            raise HardwareBlocked("Applied-action evidence could not be queued")
        if human:
            with self.lock:
                self.state["human_inputs"] = self.state.get("human_inputs", 0) + 1
                self.session.report["human_inputs"] = self.state["human_inputs"]


    def _finish_record(self, *, maintain=True):
        if not self.record_path:
            return None
        def raw_rpc(cfg, path, data=None, timeout=20):
            return self._hub(path, data, maintain=maintain, timeout=timeout)
        def rpc(cfg, path, data=None, timeout=20):
            return preserving_rpc(raw_rpc, cfg, path, data, timeout=timeout)
        def wait(seconds):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                if maintain:
                    self._heartbeat()
                time.sleep(min(.005, max(0., until-time.monotonic())))
        final = stop_owned_recording(self.cfg, rpc, self.record_path, timeout=30, sleep=wait)
        if self.session is not None:
            self.session.report["correction_finalization"] = final
        if not (final.get("healthy") is True and final.get("completed") is True
                and final.get("owned_path") == self.record_path):
            raise HardwareBlocked("Correction save is incomplete: " + str(final.get("failure")))
        return {"finalization": final}

    def _finish(self, resume=False):
        if self.session is None:
            self._release()
            self._set("ended", saved=False, error=None)
            return
        saved = self._finish_record()
        # Rear is converted only by an explicit finish/resume request.
        if self.session and self.session.report.get("phase") == "human":
            changed = self._role("follower")
            self._wait_rear_feedback(changed)
            self.rear_writer = self._motion_writer("rear")
            self.rear_writer.handoff_can_movej(companion_tick=self._heartbeat)
            measured = self.rear_writer.prime_measured_hold()
            self.rear_writer.enable(companion_tick=self._heartbeat)
            self.rear_writer.send_gripper_m(measured["grip_m"], require_enabled=False)
            self.heartbeats.add("rear")
            with self.lock:
                self.state["rear_role_restored"] = True
        self.session.report["rear_role_restored"] = self.state.get("rear_role_restored", False)
        self.session.report["correction_finalization"] = saved
        self.session.save()
        prior_evidence = self.session.report.get("expert_evidence") or {}
        valid_correction = (bool(saved) and self.state.get("human_inputs", 0) > 0
                            and prior_evidence.get("timing_evidence_incomplete") is not True)
        if resume:
            self._require_aligned()
            self.heartbeats.discard("front")
            if self.front_writer:
                self.front_writer.close()
                self.front_writer = None
            # The old front owner is released before a new original policy runner starts.
            self._release_front_lock()
            self._set("ready", saved=valid_correction, native_record_saved=bool(saved),
                      last_episode=self.record_path, episode=None, error=None)
            self.record_path = None
        else:
            if not self._release():
                raise HardwareBlocked("Native correction saved but expert evidence finalization failed")
            self._set("saved" if valid_correction else "ended", saved=valid_correction,
                      native_record_saved=bool(saved), last_episode=self.record_path, episode=None, error=None)

    def _release_front_lock(self):
        if self.front_lock:
            self.front_lock.__exit__(None, None, None)
            self.front_lock = None

    def _release(self):
        self.heartbeats.clear()
        errors = []
        for name in ("front_writer", "rear_writer"):
            adapter = getattr(self, name)
            if adapter is not None:
                try:
                    adapter.close()
                except Exception as exc:
                    errors.append(str(exc))
                setattr(self, name, None)
        if self.log is not None:
            result = self.log.close()
            if self.session:
                self.session.report["expert_evidence"] = result
                self.session.report["recent_hardware_events"] = list(self.recent_events)
                if result["timing_evidence_incomplete"]:
                    errors.append("Expert input evidence is incomplete")
                try:
                    self.session.save()
                except Exception as exc:
                    errors.append(str(exc))
            self.log = None
        self._release_front_lock()
        if self.resources:
            self.resources.close()
            self.resources = None
        if errors:
            with self.lock:
                self.state["cleanup_errors"] = errors
            return False
        return True

    def _run(self):
        next_tick = time.monotonic()
        while not self.closing.is_set():
            try:
                try:
                    action, _ = self.commands.get_nowait()
                except queue.Empty:
                    action = None
                if action == "prepare":
                    self._prepare()
                elif action == "policy_finished":
                    self._enter_human()
                elif action in ("finish", "resume"):
                    self._finish(resume=action == "resume")
                self._tick()
            except Exception as exc:
                if self.session:
                    self.session.report["fault_feedback"] = self.snapshot()["arms"]
                    self.session.report["last_applied_input"] = deepcopy(self.last_input)
                try:
                    if self.record_path:
                        self._finish_record(maintain=False)
                except Exception as finalize_error:
                    exc.add_note("Correction finalization also failed: " + str(finalize_error))
                self._release()
                self._set("fault", error=str(exc))
            interval = .05 if self.state["phase"] in FINISHED else LOOP_PERIOD_S
            next_tick = time.monotonic() + interval
            self.closing.wait(max(0., next_tick - time.monotonic()))
        try:
            if self.record_path:
                self._finish_record(maintain=False)
        except Exception as exc:
            self._set("fault", error="Disconnect finalization failed: " + str(exc))
        finally:
            self._release()

    def close(self):
        self.closing.set()
        if self.thread:
            self.thread.join(timeout=12)
        if self.rear:
            self.rear.stop()
        self.rpc_executor.shutdown(wait=False, cancel_futures=True)
