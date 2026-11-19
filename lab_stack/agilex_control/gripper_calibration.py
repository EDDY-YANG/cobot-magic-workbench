"""Explicit empty-gripper metrology and current-position zero; importing opens nothing.

The existing shared Hub supplies every real sample. CPV recovery is a documented
OEM workflow, not an alias for ResetPiper or an unverified drive-learning command.
Physical closure is an operator measurement; CAN zero alone cannot prove it.
"""
from collections import deque
import json
import math
from pathlib import Path
import time
from .gripper_state import decode_gripper_feedback
from .hardware import HardwareBlocked, require_feedback, sdk_profile_parameters

MAX_AGE_S = .2
CLOSED_GAP_MM = .2  # Provisional physical measurement acceptance, not a command limit.
ZERO_FEEDBACK_M = .0002
JOINT_STATIONARY_DEG = .5


def coordinate_review(cfg):
    """Read the coordinate transaction marker; missing means no new registered zero."""
    path = Path(cfg['remote_bundle'])/'runs/gripper-coordinate-review.json'
    try:
        if path.stat().st_size > 65536:
            raise ValueError('Oversized coordinate review marker')
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or type(value.get('pending')) is not bool:
            raise ValueError('Invalid coordinate review marker')
        return value
    except FileNotFoundError:
        return {'pending':False, 'registered_new_zero':False}
    except Exception as exc:
        return {'pending':True, 'read_error':str(exc), 'reason':'Calibration coordinate review unknown'}


def measured_gripper(state, *, now=None):
    now = time.monotonic() if now is None else now
    frame = state.get('raw', {}).get('0x2a8', state.get('raw', {}).get('0x2A8', {}))
    stamp, age = frame.get('monotonic_s'), state.get('age_gripper_s')
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in (stamp, age)):
        raise HardwareBlocked('Actual gripper receipt timestamp/age is missing')
    if not 0 <= age <= MAX_AGE_S or not 0 <= now-stamp <= MAX_AGE_S:
        raise HardwareBlocked('Actual gripper feedback is stale or future-dated')
    try:
        info = decode_gripper_feedback(frame.get('payload_hex', ''))
    except ValueError as exc:
        raise HardwareBlocked('Malformed real 0x2A8 feedback') from exc
    if info.get('linear_position_available') is not True:
        raise HardwareBlocked('Gripper metrology requires linear width feedback; angle/unknown mode blocked')
    value = state.get('grip_m')
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not math.isclose(value, info['aperture_m'], rel_tol=0, abs_tol=1e-9)):
        raise HardwareBlocked('Gripper value disagrees with real raw 0x2A8')
    info.update(received_monotonic_s=stamp, age_s=age)
    return info


def physical_measurement(value, *, closed=False):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 120:
        raise ValueError('Measured physical gap must be a finite 0..120 mm value')
    if closed and value > CLOSED_GAP_MM:
        raise HardwareBlocked('Physically closed empty gripper required; measured gap exceeds 0.2mm acceptance')
    return float(value)


def observe(snapshot, *, seconds=2., cancel=lambda: False):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not .5 <= seconds <= 5:
        raise ValueError('Gripper observation duration must be 0.5..5s')
    begin = time.monotonic()
    samples = deque(maxlen=256)
    previous, count = None, 0
    low = high = None
    first_stamp = last_stamp = None
    final = None
    while time.monotonic() - begin < seconds:
        if cancel():
            raise HardwareBlocked('Cancelled during gripper observation; no command sent')
        state = snapshot()
        info = measured_gripper(state)
        stamp = info['received_monotonic_s']
        if previous is not None and stamp < previous:
            raise HardwareBlocked('Gripper source clock moved backwards')
        if stamp != previous:
            count += 1
            value = info['aperture_m']
            low, high = (value, value) if low is None else (min(low, value), max(high, value))
            first_stamp = stamp if first_stamp is None else first_stamp
            last_stamp = stamp
            samples.append(info)
            previous = stamp
        final = info
        time.sleep(.02)
    if count < 2 or last_stamp-first_stamp < seconds*.7:
        raise HardwareBlocked('Insufficient advancing real gripper samples; repeated cache is not verification')
    return {'status': 'REAL_GRIPPER_OBSERVATION', 'samples_total': count, 'samples': list(samples),
            'samples_truncated': count > len(samples), 'source_span_s': last_stamp-first_stamp,
            'minimum_m': low, 'maximum_m': high, 'measured_span_m': high-low,
            'final': final, 'CAN_send_calls': 0, 'physical_gap_verified': False}


def require_quiet_state(state, *, disabled=False):
    state = require_feedback(state, max_age_s=MAX_AGE_S, require_enabled=False)
    info = measured_gripper(state)
    if not info['healthy']:
        raise HardwareBlocked('Actual gripper fault; no zero or automatic clear-error')
    if disabled and info['enabled']:
        raise HardwareBlocked('Gripper is still enabled; disable it explicitly before zero')
    status = state['status']
    if status.get('teach_status') != 0 or status.get('motion_status') != 0:
        raise HardwareBlocked('Teaching/playback/motion is active; gripper calibration blocked')
    for frame in state.get('external_commands', {}).values():
        age = frame.get('last_age_s')
        if type(age) in (int, float) and age <= 5:
            raise HardwareBlocked('Recent external control traffic; wait for 5s quiet and verify isolation')
    return state, info


def preflight(snapshot, *, disabled, cancel=lambda: False):
    begin = time.monotonic()
    first_q = None
    previous = None
    distinct = 0
    low = high = None
    before = None
    while time.monotonic()-begin < 1.:
        if cancel():
            raise HardwareBlocked('Cancelled before gripper calibration writer')
        state, before = require_quiet_state(snapshot(), disabled=disabled)
        q = state['q_deg']
        first_q = tuple(q) if first_q is None else first_q
        if max(abs(a-b) for a,b in zip(q, first_q)) > JOINT_STATIONARY_DEG:
            raise HardwareBlocked('Arm is not stationary; no gripper calibration command')
        value = before['aperture_m']
        low, high = (value,value) if low is None else (min(low,value), max(high,value))
        if high-low > .0005:
            raise HardwareBlocked('Gripper is not stationary; no zero command')
        if before['received_monotonic_s'] != previous:
            distinct += 1
            previous = before['received_monotonic_s']
        time.sleep(.02)
    if distinct < 2:
        raise HardwareBlocked('No advancing real gripper feedback before calibration')
    return {'before': before, 'joint_before_deg': first_q, 'distinct_samples': distinct,
            'gripper_span_m': high-low, 'CAN_send_calls': 0}


def unchanged_before_command(state, preflight_receipt, *, disabled):
    """Recheck after SDK/storage setup; reject motion before changing coordinates."""
    state, info = require_quiet_state(state, disabled=disabled)
    if max(abs(a-b) for a,b in zip(state['q_deg'],preflight_receipt['joint_before_deg'])) > JOINT_STATIONARY_DEG:
        raise HardwareBlocked('Joint posture changed after preflight; no gripper calibration command')
    if abs(info['aperture_m']-preflight_receipt['before']['aperture_m']) > .0005:
        raise HardwareBlocked('Gripper position changed after preflight; no calibration command')
    return info


def verify_after(snapshot, since, *, zero=False, joint_before=None, cancel=lambda: False):
    begin = time.monotonic()
    first = previous = None
    count = 0
    last = None
    while time.monotonic()-begin < 2.:
        if cancel():
            raise HardwareBlocked('Cancelled after submitted gripper command; no automatic recovery/retry')
        state = require_feedback(snapshot(), max_age_s=MAX_AGE_S, require_enabled=False)
        info = measured_gripper(state)
        last = info
        if not info['healthy'] or info['enabled']:
            raise HardwareBlocked('New fault or enabled feedback after gripper calibration; no automatic recovery')
        if joint_before is not None and max(abs(a-b) for a,b in zip(state['q_deg'],joint_before)) > JOINT_STATIONARY_DEG:
            raise HardwareBlocked('Joint feedback changed during gripper calibration; inspect before continuing')
        stamp = info['received_monotonic_s']
        matched = stamp > since and (not zero or (abs(info['aperture_m']) <= ZERO_FEEDBACK_M and info['homed']))
        if matched and stamp != previous:
            first = stamp if first is None else first
            count += 1
            previous = stamp
            if count >= 2 and stamp-first >= .3:
                return {'status': 'ZERO_FEEDBACK_AND_HOMING_BIT_CONFIRMED' if zero else 'GRIPPER_DISABLED_CONFIRMED',
                        'final': info, 'stable_distinct_samples': count, 'stable_span_s': stamp-first,
                        'physical_gap_verified': False, 'automatic_enable_reset_motion': False}
        elif not matched:
            first, count = None, 0
        time.sleep(.02)
    error = HardwareBlocked('Post-command feedback not confirmed; no zero retry or automatic enable')
    error.gripper_calibration_receipt = {'status': 'SUBMITTED_ACK_NOT_CONFIRMED', 'last_feedback': last,
                                        'physical_gap_verified': False, 'automatic_retry': False}
    raise error


def cpv_workflow():
    return {'status': 'OEM_CPV_WORKFLOW_ONLY', 'executable': False, 'CAN_send_calls': 0,
            'reason': 'Installed firmware/drive-learning completion not commissioned; CPV is not ResetPiper',
            'steps': ['Read firmware and confirm matching OEM recovery procedure',
                      'Record old coordinates; review full-arm support and zero restoration scope',
                      'Use the verified OEM application procedure only after explicit field authorization',
                      'Recheck gripper and J1-J6 zero/travel plus training-coordinate compatibility'],
            'references': ['https://github.com/agilexrobotics/piper_sdk/issues/128',
                           'https://github.com/agilexrobotics/piper_sdk/blob/0.6.2/piper_sdk/demo/V2/piper_set_joint_zero_cpv.py']}


def compact_receipt(result):
    """Keep stdout below the existing web job tail; full real samples stay on USB."""
    compact = {key:value for key,value in result.items() if key != 'samples'}
    if 'samples' in result:
        compact['sample_details_in_usb_report'] = True
    return compact


def require_tx_ready(status):
    if (status.get('opened') is not True or status.get('writer_error')
            or status.get('timing_evidence_incomplete') or status.get('finished')):
        raise HardwareBlocked('TX evidence writer not ready; no calibration command')


def run(cfg, args, session, *, cancel=lambda: False):
    """CLI entry; reuse Hub receiver, acquire a writer only on explicit apply."""
    from .hub import rpc
    from .storage import Ownership, require_not_stopped, write_json, verify_usb
    from .hardware import PiperAdapter
    from .tx_evidence import TxEvidenceWriter
    from contextlib import ExitStack
    operation = args.gripper_operation
    if operation == 'cpv-plan':
        if args.apply:
            raise HardwareBlocked('CPV execution is not commissioned; this action is a workflow only')
        return cpv_workflow()
    if operation in ('inspect','measure') and args.apply:
        raise ValueError('Read-only gripper action does not accept --apply')
    if operation in ('disable','zero') and not args.apply:
        return {'status': 'PLAN_ONLY', 'operation': operation, 'CAN_send_calls': 0,
                'required': 'fresh healthy quiet feedback; leader isolated; empty gripper; zero additionally requires already disabled, physically closed and measured gap<=0.2mm',
                'implicit_motion_or_enable': False}
    def full_status():
        result = rpc(cfg, '/status', timeout=2)
        if args.apply and result.get('recording', {}).get('active') is not False:
            raise HardwareBlocked('Recording active or unknown; preserve its calibration coordinate contract')
        return result
    def snapshot():
        return full_status()['arms'][args.arm]
    if operation in ('inspect','measure'):
        result = observe(snapshot, seconds=args.seconds, cancel=cancel)
        if operation == 'measure':
            physical = physical_measurement(args.measured_gap_mm)
            result.update(label=args.label, physical_gap_mm=physical,
                          operator_measurement=True, feedback_minus_physical_mm=result['final']['aperture_m']*1000-physical)
        return result
    if not args.leader_isolated or not args.empty_gripper:
        raise HardwareBlocked('Explicit leader isolation and empty-gripper review required')
    if operation == 'zero':
        if not args.physically_closed or not args.coordinate_change_reviewed:
            raise HardwareBlocked('Physical closure and training-coordinate change review required')
        physical_measurement(args.measured_gap_mm, closed=True)
    require_not_stopped(cfg)
    entry = cfg['can'][args.arm]
    events = deque(maxlen=64)
    result = {'operation': operation, 'automatic_enable_reset_motion': False}
    with Ownership('writer-'+entry['interface']), ExitStack() as stack:
        full_status()
        receipt = preflight(snapshot, disabled=operation=='zero', cancel=cancel)
        result['preflight'] = receipt
        adapter = PiperAdapter({**entry, 'sdk_version':'0.6.2', **sdk_profile_parameters(cfg,args.arm), 'leader_isolated':True,
                                'max_feedback_age_s':MAX_AGE_S}, snapshot, events.append)
        writer = TxEvidenceWriter(cfg, session.path/'host-gripper-calibration-tx-v1.jsonl')
        stack.callback(lambda: session.report.update(tx_evidence=writer.close(timeout_s=5)))
        session.report['tx_evidence_start'] = writer.start()
        require_tx_ready(session.report['tx_evidence_start'])
        stack.callback(lambda: session.report.update(gripper_sdk_events=list(events), packets_sent=adapter.packets_sent,
                                                     tx_adapter_evidence=adapter.tx_evidence_status))
        stack.callback(adapter.close)
        adapter.attach_tx_evidence(writer)
        if cancel():
            raise HardwareBlocked('Cancelled before opening calibration writer')
        adapter.connect_for_apply(gripper_calibration_only=True)
        if operation == 'zero':
            # Persist BEFORE a possibly state-changing send. A timeout/partial
            # transport must not allow the old model to resume automatically.
            write_json(Path(cfg['remote_bundle'])/'runs/gripper-coordinate-review.json',
                {'pending':True, 'reason':'Gripper zero transaction requires independent metrology and training-coordinate review',
                 'session':str(session.path), 'command_completion_verified':False,
                 'old_feedback_m':receipt['before']['aperture_m'], 'measured_gap_mm':args.measured_gap_mm}, cfg)
        with adapter.dispatch({'run_id':session.path.name,'operation':'gripper_'+operation,
                               'changes_coordinate':operation=='zero','motion_target':False}):
            require_not_stopped(cfg)
            verify_usb(cfg)
            require_tx_ready(writer.status())
            if cancel():
                raise HardwareBlocked('Cancelled before calibration command')
            unchanged_before_command(snapshot(), receipt, disabled=operation=='zero')
            if operation == 'zero':
                adapter.set_gripper_zero_once()
            else:
                adapter.disable_gripper_for_calibration()
        since = time.monotonic()
        result['command_submitted_monotonic_s'] = since
        session.report['gripper_calibration'] = result
        result['verification'] = verify_after(snapshot, since, zero=operation=='zero',
                                               joint_before=receipt['joint_before_deg'], cancel=cancel)
        result['status'] = result['verification']['status']
        if operation == 'zero':
            result['measured_gap_before_mm'] = args.measured_gap_mm
            result['post_zero_physical_metrology_required'] = True
            result['policy_blocked_until_coordinate_review'] = True
    evidence = session.report.get('tx_evidence', {})
    if (evidence.get('finished') is not True or evidence.get('queued') != 0
            or evidence.get('timing_evidence_incomplete') or evidence.get('writer_error') or evidence.get('flush_timeout')):
        result['evidence_status'] = 'INCOMPLETE'
        error = HardwareBlocked('Gripper feedback recorded but TX evidence incomplete; no automatic retry')
        error.gripper_calibration_receipt = result
        raise error
    result['evidence_status'] = 'COMPLETE'
    return result
