"""Shared, explicit deployment-window contract; no hardware or model imports."""
import math

MIN_WINDOW_SECONDS = 1.0
MAX_WINDOW_SECONDS = 1800.0
DEFAULT_WINDOW_SECONDS = 40.0
ACCEPTANCE_SECONDS = 30.0
CONTROL_PERIOD_NS = 20_000_000
MODEL_BUDGET_NS = 800_000_000
MODEL_FREEZE_STEPS = 12
KNOWN_STEPS = 35
REFERENCE_HZ = 18
EXECUTION_CUT_INDEX = math.ceil(MODEL_BUDGET_NS * REFERENCE_HZ / 1_000_000_000)
LOW_WATER_STEPS = KNOWN_STEPS - EXECUTION_CUT_INDEX
SETTLE_BUDGET_NS = 5_000_000_000


def timing_policy():
    return {'observation_to_commit_budget_ms': MODEL_BUDGET_NS / 1_000_000,
            'model_freeze_steps': MODEL_FREEZE_STEPS, 'known_steps': KNOWN_STEPS,
            'reference_hz': REFERENCE_HZ, 'execution_cut_index': EXECUTION_CUT_INDEX,
            'execution_cut_after_observation_ms': EXECUTION_CUT_INDEX / REFERENCE_HZ * 1000,
            'low_water_steps': LOW_WATER_STEPS,
            'publication': 'EVENT_WAKEUP_SAME_CONTROL_THREAD',
            'control_period_ms': CONTROL_PERIOD_NS / 1_000_000}


def validate_window_seconds(value=DEFAULT_WINDOW_SECONDS):
    """Reject booleans, strings, non-finite numbers and values outside 1..1800."""
    if type(value) not in (int, float):
        raise ValueError('V2 window must be a finite number, not bool/string')
    if not MIN_WINDOW_SECONDS <= value <= MAX_WINDOW_SECONDS or not math.isfinite(value):
        raise ValueError('V2 deployment window must be 1..1800 seconds')
    return float(value)


def window_deadline_ns(start_ns, seconds=DEFAULT_WINDOW_SECONDS):
    if type(start_ns) is not int or start_ns < 0:
        raise ValueError('Non-negative monotonic nanosecond start required')
    return start_ns + round(validate_window_seconds(seconds) * 1_000_000_000)


def limits_for_window(seconds=DEFAULT_WINDOW_SECONDS):
    """Counter guards only. These are not allocations or evidence capacities."""
    value = validate_window_seconds(seconds)
    return {
        'tick_count_max': math.ceil((value + 5) * 50) + 50,
        'packet_count_max': math.ceil((value + 5) * 18 / 14) + 8,
        'policy_window_s': value,
        'settle_budget_s': 5.0,
    }
