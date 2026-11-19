"""Fixed-clock soft node smoothing with an explicitly clamped cut velocity.

This is a reference interpolator, not identified feedback MPC.  CubicSpline is
C2 inside this tail; only q/v match the previously accepted curve at its cut.
No velocity, acceleration or jerk limit is imposed.  Position overshoot rejects
the candidate, including overshoot between otherwise valid position nodes.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

import numpy as np
from scipy.interpolate import CubicSpline

from .smoother import smooth_tail


class ContinuousSmoothingRejected(ValueError):
    pass


@dataclass(frozen=True)
class ContinuousSmoothResult:
    relative_times_s: tuple
    q0: np.ndarray
    v0: np.ndarray
    a0: np.ndarray
    jerk: np.ndarray
    terminal_q: np.ndarray
    terminal_v: np.ndarray
    terminal_a: np.ndarray
    diagnostics: dict
    positions_rad: np.ndarray


def _readonly(value):
    data = np.ascontiguousarray(value, dtype=np.float64)
    return np.frombuffer(data.tobytes(), dtype=np.float64).reshape(data.shape)


def _array(value, shape, label):
    try:
        result = np.array(value, dtype=np.float64, copy=True)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ContinuousSmoothingRejected(f"{label} must be numeric") from exc
    if result.shape != shape or not np.isfinite(result).all():
        raise ContinuousSmoothingRejected(f"{label} requires finite shape {shape}")
    return result


def _relative_times(times_ns):
    original = np.asarray(times_ns)
    if original.ndim != 1 or len(original) < 2 or original.dtype.kind not in "iu":
        raise ContinuousSmoothingRejected("at least two integer nanosecond times required")
    absolute = tuple(int(value) for value in original)
    limits = np.iinfo(np.int64)
    if any(value < limits.min or value > limits.max for value in absolute):
        raise ContinuousSmoothingRejected("times must fit int64")
    if any(right <= left for left, right in zip(absolute, absolute[1:])):
        raise ContinuousSmoothingRejected("times must be strictly increasing")
    # Subtract Python integers before converting: never subtract large float clocks.
    relative = tuple((value - absolute[0]) / 1e9 for value in absolute)
    if not all(math.isfinite(value) for value in relative) or any(
            right <= left for left, right in zip(relative, relative[1:])):
        raise ContinuousSmoothingRejected("relative floating clock is not representable")
    return absolute, relative


def _position_and_derivative_extrema(times, q0, v0, a0, jerk, terminal_q):
    """Analytic segment extrema in binary64; not an outward interval certificate."""
    lower = np.minimum(q0.min(axis=0), terminal_q)
    upper = np.maximum(q0.max(axis=0), terminal_q)
    acceleration_peak = np.zeros(6)
    velocity_peak = np.zeros(6)
    roots_checked = 0
    for segment, length in enumerate(np.diff(times)):
        end_q = q0[segment] + length * (
            v0[segment] + length * (a0[segment] / 2 + length * jerk[segment] / 6))
        end_v = v0[segment] + length * (a0[segment] + length * jerk[segment] / 2)
        end_a = a0[segment] + length * jerk[segment]
        lower = np.minimum(lower, end_q)
        upper = np.maximum(upper, end_q)
        velocity_peak = np.maximum(velocity_peak, np.maximum(abs(v0[segment]), abs(end_v)))
        acceleration_peak = np.maximum(acceleration_peak, np.maximum(abs(a0[segment]), abs(end_a)))
        for axis in range(6):
            try:
                roots = np.polynomial.polynomial.polyroots(
                    [v0[segment, axis], a0[segment, axis], jerk[segment, axis] / 2])
            except (ValueError, np.linalg.LinAlgError) as exc:
                raise ContinuousSmoothingRejected("position-extrema root computation failed") from exc
            if not np.isfinite(roots).all():
                raise ContinuousSmoothingRejected("nonfinite position-extrema roots")
            for root in roots:
                # Include numerically almost-real roots conservatively as candidates.
                if abs(root.imag) <= 1e-12 * (1 + abs(root.real)) and 0 < root.real < length:
                    h = float(root.real)
                    value = q0[segment, axis] + h * (v0[segment, axis] + h * (
                        a0[segment, axis] / 2 + h * jerk[segment, axis] / 6))
                    lower[axis] = min(lower[axis], value)
                    upper[axis] = max(upper[axis], value)
                    roots_checked += 1
            if jerk[segment, axis] != 0:
                h = -a0[segment, axis] / jerk[segment, axis]
                if 0 < h < length:
                    value = v0[segment, axis] + h * (
                        a0[segment, axis] + h * jerk[segment, axis] / 2)
                    velocity_peak[axis] = max(velocity_peak[axis], abs(value))
    if not all(np.isfinite(value).all() for value in (lower, upper, acceleration_peak, velocity_peak)):
        raise ContinuousSmoothingRejected("nonfinite polynomial extrema")
    return lower, upper, velocity_peak, acceleration_peak, np.max(abs(jerk), axis=0), roots_checked


def fit_c1_tail(times_ns, reference_rad, anchor_rad, anchor_velocity_rad_s,
                previous_sample_rad, bounds_rad, tracking_weight=10., second_weight=20.):
    """Smooth six-joint nodes, then interpolate with the actual accepted cut v.

    The supplied timestamps and first position are preserved.  The left spline
    boundary is the caller's analytic cut velocity; the right boundary is zero
    acceleration, not zero velocity or a prescribed final target position.
    """
    started = time.perf_counter_ns()
    absolute, relative = _relative_times(times_ns)
    velocity = _array(anchor_velocity_rad_s, (6,), "anchor_velocity_rad_s")
    bounds = _array(bounds_rad, (6, 2), "bounds_rad")
    # The underlying routine also validates the anchor, previous sample, bounds,
    # weights and reference; it retains the same objective and failure behavior.
    nodes = smooth_tail(np.asarray(absolute, dtype=np.int64), reference_rad, anchor_rad,
                        previous_sample_rad, bounds, nominal_dt_s=.02,
                        tracking_weight=tracking_weight, second_weight=second_weight)
    spline_started = time.perf_counter_ns()
    try:
        spline = CubicSpline(np.asarray(relative), nodes.positions_rad, axis=0,
                             bc_type=((1, velocity), (2, np.zeros(6))), extrapolate=False)
    except (ValueError, np.linalg.LinAlgError) as exc:
        raise ContinuousSmoothingRejected("clamped cubic interpolation failed") from exc
    q0 = np.array(spline.c[3], copy=True)
    v0 = np.array(spline.c[2], copy=True)
    a0 = np.array(2 * spline.c[1], copy=True)
    jerk = np.array(6 * spline.c[0], copy=True)
    terminal_q = np.array(spline(relative[-1]), copy=True)
    terminal_v = np.array(spline(relative[-1], 1), copy=True)
    terminal_a = np.array(spline(relative[-1], 2), copy=True)
    if not all(np.isfinite(value).all() for value in (q0, v0, a0, jerk, terminal_q, terminal_v, terminal_a)):
        raise ContinuousSmoothingRejected("nonfinite cubic coefficients or terminal state")
    h = np.diff(relative)[:, None]
    ends_q = q0 + h * (v0 + h * (a0 / 2 + h * jerk / 6))
    ends_v = v0 + h * (a0 + h * jerk / 2)
    ends_a = a0 + h * jerk
    errors = {
        "position": float(np.max(abs(ends_q - np.vstack((q0[1:], terminal_q))))),
        "velocity": float(np.max(abs(ends_v - np.vstack((v0[1:], terminal_v))))),
        "acceleration": float(np.max(abs(ends_a - np.vstack((a0[1:], terminal_a))))),
        "cut_velocity": float(np.max(abs(v0[0] - velocity))),
        "interpolation_nodes": float(np.max(abs(np.vstack((q0, terminal_q)) - nodes.positions_rad))),
    }
    scales = {"position": max(1., float(np.max(abs(nodes.positions_rad)))),
              "velocity": max(1., float(np.max(abs(v0))), float(np.max(abs(terminal_v)))),
              "acceleration": max(1., float(np.max(abs(a0))), float(np.max(abs(terminal_a))))}
    check_scales = {**scales, "cut_velocity": max(1., float(np.max(abs(velocity)))),
                    "interpolation_nodes": scales["position"]}
    if any(not math.isfinite(error) or error > 1e-10 * check_scales[key] for key, error in errors.items()):
        raise ContinuousSmoothingRejected("cubic node or derivative continuity check failed")
    low, high, vpeak, apeak, jpeak, roots = _position_and_derivative_extrema(
        relative, q0, v0, a0, jerk, terminal_q)
    # This is an acceptance check, not clipping or expanded position limits.
    if np.any(low < bounds[:, 0]) or np.any(high > bounds[:, 1]):
        axis = int(np.flatnonzero((low < bounds[:, 0]) | (high > bounds[:, 1]))[0])
        raise ContinuousSmoothingRejected(
            f"continuous position overshoot on axis {axis}: [{low[axis]}, {high[axis]}] "
            f"outside [{bounds[axis, 0]}, {bounds[axis, 1]}]")
    diagnostics = dict(nodes.diagnostics)
    diagnostics.update({
        "interpolation": "CubicSpline_clamped_analytic_left_velocity_natural_right_acceleration",
        "cut_continuity": "C1_q_v_only_no_cross_cut_acceleration_or_jerk_continuity_claim",
        "tail_internal_continuity": "C2_in_exact_arithmetic_with_reported_binary64_residuals",
        "right_boundary": "natural_acceleration_zero_not_a_zero_velocity_stop",
        "cross_cut_acceleration_continuity_certified": False,
        "continuous_jerk_certified": False,
        "continuous_extrema_method": "piecewise_cubic_analytic_roots_binary64_not_interval_certificate",
        "continuous_position_lower_rad": low.tolist(),
        "continuous_position_upper_rad": high.tolist(),
        "position_extrema_roots_checked": roots,
        "continuous_velocity_peak_rad_s_by_axis": vpeak.tolist(),
        "continuous_acceleration_peak_rad_s2_by_axis": apeak.tolist(),
        "piecewise_jerk_peak_rad_s3_by_axis": jpeak.tolist(),
        "continuous_acceleration_peak_deg_s2": float(np.degrees(apeak).max()),
        "piecewise_jerk_peak_deg_s3": float(np.degrees(jpeak).max()),
        "continuity_residuals": errors,
        "requested_anchor_velocity_rad_s": velocity.tolist(),
        "initial_reference_acceleration_rad_s2": a0[0].tolist(),
        "terminal_velocity_rad_s": terminal_v.tolist(),
        "terminal_acceleration_rad_s2": terminal_a.tolist(),
        "spline_and_extrema_ms": (time.perf_counter_ns() - spline_started) / 1e6,
        "fit_total_ms": (time.perf_counter_ns() - started) / 1e6,
    })
    return ContinuousSmoothResult(relative, *(_readonly(value) for value in
        (q0, v0, a0, jerk, terminal_q, terminal_v, terminal_a)), diagnostics,
        _readonly(nodes.positions_rad))
