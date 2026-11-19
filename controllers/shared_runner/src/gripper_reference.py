"""Scalar native gripper governor, isolated from unused joint trajectories.

The scalar step below is the gripper iteration of the SHA-bound frozen
h50_async.governor_step.  It retains A=.1m/s^2 and native absolute-float arithmetic;
it never constructs dummy joint values or runs the six unused joint governors.
"""
from __future__ import annotations

from dataclasses import dataclass
import bisect
import hashlib
import math
from pathlib import Path

import numpy as np

from frozen_governor import h50_async as core
from .smoother import _readonly


SOURCE_SHA256 = '309600e1494301d8cf5e9f85b4906823b8a337fd450048b5b483fda9a8e667f7'
if hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest() != SOURCE_SHA256:
    raise RuntimeError('scalar gripper implementation requires its reviewed frozen governor source')
if core.ACCEL[6] != .1 or core.HZ != 18 or core.HORIZON != 50 or core.FREEZE != 12:
    raise RuntimeError('scalar gripper contract differs from the reviewed source')
GRIPPER_ACCEL, GRIPPER_SPEED = core.ACCEL[6], core.SPEED[6]
require = core.require


def _scalar_step(current, vel, target, dt, bounds):
    """Arithmetic-identical extraction of the frozen loop, using gripper index6."""
    require(core.finite(dt) and 0 < dt <= core.SEND_PERIOD_S+1e-8,
            'bounded reference integration tick required')
    lo, hi = bounds
    speed, accel = GRIPPER_SPEED, GRIPPER_ACCEL
    require(lo <= target <= hi, 'raw target outside limits')
    error = target-current
    wanted = math.copysign(min(speed, math.sqrt(2*accel*abs(error))), error) if error else 0.
    low = max(-speed, vel-accel*dt, 2*(lo-current)/dt-vel)
    high = min(speed, vel+accel*dt, 2*(hi-current)/dt-vel)
    disc_hi = (accel*dt)**2+8*accel*(hi-current-.5*vel*dt)
    disc_lo = (accel*dt)**2+8*accel*(current-lo+.5*vel*dt)
    high = min(high, max(0., (-accel*dt+math.sqrt(max(0., disc_hi)))/2))
    low = max(low, min(0., (accel*dt-math.sqrt(max(0., disc_lo)))/2))
    require(low <= high+1e-10, 'no feasible reference continuation')
    velocity = min(high, max(low, wanted))
    if vel*velocity < 0:
        turning_position = current-vel*vel/(2*((velocity-vel)/dt))
        if turning_position < lo:
            require(current > lo, 'negative velocity at lower boundary has no feasible continuation')
            low = max(low, math.nextafter(vel+dt*vel*vel/(2*(current-lo+5e-11)), math.inf))
        if turning_position > hi:
            require(current < hi, 'positive velocity at upper boundary has no feasible continuation')
            high = min(high, math.nextafter(vel-dt*vel*vel/(2*(hi-current+5e-11)), -math.inf))
        require(low <= high+1e-10, 'no feasible reference continuation through interior extremum')
        velocity = min(high, max(low, wanted))
    position = current+.5*(vel+velocity)*dt
    rounded_outward = (velocity > 0 and position >= hi) or (velocity < 0 and position <= lo)
    if rounded_outward and abs(vel) <= accel*dt+1e-9:
        stopped_position = current+.5*vel*dt
        if lo <= stopped_position <= hi:
            velocity = 0.; position = stopped_position
    require(lo-1e-10 <= position <= hi+1e-10, 'reference crossed actual limits')
    if vel*velocity < 0:
        actual_acceleration = (velocity-vel)/dt
        turn_time = -vel/actual_acceleration
        turn_position = current+vel*turn_time+.5*actual_acceleration*turn_time**2
        require(lo-1e-10 <= turn_position <= hi+1e-10, 'reference interior extremum crossed actual limits')
    require(abs(velocity) <= speed+1e-9 and abs(velocity-vel) <= accel*dt+1e-9,
            'reference derivative violation')
    return position, velocity


def _clock(value):
    if type(value) is not int or value <= 0:
        raise ValueError('positive integer nanosecond clock required')
    return value


@dataclass(frozen=True)
class ScalarGripperCurve:
    times_s: tuple
    q_m: np.ndarray
    v_m_s: np.ndarray
    start_ns: int
    end_ns: int
    diagnostics: dict

    def __post_init__(self):
        _clock(self.start_ns); _clock(self.end_ns)
        times = tuple(float(t) for t in self.times_s)
        if len(times) < 2 or not all(math.isfinite(t) for t in times) or any(a >= b for a, b in zip(times, times[1:])):
            raise ValueError('nonempty native scalar time sequence required')
        if times[0] != self.start_ns/core.NS or abs(times[-1]-self.end_ns/core.NS) > 1e-8:
            raise ValueError('native scalar timestamps do not match the declared horizon')
        object.__setattr__(self, 'times_s', times)
        for name in ('q_m', 'v_m_s'):
            value = np.asarray(getattr(self, name), dtype=np.float64)
            if value.shape != (len(times),) or not np.isfinite(value).all():
                raise ValueError('finite scalar gripper node arrays required')
            object.__setattr__(self, name, _readonly(value))

    @property
    def origin_ns(self):
        return self.start_ns

    def state(self, at_ns, side='left'):
        _clock(at_ns)
        if side not in ('left', 'right') or not self.start_ns <= at_ns <= self.end_ns:
            raise ValueError('scalar gripper reference exhausted or invalid side')
        at_s = at_ns/core.NS
        index = (bisect.bisect_left(self.times_s, at_s) if side == 'left'
                 else bisect.bisect_right(self.times_s, at_s)) - 1
        index = max(0, min(index, len(self.times_s)-2))
        dt = self.times_s[index+1]-self.times_s[index]
        elapsed = max(0., at_s-self.times_s[index])
        acceleration = (float(self.v_m_s[index+1])-float(self.v_m_s[index]))/dt
        q = float(self.q_m[index])+float(self.v_m_s[index])*elapsed+.5*acceleration*elapsed**2
        v = float(self.v_m_s[index])+acceleration*elapsed
        if at_ns == self.end_ns:
            q = float(self.q_m[-1]); v = float(self.v_m_s[-1])
        return {'q': q, 'v': v, 'a': acceleration, 'j': 0.}

    def sample(self, at_ns):
        return self.state(at_ns, 'right')['q']

    def envelope(self):
        # Same scalar polynomial evaluation order as state(), using native
        # absolute-float piece lengths. Include both stored nodes and left ends.
        lower, upper = float(min(self.q_m)), float(max(self.q_m))
        for i, (left, right) in enumerate(zip(self.times_s, self.times_s[1:])):
            dt = right-left
            q, v = float(self.q_m[i]), float(self.v_m_s[i])
            a = (float(self.v_m_s[i+1])-v)/dt
            end = q+v*dt+.5*a*dt**2
            lower, upper = min(lower, end), max(upper, end)
            if a:
                elapsed = -v/a
                if 0 < elapsed < dt:
                    turn = q+v*elapsed+.5*a*elapsed**2
                    lower, upper = min(lower, turn), max(upper, turn)
        return lower, upper


def generate_gripper_tail(raw, obs, cut, qg, vg, boundsg):
    """Generate only the original gripper channel from its own accepted q/v."""
    _clock(obs); _clock(cut)
    if cut != obs+core.label_offset_ns(core.FREEZE):
        raise ValueError('scalar gripper cut must preserve H50/18Hz frozen12 contract')
    values = np.asarray(raw, dtype=np.float64)
    bounds = np.asarray(boundsg, dtype=np.float64)
    if values.shape != (50, 7) or not np.isfinite(values).all():
        raise ValueError('finite real-shaped H50 required; no padding or synthetic joints')
    if bounds.shape != (2,) or not np.isfinite(bounds).all() or bounds[0] >= bounds[1]:
        raise ValueError('finite ordered gripper bounds required')
    q, v = float(qg), float(vg)
    if not math.isfinite(q) or not math.isfinite(v):
        raise ValueError('finite accepted gripper q/v required')
    actual_bounds = tuple(float(x) for x in bounds)
    project_lo, project_hi = max(-.0013, actual_bounds[0]), min(.1, actual_bounds[1])
    require(project_lo < project_hi, 'configured gripper bounds do not intersect approved range')
    rows = values[:, 6].tolist()
    adopted = [x if i < core.FREEZE else min(project_hi, max(project_lo, x)) for i, x in enumerate(rows)]
    anchor, when = obs/core.NS, cut/core.NS
    end_ns = obs+core.label_offset_ns(core.HORIZON)
    end = end_ns/core.NS
    times, positions, velocities = [when], [q], [v]
    while end-when > 1e-9:
        dt = min(.02, end-when)
        index = min(49., max(0., (when+dt-anchor)*18))
        lo, hi = int(index), min(49, int(index)+1)
        fraction = index-lo
        desired = adopted[lo]+(adopted[hi]-adopted[lo])*fraction
        q, v = _scalar_step(q, v, desired, dt, actual_bounds)
        when += dt
        times.append(when); positions.append(q); velocities.append(v)
    curve = ScalarGripperCurve(tuple(times), np.asarray(positions), np.asarray(velocities), cut, end_ns,
        {'algorithm': 'native_scalar_gripper_governor_SHA_bound_arithmetic_extraction',
         'source_sha256': SOURCE_SHA256, 'gripper_acceleration_m_s2': GRIPPER_ACCEL,
         'gripper_speed_limit_m_s': None if math.isinf(GRIPPER_SPEED) else GRIPPER_SPEED,
         'joint_governor_calls': 0, 'synthetic_joint_values_created': False,
         'position_projection_count': sum(a != b for a, b in zip(adopted, rows)),
         'jerk_bound_claimed': False, 'acceleration_continuity_claimed': False,
         'absolute_float_native_sampling_preserved': True, 'initial_state_reset': False})
    lower, upper = curve.envelope()
    require(actual_bounds[0]-1e-10 <= lower and upper <= actual_bounds[1]+1e-10,
            'scalar native gripper envelope exceeds existing bounds')
    return curve
