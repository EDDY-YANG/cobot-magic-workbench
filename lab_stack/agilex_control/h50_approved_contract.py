"""Single frozen async/RTC contract; pure validation, no I/O or hardware.

Values originate from the user-approved native-bootstrap/RTC/kernel-time and uncapped velocity JSON. The public dict may be
copied by callers; validation uses its independent private copy. Raw JSON can
be parsed with loads_unique_json before validation to reject duplicate keys.
An already-parsed dict cannot reveal duplicate source keys that were discarded.
"""
from copy import deepcopy
import json
import math

APPROVED_CONTRACT_SHA256 = '6d1e907da2d35f2307af42bba7f62266c796864e622d2752288f66b20e331dd8'
_APPROVED_JSON = """{
  "schema": "tiger-h50-async-rtc-approved-v5",
  "approved_at_user_turn": "Tiger explicitly approved removing joint and gripper software velocity caps and using H50 controller speed ratio 100 percent. Preserve position bounds, acceleration continuity and fault feedback. Upper gripper predictions map to the existing actuator range with raw outputs and interventions retained.",
  "enabled": true,
  "model_port": 18883,
  "default_step": 5000,
  "allowed_steps": [
    2000,
    5000,
    10000,
    15000,
    20000
  ],
  "precision": "float32",
  "batch_size": 1,
  "sampling_steps": 10,
  "horizon": 50,
  "native_action_dim": 32,
  "physical_action_dim": 7,
  "action_hz": 18,
  "send_hz": 50,
  "inference_hz": 1.2857142857142858,
  "replan_steps": 14,
  "rtc_enabled": true,
  "rtc_freeze_steps": 12,
  "rtc_known_steps": 35,
  "rtc_beta": 5.0,
  "reply_deadline_ms": 600,
  "queue_reserve_steps": 23,
  "reference_speed_deg_s": null,
  "reference_accel_deg_s2": 50,
  "gripper_reference_speed_m_s": null,
  "gripper_reference_accel_m_s2": 0.1,
  "tracking_error_deg": 5,
  "tracking_error_duration_s": 0.1,
  "feedback_max_age_s": 0.2,
  "camera_max_age_s": 0.25,
  "start_pose": "confirmed_current",
  "prepare_ready": false,
  "automatic_enable": false,
  "return_to_zero": false,
  "automatic_retry": false,
  "record_before_s": 2,
  "record_after_s": 2,
  "default_duration_s": 30,
  "max_duration_s": 1800,
  "trial_seed_base": 42,
  "model_lease_heartbeat_s": 0.5,
  "model_lease_ttl_s": 2,
  "acceptance_warmup_calls": 3,
  "acceptance_measured_calls": 100,
  "acceptance_p95_ms": 550,
  "acceptance_max_ms": 600,
  "whole_gpu_budget_mib": 22528,
  "task": "右臂将红色方块放入绿色桶",
  "persistence_mode": "bounded_ram_async_usb",
  "commit_timing_basis": "packet_ready_to_validated_ram_commit_ready",
  "storage_semantic_version": "tiger-h50-bounded-ram-async-usb-finalflush-v1",
  "journal_buffer_limit_bytes": 134217728,
  "journal_buffer_limit_records": 2048,
  "usb_flush_required_before_trial_complete": true,
  "usb_flush_timeout_s": 300,
  "native_training_hz": 30,
  "execution_contract_version": "tiger-h50-playback18-r14-d600-native-uncapped-velocity-v3",
  "startup_semantic_version": "tiger-h50-native-bootstrap-then-rtc-v1",
  "no_send_semantic_version": "tiger-h50-diagnostic-reference-physical-hold-v1",
  "can_recording_timestamp_semantic_version": "tiger-can-kernel-wall-ns-v1",
  "native_speed_percent": 100,
  "gripper_upper_command_policy": "GRIPPER_UPPER_COMMAND_PROJECTION_V1"
}
"""


class ApprovedConfigError(ValueError):
    pass


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ApprovedConfigError('Duplicate JSON key: ' + key)
        value[key] = item
    return value


def loads_unique_json(value):
    return json.loads(value, object_pairs_hook=_unique_pairs,
                      parse_constant=_nonfinite)


def _nonfinite(value):
    raise ApprovedConfigError('Nonfinite JSON: ' + value)


_PRIVATE_APPROVED = loads_unique_json(_APPROVED_JSON)
APPROVED_CONTRACT = deepcopy(_PRIVATE_APPROVED)
_METADATA = {'schema', 'approved_at_user_turn'}


def _same_type_and_value(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, float):
        return math.isfinite(actual) and actual == expected
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same_type_and_value(a, b) for a, b in zip(actual, expected))
    return actual == expected


def require_approved_config(cfg):
    """Validate full cfg or h50_deployment subdict; return a fresh frozen dict.

    Every control field is required and type-strict (including bool vs int).
    Metadata values are not control parameters; supplied metadata must be text.
    Extra caller metadata is ignored and absent from the returned control dict.
    No defaults are silently substituted for absent control fields.
    """
    if not isinstance(cfg, dict):
        raise ApprovedConfigError('H50 config must be an object')
    settings = cfg.get('h50_deployment', cfg)
    if not isinstance(settings, dict):
        raise ApprovedConfigError('h50_deployment must be an object')
    for key, expected in _PRIVATE_APPROVED.items():
        if key in _METADATA:
            if key in settings and not isinstance(settings[key], str):
                raise ApprovedConfigError('H50 metadata must be text: ' + key)
            continue
        if key not in settings or not _same_type_and_value(settings[key], expected):
            raise ApprovedConfigError('H50 setting differs from the approved contract: ' + key)
    return deepcopy(_PRIVATE_APPROVED)


def acceptance_semantics():
    """Fresh exact service/receipt binding for the approved execution timeline."""
    cfg = _PRIVATE_APPROVED
    return {
        'timing_basis': cfg['commit_timing_basis'],
        'storage_semantic_version': cfg['storage_semantic_version'],
        'execution_contract_version': cfg['execution_contract_version'],
        'native_training_hz': cfg['native_training_hz'],
        'execution_label_hz': cfg['action_hz'],
        'replan_steps': cfg['replan_steps'],
        'reply_deadline_ms': cfg['reply_deadline_ms'],
        'acceptance_p95_ms': cfg['acceptance_p95_ms'],
        'acceptance_max_ms': cfg['acceptance_max_ms'],
        'startup_semantic_version': cfg['startup_semantic_version'],
        'no_send_semantic_version': cfg['no_send_semantic_version'],
        'can_recording_timestamp_semantic_version': cfg['can_recording_timestamp_semantic_version'],
    }
