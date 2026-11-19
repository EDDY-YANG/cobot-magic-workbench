"""Approved gripper B geometry around an unchanged frozen C1 arm bridge.

Units are rad/m and seconds. No optimizer, model, robot or filesystem is called.
B inherits old one-sided q/v/a, changes only axis 6, and searches existing knots.
It matches q/v/a at cut and at its handoff to the base's RIGHT side. Other
official linear base knots still have velocity corners. Finite acceleration/
jerk extrema below exclude those impulses.
A clips final gripper positions for comparison; its clipping corners are explicit.
"""
from dataclasses import dataclass
import json
import math
import numpy as np

NS = 1_000_000_000
GRIPPER_BOUNDS = (-0.0013, 0.1)


def _readonly(value, dtype=float):
    a = np.asarray(value, dtype=dtype)
    if not np.isfinite(a).all():
        raise ValueError("Nonfinite candidate input")
    return np.frombuffer(a.tobytes(), dtype=dtype).reshape(a.shape)


def _roots(c, lo=0., hi=1.):
    c = np.trim_zeros(np.asarray(c, float), 'b')
    if len(c) <= 1:
        return []
    result = []
    for z in np.polynomial.polynomial.polyroots(c):
        if abs(z.imag) <= 1e-8 * (1 + abs(z.real)) and lo-1e-10 <= z.real <= hi+1e-10:
            value = float(np.clip(z.real, lo, hi))
            if not any(abs(value-old) < 1e-10 for old in result):
                result.append(value)
    return sorted(result)


def _polyroots_batch(coefficients):
    """Same reversed companion matrices as NumPy polyroots, grouped by degree.

    Every polynomial is retained, including zero/linear and repeated-root cases.
    Batched eigvals removes Python dispatch overhead; no sampling or root
    approximation replaces the original analytic test.
    """
    coefficients = np.asarray(coefficients, float)
    result = [np.array([], float) for _ in coefficients]
    degree = np.max(np.where(coefficients != 0., np.arange(coefficients.shape[1]), 0), axis=1)
    for order in np.unique(degree):
        if order == 0:
            continue
        indices = np.flatnonzero(degree == order)
        c = coefficients[indices, :order+1]
        if order == 1:
            roots = (-c[:, 0] / c[:, 1])[:, None]
        else:
            companion = np.zeros((len(indices), order, order), float)
            companion[:, np.arange(1, order), np.arange(order-1)] = 1.
            companion[:, :, -1] -= c[:, :-1] / c[:, -1, None]
            roots = np.linalg.eigvals(companion[:, ::-1, ::-1])
            roots.sort(axis=1)
        for index, values in zip(indices, roots):
            result[int(index)] = values
    return result


def _filtered_roots(values, lo=0., hi=1.):
    result = []
    for z in values:
        if abs(z.imag) <= 1e-8 * (1 + abs(z.real)) and lo-1e-10 <= z.real <= hi+1e-10:
            value = float(np.clip(z.real, lo, hi))
            if not any(abs(value-old) < 1e-10 for old in result):
                result.append(value)
    return sorted(result)


def _index(times, at, side='right'):
    if side not in ('left', 'right') or not times[0] <= at <= times[-1]:
        raise ValueError("Derivative requires a covered time and left/right side")
    return int(np.clip(np.searchsorted(times, at, side=side)-1, 0, len(times)-2))


def polynomial_derivative(curve, at, order=2, side='left'):
    """One-sided regular derivative of a stored polynomial or linear curve.

    This is not a finite difference of robot feedback. At a linear base corner
    the one-sided acceleration is zero, while the velocity impulse is separate.
    """
    if order not in (1, 2, 3):
        raise ValueError("Supported derivative orders are 1, 2, 3")
    names = {1: 'velocity', 2: 'acceleration', 3: 'jerk'}
    if hasattr(curve, names[order]):
        return getattr(curve, names[order])(int(at), side)
    i = _index(curve.times, at, side)
    if not hasattr(curve, 'coefficients'):
        if order == 1:
            return curve.velocity(int(at), side)
        if not hasattr(curve, 'q'):
            raise TypeError("Cannot infer derivative of unknown curve representation")
        return np.zeros(7)
    dt = (int(curve.times[i+1])-int(curve.times[i])) / NS
    u = (at-int(curve.times[i])) / (int(curve.times[i+1])-int(curve.times[i]))
    c = np.polynomial.polynomial.polyder(curve.coefficients[i], order, axis=0)
    return np.polynomial.polynomial.polyval(u, c) / dt**order


def plan_acceleration(plan, at, side='left'):
    """Read acceleration from the same old piece selected by Plan.velocity."""
    pieces = reversed(plan.pieces) if side == 'left' else plan.pieces
    for piece in pieces:
        if (piece.start < at <= piece.end if side == 'left' else piece.start <= at < piece.end):
            return polynomial_derivative(piece.curve, int(at), 2, side)
    # Coverage endpoints have only one available side.
    if at == plan.start:
        return polynomial_derivative(plan.pieces[0].curve, int(at), 2, 'right')
    if at == plan.end:
        return polynomial_derivative(plan.pieces[-1].curve, int(at), 2, 'left')
    raise ValueError("Old plan does not cover acceleration query")


def _metrics(coefficients, times, orders=(0, 1, 2, 3), ranges=None):
    """Polynomial endpoint + stationary-root extrema, not sampled estimates."""
    coefficients = np.asarray(coefficients, float)
    dt_all = np.diff(times).astype(float) / NS
    offsets = (np.asarray(times[:-1]) - times[0]).astype(float) / NS
    result = {}
    for order in orders:
        low = (float('inf'), None, None)
        high = (-float('inf'), None, None)
        derivatives = np.polynomial.polynomial.polyder(coefficients, order, axis=1) / dt_all[:, None]**order
        stationary = np.polynomial.polynomial.polyder(derivatives, axis=1)
        roots_all = _polyroots_batch(stationary)
        for i, c in enumerate(derivatives):
            dt = dt_all[i]
            for lo, hi in (ranges[i] if ranges is not None else [(0., 1.)]):
                points = [lo, hi, *_filtered_roots(roots_all[i], lo, hi)]
                values = np.polynomial.polynomial.polyval(points, c)
                lower, upper = int(np.argmin(values)), int(np.argmax(values))
                if values[lower] < low[0]:
                    low = (float(values[lower]), float(offsets[i]+points[lower]*dt), i)
                if values[upper] > high[0]:
                    high = (float(values[upper]), float(offsets[i]+points[upper]*dt), i)
        result[('position', 'velocity', 'acceleration', 'jerk')[order]] = {
            'min': low[0], 'max': high[0], 'abs_peak': max(abs(low[0]), abs(high[0])),
            'min_after_cut_s': low[1], 'max_after_cut_s': high[1],
            'min_segment': low[2], 'max_segment': high[2]}
    return result


def _base_coefficients(legacy):
    c = np.zeros_like(legacy.coefficients)
    c[:, 0] = legacy.base_values[:-1]
    c[:, 1] = np.diff(legacy.base_values, axis=0)
    return c


def _c2_coefficients(legacy, acceleration, end_index, base_coefficients=None):
    times, base = legacy.times, legacy.base_values
    span = int(times[end_index])-int(times[0])
    duration = span/NS
    dt0 = (int(times[1])-int(times[0]))/NS
    dq = float(legacy.cut_anchor[6]-base[0, 6])
    dv = float(legacy.cut_velocity[6]-(base[1, 6]-base[0, 6])/dt0)
    da = float(acceleration)  # The official base is linear on its first side.
    a, b, c = dq, duration*dv, 0.5*duration**2*da
    left_dt = (int(times[end_index])-int(times[end_index-1]))/NS
    right_dt = (int(times[end_index+1])-int(times[end_index]))/NS
    base_left_v = (base[end_index, 6]-base[end_index-1, 6])/left_dt
    base_right_v = (base[end_index+1, 6]-base[end_index, 6])/right_dt
    # The residual terminal slope cancels this particular base slope corner.
    # Both one-sided base accelerations are zero. The residual need not have
    # zero terminal velocity; the TOTAL curve matches the restored right side.
    d = duration*(base_right_v-base_left_v)
    residual = np.array([a, b, c, -10*a-6*b-3*c-4*d, 15*a+8*b+3*c+7*d, -6*a-3*b-c-3*d])
    out = np.array(legacy.coefficients, copy=True)
    out[:, :, 6] = (_base_coefficients(legacy) if base_coefficients is None else base_coefficients)[:, :, 6]
    for i in range(end_index):
        s0 = (int(times[i])-int(times[0]))/span
        width = (int(times[i+1])-int(times[i]))/span
        for degree in range(6):
            for local in range(degree+1):
                out[i, local, 6] += residual[degree]*math.comb(degree, local)*s0**(degree-local)*width**local
    out[0, 0, 6] = legacy.cut_anchor[6]
    out[0, 1, 6] = legacy.cut_velocity[6]*dt0
    out[0, 2, 6] = 0.5*acceleration*dt0**2
    return out


def _monotonic_direction(legacy, end_index):
    # Include the inherited anchor. A constant base above/below the anchor
    # therefore requires opening/closing rather than incorrectly requiring hold.
    values = np.r_[legacy.cut_anchor[6], legacy.base_values[:end_index+1, 6]]
    delta = np.diff(values)
    tol = 128*np.finfo(float).eps*max(1., np.max(abs(values)))
    positive, negative = np.any(delta > tol), np.any(delta < -tol)
    return None if positive and negative else (1 if positive else -1 if negative else 0)


def _endpoint_contract(legacy, coefficients, acceleration, end_index, base_curve=None):
    """Check the stored polynomial, not endpoint accessors that return anchors.

    Tolerances are floating point evaluation error estimates, scaled separately
    for each derivative. They do not relax the physical position bounds.
    """
    times = legacy.times
    cut_target = [legacy.cut_anchor[6], legacy.cut_velocity[6], acceleration]
    end = int(times[end_index])
    base_curve = legacy.base if base_curve is None else base_curve
    restore_target = [base_curve.sample(end)[6], base_curve.velocity(end, 'right')[6], 0.]
    errors, tolerances = {}, {}
    for name, index, u, target in [('cut', 0, 0., cut_target),
                                  ('restore_left', end_index-1, 1., restore_target),
                                  ('restore_right', end_index, 0., restore_target)]:
        dt = (int(times[index+1])-int(times[index]))/NS
        errors[name], tolerances[name] = [], []
        for order in range(3):
            c = np.polynomial.polynomial.polyder(coefficients[index, :, 6], order)
            value = float(np.polynomial.polynomial.polyval(u, c))/dt**order
            scale = max(1., float(np.sum(abs(c))), abs(float(target[order]))*dt**order)
            tol = 128*np.finfo(float).eps*scale/dt**order
            errors[name].append(value-float(target[order]))
            tolerances[name].append(tol)
    ok = all(np.isfinite(errors[k]).all() and
             np.all(np.abs(errors[k]) <= tolerances[k]) for k in errors)
    return {'ok': bool(ok), 'errors_q_v_a': errors, 'tolerances_q_v_a': tolerances,
            'method': 'raw polynomial endpoints; 128*float64_epsilon*evaluation_scale/dt**order'}


class GripperCandidateRejected(ValueError):
    def __init__(self, reason, diagnostics):
        super().__init__(reason)
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class C2GripperCurve:
    legacy: object
    coefficients: np.ndarray
    gripper_end_ns: int
    cut_acceleration: float
    diagnostics_json: str

    def __post_init__(self):
        object.__setattr__(self, 'coefficients', _readonly(self.coefficients))

    @property
    def times(self):
        return self.legacy.times

    @property
    def base_values(self):
        return self.legacy.base_values

    @property
    def base(self):
        return self.legacy.base

    @property
    def bridge_end_ns(self):
        return self.legacy.bridge_end_ns

    @property
    def cut_anchor(self):
        return self.legacy.cut_anchor

    @property
    def cut_velocity(self):
        return self.legacy.cut_velocity

    @property
    def diagnostics(self):
        return json.loads(self.diagnostics_json)

    def sample(self, at):
        t = np.asarray(at, dtype=np.int64)
        flat = t.reshape(-1)
        result = np.array(self.legacy.sample(at), copy=True).reshape((-1, 7))
        active = flat < self.gripper_end_ns
        for row in np.flatnonzero(active):
            timestamp = int(flat[row])
            i = _index(self.times, timestamp)
            u = (timestamp-int(self.times[i]))/(int(self.times[i+1])-int(self.times[i]))
            result[row, 6] = np.polynomial.polynomial.polyval(u, self.coefficients[i, :, 6])
        if (~active).any():
            result[~active, 6] = self.base.sample(flat[~active])[:, 6]
        result[flat == self.times[0], 6] = self.cut_anchor[6]
        return result.reshape((*t.shape, 7))

    def _derivative(self, at, side, order):
        i = _index(self.times, at, side)
        out = np.array(polynomial_derivative(self.legacy, int(at), order, side), copy=True)
        dt = (int(self.times[i+1])-int(self.times[i]))/NS
        u = (at-int(self.times[i]))/(int(self.times[i+1])-int(self.times[i]))
        c = np.polynomial.polynomial.polyder(self.coefficients[i, :, 6], order)
        out[6] = np.polynomial.polynomial.polyval(u, c)/dt**order
        if at == self.times[0] and order == 1:
            out[6] = self.cut_velocity[6]
        if at == self.times[0] and order == 2:
            out[6] = self.cut_acceleration
        return out

    def velocity(self, at, side='right'):
        return self._derivative(at, side, 1)

    def acceleration(self, at, side='right'):
        return self._derivative(at, side, 2)

    def jerk(self, at, side='right'):
        return self._derivative(at, side, 3)

    def gripper_extrema(self):
        return _metrics(self.coefficients[:, :, 6], self.times)

    def extrema(self):
        lo, hi, vpeak, tol = (x.copy() for x in self.legacy.extrema())
        m = self.gripper_extrema()
        lo[6], hi[6], vpeak[6] = m['position']['min'], m['position']['max'], m['velocity']['abs_peak']
        tol[6] = 128*np.finfo(float).eps*max(1., np.max(np.sum(abs(self.coefficients[:, :, 6]), axis=1)))
        return lo, hi, vpeak, tol

    def integrity_payload(self):
        return (b'TIGER_C2_GRIPPER_CANDIDATE_B_V2_DIAGNOSTIC_MONOTONICITY\0' + self.legacy.integrity_payload()
                + np.array([self.gripper_end_ns], np.int64).tobytes()
                + np.array([self.cut_acceleration], np.float64).tobytes()
                + self.coefficients.tobytes() + self.diagnostics_json.encode('utf-8'))

    def correction_motion_extrema(self):
        """Keep six arm diagnostics; report the actual B residual for G."""
        velocity, acceleration = self.legacy.correction_motion_extrema()
        m = self.diagnostics['correction_to_official_base']
        velocity[6] = m['velocity']['abs_peak']
        acceleration[6] = m['acceleration']['abs_peak']
        return velocity, acceleration


def build_c2_gripper_candidate(legacy, cut_acceleration, known_end_ns=None,
                               bounds=GRIPPER_BOUNDS):
    """Choose the longest feasible existing 18 Hz knot, at most 22 intervals.

    `cut_acceleration` is old-reference acceleration on the LEFT of the future
    cut. It is mandatory, never estimated from current physical state or reset.
    Limits are position only; no unapproved acceleration/jerk limits are added.
    The user-approved policy records monotonicity only as a diagnostic. Both
    monotone and reversing windows can be accepted if their entire positions
    and q/v/a endpoint continuity pass. Search keeps the longest legal window.
    """
    bounds = tuple(float(x) for x in bounds)
    if len(bounds) != 2 or not np.isfinite(bounds).all() or bounds[0] >= bounds[1]:
        raise ValueError("Invalid position bounds")
    acceleration = np.asarray(cut_acceleration, float)
    if acceleration.shape == (7,):
        acceleration = acceleration[6]
    if acceleration.shape != () or not np.isfinite(acceleration):
        raise ValueError("Old left acceleration must be finite scalar or seven-vector")
    acceleration = float(acceleration)
    times = legacy.times
    if len(times) != len(legacy.base_values) or legacy.coefficients.shape != (len(times)-1, 6, 7):
        raise ValueError("Expected frozen seven-axis polynomial bridge")
    if not np.all(np.diff(times) > 0):
        raise ValueError("Non-increasing reference times")
    # label_ns rounding means neighboring 18 Hz intervals differ by 1 ns.
    if np.any(abs(np.diff(times).astype(float)-NS/18) > 1.01):
        raise ValueError("Candidate must reuse the existing 18 Hz label grid")
    known_end_ns = legacy.bridge_end_ns if known_end_ns is None else int(known_end_ns)
    max_end = min(known_end_ns, legacy.bridge_end_ns)
    ends = [i for i in range(1, min(22, len(times)-2)+1) if times[i] <= max_end]
    anchor, velocity = float(legacy.cut_anchor[6]), float(legacy.cut_velocity[6])
    common = {'schema': 'tiger-gripper-c2-knot-search-v2', 'pure_geometry_only': True,
              'bounds_m': list(bounds), 'cut_acceleration_m_s2': acceleration,
              'explicit_builder_not_default_every_packet': True,
              'caller_trigger': 'legacy gripper analytic violation only; joint admission remains separate',
              'acceleration_source': 'old reference left derivative; not measured motor acceleration',
              'terminal_contract': 'total gripper q/v/a matches restored base right side; residual terminal velocity may cancel base corner',
              'known_end_ns': known_end_ns, 'max_search_intervals': 22,
              'base_and_raw_targets_changed': False, 'bridge_output_clipped': False,
              'search': [], 'global_C1_or_C2_claimed': False,
              'monotonicity_scope': 'inherited anchor plus official base samples through candidate end',
              'candidate_policy_approved_by_user': True,
              'approved_policy': '2026-10-24 monotonicity diagnostic only; analytic position and endpoint q/v/a required',
              'physical_motion_authorized_by_this_curve': False,
              'nonmonotone_window_policy': 'diagnostic only; no monotonicity acceptance gate',
              'search_policy': 'longest first; stop at first accepted existing knot',
              'derivative_extrema_are_regular_piecewise_values_exclude_knot_impulses': True}
    if not bounds[0] <= anchor <= bounds[1]:
        raise GripperCandidateRejected("Inherited cut position outside bounds", common)
    if ((anchor == bounds[1] and (velocity > 0 or velocity == 0 and acceleration > 0)) or
            (anchor == bounds[0] and (velocity < 0 or velocity == 0 and acceleration < 0))):
        raise GripperCandidateRejected("Boundary outward inherited velocity/acceleration is infeasible; not reset", common)
    # Once the residual ends, linear interpolation has no position extrema
    # beyond its nodes. Precompute all suffix bounds once for every tested T.
    base_g = legacy.base_values[:, 6]
    suffix_low = np.minimum.accumulate(base_g[::-1])[::-1]
    suffix_high = np.maximum.accumulate(base_g[::-1])[::-1]
    min_dt = float(np.min(np.diff(times)/NS))
    base_coefficients = _base_coefficients(legacy)
    base_curve = legacy.base
    chosen = None
    for end_index in reversed(ends):
        coefficients = _c2_coefficients(legacy, acceleration, end_index, base_coefficients)
        m = _metrics(coefficients[:end_index, :, 6], times[:end_index+1], (0, 1))
        scale = max(1., np.max(np.sum(abs(coefficients[:, :, 6]), axis=1)))
        ptol = 128*np.finfo(float).eps*scale
        vtol = ptol/min_dt
        direction = _monotonic_direction(legacy, end_index)
        active = m['velocity']
        position_low = min(m['position']['min'], float(suffix_low[end_index]))
        position_high = max(m['position']['max'], float(suffix_high[end_index]))
        position_ok = position_low >= bounds[0]-ptol and position_high <= bounds[1]+ptol
        applicable = direction is not None
        monotone_ok = bool(direction == 1 and active['min'] >= -vtol or
                           direction == -1 and active['max'] <= vtol or
                           direction == 0 and active['abs_peak'] <= vtol) if applicable else None
        endpoint = _endpoint_contract(legacy, coefficients, acceleration, end_index, base_curve)
        entry = {'intervals': end_index, 'duration_s': (int(times[end_index])-int(times[0]))/NS,
                 'end_ns': int(times[end_index]), 'position_min_m': position_low,
                 'position_max_m': position_high, 'position_tolerance_m': ptol,
                 'active_velocity_min_m_s': active['min'], 'active_velocity_max_m_s': active['max'],
                 'monotonic_direction': direction, 'position_ok': bool(position_ok),
                 'monotonicity_applicable': applicable, 'monotonicity_checked': applicable,
                 'monotonicity_ok': monotone_ok, 'monotonicity_is_admission_gate': False,
                 'endpoint_continuity': endpoint,
                 'accepted': bool(position_ok and endpoint['ok'])}
        common['search'].append(entry)
        if position_ok and endpoint['ok']:
            chosen = (coefficients, end_index, entry)
            break
    common['evaluated_candidate_count'] = len(common['search'])
    if chosen is None:
        raise GripperCandidateRejected("No existing overlap knot satisfies position bounds and endpoint q/v/a continuity", common)
    coefficients, end_index, entry = chosen
    common['shorter_candidates_not_evaluated'] = [i for i in reversed(ends) if i < end_index]
    common['selected'] = dict(entry)
    common['gripper_extrema'] = _metrics(coefficients[:, :, 6], times)
    common['correction_to_official_base'] = _metrics((coefficients-base_coefficients)[:, :, 6], times)
    common['difference_from_legacy_bridge'] = _metrics((coefficients-legacy.coefficients)[:, :, 6], times)
    common['joint_coefficients_byte_identical'] = coefficients[:, :, :6].tobytes() == legacy.coefficients[:, :, :6].tobytes()
    common['cut_q_v_a_error'] = [0., 0., 0.]
    common['terminal_hold_and_horizon_unchanged'] = True
    curve = C2GripperCurve(legacy, coefficients, int(times[end_index]), acceleration, json.dumps(common, sort_keys=True))
    end = int(times[end_index])
    common['cut_q_v_a_error'] = [float(curve.sample(int(times[0]))[6]-anchor),
                                float(curve.velocity(int(times[0]))[6]-velocity),
                                float(curve.acceleration(int(times[0]))[6]-acceleration)]
    common['restore_left_residual_q_v_a'] = [
        float(np.polynomial.polynomial.polyval(1., coefficients[end_index-1, :, 6])-legacy.base.sample(end)[6]),
        float(curve.velocity(end, 'left')[6]-legacy.base.velocity(end, 'left')[6]),
        float(curve.acceleration(end, 'left')[6])]
    common['restore_right_base_q_v_a_error'] = [
        float(curve.sample(end)[6]-legacy.base.sample(end)[6]),
        float(curve.velocity(end, 'right')[6]-legacy.base.velocity(end, 'right')[6]),
        float(curve.acceleration(end, 'right')[6])]
    common['restore_left_to_right_base_q_v_a_error'] = [
        float(np.polynomial.polynomial.polyval(1., coefficients[end_index-1, :, 6])-legacy.base.sample(end)[6]),
        float(curve.velocity(end, 'left')[6]-legacy.base.velocity(end, 'right')[6]),
        float(curve.acceleration(end, 'left')[6])]
    common['restore_endpoint_residual_target_q_v_a'] = [
        0., float(legacy.base.velocity(end, 'right')[6]-legacy.base.velocity(end, 'left')[6]), 0.]
    common['restore_left_to_right_velocity_jump_m_s'] = float(curve.velocity(end, 'right')[6]-curve.velocity(end, 'left')[6])
    common['restore_right_base_exact'] = bool(np.array_equal(curve.sample(times[end_index:])[:, 6], legacy.base_values[end_index:, 6]))
    common['base_internal_velocity_corners_m_s'] = np.diff(np.diff(legacy.base_values[:, 6])/np.diff(times)*NS).tolist()
    return C2GripperCurve(legacy, coefficients, end, acceleration, json.dumps(common, sort_keys=True))


@dataclass(frozen=True)
class ClippedGripperCurve:
    """A: final-position clipping comparison, explicitly not a C1 repair."""
    legacy: object
    bounds: tuple = GRIPPER_BOUNDS

    def __post_init__(self):
        bounds = tuple(float(x) for x in self.bounds)
        if len(bounds) != 2 or not np.isfinite(bounds).all() or bounds[0] >= bounds[1]:
            raise ValueError("Invalid clipping bounds")
        object.__setattr__(self, 'bounds', bounds)

    @property
    def times(self):
        return self.legacy.times

    @property
    def base_values(self):
        return self.legacy.base_values

    def sample(self, at):
        value = np.array(self.legacy.sample(at), copy=True)
        value[..., 6] = np.clip(value[..., 6], *self.bounds)
        return value

    def _derivative(self, at, side, order):
        result = np.array(polynomial_derivative(self.legacy, int(at), order, side), copy=True)
        value = float(self.legacy.sample(int(at))[6])
        active = self.bounds[0] < value < self.bounds[1]
        if value in self.bounds:
            i = _index(self.times, at, side)
            dt = (int(self.times[i+1])-int(self.times[i]))/NS
            u = (at-int(self.times[i]))/(int(self.times[i+1])-int(self.times[i]))
            for k in range(1, 6):
                c = np.polynomial.polynomial.polyder(self.legacy.coefficients[i, :, 6], k)
                d = float(np.polynomial.polynomial.polyval(u, c))/dt**k
                if d != 0:
                    sign = np.sign(d)*(-1 if side == 'left' and k % 2 else 1)
                    active = bool(sign > 0 if value == self.bounds[0] else sign < 0)
                    break
        if not active:
            result[6] = 0.
        return result

    def velocity(self, at, side='right'):
        return self._derivative(at, side, 1)

    def acceleration(self, at, side='right'):
        return self._derivative(at, side, 2)

    def jerk(self, at, side='right'):
        return self._derivative(at, side, 3)

    def integrity_payload(self):
        return b'TIGER_OFFLINE_FINAL_CLIP_COMPARISON_A_V1\0'+self.legacy.integrity_payload()+np.array(self.bounds).tobytes()

    def gripper_extrema(self):
        coeff, times = self.legacy.coefficients[:, :, 6], self.times
        ranges, crossings = [], []
        for i, c in enumerate(coeff):
            split = [0., 1.]
            dt = (int(times[i+1])-int(times[i]))/NS
            for bound in self.bounds:
                equation = c.copy()
                equation[0] -= bound
                for u in _roots(equation):
                    split.append(u)
                    v = float(np.polynomial.polynomial.polyval(u, np.polynomial.polynomial.polyder(c)))/dt
                    after_cut = (int(times[i])-int(times[0]))/NS+u*dt
                    before, after = v, v
                    if u <= 1e-10 and i > 0:
                        dt_before = (int(times[i])-int(times[i-1]))/NS
                        before = float(np.polynomial.polynomial.polyval(1., np.polynomial.polynomial.polyder(coeff[i-1])))/dt_before
                    if u >= 1-1e-10 and i+1 < len(coeff):
                        dt_after = (int(times[i+2])-int(times[i+1]))/NS
                        after = float(coeff[i+1, 1])/dt_after
                    before_clipped = before if (before > 0 if bound == self.bounds[1] else before < 0) else 0.
                    after_clipped = after if (after < 0 if bound == self.bounds[1] else after > 0) else 0.
                    jump = after_clipped-before_clipped
                    is_internal = 1e-12 < after_cut < (int(times[-1])-int(times[0]))/NS-1e-12
                    duplicate = any(abs(after_cut-x['after_cut_s']) <= 1e-9 and bound == x['bound_m'] for x in crossings)
                    if is_internal and abs(jump) > 1e-12 and not duplicate:
                        crossings.append({'after_cut_s': after_cut, 'bound_m': bound,
                                          'legacy_velocity_m_s': v, 'clipped_side_velocity_m_s': 0.,
                                          'velocity_left_m_s': before_clipped, 'velocity_right_m_s': after_clipped,
                                          'velocity_jump_abs_m_s': abs(jump)})
            split = sorted(set(split))
            ranges.append([(a, b) for a, b in zip(split[:-1], split[1:])
                           if self.bounds[0] < np.polynomial.polynomial.polyval((a+b)/2, c) < self.bounds[1]])
        result = _metrics(coeff, times, ranges=ranges)
        legacy = _metrics(coeff, times, (0,))['position']
        for field in ('min', 'max'):
            result['position'][field] = float(np.clip(legacy[field], *self.bounds))
            result['position'][field+'_after_cut_s'] = legacy[field+'_after_cut_s']
            result['position'][field+'_segment'] = legacy[field+'_segment']
        result['position']['abs_peak'] = max(abs(result['position']['min']), abs(result['position']['max']))
        has_saturation = legacy['min'] < self.bounds[0] or legacy['max'] > self.bounds[1]
        for key in ('velocity', 'acceleration', 'jerk'):
            metric = result[key]
            if not np.isfinite(metric['min']):
                metric.update(min=0., max=0., abs_peak=0., min_after_cut_s=None, max_after_cut_s=None)
            elif has_saturation:
                metric['min'], metric['max'] = min(0., metric['min']), max(0., metric['max'])
                metric['abs_peak'] = max(abs(metric['min']), abs(metric['max']))
        result['clip_velocity_corners'] = sorted(crossings, key=lambda x: x['after_cut_s'])
        result['C1_claimed'] = False
        result['finite_global_acceleration_or_jerk_peak_claimed'] = False
        result['derivative_extrema_are_regular_values_only'] = True
        return result

    @property
    def diagnostics(self):
        return {'schema': 'tiger-offline-final-clip-A-v1', 'offline_only': True,
                'bridge_output_clipped': True, 'bounds_m': list(self.bounds),
                'gripper_extrema': self.gripper_extrema()}

    def extrema(self):
        lo, hi, vpeak, tol = (x.copy() for x in self.legacy.extrema())
        m = self.gripper_extrema()
        lo[6], hi[6], vpeak[6] = m['position']['min'], m['position']['max'], m['velocity']['abs_peak']
        return lo, hi, vpeak, tol
