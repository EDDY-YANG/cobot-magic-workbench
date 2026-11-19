"""Review-only motion-evidence abstention for raw semantic phase candidates.

This pure function reads actual-feedback summaries, never task clocks, labels,
prior predictions, commands or devices. Low measured motion is not proof of a
stationary robot. Slow motions and pauses can therefore be declined as unknown.
No probability is produced, renormalised or substituted for the raw model output.
"""
from copy import deepcopy
import math


GUARD_VERSION = "tiger-phase-motion-evidence-v1"
MOTION_PHASES = frozenset(("approaching", "transporting", "returning"))
COMPONENTS = ("q12", "q34", "q56", "gripper")
_LIMITS = {
    "minimum_window_s": 0.8,
    "minimum_unique_source_samples_per_component": 20,
    "maximum_feedback_age_s": 0.2,
    "maximum_joint_path_deg_per_axis": 0.25,
    "maximum_abs_joint_mean_velocity_deg_s": 0.25,
    "maximum_gripper_path_m": 0.0005,
    "maximum_abs_gripper_mean_velocity_m_s": 0.0005,
    "scope": "Abstention only for approaching, transporting and returning",
    "limitations": [
        "Exploratory engineering deadbands, not calibrated sensor noise bounds",
        "Slow motion or a legitimate pause may be declined as unknown",
        "Low joint/aperture motion does not prove physical standstill or holding",
        "Grasping and placing may include stationary contact and are not gated",
        "Summary input does not expose the largest inter-sample source-time gap",
        "Source times are host-receipt estimates, not device generation times",
    ],
}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _at_least(value, minimum):
    return _finite(value) and value >= minimum


def _zero_count(value):
    return type(value) is int and value == 0


def _vector(value, *, nonnegative=False):
    return (isinstance(value, (list, tuple)) and len(value) == 6
            and all(_finite(v) and (not nonnegative or v >= 0) for v in value))


def phase_assessment(raw_answer, actual_temporal, current_actual):
    """Return a separate display choice without altering any model probability.

    ``raw_answer`` is the model's phase answer dict. ``actual_temporal`` is the
    ``tiger-causal-temporal-observation-v1`` summary; ``current_actual`` is the
    recorded actual right-arm feedback. All inputs remain untouched. A caller
    must keep the raw answer alongside this review result. This is not a motion
    permission, controller state, physical-success detector or latency claim.
    """
    raw_choice = raw_answer.get("choice") if isinstance(raw_answer, dict) else None
    if not isinstance(raw_choice, str):
        raw_choice = None
    output = {"raw_choice": raw_choice, "choice": raw_choice,
              "reason": "not_motion_phase", "guard_version": GUARD_VERSION,
              "limits": deepcopy(_LIMITS), "review_only": True, "gate_applied": False}
    if raw_choice is None:
        output["reason"] = "invalid_raw_answer"
        return output
    if raw_choice not in MOTION_PHASES:
        return output

    def decline(reason):
        output.update(choice="unknown", reason=reason, gate_applied=True)
        return output

    if not isinstance(current_actual, dict) or current_actual.get("feedback_fresh") is not True:
        return decline("current_feedback_unusable")
    for key in ("age_joint_s", "age_gripper_s"):
        age = current_actual.get(key)
        if not _finite(age) or not 0 <= age <= _LIMITS["maximum_feedback_age_s"]:
            return decline("current_feedback_unusable")

    t = actual_temporal
    if (not isinstance(t, dict) or t.get("schema") != "tiger-causal-temporal-observation-v1"
            or t.get("source_clock_rate_calculation") is not True
            or t.get("all_joint_rates_available") is not True
            or t.get("gripper_rate_available") is not True
            or not _at_least(t.get("window_requested_s"), _LIMITS["minimum_window_s"])
            or not _at_least(t.get("actual_span_s"), _LIMITS["minimum_window_s"])):
        return decline("motion_history_incomplete")
    components = t.get("components")
    if not isinstance(components, dict):
        return decline("motion_source_quality_unusable")
    for name in COMPONENTS:
        component = components.get(name)
        if not isinstance(component, dict):
            return decline("motion_source_quality_unusable")
        count = component.get("unique_source_samples")
        if (component.get("available") is not True or type(count) is not int
                or count < _LIMITS["minimum_unique_source_samples_per_component"]
                or not _at_least(component.get("actual_source_span_s"), _LIMITS["minimum_window_s"])
                or not _zero_count(component.get("invalid_or_missing_samples"))
                or not _zero_count(component.get("duplicate_source_samples"))):
            return decline("motion_source_quality_unusable")

    velocity, path = t.get("joint_mean_velocity_deg_s"), t.get("joint_path_deg")
    grip = t.get("gripper")
    if (not _vector(velocity) or not _vector(path, nonnegative=True)
            or not isinstance(grip, dict) or not _finite(grip.get("path_m"))
            or grip["path_m"] < 0 or not _finite(grip.get("mean_velocity_m_s"))):
        return decline("motion_features_unusable")
    low_motion = (
        max(abs(v) for v in velocity) <= _LIMITS["maximum_abs_joint_mean_velocity_deg_s"]
        and max(path) <= _LIMITS["maximum_joint_path_deg_per_axis"]
        and grip["path_m"] <= _LIMITS["maximum_gripper_path_m"]
        and abs(grip["mean_velocity_m_s"]) <= _LIMITS["maximum_abs_gripper_mean_velocity_m_s"])
    if low_motion:
        return decline("low_measured_motion_evidence")
    output["reason"] = "motion_not_ruled_out"
    return output
