# PC PowerShell，项目根目录：以下只运行离线测试，不连接机械臂。
# python -X utf8 -B -m unittest discover -s tests -p 'test_2026_10_08_17_50_preparation.py' -v
# 实机由统一 deploy 或 move ready --close-gripper 入口调用；不可独立运行本模块。
"""Verify the measured gripper component of the seven-dimensional ready state.

Sequential deployment preparation and opt-in concurrent ready-pose preparation
reuse the already-owned PiperAdapter. This is preparation for an empty gripper and
a reviewed training start state, not a calibration or automatic fault recovery.
The caller owns authorization, recording, cancellation, writer ownership and
the six-joint path. Importing this module opens no hardware and sends nothing.
"""
from collections import deque
import math
import time

from .hardware import HardwareBlocked, require_feedback
from .gripper_state import decode_gripper_feedback


POLL_S = .02
SEND_INTERVAL_S = .1
MAX_SAMPLES = 64
MAX_READY_GRIPPER_SPEED_M_S = .010


def _finite(value, name, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (positive and value <= 0)):
        raise HardwareBlocked(f"{name} must be a finite {'positive ' if positive else ''}number")
    return float(value)


def _settings(cfg):
    # The target and tolerance must be explicit. Do not invent a physical
    # gripper stroke or silently fall back to an unreviewed initial position.
    target = _finite(cfg.get("ready_gripper_m"), "ready_gripper_m")
    tolerance = _finite(cfg.get("ready_gripper_tolerance_m"), "ready_gripper_tolerance_m", positive=True)
    timeout = _finite(cfg.get("ready_gripper_timeout_s", 5.), "ready_gripper_timeout_s", positive=True)
    stable = _finite(cfg.get("ready_gripper_stable_s", .3), "ready_gripper_stable_s", positive=True)
    bounds = cfg.get("gripper_bounds_m")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
        raise HardwareBlocked("Explicit gripper_bounds_m pair required")
    lo, hi = [_finite(value, "gripper bound") for value in bounds]
    if not lo < hi or not lo <= target <= hi:
        raise HardwareBlocked("Ready gripper target outside explicit command bounds; no clipping")
    if tolerance >= hi - lo or stable >= timeout:
        raise HardwareBlocked("Ready gripper tolerance/window incompatible with bounds/timeout")
    age = cfg.get("max_feedback_age_s", cfg.get("can", {}).get("right", {}).get("stale_after", .2))
    max_age = _finite(age, "max_feedback_age_s", positive=True)
    return target, tolerance, timeout, stable, (lo, hi), max_age


def _measurement(state, max_age, *, require_can=True):
    """Use genuine 0x2A8 receipt time; polling the same cache is not new data."""
    state = require_feedback(state, max_age_s=max_age, require_can=require_can)
    actual = _finite(state.get("grip_m"), "actual gripper feedback")
    age = _finite(state.get("age_gripper_s"), "gripper feedback age")
    raw = state.get("raw", {}).get("0x2a8", state.get("raw", {}).get("0x2A8", {}))
    stamp = _finite(raw.get("monotonic_s"), "0x2A8 receipt timestamp")
    now = time.monotonic()
    if not 0 <= age <= max_age or not 0 <= now - stamp <= max_age:
        raise HardwareBlocked("Gripper feedback missing, stale or future-dated; no new targets")
    try:
        decoded = decode_gripper_feedback(raw.get("payload_hex", ""))
    except ValueError as exc:
        raise HardwareBlocked("Malformed actual gripper 0x2A8 payload") from exc
    if decoded.get('linear_position_available') is not True:
        raise HardwareBlocked('Gripper preparation requires linear width feedback; angle/unknown mode blocked')
    if not math.isclose(decoded["aperture_m"], actual, rel_tol=0., abs_tol=1e-9):
        raise HardwareBlocked("Actual gripper value disagrees with raw 0x2A8 feedback")
    decoded.update(received_monotonic_s=stamp, age_s=age)
    if not decoded["healthy"]:
        error = HardwareBlocked("Gripper controller reports fault " + decoded["status_hex"]
                                + ": " + ", ".join(decoded["fault_flags"])
                                + "; no reset/error-clear/zero or further preparation targets")
        error.gripper_feedback = decoded
        raise error
    return actual, stamp, decoded


def enforce_ready_gripper(cfg, state):
    """Read-only instantaneous start check; does not claim stable preparation.

The caller supplies fresh ``adapter.feedback(require_can=True)`` before/after
startup inference. A current measurement outside the command bounds cannot
become a legal policy anchor merely because it is within the ready tolerance.
"""
    target, tolerance, _timeout, _stable, bounds, max_age = _settings(cfg)
    try:
        actual, stamp, decoded = _measurement(state, max_age)
    except HardwareBlocked as exc:
        exc.preparation_receipt = {"status": "READY_GRIPPER_CHECK_FAILED",
                                   "target_m": target, "failure": repr(exc),
                                   "gripper_feedback": getattr(exc, "gripper_feedback", None),
                                   "automatic_reset_disable_return": False}
        raise
    result = {"target_m": target, "actual_m": actual, "tolerance_m": tolerance,
              "error_m": abs(actual - target), "received_monotonic_s": stamp,
              "within_command_bounds": bounds[0] <= actual <= bounds[1],
              "gripper_feedback": decoded, "gripper_enabled": decoded["enabled"]}
    if not decoded["enabled"] or not result["within_command_bounds"] or result["error_m"] > tolerance:
        error = HardwareBlocked("Actual gripper not at configured training-ready start: " + repr(result))
        error.preparation_receipt = result
        raise error
    return result


class _ReadyGripperTracker:
    """One bounded RAM tracker, reused by sequential and interleaved preparation."""
    def __init__(self, cfg, adapter, cancel, *, speed_m_s=None):
        (self.target, self.tolerance, self.timeout, self.stable_s,
         self.bounds, self.max_age) = _settings(cfg)
        self.adapter, self.cancel = adapter, cancel
        speed_m_s = cfg.get("ready_gripper_speed_m_s") if speed_m_s is None else speed_m_s
        self.speed_m_s = None if speed_m_s is None else _finite(
            speed_m_s, "ready_gripper_speed_m_s", positive=True)
        if self.speed_m_s is not None and self.speed_m_s > MAX_READY_GRIPPER_SPEED_M_S:
            raise HardwareBlocked("Ready gripper software reference speed exceeds the authorized 10 mm/s; no clipping")
        self.send_interval_s = SEND_INTERVAL_S
        self.minimum_resolvable_speed_m_s = None
        if self.speed_m_s is not None:
            hz = _finite(cfg.get("pose_send_hz", 50.), "pose_send_hz", positive=True)
            if not 10. <= hz <= 200.:
                raise HardwareBlocked("Rate-limited gripper pose_send_hz must be within 10..200")
            if not math.isclose(self.target * 1e6, round(self.target * 1e6),
                                rel_tol=0., abs_tol=1e-7):
                raise HardwareBlocked("Rate-limited gripper target must use the SDK micrometre resolution")
            self.send_interval_s = 1. / hz
            self.minimum_resolvable_speed_m_s = hz / 1e6
            if self.speed_m_s < self.minimum_resolvable_speed_m_s:
                raise HardwareBlocked(
                    f"Gripper reference speed is below one SDK micrometre per nominal tick: "
                    f"minimum {self.minimum_resolvable_speed_m_s * 1000:g} mm/s at {hz:g} Hz")
        self.begin = self.deadline = self.next_send = None
        self.last_send = self.reference_start = self.reference_position = None
        self.reference_elapsed_s = 0.
        self.reference_complete = self.speed_m_s is None
        self.enabled_observed = False
        self.samples = deque(maxlen=MAX_SAMPLES)
        self.previous_stamp = None
        self.stable_first = self.stable_last = self.stable_low = self.stable_high = None
        self.stable_count = 0
        self.confirmed = False
        self.receipt = {"status": "PREPARING", "target_m": self.target,
                        "tolerance_m": self.tolerance, "timeout_s": self.timeout,
                        "required_stable_s": self.stable_s,
                        "stability_span_limit_m": self.tolerance,
                        "samples": [], "samples_total": 0, "targets_sent": 0,
                        "first_target_monotonic_s": None, "initial_actual_m": None,
                        "final_actual_m": None, "feedback_source": "real 0x2A8 gripper feedback",
                        "assumption": "empty gripper; reviewed training start",
                        "command_receipt_scope": "gripper tracker only; joint commands are in pose receipt",
                        "joint_targets_sent": False, "automatic_reset_disable_return": False}
        self.receipt.update(reference_speed_m_s=self.speed_m_s,
                            reference_send_hz=1. / self.send_interval_s,
                            authorized_reference_speed_max_m_s=MAX_READY_GRIPPER_SPEED_M_S,
                            minimum_resolvable_speed_m_s=self.minimum_resolvable_speed_m_s,
                            reference_start_m=None, expected_travel_s=0.,
                            reference_elapsed_s=0., reference_complete=self.reference_complete,
                            reference_end_monotonic_s=None,
                            actual_physical_speed_verified=False,
                            rate_limit_basis="successive submitted targets and elapsed host time; no catch-up burst",
                            fixed_deadline_monotonic_s=None)

    def check_cancel(self):
        if self.cancel():
            raise HardwareBlocked("Cancelled during ready-gripper preparation; no automatic recovery")

    def tick(self):
        self.check_cancel()
        if self.begin is None:
            self.begin = time.monotonic()
            self.deadline, self.next_send = self.begin + self.timeout, self.begin
            self.receipt["started_monotonic_s"] = self.begin
        if not self.confirmed and time.monotonic() >= self.deadline:
            raise HardwareBlocked("Ready gripper did not reach stable actual feedback before timeout")
        state = self.adapter.feedback(require_can=True)
        actual, stamp, decoded = _measurement(state, self.max_age)
        self.check_cancel()
        now = time.monotonic()
        self.receipt.update(final_actual_m=actual, gripper_feedback=decoded,
                            gripper_enabled=decoded["enabled"])
        if not self.confirmed and now >= self.deadline:
            raise HardwareBlocked("Ready gripper feedback arrived after preparation timeout")
        if self.receipt["initial_actual_m"] is None:
            self.receipt["initial_actual_m"] = actual
            if self.speed_m_s is not None:
                if not self.bounds[0] <= actual <= self.bounds[1]:
                    raise HardwareBlocked("Initial actual gripper outside command bounds; no clipping or zero adjustment")
                self.reference_start = self.reference_position = actual
                self.reference_complete = (decoded["enabled"] and
                                           abs(actual - self.target) <= self.tolerance)
                travel_s = 0. if self.reference_complete else abs(actual - self.target) / self.speed_m_s
                self.deadline = self.begin + travel_s + self.timeout
                self.receipt.update(reference_start_m=actual, expected_travel_s=travel_s,
                                    reference_complete=self.reference_complete,
                                    fixed_deadline_monotonic_s=self.deadline)
        if self.confirmed and not decoded["enabled"]:
            raise HardwareBlocked("Ready gripper lost confirmed enabled state during pose; no automatic re-enable")
        if self.speed_m_s is not None and self.enabled_observed and not decoded["enabled"]:
            raise HardwareBlocked("Ready gripper lost observed enabled state; no automatic re-enable")
        self.enabled_observed = self.enabled_observed or decoded["enabled"]
        if self.previous_stamp is not None and stamp < self.previous_stamp:
            raise HardwareBlocked("Gripper feedback receipt clock moved backwards")
        matched = (decoded["enabled"] and self.bounds[0] <= actual <= self.bounds[1]
                   and abs(actual - self.target) <= self.tolerance)
        if stamp != self.previous_stamp:
            self.receipt["samples_total"] += 1
            self.samples.append({"received_monotonic_s": stamp, "actual_m": actual,
                                 "error_m": abs(actual - self.target),
                                 "within_target_tolerance": matched,
                                 "enabled": decoded["enabled"],
                                 "status_hex": decoded["status_hex"]})
            if self.previous_stamp is not None and stamp - self.previous_stamp > self.max_age:
                self.stable_first, self.stable_count = None, 0
            self.previous_stamp = stamp
            after = (self.receipt["reference_end_monotonic_s"] if self.speed_m_s is not None
                     else self.receipt["first_target_monotonic_s"])
            if (matched and self.reference_complete and stamp >= self.begin
                    and (after is None or stamp > after)):
                if (self.stable_first is None or
                        max(self.stable_high, actual) - min(self.stable_low, actual) > self.tolerance):
                    self.stable_low = self.stable_high = actual
                    self.stable_first, self.stable_count = stamp, 0
                self.stable_last = stamp
                self.stable_low, self.stable_high = min(self.stable_low, actual), max(self.stable_high, actual)
                self.stable_count += 1
            else:
                self.stable_first, self.stable_count = None, 0
        if not matched:
            self.stable_first, self.stable_count = None, 0
        self.confirmed = (matched and self.reference_complete and self.stable_count >= 2 and
                          self.stable_last - self.stable_first >= self.stable_s)
        # A long joint path may outlast this budget AFTER the gripper settles.
        # Continue checking status/aperture; do not reset or extend its deadline.
        if not self.confirmed and now >= self.deadline:
            raise HardwareBlocked("Ready gripper feedback arrived after preparation timeout")
        needs_send = not matched or not self.reference_complete
        if needs_send and now >= self.next_send:
            self.check_cancel()
            reference = self.target
            progressed_s = 0.
            if self.speed_m_s is not None:
                reference = self.reference_position
                if self.last_send is not None:
                    # Cap reference advancement at one nominal tick. A blocked
                    # writer never skips straight to a later path point or sends
                    # a burst to catch up. The original fixed deadline still runs.
                    progressed_s = min(max(0., now - self.last_send), self.send_interval_s)
                    # SDK targets are signed integer micrometres. Floor the
                    # available increment so wire quantisation cannot round a
                    # 166.6um budget up to 167um. Tiny epsilon covers only host
                    # float-clock representation, not a physical speed margin.
                    step_um = math.floor(self.speed_m_s * progressed_s * 1e6 + 1e-7)
                    remaining_um = abs(round(self.target * 1e6) - round(reference * 1e6))
                    change_um = min(remaining_um, step_um)
                    change = change_um / 1e6
                    reference = (round(reference * 1e6) +
                                 int(math.copysign(change_um, self.target - reference))) / 1e6
                    if change_um == remaining_um:
                        reference = self.target
                    progressed_s = change / self.speed_m_s
            self.adapter.send_gripper_m(reference)
            sent_at = time.monotonic()
            self.receipt["targets_sent"] += 1
            if self.receipt["first_target_monotonic_s"] is None:
                self.receipt["first_target_monotonic_s"] = sent_at
            if self.speed_m_s is not None:
                self.reference_position = reference
                self.reference_elapsed_s += progressed_s
                self.reference_complete = reference == self.target
                if self.reference_complete and self.receipt["reference_end_monotonic_s"] is None:
                    self.receipt["reference_end_monotonic_s"] = sent_at
                self.receipt.update(reference_elapsed_s=self.reference_elapsed_s,
                                    last_reference_m=reference,
                                    last_reference_monotonic_s=sent_at,
                                    reference_complete=self.reference_complete)
            self.last_send = sent_at
            if self.speed_m_s is None:
                self.next_send = sent_at + self.send_interval_s
            else:
                # Stay on the same nominal grid as the joint writer. Anchoring
                # each due time to SDK return would miss the very next joint
                # tick even for a 0.5ms call cost and halve the reference speed.
                # Pick only the next future slot; missed slots are discarded.
                next_slot = math.floor((sent_at - self.begin) / self.send_interval_s) + 1
                self.next_send = self.begin + next_slot * self.send_interval_s
                if self.next_send <= sent_at:
                    self.next_send = self.begin + (next_slot + 1) * self.send_interval_s
        if self.confirmed:
            self.check_cancel()
            self.receipt.update(status="READY_GRIPPER_CONFIRMED_FROM_REAL_FEEDBACK",
                                final_error_m=abs(actual - self.target),
                                stable_feedback_span_s=self.stable_last - self.stable_first,
                                stable_measured_span_m=self.stable_high - self.stable_low,
                                stable_distinct_feedback_count=self.stable_count,
                                already_ready_without_target=self.receipt["targets_sent"] == 0)
        else:
            self.receipt["status"] = "PREPARING"
        return self.confirmed

    def finish(self, error=None):
        if error is not None:
            self.receipt.update(status="FAILED_NO_AUTOMATIC_RECOVERY", failure=repr(error))
            if hasattr(error, "gripper_feedback"):
                self.receipt["gripper_feedback"] = error.gripper_feedback
                self.receipt["final_actual_m"] = error.gripper_feedback["aperture_m"]
                if self.receipt["initial_actual_m"] is None:
                    self.receipt["initial_actual_m"] = error.gripper_feedback["aperture_m"]
            if isinstance(error, HardwareBlocked):
                error.preparation_receipt = self.receipt
        self.receipt.update(samples=list(self.samples),
                            samples_truncated=self.receipt["samples_total"] > MAX_SAMPLES,
                            elapsed_s=0. if self.begin is None else time.monotonic() - self.begin)
        return self.receipt


def prepare_ready_gripper(cfg, adapter, *, cancel=lambda: False):
    """Confirm healthy, enabled, stable actual feedback from one owned writer.

    An explicit ready_gripper_speed_m_s advances a bounded linear reference at
    pose_send_hz (default 50 Hz). Legacy configurations retain at-most-10-Hz
    direct targets. Neither reference limit proves the physical closing speed.

    Disabled feedback never counts as ready. A healthy disabled gripper can be
    requested enabled through the existing GripperCtrl code 1, followed by new
    distinct enabled feedback. Faults are reported before any new target; this
    function never clears errors, changes zero, or changes the joint path.
    """
    tracker = _ReadyGripperTracker(cfg, adapter, cancel)
    try:
        while not tracker.tick():
            time.sleep(max(0., min(POLL_S, tracker.send_interval_s,
                                  tracker.deadline - time.monotonic())))
        return tracker.finish()
    except BaseException as exc:
        tracker.finish(exc)
        raise


def prepare_ready_pose(cfg, adapter, *, path_reviewed=False, speed_deg_s=10.,
                       accel_deg_s2=50., transmit_hz=100., endpoint_tolerance_deg=2.,
                       settle_timeout_s=3., cancel=lambda: False, target_deg=None):
    """Move six joints to a selected pose while closing the empty gripper.

    One writer loop interleaves SDK joint and gripper packets. It is not atomic
    hardware synchronization. Both measured endpoints must be confirmed; faults
    and cancellation never trigger reset, zero calibration, or automatic return.
    """
    tracker = _ReadyGripperTracker(cfg, adapter, cancel)
    receipt = {"status": "PREPARING_READY_POSE_AND_GRIPPER", "pose": None,
               "ready_gripper": tracker.receipt, "concurrent_single_writer": True,
               "hardware_atomic_7d": False, "automatic_reset_disable_return": False}
    try:
        if not path_reviewed:
            raise HardwareBlocked("Trajectory sweep review required before ready pose and gripper")
        tracker.check_cancel()
        # Receive-only fault check before execute_pose can request a mode or target.
        _measurement(adapter.feedback(require_can=False), tracker.max_age, require_can=False)
        tracker.check_cancel()
        receipt["pose"] = adapter.execute_pose(
            cfg["ready_deg"] if target_deg is None else target_deg,
            path_reviewed=path_reviewed, speed_deg_s=speed_deg_s,
            accel_deg_s2=accel_deg_s2, transmit_hz=transmit_hz, cancel=cancel,
            endpoint_tolerance_deg=endpoint_tolerance_deg,
            settle_timeout_s=settle_timeout_s, companion_tick=tracker.tick)
        if not tracker.confirmed:
            raise HardwareBlocked("Ready pose returned without confirmed gripper feedback")
        receipt.update(status="READY_POSE_AND_GRIPPER_CONFIRMED_FROM_REAL_FEEDBACK",
                       ready_gripper=tracker.finish())
        return receipt
    except BaseException as exc:
        receipt.update(status="FAILED_NO_AUTOMATIC_RECOVERY", failure=repr(exc),
                       ready_gripper=tracker.finish(exc))
        if isinstance(exc, HardwareBlocked):
            exc.ready_pose_receipt = receipt
        raise
