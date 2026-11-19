"""Offline, stdlib-only summary of saved V2 NO_SEND evidence; never opens I/O.

Missing measurements remain null. An unsent diagnostic command is not an actual
send or tracking measurement. This program cannot grant motion acceptance.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.evidence_reader import iter_evidence_records


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def get(value, *keys):
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and isinstance(key, int) and 0 <= key < len(value):
            value = value[key]
        else:
            return None
    return value


def scaled(value, scale):
    return value * scale if number(value) else None


def delta_ms(end, start):
    return (end-start)/1e6 if number(end) and number(start) else None


def stats(values):
    ordered = sorted(v for v in values if number(v))
    if not ordered:
        return None
    def percentile(p):
        at = (len(ordered)-1)*p/100
        lo = math.floor(at)
        hi = math.ceil(at)
        return ordered[lo] + (ordered[hi]-ordered[lo])*(at-lo)
    return {'n': len(ordered), 'min': ordered[0], 'p50': percentile(50),
            'p95': percentile(95), 'p99': percentile(99), 'max': ordered[-1]}


def extent(values):
    valid = [v for v in values if number(v)]
    return None if not valid else {'n': len(valid), 'min': min(valid), 'max': max(valid)}


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items()
                if k.lower() not in ('token', 'lease_token', 'service_nonce', 'pending_request_token')}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


def analyze(run_dir):
    run_dir = Path(run_dir).resolve()
    loaded, sources, missing = {}, {}, []
    for name in ('operation.json', 'continuous-summary.json',
                 'continuous-packets.json', 'continuous-ticks.json'):
        path = run_dir / name
        if not path.is_file():
            loaded[name] = None
            sources[name] = None
            missing.append(name)
            continue
        raw = path.read_bytes()
        loaded[name] = json.loads(raw.decode('utf-8-sig'))
        sources[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    operation = loaded['operation.json'] or {}
    summary = loaded['continuous-summary.json']
    if summary is None:
        summary = get(operation, 'continuous') or {}
    packets_in = loaded['continuous-packets.json']
    ticks_in = loaded['continuous-ticks.json']
    packets = list(iter_evidence_records(run_dir/'continuous-packets.json')) if packets_in is not None else []
    ticks = list(iter_evidence_records(run_dir/'continuous-ticks.json')) if ticks_in is not None else []
    ticks = [t for t in ticks if isinstance(t, dict)]
    packets = [p for p in packets if isinstance(p, dict)]

    accepted = [p for p in packets if get(p, 'queue_acceptance', 'accepted') is True]
    packet_rows, b_rows = [], []
    raw_all, raw_accepted, analytic_low, analytic_high = [], [], [], []
    diagnosed = 0
    diagnosed_accepted = 0
    analytic_count = 0
    for sequence, packet in enumerate(packets):
        actions = get(packet, 'response', 'actions')
        raw = [scaled(get(row, 6), 1000) for row in actions] if isinstance(actions, list) else []
        raw_all.extend(raw)
        acceptance = get(packet, 'queue_acceptance') or {}
        accepted_here = acceptance.get('accepted') is True
        if accepted_here:
            raw_accepted.extend(raw)
        temporal = acceptance.get('temporal') or {}
        low = scaled(get(temporal, 'position_min_rad_m', 6), 1000)
        high = scaled(get(temporal, 'position_max_rad_m', 6), 1000)
        if accepted_here and low is not None and high is not None:
            analytic_low.append(low)
            analytic_high.append(high)
            analytic_count += 1
        observation_ns = packet.get('observation_anchor_ns')
        commit_ns = acceptance.get('commit_monotonic_ns')
        prepare_ns = acceptance.get('prepare_finished_ns')
        model_ns = acceptance.get('model_ready_ns')
        row = {
            'packet_sequence': sequence,
            'request_index': acceptance.get('request_index', get(packet, 'rtc', 'request_index')),
            'accepted': accepted_here if 'queue_acceptance' in packet else None,
            'discarded_after_stop': packet.get('discarded_after_stop'),
            'cancelled_result': packet.get('cancelled_result'),
            'raw_gripper_mm': extent(raw),
            'accepted_analytic_gripper_min_mm': low,
            'accepted_analytic_gripper_max_mm': high,
            'observation_to_commit_ms': delta_ms(commit_ns, observation_ns),
            'model_ready_to_prepare_finished_ms': delta_ms(prepare_ns, model_ns),
            'prepare_finished_to_commit_ms': delta_ms(commit_ns, prepare_ns),
            'temporal_prepare_ms': temporal.get('prepare_ms'),
            'model_round_trip_ms': packet.get('round_trip_ms'),
            'pipeline_error': packet.get('pipeline_error'),
            'timeqp_preparation_error': packet.get('timeqp_preparation_error'),
        }
        packet_rows.append(row)
        transition = temporal.get('gripper_transition')
        transition_source = 'queue_acceptance.temporal'
        if not isinstance(transition, dict):
            transition = get(packet, 'prepared_reference_diagnostics', 'gripper_transition')
            transition_source = 'prepared_reference_diagnostics'
        if not isinstance(transition, dict):
            transition = get(packet, 'soft_guided_splice_rejection', 'gripper_transition')
            transition_source = 'soft_guided_splice_rejection'
        if isinstance(transition, dict) and type(transition.get('triggered')) is bool:
            diagnosed += 1
            diagnosed_accepted += int(accepted_here)
            if transition['triggered']:
                candidate = transition.get('candidate') or {}
                b_rows.append({
                    'packet_sequence': sequence, 'request_index': row['request_index'],
                    'accepted_into_queue': accepted_here, 'diagnostic_source': transition_source,
                    'status': transition.get('status'), 'strategy': transition.get('strategy'),
                    'duration_s': get(candidate, 'selected', 'duration_s'),
                    'intervals': get(candidate, 'selected', 'intervals'),
                    'searched_candidates': candidate.get('evaluated_candidate_count'),
                    'legacy_min_mm': scaled(get(transition, 'legacy_extrema', 'position_min_rad_m', 6), 1000),
                    'legacy_peak_mm': scaled(get(transition, 'legacy_extrema', 'position_max_rad_m', 6), 1000),
                    'repaired_min_mm': scaled(get(candidate, 'gripper_extrema', 'position', 'min'), 1000),
                    'repaired_peak_mm': scaled(get(candidate, 'gripper_extrema', 'position', 'max'), 1000),
                    'position_tolerance_mm': scaled(get(candidate, 'selected', 'position_tolerance_m'), 1000),
                    'cut_q_v_a_error': candidate.get('cut_q_v_a_error'),
                    'restore_left_to_right_base_q_v_a_error': candidate.get('restore_left_to_right_base_q_v_a_error'),
                    'joint_coefficients_byte_identical': candidate.get('joint_coefficients_byte_identical'),
                    'bridge_output_clipped': candidate.get('bridge_output_clipped'),
                })

    now = [get(t, 'now_ns') for t in ticks]
    valid_now = [v for v in now if number(v)]
    intervals = [delta_ms(b, a) for a, b in zip(now, now[1:])]
    statuses = [get(t, 'mpc', 'status') for t in ticks if get(t, 'mpc', 'status') is not None]
    resets = [t.get('controller_initializations') for t in ticks if number(t.get('controller_initializations'))]
    stop = summary.get('stop')
    normal_complete = (summary.get('status') == 'REAL_IO_NO_SEND_WINDOW_COMPLETE'
                       and summary.get('error') is None) if 'status' in summary else None
    analytic_range = None if not analytic_count else {
        'min': min(analytic_low), 'max': max(analytic_high),
        'accepted_packets_with_analytic_extrema': analytic_count,
        'accepted_packets_without_analytic_extrema': len(accepted)-analytic_count,
        'scope': 'Saved accepted analytic reference extrema only; missing native extrema are not inferred',
    }
    return public({
        'schema': 'tiger-v2-nosend-offline-analysis-v1',
        'evidence_scope': 'SAVED_NO_SEND_RECEIPTS_ONLY_NOT_PHYSICAL_TRACKING_OR_MOTION_ADMISSION',
        'run_dir': str(run_dir), 'source_files': sources, 'missing_files': missing,
        'operation_status': operation.get('status'), 'continuous_status': summary.get('status'),
        'error': summary.get('error', operation.get('error')),
        'model': operation.get('model'),
        'timing': {
            'tick_count': len(ticks) if ticks_in is not None else None,
            'reported_tick_count': summary.get('tick_count'),
            'first_tick_ns': valid_now[0] if valid_now else None,
            'last_tick_ns': valid_now[-1] if valid_now else None,
            'observed_first_to_last_tick_s': (valid_now[-1]-valid_now[0])/1e9 if len(valid_now) >= 2 else None,
            'tick_span_is_not_requested_window_duration': True,
            'cycle_compute_ms': stats(t.get('cycle_compute_ms') for t in ticks),
            'tick_interval_ms': stats(intervals),
            'scheduler_lateness_ms': stats(t.get('scheduler_lateness_ms') for t in ticks),
            'feedback_source_age_ms': stats(delta_ms(t.get('now_ns'), t.get('feedback_ns')) for t in ticks),
            'accepted_observation_to_commit_ms': stats(p['observation_to_commit_ms'] for p in packet_rows if p['accepted']),
            'accepted_model_ready_to_prepare_finished_ms': stats(p['model_ready_to_prepare_finished_ms'] for p in packet_rows if p['accepted']),
            'accepted_prepare_finished_to_commit_ms': stats(p['prepare_finished_to_commit_ms'] for p in packet_rows if p['accepted']),
            'accepted_temporal_prepare_ms': stats(p['temporal_prepare_ms'] for p in packet_rows if p['accepted']),
        },
        'packets': {'recorded': len(packets) if packets_in is not None else None,
                    'accepted': len(accepted) if packets_in is not None else None,
                    'reported_accepted': summary.get('accepted_packets'), 'rows': packet_rows},
        'gripper': {
            'units': 'millimetres unless field says otherwise',
            'raw_model_all_recorded_mm': extent(raw_all),
            'raw_model_accepted_packets_mm': extent(raw_accepted),
            'accepted_analytic_reference_mm': analytic_range,
            'unsent_encoded_gripper_um': extent(t.get('gripper_um') for t in ticks),
            'unsent_sampled_reference_mm': extent(scaled(get(t, 'reference_rad_m', 6), 1000) for t in ticks),
            'B_diagnosed_recorded_packet_count': diagnosed if diagnosed else None,
            'B_triggered_recorded_packet_count': len(b_rows) if diagnosed else None,
            'B_diagnosed_accepted_packet_count': diagnosed_accepted if diagnosed else None,
            'B_triggered_accepted_packet_count': sum(row['accepted_into_queue'] for row in b_rows) if diagnosed else None,
            'B_rows': b_rows,
        },
        'mpc': {'status_counts': dict(Counter(str(v) for v in statuses)) if statuses else None,
                'controller_initialization_counts': sorted(set(resets)) if resets else None,
                'solver_ms': stats(get(t, 'mpc', 'solver_ms') for t in ticks),
                'wall_ms': stats(get(t, 'mpc', 'wall_ms') for t in ticks)},
        'stop_chain': {
            'normal_window_complete_reported': normal_complete, 'core_stop': stop,
            'continuous_drain': summary.get('drain'), 'final_model_drain': operation.get('model_drain'),
            'recording': operation.get('recording'), 'feedback_cache': operation.get('feedback_cache'),
            'monitor_errors': operation.get('monitor_errors'), 'zero_tx': operation.get('zero_tx'),
            'hub_zero_before': operation.get('hub_zero_before'), 'hub_zero_after': operation.get('hub_zero_after'),
            'protected_files_unchanged': operation.get('protected_files_unchanged'),
            'late_discarded_packet_count': sum(p.get('discarded_after_stop') is True for p in packets) if packets_in is not None else None,
            'robot_commands_reported': summary.get('robot_commands'),
            'physical_motion_started_reported': operation.get('physical_motion_started'),
            'physical_closed_loop_validated_reported': summary.get('physical_closed_loop_validated'),
            'real_time_50Hz_admission_reported': summary.get('real_time_50Hz_admission'),
        },
        'limitations': ['NO_SEND diagnostic commands were not sent to the robot.',
                       'No actuator tracking, physical braking or task-success metric is computed.',
                       'Missing saved fields remain null; sampled positions do not replace analytic extrema.',
                       'A normal ending alone is not an injected independent STOP test.'],
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.run_dir.is_dir():
        parser.error('--run-dir must be an existing saved run directory')
    if args.output.exists():
        parser.error('--output already exists; choose a new evidence filename')
    result = analyze(args.run_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'output': str(args.output.resolve()), 'operation_status': result['operation_status'],
                      'tick_count': result['timing']['tick_count'],
                      'evidence_scope': result['evidence_scope']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
