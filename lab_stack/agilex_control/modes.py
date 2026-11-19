"""Bounded control-mode operations using real observer receipts only.

Import and plan do not construct SDKs or access CAN. The caller owns the writer
lock, pinned adapter, actual USB session and STOP channel. These timing bounds
are engineering test settings, not a manufacturer watchdog specification.
No operation publishes joint/gripper targets, motor enable or calibration.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
import math
import time

from .hardware import HardwareBlocked, decode_status, require_feedback

POLL_S = .005
REQUEST_HZ = 100.0
PREFLIGHT_S = .3
STABLE_S = .3
TAKEOVER_TIMEOUT_S = 2.0
STANDBY_TIMEOUT_S = .8
POST_REQUEST_S = .2
RESET_OBSERVE_S = 5.0
STATIONARY_SPAN_DEG = .5
MAX_SAMPLES = 128


class ModeOperationBlocked(HardwareBlocked):
    def __init__(self, code, message, receipt):
        super().__init__(message)
        self.code, self.receipt = code, receipt


def bounds():
    return {"request_hz": REQUEST_HZ, "poll_s": POLL_S, "preflight_s": PREFLIGHT_S,
            "stable_feedback_s": STABLE_S, "takeover_timeout_s": TAKEOVER_TIMEOUT_S,
            "standby_timeout_s": STANDBY_TIMEOUT_S, "post_request_observe_s": POST_REQUEST_S,
            "reset_observe_s": RESET_OBSERVE_S, "stationary_span_deg": STATIONARY_SPAN_DEG,
            "basis": "engineering test bounds; factory watchdog timing is unverified"}


def plan(operation):
    if operation not in ("takeover", "reset", "high-follow", "standby", "disable"):
        raise ValueError("Unknown mode operation")
    if operation in ('high-follow', 'standby', 'disable'):
        return {"operation": operation, "applied": False, "status": "PLAN_ONLY_NO_CAN_ACCESS",
                "completed": False, "CAN_commands_sent": 0, "SDK_constructed": False,
                "steps": ['fresh healthy stationary preflight',
                          ('review fresh aligned OEM targets; request CAN/MOVEJ/AD without role/target/enable'
                           if operation == 'high-follow' else
                           'request standby only' if operation == 'standby' else
                           'disable joints 1..6 once each; do not disable gripper'),
                          'confirm distinct fresh feedback; retain unobservable internal-state limitation'],
                "engineering_bounds": bounds(), "requested_mode": requested_mode(operation),
                "required_confirmations": (['supported', 'roles-configured', 'poses-aligned', 'source-reviewed', 'teaching-ended']
                                           if operation == 'high-follow' else
                                           ['supported', 'leader-isolated', 'teaching-ended']),
                "controller_mode_confirmed": False, "targets_sent": False,
                "enabled_by_operation": False, "reset_attempted": False,
                "roles_configured_by_operation": False, "gripper_disabled_by_operation": False}
    return {"operation": operation, "applied": False, "status": "PLAN_ONLY_NO_CAN_ACCESS",
            "completed": False, "CAN_commands_sent": 0, "SDK_constructed": False,
            "steps": (["fresh stationary preflight", "request standby", "request CAN/MOVEJ position-speed",
                       "confirm distinct feedback for 0.3s", "observe 0.2s after requests stop"]
                      if operation == "takeover" else
                      ["fresh stationary preflight", "one explicitly supported ResetPiper",
                       "observe 5s without retry/enable/targets"]),
            "engineering_bounds": bounds(), "controller_mode_confirmed": False,
            "requested_mode": requested_mode(operation),
            "required_confirmations": ["leader-isolated", "supported"]
                                      + (["teaching-ended"] if operation == "takeover" else ["zero-pose-reviewed"]),
            "reset_attempted": False, "reset_submitted": False, "recovery_confirmed": False,
            "targets_sent": False, "enabled_by_operation": False,
            "manual_guidance": guidance()}


def requested_mode(operation):
    if operation == 'high-follow':
        return {'ctrl_mode': 1, 'move_mode': 1, 'is_mit_mode': 0xAD,
                'MIT_internal_state_confirmed': False,
                'high_follow_requested': True,
                'normal_position_speed_feedback_note': '0x2A1 has no is_mit_mode field; OEM roles are not changed'}
    if operation == 'standby':
        return {'ctrl_mode': 0, 'motors_disabled_by_request': False}
    if operation == 'disable':
        return {'disabled_joint_numbers': [1,2,3,4,5,6], 'gripper_disable_requested': False}
    return ({"ctrl_mode": 1, "move_mode": 1, "is_mit_mode": 0,
             "normal_position_speed_requested": True,
             "MIT_internal_state_confirmed": False,
             "normal_position_speed_feedback_note": "0x2A1 has no is_mit_mode field"}
            if operation == "takeover" else None)


def guidance():
    return ["结束实体按钮示教记录或轨迹回放，读取真实状态确认；teach_status=2 仅表示结束记录。",
            "隔离实体主臂和其他发送程序；软件 writer 锁不能阻断总线上的其他设备。",
            "旧固件高跟随/MIT 内部状态可能需要原厂复位流程；仅在可靠承托、机械零位已审查后单独提交一次复位。",
            "ResetPiper 会立即失去电机保持力；复位提交、反馈恢复和 CAN/MOVEJ 接管是三个独立结果。"]


def _frame(snapshot):
    raw = snapshot.get("raw", {})
    raw = raw.get("0x2a1", raw.get("0x2A1", {}))
    stamp = raw.get("monotonic_s")
    payload = raw.get("payload_hex")
    valid = (isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
             and math.isfinite(stamp) and stamp <= time.monotonic())
    if not valid or payload is None or payload != snapshot.get("status_hex"):
        raise HardwareBlocked("Actual raw 0x2A1 timestamp/payload is missing or inconsistent")
    status = decode_status(payload)
    decoded = snapshot.get("status")
    if isinstance(decoded, Mapping):
        for key in ("ctrl_mode", "arm_status", "mode_feed", "teach_status",
                    "motion_status", "trajectory_num", "err_code"):
            if decoded.get(key) != status[key]:
                raise HardwareBlocked("Decoded status disagrees with actual raw 0x2A1 field: "+key)
    return stamp, status


def _sample(snapshot, phase):
    record = {"observed_monotonic_s": time.monotonic(), "phase": phase,
              "status_hex": snapshot.get("status_hex"), "raw_status": None,
              "q_deg": snapshot.get("q_deg"), "enabled": snapshot.get("enabled"),
              "motor_flags": snapshot.get("motor_flags"),
              "feedback_ages_s": {key: snapshot.get(key) for key in
                                  ("age_status_s", "age_joint_s", "age_motor_s")},
              "observer_error": snapshot.get("observer_error"),
              "timestamp_failures": snapshot.get("timestamp_failures"),
              "external_commands": snapshot.get("external_commands", {})}
    try:
        stamp, status = _frame(snapshot)
        record.update(received_monotonic_s=stamp, status=status)
        raw = snapshot.get("raw", {})
        record["raw_status"] = dict(raw.get("0x2a1", raw.get("0x2A1", {})))
    except (HardwareBlocked, ValueError, TypeError) as exc:
        record["feedback_error"] = str(exc)
    return record


class _Evidence:
    def __init__(self, operation):
        self.samples = deque(maxlen=MAX_SAMPLES)
        self.count = 0
        self.last_key = None
        self.receipt = {"operation": operation, "applied": True, "completed": False,
                        "controller_mode_confirmed": False, "reset_attempted": False, "reset_submitted": False,
                        "recovery_confirmed": False, "targets_sent": False,
                        "enabled_by_operation": False, "packets_sent": {},
                        "before": None, "after": None, "engineering_bounds": bounds(),
                        "requested_mode": requested_mode(operation),
                        "manual_guidance": guidance(), "mode_was_sustained_while_requesting": False,
                        "request_count": 0, "maximum_request_gap_s": None,
                        "started_monotonic_s": time.monotonic()}

    def observe(self, snapshot, phase):
        sample = _sample(snapshot, phase)
        key = (phase, sample.get("received_monotonic_s"), sample.get("status_hex"),
               sample.get("feedback_error"), sample.get("observer_error"))
        if key != self.last_key:
            self.samples.append(sample)
            self.count += 1
            self.last_key = key
        self.receipt["after"] = sample
        return sample

    def finish(self, adapter=None):
        self.receipt.update(samples=list(self.samples), sample_count=self.count,
                            samples_truncated=self.count > MAX_SAMPLES,
                            elapsed_s=time.monotonic()-self.receipt["started_monotonic_s"])
        if adapter is not None:
            self.receipt["packets_sent"] = adapter.packets_sent
            if self.receipt['operation'] in ('high-follow','standby','disable'):
                self.receipt['CAN_commands_sent'] = sum(adapter.packets_sent.values())
            if self.receipt["operation"] == "reset":
                count = adapter.packets_sent.get("0x150", 0)
                self.receipt.update(reset_attempted=adapter.reset_attempted,
                                    reset_packets_sent=count, reset_submitted=count == 1)
        return self.receipt

    def block(self, code, message, adapter=None):
        self.receipt.update(status=code, failure_code=code, failure=message)
        raise ModeOperationBlocked(code, message, self.finish(adapter))


def _not_cancelled(cancel):
    if cancel():
        raise HardwareBlocked("STOP/cancel requested; no further mode or reset frames")


def _state(adapter, evidence, phase, *, allow_teach=False, reviewed_oem=False):
    snapshot = adapter.snapshot()
    evidence.observe(snapshot, phase)
    if reviewed_oem:
        _state_value, source = adapter.require_reviewed_oem_source(snapshot)
        evidence.receipt['last_oem_source'] = source
    else:
        adapter.require_no_foreign_commands()
    # Full real joint/status/motor health is required before changing controller
    # ownership. Motor enable is deliberately not required or modified.
    state = require_feedback(snapshot, require_enabled=False,
                             max_age_s=adapter.cfg.get("max_feedback_age_s", .2))
    stamp, status = _frame(snapshot)
    if snapshot.get("observer_error") or snapshot.get("timestamp_failures"):
        raise HardwareBlocked("Actual CAN observer reports receive/timestamp failure")
    if not allow_teach:
        if status["ctrl_mode"] in (2, 5, 6, 7) or status["teach_status"] in (1, 3, 4, 5, 7):
            raise HardwareBlocked("TEACH/remote/linkage/offline playback still active; finish with physical button/original procedure")
        if status["ctrl_mode"] not in (0, 1) or status["teach_status"] not in (0, 2, 6):
            raise HardwareBlocked("Unsupported/unknown controller or teaching state; review actual 0x2A1")
    return state, stamp


def _preflight(adapter, evidence, cancel, *, allow_teach=False, reviewed_oem=False):
    if not reviewed_oem:
        adapter.require_leader_isolation()
    history = []
    target_history = []
    deadline = time.monotonic()+PREFLIGHT_S
    last_stamp = None
    while True:
        _not_cancelled(cancel)
        state, stamp = _state(adapter, evidence, "preflight", allow_teach=allow_teach, reviewed_oem=reviewed_oem)
        if stamp != last_stamp:
            history.append((stamp, state["q_deg"]))
            if reviewed_oem:
                target_history.append(evidence.receipt['last_oem_source']['q_deg'])
            last_stamp = stamp
        if time.monotonic() >= deadline:
            break
        time.sleep(min(POLL_S, max(0, deadline-time.monotonic())))
    if len(history) < 2 or history[-1][0]-history[0][0] < PREFLIGHT_S-.02:
        raise HardwareBlocked("Distinct fresh actual feedback did not span the preflight window")
    span = max(max(q[j] for _, q in history)-min(q[j] for _, q in history) for j in range(6))
    if span > STATIONARY_SPAN_DEG:
        raise HardwareBlocked(f"Actual joints are not stationary before mode change: {span:.3f} degrees")
    evidence.receipt["preflight_joint_span_deg"] = span
    if reviewed_oem:
        target_span = max(max(q[j] for q in target_history)-min(q[j] for q in target_history) for j in range(6))
        if target_span > STATIONARY_SPAN_DEG:
            raise HardwareBlocked('OEM source targets are not stationary before high-follow request')
        evidence.receipt['preflight_oem_target_span_deg'] = target_span
        evidence.receipt['before_oem_source'] = evidence.receipt['last_oem_source']
    evidence.receipt["before"] = evidence.receipt["after"]
    return state


def control_action(adapter, operation, *, supported=False, teaching_ended=False,
                   roles_configured=False, poses_aligned=False, source_reviewed=False,
                   cancel=lambda: False):
    """Bounded mode-only or six-joint disable action; no target or role writes.

    High-follow only restores a reviewed existing OEM path. CAN/MOVEJ feedback
    does not expose the AD bit, and cannot establish physical following success.
    """
    if operation not in ('high-follow', 'standby', 'disable'):
        raise ValueError('Unknown control action')
    evidence = _Evidence(operation)
    evidence.receipt.update(roles_configured_by_operation=False, gripper_disabled_by_operation=False,
                            physical_teleoperation_confirmed=False, motor_disable_confirmed=False)
    reviewed = operation == 'high-follow'
    try:
        if adapter.cfg.get('interface') != 'can_right':
            evidence.block('RIGHT_ARM_ONLY', 'New controller actions are authorized only for can_right', adapter)
        if supported is not True or teaching_ended is not True:
            evidence.block('SUPPORT_AND_TEACHING_REVIEW_REQUIRED',
                           'Reliable physical support and ended physical teaching/playback are required', adapter)
        if reviewed and any(flag is not True for flag in (roles_configured, poses_aligned, source_reviewed)):
            evidence.block('OEM_SOURCE_REVIEW_REQUIRED',
                           'Existing OEM roles, physically aligned poses and source must be reviewed', adapter)
        _preflight(adapter, evidence, cancel, reviewed_oem=reviewed)
        _not_cancelled(cancel)
        begin = time.monotonic()
        evidence.receipt['first_request_monotonic_s'] = begin
        previous_request = None
        if operation == 'disable':
            for joint in range(1,7):
                _not_cancelled(cancel)
                adapter.disable_joint_once(joint)
                evidence.receipt['request_count'] += 1
            previous_request = time.monotonic()
        deadline = time.monotonic()+STANDBY_TIMEOUT_S
        next_send = time.monotonic()
        stable_first = stable_last = None
        distinct = 0
        while time.monotonic() < deadline:
            _not_cancelled(cancel)
            state, stamp = _state(adapter, evidence, operation, reviewed_oem=reviewed)
            now = time.monotonic()
            if operation != 'disable' and now >= next_send:
                _not_cancelled(cancel)
                if reviewed:
                    adapter.request_high_follow()
                else:
                    adapter.request_control_mode(standby=True)
                evidence.receipt['request_count'] += 1
                if previous_request is not None:
                    evidence.receipt['maximum_request_gap_s'] = max(
                        evidence.receipt['maximum_request_gap_s'] or 0, now-previous_request)
                previous_request = now
                next_send = time.monotonic()+1/REQUEST_HZ
            confirmed = (state['status']['can_movej'] if reviewed else
                         state['status']['healthy_standby'] if operation == 'standby' else
                         _disabled_motor_stamp(adapter.snapshot(), previous_request))
            if operation == 'disable':
                stamp = confirmed if confirmed is not None else stamp
            if previous_request is not None and stamp > begin and confirmed:
                if stable_first is None:
                    stable_first = stable_last = stamp
                    distinct = 1
                elif stamp != stable_last:
                    stable_last = stamp
                    distinct += 1
                if distinct >= 2 and stable_last-stable_first >= STABLE_S:
                    break
            else:
                stable_first = stable_last = None
                distinct = 0
            time.sleep(min(POLL_S, max(.000001, deadline-time.monotonic())))
        else:
            evidence.block('CONTROL_ACTION_FEEDBACK_TIMEOUT',
                           'Requested state not confirmed by distinct fresh feedback; no automatic reset, enable or retry', adapter)
        evidence.receipt.update(last_request_monotonic_s=previous_request,
                                confirmation_feedback_span_s=stable_last-stable_first,
                                confirmation_distinct_count=distinct)
        post_begin = time.monotonic()
        post_stamps = set()
        while True:
            _not_cancelled(cancel)
            state, stamp = _state(adapter, evidence, 'post_request', reviewed_oem=reviewed)
            confirmed = (state['status']['can_movej'] if reviewed else
                         state['status']['healthy_standby'] if operation == 'standby' else
                         _disabled_motor_stamp(adapter.snapshot(), previous_request))
            if not confirmed:
                evidence.block('CONTROL_ACTION_REVERTED', 'Independent feedback left the requested observable state', adapter)
            stamp = confirmed if operation == 'disable' else stamp
            if stamp > post_begin:
                post_stamps.add(stamp)
            if time.monotonic()-post_begin >= POST_REQUEST_S:
                break
            time.sleep(POLL_S)
        if len(post_stamps) < 2 or max(post_stamps)-min(post_stamps) < POST_REQUEST_S-.02:
            evidence.block('POST_REQUEST_FEEDBACK_INCOMPLETE', 'Fresh independent feedback did not span post-request window', adapter)
        evidence.receipt.update(completed=True, controller_mode_confirmed=operation == 'standby',
                                motor_disable_confirmed=operation == 'disable')
        if reviewed:
            evidence.receipt.update(status='HIGH_FOLLOW_REQUEST_SENT_CAN_MOVEJ_OBSERVED_AD_UNVERIFIED',
                                    can_movej_feedback_confirmed=True,
                                    limitation='0x2A1 confirms CAN/MOVEJ only; AD internal state, OEM role identity and physical teleoperation success are unverified')
        elif operation == 'standby':
            evidence.receipt.update(status='STANDBY_CONFIRMED_MOTOR_DISABLE_NOT_REQUESTED',
                                    limitation='Standby feedback does not establish motor disable or physical support')
        else:
            evidence.receipt.update(status='SIX_JOINTS_DISABLED_CONFIRMED_GRIPPER_UNCHANGED',
                                    limitation='Only six joint disabled bits were confirmed; gripper was not commanded; power-loss safety is not certified')
        return evidence.finish(adapter)
    except ModeOperationBlocked:
        raise
    except (HardwareBlocked, ValueError, TypeError) as exc:
        evidence.block('CONTROL_ACTION_BLOCKED', str(exc), adapter)


def _disabled_motor_stamp(snapshot, submitted_at):
    """Require independent per-joint raw 0x261..266 bits received after send."""
    now = time.monotonic()
    stamps = []
    for index in range(6):
        frame = snapshot.get('raw', {}).get(hex(0x261+index), {})
        stamp = frame.get('monotonic_s')
        try:
            payload = bytes.fromhex(frame['payload_hex'])
        except (KeyError, TypeError, ValueError):
            return None
        if (len(payload) != 8 or type(stamp) not in (int,float) or not math.isfinite(stamp)
                or not submitted_at < stamp <= now or now-stamp > .2
                or payload[5] != 0):
            return None
        if snapshot.get('enabled') != [False]*6 or snapshot.get('motor_flags') != [0]*6:
            return None
        stamps.append(stamp)
    return min(stamps)


def inspect(snapshot, *, seconds=1.0, cancel=lambda: False):
    """Receive-only bounded snapshot callable; no writer or query construction."""
    seconds = float(seconds)
    if not math.isfinite(seconds) or not .3 <= seconds <= 5:
        raise ValueError("Mode inspection seconds must be within 0.3..5")
    evidence = _Evidence("inspect")
    evidence.receipt.update(applied=False, CAN_commands_sent=0, SDK_constructed=False)
    deadline = time.monotonic()+seconds
    while True:
        if cancel():
            evidence.receipt["cancelled"] = True
            break
        evidence.observe(snapshot(), "inspect")
        if time.monotonic() >= deadline:
            break
        time.sleep(min(.02, max(0, deadline-time.monotonic())))
    evidence.receipt.update(status="RECEIVE_ONLY_OBSERVATION_COMPLETE", completed=True)
    return evidence.finish()


def takeover(adapter, *, supported=False, cancel=lambda: False):
    """Explicit standby -> CAN MOVEJ request plus fresh sustained observation."""
    evidence = _Evidence("takeover")
    try:
        if supported is not True:
            evidence.block("SUPPORT_REQUIRED", "Actual reliable physical support must be confirmed before standby/CAN mode handoff", adapter)
        _preflight(adapter, evidence, cancel)
        begin = time.monotonic()
        total_deadline = begin+TAKEOVER_TIMEOUT_S
        previous_request = None
        for phase, standby in (("standby", True), ("can_movej", False)):
            phase_deadline = min(total_deadline, time.monotonic()+STANDBY_TIMEOUT_S) if standby else total_deadline
            next_send = (time.monotonic() if previous_request is None else
                         max(time.monotonic(), previous_request+1/REQUEST_HZ))
            first_request = None
            stable_first = None
            stable_last = None
            distinct = 0
            while time.monotonic() < phase_deadline:
                _not_cancelled(cancel)
                state, stamp = _state(adapter, evidence, phase)
                now = time.monotonic()
                if now >= next_send:
                    _not_cancelled(cancel)
                    adapter.request_control_mode(standby=standby)
                    if first_request is None:
                        first_request = now
                        evidence.receipt[phase+"_first_request_monotonic_s"] = now
                    evidence.receipt["request_count"] += 1
                    if previous_request is not None:
                        gap = now-previous_request
                        evidence.receipt["maximum_request_gap_s"] = max(
                            evidence.receipt["maximum_request_gap_s"] or 0, gap)
                    previous_request = now
                    next_send = time.monotonic()+1/REQUEST_HZ
                target = state["status"]["healthy_standby"] if standby else state["status"]["can_movej"]
                if first_request is not None and stamp > first_request and target:
                    if stable_first is None:
                        stable_first, stable_last, distinct = stamp, stamp, 1
                    elif stamp != stable_last:
                        stable_last, distinct = stamp, distinct+1
                    required = .02 if standby else STABLE_S
                    if distinct >= 2 and stable_last-stable_first >= required:
                        evidence.receipt[phase+"_feedback_span_s"] = stable_last-stable_first
                        evidence.receipt[phase+"_distinct_feedback_count"] = distinct
                        break
                else:
                    stable_first = stable_last = None
                    distinct = 0
                time.sleep(max(.000001, min(POLL_S, next_send-time.monotonic(),
                                           phase_deadline-time.monotonic())))
            else:
                evidence.block("MODE_CONFIRMATION_TIMEOUT", phase+" was not confirmed by distinct fresh feedback within bounded requests", adapter)
        evidence.receipt["mode_was_sustained_while_requesting"] = True
        evidence.receipt["last_request_monotonic_s"] = previous_request
        deadline = time.monotonic()+POST_REQUEST_S
        post_stamps = set()
        while True:
            _not_cancelled(cancel)
            state, stamp = _state(adapter, evidence, "post_request")
            if not state["status"]["can_movej"]:
                evidence.block("CAN_MOVEJ_REVERTED_AFTER_REQUESTS", "Real feedback left CAN/MOVEJ after requests stopped; mode persistence is not confirmed", adapter)
            if stamp > previous_request:
                post_stamps.add(stamp)
            if time.monotonic() >= deadline:
                break
            time.sleep(min(POLL_S, max(0, deadline-time.monotonic())))
        if len(post_stamps) < 2 or max(post_stamps)-min(post_stamps) < POST_REQUEST_S-.02:
            evidence.block("POST_REQUEST_FEEDBACK_INCOMPLETE", "No distinct fresh feedback spanning the post-request observation window", adapter)
        evidence.receipt.update(status="CAN_MOVEJ_CONFIRMED_WITH_BOUNDED_POST_OBSERVATION",
                                completed=True, controller_mode_confirmed=True,
                                limitation="Bounded mode-only feedback evidence; motor enable, motion, long-term persistence and MIT internal state remain unverified")
        return evidence.finish(adapter)
    except ModeOperationBlocked:
        raise
    except (HardwareBlocked, ValueError, TypeError) as exc:
        evidence.block("MODE_TAKEOVER_BLOCKED", str(exc), adapter)


def reset(adapter, *, supported=False, cancel=lambda: False):
    """Single supported reset, retaining startup transient and recovery evidence."""
    evidence = _Evidence("reset")
    try:
        if not supported:
            evidence.block("SUPPORT_REQUIRED", "Actual reliable physical support must be confirmed before reset", adapter)
        _preflight(adapter, evidence, cancel, allow_teach=True)
        _not_cancelled(cancel)
        submitted_at = time.monotonic()
        adapter.reset_supported_once(supported=True)
        evidence.receipt.update(reset_submitted=True, reset_submitted_monotonic_s=submitted_at,
                                reset_packets_sent=adapter.packets_sent.get("0x150", 0))
        deadline = time.monotonic()+RESET_OBSERVE_S
        tail = deque()
        observed_problems = deque(maxlen=32)
        evidence.receipt["observed_recovery_problems"] = []
        last_problem = None
        last_stamp = None
        while True:
            _not_cancelled(cancel)
            try:
                state, stamp = _state(adapter, evidence, "reset_recovery", allow_teach=True)
                if stamp <= submitted_at:
                    raise HardwareBlocked("Reset recovery feedback predates reset request")
                if state["enabled"] is None or len(state["enabled"]) != 6 or any(state["enabled"]):
                    raise HardwareBlocked("All six motors are not yet confirmed disabled after reset")
                if stamp != last_stamp:
                    tail.append((stamp, state["q_deg"]))
                    last_stamp = stamp
                while tail and stamp-tail[0][0] > STABLE_S+.03:
                    tail.popleft()
                last_problem = None
            except (HardwareBlocked, ValueError, TypeError) as exc:
                problem = str(exc)
                tail.clear()
                if problem != last_problem:
                    observed_problems.append({"monotonic_s": time.monotonic(), "problem": problem})
                    evidence.receipt["observed_recovery_problems"] = list(observed_problems)
                last_problem = problem
                # A sender appearing after reset is not a boot transient.
                if "external CAN command" in problem:
                    raise
            if time.monotonic() >= deadline:
                break
            time.sleep(min(.02, max(0, deadline-time.monotonic())))
        evidence.receipt["observed_recovery_problems"] = list(observed_problems)
        span = (max(max(q[j] for _, q in tail)-min(q[j] for _, q in tail) for j in range(6))
                if tail else None)
        evidence.receipt["recovery_joint_span_deg"] = span
        recovered = (len(tail) >= 2 and tail[-1][0]-tail[0][0] >= STABLE_S-.02
                     and span <= STATIONARY_SPAN_DEG and last_problem is None)
        if not recovered:
            evidence.block("RESET_SUBMITTED_RECOVERY_NOT_CONFIRMED", "One reset was submitted; fresh healthy disabled stationary recovery was not confirmed within 5s", adapter)
        evidence.receipt.update(status="SUPPORTED_RESET_SUBMITTED_RECOVERY_CONFIRMED",
                                completed=True, recovery_confirmed=True,
                                controller_mode_confirmed=False,
                                limitation="Reset does not establish CAN/MOVEJ or exit teaching; re-inspect actual mode before a separate takeover")
        return evidence.finish(adapter)
    except ModeOperationBlocked:
        raise
    except (HardwareBlocked, ValueError, TypeError) as exc:
        evidence.block("RESET_BLOCKED_OR_OBSERVATION_STOPPED", str(exc), adapter)
