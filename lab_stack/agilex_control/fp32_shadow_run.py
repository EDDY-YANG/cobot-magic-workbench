"""Owned, finite FP32 H50 shadow run through the existing receive-only Hub.

No CAN/socket/SDK/device producer is created. Only native recording is started;
all returned model actions are evidence and are never dispatched.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid


def _hooks():
    from .hub import rpc
    from .remote_fp32 import FP32ShadowClient, USBShadowJournal
    from .recording_lifecycle import preserving_rpc, stop_owned_recording, OWNED_RECORDING_STOP_PROTOCOL
    from .storage import Ownership, verify_usb, write_json, open_usb_directory
    from .time_contract import capture_clock_domain
    return dict(rpc=rpc, client=FP32ShadowClient, journal=USBShadowJournal,
                preserving_rpc=preserving_rpc, stop_owned=stop_owned_recording,
                stop_protocol=OWNED_RECORDING_STOP_PROTOCOL, ownership=Ownership,
                verify_usb=verify_usb, write_json=write_json, open_dir=open_usb_directory,
                clock_domain=capture_clock_domain)


def _require(condition, message):
    if not condition:
        raise RuntimeError('FP32 shadow run: ' + message)


def _error(exc):
    return {'type': type(exc).__name__, 'message': str(exc)}


def _tx_packets(cfg):
    """Read interface-wide kernel counters; they cannot attribute other writers."""
    result = {}
    for role, entry in cfg.get('can', {}).items():
        interface = entry.get('interface')
        _require(isinstance(interface, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}', interface),
                 'invalid registered CAN interface')
        raw = (Path('/sys/class/net') / interface / 'statistics' / 'tx_packets').read_text(encoding='ascii').strip()
        _require(raw.isdecimal(), 'invalid kernel TX counter')
        result[role] = {'interface': interface, 'tx_packets': int(raw)}
    _require(bool(result) and 'right' in result, 'registered right CAN interface missing')
    return result


def _hub_no_send(status):
    _require(isinstance(status, dict) and type(status.get('CAN_send_calls')) is int and status['CAN_send_calls'] == 0,
             'Hub lacks zero application-send evidence')
    arms = status.get('arms')
    _require(isinstance(arms, dict) and 'right' in arms, 'right Hub observer missing')
    evidence = {}
    receive_overflows = {}
    for role, state in arms.items():
        value = state.get('CAN_commands_sent_by_this_observer') if isinstance(state, dict) else None
        _require(type(value) is int and value == 0, 'observer lacks zero-send evidence: ' + role)
        evidence[role] = value
        dropped = state.get('socket_rx_overflow_packets')
        _require(type(dropped) is int and dropped >= 0, 'observer kernel receive-overflow counter missing: ' + role)
        receive_overflows[role] = dropped
    return {'CAN_send_calls': status['CAN_send_calls'], 'observer_commands_sent': evidence,
            'socket_rx_overflow_packets': receive_overflows,
            'scope': 'Hub/observer application counters; interface TX includes all processes.'}


def _episode_path(cfg, value, previous_path):
    _require(isinstance(value, str) and bool(value), 'recording start did not return an owned path')
    root, path = Path(cfg['usb']), Path(value)
    _require(path.is_absolute() and '..' not in path.parts and path != root and path.is_relative_to(root),
             'owned episode escapes the selected USB')
    _require(path.parent == Path(cfg['remote_bundle']) / 'runs' and value != previous_path,
             'start returned an old or foreign recording episode')
    return value


def _recover_start_path(cfg, rpc, previous_path, record_task, hooks):
    """A lost start response never authorizes stopping an unproven episode."""
    current = rpc(cfg, '/status?compact=1', timeout=3)['recording']
    path = _episode_path(cfg, current.get('path'), previous_path)
    hooks['verify_usb'](cfg)
    candidate = Path(path)
    _require(candidate.is_dir() and candidate.resolve(strict=True) == candidate,
             'unknown recording path is redirected or missing')
    operation_path = candidate / 'operation.json'
    _require(operation_path.is_file() and not operation_path.is_symlink() and
             operation_path.resolve(strict=True).parent == candidate,
             'lost-start ownership receipt is redirected or missing')
    operation = json.loads(operation_path.read_text(encoding='utf-8'))
    _require(operation.get('raw_storage') == path and operation.get('task') == record_task,
             'lost-start episode does not prove this unique run task')
    return path


def _wait_active(cfg, rpc, owned_path, stop_event):
    deadline = time.monotonic() + 5
    while True:
        _require(not stop_event.is_set(), 'stop requested while waiting for recording')
        status = rpc(cfg, '/status?compact=1', timeout=3)['recording']
        _require(status.get('path') == owned_path, 'recording owner changed before sampling')
        _require(status.get('error') is None and type(status.get('overflows')) is int and status['overflows'] == 0,
                 'native recorder reports an error or overflow')
        if status.get('active') is True and type(status.get('samples')) is int and status['samples'] > 0:
            return status
        _require(time.monotonic() < deadline, 'native recording did not become active with real samples')
        stop_event.wait(.05)


def _save_episode_evidence(cfg, path, receipt, hooks):
    hooks['verify_usb'](cfg)
    directory = Path(path) / 'evidence'
    fd = hooks['open_dir'](directory, cfg, create=True)
    os.close(fd)
    hooks['write_json'](directory / 'fp32-shadow-run.json', receipt, cfg)


def run(cfg, session, stop_event, count, task, *, runtime=None):
    """Run 1..5 serial shadow predictions, always finalize only our episode."""
    _require(type(count) is int and 1 <= count <= 5, 'count must be an integer in 1..5')
    _require(isinstance(task, str) and 0 < len(task.strip()) <= 200 and len(task) <= 200 and '\x00' not in task,
             'explicit task must contain 1..200 characters')
    setting = cfg.get('fp32_shadow')
    _require(isinstance(setting, dict) and setting.get('enabled') is True and
             type(setting.get('health_port')) is int and setting['health_port'] == 18883,
             'explicit enabled FP32 shadow config with loopback port 18883 required')
    hooks = _hooks()
    raw_rpc = hooks['rpc']

    def rpc(cfg, path, data=None, timeout=20):
        return hooks['preserving_rpc'](raw_rpc, cfg, path, data, timeout=timeout)

    run_id = uuid.uuid4().hex
    record_task = task + ' [fp32-shadow-run:' + run_id + ']'
    receipt = {
        'schema': 'tiger-fp32-shadow-run-v1', 'run_id': run_id,
        'task': task, 'recording_task': record_task, 'requested_predictions': count,
        'decision': 'SHADOW_NOT_DISPATCHED', 'module_dispatched_commands': 0,
        'status': 'STARTED', 'started_monotonic_ns': time.monotonic_ns(),
        'attempts': [], 'cleanup_errors': [], 'owned_recording_path': None,
        'physical_camera_frame_drop_count': None,
        'scope': 'Finite live inference and native USB recording only. No enable/mode/joint/gripper/STOP command.',
    }
    session.report['fp32_shadow_run'] = receipt
    primary, primary_tb = None, None
    owned_path = None
    before_tx = None
    try:
        with hooks['ownership']('fp32-shadow-run'):
            try:
                _require(not stop_event.is_set(), 'stop requested before preflight')
                root = Path(hooks['verify_usb'](cfg))
                _require(root == Path(cfg['usb']) and Path(session.path).is_relative_to(root), 'audit session is outside selected USB')
                local_domain = hooks['clock_domain']()
                _require(isinstance(local_domain, dict) and local_domain.get('identity_verified') is True,
                         'local host/boot clock identity is unverified')
                receipt['local_clock_domain'] = deepcopy(local_domain)
                client = runtime if runtime is not None else hooks['client']('http://127.0.0.1:18883')
                receipt['health_preflight'] = client.check_health()
                preflight_fingerprint = receipt['health_preflight'].get('policy_fingerprint')
                _require(isinstance(preflight_fingerprint, str) and bool(preflight_fingerprint),
                         'public health preflight lacks a validated policy fingerprint')
                before = rpc(cfg, '/status?compact=1', timeout=3)
                receipt['hub_send_before'] = _hub_no_send(before)
                current = before.get('recording', {})
                _require(current.get('active') is False and current.get('saving') is False,
                         'another recording is active or still saving')
                _require(current.get('owned_stop_protocol') == hooks['stop_protocol'],
                         'Hub does not prove atomic owned recording stop')
                previous_path = current.get('path')
                before_tx = _tx_packets(cfg)
                receipt['interface_tx_before'] = before_tx
                _require(not stop_event.is_set(), 'stop requested before native recording')
                receipt['recording_start_attempted'] = True
                try:
                    started = rpc(cfg, '/record/start', {'task': record_task}, timeout=20)
                    owned_path = _episode_path(cfg, started.get('path'), previous_path)
                    receipt['recording_start_response'] = started
                except BaseException:
                    # Observe once; never retry POST or adopt a foreign episode.
                    try:
                        owned_path = _recover_start_path(cfg, rpc, previous_path, record_task, hooks)
                        receipt['lost_start_response_owned_path_recovered'] = True
                    except Exception as recovery_error:
                        receipt['start_recovery_error'] = _error(recovery_error)
                        receipt['recording_start_outcome_unknown'] = True
                    raise
                receipt['owned_recording_path'] = owned_path
                receipt['recording_active'] = _wait_active(cfg, rpc, owned_path, stop_event)
                journal = hooks['journal'](cfg, owned_path)
                for index in range(count):
                    _require(not stop_event.is_set(), 'stop requested before next prediction')
                    attempt = {'ordinal': index + 1, 'started_monotonic_ns': time.monotonic_ns(), 'status': 'STARTED'}
                    receipt['attempts'].append(attempt)
                    try:
                        snapshot = rpc(cfg, '/observation/fp32', timeout=3)
                        result = client.run_once(snapshot, task, journal, local_clock_domain=local_domain,
                                                 recording_guard=lambda: rpc(cfg, '/recording/fp32-binding', timeout=3))
                        _require(isinstance(result, dict) and result.get('status') == 'SHADOW_INFERENCE_RECORDED',
                                 'client did not return a fully recorded successful shadow result')
                        _require(result.get('policy_fingerprint') == preflight_fingerprint,
                                 'policy fingerprint changed after preflight')
                        attempt.update(status='SHADOW_INFERENCE_RECORDED', result=deepcopy(result))
                    except BaseException as exc:
                        attempt.update(status='FAILED', error=_error(exc))
                        raise
                    finally:
                        attempt['finished_monotonic_ns'] = time.monotonic_ns()
                    session.save()
            except BaseException as exc:
                primary, primary_tb = exc, sys.exc_info()[2]
                receipt['error'] = _error(exc)
            finally:
                receipt['owned_recording_path'] = owned_path
                if owned_path is not None:
                    try:
                        finalization = hooks['stop_owned'](cfg, rpc, owned_path, timeout=300)
                        receipt['recording_finalization'] = finalization
                        _require(finalization.get('completed') is True and finalization.get('healthy') is True,
                                 'owned recording did not finalize with healthy saved evidence')
                    except Exception as exc:
                        receipt['cleanup_errors'].append({'phase': 'owned_recording_finalization', **_error(exc)})
                if before_tx is not None:
                    try:
                        after_tx = _tx_packets(cfg)
                        receipt['interface_tx_after'] = after_tx
                        _require(set(after_tx) == set(before_tx), 'registered interfaces changed during run')
                        deltas = {}
                        for role, before_counter in before_tx.items():
                            after_counter = after_tx[role]
                            _require(after_counter['interface'] == before_counter['interface'], 'interface identity changed')
                            deltas[role] = after_counter['tx_packets'] - before_counter['tx_packets']
                        receipt['interface_tx_deltas'] = deltas
                        _require(all(delta == 0 for delta in deltas.values()),
                                 'interface-wide TX changed or counter reset; zero-TX acceptance failed')
                    except Exception as exc:
                        receipt['cleanup_errors'].append({'phase': 'interface_tx_after', **_error(exc)})
                try:
                    receipt['hub_send_after'] = _hub_no_send(rpc(cfg, '/status?compact=1', timeout=3))
                    if 'hub_send_before' in receipt:
                        before_overflow = receipt['hub_send_before']['socket_rx_overflow_packets']
                        after_overflow = receipt['hub_send_after']['socket_rx_overflow_packets']
                        _require(set(before_overflow) == set(after_overflow), 'observer roles changed during run')
                        deltas = {role: after_overflow[role] - value for role, value in before_overflow.items()}
                        receipt['observer_socket_rx_overflow_deltas'] = deltas
                        _require(all(delta == 0 for delta in deltas.values()),
                                 'CAN receive-overflow increased or counter reset; raw CAN diagnostic is incomplete')
                except Exception as exc:
                    receipt['cleanup_errors'].append({'phase': 'hub_send_after', **_error(exc)})
                receipt['finished_monotonic_ns'] = time.monotonic_ns()
                receipt['status'] = 'SHADOW_RUN_RECORDED' if primary is None and not receipt['cleanup_errors'] else 'FAILED'
                if owned_path is not None:
                    try:
                        receipt['episode_evidence_path'] = str(Path(owned_path) / 'evidence' / 'fp32-shadow-run.json')
                        _save_episode_evidence(cfg, owned_path, receipt, hooks)
                    except Exception as exc:
                        receipt['cleanup_errors'].append({'phase': 'episode_evidence_save', **_error(exc)})
                        receipt['status'] = 'FAILED'
                try:
                    session.save()
                except Exception as exc:
                    receipt['cleanup_errors'].append({'phase': 'audit_session_save', **_error(exc)})
                    receipt['status'] = 'FAILED'
    except BaseException as exc:
        if primary is None:
            primary, primary_tb = exc, sys.exc_info()[2]
            receipt['error'] = _error(exc)
            receipt['status'] = 'FAILED'
            try:
                session.save()
            except Exception as save_error:
                receipt['cleanup_errors'].append({'phase': 'outer_failure_save', **_error(save_error)})
    if primary is not None:
        raise primary.with_traceback(primary_tb)
    _require(not receipt['cleanup_errors'], 'cleanup or evidence failed: ' + json.dumps(receipt['cleanup_errors'], ensure_ascii=False))
    return receipt




def compact_receipt(result):
    """Small CLI/web receipt; complete observations/H50 stay in USB evidence.

    This pure projection does not alter the full run/journal. Unknown values
    remain None; a response alone cannot count as a recorded successful attempt.
    """
    _require(isinstance(result, dict), 'full run result must be an object')
    attempts = result.get('attempts')
    _require(isinstance(attempts, list) and len(attempts) <= 5, 'full run attempts must be a list of at most five')
    summaries = []
    completed = 0
    for attempt in attempts:
        _require(isinstance(attempt, dict), 'invalid full run attempt')
        inference = attempt.get('result')
        inference = inference if isinstance(inference, dict) else {}
        response = inference.get('response')
        response = response if isinstance(response, dict) else {}
        if attempt.get('status') == 'SHADOW_INFERENCE_RECORDED' and inference.get('status') == 'SHADOW_INFERENCE_RECORDED':
            completed += 1
        summaries.append({
            'ordinal': attempt.get('ordinal'), 'status': attempt.get('status'),
            'request_id': inference.get('request_id'),
            'client_round_trip_ms': inference.get('client_round_trip_ms'),
            'action_shape': deepcopy(response.get('action_shape')),
            'action_dtype': response.get('action_dtype'),
            'server_inference_wall_ms': response.get('inference_wall_ms'),
        })
    finalization = result.get('recording_finalization')
    finalization = finalization if isinstance(finalization, dict) else {}
    saved_episode = finalization.get('episode_finalization')
    saved_episode = saved_episode if isinstance(saved_episode, dict) else {}
    return {
        'schema': 'tiger-fp32-shadow-run-summary-v1',
        'status': result.get('status'), 'run_id': result.get('run_id'), 'task': result.get('task'),
        'requested_predictions': result.get('requested_predictions'), 'completed_predictions': completed,
        'decision': result.get('decision'), 'module_dispatched_commands': result.get('module_dispatched_commands'),
        'owned_recording_path': result.get('owned_recording_path'),
        'episode_evidence_path': result.get('episode_evidence_path'), 'attempts': summaries,
        'interface_tx_deltas': deepcopy(result.get('interface_tx_deltas')),
        'observer_socket_rx_overflow_deltas': deepcopy(result.get('observer_socket_rx_overflow_deltas')),
        'recording_finalization': {'completed': finalization.get('completed'),
                                   'healthy': finalization.get('healthy'), 'samples': saved_episode.get('samples')},
    }
