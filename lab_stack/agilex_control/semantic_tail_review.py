"""Pure, causal tail-motion abstention for semantic replay candidates.

Only measured actual feedback is used. A low-motion result is not physical
standstill, object contact, release, task success, or permission to move.
The caller must display the untouched raw answer alongside this review.
"""
from copy import deepcopy
import math

from .temporal_observation import COMPONENTS, SCHEMA, causal_features


GUARD_VERSION = "tiger-phase-tail-motion-evidence-v1"
MOTION_PHASES = frozenset(("approaching", "transporting", "returning"))
PHASES = MOTION_PHASES | frozenset(("grasping", "placing", "unknown"))
_LIMITS = {
    "tail_window_s": .25,
    "maximum_full_history_s": 1.0,
    "minimum_unique_source_samples_per_component": 5,
    "minimum_actual_source_span_s": .15,
    "maximum_feedback_age_s": .2,
    "maximum_inter_source_gap_s": .1,
    "maximum_abs_joint_mean_velocity_deg_s": .25,
    "maximum_joint_path_velocity_deg_s": .25,
    "maximum_abs_gripper_mean_velocity_m_s": .0005,
    "maximum_gripper_path_velocity_m_s": .0005,
    "threshold_status": "Uncalibrated exploratory engineering deadbands",
    "scope": "Motion-class abstention; explicit all-phase abstention with no past row",
    "limitations": [
        "Slow movement and a legitimate pause may be declined as unknown",
        "No physical standstill, holding, release or task completion is inferred",
        "Grasping and placing are retained when any valid past history exists",
        "One-second history checks source quality, not present motion magnitude",
        "Host source-time estimates are not hardware generation timestamps",
    ],
}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _history(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot_missing")
    source = snapshot.get("stamps", {}).get("camera_source_index_v1", {})
    evidence = snapshot.get("stamps", {}).get("temporal_evidence", {})
    now, domain = source.get("sample_monotonic_ns"), source.get("clock_domain")
    if (type(now) is not int or now < 0 or not isinstance(domain, dict)
            or domain.get("identity_verified") is not True or not domain.get("session_id")
            or evidence.get("clock_domain") != domain
            or evidence.get("current_sample_monotonic_ns") != now):
        raise ValueError("temporal_clock_unusable")
    rows = evidence.get("state_rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("temporal_history_missing")
    records, last = [], None
    for row in rows:
        stamp = row.get("sample_monotonic_ns") if isinstance(row, dict) else None
        if (type(stamp) is not int or not max(0, now - 1_000_000_000) <= stamp <= now
                or last is not None and stamp <= last or not isinstance(row.get("right"), dict)):
            raise ValueError("temporal_rows_not_causal_unique_ordered")
        right = deepcopy(row["right"])
        timing = right.get("source_timing")
        if not isinstance(timing, dict):
            raise ValueError("temporal_source_identity_missing")
        for value in timing.values():
            if not isinstance(value, dict) or value.pop("domain_matches_current", None) is not True:
                raise ValueError("temporal_source_domain_unusable")
            value["clock_domain"] = domain
        records.append({**row, "right": right, "episode_id": snapshot.get("session_id"),
                        "clock_domain": domain})
        last = stamp
    current = snapshot.get("state", {}).get("right")
    if (last != now or not isinstance(current, dict)
            or any(records[-1]["right"].get(key) != current.get(key) for key in ("q_deg", "grip_m"))):
        raise ValueError("temporal_current_endpoint_unusable")
    return records, current, now, domain


def _points(records, name, indices, domain):
    """Validate full-window provenance; repeated sources add no evidence."""
    points, identities, stamps, duplicates = [], {}, {}, 0
    for row in records:
        right = row["right"]
        timing = right.get("source_timing", {}).get(name, {})
        stamp, available = timing.get("source_monotonic_ns_estimate"), timing.get("available_monotonic_ns")
        identity = timing.get("sample_id")
        age = right.get("age_gripper_s" if indices is None else "age_joint_s")
        if (right.get("feedback_fresh") is not True or not _finite(age) or not 0 <= age <= .2
                or timing.get("clock_domain") != domain or type(stamp) is not int or type(available) is not int
                or not 0 <= stamp <= available <= row["sample_monotonic_ns"]
                or row["sample_monotonic_ns"] - stamp > 200_000_000
                or not isinstance(identity, str) or not identity):
            raise ValueError("full_source_quality_unusable")
        q, grip = right.get("q_deg"), right.get("grip_m")
        if indices is None:
            if not _finite(grip):
                raise ValueError("full_source_values_unusable")
            values = [grip]
        else:
            if not isinstance(q, list) or len(q) != 6 or not all(_finite(v) for v in q):
                raise ValueError("full_source_values_unusable")
            values = [q[i] for i in indices]
        point = (stamp, values)
        if identity in identities or stamp in stamps:
            if identities.get(identity) != point or stamps.get(stamp) != (identity, values):
                raise ValueError("source_identity_values_conflict")
            duplicates += 1
            continue
        if points and stamp < points[-1][0]:
            raise ValueError("source_time_regressed")
        identities[identity] = point
        stamps[stamp] = (identity, values)
        points.append(point)
    return points, duplicates


def _metrics(points, now):
    span = (points[-1][0] - points[0][0]) / 1e9 if len(points) >= 2 else 0.0
    gap = max(((b[0] - a[0]) / 1e9 for a, b in zip(points, points[1:])), default=0.0)
    result = {"unique_source_samples": len(points), "actual_source_span_s": span,
              "maximum_inter_source_gap_s": gap,
              "latest_source_age_s": (now - points[-1][0]) / 1e9 if points else None}
    if span <= 0:
        return result
    delta = [b - a for a, b in zip(points[0][1], points[-1][1])]
    path = [sum(abs(b[1][j] - a[1][j]) for a, b in zip(points, points[1:]))
            for j in range(len(delta))]
    result.update(delta=delta, path=path, mean_velocity=[v / span for v in delta],
                  path_velocity=[v / span for v in path])
    if not all(_finite(v) for key in ("delta", "path", "mean_velocity", "path_velocity") for v in result[key]):
        raise ValueError("tail_features_nonfinite")
    return result


def tail_motion_assessment(raw_answer, snapshot, *, abstain_without_past=True):
    """Return review metadata without changing the raw answer or any probability.

    Reads the existing snapshot.stamps.temporal_evidence format. Only original
    source samples in [t-0.25s, t] determine motion magnitude; the full <=1s
    window checks clocks, actual values, identity and source continuity. There
    is no interpolation. Each source component needs >=5 distinct samples over
    >=0.15s, source age <=0.2s and gaps <=0.1s. Path limits scale by that source's
    actual span, so an out-and-back movement cannot vanish in mean velocity.

    With abstain_without_past=True, no earlier actual row explicitly abstains
    from every known phase. Otherwise only movement classes can be declined.
    Malformed history is not evidence of low motion and has its own reason.
    """
    raw_choice = raw_answer.get("choice") if isinstance(raw_answer, dict) else None
    if not isinstance(raw_choice, str) or raw_choice not in PHASES:
        raw_choice = None
    output = {"raw_choice": raw_choice, "choice": raw_choice, "reason": "not_motion_phase",
              "guard_version": GUARD_VERSION, "limits": deepcopy(_LIMITS), "review_only": True,
              "gate_applied": False, "abstain_without_past": bool(abstain_without_past),
              "full_source_quality": {}, "tail_evidence": {},
              "physical_task_result": "unverified", "motion_authority": False}
    if raw_choice is None:
        output["reason"] = "invalid_raw_answer"
        return output

    def decline(reason):
        output.update(choice="unknown", reason=reason, gate_applied=True)
        return output

    try:
        records, current, now, domain = _history(snapshot)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        output["source_quality_error"] = str(error)
        return decline("temporal_history_unusable") if raw_choice in MOTION_PHASES else output
    if abstain_without_past and not any(row["sample_monotonic_ns"] < now for row in records):
        return decline("no_past_phase_evidence")
    if raw_choice not in MOTION_PHASES:
        return output
    if (current.get("feedback_fresh") is not True or any(
            not _finite(current.get(key)) or not 0 <= current[key] <= .2 for key in ("age_joint_s", "age_gripper_s"))):
        return decline("current_feedback_unusable")
    try:
        declared = snapshot.get("state", {}).get("temporal", {})
        window = declared.get("window_requested_s")
        if (declared.get("schema") != SCHEMA or not _finite(window) or not 0 < window <= 1
                or not snapshot.get("session_id")):
            raise ValueError("full_history_summary_unusable")
        expected = causal_features(records, episode_id=snapshot["session_id"],
                                   sample_monotonic_ns=now, clock_domain=domain, window_s=window)["features"]
        if expected != declared:
            raise ValueError("full_history_summary_differs_from_actual")
        for name, indices in COMPONENTS:
            points, duplicates = _points(records, name, indices, domain)
            full = _metrics(points, now)
            full["duplicate_source_samples"] = duplicates
            # Full-window motion magnitudes are intentionally not retained.
            output["full_source_quality"][name] = {key: full[key] for key in (
                "unique_source_samples", "actual_source_span_s", "maximum_inter_source_gap_s",
                "latest_source_age_s", "duplicate_source_samples")}
            if full["maximum_inter_source_gap_s"] > .1:
                raise ValueError("full_source_gap_exceeds_limit")
            tail = [point for point in points if now - 250_000_000 <= point[0] <= now]
            metric = _metrics(tail, now)
            output["tail_evidence"][name] = metric
            if (metric["unique_source_samples"] < 5 or metric["actual_source_span_s"] < .15
                    or metric["maximum_inter_source_gap_s"] > .1
                    or metric["latest_source_age_s"] is None or metric["latest_source_age_s"] > .2):
                raise ValueError("tail_source_coverage_unusable")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        output["source_quality_error"] = str(error)
        return decline("tail_source_quality_unusable")
    tail = output["tail_evidence"]
    # Same amplitudes as v1, expressed as rates on each actual source span.
    low_joint = all(abs(v) <= .25 + 1e-12 and p <= .25 + 1e-12
                    for name in ("q12", "q34", "q56")
                    for v, p in zip(tail[name]["mean_velocity"], tail[name]["path_velocity"]))
    grip = tail["gripper"]
    low_gripper = (abs(grip["mean_velocity"][0]) <= .0005 + 1e-12
                   and grip["path_velocity"][0] <= .0005 + 1e-12)
    if low_joint and low_gripper:
        return decline("low_measured_tail_motion_evidence")
    output["reason"] = "tail_motion_not_ruled_out"
    return output
