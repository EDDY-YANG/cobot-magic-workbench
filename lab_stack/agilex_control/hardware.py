# PC PowerShell：在正式项目根目录运行；这条只生成远端计划，需 SSH。
# .\2026-10-06-15-00-AgileX.cmd move ready --arm right
# 真正执行、现场条件及参数见 README.md；不要点击“运行 Python 文件”驱动本模块。
# 导入无硬件副作用；connect_for_apply() 才创建官方 SDK 写入接口。
# 离线审阅与断点调试见 pc-receipts/2026-10-07-14-02-执行层与适配层审阅/START-HERE.md。
"""Audited Piper SDK 0.6.2 write adapter. Importing this file never opens CAN.

Receive-only state belongs to sensors.CanObserver. This adapter opens a *writer*
only through connect_for_apply(). An explicit supported reset is confined to a
separate reset-only socket. It never implicitly resets, disables, calibrates or
changes firmware limits. Endpoint arrival, power-loss safety and collision freedom are
different properties: this module verifies only real feedback and joint commands.

Reviewed installed source on 2026-10-06 via SSH, no SDK construction/query:
 piper_sdk/interface/piper_interface_v2.py SHA256
 1dbdbfd8eb133542634c40356e2096efe08c4c4c47501b13808cdd20661800e5
 __init__:373, ConnectPort:590, PiperInit:723, GetArmStatus:899,
 MotionCtrl_2:2513, JointCtrl:2729, GripperCtrl:2916,
 MasterSlaveConfig:2967, EnableArm:3062, SearchMotorMaxAngleSpdAccLimit:3114.
 protocol/protocol_v2/piper_protocol_v2.py:228 decodes 0x473 signed limits.
 Source upstream: https://github.com/agilexrobotics/piper_sdk
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import math
import struct
import time
import threading
from contextlib import contextmanager
from enum import Enum
from collections import deque
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence
from .tx_evidence import freeze_evidence
from .gripper_state import decode_gripper_feedback

SDK_VERSION = "0.6.2"
SDK_INTERFACE_SHA256 = "1dbdbfd8eb133542634c40356e2096efe08c4c4c47501b13808cdd20661800e5"
READY_DEG = (15.872, 56.431, -36.060, 8.216, 35.394, -4.867)
JOINT_ZERO_DEG = (0.0,) * 6
# This is the installed SDK's documented command envelope, NOT a collision model,
# NOT a rewrite of controller limits and NOT inferred from sampled training data.
DOCUMENTED_COMMAND_LIMITS_DEG = ((-150.0, 150.0), (0.0, 180.0), (-170.0, 0.0),
                                 (-100.0, 100.0), (-70.0, 70.0), (-120.0, 120.0))


class HardwareBlocked(RuntimeError):
    """A named hardware condition prevents sending further commands."""


class TeleoperationBlocked(HardwareBlocked):
    pass


def sdk_profile_parameters(cfg, role):
    """Explicit per-arm SDK selection; no environment or device mutation."""
    profiles = cfg.get('firmware_profiles', {})
    if not isinstance(profiles, dict):
        raise HardwareBlocked('Firmware profiles must be an object')
    profile = profiles.get(role)
    if profile is None:
        return {}
    allowed = {'sdk_version','controller_firmware_version','sdk_interface','protocol_version'}
    if not isinstance(profile, dict) or set(profile)-allowed:
        raise HardwareBlocked('Unsupported per-arm firmware profile fields')
    if profile.get('sdk_version') != '0.7.0a1':
        raise HardwareBlocked('Explicit firmware profile requires the reviewed V3 SDK')
    return dict(profile)


def _vector(values: Sequence[float], name: str) -> tuple[float, ...]:
    if values is None or len(values) != 6:
        raise HardwareBlocked(name + " must contain six measured/target degrees")
    result = tuple(float(x) for x in values)
    if not all(math.isfinite(x) for x in result):
        raise HardwareBlocked(name + " contains non-finite values")
    return result


def validate_target_deg(values, limits) -> tuple[float, ...]:
    """Reject, never clamp. SDK unit conversion occurs only after this check."""
    values = _vector(values, "target")
    if limits is None or len(limits) != 6:
        raise HardwareBlocked("Six authoritative command bounds are required")
    for i, (q, bound) in enumerate(zip(values, limits), 1):
        lo, hi = map(float, bound)
        if not (math.isfinite(lo) and math.isfinite(hi) and lo <= hi):
            raise HardwareBlocked("Invalid limits for J" + str(i))
        if not lo <= q <= hi:
            raise HardwareBlocked(f"J{i} target {q:.6f} deg outside [{lo}, {hi}]; no clamp")
        quantized = round(q * 1000) / 1000
        if not lo <= quantized <= hi:
            raise HardwareBlocked(f"J{i} quantized target outside bounds; no clamp")
    return values


def decode_status(status_hex: str | bytes) -> dict:
    raw = bytes.fromhex(status_hex) if isinstance(status_hex, str) else bytes(status_hex)
    if len(raw) != 8:
        raise HardwareBlocked("0x2A1 must contain eight real bytes")
    # SDK's err_code is big endian. The high byte carries angle errors; low byte
    # carries communications errors. Preserve unknown/reserved bits as faults.
    error = int.from_bytes(raw[6:8], "big")
    return {"ctrl_mode": raw[0], "arm_status": raw[1], "mode_feed": raw[2],
            "teach_status": raw[3], "motion_status": raw[4], "trajectory_num": raw[5],
            "err_code": error,
            "angle_limit_joints": [i + 1 for i in range(6) if raw[6] & (1 << i)],
            "communication_error_joints": [i + 1 for i in range(6) if raw[7] & (1 << i)],
            "healthy": raw[1] == 0 and error == 0,
            "healthy_standby": raw[0] == 0 and raw[1] == 0 and error == 0,
            "can_movej": raw[0] == 1 and raw[2] == 1 and raw[1] == 0 and error == 0,
            "status_hex": raw.hex()}


def normalized_snapshot(snapshot: Mapping) -> dict:
    """Accept the shared observer schema or a deliberately named root wrapper."""
    q = snapshot.get("q_deg", snapshot.get("joints_deg"))
    grip = snapshot.get("grip_m", snapshot.get("gripper_m"))
    enabled = snapshot.get("enabled", snapshot.get("joint_enabled"))
    ages = [snapshot.get(k) for k in ("age_joint_s", "age_status_s", "age_motor_s")]
    if all(a is None for a in ages) and "max_age_s" in snapshot:
        ages = [snapshot["max_age_s"]] * 3
    return {**snapshot, "q_deg": q, "grip_m": grip, "enabled": enabled,
            "feedback_ages_s": ages, "status": decode_status(snapshot.get("status_hex", ""))}


def require_feedback(snapshot: Mapping, *, max_age_s=0.2, require_enabled=True,
                     require_can=False) -> dict:
    snap = normalized_snapshot(snapshot)
    snap["q_deg"] = list(_vector(snap["q_deg"], "actual feedback"))
    if any(x is None or not math.isfinite(float(x)) or not 0 <= float(x) <= max_age_s
           for x in snap["feedback_ages_s"]):
        raise HardwareBlocked("Joint/status/motor feedback missing or stale; no new targets")
    if snap.get("fresh") is False:
        raise HardwareBlocked("Shared observer reports stale feedback")
    if not snap["status"]["healthy"]:
        raise HardwareBlocked("Controller reports fault: " + snap["status"]["status_hex"])
    flags = snap.get("motor_flags")
    if flags is not None and (len(flags) != 6 or any(int(x) & 0xBF for x in flags)):
        raise HardwareBlocked("Driver fault bits in actual 0x261-0x266 feedback")
    enabled = snap.get("enabled")
    if require_enabled and (enabled is None or len(enabled) != 6 or not all(enabled)):
        raise HardwareBlocked("Not all six motors are confirmed enabled")
    if require_can and not snap["status"]["can_movej"]:
        raise HardwareBlocked("Actual controller mode is not healthy CAN/MOVEJ")
    return snap


def require_gripper_feedback(snapshot: Mapping, *, max_age_s=0.2,
                             require_enabled=True, now_monotonic=None) -> dict:
    """Validate the raw 0x2A8 separately from the six joint driver statuses.

    No hardware is opened or commanded. The timestamp belongs to the local
    robot observer clock; a compact/page snapshot without raw evidence cannot
    pass. A valid measurement is not proof of contact, holding or temperature.
    """
    raw_group = snapshot.get("raw") if isinstance(snapshot, Mapping) else None
    raw = (raw_group.get("0x2a8", raw_group.get("0x2A8"))
           if isinstance(raw_group, Mapping) else None)
    raw = raw if isinstance(raw, Mapping) else {}
    decoded = None
    age = snapshot.get("age_gripper_s") if isinstance(snapshot, Mapping) else None
    stamp = raw.get("monotonic_s")

    def reject(reason, message):
        error = HardwareBlocked(message + "; no new gripper target or automatic recovery")
        if decoded is not None:
            error.gripper_feedback = decoded
        error.gripper_guard = {
            "reason": reason, "raw_payload_hex": raw.get("payload_hex"),
            "received_monotonic_s": stamp, "reported_age_s": age,
            "require_enabled": bool(require_enabled),
        }
        raise error

    def finite(value):
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value))

    if not finite(max_age_s) or max_age_s <= 0:
        reject("invalid_freshness_limit", "Positive finite gripper freshness limit required")
    try:
        decoded = decode_gripper_feedback(raw.get("payload_hex"))
    except (TypeError, ValueError):
        reject("missing_or_malformed_raw", "Missing or malformed actual gripper 0x2A8 payload")
    if decoded.get('linear_position_available') is not True:
        reject('unsupported_position_unit', 'Gripper feedback is angle mode or unknown; linear width required')
    now = time.monotonic() if now_monotonic is None else now_monotonic
    decoded.update(received_monotonic_s=stamp, age_s=age)
    if (not finite(age) or not finite(stamp) or not finite(now)
            or not 0 <= age <= max_age_s or not 0 <= now - stamp <= max_age_s
            or snapshot.get("fresh") is False or snapshot.get("feedback_fresh") is False):
        reject("feedback_not_fresh", "Gripper feedback missing, stale or future-dated")
    actual = snapshot.get("grip_m", snapshot.get("gripper_m"))
    if (not finite(actual)
            or not math.isclose(decoded["aperture_m"], actual, rel_tol=0., abs_tol=1e-9)):
        reject("position_raw_mismatch", "Actual gripper value disagrees with signed raw 0x2A8 feedback")
    if not decoded["healthy"]:
        reject("controller_fault", "Gripper controller reports fault " + decoded["status_hex"]
               + ": " + ", ".join(decoded["fault_flags"]))
    if require_enabled and not decoded["enabled"]:
        reject("not_enabled", "Gripper not confirmed enabled; policy cannot automatically re-enable")
    return decoded


@dataclass(frozen=True)
class PosePlan:
    start_deg: tuple[float, ...]
    target_deg: tuple[float, ...]
    duration_s: float
    max_speed_deg_s: float
    max_accel_deg_s2: float

    def sample(self, elapsed_s: float) -> tuple[float, ...]:
        if self.duration_s == 0 or elapsed_s >= self.duration_s:
            return self.target_deg
        u = max(0.0, float(elapsed_s)) / self.duration_s
        blend = 0.5 - 0.5 * math.cos(math.pi * u)
        return tuple(a + (b - a) * blend for a, b in zip(self.start_deg, self.target_deg))

    def as_dict(self) -> dict:
        return {"start_deg": list(self.start_deg), "target_deg": list(self.target_deg),
                "duration_s": self.duration_s, "max_speed_deg_s": self.max_speed_deg_s,
                "max_accel_deg_s2": self.max_accel_deg_s2,
                "trajectory": "synchronized cosine joint interpolation",
                "collision_checked": False, "power_loss_protection": False}


def plan_pose(current_deg, target_deg, limits, *, max_speed_deg_s=10.0,
              max_accel_deg_s2=50.0) -> PosePlan:
    start = validate_target_deg(current_deg, limits)
    target = validate_target_deg(target_deg, limits)
    speed, accel = float(max_speed_deg_s), float(max_accel_deg_s2)
    if not (0 < speed <= 30 and 0 < accel <= 50):
        raise HardwareBlocked("Explicit supported caps: 0 < speed <=30 deg/s, acceleration <=50 deg/s2")
    distance = max(abs(b - a) for a, b in zip(start, target))
    duration = max(math.pi * distance / (2 * speed),
                   math.sqrt(math.pi**2 * distance / (2 * accel))) if distance else 0.0
    return PosePlan(start, target, duration, speed, accel)


def decode_limit_reply(payload: bytes | str) -> dict:
    payload = bytes.fromhex(payload) if isinstance(payload, str) else bytes(payload)
    if len(payload) != 8 or payload[0] not in range(1, 7):
        raise HardwareBlocked("Invalid motor-specific 0x473 reply")
    maximum, minimum, speed = struct.unpack(">hhH", payload[1:7])
    if minimum >= maximum or speed == 0:
        raise HardwareBlocked("Empty/reversed angle limits or zero max speed in real reply")
    return {"joint": payload[0], "minimum_deg": minimum / 10.0,
            "maximum_deg": maximum / 10.0, "maximum_speed_rad_s": speed / 1000.0,
            "payload_hex": payload.hex()}


def teleoperation_capability() -> dict:
    """Tell the truth: 0x470 is role/ID configuration, not an unlink command."""
    return {"supported_sdk_role_api": "MasterSlaveConfig(linkage_config,feedback_offset,ctrl_offset,linkage_offset)",
            "role_input": "0xFA", "role_output": "0xFC", "role_0x00": "INVALID, not unlink",
            "automatic_enter_verified": False, "automatic_exit_verified": False,
            "leader_signal_blocking_verified": False, "power_loss_protection": False,
            "blocker": "Only follower bus identities are known. Software cannot isolate powered leader CAN transmitters. Original leader topology/role restoration and independent control must be verified first."}


def enter_teleoperation(*args, **kwargs):
    raise TeleoperationBlocked(teleoperation_capability()["blocker"])


def exit_teleoperation(*args, **kwargs):
    raise TeleoperationBlocked(teleoperation_capability()["blocker"])


class PiperAdapter:
    """No implicit enable/reset/calibration. Caller supplies writer lock and USB logs.

    cfg: interface, sdk_version, command_limits_deg, max_feedback_age_s,
    native_speed_percent (controller speed percentage, NOT degrees per second),
    gripper_bounds_m, gripper_effort_mNm, leader_isolated.
    snapshot callable: sensors.CanObserver.snapshot for this exact bus.
    event callback must enqueue bounded records without blocking the control loop.
    """
    def __init__(self, cfg: Mapping, snapshot: Callable[[], Mapping], event=None, *, send_guard=None):
        self.cfg = dict(cfg)
        self.snapshot = snapshot
        self.event = event or (lambda value: None)
        self.send_guard = send_guard
        self.sdk = None
        self._allowed_ids = frozenset()
        self._sent = {}
        self._transport = None
        self._original_send = None
        self._expected_echoes = deque(maxlen=2048)
        self.last_mode_handoff = None
        self._connection_scope = None
        self._reset_attempted = False
        self._gripper_zero_attempted = False
        self._gripper_zero_transport_attempted = False
        self._disable_attempted = set()
        self._disable_transport_attempted = set()
        self._tx_evidence = None
        self._tx_evidence_errors = 0
        self._tx_evidence_error = None
        self._dispatch_local = threading.local()
        self._dispatch_sequence = 0
        self._tx_attempt_sequence = 0

    def attach_tx_evidence(self, writer):
        """Attach an already prepared bounded sink; no I/O or thread creation."""
        self._tx_evidence = writer

    def _tx_record(self, record):
        if self._tx_evidence is None:
            return
        try:
            self._tx_evidence.enqueue(record)
        except Exception as exc:
            self._tx_evidence_errors += 1
            self._tx_evidence_error = type(exc).__name__ + ': ' + str(exc)
            try:
                self._tx_evidence.mark_incomplete(self._tx_evidence_error)
            except Exception:
                pass

    @property
    def tx_evidence_status(self):
        result = {'attached': self._tx_evidence is not None,
                  'adapter_evidence_errors': self._tx_evidence_errors,
                  'adapter_evidence_error': self._tx_evidence_error}
        if self._tx_evidence is not None:
            try:
                result.update(self._tx_evidence.status())
            except Exception as exc:
                result['status_error'] = repr(exc)
                result['timing_evidence_incomplete'] = True
        result['timing_evidence_incomplete'] = bool(result.get('timing_evidence_incomplete') or self._tx_evidence_errors)
        return result

    @contextmanager
    def dispatch(self, context=None, **fields):
        """Bind immutable provenance around one joint+gripper publication tick.

        The caller owns scheduled time, final seven-dimensional reference and
        run/request/block/revision IDs. Missing context remains unknown. This
        observer never changes targets, limits, retries or exception priority.
        """
        self._dispatch_sequence += 1
        dispatch_id = self._dispatch_sequence
        try:
            frozen = freeze_evidence({**(context or {}), **fields})
        except Exception as exc:
            self._tx_evidence_errors += 1
            self._tx_evidence_error = 'Dispatch context unavailable: ' + repr(exc)
            frozen = freeze_evidence({'unavailable_reason': self._tx_evidence_error[:1024]})
            if self._tx_evidence is not None:
                try:
                    self._tx_evidence.mark_incomplete(self._tx_evidence_error)
                except Exception:
                    pass
        previous = getattr(self._dispatch_local, 'state', None)
        state = {'dispatch_id': dispatch_id, 'context': frozen, 'attempted': 0,
                 'succeeded': 0, 'failed': 0, 'successful_can_ids': []}
        self._dispatch_local.state = state
        begin = time.monotonic_ns()
        error = None
        self._tx_record({'kind': 'dispatch_begin', 'dispatch_id': dispatch_id,
                         'host_begin_monotonic_ns': begin, 'context': frozen})
        try:
            yield dispatch_id
        except BaseException as exc:
            error = {'type': type(exc).__name__, 'message': str(exc)[:1024]}
            raise
        finally:
            self._tx_record({'kind': 'dispatch_end', 'dispatch_id': dispatch_id, 'context': frozen,
                'host_begin_monotonic_ns': begin, 'host_end_monotonic_ns': time.monotonic_ns(),
                'packet_attempts': state['attempted'], 'packet_successes': state['succeeded'],
                'packet_failures': state['failed'], 'successful_can_ids': tuple(state['successful_can_ids']),
                'exception': error, 'partial_send': bool(state['succeeded'] and (error or state['failed'])),
                'partial_send_basis': 'some host sends succeeded before this dispatch failed; inspect per-packet evidence'})
            self._dispatch_local.state = previous

    def _checked_send(self, can_id, data):
        """Original TX guards plus observation of every attempted SDK send."""
        begin = time.monotonic_ns()
        self._tx_attempt_sequence += 1
        attempt = self._tx_attempt_sequence
        state = getattr(self._dispatch_local, 'state', None)
        context = None if state is None else state['context']
        dispatch_id = None if state is None else state['dispatch_id']
        if state is not None:
            state['attempted'] += 1
        payload = result = error = None
        send_begin = send_end = None
        succeeded = False
        begin_recorded = False
        phase = 'payload_conversion'
        try:
            payload = bytes(data)
            self._tx_record({'kind': 'packet_begin', 'attempt_sequence': attempt, 'dispatch_id': dispatch_id,
                'context': context, 'can_id': can_id, 'payload': payload,
                'host_begin_monotonic_ns': begin, 'interface': self.cfg['interface']})
            begin_recorded = True
            phase = 'writer_guard'
            if self.send_guard is not None:
                self.send_guard()
            if can_id not in self._allowed_ids or len(payload) != 8:
                raise HardwareBlocked(f"Unexpected SDK TX 0x{can_id:x}; prohibited")
            if self._connection_scope == "role":
                if payload not in (bytes.fromhex("fa00000000000000"), bytes.fromhex("fc00000000000000")):
                    raise HardwareBlocked("Role writer accepts only zero-offset leader/follower configuration")
                if self._sent.get("0x470", 0):
                    raise HardwareBlocked("One role write per explicitly opened role operation")
            if self._connection_scope == 'mode':
                percent = int(self.cfg.get('native_speed_percent', 40))
                allowed = {bytes((ctrl, 1, percent, 0, 0, 0, 0, 0)) for ctrl in (0, 1)}
                if payload not in allowed:
                    raise HardwareBlocked('Mode-only writer permits only reviewed standby/CAN MOVEJ position-speed requests')
            if self._connection_scope == 'high_follow':
                if can_id != 0x151 or payload != bytes.fromhex('010100ad00000000'):
                    raise HardwareBlocked('High-follow writer permits only the reviewed CAN/MOVEJ/AD request; no targets or roles')
            if self._connection_scope == 'disable':
                joint = payload[0]
                if (can_id != 0x471 or joint not in range(1, 7)
                        or payload != bytes((joint, 1, 0, 0, 0, 0, 0, 0))
                        or joint in self._disable_transport_attempted):
                    raise HardwareBlocked('Disable-only writer permits one disable per joint 1..6; no enable or gripper')
                self._disable_transport_attempted.add(joint)
            if self._connection_scope == 'reset':
                if payload != bytes.fromhex('0200000000000000') or self._sent.get('0x150', 0):
                    raise HardwareBlocked('Reset-only writer permits exactly one reviewed supported reset')
            if self._connection_scope == 'gripper_calibration':
                allowed = (bytes.fromhex('0000000003e80000'), bytes.fromhex('0000000003e800ae'))
                if can_id != 0x159 or payload not in allowed:
                    raise HardwareBlocked('Gripper calibration writer permits only disable and current-position zero; no motion/clear-error/enable')
                if payload[-1] == 0xAE:
                    if self._gripper_zero_transport_attempted:
                        raise HardwareBlocked('Gripper zero transport was already attempted; no retry')
                    self._gripper_zero_transport_attempted = True
            phase = 'transport_send'
            send_begin = time.monotonic_ns()
            try:
                result = self._original_send(can_id, data)
            finally:
                send_end = time.monotonic_ns()
            if result != self._transport.CAN_STATUS.SEND_MESSAGE_SUCCESS:
                raise HardwareBlocked(f"Actual SocketCAN send failed for 0x{can_id:x}: {result}")
            succeeded = True
            if state is not None:
                state['succeeded'] += 1
                state['successful_can_ids'].append(can_id)
            self._sent[f'0x{can_id:x}'] = self._sent.get(f'0x{can_id:x}', 0) + 1
            self._expected_echoes.append((time.monotonic(), f'0x{can_id:x}', payload.hex()))
            phase = 'legacy_can_tx_event'
            self._emit('can_tx', can_id=f'0x{can_id:x}', payload_hex=payload.hex())
            return result
        except BaseException as exc:
            error = {'type': type(exc).__name__, 'message': str(exc)[:1024], 'phase': phase}
            if state is not None:
                state['failed'] += 1
            raise
        finally:
            if not begin_recorded:
                self._tx_record({'kind': 'packet_begin', 'attempt_sequence': attempt, 'dispatch_id': dispatch_id,
                    'context': context, 'can_id': can_id, 'payload': payload, 'host_begin_monotonic_ns': begin})
            # Preserve ordinary SDK enum/scalar returns. Unknown object types
            # are identified explicitly rather than guessed to mean success.
            return_evidence = result
            if result is not None and not isinstance(result, (Enum, str, bytes, bool, int, float)):
                return_evidence = {'type': type(result).__name__, 'value_unavailable': True}
                self._tx_evidence_errors += 1
                self._tx_evidence_error = 'Unsupported transport return type: ' + type(result).__name__
            self._tx_record({'kind': 'packet_end', 'attempt_sequence': attempt, 'dispatch_id': dispatch_id,
                'context': context, 'interface': self.cfg['interface'], 'can_id': can_id, 'payload': payload,
                'host_begin_monotonic_ns': begin, 'host_end_monotonic_ns': time.monotonic_ns(),
                'transport_begin_monotonic_ns': send_begin, 'transport_end_monotonic_ns': send_end,
                'transport_return': return_evidence, 'send_succeeded': succeeded, 'exception': error,
                'dispatch_successes_so_far': None if state is None else state['succeeded'],
                'partial_send': bool(state is not None and state['succeeded'] and error)})

    def _emit(self, name, **fields):
        self.event({"kind": name, "monotonic_s": time.monotonic(), **fields})

    def connect_for_apply(self, *, query_only=False, mode_only=False, reset_only=False,
                          gripper_calibration_only=False, high_follow_only=False, disable_only=False, role_only=False):
        """Explicit socket acquisition. No query/reset or motor enable on connect."""
        if self.sdk is not None:
            raise HardwareBlocked("Writer already connected")
        if sum(bool(flag) for flag in (query_only, mode_only, reset_only, gripper_calibration_only,
                                       high_follow_only, disable_only, role_only)) > 1:
            raise HardwareBlocked("Writer scopes are mutually exclusive")
        snap = self.snapshot()
        if snap.get("interface") is not None and snap["interface"] != self.cfg["interface"]:
            raise HardwareBlocked("Observer and writer CAN interfaces differ")
        if self.cfg.get("expected_serial"):
            identity = snap.get("identity", {})
            if (not identity.get("identity_verified") or
                    identity.get("serial") != self.cfg["expected_serial"]):
                raise HardwareBlocked("Actual CAN USB serial is not verified for this writer")
        v3 = self.cfg.get('sdk_version') == '0.7.0a1'
        transport_attribute = '_C_PiperInterface_V2__arm_can'
        if v3:
            from .sdk_v3_compatibility import load_reviewed_v3_class, SdkV3CompatibilityError, SDK_V3_TRANSPORT_ATTRIBUTE
            try:
                interface_class = load_reviewed_v3_class(self.cfg)
            except SdkV3CompatibilityError as exc:
                raise HardwareBlocked('V3 SDK compatibility check failed: '+str(exc)) from exc
            transport_attribute = SDK_V3_TRANSPORT_ATTRIBUTE
        else:
            if self.cfg.get("sdk_version", SDK_VERSION) != SDK_VERSION:
                raise HardwareBlocked("Config SDK pin differs from reviewed 0.6.2")
            if importlib.metadata.version("piper-sdk") != SDK_VERSION:
                raise HardwareBlocked("Installed piper-sdk must be exactly " + SDK_VERSION)
            from piper_sdk import C_PiperInterface_V2
            interface_class = C_PiperInterface_V2
            source_path = inspect.getfile(interface_class)
            with open(source_path, "rb") as stream:
                actual_hash = hashlib.sha256(stream.read()).hexdigest()
            if actual_hash != SDK_INTERFACE_SHA256:
                raise HardwareBlocked("SDK interface source differs from reviewed installed source")
        self._connection_scope = ("query" if query_only else "mode" if mode_only
                                  else "reset" if reset_only else "gripper_calibration" if gripper_calibration_only
                                  else "high_follow" if high_follow_only else "disable" if disable_only else "role" if role_only else "motion")
        self._allowed_ids = frozenset({0x472} if query_only else {0x151} if mode_only
                                    else {0x150} if reset_only else
                                    {0x159} if gripper_calibration_only else
                                    {0x151} if high_follow_only else {0x471} if disable_only else
                                    {0x151, 0x155, 0x156, 0x157, 0x159, 0x471, 0x472})
        sdk = interface_class(self.cfg["interface"], judge_flag=True,
                                 can_auto_init=not v3, start_sdk_joint_limit=False,
                                 start_sdk_gripper_limit=False, log_to_file=False)
        self.sdk = sdk
        try:
            if v3:
                sdk.CreateCanBus(self.cfg['interface'],bustype='socketcan',expected_bitrate=1000000,judge_flag=True)
            transport = getattr(sdk, transport_attribute)
            self._transport = transport
            self._original_send = transport.SendCanMessage
            transport.SendCanMessage = self._checked_send
            # Default piper_init=True sends many queries; deliberately FALSE.
            # Shared observer owns receive thread, so no SDK read thread is started.
            sdk.ConnectPort(can_init=False, piper_init=False, start_thread=False)
            if v3 and (getattr(transport,'recv_bus',None) is None or getattr(transport,'send_bus',None) is None):
                raise HardwareBlocked('V3 host CAN socket initialization failed; no writer ready')
        except BaseException as primary_error:
            if v3 and self._transport is None:
                self._transport = getattr(sdk,transport_attribute,None)
            try:
                self.close()
            except Exception as cleanup_error:
                primary_error.add_note('Writer connection cleanup failed: '+repr(cleanup_error))
            raise
        self._emit("writer_connected", interface=self.cfg["interface"], query_only=query_only)

    def close(self):
        if self.sdk is not None:
            # DisconnectPort closes only host CAN socket; no DisableArm is called.
            try:
                # The SDK caches singleton instances per bus. Restore wrapper so a
                # subsequent query-only/apply session cannot inherit the old whitelist.
                if self._transport is not None and self._original_send is not None:
                    self._transport.SendCanMessage = self._original_send
                try:
                    self.sdk.DisconnectPort()
                finally:
                    # V3 DisconnectPort may return without closing host sockets
                    # when ConnectPort never reached _connected=True.
                    if self.cfg.get('sdk_version')=='0.7.0a1' and self._transport is not None:
                        failures = []
                        closed = set()
                        for name in ('recv_bus','send_bus'):
                            bus = getattr(self._transport,name,None)
                            if bus is None:
                                continue
                            try:
                                if id(bus) not in closed:
                                    bus.shutdown()
                                    closed.add(id(bus))
                                setattr(self._transport,name,None)
                            except Exception as exc:
                                failures.append(repr(exc))
                        if failures:
                            raise HardwareBlocked('V3 host socket cleanup unconfirmed: '+repr(failures))
            finally:
                self.sdk = None
                self._transport = None
                self._original_send = None
                self._connection_scope = None

    def _require_connected(self):
        if self.sdk is None:
            raise HardwareBlocked("No writer: call connect_for_apply explicitly")

    def configure_role(self, role, latest_frame, *, timeout_s=2.0, companion_tick=None):
        """Official MasterSlaveConfig with a fresh configuration acknowledgement."""
        self._require_connected()
        self.require_leader_isolation()
        if self._connection_scope != "role" or role not in ("leader", "follower"):
            raise HardwareBlocked("Role change needs a dedicated role writer")
        began = time.monotonic()
        self.sdk.MasterSlaveConfig(0xFA if role == "leader" else 0xFC, 0, 0, 0)
        while time.monotonic() - began < timeout_s:
            if companion_tick is not None:
                companion_tick()
            frame = latest_frame(0x476)
            if frame and began < frame[0] <= time.monotonic() and len(frame[1]) == 8 and frame[1][0] == 0x70:
                return {"role_requested": role, "ack_monotonic_s": frame[0],
                        "ack_payload": frame[1].hex(), "feedback_still_required": True}
            time.sleep(.005)
        raise HardwareBlocked("Role request lacks a fresh 0x476 configuration acknowledgement")

    def disable_gripper_for_calibration(self):
        """Explicit separate operator action; disables only the gripper, never clears a fault."""
        self._require_connected()
        if self._connection_scope != 'gripper_calibration':
            raise HardwareBlocked('Dedicated gripper calibration writer required')
        self.require_leader_isolation()
        self.require_no_foreign_commands()
        self.feedback(require_enabled=False)
        self.sdk.GripperCtrl(0, 1000, 0x00, 0x00)

    def set_gripper_zero_once(self):
        """Set the physically reviewed CURRENT position, with fresh disabled feedback."""
        self._require_connected()
        if self._connection_scope != 'gripper_calibration':
            raise HardwareBlocked('Dedicated gripper calibration writer required')
        if self._gripper_zero_attempted:
            raise HardwareBlocked('Gripper zero already attempted; no automatic retry')
        self.require_leader_isolation()
        self.require_no_foreign_commands()
        state = self.feedback(require_enabled=False)
        from .gripper_calibration import require_quiet_state
        require_quiet_state(state, disabled=True)
        self._gripper_zero_attempted = True
        self.sdk.GripperCtrl(0, 1000, 0x00, 0xAE)

    def feedback(self, *, require_can=False, require_enabled=True):
        snapshot = self.snapshot()
        if snapshot.get("interface") is not None and snapshot["interface"] != self.cfg["interface"]:
            raise HardwareBlocked("Observer and writer CAN interfaces differ")
        return require_feedback(snapshot,
                                max_age_s=self.cfg.get("max_feedback_age_s", 0.2),
                                require_can=require_can, require_enabled=require_enabled)

    def require_leader_isolation(self):
        if not self.cfg.get("leader_isolated", False):
            raise HardwareBlocked("Leader isolation not confirmed; a host lock cannot stop external CAN writers")

    def require_no_foreign_commands(self):
        """Observe unmatched recent targets; local SocketCAN echoes are possible.

        Matching our own payload is not proof of physical sender identity. This
        guard complements confirmed leader isolation; it never claims bus blocking.
        """
        snapshot = self.snapshot()
        now = time.monotonic()
        expected = {(cid, payload) for t,cid,payload in self._expected_echoes if now-t<=2.0}
        for cid, frame in snapshot.get("external_commands", {}).items():
            age = frame.get("last_age_s")
            payload = frame.get("payload_hex")
            if age is not None and age <= self.cfg.get("foreign_command_window_s", 0.2):
                if (cid.lower(), payload) not in expected:
                    raise HardwareBlocked("Unmatched recent external CAN command " + cid + "; single control owner not established")

    def request_control_mode(self, *, standby=False):
        """One mode-only request; receipt interpretation belongs to modes.py."""
        self._require_connected()
        if self._connection_scope != "mode":
            raise HardwareBlocked("Explicit mode-only writer scope is required")
        self.require_leader_isolation()
        self.require_no_foreign_commands()
        state = self.feedback(require_enabled=False)
        status = state["status"]
        if status["ctrl_mode"] not in (0, 1) or status["teach_status"] not in (0, 2, 6):
            raise HardwareBlocked("Teaching/playback or unsupported controller state still active; mode request blocked")
        percent = int(self.cfg.get("native_speed_percent", 40))
        if not 1 <= percent <= 100:
            raise HardwareBlocked("Native speed percentage must be within 1..100")
        self.sdk.MotionCtrl_2(0 if standby else 1, 1, percent, 0)

    def require_reviewed_oem_source(self, snapshot=None):
        """Narrow high-follow precondition, never a global foreign-writer bypass.

        CAN IDs cannot authenticate a sender. The physical role/source review
        remains an explicit operator assertion; raw target freshness, bounds,
        coherence and proximity are independently checked here on every request.
        """
        if self._connection_scope != 'high_follow':
            raise HardwareBlocked('Reviewed OEM source exception requires dedicated high-follow scope')
        if any(self.cfg.get(key) is not True for key in ('roles_configured', 'poses_aligned', 'source_reviewed')):
            raise HardwareBlocked('OEM roles, matched physical poses and command source must be explicitly reviewed')
        snap = self.snapshot() if snapshot is None else snapshot
        state = require_feedback(snap, require_enabled=True,
                                 max_age_s=self.cfg.get('max_feedback_age_s', .2))
        now = time.monotonic()
        max_age = self.cfg.get('max_feedback_age_s', .2)
        stamps, target = [], []
        raw = snap.get('raw', {})
        for cid in (0x155, 0x156, 0x157):
            frame = raw.get(hex(cid), {})
            stamp = frame.get('monotonic_s')
            if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                    or not 0 <= now-stamp <= max_age):
                raise HardwareBlocked('Fresh raw OEM joint target triplet is required; old drafts cannot restore following')
            try:
                payload = bytes.fromhex(frame['payload_hex'])
                values = struct.unpack('>ii', payload)
            except (KeyError, TypeError, ValueError, struct.error) as exc:
                raise HardwareBlocked('Malformed raw OEM joint targets') from exc
            stamps.append(stamp)
            target.extend(value/1000 for value in values)
        if max(stamps)-min(stamps) > .05:
            raise HardwareBlocked('OEM joint target triplet receipt skew exceeds 50 ms')
        target = validate_target_deg(target, self.cfg.get('command_limits_deg', DOCUMENTED_COMMAND_LIMITS_DEG))
        reported = snap.get('action_targets', {}).get('q_deg')
        if reported is None or len(reported) != 6 or any(abs(a-b) > 1e-9 for a,b in zip(_vector(reported, 'observed target'), target)):
            raise HardwareBlocked('Observed OEM target values disagree with raw CAN payloads')
        error = max(abs(a-b) for a,b in zip(target, state['q_deg']))
        if error > 2.0:
            raise HardwareBlocked(f'OEM target and actual joints differ by {error:.3f} degrees; no automatic catch-up')
        expected = {(cid, payload) for stamp,cid,payload in self._expected_echoes if now-stamp <= 2.0}
        for cid, frame in snap.get('external_commands', {}).items():
            age, payload_hex = frame.get('last_age_s'), frame.get('payload_hex')
            if age is None:
                continue
            if type(age) not in (int, float) or not math.isfinite(age) or age < 0:
                raise HardwareBlocked('Invalid external command receipt age')
            if age > self.cfg.get('foreign_command_window_s', .2):
                continue
            if (cid.lower(), payload_hex) in expected:
                continue
            try:
                number, payload = int(cid, 16), bytes.fromhex(payload_hex)
            except (TypeError, ValueError) as exc:
                raise HardwareBlocked('Malformed OEM/external command') from exc
            if len(payload) != 8:
                raise HardwareBlocked('Malformed OEM/external command length')
            if number in (0x155, 0x156, 0x157):
                if payload_hex != raw.get(hex(number), {}).get('payload_hex'):
                    raise HardwareBlocked('OEM command view disagrees with raw target evidence')
                continue
            if number == 0x151 and (payload[0:2] == b'\x01\x01' and payload[2] <= 100
                                    and payload[3:] == b'\xad\x00\x00\x00\x00'):
                continue
            if number == 0x159:
                if payload[6] not in (0,1,2,3) or payload[7] != 0:
                    raise HardwareBlocked('OEM gripper command has unsupported units or zero request')
                grip = require_gripper_feedback(snap, require_enabled=False, max_age_s=max_age)
                target_m = int.from_bytes(payload[:4], 'big', signed=True)/1e6
                lo, hi = self.cfg.get('gripper_bounds_m', (-.0013, .1))
                if not lo <= target_m <= hi or abs(target_m-grip['aperture_m']) > .002:
                    raise HardwareBlocked('OEM gripper target is outside bounds or differs by more than 2 mm')
                continue
            raise HardwareBlocked('Unreviewed external CAN command '+cid+' during high-follow request')
        return state, {'q_deg': list(target), 'raw_target_receipt_stamps_s': stamps,
                       'max_joint_error_deg': error, 'sender_authenticated': False,
                       'source_review_basis': 'explicit operator review; CAN IDs do not identify physical sender'}

    def request_high_follow(self):
        """Request existing OEM following; never configure roles or send targets."""
        self._require_connected()
        state, source = self.require_reviewed_oem_source()
        if state['status']['ctrl_mode'] not in (0,1) or state['status']['teach_status'] not in (0,2,6):
            raise HardwareBlocked('Teaching/playback or unsupported state blocks high-follow request')
        self.sdk.MotionCtrl_2(1, 1, 0, 0xAD)
        return source

    def disable_joint_once(self, joint):
        """Disable one numbered joint; explicit 1..6 avoids SDK all/gripper ambiguity."""
        self._require_connected()
        if self._connection_scope != 'disable' or type(joint) is not int or joint not in range(1,7):
            raise HardwareBlocked('Disable-only scope and a joint number 1..6 are required')
        if joint in self._disable_attempted:
            raise HardwareBlocked('Joint disable already attempted; no automatic retry')
        self.require_leader_isolation()
        self.require_no_foreign_commands()
        state = self.feedback(require_enabled=False)
        if state['status']['ctrl_mode'] not in (0,1) or state['status']['teach_status'] not in (0,2,6):
            raise HardwareBlocked('Teaching/playback or unsupported state blocks disable')
        self._disable_attempted.add(joint)
        self.sdk.DisableArm(joint)

    def reset_supported_once(self, *, supported=False):
        """Explicit single ResetPiper; motor holding power can be lost immediately.

        Caller must establish actual support and zero-pose review before opening
        this separate writer. No retry, enable, target or mode request follows.
        """
        self._require_connected()
        if self._connection_scope != "reset" or not supported:
            raise HardwareBlocked("Explicit reset-only writer and actual physical support are required")
        if self._reset_attempted:
            raise HardwareBlocked("Supported reset already attempted; no automatic retry")
        self.require_leader_isolation()
        self.require_no_foreign_commands()
        self.feedback(require_enabled=False)
        self._reset_attempted = True
        self.sdk.ResetPiper()
        self._emit("supported_reset_submitted", reset_packets_sent=self._sent.get("0x150", 0))

    def handoff_can_movej(self, *, timeout_s=2.0, companion_tick=None):
        """Bounded mode-only request; a real post-request 0x2A1 is required.

        Historical hardware evidence supported 100 Hz mode-only requests. That
        evidence does not certify motion, nor prove that later 50 Hz trajectory
        publication maintains this firmware's mode. No target is sent here.
        """
        self._require_connected()
        self.require_leader_isolation()
        self.require_no_foreign_commands()
        initial = self.feedback(require_enabled=False)
        percent = int(self.cfg.get("native_speed_percent", 40))
        if not 1 <= percent <= 100:
            raise HardwareBlocked("Native speed percentage must be within 1..100")
        timeout_s = float(timeout_s)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise HardwareBlocked("Mode handoff timeout must be finite and positive")
        begin = time.monotonic()
        deadline, next_send = begin + timeout_s, begin
        changes = deque(maxlen=32)
        previous_status = None
        transition_count = 0
        receipt = {"started_monotonic_s": begin, "timeout_s": timeout_s,
                   "requested_send_hz": 100.0, "poll_interval_s": .005,
                   "before_status_hex": initial["status"]["status_hex"],
                   "first_request_monotonic_s": None, "last_request_monotonic_s": None,
                   "mode_frames_sent": 0, "maximum_request_gap_s": None,
                   "post_request_ack_monotonic_s": None, "last_status_hex": None,
                   "joint_targets_sent": False, "gripper_targets_sent": False,
                   "completed": False}
        self.last_mode_handoff = receipt
        self._emit("handoff_start", requested_send_hz=100.0,
                   before_status_hex=receipt["before_status_hex"], timeout_s=timeout_s)
        try:
            while time.monotonic() < deadline:
                if companion_tick is not None:
                    companion_tick()
                self.require_no_foreign_commands()
                now = time.monotonic()
                if now >= deadline:
                    break
                if now >= next_send:
                    # Submode0 requests normal position control only. This never
                    # resets/enables/disables, writes zero, or uses MIT/CPV.
                    self.sdk.MotionCtrl_2(0x01, 0x01, percent, 0x00)
                    previous_request = receipt["last_request_monotonic_s"]
                    if receipt["first_request_monotonic_s"] is None:
                        receipt["first_request_monotonic_s"] = now
                    if previous_request is not None:
                        gap = now - previous_request
                        receipt["maximum_request_gap_s"] = max(receipt["maximum_request_gap_s"] or 0.0, gap)
                    receipt["last_request_monotonic_s"] = now
                    receipt["mode_frames_sent"] += 1
                    # Missed sends are skipped. Never burst packets to catch up.
                    next_send = time.monotonic() + .01
                state = self.feedback(require_enabled=False)
                status_hex = state["status"]["status_hex"]
                raw = state.get("raw", {})
                raw_status = raw.get("0x2a1", raw.get("0x2A1", {}))
                stamp = raw_status.get("monotonic_s")
                stamp_valid = (isinstance(stamp, (int, float)) and math.isfinite(stamp)
                               and receipt["first_request_monotonic_s"] is not None
                               and receipt["first_request_monotonic_s"] < stamp <= time.monotonic())
                receipt["last_status_hex"] = status_hex
                receipt["last_status_monotonic_s"] = stamp
                if status_hex != previous_status:
                    changes.append({"status_hex": status_hex, "received_monotonic_s": stamp})
                    previous_status = status_hex
                    transition_count += 1
                if state["status"]["can_movej"] and stamp_valid:
                    receipt.update(completed=True, post_request_ack_monotonic_s=stamp,
                                   elapsed_s=time.monotonic() - begin)
                    self._emit("handoff_confirmed", status_hex=status_hex,
                               received_monotonic_s=stamp, mode_frames_sent=receipt["mode_frames_sent"])
                    return state
                time.sleep(max(0.0, min(.005, next_send-time.monotonic(), deadline-time.monotonic())))
            raise HardwareBlocked("CAN/MOVEJ mode not confirmed by fresh post-request actual 0x2A1 feedback; no joint target sent")
        except BaseException as exc:
            receipt.update(failure=repr(exc), elapsed_s=time.monotonic() - begin)
            self._emit("handoff_failed", failure=repr(exc), mode_frames_sent=receipt["mode_frames_sent"],
                       last_status_hex=receipt["last_status_hex"])
            raise
        finally:
            receipt.update(status_changes=list(changes), status_transition_count=transition_count,
                           status_changes_truncated=transition_count > changes.maxlen)

    def keep_control_mode(self):
        """Refresh an already verified CAN/MOVEJ motion mode without a new target."""
        self._require_connected()
        if self._connection_scope != "motion":
            raise HardwareBlocked("Control heartbeat requires the motion writer scope")
        self.require_leader_isolation()
        self.require_no_foreign_commands()
        self.feedback(require_can=True)
        percent = int(self.cfg.get("native_speed_percent", 40))
        if not 1 <= percent <= 100:
            raise HardwareBlocked("Native speed percentage must be within 1..100")
        self.sdk.MotionCtrl_2(1, 1, percent, 0)

    def prime_measured_hold(self):
        """Submit the measured joint pose before an explicit motor-enable request."""
        self._require_connected()
        if self._connection_scope != "motion":
            raise HardwareBlocked("Measured hold requires the motion writer scope")
        self.require_leader_isolation()
        self.require_no_foreign_commands()
        state = self.feedback(require_can=True, require_enabled=False)
        values = validate_target_deg(state["q_deg"], self.cfg.get("command_limits_deg"))
        self.sdk.JointCtrl(*(round(value * 1000) for value in values))
        return state

    def enable(self, *, timeout_s=2.0, companion_tick=None):
        self._require_connected()
        self.require_leader_isolation()
        self.require_no_foreign_commands()
        self.feedback(require_can=True, require_enabled=False)
        deadline = time.monotonic() + timeout_s
        while True:
            self.require_no_foreign_commands()
            self.sdk.EnableArm(7, 0x02)
            state = self.feedback(require_can=True, require_enabled=False)
            if state["enabled"] is not None and len(state["enabled"]) == 6 and all(state["enabled"]):
                self._emit("enable_confirmed")
                return state
            if time.monotonic() >= deadline:
                raise HardwareBlocked("Motor enable not confirmed by real low-speed feedback")
            if companion_tick is None:
                time.sleep(.05)
            else:
                next_request = time.monotonic() + .05
                while time.monotonic() < next_request:
                    companion_tick()
                    time.sleep(.005)

    def send_joint_target_deg(self, target_deg):
        self._require_connected()
        self.require_leader_isolation()
        self.require_no_foreign_commands()
        self.feedback(require_can=True)
        values = validate_target_deg(target_deg, self.cfg.get("command_limits_deg"))
        self.sdk.MotionCtrl_2(0x01, 0x01, int(self.cfg.get("native_speed_percent", 40)), 0x00)
        self.sdk.JointCtrl(*(round(x * 1000) for x in values))

    def require_gripper_health(self, *, require_enabled=True):
        """Fresh read-only guard for the seven-dimensional policy dispatch."""
        return require_gripper_feedback(
            self.feedback(require_can=True),
            max_age_s=self.cfg.get("max_feedback_age_s", 0.2),
            require_enabled=require_enabled)

    def send_gripper_m(self, aperture_m, *, require_enabled=False):
        """Check raw gripper health before every target; never clear/zero/reset.

        Explicit preparation may enable a healthy disabled gripper. Policy
        execution must require_enabled=True to prevent silent re-enabling.
        """
        self._require_connected()
        self.require_leader_isolation()
        self.require_no_foreign_commands()
        self.require_gripper_health(require_enabled=require_enabled)
        lo, hi = self.cfg.get("gripper_bounds_m", (0.0, 0.1))
        aperture = float(aperture_m)
        if not math.isfinite(aperture) or not lo <= aperture <= hi:
            raise HardwareBlocked("Gripper target outside explicit aperture bounds; no abs/clamp")
        quantized = round(aperture * 1_000_000)
        if not lo <= quantized / 1_000_000 <= hi:
            raise HardwareBlocked("Gripper quantized target outside explicit aperture bounds; no clamp")
        effort = int(self.cfg.get("gripper_effort_mNm", 1000))
        if not 0 <= effort <= 5000:
            raise HardwareBlocked("Gripper effort outside SDK0..5000 range")
        # meters -> 0.001mm = micrometers; enable1, NEVER clear errors/zero.
        self.sdk.GripperCtrl(quantized, effort, 0x01, 0x00)

    def query_limits(self, latest_frame: Callable[[int], Mapping | None], *, timeout_s=1.0, companion_tick=None):
        """Explicit six sequential queries, not a receive-only operation.

        latest_frame(0x473) returns (monotonic_s,bytes), or equivalent raw dict.
        Requires a new timestamp AND matching motor, avoids SDK shared object cache.
        No mode, enable, motion, parameters or calibration writes are made.
        """
        self._require_connected()
        result = []
        for joint in range(1, 7):
            query_time = time.monotonic()
            self.sdk.SearchMotorMaxAngleSpdAccLimit(joint, 0x01)
            deadline = query_time + timeout_s
            while True:
                if companion_tick is not None:
                    companion_tick()
                frame = latest_frame(0x473)
                if isinstance(frame, tuple):
                    frame = {"monotonic_s": frame[0], "payload_hex": bytes(frame[1]).hex()}
                if frame is not None and float(frame["monotonic_s"]) >= query_time:
                    reply = decode_limit_reply(frame["payload_hex"])
                    if reply["joint"] == joint:
                        result.append({**reply, "received_monotonic_s": frame["monotonic_s"]})
                        break
                if time.monotonic() >= deadline:
                    raise HardwareBlocked(f"No fresh motor-specific 0x473 reply for J{joint}")
                time.sleep(.005 if companion_tick is not None else .01)
        return {"source": "six sequential raw 0x473 controller replies", "interface": self.cfg["interface"],
                "sdk_version": self.cfg.get('sdk_version',SDK_VERSION),
                "sdk_interface": 'C_PiperInterface_V3' if self.cfg.get('sdk_version')=='0.7.0a1' else 'C_PiperInterface_V2',
                "controller_firmware_version_declared": self.cfg.get('controller_firmware_version'),
                "query_only": True, "limits": result,
                "controller_limits_are_not_collision_limits": True}

    def execute_pose(self, target_deg, *, path_reviewed=False, speed_deg_s=10.0,
                     accel_deg_s2=50.0, cancel=lambda: False, transmit_hz=100.0,
                     endpoint_tolerance_deg=2.0, settle_timeout_s=3.0,
                     stationary_window_s=0.5, stationary_span_deg=0.5,
                     companion_tick=None, require_existing_can=False):
        """Cosine trajectory from fresh stationary feedback, then verify arrival.

        path_reviewed acknowledges actual workspace sweep review; it does NOT
        certify arbitrary-pose collision safety. Fault/cancel stops *new targets*,
        never automatically disables, resets, returns, or performs emergency stop.
        No per-step 0.2-rad tracking cutoff is used. Endpoint failure is reported
        after completion, so next deployment phase cannot pretend preparation passed.
        Optional companion_tick runs in this same writer thread BEFORE each joint
        publication. False means its bounded preparation is not yet confirmed;
        its exception prevents the next joint target. It must enforce its own
        deadline and may send only through this already-owned adapter. This is
        interleaved publication, not an atomic seven-axis hardware transaction.
        """
        if not path_reviewed:
            raise HardwareBlocked("Trajectory sweep review required; arbitrary current pose is not collision-verified")
        if companion_tick is not None and not callable(companion_tick):
            raise HardwareBlocked("Pose companion_tick must be callable")
        self._require_connected()
        self.require_leader_isolation()
        if not 10 <= transmit_hz <= 200:
            raise HardwareBlocked("Pose transmit_hz must be within10..200")
        state = self.feedback(require_can=require_existing_can)
        # Observe stationarity BEFORE the final effective mode handoff. A mode
        # request followed by a silent 0.5s window was an input-watchdog gap.
        # Healthy standby is a readable state, never an executable CAN substitute.
        history = [(time.monotonic(), tuple(state["q_deg"]))]
        until = time.monotonic() + stationary_window_s
        while time.monotonic() < until:
            if cancel():
                raise HardwareBlocked("Cancelled before first pose target")
            state = self.feedback(require_can=require_existing_can)
            history.append((time.monotonic(), tuple(state["q_deg"])))
            time.sleep(0.02)
        span = max(max(q[j] for _, q in history) - min(q[j] for _, q in history) for j in range(6))
        if span > stationary_span_deg:
            raise HardwareBlocked(f"Actual starting feedback is not stationary: span{span:.3f}deg")
        plan = plan_pose(state["q_deg"], target_deg, self.cfg.get("command_limits_deg"),
                         max_speed_deg_s=speed_deg_s, max_accel_deg_s2=accel_deg_s2)
        if cancel():
            raise HardwareBlocked("Cancelled before first pose target")
        if require_existing_can:
            self.feedback(require_can=True, require_enabled=True)
        self.handoff_can_movej()
        # Replan using fresh actual feedback after the acknowledged handoff, then
        # send the first reference without another stationary/model-load window.
        state = self.feedback(require_can=True)
        plan = plan_pose(state["q_deg"], target_deg, self.cfg.get("command_limits_deg"),
                         max_speed_deg_s=speed_deg_s, max_accel_deg_s2=accel_deg_s2)
        self._emit("pose_start", plan=plan.as_dict())
        start = time.monotonic()
        step = 1.0 / transmit_hz
        next_tick = start
        max_tracking_error = 0.0
        while True:
            if cancel():
                raise HardwareBlocked("Cancelled: no further targets; motors are not disabled")
            if companion_tick is not None:
                companion_tick()
            if cancel():
                raise HardwareBlocked("Cancelled after companion check; no further targets")
            elapsed = time.monotonic() - start
            target = plan.sample(elapsed)
            self.send_joint_target_deg(target)
            actual = self.feedback(require_can=True)["q_deg"]
            max_tracking_error = max(max_tracking_error, max(abs(a-b) for a,b in zip(actual,target)))
            if elapsed >= plan.duration_s:
                break
            next_tick += step
            # Do not send a burst to catch up after missed deadlines.
            if next_tick < time.monotonic():
                next_tick = time.monotonic()
            time.sleep(max(0.0, next_tick-time.monotonic()))
        deadline = time.monotonic() + settle_timeout_s
        settle = []
        final = None
        while True:
            if cancel():
                raise HardwareBlocked("Cancelled during endpoint verification; no automatic return")
            companion_ready = companion_tick() if companion_tick is not None else True
            if cancel():
                raise HardwareBlocked("Cancelled after companion check; no further targets")
            final = self.feedback(require_can=True)
            now = time.monotonic()
            settle.append((now, final["q_deg"]))
            settle = [(t,q) for t,q in settle if now-t <= stationary_window_s +0.02]
            error = max(abs(a-b) for a,b in zip(final["q_deg"],plan.target_deg))
            stable = len(settle)>1 and settle[-1][0]-settle[0][0]>=stationary_window_s
            if stable:
                stable = max(max(q[j] for _,q in settle)-min(q[j] for _,q in settle) for j in range(6)) <= stationary_span_deg
            joint_endpoint_ready = error <= endpoint_tolerance_deg and stable
            if joint_endpoint_ready and companion_ready is not False:
                planned_change = [b-a for a,b in zip(plan.start_deg, plan.target_deg)]
                measured_change = [b-a for a,b in zip(plan.start_deg, final["q_deg"])]
                # Endpoint tolerance may cover an entire small request. Report
                # measured change separately; arrival alone cannot prove motion.
                change_observed = max(abs(x) for x in measured_change) > stationary_span_deg
                report = {"status": "ENDPOINT_CONFIRMED_FROM_REAL_FEEDBACK", "plan": plan.as_dict(),
                          "final_deg": final["q_deg"], "endpoint_error_deg": error,
                          "endpoint_tolerance_deg": endpoint_tolerance_deg,
                          "planned_joint_change_deg": planned_change,
                          "measured_joint_change_deg": measured_change,
                          "target_already_within_endpoint_tolerance_at_start":
                              max(abs(x) for x in planned_change) <= endpoint_tolerance_deg,
                          "joint_change_observed": change_observed,
                          "joint_change_observation_threshold_deg": stationary_span_deg,
                          "joint_change_observation_basis":
                              "final minus fresh starting feedback; change must exceed the stationarity span threshold",
                          "tracking_error_max_deg_diagnostic_only": max_tracking_error,
                          "motors_disabled": False, "collision_verified": False}
                self._emit("pose_complete", report=report)
                return report
            if now >= deadline and not joint_endpoint_ready:
                raise HardwareBlocked(f"Endpoint not verified within{settle_timeout_s}s; actual error{error:.3f}deg. No automatic reset/disable/recovery")
            # Continue the SAME final joint target while controller settles.
            self.send_joint_target_deg(plan.target_deg)
            time.sleep(0.02)

    @property
    def reset_attempted(self):
        """A call reached ResetPiper; transport submission may remain unknown."""
        return self._reset_attempted

    @property
    def packets_sent(self):
        return dict(self._sent)
