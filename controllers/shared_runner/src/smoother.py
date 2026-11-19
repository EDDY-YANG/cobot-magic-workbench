"""Fixed-clock, six-joint command soft smoothing; no plant model or A/J cap.

The committed anchor and its previous accepted sample are boundary data.  Only
future node positions are optimized.  A second-position-difference cost is a
soft preference, not a continuous acceleration/jerk certificate or feedback MPC.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import threading
import time

import numpy as np
from scipy import linalg
from scipy.optimize import lsq_linear


class SmoothingRejected(ValueError):
    """Invalid input or a candidate that failed independent acceptance."""


@dataclass(frozen=True)
class SmoothResult:
    positions_rad: np.ndarray
    diagnostics: dict


@dataclass(frozen=True)
class _Structure:
    design: np.ndarray
    curvature: np.ndarray
    anchor_coeff: np.ndarray
    previous_coeff: np.ndarray
    factor: tuple[np.ndarray, bool]
    bytes_used: int


_CACHE: OrderedDict = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX_ENTRIES = 8
_CACHE_MAX_BYTES = 8 * 1024 * 1024
_CACHE_BYTES = 0
_KKT_TOL = 1e-8


def _readonly(value):
    # A bytes-backed copy cannot be made writable again by setflags().
    array = np.ascontiguousarray(value, dtype=np.float64)
    return np.frombuffer(array.tobytes(), dtype=np.float64).reshape(array.shape)


def _finite_array(value, shape, name):
    try:
        array = np.array(value, dtype=np.float64, copy=True)
    except (ValueError, TypeError, OverflowError) as exc:
        raise SmoothingRejected(f"{name} must be a finite numeric array") from exc
    if array.shape != shape or not np.isfinite(array).all():
        raise SmoothingRejected(f"{name} must have finite shape {shape}")
    return array


def _scalar(value, name, *, positive=False):
    if isinstance(value, (bool, np.bool_)) or not np.isscalar(value):
        raise SmoothingRejected(f"{name} must be a scalar")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SmoothingRejected(f"{name} must be finite") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise SmoothingRejected(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def _time_intervals(value):
    array = np.asarray(value)
    if array.ndim != 1 or len(array) < 1 or array.dtype.kind not in "iu":
        raise SmoothingRejected("times_ns must be a nonempty 1D integer array")
    values = tuple(int(item) for item in array)
    limits = np.iinfo(np.int64)
    if any(item < limits.min or item > limits.max for item in values):
        raise SmoothingRejected("times_ns must fit int64")
    intervals = tuple(right - left for left, right in zip(values, values[1:]))
    if any(item <= 0 for item in intervals):
        raise SmoothingRejected("times_ns must be strictly increasing")
    return values, intervals


def _get_structure(intervals_ns, nominal_dt_s, tracking_weight, second_weight):
    global _CACHE_BYTES
    key = (intervals_ns, nominal_dt_s, tracking_weight, second_weight)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return cached, True
    count = len(intervals_ns)
    curvature = np.zeros((count, count))
    anchor_coeff, previous_coeff = np.zeros(count), np.zeros(count)
    steps = np.asarray(intervals_ns, dtype=np.float64) / 1e9
    for index, next_dt in enumerate(steps):
        previous_dt = nominal_dt_s if index == 0 else steps[index - 1]
        # nominal_dt**2 times the nonuniform centered physical second derivative.
        # At equal nominal intervals this is q[i+1]-2*q[i]+q[i-1].
        scale = 2 * nominal_dt_s**2 / (previous_dt + next_dt)
        coefficients = (scale / next_dt, -scale * (1 / next_dt + 1 / previous_dt),
                        scale / previous_dt)
        for node, coefficient in zip((index + 1, index, index - 1), coefficients):
            if node == -1:
                previous_coeff[index] += coefficient
            elif node == 0:
                anchor_coeff[index] += coefficient
            else:
                curvature[index, node - 1] += coefficient
    design = np.vstack((math.sqrt(tracking_weight) * np.eye(count),
                        math.sqrt(second_weight) * curvature))
    if not np.isfinite(design).all():
        raise SmoothingRejected("nonfinite smoothing matrix")
    try:
        factor = linalg.cho_factor(design.T @ design, lower=True, check_finite=True)
    except (ValueError, linalg.LinAlgError) as exc:
        raise SmoothingRejected("smoothing objective is numerically singular") from exc
    arrays = [design, curvature, anchor_coeff, previous_coeff, factor[0]]
    arrays = [_readonly(array) for array in arrays]
    structure = _Structure(*arrays[:4], (arrays[4], factor[1]),
                           sum(array.nbytes for array in arrays))
    with _CACHE_LOCK:
        # Concurrent construction may duplicate temporary work, never share mutations.
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key], True
        if structure.bytes_used <= _CACHE_MAX_BYTES:
            while _CACHE and (len(_CACHE) >= _CACHE_MAX_ENTRIES or
                             _CACHE_BYTES + structure.bytes_used > _CACHE_MAX_BYTES):
                _, removed = _CACHE.popitem(last=False)
                _CACHE_BYTES -= removed.bytes_used
            _CACHE[key] = structure
            _CACHE_BYTES += structure.bytes_used
    return structure, False


def _verify_solution(design, target, solution, lower, upper):
    """Independent box/KKT check, evaluated from the original least-squares data."""
    if not np.isfinite(solution).all():
        raise SmoothingRejected("solver returned a nonfinite solution")
    scale = max(1., float(np.max(np.abs(solution))),
                float(np.max(np.abs(lower))), float(np.max(np.abs(upper))))
    bound_tolerance = 128 * np.finfo(np.float64).eps * scale
    bound_violation = max(0., float(np.max(lower - solution)), float(np.max(solution - upper)))
    if bound_violation > bound_tolerance:
        raise SmoothingRejected("solver candidate violates position bounds")
    with np.errstate(over="ignore", invalid="ignore"):
        residual = design @ solution - target
        gradient = design.T @ residual
        terms_scale = np.maximum(1., np.maximum(np.max(np.abs(design.T @ (design @ solution)), axis=0),
                                               np.max(np.abs(design.T @ target), axis=0)))
        objectives = np.sum(residual * residual, axis=0)
    if not all(np.isfinite(value).all() for value in (residual, gradient, terms_scale, objectives)):
        raise SmoothingRejected("nonfinite independent objective/KKT arithmetic")
    violation = np.abs(gradient)
    for axis in range(6):
        if lower[axis] == upper[axis]:
            violation[:, axis] = 0.  # Equality-fixed coordinates have unrestricted multipliers.
            continue
        at_lower = solution[:, axis] <= lower[axis] + bound_tolerance
        at_upper = solution[:, axis] >= upper[axis] - bound_tolerance
        violation[at_lower, axis] = np.maximum(-gradient[at_lower, axis], 0.)
        violation[at_upper, axis] = np.maximum(gradient[at_upper, axis], 0.)
    normalized = violation / terms_scale[None, :]
    maximum = float(np.max(normalized))
    if not np.isfinite(residual).all() or not np.isfinite(normalized).all() or maximum > _KKT_TOL:
        raise SmoothingRejected(f"independent KKT check failed: {maximum}")
    return {"position_bound_violation_rad": bound_violation,
            "position_bound_numerical_tolerance_rad": bound_tolerance,
            "kkt_max_scaled_residual": maximum, "kkt_tolerance": _KKT_TOL,
            "kkt_axis_scale": terms_scale.tolist(),
            "objective_by_axis": objectives.tolist()}


def smooth_tail(times_ns, reference_rad, anchor_rad, previous_sample_rad, bounds_rad,
                nominal_dt_s=.02, tracking_weight=10., second_weight=20.):
    """Return future commands on the supplied clock, with node zero fixed.

    previous_sample_rad is the actually accepted reference at
    times_ns[0] - nominal_dt_s, not a zero-velocity assumption.  The six-axis
    position box is the only inequality.  Solver failures raise; no clipping,
    raw-reference fallback, end-stop constraint or time rescaling is performed.
    """
    started = time.perf_counter_ns()
    times, intervals = _time_intervals(times_ns)
    reference = _finite_array(reference_rad, (len(times), 6), "reference_rad")
    anchor = _finite_array(anchor_rad, (6,), "anchor_rad")
    previous = _finite_array(previous_sample_rad, (6,), "previous_sample_rad")
    bounds = _finite_array(bounds_rad, (6, 2), "bounds_rad")
    nominal_dt_s = _scalar(nominal_dt_s, "nominal_dt_s", positive=True)
    tracking_weight = _scalar(tracking_weight, "tracking_weight")
    second_weight = _scalar(second_weight, "second_weight")
    if tracking_weight == second_weight == 0:
        raise SmoothingRejected("at least one objective weight must be positive")
    lower, upper = bounds[:, 0], bounds[:, 1]
    if np.any(lower > upper):
        raise SmoothingRejected("reversed position bounds")
    if np.any(anchor < lower) or np.any(anchor > upper):
        raise SmoothingRejected("committed anchor is outside position bounds")
    if np.any(previous < lower) or np.any(previous > upper):
        raise SmoothingRejected("previous accepted sample is outside position bounds")
    diagnostics = {"algorithm": "fixed_clock_command_second_difference_soft_smoothing",
                   "feedback_mpc": False, "plant_tau_used": False,
                   "tracking_weight": tracking_weight, "second_weight": second_weight,
                   "nominal_dt_s": nominal_dt_s, "nodes": len(times),
                   "time_grid_unchanged": True, "cut_ns": times[0], "end_ns": times[-1],
                   "hard_constraints": ["committed_anchor_position", "joint_position_bounds"],
                   "velocity_hard_limit": None, "acceleration_hard_limit": None,
                   "jerk_hard_limit": None, "continuous_jerk_certified": False,
                   "terminal_stop_constraint": False, "solver_axes": []}
    if not intervals:
        diagnostics.update(solver="NO_FREE_NODES", cache_hit=False, objective_by_axis=[0.] * 6,
                           kkt_max_scaled_residual=0., position_bound_violation_rad=0.,
                           total_ms=(time.perf_counter_ns() - started) / 1e6)
        return SmoothResult(_readonly(anchor[None, :]), diagnostics)
    structure, hit = _get_structure(intervals, nominal_dt_s, tracking_weight, second_weight)
    diagnostics["cache_hit"] = hit
    offsets = structure.anchor_coeff[:, None] * anchor + structure.previous_coeff[:, None] * previous
    target = np.vstack((math.sqrt(tracking_weight) * reference[1:],
                        -math.sqrt(second_weight) * offsets))
    solve_start = time.perf_counter_ns()
    try:
        solution = linalg.cho_solve(structure.factor, structure.design.T @ target,
                                   check_finite=True, overwrite_b=False)
    except (ValueError, linalg.LinAlgError) as exc:
        raise SmoothingRejected("Cholesky solve failed") from exc
    if not np.isfinite(solution).all():
        raise SmoothingRejected("Cholesky produced nonfinite values")
    for axis in range(6):
        if lower[axis] == upper[axis]:
            solution[:, axis] = lower[axis]
            diagnostics["solver_axes"].append({"axis": axis, "method": "FIXED_POSITION_EQUALITY", "success": True})
        elif np.any(solution[:, axis] < lower[axis]) or np.any(solution[:, axis] > upper[axis]):
            try:
                bounded = lsq_linear(structure.design, target[:, axis],
                                     bounds=(lower[axis], upper[axis]), method="bvls",
                                     tol=1e-12, max_iter=max(100, 5 * len(intervals)))
            except (ValueError, linalg.LinAlgError) as exc:
                raise SmoothingRejected("bounded least-squares solve failed") from exc
            diagnostics["solver_axes"].append({"axis": axis, "method": "BVLS", "success": bool(bounded.success),
                                                "status": int(bounded.status), "iterations": int(bounded.nit)})
            if not bounded.success or bounded.status <= 0:
                raise SmoothingRejected(f"bounded solver did not converge on axis {axis}: {bounded.message}")
            solution[:, axis] = bounded.x
        else:
            diagnostics["solver_axes"].append({"axis": axis, "method": "CHOLESKY", "success": True})
    diagnostics["solve_ms"] = (time.perf_counter_ns() - solve_start) / 1e6
    diagnostics.update(_verify_solution(structure.design, target, solution, lower, upper))
    positions = np.vstack((anchor, solution))
    before = structure.curvature @ reference[1:] + offsets
    after = structure.curvature @ solution + offsets
    difference = positions - reference
    diagnostics.update(solver="CHOLESKY_WITH_POSITION_BOX_BVLS_IF_NEEDED",
                       tracking_rms_rad=float(np.sqrt(np.mean(difference[1:] ** 2))),
                       tracking_max_abs_rad=float(np.max(np.abs(difference[1:]))),
                       anchor_to_input_reference_delta_rad=(anchor - reference[0]).tolist(),
                       terminal_reference_delta_rad=difference[-1].tolist(),
                       second_difference_before_rms_rad=float(np.sqrt(np.mean(before ** 2))),
                       second_difference_after_rms_rad=float(np.sqrt(np.mean(after ** 2))),
                       second_difference_before_max_abs_rad=float(np.max(np.abs(before))),
                       second_difference_after_max_abs_rad=float(np.max(np.abs(after))),
                       total_ms=(time.perf_counter_ns() - started) / 1e6)
    if any(not math.isfinite(value) for value in diagnostics.values() if isinstance(value, float)):
        raise SmoothingRejected("nonfinite smoothing diagnostics")
    return SmoothResult(_readonly(positions), diagnostics)
