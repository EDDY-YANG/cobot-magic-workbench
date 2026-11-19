"""Pure seven-axis command saturation and inward SDK-grid quantization.

Inputs and outputs use rad6 + m. Joint SDK integers encode 0.001 degree and
gripper SDK integers encode 0.001 mm. This module does not read feedback, alter
reference velocities, certify stopping distance, or contact any hardware.
"""
from dataclasses import dataclass, field
import math

import numpy as np


POLICY_ID = 'seven_axis_command_saturation_sdk_grid_v1'
RAD_PER_DEG = math.pi / 180.
# The source governor checks native deg/m envelopes with 1e-10 arithmetic
# allowance. This metadata is not a widened physical command interval and is
# deliberately much smaller than half an SDK step.
NATIVE_REFERENCE_ARITHMETIC_ALLOWANCE = 1e-10


class CommandLimitError(ValueError):
    pass


def _readonly(value, shape, name):
    try:
        result = np.array(value, dtype=np.float64, copy=True)
    except (ValueError, TypeError, OverflowError) as exc:
        raise CommandLimitError(f'{name} requires finite numeric values') from exc
    if result.shape != shape or not np.isfinite(result).all():
        raise CommandLimitError(f'{name} requires finite shape {shape}')
    return np.frombuffer(result.tobytes(), dtype=np.float64).reshape(shape)


def _decode(axis, integer):
    return (integer / 1000.) * RAD_PER_DEG if axis < 6 else integer / 1_000_000.


def _grid_units(axis, value):
    return math.degrees(value) * 1000. if axis < 6 else value * 1_000_000.


def _integer_interval(axis, lower, upper):
    left, right = _grid_units(axis, lower), _grid_units(axis, upper)
    # Distinct adjacent SDK integers must remain distinguishable in binary64.
    if not all(math.isfinite(x) and abs(x) < 2**52 for x in (left, right)):
        raise CommandLimitError('position bounds exceed supported SDK-grid arithmetic')
    minimum, maximum = math.ceil(left), math.floor(right)
    # A rad/degree roundtrip can place an exact integer edge just above/below
    # itself. Resolve it by decoding the neighboring integer against the exact
    # supplied float bound, never by expanding that bound or adding tolerance.
    for _ in range(4):
        if _decode(axis, minimum) < lower:
            minimum += 1
        elif _decode(axis, minimum - 1) >= lower:
            minimum -= 1
        else:
            break
    else:
        raise CommandLimitError('lower SDK-grid edge did not stabilize')
    for _ in range(4):
        if _decode(axis, maximum) > upper:
            maximum -= 1
        elif _decode(axis, maximum + 1) <= upper:
            maximum += 1
        else:
            break
    else:
        raise CommandLimitError('upper SDK-grid edge did not stabilize')
    if minimum > maximum:
        raise CommandLimitError('position interval contains no representable SDK command')
    if not (lower <= _decode(axis, minimum) <= upper and
            lower <= _decode(axis, maximum) <= upper):
        raise CommandLimitError('SDK-grid interval is outside supplied position bounds')
    return int(minimum), int(maximum)


@dataclass(frozen=True)
class LimitedCommand:
    raw_reference_rad_m: np.ndarray
    clipped_reference_rad_m: np.ndarray
    target_rad_m: np.ndarray
    joint_mdeg: tuple
    gripper_um: int
    clipped_axes: tuple
    clipping_delta_rad_m: np.ndarray
    quantization_delta_rad_m: np.ndarray

    def __post_init__(self):
        for name in ('raw_reference_rad_m', 'clipped_reference_rad_m', 'target_rad_m',
                     'clipping_delta_rad_m', 'quantization_delta_rad_m'):
            object.__setattr__(self, name, _readonly(getattr(self, name), (7,), name))
        joints = tuple(self.joint_mdeg)
        axes = tuple(self.clipped_axes)
        if len(joints) != 6 or any(type(x) is not int for x in joints) or type(self.gripper_um) is not int:
            raise CommandLimitError('SDK command fields require six joint integers and one gripper integer')
        if axes != tuple(sorted(set(axes))) or any(type(x) is not int or not 0 <= x < 7 for x in axes):
            raise CommandLimitError('clipped_axes requires distinct sorted zero-based indices')
        object.__setattr__(self, 'joint_mdeg', joints)
        object.__setattr__(self, 'clipped_axes', axes)

    def to_dict(self):
        return {'policy': POLICY_ID,
                'raw_reference_rad_m': self.raw_reference_rad_m.tolist(),
                'clipped_reference_rad_m': self.clipped_reference_rad_m.tolist(),
                'target_rad_m': self.target_rad_m.tolist(),
                'joint_mdeg': list(self.joint_mdeg), 'gripper_um': self.gripper_um,
                'clipped_axes_zero_based': list(self.clipped_axes),
                'clipping_delta_rad_m': self.clipping_delta_rad_m.tolist(),
                'quantization_delta_rad_m': self.quantization_delta_rad_m.tolist(),
                'feedback_modified': False, 'reference_velocity_modified': False,
                'physical_tracking_or_stopping_certified': False}


@dataclass(frozen=True)
class CommandLimiter:
    bounds_rad_m: np.ndarray
    declared_bounds_rad_m: np.ndarray = field(init=False)
    reference_numerical_allowance_rad_m: np.ndarray = field(init=False)
    integer_bounds: tuple = field(init=False)

    def __post_init__(self):
        declared = _readonly(self.bounds_rad_m, (7, 2), 'bounds_rad_m')
        if np.any(declared[:, 0] >= declared[:, 1]):
            raise CommandLimitError('position bounds must be strictly ordered')
        effective = declared.copy()
        effective[6, 0] = max(-.0013, effective[6, 0])
        effective[6, 1] = min(.1, effective[6, 1])
        if effective[6, 0] >= effective[6, 1]:
            raise CommandLimitError('gripper bounds do not intersect the approved command interval')
        integer_bounds = tuple(_integer_interval(i, float(lo), float(hi))
                               for i, (lo, hi) in enumerate(effective))
        allowance = [NATIVE_REFERENCE_ARITHMETIC_ALLOWANCE * RAD_PER_DEG] * 6 + [NATIVE_REFERENCE_ARITHMETIC_ALLOWANCE]
        object.__setattr__(self, 'declared_bounds_rad_m', declared)
        object.__setattr__(self, 'bounds_rad_m', _readonly(effective, (7, 2), 'effective bounds'))
        object.__setattr__(self, 'integer_bounds', integer_bounds)
        object.__setattr__(self, 'reference_numerical_allowance_rad_m', _readonly(allowance, (7,), 'arithmetic allowance'))

    def encode(self, reference_rad_m):
        raw = _readonly(reference_rad_m, (7,), 'reference_rad_m')
        clipped = np.minimum(np.maximum(raw, self.bounds_rad_m[:, 0]), self.bounds_rad_m[:, 1])
        integers = tuple(min(hi, max(lo, round(_grid_units(i, float(value)))))
                         for i, (value, (lo, hi)) in enumerate(zip(clipped, self.integer_bounds)))
        target = np.asarray([_decode(i, integer) for i, integer in enumerate(integers)])
        if np.any(target < self.bounds_rad_m[:, 0]) or np.any(target > self.bounds_rad_m[:, 1]):
            raise CommandLimitError('decoded final SDK command is outside actual command bounds')
        axes = tuple(int(i) for i in np.flatnonzero(clipped != raw))
        with np.errstate(over='raise', invalid='raise'):
            try:
                clip_delta, quant_delta = clipped - raw, target - clipped
            except FloatingPointError as exc:
                raise CommandLimitError('command correction cannot be represented without overflow') from exc
        return LimitedCommand(raw, clipped, target, integers[:6], integers[6], axes, clip_delta, quant_delta)
