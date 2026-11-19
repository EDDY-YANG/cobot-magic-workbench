"""Causal, observation-only features from recorded actual robot feedback.

No stages, object labels, commands, device access, or file writes are present.
Rates use the original CAN source timestamps, not the replay machine's clock.
An aperture change measures opening/closing only; it does not prove a grasp.
"""
from copy import deepcopy
import math


SCHEMA = "tiger-causal-temporal-observation-v1"
COMPONENTS = (("q12", (0, 1)), ("q34", (2, 3)), ("q56", (4, 5)), ("gripper", None))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def actual_record(state, episode_id, state_row):
    """Whitelist an actual sample, excluding actions and prior derived labels.

    Invalid/missing streams remain explicit rather than manufactured. The
    caller must already have frozen/verified its raw source before using this.
    """
    index = state.get("camera_source_index_v1", {})
    arm = state.get("sensors", {}).get("arms", {}).get("right", {})
    actual = {key: deepcopy(arm[key]) for key in (
        "q_deg", "grip_m", "feedback_fresh", "age_joint_s", "age_gripper_s", "age_status_s") if key in arm}
    clocks = {}
    for name, _ in COMPONENTS:
        timing = arm.get("source_timing", {}).get(name)
        if isinstance(timing, dict):
            clocks[name] = {key: deepcopy(timing[key]) for key in (
                "source_monotonic_ns_estimate", "available_monotonic_ns", "sample_id", "clock_domain") if key in timing}
    actual["source_timing"] = clocks
    return {"episode_id": episode_id, "state_row": state_row,
            "sample_wall_ns": state.get("sample_wall_ns"),
            "sample_monotonic_ns": index.get("sample_monotonic_ns"),
            "clock_domain": deepcopy(index.get("clock_domain")), "right": actual,
            "cameras": deepcopy(index.get("cameras", {}))}


def _valid_domain(domain):
    return isinstance(domain, dict) and domain.get("identity_verified") is True and bool(domain.get("session_id"))


def _source_point(record, name, indices, domain):
    right = record["right"]
    timing = right.get("source_timing", {}).get(name, {})
    stamp, available = timing.get("source_monotonic_ns_estimate"), timing.get("available_monotonic_ns")
    if (timing.get("clock_domain") != domain or type(stamp) is not int or type(available) is not int
            or not 0 <= stamp <= available <= record["sample_monotonic_ns"]
            or not isinstance(timing.get("sample_id"), str) or not timing["sample_id"]
            or right.get("feedback_fresh") is not True):
        return None
    q, grip = right.get("q_deg"), right.get("grip_m")
    if indices is None:
        if not finite(grip):
            return None
        values = [grip]
    else:
        if not isinstance(q, list) or len(q) != 6 or not all(finite(v) for v in q):
            return None
        values = [q[i] for i in indices]
    return stamp, timing["sample_id"], values


def causal_features(records, *, episode_id, sample_monotonic_ns, clock_domain, window_s=1.0):
    """Return a strict <=t window and source-clock kinematics, without stages.

    A domain/episode boundary cannot be crossed. Duplicate source identities
    or timestamps never add motion evidence. No interpolation is performed.
    """
    if not finite(window_s) or not 0 < window_s <= 2:
        raise ValueError("window_s must be finite and in (0, 2]")
    if type(sample_monotonic_ns) is not int or not _valid_domain(clock_domain):
        raise ValueError("A verified original source clock domain and integer time are required")
    start = sample_monotonic_ns - round(window_s * 1e9)
    history, seen, rejected = [], set(), {"future": 0, "outside_window": 0, "identity_or_clock": 0, "duplicate_timestamp": 0}
    for record in records:
        stamp = record.get("sample_monotonic_ns")
        if (record.get("episode_id") != episode_id or record.get("clock_domain") != clock_domain or type(stamp) is not int):
            rejected["identity_or_clock"] += 1
            continue
        if stamp > sample_monotonic_ns:
            rejected["future"] += 1
            continue
        if stamp < start:
            rejected["outside_window"] += 1
            continue
        if stamp in seen:
            rejected["duplicate_timestamp"] += 1
            continue
        seen.add(stamp)
        history.append(deepcopy(record))
    history.sort(key=lambda row: row["sample_monotonic_ns"])
    summary = {"schema": SCHEMA, "window_requested_s": window_s, "sample_count": len(history),
               "actual_span_s": ((history[-1]["sample_monotonic_ns"] - history[0]["sample_monotonic_ns"]) / 1e9
                                 if len(history) >= 2 else 0.0),
               "source_clock_rate_calculation": True,
               "limitations": "Host source-time estimates, not hardware generation timestamps. Motion is not task completion; aperture is not object contact.",
               "joint_delta_deg": [None] * 6, "joint_mean_velocity_deg_s": [None] * 6,
               "joint_path_deg": [None] * 6, "joint_peak_abs_velocity_deg_s": [None] * 6,
               "joint_mean_squared_velocity_deg2_s2": [None] * 6,
               "components": {}}
    for name, indices in COMPONENTS:
        points, source_ids, source_stamps, invalid, repeated = [], set(), set(), 0, 0
        for record in history:
            point = _source_point(record, name, indices, clock_domain)
            if point is None:
                invalid += 1
                continue
            stamp, identity, values = point
            if identity in source_ids or stamp in source_stamps:
                repeated += 1
                continue
            source_ids.add(identity)
            source_stamps.add(stamp)
            points.append((stamp, values))
        points.sort(key=lambda point: point[0])
        quality = {"unique_source_samples": len(points), "invalid_or_missing_samples": invalid,
                   "duplicate_source_samples": repeated, "available": len(points) >= 2}
        summary["components"][name] = quality
        if len(points) < 2:
            quality["reason"] = "At least two distinct valid actual source samples are required"
            continue
        span = (points[-1][0] - points[0][0]) / 1e9
        delta = [b - a for a, b in zip(points[0][1], points[-1][1])]
        path, peak, energy = [0.0] * len(delta), [0.0] * len(delta), [0.0] * len(delta)
        for (t0, a), (t1, b) in zip(points, points[1:]):
            dt = (t1 - t0) / 1e9
            for j, (x0, x1) in enumerate(zip(a, b)):
                velocity = (x1 - x0) / dt
                path[j] += abs(x1 - x0)
                peak[j] = max(peak[j], abs(velocity))
                energy[j] += velocity * velocity * dt
        quality["actual_source_span_s"] = span
        if indices is None:
            summary["gripper"] = {"delta_m": delta[0], "mean_velocity_m_s": delta[0] / span,
                                  "path_m": path[0], "peak_abs_velocity_m_s": peak[0],
                                  "latest_m": points[-1][1][0], "first_m": points[0][1][0],
                                  "trend": "opening" if delta[0] > .0005 else "closing" if delta[0] < -.0005 else "little_net_change",
                                  "trend_deadband_m": .0005}
        else:
            for j, axis in enumerate(indices):
                summary["joint_delta_deg"][axis] = delta[j]
                summary["joint_mean_velocity_deg_s"][axis] = delta[j] / span
                summary["joint_path_deg"][axis] = path[j]
                summary["joint_peak_abs_velocity_deg_s"][axis] = peak[j]
                summary["joint_mean_squared_velocity_deg2_s2"][axis] = energy[j] / span
    summary["all_joint_rates_available"] = all(v is not None for v in summary["joint_mean_velocity_deg_s"])
    summary["gripper_rate_available"] = "gripper" in summary
    return {"features": summary, "history": history, "rejected_rows": rejected}
