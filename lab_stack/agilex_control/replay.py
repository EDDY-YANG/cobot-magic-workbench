"""Offline, bounded replay of archived observations; no robot/device imports.

The archive contains lossy JPEGs, not the original inference RGB. Replaying
real recorded observations is an open-loop comparison, never a new episode or
proof of recovery. RTC prefixes must come from a recorded accepted plan.
"""
from __future__ import annotations

import bisect
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics
import struct
import sys
import time


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def records(path, max_payload=8 * 1024 * 1024):
    """Yield validated chunk record locations and bytes; reject truncated tails."""
    with Path(path).open('rb') as stream:
        while True:
            header = stream.read(12)
            if not header:
                return
            require(len(header) == 12, 'Truncated chunk header: ' + str(path))
            stamp, length = struct.unpack('<QI', header)
            require(stamp > 0 and 0 < length <= max_payload, 'Invalid chunk record length/timestamp')
            offset = stream.tell()
            payload = stream.read(length)
            require(len(payload) == length, 'Truncated chunk payload: ' + str(path))
            yield stamp, offset, payload


def archive_states(episode):
    receipt_path = Path(episode) / 'operation.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8-sig'))
    require(receipt.get('ended_at') and receipt.get('format') == 'tiger-native-sensors-v1',
            'Only finalized native sensor recordings are accepted')
    rows = []
    for path in sorted(Path(episode).glob('state-*.bin')):
        for stamp, offset, payload in records(path):
            value = json.loads(payload)
            require(value.get('sample_wall_ns') == stamp, 'State header/body timestamps differ')
            value['_source'] = {'path': str(path.resolve()), 'offset': offset, 'length': len(payload),
                                'sha256': hashlib.sha256(payload).hexdigest()}
            rows.append(value)
    require(rows and all(a['sample_wall_ns'] < b['sample_wall_ns'] for a, b in zip(rows, rows[1:])),
            'State timestamps must be strictly increasing')
    return receipt, rows


def clock_mapping(rows, same_clock_verified):
    """Use recorded wall/perf pairs, only when the recording host clock is known.

    CPython on the recording Linux host must identify both clocks as
    CLOCK_MONOTONIC. The caller passes evidence, never assumes a Windows clock.
    """
    require(same_clock_verified, 'Recording-host perf_counter/monotonic equivalence unverified')
    offsets = []
    seen = set()
    for row in rows:
        stage = row.get('sensors', {}).get('stage_observer', {})
        for pair in stage.get('source_timestamps', {}).values():
            wall, perf = pair.get('wall_ns'), pair.get('stamp_perf')
            if isinstance(wall, int) and isinstance(perf, (float, int)) and math.isfinite(perf):
                key = (wall, perf)
                if key not in seen:
                    seen.add(key)
                    offsets.append(wall / 1e9 - perf)
    require(len(offsets) >= 4, 'Insufficient recorded wall/perf timestamp pairs')
    spread = max(offsets) - min(offsets)
    require(spread <= .01, 'Recorded wall/perf mapping spread exceeds 10 ms; no RTC alignment')
    return {'wall_minus_monotonic_s': statistics.median(offsets), 'spread_s': spread,
            'pair_count': len(offsets), 'basis': 'recorded stage camera wall_ns/stamp_perf pairs; verified recording-host CLOCK_MONOTONIC'}


def load_reference_policy(path, expected_sha):
    require(len(expected_sha) == 64 and sha256(path) == expected_sha.lower(),
            'Historical reference policy hash mismatch')
    name = '_offline_recorded_reference_' + expected_sha[:12]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def reconstruct_plans(report, cfg, reference):
    """Rebuild only previously accepted software references, with report checks.

    Never infer a future command from actual feedback. The report must contain
    the bootstrap and every subsequent committed block; any gap rejects RTC.
    """
    events = report['events']
    first = next(e for e in events if 'first_actions' in e)
    bounds = next(e['bounds_deg'] for e in events if e['event'] == 'EXPLICIT_COMMAND_BOUNDS')
    clock = report['policy_clock']
    commits = [e for e in report['recent_RTC_events'] if e['event'] == 'rtc_reply_committed']
    require(len(commits) == report['policy_metrics']['rtc_committed_count'],
            'Historical committed blocks truncated; RTC prefix unavailable')
    preparation = next(e for e in events if e['event'] == 'POLICY_CLOCK_PREPARATION_NO_TARGETS')
    require(preparation['action_time_hz'] == cfg['action_time_hz'] and
            preparation['control_send_hz'] == cfg['policy_send_hz'], 'Historical execution clock differs')
    common = dict(execution_hz=cfg['action_time_hz'], send_hz=cfg['policy_send_hz'],
                  speed_deg_s=cfg['policy_speed_deg_s'], accel_deg_s2=cfg['policy_accel_deg_s2'],
                  gripper_bounds_m=cfg['gripper_bounds_m'])
    anchor = report['policy_startup']['final_actual_pose_check']['actual_anchor_deg_and_grip_m']
    q = [math.radians(x) for x in anchor[:6]] + [anchor[6]]
    bounds_rad = [[math.radians(v) for v in pair] for pair in bounds]
    relative, _ = reference.ContinuousPlan.build(first['first_actions'], 0., 0., q, bounds_rad, **common)
    start = clock['first_reference_origin_monotonic']
    nodes = [reference.ReferenceNode(first['actual_observation_monotonic'], relative.nodes[0].q, (0.,) * 6)]
    nodes += [reference.ReferenceNode(start + n.t, n.q, n.v) for n in relative.nodes]
    plan = reference.ContinuousPlan(nodes, cfg['action_time_hz'], cfg['policy_send_hz'])
    require(abs(plan.end - clock['first_reference_end_monotonic']) < 1e-6, 'Bootstrap reference mismatch')
    history = [(start, plan)]
    for event in commits:
        cut = event['committed_until']
        observation = cut - cfg['rtc_delay_steps'] / cfg['action_time_hz']
        prefix = plan.rtc_prefix(observation, cfg['rtc_delay_steps'], cfg['rtc_attention_extra'])
        require(prefix['previous_valid_steps'] == event['valid_prefix'] and
                prefix['execution_horizon_steps'] - prefix['delay_steps'] == event['effective_attention_steps'],
                'Reconstructed historical prefix coverage differs')
        require(abs((plan.end - cut) - event['queue_coverage_margin_s']) < 1e-6,
                'Historical reference timeline differs')
        anchor, velocity = plan.sample(cut)
        plan, peak = reference.ContinuousPlan.build(event['model_actions'], observation, cut, anchor,
                                                   bounds_rad, v=velocity, old=plan, **common)
        require(abs(peak - event['peak_shaping_deg']) < .001,
                'Historical shaper mismatch; parameters or code differ')
        installed = observation + event['source_to_install_s']
        require(history[-1][0] < installed < cut, 'Historical install ordering invalid')
        history.append((installed, plan))
    return history


def accepted_prefix(history, observation_monotonic, cfg):
    index = bisect.bisect_right([when for when, _ in history], observation_monotonic) - 1
    require(index >= 0, 'No accepted historical plan at this observation')
    return history[index][1].rtc_prefix(observation_monotonic, cfg['rtc_delay_steps'], cfg['rtc_attention_extra'])


def choose_states(rows, offsets_s, origin_wall_ns):
    require(1 <= len(offsets_s) <= 24 and all(math.isfinite(x) and 0 <= x <= 300 for x in offsets_s),
            'Choose 1..24 finite offsets within 0..300 s')
    stamps = [row['sample_wall_ns'] for row in rows]
    selected = []
    for offset in offsets_s:
        target = origin_wall_ns + round(offset * 1e9)
        index = bisect.bisect_left(stamps, target)
        candidates = [i for i in (index - 1, index) if 0 <= i < len(rows)]
        index = min(candidates, key=lambda i: abs(stamps[i] - target))
        require(abs(stamps[index] - target) <= 100_000_000, 'No recorded state within 100 ms of requested offset')
        selected.append((offset, rows[index]))
    require(len({row['sample_wall_ns'] for _, row in selected}) == len(selected), 'Offsets select duplicate states')
    return selected


def state_vector(row):
    arm = row['sensors']['arms']['right']
    q, grip = arm.get('q_deg'), arm.get('grip_m')
    require(isinstance(q, list) and len(q) == 6 and
            all(type(x) in (int, float) and math.isfinite(x) for x in q + [grip]),
            'Recorded real q/gripper invalid')
    require(arm.get('feedback_fresh') is True and all(
        isinstance(arm.get(k), (int, float)) and 0 <= arm[k] <= .2
        for k in ('age_joint_s', 'age_gripper_s')), 'Recorded feedback stale')
    return [math.radians(x) for x in q] + [grip]


def find_frames(episode, selections):
    found = {}
    for role in ('front', 'right'):
        wanted = {row['camera_sample_ns'][role] for _, row in selections}
        for path in sorted(Path(episode).glob(role + '-*.bin')):
            # Seek over unrelated JPEG bytes to avoid rereading hundreds of MB
            # from USB just to select a handful of actual archived frames.
            size = path.stat().st_size
            with path.open('rb') as stream:
                while wanted:
                    header = stream.read(12)
                    if not header:
                        break
                    require(len(header) == 12, 'Truncated image chunk header')
                    stamp, length = struct.unpack('<QI', header)
                    offset = stream.tell()
                    require(stamp > 0 and 0 < length <= 8 * 1024 * 1024 and offset + length <= size,
                            'Truncated/invalid image chunk payload')
                    if stamp in wanted:
                        payload = stream.read(length)
                        found[(role, stamp)] = {'path': str(path.resolve()), 'offset': offset,
                            'length': length, 'sha256': hashlib.sha256(payload).hexdigest(), 'wall_ns': stamp}
                        wanted.remove(stamp)
                    else:
                        stream.seek(length, 1)
            if not wanted:
                break
        require(not wanted, 'Referenced original camera frames missing: ' + role)
    return found


def read_blob(ref):
    with Path(ref['path']).open('rb') as stream:
        stream.seek(ref['offset'])
        payload = stream.read(ref['length'])
    require(len(payload) == ref['length'] and hashlib.sha256(payload).hexdigest() == ref['sha256'],
            'Archived source bytes changed')
    return payload


def prepare_image(payload):
    """Exactly the production/training aspect-preserving OpenCV transform.

    Kept hardware-free: do not import hub (which imports the CAN observer).
    """
    import cv2
    import numpy as np
    decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    require(decoded is not None and decoded.shape == (480, 640, 3), 'Archive image must be original 640x480 JPEG')
    rgb = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (224, 168), interpolation=cv2.INTER_AREA)
    result = np.zeros((224, 224, 3), dtype=np.uint8)
    result[28:196] = resized
    return result


def prepare(episode, deployment, offsets_s, *, cfg=None, reference=None, same_clock_verified=False):
    receipt, rows = archive_states(episode)
    report = json.loads(Path(deployment).read_text(encoding='utf-8-sig'))
    require(Path(report['recording_closed']['path']).name == Path(episode).name,
            'Deployment report does not name this recording')
    mapping = history = None
    rtc_reason = None
    try:
        mapping = clock_mapping(rows, same_clock_verified)
        require(reference is not None and cfg is not None, 'No authenticated historical reference/config supplied')
        history = reconstruct_plans(report, cfg, reference)
    except (ValueError, RuntimeError, KeyError, StopIteration) as exc:
        rtc_reason = str(exc) or type(exc).__name__
    # Without an authenticated clock, offsets refer to recording start, explicitly.
    origin = round((report['policy_clock']['first_target_at_monotonic'] + mapping['wall_minus_monotonic_s']) * 1e9) if mapping else rows[0]['sample_wall_ns']
    selections = choose_states(rows, offsets_s, origin)
    # At arbitrary wall times a previous plan can be awaiting renewal and have
    # less than 8 steps left. Select a nearby REAL state with sufficient known
    # queue coverage; never extend/repeat that plan to manufacture a prefix.
    if history is not None:
        covered = []
        used = set()
        for offset, nearest in selections:
            target = origin + round(offset * 1e9)
            candidates = sorted((row for row in rows if abs(row['sample_wall_ns'] - target) <= 250_000_000),
                                key=lambda row: abs(row['sample_wall_ns'] - target))
            chosen = nearest
            for candidate in candidates:
                if candidate['sample_wall_ns'] in used:
                    continue
                try:
                    state_vector(candidate)
                    accepted_prefix(history, candidate['sample_wall_ns'] / 1e9 - mapping['wall_minus_monotonic_s'], cfg)
                    chosen = candidate
                    break
                except (ValueError, RuntimeError):
                    continue
            used.add(chosen['sample_wall_ns'])
            covered.append((offset, chosen))
        selections = covered
    frames = find_frames(episode, selections)
    samples = []
    for index, (offset, row) in enumerate(selections):
        stamp = row['sample_wall_ns']
        mono = stamp / 1e9 - mapping['wall_minus_monotonic_s'] if mapping else stamp / 1e9
        prefix, reason = None, rtc_reason
        if history is not None:
            try:
                prefix = accepted_prefix(history, mono, cfg)
            except (ValueError, RuntimeError) as exc:
                reason = str(exc)
        frame_refs = {role: frames[(role, row['camera_sample_ns'][role])] for role in ('front', 'right')}
        require(all(0 <= (stamp - ref['wall_ns']) / 1e9 <= .5 for ref in frame_refs.values()), 'Recorded camera too old')
        images = {role: prepare_image(read_blob(ref)) for role, ref in frame_refs.items()}
        samples.append({'id': 'sample-%02d' % index, 'requested_offset_s': offset,
            'actual_offset_s': (stamp - origin) / 1e9, 'state_source': row['_source'],
            'sample_wall_ns': stamp, 'observation_monotonic': mono,
            'observation_clock_basis': 'recorded wall mapped to monotonic' if mapping else 'wall seconds as offline transport label; RTC disabled',
            'state_right_rad_m': state_vector(row), 'source_frames': frame_refs,
            'input_rgb224_sha256': {role: hashlib.sha256(im.tobytes()).hexdigest() for role, im in images.items()},
            'rtc_prefix': prefix, 'rtc_unavailable_reason': reason,
            'rtc_prefix_basis': 'reconstructed previously installed accepted reference; no future real feedback' if prefix else None})
    return {'schema': 'tiger-recorded-shadow-ab-v1', 'robot_command_sent': False,
        'episode': str(Path(episode).resolve()), 'episode_receipt_sha256': sha256(Path(episode) / 'operation.json'),
        'deployment_report': str(Path(deployment).resolve()), 'deployment_report_sha256': sha256(deployment),
        'recording_status': receipt.get('status'), 'clock_mapping': mapping,
        'sample_selection': 'nearest recorded state; when RTC history exists prefer actual queue-covered states within 250 ms; actual offsets reported',
        'offset_origin': 'first policy target' if mapping else 'recording start; clock mapping unavailable',
        'execution_hz': cfg['action_time_hz'] if cfg else 18, 'samples': samples,
        'limitation': 'Open-loop paired archived observations. JPEG archive differs from live RGB. Reconstructed prefix is historical accepted command, never an alternate rollout. Cannot prove grasp or recovery.',
        'gripper_guidance_ablation': 'NOT_IMPLEMENTED; pinned server supports temporal weights only'}


def run_pairs(manifest, client, sampling_steps, variants=('rtc', 'no-rtc'), seed=42, max_seconds=120):
    require(sampling_steps in (5, 10), 'Only explicit 5/10 sampling steps')
    require(variants and len(variants) == len(set(variants)) and set(variants) <= {'rtc', 'no-rtc'}, 'Unknown/duplicate variants')
    require(type(seed) is int and 0 <= seed < 2**32 and 0 < max_seconds <= 600, 'Invalid seed/time bound')
    require(1 <= len(manifest['samples']) <= 24, 'Sample bound exceeded')
    health = client.health()
    require(health.get('rtc', {}).get('num_steps') == sampling_steps,
            'Service sampler mismatch; start a separate verified service with requested steps')
    require(health.get('robot_command_sent') is False and not health.get('busy') and not health.get('fault'),
            'Model service unavailable or busy')
    started = time.monotonic()
    results = []
    failure = None
    try:
        for sample in manifest['samples']:
            images = {role: prepare_image(read_blob(ref)) for role, ref in sample['source_frames'].items()}
            for role, im in images.items():
                require(hashlib.sha256(im.tobytes()).hexdigest() == sample['input_rgb224_sha256'][role], 'Preprocessing output changed')
            # Recheck actual state bytes; the manifest cannot silently substitute q.
            state = json.loads(read_blob(sample['state_source']))
            require(state_vector(state) == sample['state_right_rad_m'], 'Prepared state differs from original real feedback')
            for variant in variants:
                item = {'sample_id': sample['id'], 'sampling_steps': sampling_steps, 'variant': variant, 'seed': seed,
                        'source_timestamps': {role: ref['wall_ns'] for role, ref in sample['source_frames'].items()},
                        'state_right_rad_m': sample['state_right_rad_m']}
                item['input_rgb224_sha256'] = sample['input_rgb224_sha256']
                if variant == 'rtc' and sample['rtc_prefix'] is None:
                    results.append({**item, 'status': 'SKIPPED', 'reason': sample['rtc_unavailable_reason']})
                    continue
                if time.monotonic() - started >= max_seconds:
                    results.append({**item, 'status': 'SKIPPED', 'reason': 'bounded replay duration reached'})
                    continue
                snapshot = {'state_right_rad_m': sample['state_right_rad_m'], 'source_timestamps': item['source_timestamps'],
                            'observation_monotonic': sample['observation_monotonic'], 'seed': seed,
                            'front_rgb224': images['front'], 'right_rgb224': images['right']}
                result, latency = client.infer(snapshot, sample['rtc_prefix'] if variant == 'rtc' else None)
                require(result.get('rtc', {}).get('num_steps') == sampling_steps, 'Response sampler differs')
                results.append({**item, 'status': 'COMPUTED', 'rpc_s': latency, 'actions': result['actions'],
                    'rtc': result['rtc'], 'model_identity': {key: result.get(key) for key in
                        ('model_id', 'config_sha256', 'server_code_sha256', 'server_instance_id')},
                    'timing': result.get('timing'), 'server_total_ms': result.get('server_total_ms'),
                    'physical_outcome': 'NOT_EVALUATED_OPEN_LOOP'})
    except Exception as exc:
        failure = repr(exc)
    return {'schema': 'tiger-recorded-shadow-ab-result-v1', 'status': 'FAILED' if failure else 'COMPUTED_WITH_POSSIBLE_SKIPS',
            'failure': failure, 'robot_command_sent': False, 'model_health': health,
            'seed': seed, 'elapsed_s': time.monotonic() - started, 'time_limit_s': max_seconds,
            'duration_bound': 'no new RPC after deadline; one in-flight request may finish within client timeout',
            'limitation': manifest['limitation'], 'samples': results}
