"""Fixed-time cubic B-spline soft smoothing with q/v/a in the optimization.

The first three clamped spline coefficients satisfy the accepted cut state.
Remaining coefficients minimize position error and a soft acceleration cost.
A conservative position convex hull is enforced; no A/J/v cap is imposed.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import threading
import time

import numpy as np
from scipy import linalg
from scipy.interpolate import BSpline
from scipy.optimize import lsq_linear

from .continuous_smoother import (
    ContinuousSmoothResult, _array, _position_and_derivative_extrema, _relative_times,
)
from .smoother import _readonly, _scalar, _verify_solution


class BoundarySmoothingRejected(ValueError):
    pass


@dataclass(frozen=True)
class _Basis:
    q: np.ndarray
    v: np.ndarray
    a: np.ndarray
    j: np.ndarray
    boundary: np.ndarray
    design: np.ndarray
    fixed_design: np.ndarray
    factor: tuple
    bytes_used: int


_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_BYTES = 0
_MAX_ENTRIES, _MAX_BYTES = 8, 8 * 1024 * 1024
_NOMINAL_DT = .02


def _basis(absolute, relative, tracking_weight, second_weight):
    global _CACHE_BYTES
    intervals = tuple(b - a for a, b in zip(absolute, absolute[1:]))
    key = (intervals, tracking_weight, second_weight)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key], True
    times = np.asarray(relative)
    knots = np.r_[np.repeat(times[0], 4), times[1:-1], np.repeat(times[-1], 4)]
    controls = len(knots) - 4
    spline = BSpline(knots, np.eye(controls), 3, axis=0, extrapolate=False)
    q, v, a, j = (np.asarray(spline(times, order), dtype=float) for order in range(4))
    boundary = np.vstack((q[0, :3], v[0, :3], a[0, :3]))
    if np.any(np.triu(boundary, 1) != 0) or np.any(np.diag(boundary) == 0):
        raise BoundarySmoothingRejected("clamped start basis is not the expected triangular system")
    if any(np.any(matrix[0, 3:] != 0) for matrix in (q, v, a)):
        raise BoundarySmoothingRejected("free coefficients influence the constrained initial state")
    full = np.vstack((math.sqrt(tracking_weight) * q,
                      math.sqrt(second_weight) * _NOMINAL_DT**2 * a))
    design, fixed = full[:, 3:], full[:, :3]
    if not all(np.isfinite(item).all() for item in (q, v, a, j, boundary, design, fixed)):
        raise BoundarySmoothingRejected("nonfinite B-spline basis")
    try:
        factor = linalg.cho_factor(design.T @ design, lower=True, check_finite=True)
    except (ValueError, linalg.LinAlgError) as exc:
        raise BoundarySmoothingRejected("free B-spline objective is numerically singular") from exc
    values = [_readonly(item) for item in (q, v, a, j, boundary, design, fixed, factor[0])]
    result = _Basis(*values[:7], (values[7], factor[1]), sum(item.nbytes for item in values))
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key], True
        if result.bytes_used <= _MAX_BYTES:
            while _CACHE and (len(_CACHE) >= _MAX_ENTRIES or _CACHE_BYTES + result.bytes_used > _MAX_BYTES):
                _, old = _CACHE.popitem(last=False)
                _CACHE_BYTES -= old.bytes_used
            _CACHE[key] = result
            _CACHE_BYTES += result.bytes_used
    return result, False


def fit_boundary_tail(times_ns, reference_rad, anchor_rad, anchor_velocity_rad_s,
                      anchor_acceleration_rad_s2, previous_sample_rad, bounds_rad,
                      tracking_weight=10., second_weight=20.):
    """Optimize the spline itself on an unchanged clock; reject rather than repair.

    previous_sample_rad is validated and retained as evidence.  It is not used
    as a substitute for the analytic cut velocity or acceleration.  Position
    bounds apply to all control coefficients, a sufficient but conservative
    condition; rejecting a coefficient does not prove physical infeasibility.
    """
    started = time.perf_counter_ns()
    absolute, relative = _relative_times(times_ns)
    reference = _array(reference_rad, (len(absolute), 6), "reference_rad")
    anchor = _array(anchor_rad, (6,), "anchor_rad")
    velocity = _array(anchor_velocity_rad_s, (6,), "anchor_velocity_rad_s")
    acceleration = _array(anchor_acceleration_rad_s2, (6,), "anchor_acceleration_rad_s2")
    previous = _array(previous_sample_rad, (6,), "previous_sample_rad")
    bounds = _array(bounds_rad, (6, 2), "bounds_rad")
    tracking_weight = _scalar(tracking_weight, "tracking_weight")
    second_weight = _scalar(second_weight, "second_weight")
    if tracking_weight == second_weight == 0:
        raise BoundarySmoothingRejected("at least one objective weight must be positive")
    lower, upper = bounds[:, 0], bounds[:, 1]
    if np.any(lower > upper):
        raise BoundarySmoothingRejected("reversed position bounds")
    if np.any(anchor < lower) or np.any(anchor > upper):
        raise BoundarySmoothingRejected("accepted anchor exceeds position bounds")
    if np.any(previous < lower) or np.any(previous > upper):
        raise BoundarySmoothingRejected("previous accepted position exceeds bounds")
    prepared, hit = _basis(absolute, relative, tracking_weight, second_weight)
    b = prepared.boundary
    fixed = np.empty((3, 6))
    # Forward substitution in the exact clamped-start q/v/a system.  c0 is the
    # original accepted q, never a corrected or reset boundary state.
    fixed[0] = anchor
    fixed[1] = (velocity - b[1, 0] * fixed[0]) / b[1, 1]
    fixed[2] = (acceleration - b[2, 0] * fixed[0] - b[2, 1] * fixed[1]) / b[2, 2]
    if not np.isfinite(fixed).all():
        raise BoundarySmoothingRejected("nonfinite fixed boundary coefficients")
    if np.any(fixed < lower) or np.any(fixed > upper):
        axis = int(np.argwhere((fixed < lower) | (fixed > upper))[0, 1])
        raise BoundarySmoothingRejected(
            f"fixed boundary coefficient violates conservative position hull on axis {axis}; "
            "no clipping/reset and no physical-infeasibility claim")
    target = np.vstack((math.sqrt(tracking_weight) * reference, np.zeros_like(reference)))
    target -= prepared.fixed_design @ fixed
    if not np.isfinite(target).all():
        raise BoundarySmoothingRejected("nonfinite least-squares target")
    solve_started = time.perf_counter_ns()
    try:
        free = linalg.cho_solve(prepared.factor, prepared.design.T @ target,
                                overwrite_b=False, check_finite=True)
    except (ValueError, linalg.LinAlgError) as exc:
        raise BoundarySmoothingRejected("B-spline Cholesky solve failed") from exc
    if not np.isfinite(free).all():
        raise BoundarySmoothingRejected("nonfinite B-spline solution")
    axis_reports = []
    for axis in range(6):
        if lower[axis] == upper[axis]:
            free[:, axis] = lower[axis]
            axis_reports.append({"axis": axis, "method": "FIXED_POSITION_EQUALITY", "success": True})
        elif np.any(free[:, axis] < lower[axis]) or np.any(free[:, axis] > upper[axis]):
            try:
                bounded = lsq_linear(prepared.design, target[:, axis], bounds=(lower[axis], upper[axis]),
                                     method="bvls", tol=1e-12, max_iter=max(100, 5 * len(free)))
            except (ValueError, linalg.LinAlgError) as exc:
                raise BoundarySmoothingRejected("bounded B-spline least squares failed") from exc
            axis_reports.append({"axis": axis, "method": "BVLS", "success": bool(bounded.success),
                                 "status": int(bounded.status), "iterations": int(bounded.nit)})
            if not bounded.success or bounded.status <= 0:
                raise BoundarySmoothingRejected(f"bounded B-spline solve failed on axis {axis}")
            free[:, axis] = bounded.x
        else:
            axis_reports.append({"axis": axis, "method": "CHOLESKY", "success": True})
    solve_ms = (time.perf_counter_ns() - solve_started) / 1e6
    checked = _verify_solution(prepared.design, target, free, lower, upper)
    coefficients = np.vstack((fixed, free))
    if np.any(coefficients < lower) or np.any(coefficients > upper):
        raise BoundarySmoothingRejected("candidate control coefficients exceed unchanged position hull")
    # Evaluate the same spline through de Boor and derivative coefficient
    # differences.  Multiplying derivative basis matrices by a large constant
    # offset introduces cancellation residue, even on a constant saturated span.
    # This changes neither coefficients nor position limits; no bound snapping.
    times = np.asarray(relative)
    knots = np.r_[np.repeat(times[0], 4), times[1:-1], np.repeat(times[-1], 4)]
    fitted = BSpline(knots, coefficients, 3, axis=0, extrapolate=False)
    # Use the basis partition-of-unity identity in translated coordinates.
    # A saturated constant support then evaluates exactly to its coefficient,
    # instead of 0.5 * sum(weights) becoming 0.5000000000000001.  This is the
    # same affine-invariant spline evaluation, not clipping to a position bound.
    positions = np.empty_like(reference)
    for index, row in enumerate(prepared.q):
        support = np.flatnonzero(row)
        origin = coefficients[int(support[0])]
        positions[index] = origin + row[support] @ (coefficients[support] - origin)
    velocities, accelerations, jerks = (fitted.derivative(order)(times) for order in (1, 2, 3))
    if not all(np.isfinite(item).all() for item in (positions, velocities, accelerations, jerks)):
        raise BoundarySmoothingRejected("nonfinite B-spline state reconstruction")
    q0, v0, a0, jerk = positions[:-1], velocities[:-1], accelerations[:-1], jerks[:-1]
    terminal_q, terminal_v, terminal_a = positions[-1], velocities[-1], accelerations[-1]
    h = np.diff(relative)[:, None]
    ends = (q0 + h * (v0 + h * (a0 / 2 + h * jerk / 6)),
            v0 + h * (a0 + h * jerk / 2), a0 + h * jerk)
    errors = {"position": float(np.max(abs(ends[0] - positions[1:]))),
              "velocity": float(np.max(abs(ends[1] - velocities[1:]))),
              "acceleration": float(np.max(abs(ends[2] - accelerations[1:]))),
              "cut_position": float(np.max(abs(q0[0] - anchor))),
              "cut_velocity": float(np.max(abs(v0[0] - velocity))),
              "cut_acceleration": float(np.max(abs(a0[0] - acceleration)))}
    scales = {"position": max(1., float(np.max(abs(positions)))),
              "velocity": max(1., float(np.max(abs(velocities)))),
              "acceleration": max(1., float(np.max(abs(accelerations)))),
              "cut_position": max(1., float(np.max(abs(anchor)))),
              "cut_velocity": max(1., float(np.max(abs(velocity)))),
              "cut_acceleration": max(1., float(np.max(abs(acceleration))))}
    if any(not math.isfinite(value) or value > 1e-9 * scales[name] for name, value in errors.items()):
        raise BoundarySmoothingRejected(f"B-spline state continuity failed: {errors}")
    low, high, vpeak, apeak, jpeak, roots = _position_and_derivative_extrema(
        relative, q0, v0, a0, jerk, terminal_q)
    if np.any(low < lower) or np.any(high > upper):
        axis = int(np.flatnonzero((low < lower) | (high > upper))[0])
        raise BoundarySmoothingRejected(f"polynomial position envelope exceeds original bounds on axis {axis}")
    difference = positions - reference
    track_cost = tracking_weight * np.sum(difference * difference, axis=0)
    second_cost = second_weight * np.sum((_NOMINAL_DT**2 * accelerations)**2, axis=0)
    if not np.isfinite(track_cost).all() or not np.isfinite(second_cost).all():
        raise BoundarySmoothingRejected("nonfinite physical objective diagnostics")
    diagnostics = {**checked,
        "algorithm": "boundary_matched_cubic_bspline_command_soft_smoothing",
        "tracking_weight": tracking_weight, "second_weight": second_weight,
        "nominal_dt_s": _NOMINAL_DT, "time_grid_unchanged": True,
        "nodes": len(absolute), "control_coefficients": len(coefficients), "fixed_coefficients": 3,
        "cut_ns": absolute[0], "end_ns": absolute[-1], "cache_hit": hit,
        "solver": "CHOLESKY_WITH_POSITION_HULL_BVLS_IF_NEEDED", "solver_axes": axis_reports,
        "solve_ms": solve_ms, "fit_total_ms": (time.perf_counter_ns() - started) / 1e6,
        "feedback_mpc": False, "plant_tau_used": False,
        "hard_constraints": ["accepted_cut_q_v_a", "control_coefficient_position_convex_hull"],
        "position_hull_is_conservative": True, "velocity_hard_limit": None,
        "acceleration_hard_limit": None, "jerk_hard_limit": None,
        "terminal_stop_constraint": False, "right_boundary": "unconstrained_terminal_state",
        "cut_continuity": "q_v_a_in_basis_boundary_system_with_reported_binary64_residuals",
        "continuous_jerk_certified": False, "continuity_residuals": errors,
        "previous_sample_role": "validated_evidence_only_not_a_substitute_for_analytic_q_v_a",
        "previous_sample_rad": previous.tolist(),
        "requested_anchor_velocity_rad_s": velocity.tolist(),
        "requested_anchor_acceleration_rad_s2": acceleration.tolist(),
        "tracking_rms_rad": float(np.sqrt(np.mean(difference[1:] ** 2))),
        "tracking_max_abs_rad": float(np.max(abs(difference[1:]))),
        "terminal_reference_delta_rad": difference[-1].tolist(),
        "tracking_cost_by_axis": track_cost.tolist(), "second_derivative_cost_by_axis": second_cost.tolist(),
        "continuous_extrema_method": "piecewise_cubic_analytic_roots_binary64_not_interval_certificate",
        "continuous_position_lower_rad": low.tolist(), "continuous_position_upper_rad": high.tolist(),
        "position_extrema_roots_checked": roots,
        "continuous_velocity_peak_rad_s_by_axis": vpeak.tolist(),
        "continuous_acceleration_peak_rad_s2_by_axis": apeak.tolist(),
        "piecewise_jerk_peak_rad_s3_by_axis": jpeak.tolist(),
        "continuous_acceleration_peak_deg_s2": float(np.degrees(apeak).max()),
        "piecewise_jerk_peak_deg_s3": float(np.degrees(jpeak).max()),
        "initial_reference_acceleration_rad_s2": a0[0].tolist(),
        "terminal_velocity_rad_s": terminal_v.tolist(), "terminal_acceleration_rad_s2": terminal_a.tolist(),
    }
    return ContinuousSmoothResult(relative, *(_readonly(item) for item in
        (q0, v0, a0, jerk, terminal_q, terminal_v, terminal_a)), diagnostics, _readonly(positions))
