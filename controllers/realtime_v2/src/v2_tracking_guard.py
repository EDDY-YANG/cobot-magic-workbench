"""V2-only following-error action; no hardware operations or disk logging.

The original guard class defaults to stopping. The separately approved candidate
factory selects diagnostic_only for numeric 10deg/100ms exceedance. Invalid
numeric inputs and clocks remain fatal; all external safety guards are unchanged.
"""
from copy import deepcopy
import math


ACTIVE_TRACKING_ACTION = 'diagnostic_only'
TRACKING_ACTIONS = ('stop', 'diagnostic_only')


class V2TrackingError(RuntimeError):
    def __init__(self, receipt):
        self.receipt = deepcopy(receipt)
        super().__init__('V2 tracking: actual versus last host-submitted reference >10 deg for 100 ms')


class V2TrackingGuard:
    THRESHOLD_DEG = 10.0
    HOLD_S = 0.1
    ACTION = 'stop'

    def __init__(self):
        self.bad_since = None
        self.last_now = None
        self.last_check = None

    @classmethod
    def policy(cls):
        return {
            **tracking_policy(cls.ACTION),
            'schema': 'isolated-v2-tracking-policy-v1',
            'hold_ms': cls.HOLD_S * 1000,
            'comparison': 'max(abs(actual_deg-last_host_submitted_deg)) > threshold_deg',
            'timer_detail': 'one_continuous_max_axis_timer; axis_switch_does_not_reset',
            'reset_condition': 'max_error_deg <= threshold_deg',
            'stop_condition': 'continuous_exceedance_s >= hold_s' if cls.ACTION == 'stop' else None,
            'warning_condition': 'continuous_exceedance_s >= hold_s',
            'stop_action': ('stop_new_targets; last_received_target_may_finish; not_hardware_estop'
                            if cls.ACTION == 'stop' else 'none_from_numeric_tracking_exceedance'),
        }

    @staticmethod
    def _vector(value, label):
        if (not isinstance(value, (list, tuple)) or len(value) != 6 or
                not all(type(v) in (int, float) and math.isfinite(v) for v in value)):
            raise ValueError('V2 tracking requires six finite ' + label + ' angles')
        return [float(v) for v in value]

    def check(self, actual, last_sent, now):
        actual = self._vector(actual, 'actual')
        previous = None if last_sent is None else self._vector(last_sent, 'last-submitted')
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError('V2 tracking requires finite monotonic time')
        if self.last_now is not None and now < self.last_now:
            raise ValueError('V2 tracking time moved backwards')
        self.last_now = now
        errors = [0.0] * 6 if previous is None else [abs(a-b) for a, b in zip(actual, previous)]
        if not all(math.isfinite(v) for v in errors):
            raise ValueError('V2 tracking difference is nonfinite')
        error = max(errors)
        self.last_check = {'check_monotonic_s': now, 'max_error_deg': error,
                           'absolute_errors_deg': errors, 'qualified_exceedance': False,
                           'action': self.ACTION}
        if error > self.THRESHOLD_DEG:
            if self.bad_since is None:
                self.bad_since = now
            elapsed = now - self.bad_since
            if elapsed >= self.HOLD_S:
                self.last_check['qualified_exceedance'] = True
                self._persistent_exceedance({
                    'schema': 'isolated-v2-tracking-fault-v1',
                    'actual_deg': actual,
                    'last_host_submitted_deg': previous,
                    'absolute_errors_deg': errors,
                    'signed_actual_minus_last_submitted_deg': [a-b for a, b in zip(actual, previous)],
                    'dominant_joint': 'J' + str(errors.index(error) + 1),
                    'max_error_deg': error,
                    'threshold_deg': self.THRESHOLD_DEG,
                    'hold_ms': self.HOLD_S * 1000,
                    'bad_since_monotonic_s': self.bad_since,
                    'check_monotonic_s': now,
                    'continuous_exceedance_ms': elapsed * 1000,
                    'timer_semantics': self.policy()['timer_semantics'],
                    'next_group_sent': False,
                })
        else:
            self.bad_since = None
        return error


    def _persistent_exceedance(self, receipt):
        raise V2TrackingError(receipt)


class V2DiagnosticTrackingGuard(V2TrackingGuard):
    """Aggregate numeric exceedance without interrupting command submission.

    Constant-size summaries, not a warning per cycle or unbounded event list.
    Calling check does not imply that a later command passes other guards.
    """
    ACTION = 'diagnostic_only'

    def __init__(self):
        super().__init__()
        self.check_count = self.exceedance_checks = self.qualified_checks = 0
        self.warning_episodes = 0
        self.active_warning = False
        self.max_error_deg = 0.
        self.max_error_by_joint_deg = [0.] * 6
        self.max_error_joint = None
        self.max_error_monotonic_s = None
        self.first_warning_monotonic_s = self.last_warning_monotonic_s = None
        self.longest_continuous_exceedance_ms = 0.
        self.latest_warning = None

    def _persistent_exceedance(self, receipt):
        if not self.active_warning:
            self.warning_episodes += 1
            self.active_warning = True
        now = receipt['check_monotonic_s']
        if self.first_warning_monotonic_s is None:
            self.first_warning_monotonic_s = now
        self.last_warning_monotonic_s = now
        self.qualified_checks += 1
        self.longest_continuous_exceedance_ms = max(self.longest_continuous_exceedance_ms,
                                                   receipt['continuous_exceedance_ms'])
        self.latest_warning = {k: deepcopy(v) for k, v in receipt.items() if k != 'next_group_sent'}
        self.latest_warning.update(schema='isolated-v2-tracking-warning-v1',
            automatic_stop_requested=False, next_group_sent='NOT_DETERMINED_BY_TRACKING_CHECK')

    def check(self, actual, last_sent, now):
        error = super().check(actual, last_sent, now)
        self.check_count += 1
        self.exceedance_checks += error > self.THRESHOLD_DEG
        if error <= self.THRESHOLD_DEG:
            self.active_warning = False
        errors = self.last_check['absolute_errors_deg']
        self.max_error_by_joint_deg = [max(a, b) for a, b in zip(self.max_error_by_joint_deg, errors)]
        if error > self.max_error_deg:
            self.max_error_deg = error
            self.max_error_joint = 'J' + str(errors.index(error) + 1)
            self.max_error_monotonic_s = now
        return error

    def diagnostic_receipt(self):
        return {'schema': 'isolated-v2-tracking-diagnostics-v1', 'action': self.ACTION,
                'threshold_deg': self.THRESHOLD_DEG, 'hold_ms': self.HOLD_S * 1000,
                'check_count': self.check_count, 'exceedance_checks': self.exceedance_checks,
                'qualified_checks': self.qualified_checks, 'warning_episodes': self.warning_episodes,
                'active_warning_at_close': self.active_warning,
                'max_error_deg': self.max_error_deg, 'max_error_joint': self.max_error_joint,
                'max_error_by_joint_deg': list(self.max_error_by_joint_deg),
                'max_error_monotonic_s': self.max_error_monotonic_s,
                'first_warning_monotonic_s': self.first_warning_monotonic_s,
                'last_warning_monotonic_s': self.last_warning_monotonic_s,
                'longest_continuous_exceedance_ms': self.longest_continuous_exceedance_ms,
                'latest_warning': deepcopy(self.latest_warning),
                'memory': 'constant_size_aggregates_and_latest_warning',
                'automatic_stop_from_numeric_exceedance': False,
                'meaning': 'Following error warning; neither task success nor proof of safe contact'}


def tracking_guard_class(action=None):
    action = ACTIVE_TRACKING_ACTION if action is None else action
    if action not in TRACKING_ACTIONS:
        raise ValueError('Unknown V2 tracking action: ' + repr(action))
    return V2TrackingGuard if action == 'stop' else V2DiagnosticTrackingGuard


def make_tracking_guard(action=None):
    return tracking_guard_class(action)()


def tracking_policy(action=None):
    """Compact JSON identity for binding/capture; constructing hardware is unnecessary."""
    action = ACTIVE_TRACKING_ACTION if action is None else action
    if action not in TRACKING_ACTIONS:
        raise ValueError('Unknown V2 tracking action: ' + repr(action))
    return {
        'threshold_deg': V2TrackingGuard.THRESHOLD_DEG,
        'duration_ms': V2TrackingGuard.HOLD_S * 1000,
        'joints': ['J1', 'J2', 'J3', 'J4', 'J5', 'J6'],
        'timer_semantics': 'max_axis_continuous',
        'scope': 'V2_ONLY',
        'action': action,
        'automatic_stop_from_numeric_exceedance': action == 'stop',
        'invalid_numeric_or_clock_remains_fatal': True,
    }
