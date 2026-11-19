"""Read-only FP32 shadow packets from the sole existing Hub sensor caches.

No device, socket, SDK, writer, or recorder is started here. JPEG bytes retain
the existing producer's encoding. Independent latest receipts are explicitly
not synchronized device exposures. Failed prerequisites raise; no fallback.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import time


SCHEMA = 'tiger-fp32-shadow-observation-v1'
UNITS = ['rad'] * 6 + ['m']
COMPONENTS = ('q12', 'q34', 'q56', 'gripper')
MAX_STATE_AGE_NS = 200_000_000
MAX_CAMERA_AGE_NS = 500_000_000
F32_MAX = 3.4028234663852886e38


def _require(condition, message):
    if not condition:
        raise RuntimeError('FP32 shadow observation: ' + message)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= F32_MAX


def _positive_ns(value):
    return type(value) is int and value > 0


def _storage_hooks():
    # Late imports permit device-free PC fixtures. These functions inspect only.
    from .storage import verify_usb
    from .capture_profile import contract
    return verify_usb, contract


def _storage_config(cfg):
    _require(isinstance(cfg, dict), 'recorder configuration missing')
    selected = cfg.get('usb_selection')
    _require(isinstance(selected, dict) and selected.get('schema') == 'tiger-usb-selection-v1',
             'explicit frozen USB physical identity required')
    for field in ('mountpoint', 'uuid', 'major_minor', 'source', 'device', 'physical_usb_path'):
        _require(isinstance(selected.get(field), str) and bool(selected[field]), 'USB identity missing ' + field)
    _require(cfg.get('usb') == selected['mountpoint'] and
             str(cfg.get('usb_uuid', '')).upper() == selected['uuid'].upper(), 'USB selection differs from config')
    _require(isinstance(cfg.get('remote_bundle'), str) and bool(cfg['remote_bundle']), 'runtime bundle missing')
    return (cfg['usb'], str(cfg['usb_uuid']).upper(), cfg['remote_bundle'], deepcopy(selected))


def recording_binding(hub):
    """Require the existing live recorder and a currently verified frozen USB.

    The queue counter is measured by Recorder; physical camera drops are unknown.
    No recording is started, stopped, or retargeted by this query.
    """
    recorder = getattr(hub, 'recorder', None)
    _require(recorder is not None, 'existing recorder missing')
    with recorder.lock:
        status = deepcopy(recorder.status())
        cfg = deepcopy(recorder.cfg)
        capture_contract = deepcopy(recorder.capture_contract)
        session_path = None if recorder.session is None else str(recorder.session.path)
        _require(recorder.collection_bound is True, 'recorder session is not bound')
        _require(not recorder.stop_event.is_set(), 'recorder stop is pending')
        _require(all(thread is not None and thread.is_alive()
                     for thread in (recorder.thread, recorder.sample_thread)), 'recorder worker is not alive')
    _require(status.get('active') is True and status.get('saving') is False, 'recorder is not actively collecting')
    _require('error' in status and status['error'] is None, 'recorder reports an error or lacks error status')
    overflow = status.get('overflows')
    _require(type(overflow) is int and overflow == 0, 'recorder queue overflow or missing counter')
    _require(isinstance(session_path, str) and session_path == status.get('path'), 'recorder episode path mismatch')
    _require(_storage_config(hub.cfg) == _storage_config(cfg), 'Hub USB owner differs from recorder')
    verify_usb, contract = _storage_hooks()
    _require(contract(hub.cfg) == capture_contract == contract(cfg), 'capture contract changed')
    _require('right' in capture_contract.get('required_arm_roles', []) and
             {'front', 'right'} <= set(capture_contract.get('required_camera_roles', [])),
             'recording does not cover both model cameras and right feedback')
    root = Path(verify_usb(cfg))
    _require(str(root) == cfg['usb'], 'USB verifier returned another mountpoint')
    episode = Path(session_path)
    _require(root.is_dir() and episode.is_dir(), 'verified USB or episode directory missing')
    _require(not episode.is_symlink() and episode.resolve(strict=True) == episode,
             'episode path is redirected')
    _require(episode != root and episode.is_relative_to(root), 'episode escapes selected USB')
    _require(episode.parent == Path(cfg['remote_bundle']) / 'runs', 'episode is not under the owned runtime runs')
    _require(episode.stat().st_dev == root.stat().st_dev, 'episode is on another filesystem')
    selected = cfg['usb_selection']
    return {
        'recording': {'active': True, 'episode_path': session_path, 'errors': [],
                      'queue_overflows': overflow, 'physical_frame_drop_count': None},
        'storage': {'mountpoint': cfg['usb'], 'uuid': str(cfg['usb_uuid']).upper(),
                    'major_minor': selected['major_minor']},
    }


def _domain(domain):
    _require(isinstance(domain, dict) and domain.get('identity_verified') is True and
             domain.get('clock_name') == 'monotonic_ns' and
             all(isinstance(domain.get(key), str) and bool(domain[key].strip())
                 for key in ('host_id', 'boot_id', 'session_id')), 'Hub clock identity is unverified')
    return deepcopy(domain)


def _timing(timing, domain, ready_ns, maximum_age_ns, label, kind):
    _require(isinstance(timing, dict), label + ' source timing missing')
    _require(timing.get('clock_domain') == domain and
             timing.get('clock_domain_identity_verified') is True and
             timing.get('mapping_valid') is True and timing.get('provenance_complete') is True,
             label + ' source clock/mapping/provenance unverified')
    _require(timing.get('source_timestamp_kind') == kind, label + ' unexpected source timing kind')
    source = timing.get('source_monotonic_ns_estimate')
    available = timing.get('available_monotonic_ns')
    _require(_positive_ns(source) and _positive_ns(available) and
             source <= available <= ready_ns and 0 <= ready_ns - source <= maximum_age_ns,
             label + ' source is stale, future, or misordered')
    return source, available


def build_snapshot(hub):
    """Serialize native-accepted original frames and current shared feedback."""
    recorder = getattr(hub, 'recorder', None)
    initial_binding = recording_binding(hub)
    _require(getattr(hub, 'recorder', None) is recorder, 'recorder owner changed during binding')
    episode_path = initial_binding['recording']['episode_path']
    accepted = recorder.latest_native_frames(episode_path)
    _require(isinstance(accepted, dict) and accepted.get('episode_path') == episode_path and
             _positive_ns(accepted.get('sample_wall_ns')) and _positive_ns(accepted.get('accepted_monotonic_ns')) and
             isinstance(accepted.get('frames'), dict), 'native accepted-frame provenance missing')
    domain = _domain(getattr(hub, 'clock_domain', None))
    _require('right' in hub.observers and {'front', 'right'} <= set(hub.cameras), 'required existing producers missing')
    state = hub.observers['right'].control_snapshot()
    _require(isinstance(state, dict) and state.get('feedback_fresh') is True and
             state.get('observer_running') is True and not state.get('observer_error') and
             not state.get('source_callback_error') and type(state.get('timestamp_failures')) is int and
             state['timestamp_failures'] == 0,
             'right full feedback is missing, stale, or unhealthy')
    _require(bool(state.get('status_hex')), 'right status feedback missing')
    for key in ('age_joint_s', 'age_status_s', 'age_gripper_s'):
        age = state.get(key)
        _require(_finite(age) and 0 <= age <= .2, 'right feedback freshness failed: ' + key)
    q, grip = state.get('q_deg'), state.get('grip_m')
    _require(isinstance(q, (list, tuple)) and len(q) == 6 and all(_finite(value) for value in q)
             and _finite(grip), 'actual six joint angles and linear gripper width required')
    vector = [math.radians(value) for value in q] + [grip]
    source_metadata = state.get('source_timing')
    _require(isinstance(source_metadata, dict) and all(key in source_metadata for key in COMPONENTS),
             'four actual right feedback source streams required')
    components = {key: deepcopy(source_metadata[key]) for key in COMPONENTS}
    frames = {}
    for role in ('front', 'right'):
        camera_status = hub.cameras[role].status()
        _require(isinstance(camera_status, dict) and camera_status.get('fresh') is True and
                 camera_status.get('thread_alive') is True and not camera_status.get('error'),
                 role + ' current camera producer is unhealthy')
        frame = accepted['frames'].get(role)
        _require(isinstance(frame, dict), role + ' camera cache is stale or missing')
        raw = frame.get('jpeg')
        _require(isinstance(raw, bytes) and 0 < len(raw) <= 3 * 1024 * 1024 and
                 raw.startswith(b'\xff\xd8') and raw.endswith(b'\xff\xd9'), role + ' original JPEG bytes missing')
        frame_id = frame.get('frame_id')
        _require(type(frame_id) is int and frame_id > 0, role + ' actual frame ID missing')
        _require(_positive_ns(frame.get('wall_ns')), role + ' source wall estimate missing')
        timing = deepcopy(frame.get('source_timing'))
        _require(isinstance(timing, dict) and timing.get('frame_id') == frame_id and
                 timing.get('available_monotonic_ns') == frame.get('available_monotonic_ns'),
                 role + ' cache/source metadata mismatch')
        frames[role] = {
            'jpeg_base64': base64.b64encode(raw).decode('ascii'),
            'sha256': hashlib.sha256(raw).hexdigest(), 'frame_id': frame_id,
            'wall_ns': frame['wall_ns'], 'available_monotonic_ns': frame['available_monotonic_ns'],
            'source_monotonic_ns': timing.get('source_monotonic_ns_estimate'), 'source_timing': timing,
        }
    final_binding = recording_binding(hub)
    _require(getattr(hub, 'recorder', None) is recorder, 'recorder owner changed during observation')
    _require(final_binding == initial_binding, 'recording episode or USB changed during observation')
    _require(_domain(getattr(hub, 'clock_domain', None)) == domain, 'Hub capture clock changed during observation')
    ready_ns = time.monotonic_ns()
    can_times = [_timing(timing, domain, ready_ns, MAX_STATE_AGE_NS, name,
                         'linux_so_timestampns_kernel_receipt') for name, timing in components.items()]
    for role, frame in frames.items():
        _timing(frame['source_timing'], domain, ready_ns, MAX_CAMERA_AGE_NS, role,
                'lerobot_latest_timestamp_host_software')
        _require(frame['available_monotonic_ns'] <= accepted['accepted_monotonic_ns'] <= ready_ns,
                 role + ' native queue acceptance time is future or precedes frame availability')
    oldest = min(source for source, available in can_times)
    aggregate = {
        'schema': 'tiger-can-state-aggregate-time-v1', 'source_monotonic_ns_estimate': oldest,
        'available_monotonic_ns': max(available for source, available in can_times),
        'clock_domain': deepcopy(domain), 'clock_domain_identity_verified': True,
        'mapping_valid': True, 'provenance_complete': True,
        'source_timestamp_kind': 'oldest_required_host_kernel_receipt',
        'components': components, 'hardware_generation_timestamp_available': False,
        'mapping_error_ns': None,
        'limitations': 'Oldest required host kernel receipt estimate; not device generation time. Total mapping error unverified.',
    }
    return {
        **final_binding, 'schema': SCHEMA, 'selection_mode': 'independent_latest',
        'frame_selection_provenance': {
            'mode': 'latest_native_accepted_frame', 'episode_path': episode_path,
            'sample_wall_ns': accepted['sample_wall_ns'],
            'accepted_monotonic_ns': accepted['accepted_monotonic_ns'],
            'persistence': 'accepted_into_bounded_native_queue; final flush and readback still required'},
        'hardware_synchronized': False, 'clock_domain': domain,
        'packet_ready_monotonic_ns': ready_ns, 'state_right_rad_m': vector,
        'state_units': list(UNITS), 'frames': frames,
        'right_state': {'source_monotonic_ns': oldest, 'source_timing': aggregate},
        'limitations': 'Latest native-accepted camera receipts and current feedback; no hardware exposure/generation synchronization.',
    }
