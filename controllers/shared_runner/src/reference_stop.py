"""Normal reference stopping only: no SDK, feedback, writer or hardware claim.

Each coordinate decelerates from its supplied velocity to zero without reversal.
Position and velocity are continuous to floating-point arithmetic precision;
acceleration may jump at entry and at each stop event. No jerk bound is claimed.
The caller must supply an explicit seven-coordinate software deceleration budget.
An explicit reviewed command limiter may admit the inherited governor's tiny
numerical reference excess. Such curves are reported separately from strictly
position-bounded references; their states are never clipped or reset.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math

import numpy as np

from .command_pipeline import CubicCurve


NS = 1_000_000_000
TERMINAL_HOLD_NS = 20_000_000
INT64_MAX = (1 << 63) - 1


class StopInfeasible(ValueError):
    """No valid normal-stop reference was returned; the original state is intact."""

    def __init__(self, reason, diagnostics=None):
        self.reason = str(reason)
        self.diagnostics = {'status': 'STOP_INFEASIBLE', 'reason': self.reason,
                            **(diagnostics or {})}
        super().__init__(self.reason)


@dataclass(frozen=True)
class StopResult:
    curve: CubicCurve
    diagnostics: dict


def _vector(value, shape, name):
    try:
        result = np.array(value, dtype=np.float64, copy=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StopInfeasible(f'{name} must contain finite numeric values') from exc
    if result.shape != shape or not np.isfinite(result).all():
        raise StopInfeasible(f'{name} must have finite shape {shape}')
    return result


def _duration_without_increased_deceleration(speed, deceleration):
    exact = Fraction.from_float(speed) / Fraction.from_float(deceleration)
    try:
        duration = float(exact)
    except OverflowError as exc:
        raise StopInfeasible('nonzero velocity has an unrepresentable stop duration') from exc
    if not math.isfinite(duration) or duration <= 0:
        raise StopInfeasible('nonzero velocity has an unrepresentable stop duration')
    rounded_up = False
    if Fraction.from_float(duration) < exact:
        duration = math.nextafter(duration, math.inf)
        rounded_up = True
    applied = speed / duration
    if not math.isfinite(duration) or not 0 < applied <= deceleration:
        raise StopInfeasible('stop duration cannot respect supplied deceleration')
    return duration, applied, rounded_up


def _ceil_nanoseconds(seconds):
    value = Fraction.from_float(seconds) * NS
    return (value.numerator + value.denominator - 1) // value.denominator


def _command_allowance(command_limiter, bounds):
    if command_limiter is None:
        return np.zeros(7)
    # Import at call time: the existing stop curve type lives in the queue module.
    from .command_limits import CommandLimiter
    expected = np.asarray([math.radians(1e-10)] * 6 + [1e-10])
    if (type(command_limiter) is not CommandLimiter or
            not np.array_equal(command_limiter.bounds_rad_m, bounds) or
            not np.array_equal(command_limiter.reference_numerical_allowance_rad_m, expected)):
        raise StopInfeasible('stop requires the reviewed immutable command limiter with identical bounds')
    return expected


def _exact_excess(value, lower, upper):
    return max(Fraction(0), Fraction.from_float(float(lower)) - value,
               value - Fraction.from_float(float(upper)))


def build_normal_stop(start_ns: int, q_rad_m7, v_rad_m_s7, bounds7x2,
                      deceleration_rad_m_s2: object, *, command_limiter=None) -> StopResult:
    """Build an immutable, position-bounded normal-stop reference.

    Units are radians for six joints and metres for the gripper. Stop events
    retain their floating-point seconds; they are not rounded onto 20 ms ticks.
    The final integer timestamp rounds dynamic duration upward, then adds an
    explicit 20 ms stationary hold. This hold does not postpone deceleration
    and is not evidence of physical settling. There are no default A/J budgets.
    Without command_limiter, original strict position semantics remain unchanged.
    With it, only the reviewed inherited arithmetic allowance may exceed the raw
    reference bounds; the final encoded command bounds remain strict.
    """
    if type(start_ns) is not int or not 0 < start_ns <= INT64_MAX:
        raise StopInfeasible('start_ns must be a positive int64 monotonic clock')
    q = _vector(q_rad_m7, (7,), 'q_rad_m7')
    v = _vector(v_rad_m_s7, (7,), 'v_rad_m_s7')
    bounds = _vector(bounds7x2, (7, 2), 'bounds7x2')
    budget = _vector(deceleration_rad_m_s2, (7,), 'deceleration_rad_m_s2')
    if np.any(bounds[:, 0] >= bounds[:, 1]):
        raise StopInfeasible('position bounds must be strictly ordered')
    if np.any(budget <= 0):
        raise StopInfeasible('all seven deceleration budgets must be positive')
    if np.any(q < bounds[:, 0]) or np.any(q > bounds[:, 1]):
        raise StopInfeasible('initial position outside original bounds',
                             {'initial_q_rad_m': q.tolist()})
    allowance = _command_allowance(command_limiter, bounds)

    times = np.zeros(7)
    applied = np.zeros(7)
    rounded_axes = []
    stop_q = q.copy()
    violations = []
    rejected_violations = []
    for axis in range(7):
        if v[axis] == 0:
            continue  # Exact zero only: nonzero velocities are never reset.
        duration, magnitude, rounded_up = _duration_without_increased_deceleration(
            float(abs(v[axis])), float(budget[axis]))
        times[axis], applied[axis] = duration, magnitude
        if rounded_up:
            rounded_axes.append(axis)
        # Exact comparison of the float inputs avoids losing tiny outward
        # displacements when q is already at a position boundary.
        exact_stop = Fraction.from_float(float(q[axis])) + (
            Fraction.from_float(float(v[axis])) * Fraction.from_float(duration) / 2)
        try:
            stop_q[axis] = float(exact_stop)
        except OverflowError as exc:
            raise StopInfeasible('stop position exceeds finite arithmetic range',
                                 {'axis_zero_based': axis}) from exc
        excess = _exact_excess(exact_stop, *bounds[axis])
        if excess > 0:
            outward_at_boundary = ((q[axis] == bounds[axis, 0] and v[axis] < 0) or
                                   (q[axis] == bounds[axis, 1] and v[axis] > 0))
            accepted_numeric = (command_limiter is not None and not outward_at_boundary and
                                excess <= Fraction.from_float(float(allowance[axis])))
            violation = {'axis_zero_based': axis,
                               'initial_q': float(q[axis]), 'initial_v': float(v[axis]),
                               'stop_q': float(stop_q[axis]),
                               'signed_stop_distance': float(exact_stop - Fraction.from_float(float(q[axis]))),
                               'bounds': bounds[axis].tolist(),
                               'reference_excess_rad_m': float(excess),
                               'fixed_numerical_allowance_rad_m': float(allowance[axis]),
                               'outward_at_initial_boundary': bool(outward_at_boundary),
                               'accepted_only_as_command_numerical_excess': bool(accepted_numeric)}
            violations.append(violation)
            if not accepted_numeric:
                rejected_violations.append(violation)
    if rejected_violations:
        raise StopInfeasible('normal stopping distance exceeds original position bounds',
                             {'violations': rejected_violations,
                              'all_strict_reference_violations': violations,
                              'axis_stop_duration_s': times.tolist(),
                              'deceleration_budget_rad_m_s2': budget.tolist()})
    if not np.isfinite(stop_q).all():
        raise StopInfeasible('nonfinite stop position')

    maximum_time = float(times.max())
    dynamic_ns = _ceil_nanoseconds(maximum_time)
    total_ns = dynamic_ns + TERMINAL_HOLD_NS
    if start_ns + total_ns > INT64_MAX:
        raise StopInfeasible('stop endpoint exceeds int64 clock range')
    total_time = total_ns / NS
    if not math.isfinite(total_time) or total_time <= maximum_time:
        raise StopInfeasible('explicit stationary hold cannot be represented')
    knots = tuple(sorted({0., *(float(t) for t in times if t > 0), total_time}))
    positions, velocities, accelerations = [], [], []
    for left in knots[:-1]:
        qp, vp, ap = [], [], []
        for axis in range(7):
            duration = float(times[axis])
            if left >= duration:
                qp.append(float(stop_q[axis])); vp.append(0.); ap.append(0.)
            else:
                fraction = left / duration
                qp.append(float(q[axis] + v[axis] * left * (1 - .5 * fraction)))
                vp.append(float(v[axis] * (1 - fraction)))
                ap.append(float(-math.copysign(applied[axis], v[axis])))
        positions.append(qp); velocities.append(vp); accelerations.append(ap)
    positions = np.asarray(positions)
    velocities = np.asarray(velocities)
    accelerations = np.asarray(accelerations)
    jerk = np.zeros_like(positions)
    if not all(np.isfinite(x).all() for x in (positions, velocities, accelerations)):
        raise StopInfeasible('nonfinite stop polynomial coefficients')
    try:
        curve = CubicCurve(start_ns, start_ns + total_ns, knots, positions, velocities,
                           accelerations, jerk, stop_q)
    except (ValueError, FloatingPointError, OverflowError) as exc:
        raise StopInfeasible('stop polynomial position continuity failed',
                             {'detail': str(exc)}) from exc

    # Independently inspect every polynomial endpoint and stationary point.
    # No clipping or initial-state repair occurs. A numerical excess is kept in
    # the reference and reported; it is not relabeled as strict geometric success.
    low, high = curve.envelope()
    if not np.isfinite(low).all() or not np.isfinite(high).all():
        raise StopInfeasible('nonfinite rounded stop envelope')
    envelope_excess = np.asarray([float(max(
        _exact_excess(Fraction.from_float(float(low[i])), *bounds[i]),
        _exact_excess(Fraction.from_float(float(high[i])), *bounds[i]))) for i in range(7)])
    if any(_exact_excess(Fraction.from_float(float(value)), *bounds[axis]) >
           Fraction.from_float(float(allowance[axis]))
           for axis in range(7) for value in (low[axis], high[axis])):
        raise StopInfeasible('rounded stop polynomial exceeds original position bounds',
                             {'envelope_low': low.tolist(), 'envelope_high': high.tolist(),
                              'reference_envelope_excess_rad_m': envelope_excess.tolist(),
                              'fixed_numerical_allowance_rad_m': allowance.tolist()})
    h = np.diff(knots)[:, None]
    end_velocity = velocities + accelerations * h
    next_velocity = np.vstack([velocities[1:], np.zeros(7)])
    velocity_join_error = float(np.max(abs(end_velocity - next_velocity)))
    arithmetic_v_tolerance = 64 * np.finfo(float).eps * max(1., float(np.max(abs(v))))
    if velocity_join_error > arithmetic_v_tolerance:
        raise StopInfeasible('stop polynomial velocity continuity failed',
                             {'maximum_velocity_join_error': velocity_join_error})
    if (not np.array_equal(positions[0], q) or not np.array_equal(velocities[0], v) or
            np.any(abs(accelerations) > budget) or
            not np.array_equal(velocities[-1], np.zeros(7)) or
            not np.array_equal(accelerations[-1], np.zeros(7))):
        raise StopInfeasible('stop state or explicit budget invariant failed')

    strict_reference = not violations and bool(np.all(envelope_excess == 0))
    encoded_terminal = None
    if command_limiter is not None:
        try:
            # Componentwise clipping/quantization is monotone. Encoded analytic
            # extrema bound every encoded sample, including interior stop events.
            encoded_lower = command_limiter.encode(low)
            encoded_upper = command_limiter.encode(high)
            encoded_terminal = command_limiter.encode(stop_q)
            for encoded in (encoded_lower, encoded_upper, encoded_terminal):
                if (np.any(encoded.target_rad_m < bounds[:, 0]) or
                        np.any(encoded.target_rad_m > bounds[:, 1])):
                    raise ValueError('encoded stop command exceeded unchanged bounds')
        except (ValueError, TypeError, OverflowError) as exc:
            raise StopInfeasible('stop command encoding failed', {'detail': str(exc)}) from exc

    diagnostics = {
        'status': ('NORMAL_REFERENCE_STOP_READY_NOT_HARDWARE_ACCEPTANCE' if strict_reference else
                   'COMMAND_BOUNDED_NORMAL_STOP_WITH_NUMERICAL_EXCESS_NOT_HARDWARE_ACCEPTANCE'),
        'scope': 'PURE_CPU_SOFTWARE_REFERENCE_NO_FEEDBACK_OR_DEVICE',
        'already_stationary': bool(np.all(v == 0)),
        'start_ns': start_ns, 'end_ns': curve.end_ns,
        'initial_q_rad_m': q.tolist(), 'initial_v_rad_m_s': v.tolist(),
        'terminal_q_rad_m': stop_q.tolist(), 'terminal_v_rad_m_s': [0.] * 7,
        'signed_stop_distance_rad_m': (v * times / 2).tolist(),
        'axis_stop_duration_s': times.tolist(),
        'dynamic_stop_duration_s': maximum_time,
        'scheduled_dynamic_stop_duration_ns': dynamic_ns,
        'explicit_terminal_hold_ns': TERMINAL_HOLD_NS,
        'total_reference_duration_ns': total_ns,
        'actual_stationary_tail_s': total_time - maximum_time,
        'deceleration_budget_rad_m_s2': budget.tolist(),
        'applied_deceleration_rad_m_s2': applied.tolist(),
        'duration_rounded_up_axes_zero_based': rounded_axes,
        'dynamic_clock_rounding_ns': dynamic_ns - maximum_time * NS,
        'initial_velocity_reset': False, 'position_clipped': False,
        'deceleration_increased': False, 'jerk_limit': None,
        'continuous_jerk_certified': False,
        'continuity': 'POSITION_AND_VELOCITY_WITH_FLOAT_ARITHMETIC_TOLERANCE',
        'acceleration_jumps_allowed': True,
        'maximum_velocity_join_error_rad_m_s': velocity_join_error,
        'velocity_arithmetic_tolerance_rad_m_s': arithmetic_v_tolerance,
        'position_envelope_method': 'FLOAT_POLYNOMIAL_ENDPOINTS_AND_STATIONARY_ROOTS',
        'physical_settling_confirmed': False,
        'command_numerical_policy_enabled': command_limiter is not None,
        'strict_reference_within_bounds': strict_reference,
        'strict_stop_distance_within_bounds': not violations,
        'strict_reference_violations': violations,
        'reference_envelope_excess_rad_m': envelope_excess.tolist(),
        'fixed_numerical_allowance_rad_m': allowance.tolist(),
        'numerical_allowance_basis': ('FROZEN_GOVERNOR_NATIVE_1E_MINUS10_DEG_AND_M_NOT_HALF_SDK_STEP'
                                     if command_limiter is not None else 'STRICT_NO_ALLOWANCE'),
        'raw_reference_curve_clipped': False,
        'command_envelope_verified': command_limiter is not None,
        'terminal_command_rad_m': None if encoded_terminal is None else encoded_terminal.target_rad_m.tolist(),
        'terminal_joint_command_mdeg': None if encoded_terminal is None else list(encoded_terminal.joint_mdeg),
        'terminal_gripper_command_um': None if encoded_terminal is None else encoded_terminal.gripper_um,
        'encoded_commands_have_continuous_derivative_guarantee': False,
    }
    return StopResult(curve, diagnostics)
