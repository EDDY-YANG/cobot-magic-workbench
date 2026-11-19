"""Filter-aware reference braking in an exact auxiliary double-integrator.

tau=20ms is a software filter constant, not an identified Piper plant parameter.
For a_dot=(u-a)/tau, z=q+tau*v and w=v+tau*a satisfy z_dot=w,
w_dot=u. The frozen governor therefore plans z/w rather than the lagging q/v.
The physical reference still integrates the original exponential q/v/a dynamics;
its states are never reset to the auxiliary trajectory or clipped to the bounds.
"""
from __future__ import annotations

from dataclasses import dataclass
import bisect
import math
import time

import numpy as np
from scipy.optimize import brentq

from frozen_governor.h50_async import governor_step, ACCEL
from .smoother import _readonly


NS, PERIOD_NS = 1_000_000_000, 20_000_000
TAU_S = .02
# Frozen governor checks its auxiliary degree-coordinate trajectory with this
# native arithmetic allowance. Actual q below retains the original strict check.
AUXILIARY_POSITION_ARITHMETIC_RAD = math.radians(1e-10)


class AccelerationFilterRejected(ValueError):
    pass


def _vector(value, shape, label):
    result = np.array(value, dtype=np.float64, copy=True)
    if result.shape != shape or not np.isfinite(result).all():
        raise AccelerationFilterRejected(f"{label} requires finite shape {shape}")
    return result


def _clock(value):
    if type(value) is not int or value < 0 or value > np.iinfo(np.int64).max:
        raise AccelerationFilterRejected("nonnegative int64-compatible nanosecond clock required")
    return value


def _state(q, v, a, desired, elapsed, tau):
    x = elapsed / tau
    one_minus_exp = -math.expm1(-x)
    # Stable integral of 1-exp(-x), including tiny endpoint intervals.
    remainder = (x*x*(.5 + x*(-1/6 + x*(1/24 + x*(-1/120 + x/720))))
                 if abs(x) < 1e-4 else x - one_minus_exp)
    delta = a - desired
    position = q + v * elapsed + .5 * desired * elapsed**2 + delta * tau**2 * remainder
    velocity = v + desired * elapsed + delta * tau * one_minus_exp
    acceleration = a + (desired - a) * one_minus_exp
    jerk = (desired - a) * math.exp(-x) / tau
    return {"q": position, "v": velocity, "a": acceleration, "j": jerk}


def _piece_envelope(q, v, a, desired, length, tau):
    end = _state(q, v, a, desired, length, tau)
    low, high = np.minimum(q, end['q']), np.maximum(q, end['q'])
    vpeak = np.maximum(abs(v), abs(end['v']))
    apeak = np.maximum(abs(a), abs(end['a']))
    jpeak = abs(desired - a) / tau
    roots_count = 0
    for axis in range(6):
        split = [0., length]
        if desired[axis] != a[axis]:
            ratio = desired[axis] / (desired[axis] - a[axis])
            if 0 < ratio < 1:
                turn_a = -tau * math.log(ratio)
                if 0 < turn_a < length:
                    split.insert(1, turn_a)
                    vv = _state(q, v, a, desired, turn_a, tau)['v'][axis]
                    vpeak[axis] = max(vpeak[axis], abs(vv))
        # a(t) is monotone. Splitting at a=0 makes v(t) monotone on each
        # interval, so a sign change brackets every interior position extremum.
        def velocity_at(at):
            return float(_state(q, v, a, desired, at, tau)['v'][axis])
        for left, right in zip(split, split[1:]):
            vl, vr = velocity_at(left), velocity_at(right)
            if vl == 0.:
                root = left
            elif vr == 0.:
                root = right
            elif (vl < 0) != (vr < 0):
                try:
                    root = brentq(velocity_at, left, right, xtol=np.nextafter(0., 1.),
                                  rtol=4*np.finfo(float).eps, maxiter=100)
                except (ValueError, RuntimeError) as exc:
                    raise AccelerationFilterRejected("velocity-root bracket did not converge") from exc
            else:
                continue
            value = _state(q, v, a, desired, root, tau)['q'][axis]
            low[axis], high[axis] = min(low[axis], value), max(high[axis], value)
            roots_count += int(0 < root < length)
    if not all(np.isfinite(value).all() for value in (low, high, vpeak, apeak, jpeak)):
        raise AccelerationFilterRejected("nonfinite exponential-piece extrema")
    return low, high, vpeak, apeak, jpeak, roots_count


@dataclass(frozen=True)
class AccelerationFilteredCurve:
    times_ns: tuple
    q0: np.ndarray
    v0: np.ndarray
    a0: np.ndarray
    desired_acceleration: np.ndarray
    tau_s: float
    terminal_q: np.ndarray
    terminal_v: np.ndarray
    terminal_a: np.ndarray
    position_lower: np.ndarray
    position_upper: np.ndarray
    diagnostics: dict

    def __post_init__(self):
        times = tuple(_clock(value) for value in self.times_ns)
        if len(times) < 2 or any(b <= a for a, b in zip(times, times[1:])):
            raise AccelerationFilterRejected("nonempty increasing reference pieces required")
        if self.tau_s != TAU_S:
            raise AccelerationFilterRejected("this isolated candidate fixes software tau at 20ms")
        object.__setattr__(self, 'times_ns', times)
        for name in ('q0', 'v0', 'a0', 'desired_acceleration'):
            object.__setattr__(self, name, _readonly(_vector(getattr(self, name), (len(times)-1, 6), name)))
        for name in ('terminal_q', 'terminal_v', 'terminal_a', 'position_lower', 'position_upper'):
            object.__setattr__(self, name, _readonly(_vector(getattr(self, name), (6,), name)))

    @property
    def start_ns(self):
        return self.times_ns[0]

    @property
    def origin_ns(self):
        return self.times_ns[0]

    @property
    def end_ns(self):
        return self.times_ns[-1]

    def state(self, at_ns, side='left'):
        at_ns = _clock(at_ns)
        if side not in ('left', 'right'):
            raise AccelerationFilterRejected("state side must be left or right")
        if not self.start_ns <= at_ns <= self.end_ns:
            raise AccelerationFilterRejected("reference exhausted; no extrapolation or implicit HOLD")
        index = (bisect.bisect_left(self.times_ns, at_ns) if side == 'left'
                 else bisect.bisect_right(self.times_ns, at_ns)) - 1
        index = max(0, min(index, len(self.q0)-1))
        elapsed = (at_ns - self.times_ns[index]) / NS
        return _state(self.q0[index], self.v0[index], self.a0[index],
                      self.desired_acceleration[index], elapsed, self.tau_s)

    def sample(self, at_ns):
        return self.state(at_ns, side='right')['q']

    def envelope(self):
        return self.position_lower.copy(), self.position_upper.copy()


def generate_filtered_tail(start_ns, end_ns, q0rad6, v0rad6, a0rad6, target_at_ns,
                           bounds6x2, tau_s=TAU_S):
    """Generate a six-joint reference from this branch's own q/v/a state.

    target_at_ns receives each piece endpoint, matching the existing governor's
    next-tick target convention. It must return already projected six-joint rad
    targets. Gripper and source-clock mapping are deliberately outside this API.
    """
    started = time.perf_counter_ns()
    start_ns, end_ns = _clock(start_ns), _clock(end_ns)
    if start_ns >= end_ns or tau_s != TAU_S or not callable(target_at_ns):
        raise AccelerationFilterRejected("positive horizon, callable target and fixed tau=.02 required")
    q = _vector(q0rad6, (6,), 'q0rad6')
    v = _vector(v0rad6, (6,), 'v0rad6')
    a = _vector(a0rad6, (6,), 'a0rad6')
    bounds = _vector(bounds6x2, (6, 2), 'bounds6x2')
    if np.any(bounds[:, 0] >= bounds[:, 1]) or np.any(q < bounds[:, 0]) or np.any(q > bounds[:, 1]):
        raise AccelerationFilterRejected("initial position or position box is invalid")
    caps = np.deg2rad(np.asarray(ACCEL[:6], dtype=float))
    # An accepted exponential state can exceed the nominal binary64 cap by
    # roundoff in deg/rad conversion (e.g. 1e-15 rad/s^2).  Keep that state,
    # record the arithmetic allowance, and never reset/clamp it to the cap.
    initial_a_roundoff = 128*np.finfo(float).eps * (1 + caps + abs(v)/TAU_S)
    if np.any(abs(a) > caps + initial_a_roundoff):
        raise AccelerationFilterRejected("initial acceleration exceeds existing governor cap; no reset")
    times = [start_ns]
    qs, vs, accs, desired_rows = [], [], [], []
    lower, upper = q.copy(), q.copy()
    velocity_peak, acceleration_peak, jerk_peak = abs(v), abs(a), np.zeros(6)
    root_count = 0
    desired_peak = np.zeros(6)
    control_roundoff_projection_count=0
    control_roundoff_projection_max=0.
    initial = {'q': q.copy(), 'v': v.copy(), 'a': a.copy()}
    auxiliary_initial_q=q+tau_s*v
    auxiliary_initial_v=v+tau_s*a
    auxiliary_stop=auxiliary_initial_q+auxiliary_initial_v*abs(auxiliary_initial_v)/(2*caps)
    if (np.any(auxiliary_initial_q<bounds[:,0]-AUXILIARY_POSITION_ARITHMETIC_RAD) or
            np.any(auxiliary_initial_q>bounds[:,1]+AUXILIARY_POSITION_ARITHMETIC_RAD)):
        raise AccelerationFilterRejected('filter-aware auxiliary initial position is outside conservative feasible domain')
    if (np.any(auxiliary_stop<bounds[:,0]-AUXILIARY_POSITION_ARITHMETIC_RAD) or
            np.any(auxiliary_stop>bounds[:,1]+AUXILIARY_POSITION_ARITHMETIC_RAD)):
        raise AccelerationFilterRejected('filter-aware auxiliary initial braking is outside conservative feasible domain')
    auxiliary_low,auxiliary_high=auxiliary_initial_q.copy(),auxiliary_initial_q.copy()
    auxiliary_identity_error=0.
    while times[-1] < end_ns:
        next_ns = min(times[-1] + PERIOD_NS, end_ns)
        dt = (next_ns - times[-1]) / NS
        desired = _vector(target_at_ns(next_ns), (6,), 'target_at_ns result')
        if np.any(desired < bounds[:, 0]) or np.any(desired > bounds[:, 1]):
            raise AccelerationFilterRejected("target must already satisfy existing position bounds")
        try:
            z=q+tau_s*v
            w=v+tau_s*a
            _, next_velocity_deg = governor_step(np.degrees(z).tolist(), np.degrees(w).tolist(),
                np.degrees(desired).tolist(), dt, np.degrees(bounds).tolist())
        except (RuntimeError, ValueError) as exc:
            raise AccelerationFilterRejected(f"frozen governor failed at piece {len(qs)}: {exc}") from exc
        desired_a = (np.deg2rad(next_velocity_deg) - w) / dt
        # Mirror the frozen governor's 1e-9 deg/s numerical velocity-change
        # allowance; do not clamp the actual acceleration or change its limit.
        numerical_a = np.deg2rad(1e-9) / dt + 128*np.finfo(float).eps
        if not np.isfinite(desired_a).all() or np.any(abs(desired_a) > caps + numerical_a):
            raise AccelerationFilterRejected("governor desired acceleration violated its existing cap")
        # Enforce the existing control-input cap after the native deg/rad
        # conversion. This only corrects a proposal already checked against the
        # frozen governor's arithmetic budget; never clamp actual q/v/a states.
        bounded_control=np.minimum(np.maximum(desired_a,-caps),caps)
        correction=abs(bounded_control-desired_a)
        control_roundoff_projection_count+=int(np.count_nonzero(correction))
        control_roundoff_projection_max=max(control_roundoff_projection_max,float(correction.max()))
        desired_a=bounded_control
        # z(t) is a quadratic double-integrator trajectory. Check its endpoints
        # and every interior velocity-zero point independently of governor_step.
        z_end=z+w*dt+.5*desired_a*dt**2
        z_low,z_high=np.minimum(z,z_end),np.maximum(z,z_end)
        for axis in range(6):
            if desired_a[axis]!=0:
                turn=-w[axis]/desired_a[axis]
                if 0<turn<dt:
                    value=z[axis]+w[axis]*turn+.5*desired_a[axis]*turn**2
                    z_low[axis]=min(z_low[axis],value);z_high[axis]=max(z_high[axis],value)
        if (np.any(z_low<bounds[:,0]-AUXILIARY_POSITION_ARITHMETIC_RAD) or
                np.any(z_high>bounds[:,1]+AUXILIARY_POSITION_ARITHMETIC_RAD)):
            raise AccelerationFilterRejected('auxiliary governor trajectory exceeded its unchanged numerical envelope')
        local = _piece_envelope(q, v, a, desired_a, dt, tau_s)
        lo, hi, vp, ap, jp, roots = local
        if np.any(lo < bounds[:, 0]) or np.any(hi > bounds[:, 1]):
            axis = int(np.flatnonzero((lo < bounds[:, 0]) | (hi > bounds[:, 1]))[0])
            raise AccelerationFilterRejected(
                f"filtered reference position overshoot on axis {axis}, piece {len(qs)}; braking lag not repaired")
        qs.append(q.copy()); vs.append(v.copy()); accs.append(a.copy()); desired_rows.append(desired_a.copy())
        final = _state(q, v, a, desired_a, dt, tau_s)
        q, v, a = final['q'], final['v'], final['a']
        auxiliary_identity_error=max(auxiliary_identity_error,float(np.max(abs(q+tau_s*v-z_end))),
                                     float(np.max(abs(v+tau_s*a-(w+desired_a*dt)))))
        auxiliary_low=np.minimum(auxiliary_low,z_low);auxiliary_high=np.maximum(auxiliary_high,z_high)
        if not all(np.isfinite(value).all() for value in (q, v, a)):
            raise AccelerationFilterRejected("nonfinite integrated state")
        lower, upper = np.minimum(lower, lo), np.maximum(upper, hi)
        velocity_peak = np.maximum(velocity_peak, vp)
        acceleration_peak = np.maximum(acceleration_peak, ap)
        jerk_peak = np.maximum(jerk_peak, jp)
        desired_peak = np.maximum(desired_peak, abs(desired_a))
        root_count += roots
        times.append(next_ns)
    cap_for_convexity = np.maximum(abs(initial['a']), desired_peak)
    if np.any(acceleration_peak > cap_for_convexity + 128*np.finfo(float).eps):
        raise AccelerationFilterRejected("positive-kernel acceleration convexity check failed")
    diagnostics = {
        'algorithm': 'filter_aware_auxiliary_double_integrator_governor_exact_q_v_a_integration',
        'auxiliary_coordinates': 'z=q+tau*v; w=v+tau*a; zdot=w; wdot=u',
        'auxiliary_initial_position_rad':auxiliary_initial_q.tolist(),
        'auxiliary_initial_velocity_rad_s':auxiliary_initial_v.tolist(),
        'auxiliary_initial_constant_control_stop_rad':auxiliary_stop.tolist(),
        'auxiliary_position_lower_rad':auxiliary_low.tolist(),
        'auxiliary_position_upper_rad':auxiliary_high.tolist(),
        'auxiliary_native_position_arithmetic_allowance_deg':1e-10,
        'auxiliary_identity_max_binary64_error':auxiliary_identity_error,
        'actual_q_position_bounds_expanded':False,
        'auxiliary_feasible_domain_is_conservative_not_physical_reachability_proof':True,
        'software_filter_tau_s': tau_s, 'identified_piper_dynamics_tau': False,
        'feedback_mpc': False, 'time_compression': False, 'time_grid_unchanged': True,
        'piece_count': len(qs), 'target_sample_convention': 'piece_endpoint_ns',
        'initial_q_rad': initial['q'].tolist(), 'initial_v_rad_s': initial['v'].tolist(),
        'initial_a_rad_s2': initial['a'].tolist(), 'state_reset_between_pieces': False,
        'existing_governor_acceleration_cap_deg_s2': list(ACCEL[:6]),
        'initial_acceleration_roundoff_allowance_rad_s2_by_axis': initial_a_roundoff.tolist(),
        'additional_jerk_hard_limit': None, 'additional_velocity_hard_limit': None,
        'continuous_acceleration_peak_deg_s2': float(np.degrees(acceleration_peak).max()),
        'continuous_acceleration_peak_rad_s2_by_axis': acceleration_peak.tolist(),
        'piecewise_jerk_peak_deg_s3': float(np.degrees(jerk_peak).max()),
        'piecewise_jerk_peak_rad_s3_by_axis': jerk_peak.tolist(),
        'continuous_velocity_peak_rad_s_by_axis': velocity_peak.tolist(),
        'desired_acceleration_peak_deg_s2_by_axis': np.degrees(desired_peak).tolist(),
        'governor_control_roundoff_projection_count':control_roundoff_projection_count,
        'governor_control_roundoff_projection_max_rad_s2':control_roundoff_projection_max,
        'roundoff_projection_changes_actual_q_v_a_state':False,
        'acceleration_convex_hull_verified': True, 'position_stationary_roots_checked': root_count,
        'position_envelope_method': 'analytic_a_zero_split_monotone_v_brent_roots_binary64',
        'terminal_stop_constraint': False, 'hardware_or_task_success_validated': False,
        'generate_ms': (time.perf_counter_ns()-started)/1e6,
    }
    return AccelerationFilteredCurve(tuple(times), np.asarray(qs), np.asarray(vs), np.asarray(accs),
        np.asarray(desired_rows), tau_s, q, v, a, lower, upper, diagnostics)
