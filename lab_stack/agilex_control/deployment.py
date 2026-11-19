# PC PowerShell：在正式项目根目录运行；以下仅远端计划，需 SSH。
# .\2026-10-06-15-00-AgileX.cmd deploy --duration 30
# 真正执行必须经统一入口；条件、记录与失败分支见 README.md。
# 模块导入不开始运动；离线测试不是三阶段实机验收。
"""Right-arm three-stage execution with async RTC and a separate CAN clock.

This is newly written integration, NOT a physically accepted deployment. No
hardware is touched by import. The caller owns writer locks, record lifecycle,
actual bounds, leader isolation and explicit path review.
"""
from collections import deque
from contextlib import nullcontext
import math
import copy
import json
import queue
import threading
import time
import sys
from fractions import Fraction
import urllib.error
from .hardware import HardwareBlocked, validate_target_deg, require_gripper_feedback
from .hub import rpc
from .policy import ContinuousPlan, PolicyClient, ReferenceNode, finite_vector, copy_source_timestamps
from .preparation import enforce_ready_gripper, prepare_ready_gripper
from .storage import now as evidence_now


class TaskEventPublisher:
    """Ordered observational events; never block the CAN sender on hub HTTP."""
    def __init__(self, cfg, session_id):
        self.cfg, self.session_id = cfg, session_id
        self.enabled = cfg.get("task_state", {}).get("enabled") is True
        self.queue = queue.Queue(maxsize=32)
        self.stop = threading.Event()
        self.errors = deque(maxlen=8)
        self.thread = None
        self.sequence = 0
        self.closed = False
        self.sequence_lock = threading.Lock()
        if self.enabled:
            self.thread = threading.Thread(target=self._run, name="task-state-events", daemon=True)
            self.thread.start()

    def publish(self, event, **receipt):
        if self.enabled and not self.closed:
            try:
                self.queue.put_nowait({"session_id": self.session_id, "sequence": self.reserve_sequence(),
                    "event": event, "receipt": receipt})
            except queue.Full:
                self.errors.append("Task event queue full; stage delivery incomplete")

    def reserve_sequence(self):
        with self.sequence_lock:
            self.sequence += 1
            return self.sequence

    def _run(self):
        next_heartbeat = time.monotonic() + 2
        while not self.stop.is_set() or not self.queue.empty():
            if not self.stop.is_set() and time.monotonic() >= next_heartbeat:
                next_heartbeat = time.monotonic() + 2
                try:
                    rpc(self.cfg, "/task/heartbeat", {"session_id": self.session_id}, timeout=.5)
                except Exception as exc:
                    self.errors.append(repr(exc))
            try:
                event = self.queue.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                rpc(self.cfg, "/task/event", event, timeout=.5)
            except Exception as exc:
                self.errors.append(repr(exc))

    def status(self):
        return {"enabled": self.enabled, "errors": list(self.errors),
                "pending": self.queue.qsize(), "worker_stopped": self.thread is None or not self.thread.is_alive()}

    def close(self):
        self.closed = True
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        return self.status()


def cancelled(cfg, stop_event):
    from .storage import stop_requested
    return stop_event.is_set() or stop_requested(cfg)


def wait_stationary(adapter, cancel, timeout=5, *, require_gripper=False):
    """Observe healthy, enabled feedback; execute_pose owns the later CAN handoff."""
    history = deque()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel():
            raise HardwareBlocked("Stopped before return; no automatic recovery motion")
        state = adapter.feedback(require_can=False)
        validate_target_deg(state["q_deg"], adapter.cfg["command_limits_deg"])
        if require_gripper:
            require_gripper_feedback(state,
                max_age_s=adapter.cfg.get("max_feedback_age_s", .2),
                require_enabled=True, now_monotonic=time.monotonic())
        t = time.monotonic()
        history.append((t, state["q_deg"]))
        while history and t - history[0][0] > .55:
            history.popleft()
        if len(history) > 1 and t - history[0][0] >= .5:
            if max(max(q[j] for _, q in history) - min(q[j] for _, q in history) for j in range(6)) <= .5:
                return state
        time.sleep(.02)
    raise HardwareBlocked("Actual motion did not settle before automatic normal return")


def _bootstrap_reference(relative_plan, observation_time, first_start):
    """Assign the first accepted H15 queue to the actual execution clock.

    There is no preceding policy execution to align on startup. Loading, the
    first RPC and synchronous startup evidence writes are excluded from the
    active period. Only this first queue is retimed; sensor/source timestamps
    remain unchanged, and all subsequent RTC queues use their real observation
    times. The pre-start segment is an explicit stationary COMMAND reference,
    not fabricated feedback or a repeated expired policy action.
    """
    if (not math.isfinite(observation_time) or not math.isfinite(first_start)
            or observation_time >= first_start or relative_plan.times[0] != 0.):
        raise HardwareBlocked("Invalid initial reference clock; no policy targets sent")
    initial = relative_plan.nodes[0]
    nodes = [ReferenceNode(observation_time, initial.q, (0.,) * 6)]
    nodes.extend(ReferenceNode(first_start + node.t, node.q, node.v)
                 for node in relative_plan.nodes)
    return ContinuousPlan(nodes, relative_plan.execution_hz, relative_plan.send_hz)


def _startup_pose_check(cfg, observation, state, prior_state=None):
    """Check actual startup pose only, using the existing endpoint tolerance.

    This is not a per-target tracking-error stop. It prevents retiming a first
    prediction after the ready arm or its observation anchor has drifted.
    """
    observed = finite_vector(observation.get("state_right_rad_m"))
    actual = finite_vector(list(state["q_deg"]) + [state["grip_m"]])
    observed_deg = [math.degrees(value) for value in observed[:6]]
    tolerance = float(cfg["endpoint_tolerance_deg"])
    ready_error = max(abs(a - b) for a, b in zip(actual[:6], cfg["ready_deg"]))
    observation_drift = max(abs(a - b) for a, b in zip(actual[:6], observed_deg))
    prior_drift = (max(abs(a - b) for a, b in zip(actual[:6], prior_state["q_deg"]))
                   if prior_state is not None else 0.)
    diagnostic = {"ready_distance_deg": ready_error,
        "observation_to_actual_drift_deg": observation_drift,
        "startup_feedback_drift_deg": prior_drift, "existing_endpoint_tolerance_deg": tolerance,
        "actual_anchor_deg_and_grip_m": actual,
        "observation_to_actual_gripper_delta_m": actual[6] - observed[6]}
    if "ready_gripper_m" in cfg:
        # Explicit seven-dimensional preparation is checked only at startup.
        # This does not introduce any per-target policy tracking-error stop.
        grip_check = enforce_ready_gripper(cfg, state)
        grip_drift = abs(actual[6] - observed[6])
        prior_grip = (finite_vector([prior_state.get("grip_m")], length=1)[0]
                      if prior_state is not None else actual[6])
        prior_grip_drift = abs(actual[6] - prior_grip)
        diagnostic.update(ready_gripper=grip_check,
                          observation_to_actual_gripper_drift_m=grip_drift,
                          startup_gripper_feedback_drift_m=prior_grip_drift,
                          ready_gripper_tolerance_m=grip_check["tolerance_m"])
        if max(grip_drift, prior_grip_drift) > grip_check["tolerance_m"]:
            raise HardwareBlocked("Actual/observed ready gripper drifted during startup; no policy targets sent: "
                                  + repr(diagnostic))
    if max(ready_error, observation_drift, prior_drift) > tolerance:
        raise HardwareBlocked("Actual ready pose drifted during startup; no policy targets sent: " + repr(diagnostic))
    return diagnostic


def _record_extreme(metrics, key, value, *, maximum=False):
    """Update bounded timing summaries in memory; never write a per-block log."""
    previous = metrics.get(key)
    metrics[key] = value if previous is None else (max if maximum else min)(previous, value)


def _image_age_diagnostic(observation, at_monotonic):
    """Source-image age, distinct from the RTC packet-ready clock.

    The hub converts its camera perf-counter stamps onto this host's monotonic
    clock. Older hubs do not provide them: report unavailable, never substitute
    observation_monotonic and pretend the pixels were captured then. Diagnostic
    only; this does not change existing deadlines or add a new stop threshold.
    """
    stamps = observation.get("source_frame_monotonic", {})
    ages = {}
    for role in ("front", "right"):
        stamp = stamps.get(role) if isinstance(stamps, dict) else None
        if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                or not math.isfinite(stamp) or not math.isfinite(at_monotonic)
                or stamp > at_monotonic):
            ages[role] = None
        else:
            ages[role] = at_monotonic - stamp
    available = all(age is not None for age in ages.values())
    return {"available": available, "source_age_s": ages,
            "oldest_source_age_s": max(ages.values()) if available else None,
            "basis": "hub camera source_frame_monotonic; not RTC packet-ready time"}


def _policy_observation_evidence(observation):
    """Bind a request id before inference and copy only bounded input metadata."""
    from uuid import uuid4
    observation.setdefault("request_id", uuid4().hex)
    return {"request_id": observation["request_id"],
        "state_right_rad_m": list(observation["state_right_rad_m"]),
        "source_timestamps": copy.deepcopy(observation.get("source_timestamps") or {}),
        "source_frame_monotonic": dict(observation.get("source_frame_monotonic") or {}),
        "source_frame_age_s": dict(observation.get("source_frame_age_s") or {}),
        "observation_monotonic": observation["observation_monotonic"],
        "observation_monotonic_basis": "legacy packet-ready scheduling time; not source acquisition",
        "observation_acquired_monotonic": observation.get("observation_acquired_monotonic"),
        "observation_packet_wall_ns": observation.get("wall_ns")}


def _policy_future_nodes(plan, cut):
    """Accepted reference evidence only; never reinterpret nodes as feedback."""
    from bisect import bisect_left
    future = plan.nodes[bisect_left(plan.times, cut - 1e-9):]
    return {"basis": "accepted software reference; not actual motion", "cut_monotonic": cut,
        "total_nodes": len(future), "omitted_nodes": max(0, len(future) - 60),
        "nodes": [{"monotonic_s": node.t, "q_rad_m": list(node.q), "joint_v_rad_s": list(node.v)}
                  for node in future[:60]]}


def _policy_buffer_append(buffer, counts, item):
    counts["appended"] += 1
    if len(buffer) == buffer.maxlen:
        counts["discarded_oldest"] += 1
    buffer.append(item)


def _policy_alignment_sample(now, read_at, target, actual, source_request_id):
    raw = actual.get("raw") or {}
    return {"reference_monotonic": now, "feedback_read_monotonic": read_at,
        "plan_source_request_id": source_request_id,
        "reference_q_deg": [math.degrees(value) for value in target[:6]], "reference_grip_m": target[6],
        "actual_q_deg": list(actual["q_deg"]), "actual_grip_m": actual["grip_m"],
        "feedback_ages_s": {key: actual.get(key) for key in
            ("age_joint_s", "age_status_s", "age_gripper_s", "age_motor_s")},
        "feedback_raw_monotonic_s": {key: value.get("monotonic_s") for key, value in raw.items()
            if isinstance(value, dict) and "monotonic_s" in value},
        "feedback_timestamp_basis": actual.get("freshness_basis"),
        "missing_feedback_timestamps_are_unavailable": True}


def observation_schedule_time(cfg, observation):
    """Select an explicit reference clock without relabeling source/ready time."""
    basis = cfg.get("rtc_observation_clock", "packet_ready")
    if basis == "packet_ready":
        return observation["observation_monotonic"]
    if basis != "selection_anchor":
        raise HardwareBlocked("Unknown RTC observation clock")
    timing = copy_source_timestamps(observation.get("source_timestamps")).get("timing_v1")
    if not isinstance(timing, dict):
        raise HardwareBlocked("Selection-anchor RTC requires source timing metadata")
    if timing.get("selection_mode") != "query_grid" or timing.get("alignment_applied") is not True:
        raise HardwareBlocked("Selection-anchor RTC requires a validated aligned observation")
    return timing["selection_anchor_monotonic_ns"] / 1e9


def alignment_wait_ms(cfg):
    value = cfg.get('alignment_wait_ms', 0)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 20):
        raise HardwareBlocked('alignment_wait_ms must be finite from 0 to 20 ms')
    if value and (cfg.get('observation_alignment_mode', 'legacy') != 'query_grid'
                  or cfg.get('rtc_observation_clock', 'packet_ready') != 'selection_anchor'):
        raise HardwareBlocked('Bounded requery requires query_grid and explicit selection_anchor RTC')
    return value


def bounded_query_deadline(cfg, request_started, old_plan=None, run_deadline=None):
    """Pure upper bound; never extend an actually accepted reference."""
    wait = alignment_wait_ms(cfg)
    if not math.isfinite(request_started):
        raise HardwareBlocked('Finite query request start required')
    deadline_ns = round(request_started * 1e9) + round(wait * 1e6)
    if old_plan is not None:
        last_anchor = old_plan.end - cfg['rtc_delay_steps'] / cfg['action_time_hz']
        deadline_ns = min(deadline_ns, math.floor(last_anchor * 1e9) - 1)
    if run_deadline is not None:
        deadline_ns = min(deadline_ns, math.floor(run_deadline * 1e9) - 1)
    if deadline_ns <= round(request_started * 1e9):
        failure = HardwareBlocked('Accepted reference coverage leaves no bounded-query budget')
        failure.alignment_query_response = {'request_start_monotonic': request_started,
            'deadline_monotonic_ns': deadline_ns, 'requested_wait_ms': wait,
            'accepted_reference_end_monotonic': None if old_plan is None else old_plan.end,
            'http_request_made': False}
        raise failure
    return deadline_ns


def policy_observation_route(cfg, *, deadline_monotonic_ns=None):
    mode = cfg.get("observation_alignment_mode", "legacy")
    if mode not in ("legacy", "query_grid"):
        raise HardwareBlocked("Unknown policy observation alignment mode")
    route = "/observation?alignment=query_grid" if mode == "query_grid" else "/observation"
    wait = alignment_wait_ms(cfg)
    if wait:
        if deadline_monotonic_ns is None:
            deadline_monotonic_ns = bounded_query_deadline(cfg, time.monotonic())
        route += '&wait_ms=%g&deadline_monotonic_ns=%d' % (wait, deadline_monotonic_ns)
    return route


def request_policy_observation(cfg, request_started, old_plan=None, run_deadline=None):
    if not alignment_wait_ms(cfg):
        return rpc(cfg, policy_observation_route(cfg))
    bound = bounded_query_deadline(cfg, request_started, old_plan, run_deadline)
    route = policy_observation_route(cfg, deadline_monotonic_ns=bound)
    timeout = min(2., max(.001, old_plan.end - request_started)) if old_plan is not None else 2.
    try:
        observation = rpc(cfg, route, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read(262145)
        failure = HardwareBlocked('Aligned observation HTTP failure %d; no latest fallback' % exc.code)
        try:
            parsed = json.loads(body) if len(body) <= 262144 else None
        except (ValueError, TypeError):
            parsed = None
        failure.alignment_query_evidence = (parsed.get('alignment_query_evidence') if isinstance(parsed, dict) else None)
        failure.alignment_query_response = {'http_status': exc.code,
            'request_start_monotonic': request_started, 'deadline_monotonic_ns': bound,
            'response': parsed if parsed is not None else body[:8192].decode('utf-8', errors='replace'),
            'body_truncated': len(body) > 262144}
        raise failure from exc
    timing = observation.get('source_timestamps', {}).get('timing_v1', {})
    selected_ns = timing.get('selection_anchor_monotonic_ns')
    evidence = observation.get('alignment_query_evidence')
    if (type(selected_ns) is not int or not round(request_started * 1e9) <= selected_ns <= bound
            or not isinstance(evidence, dict) or not isinstance(evidence.get('attempts'), list)
            or not evidence['attempts']):
        failure = HardwareBlocked('Hub did not return a bounded-query receipt within the caller deadline')
        failure.alignment_query_evidence = evidence
        raise failure
    observation['alignment_client_query'] = {'request_start_monotonic': request_started,
        'deadline_monotonic_ns': bound, 'request_end_monotonic': time.monotonic(),
        'accepted_reference_end_monotonic': None if old_plan is None else old_plan.end}
    return observation


def advance_policy_tick(origin, current_index, finished, hz):
    """Keep the original sender epoch; skip every missed slot without bursts.

    Nanosecond rounding avoids floating subtraction treating an exact deadline
    as one late slot. This does not extend an accepted policy queue's lifetime.
    """
    if (not all(math.isfinite(v) for v in (origin, finished, hz)) or hz <= 0
            or type(current_index) is not int or current_index < 0 or finished < origin):
        raise ValueError("Invalid policy sender clock")
    rate = Fraction(str(hz))
    elapsed_ns = round(finished * 1e9) - round(origin * 1e9)
    numerator, denominator = elapsed_ns * rate.numerator, 1_000_000_000 * rate.denominator
    earliest = (numerator + denominator - 1) // denominator
    index = max(current_index + 1, earliest)
    return index, origin + index / hz, index - current_index - 1


def run_policy(cfg, adapter, session, stop_event, duration):
    """30°/s and50°/s² target-reference caps; never claim measured dynamics.

    First prediction is made at verified ready pose, before duration starts;
    its full queue begins on the execution clock. Durable startup preparation
    evidence is written BEFORE the first observation; prediction evidence is
    buffered until the existing final save, outside the sender loop.
    Async worker renews subsequent observation-aligned queues. If a reply cannot be merged
    before its commitment cut, targets stop with an explicit failure. It does
    not silently extend the queue, repeat the last action, or stop each chunk.
    """
    duration = float(duration)
    if not math.isfinite(duration) or not 0 < duration <= 300:
        raise ValueError("Deployment duration must be finite, >0 and<=300s")
    dry_run = getattr(adapter, "dry_run", False) is True
    if dry_run and getattr(adapter, "CAN_send_calls", None) != 0:
        raise HardwareBlocked("NoCanTargetSink must explicitly report zero CAN sends")
    bounds = [[math.radians(x) for x in pair] for pair in adapter.cfg["command_limits_deg"]]
    client = PolicyClient(url=f"http://127.0.0.1:{cfg['model_port']}", execution_hz=cfg["action_time_hz"])
    tx_writer = None
    try:
        if cancelled(cfg, stop_event):
            raise HardwareBlocked("Stopped before initial policy observation")
        if not dry_run and cfg.get("tx_evidence_enabled", False):
            from .tx_evidence import TxEvidenceWriter
            tx_writer = TxEvidenceWriter(cfg, session.path / "host-can-tx-evidence-v1.jsonl",
                                         capacity=cfg.get("tx_evidence_capacity", 4096))
            session.report["tx_evidence_start"] = tx_writer.start()
            adapter.attach_tx_evidence(tx_writer)
        # First RPC excludes compilation/startup from the timed active period.
        startup_started = time.monotonic()
        startup_io_started = time.monotonic()
        session.event("POLICY_CLOCK_PREPARATION_NO_TARGETS", duration_s=duration,
                      control_send_hz=cfg["policy_send_hz"], action_time_hz=cfg["action_time_hz"],
                      startup_RPC_and_evidence_IO_excluded=True,
                      evidence_write_precedes_first_observation=True)
        startup_io_s = time.monotonic() - startup_io_started
        first_hub_started = time.monotonic()
        try:
            observation = request_policy_observation(cfg, first_hub_started)
        except Exception as exc:
            session.report['policy_first_observation_failure'] = {
                'request_start_monotonic': first_hub_started,
                'alignment_query_evidence': getattr(exc, 'alignment_query_evidence', None),
                'alignment_query_response': getattr(exc, 'alignment_query_response', None), 'error': repr(exc)}
            raise
        first_observation_time = observation_schedule_time(cfg, observation)
        capture_domain = copy.deepcopy(observation.get("source_timestamps", {}).get("timing_v1", {}).get("clock_domain"))
        first_hub_s = time.monotonic() - first_hub_started
        first_attempt = _policy_observation_evidence(observation)
        first_attempt.update(request_start_monotonic=first_hub_started,
            alignment_query_evidence=copy.deepcopy(observation.get('alignment_query_evidence')),
            alignment_client_query=copy.deepcopy(observation.get('alignment_client_query')))
        first_attempt.update(reference_anchor_monotonic=first_observation_time,
                             reference_clock_basis=cfg.get("rtc_observation_clock", "packet_ready"))
        first_attempt.update(rtc_prefix=None, infer_started_monotonic=time.monotonic(),
                             status="FIRST_REQUEST_STARTED")
        session.report["policy_first_attempt"] = first_attempt
        if cancelled(cfg, stop_event):
            first_attempt.update(status="FIRST_OBSERVATION_NOT_INFERRED", infer_started_monotonic=None)
            raise HardwareBlocked("Stopped after initial observation; no inference or targets")
        first, first_rpc = client.infer(observation)
        first_attempt.update(model_actions=first["actions"], received_monotonic=time.monotonic(),
                             rpc_s=first_rpc, status="FIRST_PREDICTION_RECEIVED")
        if cancelled(cfg, stop_event):
            first_attempt.update(status="FIRST_PREDICTION_CANCELLED_NO_TARGETS")
            raise HardwareBlocked("Stopped during initial inference; no targets")
        state = adapter.feedback(require_can=True)
        initial_pose_check = _startup_pose_check(cfg, observation, state)
        common = dict(execution_hz=cfg["action_time_hz"], send_hz=cfg["policy_send_hz"],
            speed_deg_s=cfg["policy_speed_deg_s"], accel_deg_s2=cfg["policy_accel_deg_s2"],
            gripper_bounds_m=cfg["gripper_bounds_m"])
        # Do not call Session.event here: it synchronously verifies/writes USB.
        # Preserve occurrence time in the report's existing RAM event list;
        # normal/fault teardown persists it together with the timing evidence.
        session.report.setdefault("events", []).append({"at": evidence_now(),
            "event": "FIRST_PREDICTION_READY_NO_POLICY_TARGET_YET", "first_rpc_s": first_rpc,
            "request_id": first_attempt["request_id"], "input_evidence": first_attempt,
            "first_actions": first["actions"], "initial_actual_pose_check": initial_pose_check,
            "actual_observation_monotonic": observation["observation_monotonic"],
            "image_age_at_prediction_ready": _image_age_diagnostic(observation, time.monotonic()),
            "first_queue_clock": "explicit execution-start bootstrap; subsequent queues observation-aligned",
            "persistence": "buffered until post-dispatch/fault session save"})
        # Re-read real feedback before building the initial reference. A moved
        # arm cannot reuse a stationary first-prediction anchor.
        final_state = adapter.feedback(require_can=True)
        final_pose_check = _startup_pose_check(cfg, observation, final_state, state)
        q = [math.radians(x) for x in final_state["q_deg"]] + [final_state["grip_m"]]
        # Shape once on a relative clock; do not consume this first H15 horizon
        # while the initial model RPC and USB evidence writes are in progress.
        build_started = time.monotonic()
        try:
            relative_plan, peak = ContinuousPlan.build(first["actions"], 0., 0., q, bounds, **common)
        except Exception as exc:
            first_attempt.update(status="FIRST_REFERENCE_FAILED", error=repr(exc),
                                 bounds_failure=getattr(exc, "bounds_failure", None))
            raise
        first_build_s = time.monotonic() - build_started
        session.report["policy_startup"] = {"hub_s": first_hub_s, "rpc_s": first_rpc,
            "synchronous_evidence_io_s": startup_io_s, "reference_build_s": first_build_s,
            "evidence_io_position": "before_first_observation",
            "prediction_evidence_buffered": True,
            "total_before_clock_arm_s": time.monotonic() - startup_started,
            "initial_actual_pose_check": initial_pose_check, "final_actual_pose_check": final_pose_check}
        lock = threading.Lock()
        shared = {"plan": None, "worker_error": None, "worker_error_at": None,
                  "plan_sources": deque(maxlen=3001),
                  "source_plans": {}}
        worker_stop = threading.Event()
        events = deque(maxlen=200)
        rpc_evidence, alignment = deque(maxlen=3001), deque(maxlen=3001)
        rpc_evidence_counts = {"appended": 0, "discarded_oldest": 0}
        alignment_counts = {"appended": 0, "discarded_oldest": 0, "skipped_periods": 0,
                            "dispatch_ticks_not_sampled": 0}
        metrics = {"rpc_count": 1, "rpc_min_s": first_rpc, "rpc_max_s": first_rpc,
            "max_reference_shaping_deg": peak, "targets_sent": 0, "reference_dispatch_count": 0,
            "missed_CAN_slots": 0, "missed_reference_slots": 0,
            "execution_mode": "MODEL_RTC_NO_CAN_REFERENCE_VALIDATION" if dry_run else "PHYSICAL_POLICY_DISPATCH",
            "physical_task_success": "NOT_ASSESSED" if dry_run else "OPERATOR_REVIEW_REQUIRED",
            "rtc_rpc_completed_count": 0, "rtc_committed_count": 0,
            "observation_interval_max_s": None, "source_to_install_max_s": None,
            "hub_max_s": None, "reference_build_max_s": None,
            "image_age_at_inference_max_s": None, "image_age_at_install_max_s": None,
            "valid_prefix_min_steps": None, "effective_attention_min_steps": None,
            "queue_coverage_margin_min_s": None, "commit_budget_margin_min_s": None,
            "min_budget_margin_s": None,
            "budget_bounds": {"adjacent_observation_interval_max_s":
                (len(first["actions"]) - cfg["rtc_delay_steps"]) / cfg["action_time_hz"],
                "source_to_install_strict_max_s": cfg["rtc_delay_steps"] / cfg["action_time_hz"]
                    - 1 / cfg["policy_send_hz"], "submission_guard_s": 1 / cfg["policy_send_hz"]}}

        def worker():
            previous_observation = None
            attempt = None
            attempt_installed = False
            try:
                # A pre-start observation would give a renewal ending before
                # the bootstrap queue, unnecessarily shortening its horizon.
                if worker_stop.wait(max(0., first_start - time.monotonic())):
                    return
                while not worker_stop.is_set() and time.monotonic() < deadline:
                    if cancelled(cfg, stop_event):
                        break
                    hub_started = time.monotonic()
                    attempt = {"hub_started_monotonic": hub_started, "status": "OBSERVATION_REQUESTED"}
                    attempt_installed = False
                    with lock:
                        query_plan = shared['plan']
                    obs = request_policy_observation(cfg, hub_started, query_plan, deadline)
                    # A Hub reply can outlive this run. Keep its provenance, but
                    # never begin another expensive inference after cancellation.
                    if worker_stop.is_set() or cancelled(cfg, stop_event) or time.monotonic() >= deadline:
                        attempt.update(_policy_observation_evidence(obs))
                        attempt.update(status="OBSERVATION_NOT_INFERRED",
                            reason="stop requested or policy deadline reached",
                            received_monotonic=time.monotonic(),
                            alignment_query_evidence=copy.deepcopy(obs.get('alignment_query_evidence')))
                        _policy_buffer_append(rpc_evidence, rpc_evidence_counts, attempt)
                        break
                    if cfg.get("observation_alignment_mode", "legacy") == "query_grid":
                        incoming_domain = obs.get("source_timestamps", {}).get("timing_v1", {}).get("clock_domain")
                        if capture_domain is None or incoming_domain != capture_domain:
                            raise HardwareBlocked("Capture host/boot/session changed during RTC; no cross-session continuation")
                    hub_s = time.monotonic() - hub_started
                    with lock:
                        old = shared["plan"]
                    observation_time = observation_schedule_time(cfg, obs)
                    interval = (observation_time - previous_observation
                                if previous_observation is not None else None)
                    if interval is not None and interval <= 0:
                        raise HardwareBlocked("RTC observation clock did not advance; no expired observation replay")
                    previous_observation = observation_time
                    expected_cut = observation_time + cfg["rtc_delay_steps"] / cfg["action_time_hz"]
                    coverage_margin = old.end - expected_cut
                    attempt.update(_policy_observation_evidence(obs))
                    attempt.update(request_start_monotonic=hub_started,
                        alignment_query_evidence=copy.deepcopy(obs.get('alignment_query_evidence')),
                        alignment_client_query=copy.deepcopy(obs.get('alignment_client_query')))
                    attempt.update(reference_anchor_monotonic=observation_time,
                                   reference_clock_basis=cfg.get("rtc_observation_clock", "packet_ready"))
                    attempt.update(hub_s=hub_s, observation_interval_s=interval,
                                   queue_coverage_margin_s=coverage_margin, status="OBSERVATION_RECEIVED")
                    with lock:
                        _record_extreme(metrics, "hub_max_s", hub_s, maximum=True)
                        if interval is not None:
                            _record_extreme(metrics, "observation_interval_max_s", interval, maximum=True)
                        _record_extreme(metrics, "queue_coverage_margin_min_s", coverage_margin)
                        _record_extreme(metrics, "min_budget_margin_s", coverage_margin)
                    prefix = old.rtc_prefix(observation_time, cfg["rtc_delay_steps"], cfg["rtc_attention_extra"])
                    cut = prefix["committed_until"]
                    valid = prefix["previous_valid_steps"]
                    attention = prefix["execution_horizon_steps"] - prefix["delay_steps"]
                    attempt.update(valid_prefix=valid, effective_attention_steps=attention,
                        committed_until=cut, rtc_prefix={**prefix,
                            "previous_actions": [list(row) for row in prefix["previous_actions"]]})
                    with lock:
                        _record_extreme(metrics, "valid_prefix_min_steps", valid)
                        _record_extreme(metrics, "effective_attention_min_steps", attention)
                    infer_started = time.monotonic()
                    attempt.update(infer_started_monotonic=infer_started, status="RPC_STARTED")
                    image_age_at_infer = _image_age_diagnostic(obs, infer_started)
                    if worker_stop.is_set() or cancelled(cfg, stop_event) or time.monotonic() >= deadline:
                        attempt.update(status="OBSERVATION_NOT_INFERRED",
                            reason="stop requested or policy deadline reached", checked_monotonic=time.monotonic())
                        _policy_buffer_append(rpc_evidence, rpc_evidence_counts, attempt)
                        break
                    if time.monotonic() + 1 / cfg["policy_send_hz"] >= cut:
                        raise HardwareBlocked("Observation preparation missed committed RTC cut; inference not started")
                    result, latency = client.infer(obs, prefix)
                    received = time.monotonic()
                    reply_margin = cut - received - 1 / cfg["policy_send_hz"]
                    attempt.update(rpc_s=latency, reply_budget_margin_s=reply_margin,
                                   received_monotonic=received, model_actions=result["actions"], status="REPLY_RECEIVED")
                    with lock:
                        metrics["rtc_rpc_completed_count"] += 1
                        metrics["rpc_min_s"] = min(metrics["rpc_min_s"], latency)
                        metrics["rpc_max_s"] = max(metrics["rpc_max_s"], latency)
                    if worker_stop.is_set() or cancelled(cfg, stop_event) or received >= deadline:
                        attempt.update(status="REPLY_NOT_INSTALLED", reason="stop requested or policy deadline reached")
                        _policy_buffer_append(rpc_evidence, rpc_evidence_counts, attempt)
                        break
                    with lock:
                        _record_extreme(metrics, "min_budget_margin_s", reply_margin)
                    if received + 1 / cfg["policy_send_hz"] >= cut:
                        raise HardwareBlocked("Inference missed committed RTC cut; no expired action replay")
                    anchor, velocity = old.sample(cut)
                    attempt.update(reference_anchor_rad_m=list(anchor), reference_anchor_joint_v_rad_s=list(velocity))
                    build_started = time.monotonic()
                    new, shaping = ContinuousPlan.build(result["actions"], observation_time,
                        cut, anchor, bounds, v=velocity, old=old, **common)
                    built_at = time.monotonic()
                    attempt.update(reference_build_s=built_at - build_started,
                        generation_budget_margin_s=cut - built_at - 1 / cfg["policy_send_hz"])
                    if built_at + 1 / cfg["policy_send_hz"] >= cut:
                        raise HardwareBlocked("Reference generation missed RTC cut")
                    future_evidence = _policy_future_nodes(new, cut)
                    # Prepare observational evidence outside the sender's lock.
                    # Recheck at the actual commit boundary, not before that work.
                    diagnostic_at = time.monotonic()
                    image_age_at_install = _image_age_diagnostic(obs, diagnostic_at)
                    cancelled_before_install = cancelled(cfg, stop_event)
                    with lock:
                        installed_at = time.monotonic()
                        if (worker_stop.is_set() or stop_event.is_set() or cancelled_before_install
                                or installed_at >= deadline):
                            attempt.update(status="REPLY_NOT_INSTALLED",
                                reason="stop requested or policy deadline reached before installation",
                                checked_monotonic=installed_at)
                            _policy_buffer_append(rpc_evidence, rpc_evidence_counts, attempt)
                            break
                        if installed_at + 1 / cfg["policy_send_hz"] >= cut:
                            raise HardwareBlocked("Reference installation missed RTC cut")
                        build_s = built_at - build_started
                        source_to_install = installed_at - observation_time
                        request_to_install = installed_at - hub_started
                        _record_extreme(metrics, 'request_to_install_max_s', request_to_install, maximum=True)
                        query_retry = obs.get('alignment_query_evidence', {}).get('query_retry', {})
                        if query_retry:
                            _record_extreme(metrics, 'alignment_query_wait_max_s', query_retry['total_query_ns'] / 1e9, maximum=True)
                        attempt.update(request_to_install_s=request_to_install,
                            request_to_final_anchor_s=observation_time - hub_started,
                            final_anchor_to_install_s=source_to_install)
                        commit_margin = cut - installed_at - 1 / cfg["policy_send_hz"]
                        _record_extreme(metrics, "reference_build_max_s", build_s, maximum=True)
                        _record_extreme(metrics, "source_to_install_max_s", source_to_install, maximum=True)
                        # Advance previously evaluated ages to the commit clock;
                        # do not hide diagnostic/lock delay in installation metrics.
                        diagnostic_delay = installed_at - diagnostic_at
                        image_age_at_install["source_age_s"] = {
                            role: None if age is None else age + diagnostic_delay
                            for role, age in image_age_at_install["source_age_s"].items()}
                        if image_age_at_install["available"]:
                            image_age_at_install["oldest_source_age_s"] += diagnostic_delay
                        image_age_at_install["at_monotonic"] = installed_at
                        for key, diagnostic in (("image_age_at_inference_max_s", image_age_at_infer),
                                                ("image_age_at_install_max_s", image_age_at_install)):
                            if diagnostic["available"]:
                                _record_extreme(metrics, key, diagnostic["oldest_source_age_s"], maximum=True)
                        _record_extreme(metrics, "commit_budget_margin_min_s", commit_margin)
                        _record_extreme(metrics, "min_budget_margin_s", commit_margin)
                        shared["plan"] = new
                        attempt_installed = True
                        shared["plan_sources"].append((cut, attempt["request_id"]))
                        shared["source_plans"][attempt["request_id"]] = {
                            "revision": metrics["rtc_committed_count"] + 1, "origin": observation_time}
                        if len(shared["source_plans"]) > 3001:
                            del shared["source_plans"][next(iter(shared["source_plans"]))]
                        attempt.update(installed_monotonic=installed_at, status="REFERENCE_INSTALLED",
                                       accepted_future_reference=future_evidence)
                        _policy_buffer_append(rpc_evidence, rpc_evidence_counts, attempt)
                        metrics["rpc_count"] += 1
                        metrics["rtc_committed_count"] += 1
                        metrics["max_reference_shaping_deg"] = max(metrics["max_reference_shaping_deg"], shaping)
                        events.append({"event": "rtc_reply_committed", "at_monotonic": received,
                            "request_id": attempt["request_id"], "input_evidence": attempt,
                            "rpc_s": latency, "committed_until": cut, "valid_prefix": prefix["previous_valid_steps"],
                            "hub_s": hub_s, "source_age_at_inference_s": infer_started - observation_time,
                            "image_age_at_inference": image_age_at_infer,
                            "image_age_at_install": image_age_at_install,
                            "observation_interval_s": interval, "source_to_install_s": source_to_install,
                            "effective_attention_steps": attention, "queue_coverage_margin_s": coverage_margin,
                            "commit_budget_margin_s": commit_margin, "reference_build_s": build_s,
                            "install_margin_before_cut_s": cut - installed_at,
                            "peak_shaping_deg": shaping, "model_actions": result["actions"]})
            except Exception as exc:
                # Capture occurrence before waiting for the sender lock. A
                # pre-deadline failure remains fatal even if publication/join
                # completes later. A reply never installed after the deadline
                # is evidence, not an accepted block or a new motion failure.
                failure_captured_at = time.monotonic()
                late_unused = failure_captured_at >= deadline and not attempt_installed
                with lock:
                    if not late_unused:
                        shared["worker_error"] = repr(exc)
                        shared["worker_error_at"] = failure_captured_at
                    if attempt is not None:
                        attempt.update(status="LATE_UNUSED_FAILURE" if late_unused else "FAILED",
                                       error=repr(exc), failure_captured_monotonic=failure_captured_at,
                                       used_for_control=attempt_installed,
                                       bounds_failure=getattr(exc, "bounds_failure", None))
                        attempt.update(alignment_query_evidence=getattr(exc, 'alignment_query_evidence',
                                       attempt.get('alignment_query_evidence')),
                                       alignment_query_response=getattr(exc, 'alignment_query_response', None))
                        _policy_buffer_append(rpc_evidence, rpc_evidence_counts, attempt)
                    events.append({"event": "rtc_late_unused_failure" if late_unused else "rtc_worker_failed",
                                   "error": repr(exc), "attempt": attempt,
                                   "at_monotonic": failure_captured_at,
                                   "published_monotonic": time.monotonic()})

        thread = threading.Thread(target=worker, name="async-RTC-inference", daemon=True)
        if cancelled(cfg, stop_event):
            first_attempt.update(status="FIRST_REFERENCE_CANCELLED_NO_TARGETS")
            raise HardwareBlocked("Stopped before policy clock arming; no targets")
        if dry_run:
            print("开始真实模型RTC无CAN验证，参考仅进入软件sink，机械臂不动；首步推理和启动记录时间不计入运行时长。", flush=True)
        else:
            print("即将开始真实右臂模型目标发送；首步推理和启动记录时间不计入运行时长。", flush=True)
        first_start = time.monotonic() + 2 / cfg["policy_send_hz"]
        deadline = first_start + duration
        plan = _bootstrap_reference(relative_plan, first_observation_time, first_start)
        shared["plan"] = plan
        shared["plan_sources"].append((first_start, first_attempt["request_id"]))
        shared["source_plans"][first_attempt["request_id"]] = {"revision": 0, "origin": first_start}
        first_attempt.update(status="BOOTSTRAP_REFERENCE_INSTALLED", installed_monotonic=time.monotonic(),
            committed_until=first_start, accepted_future_reference=_policy_future_nodes(plan, first_start),
            bootstrap_retimed=True, action_clock_origin_monotonic=first_start)
        # Keep clock evidence in memory until the existing final session.save;
        # synchronous USB I/O must not sit between clock arming and dispatch.
        session.report["policy_clock"] = {"first_target_at_monotonic": first_start,
            "actual_first_observation_monotonic": observation["observation_monotonic"],
            "first_reference_origin_monotonic": first_start, "first_reference_end_monotonic": plan.end,
            "bootstrap_retimed": True, "subsequent_reference_clock": cfg.get("rtc_observation_clock", "packet_ready"),
            "first_selection_reference_anchor_monotonic": first_observation_time,
            "observation_alignment_mode": cfg.get("observation_alignment_mode", "legacy"),
            "image_age_at_first_scheduled_target": _image_age_diagnostic(observation, first_start)}
        thread.start()
        publisher = getattr(session, "_task_publisher", None)
        if publisher is not None:
            publisher.publish("policy_started", first_target_monotonic=first_start, duration_s=duration)
        next_tick, last_print = first_start, first_start
        tick_index = 0
        next_alignment = first_start
        error_peak = 0.
        try:
            while True:
                now = time.monotonic()
                if now >= deadline:
                    # Waking after the deadline does not erase an earlier
                    # worker error, nor turn an explicit STOP into normal return.
                    if cancelled(cfg, stop_event):
                        raise HardwareBlocked("Software stop requested; no automatic return")
                    with lock:
                        failure = shared["worker_error"]
                    if failure:
                        raise HardwareBlocked("Asynchronous worker failed: " + failure)
                    break  # Stop policy dispatch at deadline; do not replay pending actions.
                if cancelled(cfg, stop_event):
                    raise HardwareBlocked("Software stop requested; no automatic return")
                with lock:
                    current, failure = shared["plan"], shared["worker_error"]
                    source_request_id = next((rid for cut, rid in reversed(shared["plan_sources"]) if cut <= now), None)
                    source_plan = shared["source_plans"].get(source_request_id)
                if failure:
                    raise HardwareBlocked("Asynchronous worker failed: " + failure)
                if now < next_tick:
                    stop_event.wait(min(next_tick - now, .02))
                    continue
                try:
                    require_gripper_feedback(adapter.feedback(require_can=True),
                        max_age_s=adapter.cfg.get("max_feedback_age_s", .2),
                        require_enabled=True, now_monotonic=time.monotonic())
                except HardwareBlocked as exc:
                    if hasattr(exc, "gripper_guard"):
                        session.report["policy_gripper_failure"] = dict(exc.gripper_guard)
                    raise
                # Feedback/guards may block. A pre-read clock and target are
                # no longer a valid dispatch decision after they return.
                now = time.monotonic()
                if cancelled(cfg, stop_event):
                    raise HardwareBlocked("Software stop requested; no automatic return")
                with lock:
                    current, failure = shared["plan"], shared["worker_error"]
                    source_request_id = next((rid for cut, rid in reversed(shared["plan_sources"]) if cut <= now), None)
                    source_plan = shared["source_plans"].get(source_request_id)
                if failure:
                    raise HardwareBlocked("Asynchronous worker failed: " + failure)
                if now >= deadline:
                    break
                target, _velocity = current.sample(now)
                angles = [math.degrees(x) for x in target[:6]]
                dispatch_context = {
                    "run_id": session.path.name if hasattr(session, "path") else None,
                    "request_id": source_request_id, "block_id": source_request_id,
                    "plan_revision": None if source_plan is None else source_plan["revision"],
                    "clock_domain": capture_domain,
                    "scheduled_tick_monotonic_ns": round(next_tick * 1e9),
                    "reference_sample_monotonic_ns": round(now * 1e9),
                    "final_target_rad_m": list(target),
                    "raw_position": None if source_plan is None else {
                        "basis": "nominal model time coordinate; not shared 7D retimed progress",
                        "action_index": (now - source_plan["origin"]) * cfg["action_time_hz"]}}
                dispatch = getattr(adapter, "dispatch", None)
                try:
                    # Inspect the existing local feedback cache before any
                    # joint write, then recheck in the gripper writer. CAN is
                    # sequential, so this is not an atomic seven-axis command.
                    with dispatch(dispatch_context) if callable(dispatch) else nullcontext():
                        if cancelled(cfg, stop_event):
                            raise HardwareBlocked("Software stop requested; no automatic return")
                        if time.monotonic() >= deadline:
                            break
                        adapter.send_joint_target_deg(angles)
                        if cancelled(cfg, stop_event):
                            raise HardwareBlocked("Software stop requested after joint write; no gripper target or automatic return")
                        if time.monotonic() >= deadline:
                            raise HardwareBlocked("Policy deadline crossed during joint write; partial dispatch, no automatic return")
                        adapter.send_gripper_m(target[6], require_enabled=True)
                except HardwareBlocked as exc:
                    if hasattr(exc, "gripper_guard"):
                        session.report["policy_gripper_failure"] = dict(exc.gripper_guard)
                    raise
                actual = adapter.feedback(require_can=True)
                if now + 1e-9 >= next_alignment:
                    skipped = max(0, int((now - next_alignment + 1e-9) / .1))
                    alignment_counts["skipped_periods"] += skipped
                    next_alignment += (skipped + 1) * .1
                    _policy_buffer_append(alignment, alignment_counts,
                        _policy_alignment_sample(now, time.monotonic(), target, actual, source_request_id))
                else:
                    alignment_counts["dispatch_ticks_not_sampled"] += 1
                error_peak = max(error_peak, max(abs(a - b) for a, b in zip(actual["q_deg"], angles)))
                metrics["reference_dispatch_count"] += 1
                if not dry_run:
                    metrics["targets_sent"] += 1
                if now - last_print >= 2:
                    label = "软件参考与真实静态反馈差（不评估跟踪）" if dry_run else "tracking error仅诊断"
                    print(f"运行 {now-first_start:.1f}/{duration:.1f}s; RPC {metrics['rpc_count']}; {label} {error_peak:.2f}°", flush=True)
                    last_print = now
                tick_index, next_tick, missed = advance_policy_tick(
                    first_start, tick_index, time.monotonic(), cfg["policy_send_hz"])
                metrics["missed_reference_slots"] += missed
                if not dry_run:
                    metrics["missed_CAN_slots"] += missed
        finally:
            control_error = sys.exc_info()[1]
            dispatch_ended = time.monotonic()
            worker_stop.set()
            thread.join(timeout=6)
            # A worker may publish its original control failure only during
            # join. Saving USB evidence must not replace that failure either.
            pending_control_error = None
            if control_error is None:
                if not thread.is_alive() and shared["worker_error"]:
                    pending_control_error = HardwareBlocked("Asynchronous worker failed: " + shared["worker_error"])
                elif cancelled(cfg, stop_event):
                    pending_control_error = HardwareBlocked("Software stop requested; no automatic return")
                elif thread.is_alive():
                    pending_control_error = HardwareBlocked("Inference worker did not close; no automatic return")
            metrics.update(worker_stopped=not thread.is_alive(), reference_to_actual_deviation_deg_diagnostic_only=error_peak,
                           ended_at_monotonic=time.monotonic(), policy_timed_duration_s=duration,
                           policy_requested_duration_s=duration,
                           policy_dispatch_elapsed_s=max(0., dispatch_ended-first_start),
                           dispatch_ended_at_monotonic=dispatch_ended,
                           policy_timed_duration_basis="legacy requested duration; actual elapsed is policy_dispatch_elapsed_s",
                           worker_error_captured_monotonic=shared["worker_error_at"])
            if dry_run:
                metrics["CAN_send_calls"] = getattr(adapter, "CAN_send_calls", None)
            else:
                metrics["tracking_error_peak_deg_diagnostic_only"] = error_peak
            session.report.update(policy_metrics=metrics, recent_RTC_events=list(events))
            session.report["policy_rpc_evidence"] = {"capacity": rpc_evidence.maxlen,
                "counts": rpc_evidence_counts, "events": list(rpc_evidence),
                "persistence": "bounded RAM during control; one final save"}
            session.report["policy_alignment_evidence"] = {"requested_hz": 10, "capacity": alignment.maxlen,
                "counts": alignment_counts, "samples": list(alignment),
                "basis": "existing reference/feedback read; no additional RPC or hardware read",
                "persistence": "bounded RAM during control; one final save",
                "physical_tracking_verified": False}
            try:
                session.save()
            except Exception as exc:
                session.report["policy_evidence_save_error"] = repr(exc)
                if control_error is None and pending_control_error is None:
                    raise
            if pending_control_error is not None:
                raise pending_control_error
        if thread.is_alive():
            raise HardwareBlocked("Inference worker did not close; no automatic return")
        if cancelled(cfg, stop_event):
            raise HardwareBlocked("Software stop requested; no automatic return")
        # A pre-deadline exception may have waited for publication until join.
        # Only unused post-deadline RPC failures are excluded by the worker.
        if shared["worker_error"]:
            raise HardwareBlocked("Asynchronous worker failed: " + shared["worker_error"])
        if dry_run and metrics["CAN_send_calls"] != 0:
            raise HardwareBlocked("No-CAN validation sink reports unexpected CAN sending")
        session.event("POLICY_DEADLINE_REACHED_NO_NEW_POLICY_TARGETS")
        return metrics
    finally:
        original_error = sys.exc_info()[1]
        cleanup_error = None
        cleanup_errors = []
        def cleanup_failure(stage, exc):
            nonlocal cleanup_error
            if cleanup_error is None:
                cleanup_error = exc
            cleanup_errors.append({"stage": stage, "error": repr(exc)})
            session.report["policy_cleanup_errors"] = list(cleanup_errors)
        try:
            client.close()
        except Exception as exc:
            cleanup_failure("client.close", exc)
            session.report["policy_client_close_error"] = repr(exc)
        if tx_writer is not None:
            try:
                session.report["tx_evidence"] = tx_writer.close(timeout_s=5)
            except Exception as exc:
                cleanup_failure("tx_writer.close", exc)
            try:
                session.report["tx_adapter_evidence"] = adapter.tx_evidence_status
            except Exception as exc:
                cleanup_failure("tx_adapter.status", exc)
            finally:
                try:
                    adapter.attach_tx_evidence(None)
                except Exception as exc:
                    cleanup_failure("tx_adapter.detach", exc)
            try:
                session.save()
            except Exception as exc:
                cleanup_failure("tx_evidence.save", exc)
                session.report["tx_evidence_save_error"] = repr(exc)
        if original_error is None and cleanup_error is not None:
            raise cleanup_error


def run_three_stage(cfg, adapter, session, stop_event, duration, path_reviewed):
    # Independent low-rate I/O guard. No storage or HTTP request occurs in the
    # 50Hz CAN sender; a failed recorder must not yield a fake complete review.
    from .storage import verify_usb
    guard_stop = threading.Event()
    guard_errors = []
    recovery_heartbeat_errors = []
    expected_path = session.report.get("owned_recording_path")
    session.report["recording_guard_expected_path"] = expected_path
    session.report["recording_guard_errors"] = guard_errors
    session.report["recording_guard_stopped"] = True

    def checked_recording():
        record = rpc(cfg, "/status?compact=1", timeout=2)["recording"]
        session.report["recording_guard_last"] = copy.deepcopy(record)
        if not isinstance(record, dict) or record.get("path") != expected_path:
            raise HardwareBlocked("Deployment recording owner/path changed; no stop sent to another episode")
        if (record.get("active") is not True or record.get("error")
                or record.get("overflows") != 0):
            raise HardwareBlocked("Deployment recorder not healthy/active: " + repr(record))
        return record

    # A first guard poll after .5s is too late to authorize preparation. The
    # path comes from this deployment's start receipt, never the latest episode.
    try:
        if not isinstance(expected_path, str) or not expected_path.strip():
            raise HardwareBlocked("Deployment owned recording path is missing; no motion started")
        verify_usb(cfg)
        session.report["recording_guard_initial"] = copy.deepcopy(checked_recording())
    except BaseException as exc:
        guard_errors.append(repr(exc))
        raise

    def guard():
        next_heartbeat = 0.
        while not guard_stop.wait(.5):
            try:
                verify_usb(cfg)
                checked_recording()
                if time.monotonic() >= next_heartbeat:
                    next_heartbeat = time.monotonic() + 2.
                    try:
                        rpc(cfg, "/recovery/heartbeat", {"session_id": session.path.name}, timeout=2)
                    except Exception as exc:
                        if len(recovery_heartbeat_errors) < 8:
                            recovery_heartbeat_errors.append(repr(exc))
            except Exception as exc:
                guard_errors.append(repr(exc))
                stop_event.set()
                break
    monitor = threading.Thread(target=guard, name="deployment-recording-guard", daemon=True)
    monitor.start()
    try:
        result = _three_stage(cfg, adapter, session, stop_event, duration, path_reviewed)
    finally:
        guard_stop.set()
        monitor.join(timeout=8)
        session.report["recording_guard_errors"] = guard_errors
        session.report["recording_guard_stopped"] = not monitor.is_alive()
        session.report["recovery_heartbeat_errors"] = recovery_heartbeat_errors
    # If cancellation arrived as the motion body finished, retain a failed
    # outcome instead of claiming that a replacement recorder belonged to us.
    if guard_errors or monitor.is_alive():
        stop_event.set()
        raise HardwareBlocked("Deployment recording guard failed or did not close; no automatic recovery")
    return result


def recovery_context(cfg, session, active):
    """Activate only observational bookkeeping; an error never issues a motion."""
    try:
        return rpc(cfg, "/recovery/context", {"session_id": session.path.name,
                   "deployment_active": active}, timeout=2)
    except Exception as exc:
        return {"available": False, "error": repr(exc), "commands_sent": 0}


def _three_stage(cfg, adapter, session, stop_event, duration, path_reviewed):
    publisher = getattr(session, "_task_publisher", None)
    if publisher is None:
        publisher = TaskEventPublisher(cfg, session.path.name if cfg.get("task_state", {}).get("enabled") is True else None)
    session._task_publisher = publisher
    context = (session.defer_persistence() if cfg.get("defer_policy_evidence_until_return") is True else nullcontext())
    try:
        with context:
            return _three_stage_body(cfg, adapter, session, stop_event, duration, path_reviewed, publisher)
    except BaseException as exc:
        phase = session.report.get("motion_phase", "prepare")
        if session.report.get("normal_return_result") is not None:
            publisher.publish("recording_failed", reason=repr(exc))
        elif phase in ("return_wait", "return"):
            publisher.publish("return_failed", reason=repr(exc), phase=phase,
                              target_sent=phase == "return")
            publisher.publish("fault", area="return", reason=repr(exc))
        else:
            publisher.publish("return_skipped", reason="Control failure before normal return; no automatic recovery")
            publisher.publish("fault", area=phase, reason=repr(exc))
        raise
    finally:
        # CLI cleanup owns the single FIFO until the final recording event.
        session.report["task_event_delivery"] = publisher.status()


def _three_stage_body(cfg, adapter, session, stop_event, duration, path_reviewed, publisher):
    cancel = lambda: cancelled(cfg, stop_event)
    pose_kwargs = dict(path_reviewed=path_reviewed, speed_deg_s=cfg["pose_speed_deg_s"],
        accel_deg_s2=cfg["pose_accel_deg_s2"], transmit_hz=cfg["pose_send_hz"], cancel=cancel,
        endpoint_tolerance_deg=cfg["endpoint_tolerance_deg"], settle_timeout_s=cfg["endpoint_settle_timeout_s"])
    session.event("PREPARE_READY_START", target_deg=cfg["ready_deg"])
    session.report["motion_phase"] = "prepare"
    publisher.publish("prepare_started", target_deg=cfg["ready_deg"])
    prepared = adapter.execute_pose(cfg["ready_deg"], **pose_kwargs)
    session.report["ready_pose_result"] = prepared
    session.save()
    gripper_prepared = None
    if "ready_gripper_m" in cfg:
        session.event("PREPARE_READY_GRIPPER_START", target_m=cfg["ready_gripper_m"],
                      tolerance_m=cfg.get("ready_gripper_tolerance_m"),
                      basis="explicit empty-gripper training start after six-joint ready pose")
        try:
            gripper_prepared = prepare_ready_gripper(cfg, adapter, cancel=cancel)
        except BaseException as exc:
            session.report["ready_gripper_result"] = getattr(exc, "preparation_receipt", {
                "status": "FAILED_NO_AUTOMATIC_RECOVERY", "failure": repr(exc),
                "target_m": cfg["ready_gripper_m"], "automatic_reset_disable_return": False})
            try:
                session.save()
            except Exception as save_exc:
                # Preserve the original control failure if storage also fails;
                # the caller's ordinary cleanup can still persist this detail.
                session.report["ready_gripper_evidence_write_error"] = repr(save_exc)
            raise
        session.report["ready_gripper_result"] = gripper_prepared
        session.save()
    session.report["recovery_start"] = recovery_context(cfg, session, True)
    publisher.publish("prepare_completed", real_preparation_complete=True)
    session.report["motion_phase"] = "policy"
    try:
        metrics = run_policy(cfg, adapter, session, stop_event, duration)
    finally:
        # Observational context is limited to inference/execution, excluding
        # preparation and the normal return. Its helper never sends motion.
        session.report["recovery_end"] = recovery_context(cfg, session, False)
    # Only the ordinary deadline route reaches this point. Faults/cancellation
    # propagate; no blind return, disable, firmware reset or coordinate write.
    publisher.publish("deadline", duration_s=duration)
    session.report["motion_phase"] = "return_wait"
    session.event("NORMAL_RETURN_WAIT_FOR_REAL_STATIONARY_FEEDBACK")
    wait_stationary(adapter, cancel, require_gripper=True)
    def return_gripper_guard():
        # Read the existing observer only; never issue a gripper command while
        # returning the joints. A driver fault cannot silently turn into a
        # normal return just because it appeared at the policy deadline.
        try:
            require_gripper_feedback(adapter.feedback(require_can=False),
                max_age_s=adapter.cfg.get("max_feedback_age_s", .2),
                require_enabled=True, now_monotonic=time.monotonic())
        except HardwareBlocked as exc:
            if hasattr(exc, "gripper_guard"):
                session.report["return_gripper_failure"] = dict(exc.gripper_guard)
            raise
        return True
    return_gripper_guard()
    session.event("NORMAL_RETURN_JOINT_ZERO_START", target_deg=cfg["joint_zero_deg"])
    publisher.publish("return_started", target_deg=cfg["joint_zero_deg"])
    session.report["motion_phase"] = "return"
    returned = adapter.execute_pose(cfg["joint_zero_deg"], companion_tick=return_gripper_guard, **pose_kwargs)
    session.report["normal_return_result"] = returned
    session.report["motion_phase"] = "return_complete"
    publisher.publish("return_completed", endpoint_verified=True, target_deg=cfg["joint_zero_deg"])
    publisher.publish("recording_pending", reason="Movement complete; recording finalization follows")
    result = {"ready": prepared, "policy": metrics, "normal_return": returned,
              "physical_task_success": "OPERATOR_REVIEW_REQUIRED", "power_loss_protection": False}
    if gripper_prepared is not None:
        result["ready_gripper"] = gripper_prepared
    return result
