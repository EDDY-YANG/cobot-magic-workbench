"""Bounded human speed preference; never a physical-speed guarantee.

The target changes upstream s_ref and expands its baseline local rate bounds
only as needed to contain the requested rate. At 1x the previous objective and
bounds are exact. The 18 Hz output grid and downstream time map stay intact.
"""
import math
import time

import numpy as np


MIN_TARGET_SCALE = 0.5
MAX_TARGET_SCALE = 4.0
TARGET_CONTRACT = {
    'schema': 'tiger-v2-human-timeqp-target-v2-seven-gears',
    'target_scale_min': MIN_TARGET_SCALE,
    'target_scale_max': MAX_TARGET_SCALE,
    'default_target_scale': 1.0,
    'reference_hz': 18.0,
    'semantics': 'QP_S_REF_PREFERENCE_NOT_PHYSICAL_SPEED',
    'output_grid_hz': 18.0,
    'inflight_policy': 'SNAPSHOT_ON_SOLVE_ENTRY',
    'local_time_bounds_unchanged': False,
    'local_bound_policy': 'EXPAND_BASELINE_ONLY_TO_INCLUDE_TARGET',
    'baseline_local_scale_bounds': [0.9, 1.8],
    'presets': [0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 4.0],
}


def validate_target_scale(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Target scale must be a finite JSON number')
    if not MIN_TARGET_SCALE <= value <= MAX_TARGET_SCALE:
        raise ValueError('Target scale must be within [0.5, 4.0]')
    return float(value)


def apply_target_scale(optimizer, value):
    """Preserve default bounds exactly; never multiply clocks or robot limits.

    The original 0.9..1.8 local range remains for targets inside it. Slower
    targets extend only its lower edge; faster targets extend only its upper
    edge. Expanding to a 4x target never silently permits a 7.2x local rate.
    """
    scale = validate_target_scale(value)
    if not hasattr(optimizer, '_human_baseline_rate_bounds'):
        optimizer._human_baseline_rate_bounds = (optimizer.s_min, optimizer.s_max)
    low, high = optimizer._human_baseline_rate_bounds
    optimizer.s_ref = scale / optimizer.dt_ref
    optimizer.s_min = min(low, optimizer.s_ref)
    optimizer.s_max = max(high, optimizer.s_ref)
    return scale


def local_scale_stats(source_times, dt_ref=1 / 18):
    """Actual time-map rates, including endpoint padding, not CAN velocities."""
    times = np.asarray(source_times, dtype=np.float64)
    if times.ndim != 1 or len(times) < 2 or not np.isfinite(times).all():
        raise ValueError('Finite source time vector required')
    intervals = np.diff(times)
    if times[0] != 0 or np.any(intervals <= 0):
        raise ValueError('Strictly increasing source time vector required')
    rates = dt_ref / intervals
    return {
        'min': float(np.min(rates)),
        'mean': float(np.mean(rates)),
        'max': float(np.max(rates)),
        'reference_duration_ratio': float((len(times) - 1) * dt_ref / times[-1]),
        'source_duration_s': float(times[-1]),
        'interval_count': int(len(intervals)),
        'semantics': 'REFERENCE_TIME_MAP_ONLY_NOT_MEASURED_MOTION',
    }


def local_target_optimizer(*, target_scale=1.0, **cfg):
    """Explicit CPU prototype factory, using unchanged upstream OSQP.

    This is selected only when no remote TimeQP instance was requested. It is
    not a fallback for a remote error. The caller freezes one request at a time.
    NativeBootstrap can still wrap .solve to capture the real source time map.
    """
    try:
        from .temporal_rpc import load_original
    except ImportError:
        from temporal_rpc import load_original
    optimizer = load_original()(**cfg)
    optimizer.last_receipt = None
    def set_scale(value):
        optimizer.target_scale = validate_target_scale(value)
    set_scale(target_scale)
    optimizer.set_target_scale = set_scale
    original_solve = optimizer.solve
    def solve(*args):
        scale = optimizer.target_scale
        optimizer.last_receipt = None
        apply_target_scale(optimizer, scale)
        began = time.perf_counter_ns()
        result = original_solve(*args)
        optimizer.last_receipt = {
            'target_scale': scale,
            'local_scale_stats': local_scale_stats(result[1], optimizer.dt_ref),
            'server_compute_ms': (time.perf_counter_ns() - began) / 1e6,
            'transport': 'EXPLICIT_LOCAL_CPU_NO_RPC',
            'no_local_fallback': True,
            'model_calls': 0,
            'hardware_calls': 0,
        }
        return result
    optimizer.solve = solve
    return optimizer
