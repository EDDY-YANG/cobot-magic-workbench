"""Bounded V2 policy window and normal reference-hold settling.

No device is opened here. The caller supplies an owned canonical writer and
fresh feedback. A fault stops new targets; it is not a motor emergency stop.
"""
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from copy import deepcopy
import json
import threading
import time
from .live_io import require
from .runtime import dispatch_prepare_and_infer, emit, CompletionWakeup
from .lifecycle import drain_before_unlock
from .window_contract import (validate_window_seconds, window_deadline_ns, limits_for_window,
                              CONTROL_PERIOD_NS, MODEL_BUDGET_NS, SETTLE_BUDGET_NS)


class BoundedTxEvents:
    def __init__(self, capacity=30000):
        require(type(capacity) is int and 1<=capacity<=100000,'Bounded TX record capacity required')
        # Canonical dispatch contexts already contain MappingProxyType values.
        # deepcopy cannot copy those. Keep bounded immutable evidence in RAM;
        # convert to JSON-compatible values only when the caller flushes it.
        from agilex_control.tx_evidence import freeze_evidence,_jsonable
        self._freeze=freeze_evidence;self._jsonable=_jsonable
        self.capacity=capacity;self._records=[];self.lock=threading.Lock();self.incomplete=False
        self.failure_reasons=[]
    def enqueue(self, record):
        try:frozen=self._freeze(record)
        except BaseException as exc:
            self.mark_incomplete(type(exc).__name__+': '+str(exc));raise
        with self.lock:
            if len(self._records)>=self.capacity:
                self.incomplete=True
                raise RuntimeError('Bounded TX RAM evidence exhausted')
            self._records.append(frozen)
        return True
    def mark_incomplete(self,reason):
        with self.lock:
            self.incomplete=True
            if len(self.failure_reasons)<16:self.failure_reasons.append(str(reason)[:500])
    @property
    def records(self):
        with self.lock:items=tuple(self._records)
        return [self._jsonable(item) for item in items]
    def status(self):
        with self.lock:
            return {'timing_evidence_incomplete':self.incomplete or len(self._records)>self.capacity-16,
                    'queued':len(self._records),'capacity':self.capacity,'storage':'BOUNDED_RAM_UNTIL_EXIT',
                    'failure_reasons':list(self.failure_reasons)}


def position_settled(samples, target_rad_m):
    """Source-time stability and arm tracking; contact may block gripper closure."""
    if len(samples)<2 or samples[-1][0]-samples[0][0]<300_000_000:
        return False
    import numpy as np
    q=np.asarray([v for _,v in samples]);target=np.asarray(target_rad_m)
    tolerance=np.r_[np.deg2rad(np.full(6,.5)),.002]
    return bool(np.all(np.ptp(q,axis=0)<=tolerance)
                and np.all(np.abs(q[-1,:6]-target[:6])<=np.deg2rad(5.)))


def settle_measurements(samples, target_rad_m):
    """Report actual target errors separately from stable position evidence."""
    if not samples or target_rad_m is None:
        return {'available':False,'grasp_success':'NOT_EVALUATED'}
    import numpy as np
    measured=np.asarray(samples[-1][1]);target=np.asarray(target_rad_m)
    return {'available':True,'source_window_ms':(samples[-1][0]-samples[0][0])/1e6,
        'joint_target_error_deg':np.rad2deg(measured[:6]-target[:6]).tolist(),
        'gripper_target_error_mm':float((measured[6]-target[6])*1000),
        'gripper_target_reached':bool(abs(measured[6]-target[6])<=.002),
        'grasp_success':'NOT_EVALUATED','motor_zero_velocity_measured':False}


def retain_packet_preserving_error(result, retain):
    """A logging failure must not replace an already-known planning failure."""
    try:
        retain(result)
    except Exception as exc:
        primary = (result.get('physical_commit_error') or result.get('timeqp_preparation_error')
                   or result.get('pipeline_error'))
        if primary:
            raise RuntimeError(str(primary) + '; V2 evidence recording also failed: '
                               + type(exc).__name__ + ': ' + str(exc)) from exc
        raise


def run_physical_window(core, model, hub, journal, writer, cache, *, prompt,
                        duration_s, stop_event, fast_guard, initial_capture, save, evidence):
    """Production loop with bounded off-thread evidence; never a whole-run list.

    Evidence is finalized by the owning session only after model and writer close.
    A late candidate is recorded but cannot become active after the policy deadline.
    """
    from .bridge_step_policy import emit_if_due
    duration_s = validate_window_seconds(duration_s)
    limits = limits_for_window(duration_s)
    start = time.monotonic_ns(); end = window_deadline_ns(start, duration_s)
    scheduled = request_due = start
    future = None; request = {}; settling = False; settle_event = None
    first_dispatch = True; pause_recovery_packets = 0
    last_step_warning_ns = None
    settle_samples = deque(); error = None; last_attempt = None
    last_tick_ns = None; tick_count = packet_count = 0; last_settle_revision = None
    # Scalar stage timestamps only; bounded RAM, flushed once at finalization.
    timing_ring = deque(maxlen=128)
    read_control = getattr(cache, 'read_control', cache.read_latest)
    completion_wakeup = CompletionWakeup()
    evidence.check_alive()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='v2-physical-model-timeqp')

    def retain_packet(result):
        nonlocal packet_count, pause_recovery_packets
        packet_count += 1
        if result.get('reference_pause_recovery') is not None:
            pause_recovery_packets += 1
        require(packet_count-pause_recovery_packets <= limits['packet_count_max'], 'Physical policy count guard exceeded')
        require(pause_recovery_packets <= limits['tick_count_max'], 'Physical pause recovery count guard exceeded')
        evidence.append_packet(result)

    def schedule_request(now):
        nonlocal request_due, future, request, initial_capture, first_dispatch
        if settling:
            return
        if future is None and not first_dispatch:
            request_due = core.next_request_due_ns(now)
        if future is None and request_due is not None and now >= request_due and now < end-MODEL_BUDGET_NS:
            request = {}
            future = pool.submit(dispatch_prepare_and_infer, core, model, hub, journal, prompt,
                                 initial_capture, request, stop_event)
            completion_wakeup.attach(future)
            initial_capture = None
            first_dispatch = False

    try:
        while True:
            now = time.monotonic_ns()
            if now < scheduled and not (future is not None and future.done()):
                completion_wakeup.wait(future, min(.005, (scheduled-now)/1e9)); continue
            fast_guard(); require(not stop_event.is_set(), 'Operator STOP/EOF received')
            evidence.check_alive()
            guards_completed_ns = time.monotonic_ns()
            hold = getattr(core, 'coverage_hold', None)
            if (now >= end or (hold is not None and future is None)) and not settling:
                settle_event = core.begin_settle(now); model.cancel(); settling = True
                if hold is not None:
                    settle_event['coverage_hold'] = hold
            if future is not None and future.done():
                result = future.result(); candidate = result.pop('_prepared_candidate', None)
                result['captured_snapshot'] = request.get('captured_snapshot'); future = None
                try:
                    fast_guard(); require(not stop_event.is_set(), 'Cancelled before physical commit')
                    # A guard/read can cross the deadline after the first clock read.
                    current = time.monotonic_ns()
                    if current >= end and not settling:
                        settle_event = core.begin_settle(current); model.cancel(); settling = True
                    if result.get('reference_coverage_hold') is not None:
                        require(result['reference_coverage_hold'] == core.coverage_hold, 'unowned coverage hold')
                        result['discarded_without_commit'] = True
                    elif settling:
                        result['discarded_after_window'] = True
                    else:
                        recover = getattr(core, 'consume_pause_result', None)
                        recovery = None if recover is None else recover(request, result, current)
                        if recovery is not None:
                            result['reference_pause_recovery'] = recovery
                            result['discarded_without_commit'] = True
                        else:
                            require(result['eligible_for_diagnostic_commit'], result.get('pipeline_error')
                                    or result.get('timeqp_preparation_error') or 'Policy response late/cancelled')
                            require(result['observation_sha256'] == request['observation_sha256'],
                                    'Policy input binding changed')
                            result['publication_attempt_started_ns'] = time.monotonic_ns()
                            result['physical_reference_acceptance'] = core.commit_candidate(request['token'], candidate)
                            result['publication_returned_ns'] = time.monotonic_ns()
                            last_step_warning_ns = emit_if_due(getattr(core,'manual_speed',None),
                                result['physical_reference_acceptance'],result['publication_returned_ns'],last_step_warning_ns)
                except BaseException as exc:
                    result['publication_rejected_ns'] = time.monotonic_ns()
                    result['physical_commit_error'] = type(exc).__name__ + ': ' + str(exc)
                    result['discarded_without_commit'] = 'physical_reference_acceptance' not in result
                    raise
                finally:
                    retain_packet_preserving_error(result, retain_packet)
                request_due = None
                schedule_request(time.monotonic_ns())
            # Completion can publish between control ticks, but cannot send early.
            if time.monotonic_ns() < scheduled:
                continue
            feedback_read_started_ns = time.monotonic_ns()
            feedback = read_control(); now = time.monotonic_ns()
            hold = getattr(core, 'coverage_hold', None)
            if (now >= end or (hold is not None and future is None)) and not settling:
                settle_event = core.begin_settle(now); model.cancel(); settling = True
                if hold is not None:
                    settle_event['coverage_hold'] = hold
            revision = tuple(feedback['source_ns_by_component'][k] for k in ('q12','q34','q56','gripper'))
            last_attempt = {'now_ns': now, 'feedback_ns': feedback['source_ns'],
                            'gap_ms': None if last_tick_ns is None else (now-last_tick_ns)/1e6,
                            'scheduled_ns':scheduled,'guards_completed_ns':guards_completed_ns,
                            'feedback_read_started_ns':feedback_read_started_ns,
                            'core_started_ns':time.monotonic_ns(),'stage':'core_tick'}
            timing_ring.append(last_attempt)
            tick = core.tick(now, feedback['q_rad_m'], feedback['source_ns'], feedback_revision=revision)
            last_attempt.update(core_completed_ns=time.monotonic_ns(),
                control_lock_requested_ns=tick.get('control_lock_requested_ns'),
                control_lock_acquired_ns=tick.get('control_lock_acquired_ns'),
                core_compute_ms=tick.get('cycle_compute_ms'),stage='writer_send')
            tick['scheduled_tick_ns'] = scheduled
            sent = writer.send(tick['command_rad_m'], {
                'command_id': tick['command_id'], 'reference_sample_monotonic_ns': now,
                'scheduled_tick_monotonic_ns': scheduled, 'phase': tick['status'],
                'reference_scope': tick['reference_semantics'], 'controller':'OFFICIAL_V2_ACADOS'},
                ack_joint=core.ack_joints)
            last_attempt.update(writer_returned_ns=time.monotonic_ns(),
                writer=dict(getattr(writer,'receipt',{}).get('last_send_attempt',{})),stage='gripper_ack')
            tick['gripper_ack'] = core.ack_gripper(sent)
            writer_ack_completed_ns=time.monotonic_ns()
            tick['writer_ack_completed_ns']=writer_ack_completed_ns
            speed_control=getattr(core,'manual_speed',None)
            if speed_control is not None:
                if settling:
                    speed_control.close('Window entered settling; no new speed changes')
                else:
                    speed_control.advance_reference(writer_ack_completed_ns,tick.get('reference_phase_ns'),
                                                    reference_sample_ns=now)
            tick['host_send'] = sent; tick['physical_dispatch_performed'] = True; tick['command_is_unsent'] = False
            tick_count += 1; last_tick_ns = now
            evidence.append_tick(tick)
            last_attempt.update(evidence_enqueued_ns=time.monotonic_ns(),stage='post_send')
            require(tick_count <= limits['tick_count_max'], 'Physical tick count guard exceeded')
            if settling:
                if revision != last_settle_revision:
                    settle_samples.append((feedback['source_ns'], feedback['q_rad_m']))
                    last_settle_revision = revision
                while settle_samples and feedback['source_ns']-settle_samples[0][0] > 350_000_000:
                    settle_samples.popleft()
                if position_settled(settle_samples, core.settle_reference):
                    break
                require(now-core.settle_started_ns <= SETTLE_BUDGET_NS,
                        'Real position did not settle within five seconds')
            else:
                model.check_alive()
                schedule_request(now)
            finished = time.monotonic_ns(); scheduled += CONTROL_PERIOD_NS
            last_attempt.update(completed_ns=finished,stage='completed')
            if finished >= scheduled:
                scheduled += ((finished-scheduled)//CONTROL_PERIOD_NS+1)*CONTROL_PERIOD_NS
    except BaseException as exc:
        error = type(exc).__name__ + ': ' + str(exc)
        if last_attempt is not None:
            last_attempt.update(failed_ns=time.monotonic_ns(),error=error)
            if last_attempt.get('stage')=='writer_send':
                last_attempt['writer']=dict(getattr(writer,'receipt',{}).get('last_send_attempt',{}))
        core.fault(error); writer.fault_stop(error); stop_event.set()
    finally:
        # Stop new targets first, then drain. No new command is issued by cleanup.
        speed_control=getattr(core,'manual_speed',None)
        if speed_control is not None:speed_control.close('Window completed or faulted')
        # In physical sessions this is bounded RAM enqueue, not stdout. A
        # blocked output worker must never prevent model/writer ownership drain.
        diagnostic_emit=speed_control.emit if speed_control is not None else emit
        drain = drain_before_unlock(model, future, emit=diagnostic_emit)
        if future is not None:
            try:
                result = future.result(); result.pop('_prepared_candidate', None)
                result.update(discarded_after_window=True, captured_snapshot=request.get('captured_snapshot'))
            except BaseException as exc:
                result = {'discarded_after_window': True, 'error_type': type(exc).__name__,
                          'captured_snapshot': request.get('captured_snapshot')}
            try:
                retain_packet(result)
            except BaseException as exc:
                error = error or 'Evidence final packet: ' + repr(exc)
        pool.shutdown(wait=True)
        close_reason = 'REFERENCE_COVERAGE_HOLD' if getattr(core, 'coverage_hold', None) else 'WINDOW_FINISHED'
        closed = core.close(close_reason if error is None else 'FAULT_EXIT')
    if model.last_attempt:
        save('physical-last-model-attempt.json', model.last_attempt)
    result = {'status': 'PHYSICAL_WINDOW_COMPLETED_AND_POSITION_SETTLED' if error is None else 'PHYSICAL_FAILED_RETAINED',
        'error': error, 'policy_window_s': duration_s, 'requested_window_s': duration_s,
        'start_ns': start, 'policy_end_ns': end, 'count_guards': limits,
        'last_tick_attempt': last_attempt, 'actual_send_groups': tick_count, 'packet_count': packet_count,
        'pause_recovery_packets':pause_recovery_packets,
        'control_stage_timing':{'schema':'v2-control-stage-timing-v1','capacity':128,
            'retained':list(timing_ring),'storage':'BOUNDED_RAM_FINAL_FLUSH_ONLY',
            'clock':'host_monotonic_ns','not_motor_receipt_or_hardware_timing':True},
        'accepted_packets': core.accepted_count, 'normal_settle': settle_event,
        'position_stable_300ms': error is None,
        'settle_measurements': settle_measurements(settle_samples, core.settle_reference),
        'stability_is_not_measured_zero_motor_velocity': True, 'model_drain': drain, 'core_close': closed,
        'evidence_finalization_pending': True,
        'publication_wakeup': 'FUTURE_CALLBACK_EVENT_SAME_CONTROL_THREAD',
        'control_send_period_ns_unchanged': CONTROL_PERIOD_NS,
        'fault_behavior': 'No new targets; last received target may finish. Not hardware emergency stop.'}
    hold = getattr(core, 'coverage_hold', None)
    if hold is not None:
        result['reference_coverage_hold'] = hold
        if error is None:
            error = 'REFERENCE_HELD_INSUFFICIENT_COVERAGE: 原轨迹不足 35 点，已保持原参考并结束本轮，需人工开始下一轮'
            result.update(status='PHYSICAL_REFERENCE_HELD_AND_SETTLED', error=error)
    save('physical-summary.json', result)
    if error:
        raise RuntimeError(error)
    return result
