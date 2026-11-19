"""Real-I/O NO_SEND orchestration; this module contains no motion interface.

Kept separate from both deployed runtimes. Only an explicitly bound Linux/USB
installation can enter these functions. CPU fixtures exercise lifecycle and
controller separately; they never create a real-I/O PASS receipt.
"""
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import math
from datetime import datetime
import json
import os
import sys
from pathlib import Path
import threading
import time
import uuid

from .packet_images import prepare_snapshot
from .lifecycle import drain_before_unlock
from .request_lifecycle import ReferenceCaptureDeferred
from .live_io import (ExistingHub, PinnedLoadedModel, owned_session, make_rtc,
                      FRESH, FROZEN, require, public_receipt)
from .runtime_guard import (can_counters, verify_zero_tx, verify_model,
                            check_stop, verify_files, source_files, sha)


_EMIT_LOCK = threading.Lock()


def emit(value):
    # One complete protocol line across model/command-reader threads.
    with _EMIT_LOCK:
        print(json.dumps(public_receipt(value), ensure_ascii=False), flush=True)


def emit_unbuffered(value):
    """Physical-session output worker only; never called by control/cleanup.

    Avoid holding Python stdout's buffered-I/O lock if a downstream pipe is
    stuck during interpreter shutdown. This does not change FD flags, redirect
    stdout, or alter the normal NO_SEND emitter. Outbox owns bounded shutdown.
    """
    raw=(json.dumps(public_receipt(value),ensure_ascii=False)+'\n').encode('utf8')
    with _EMIT_LOCK:
        offset=0
        while offset<len(raw):
            count=os.write(sys.stdout.fileno(),raw[offset:])
            if count<=0:raise OSError('Short event stdout write')
            offset+=count


class CompletionWakeup:
    """Future completion wakes the existing owner thread; never publishes/sends."""
    def __init__(self):
        self.event = threading.Event()
    def attach(self, future):
        future.add_done_callback(lambda _: self.event.set())
    def wait(self, future, timeout):
        # Clear before rechecking the future to close the completion race.
        self.event.clear()
        if future is not None and future.done():
            return
        self.event.wait(timeout)


def wait_owned_recording_ready(hub, stop_event, *, external_guard=lambda: None, timeout_s=5.):
    deadline = time.monotonic()+timeout_s
    while True:
        external_guard()
        require(not stop_event.is_set(), 'Stopped while awaiting native recording readiness')
        state = hub.status()['recording']
        require(state.get('path') == hub.owned_path and state.get('active') is True,
                'Owned recording identity/active state changed')
        require(state.get('error') is None and state.get('overflows') == 0, 'Owned recording error/overflow')
        count = state.get('samples')
        require(type(count) is int and count >= 0, 'Invalid actual recording sample counter')
        if count > 0:
            return {'samples': count, 'owned_path': hub.owned_path}
        require(time.monotonic() < deadline, 'Native recording readiness timed out')
        stop_event.wait(.02)


def infer_and_prepare(core, model, captured, request, hub, journal):
    """Single background worker owns inference and remote TimeQP preparation."""
    result = model.infer(captured, request['rtc'], observation_mode=FRESH,
                         fresh_validator=lambda c: hub.validate_fresh(c, journal))
    # infer returns only after model/RTC/input identity validation. Exceptions
    # in that path are still fatal, including for a locally invalidated request.
    result['model_response_validated'] = True
    pause_info = getattr(core, 'pause_discard_info', lambda token: None)
    discarded = pause_info(request['token'])
    if discarded is not None:
        result['reference_pause_discard'] = discarded
        result['eligible_for_diagnostic_commit'] = False
        return result
    if result['eligible_for_diagnostic_commit']:
        try:
            candidate = core.prepare_candidate(request['token'], result['response'], result['validated_ns'])
            result['timeqp_prepared_ns'] = candidate.prepare_finished_ns
            if getattr(candidate,'candidate',None) is not None:
                result['prepared_reference_diagnostics'] = public_receipt(candidate.candidate.diagnostics)
            result['_prepared_candidate'] = candidate
        except Exception as exc:
            result['timeqp_preparation_error'] = type(exc).__name__+': '+str(exc)
            if hasattr(exc,'reference_pause_discard'):
                result['reference_pause_discard'] = exc.reference_pause_discard
            if hasattr(exc,'splice_diagnostics'):
                result['soft_guided_splice_rejection'] = public_receipt(exc.splice_diagnostics)
            result['eligible_for_diagnostic_commit'] = False
    discarded = pause_info(request['token'])
    if discarded is not None and (not result.get('timeqp_preparation_error') or
                                 result.get('reference_pause_discard') is not None):
        result['reference_pause_discard'] = discarded
        result['eligible_for_diagnostic_commit'] = False
    return result


def dispatch_prepare_and_infer(core, model, hub, journal, prompt, initial_capture,
                               request_metadata, stop_event):
    """All observation HTTP, validation, model and TimeQP work is off the tick."""
    try:
        require(not stop_event.is_set(), 'Stopped before background capture')
        request_metadata['capture_started_ns']=time.monotonic_ns()
        captured = initial_capture if initial_capture is not None else hub.capture(prompt, journal)
        request_metadata['capture_completed_ns']=time.monotonic_ns()
        request_metadata['observation_anchor_ns']=captured.anchor_ns
        request_metadata['observation_age_at_capture_completion_ms']=(
            request_metadata['capture_completed_ns']-captured.anchor_ns)/1e6
        request_metadata['reused_initial_capture']=initial_capture is not None
        request_metadata['captured_snapshot'] = prepare_snapshot(captured.snapshot)
        request_metadata['observation_sha256'] = captured.snapshot_sha256
        require(not stop_event.is_set(), 'Stopped after background capture')
        request_metadata['prepare_request_started_ns']=time.monotonic_ns()
        request = core.prepare_request(captured.anchor_ns, time.monotonic_ns())
        request_metadata['prepare_request_completed_ns']=time.monotonic_ns()
        request_metadata.update(request)
        require(not stop_event.is_set(), 'Stopped before background model request')
        result = infer_and_prepare(core, model, captured, request, hub, journal)
        result['captured_snapshot'] = request_metadata['captured_snapshot']
    except Exception as exc:
        result = {'eligible_for_diagnostic_commit': False,
                  'pipeline_error': type(exc).__name__+': '+str(exc),
                  'captured_snapshot': request_metadata.get('captured_snapshot'),
                  'observation_sha256': request_metadata.get('observation_sha256'),
                  'rtc': request_metadata.get('rtc'),
                  'physical_execution_admissible': False}
        if hasattr(exc, 'reference_coverage_hold'):
            result['reference_coverage_hold'] = exc.reference_coverage_hold
        if isinstance(exc, ReferenceCaptureDeferred):
            result['reference_capture_deferred'] = exc.reference_capture_deferred
            result['model_response_validated'] = False
    # SessionEvidence bounds/freeze accounts metadata without a pre-publication
    # JSON pass. Actual serialization remains on the USB worker.
    result['prepublication_diagnostic_json_serialization'] = False
    result['observation_timing']={k:request_metadata.get(k) for k in (
        'capture_started_ns','capture_completed_ns','observation_anchor_ns',
        'observation_age_at_capture_completion_ms','reused_initial_capture',
        'prepare_request_started_ns','prepare_request_completed_ns')}
    result['worker_result_ready_ns'] = time.monotonic_ns()
    return result


def paired_first_packets(model, hub, journal, *, prompt, trial_index, session_prefix,
                         stop_event, save, external_guard=lambda: None):
    """Same real bytes and seed; two leases, no rewritten input timestamps."""
    captured = hub.capture(prompt, journal)
    save('first-packet-observation.json', captured.snapshot)
    anchor = captured.payload['state']
    rows = []
    for mode, enabled in (('native_first', False), ('guided_hold35', True)):
        external_guard()
        require(not stop_event.is_set(), 'Pair cancelled before new inference')
        model.acquire(session_prefix + '-' + mode, trial_index)
        try:
            rtc = make_rtc([list(anchor) for _ in range(35)] if enabled else [],
                           enabled=enabled, trial_index=trial_index, request_index=0)
            result = model.infer(captured, rtc, observation_mode=FROZEN)
            save(mode + '-response.json', result)
            external_guard()
            require(not stop_event.is_set() and not result.get('cancelled_result'),
                    'Cancelled pair response is retained but cannot pass')
            rows.append({'mode': mode, 'snapshot_sha256': captured.snapshot_sha256,
                         'observation_payload_sha256': captured.payload_sha256,
                         'trial_seed': 42 + trial_index, 'request_index': 0,
                         'response': result})
        finally:
            drain_before_unlock(model, emit=emit)
            if getattr(model, 'last_attempt', None):
                save(mode + '-last-attempt.json', model.last_attempt)
    require(rows[0]['snapshot_sha256'] == rows[1]['snapshot_sha256'], 'Pair inputs changed')
    return {'status': 'REAL_INPUT_PAIR_COLLECTED_NOT_MOTION_ACCEPTANCE', 'pairs': rows,
            'same_input_and_seed': True, 'observation_mode': FROZEN,
            'physical_execution_admissible': False}


def continuous_window(core, model, hub, journal, *, prompt, duration, stop_event,
                      external_guard, save, evidence, initial_capture=None, feedback_provider=None):
    """Actual-feedback 50Hz NO_SEND loop, streaming the same evidence as APPLY."""
    require(type(duration) in (int,float) and math.isfinite(duration) and 1 <= duration <= 60,
            'Diagnostic duration must be a finite number in 1..60 seconds')
    start = time.monotonic_ns(); end = start + round(duration * 1e9)
    tick_due = request_due = start
    capture_retry_after_ns = 0
    compute = deque(maxlen=4096); intervals = deque(maxlen=4096)
    tick_count = packet_count = cycles_over = intervals_over = 0
    last_tick_ns = None; future = prepared = None; error = attempt = None
    completion_wakeup = CompletionWakeup()
    evidence.check_alive()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='v2-real-model-nosend')
    feedback_provider = feedback_provider or hub.feedback

    def retain_packet(row):
        nonlocal packet_count
        packet_count += 1
        require(packet_count <= math.ceil((duration+5)*18/14)+8, 'NO_SEND policy count guard exceeded')
        evidence.append_packet(row)

    def schedule_request(now):
        nonlocal request_due, future, prepared, initial_capture
        if request_due is None and future is None:
            request_due = core.next_request_due_ns(now)
        if future is None and request_due is not None and now >= max(request_due, capture_retry_after_ns) and now < end:
            require(not stop_event.is_set(), 'Stopped before background request')
            prepared = {}
            future = pool.submit(dispatch_prepare_and_infer, core, model, hub, journal, prompt,
                                 initial_capture, prepared, stop_event)
            completion_wakeup.attach(future)
            initial_capture = None

    try:
        while time.monotonic_ns() < end and not stop_event.is_set():
            now = time.monotonic_ns()
            if now < tick_due and not (future is not None and future.done()):
                completion_wakeup.wait(future, min((tick_due-now)/1e9, .005)); continue
            external_guard(); evidence.check_alive()
            if stop_event.is_set():
                model.cancel(); break
            model.check_alive()
            if getattr(core, 'coverage_hold', None) is not None and future is None:
                break
            if future is not None and future.done():
                result = future.result(); candidate = result.pop('_prepared_candidate', None)
                result['captured_snapshot'] = prepared.get('captured_snapshot')
                finished_context = prepared; future = prepared = None
                try:
                    external_guard()
                    require(not stop_event.is_set(), 'Cancelled before diagnostic commit')
                    if result.get('reference_coverage_hold') is not None:
                        require(result['reference_coverage_hold'] == core.coverage_hold, 'unowned coverage hold')
                        result['discarded_without_commit'] = True
                    elif result.get('reference_capture_deferred') is not None:
                        require(not finished_context.get('token') and core.pending is None, 'deferred capture owns a request')
                        capture_retry_after_ns = time.monotonic_ns() + 20_000_000
                        result['discarded_without_commit'] = True
                    elif time.monotonic_ns() >= end:
                        result['discarded_after_stop'] = True
                    elif (drain_pause := getattr(core, 'drain_diagnostic_pause', None)) is not None and (
                            recovery := drain_pause(finished_context, result)) is not None:
                        result['reference_pause_recovery'] = recovery
                        result['discarded_without_commit'] = True
                    else:
                        require(result['eligible_for_diagnostic_commit'], result.get('pipeline_error')
                                or result.get('timeqp_preparation_error') or 'Late/cancelled result cannot commit')
                        require(result['observation_sha256'] == finished_context['observation_sha256'],
                                'In-flight observation binding changed')
                        result['publication_attempt_started_ns'] = time.monotonic_ns()
                        result['queue_acceptance'] = core.commit_candidate(finished_context['token'], candidate)
                        result['publication_returned_ns'] = time.monotonic_ns()
                except BaseException as exc:
                    result['publication_rejected_ns'] = time.monotonic_ns()
                    result['commit_error'] = type(exc).__name__ + ': ' + str(exc)
                    result['discarded_after_stop'] = 'queue_acceptance' not in result
                    raise
                finally:
                    retain_packet(result)
                request_due = None
                if time.monotonic_ns() >= end:
                    break
                schedule_request(time.monotonic_ns())
            # Wakeup may publish between ticks; feedback/MPC remain on the grid.
            if time.monotonic_ns() < tick_due:
                continue
            feedback = feedback_provider(); actual = time.monotonic_ns()
            if actual >= end:
                break
            attempt = {'actual_ns': actual, 'previous_success_ns': last_tick_ns,
                       'attempt_gap_ms': (actual-last_tick_ns)/1e6 if last_tick_ns is not None else None,
                       'feedback_source_age_ms': (actual-feedback['source_ns'])/1e6}
            revision = tuple(feedback.get('source_ns_by_component', {}).get(k, feedback['source_ns'])
                             for k in ('q12','q34','q56','gripper'))
            tick = core.tick(actual, feedback['q_rad_m'], feedback['source_ns'], feedback_revision=revision)
            tick['scheduled_tick_ns'] = tick_due; tick['scheduler_lateness_ms'] = (actual-tick_due)/1e6
            if last_tick_ns is not None:
                interval = (actual-last_tick_ns)/1e6; intervals.append(interval)
                intervals_over += int(interval > 20)
            last_tick_ns = actual; tick_count += 1
            elapsed = tick['cycle_compute_ms']; compute.append(elapsed); cycles_over += int(elapsed > 20)
            evidence.append_tick(tick)
            require(tick_count <= math.ceil(duration*50)+50, 'NO_SEND tick count guard exceeded')
            tick_due += 20_000_000
            if actual >= tick_due:
                tick_due += ((actual-tick_due)//20_000_000+1)*20_000_000
            schedule_request(actual)
        if stop_event.is_set():
            error = 'OPERATOR_CANCELLED'
        elif getattr(core, 'coverage_hold', None) is not None:
            error = 'REFERENCE_HELD_INSUFFICIENT_COVERAGE: 原轨迹不足 35 个参考点，本轮无运动验收已结束'
    except BaseException as exc:
        error = type(exc).__name__ + ': ' + str(exc)
    finally:
        stop_receipt = core.stop(error or 'WINDOW_COMPLETE')
        drain_receipt = drain_before_unlock(model, future, emit=emit)
        if future is not None:
            try:
                late = future.result(); late.pop('_prepared_candidate', None)
                late['discarded_after_stop'] = True
                late['captured_snapshot'] = prepared.get('captured_snapshot')
            except BaseException as exc:
                late = {'discarded_after_stop': True, 'error_type': type(exc).__name__,
                        'captured_snapshot': prepared.get('captured_snapshot'),
                        'rtc': prepared.get('rtc'), 'observation_sha256': prepared.get('observation_sha256')}
            try:
                retain_packet(late)
            except BaseException as exc:
                error = error or 'Evidence final packet: ' + repr(exc)
        pool.shutdown(wait=True)
    if getattr(model, 'last_attempt', None):
        save('continuous-last-attempt.json', model.last_attempt)
    if error is None and core.accepted_count < 2:
        error = 'INSUFFICIENT_ACCEPTED_PACKETS_FOR_CONTINUITY'
    def stats(values):
        import numpy as np
        return {} if not values else {'n': len(values), 'p50': float(np.percentile(values, 50)),
                                     'p95': float(np.percentile(values, 95)), 'max': max(values)}
    result = {'status': 'REAL_IO_NO_SEND_WINDOW_COMPLETE' if error is None else 'NO_SEND_FAILED_OR_CANCELLED',
              'error': error, 'reference_coverage_hold': getattr(core, 'coverage_hold', None),
              'requested_window_s': duration, 'tick_count': tick_count,
              'packet_count': packet_count, 'accepted_packets': core.accepted_count,
              'last_tick_attempt': attempt, 'compute_ms': stats(compute),
              'observed_tick_interval_ms': stats(intervals), 'cycles_over_20ms': cycles_over,
              'observed_intervals_over_20ms': intervals_over,
              'metric_percentile_scope': 'LAST_4096_SAMPLES_FULL_RECORDS_STREAMED',
              'publication_wakeup': 'FUTURE_CALLBACK_EVENT_SAME_CONTROL_THREAD',
              'control_send_period_ns_unchanged': 20_000_000,
              'stop': stop_receipt, 'drain': drain_receipt, 'evidence_finalization_pending': True,
              'physical_closed_loop_validated': False, 'robot_commands': 0,
              'guidance_semantics': 'DIAGNOSTIC_UNSENT_REFERENCE', 'real_time_50Hz_admission': False}
    save('continuous-summary.json', result)
    if error:
        raise RuntimeError(error)
    return result


def execute(binding, cfg, storage, args):
    """Own shared resources until actual drain and USB persistence finish."""
    from agilex_control.h50_journal import H50USBJournal
    from agilex_control.time_contract import capture_clock_domain
    from .long_window_evidence import SessionEvidence
    root = Path(binding['runtime_root'])
    run_id = datetime.now().strftime('%Y-%m-%d-%H-%M-%S') + '-v2-nosend-' + uuid.uuid4().hex[:8]
    run_dir = root / 'runs' / run_id
    stop_event = threading.Event()
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())
    def eof_watch():
        import sys, select
        fd = sys.stdin.fileno()
        while not stop_event.is_set():
            ready, _, _ = select.select([fd], [], [], .2)
            if ready and not os.read(fd, 1):
                stop_event.set()
                return
    if args.stop_on_stdin_eof:
        threading.Thread(target=eof_watch, daemon=True).start()
    with owned_session(cfg, storage=storage):
        check_stop(cfg, root/'STOP-NO_SEND')
        before = can_counters()
        model = PinnedLoadedModel(binding['model']['precision'], binding['model']['step'])
        model_receipt = model.bind()
        verify_model(binding, model_receipt)
        hub = ExistingHub(cfg)
        state = hub.status()
        from agilex_control.fp32_shadow_run import _hub_no_send
        hub_zero_before = _hub_no_send(state)
        require(not state['recording']['active'] and not state['recording']['saving'], 'Hub is occupied')
        run_dir.mkdir(parents=True, exist_ok=False)
        journal = H50USBJournal(cfg, run_dir)
        episode_journal = None
        feedback_cache = None
        evidence = None
        result = {'schema': 'tiger-v2-real-io-nosend-v1', 'model': model_receipt,
                  'mode': args.mode, 'run_dir': str(run_dir), 'APPLY_supported': False,
                  'physical_motion_started': False, 'status': 'STARTED', 'SDK_instances': 0}
        result['hub_zero_before'] = hub_zero_before
        def save(name, value):
            journal.write_json(run_dir/name, public_receipt(value))
        save('operation.json', result)
        emit({'status': 'NO_SEND_STARTED', 'run_dir': str(run_dir), 'model': binding['model']})
        error = None
        last_guard = [0.]
        monitor_closed = threading.Event()
        monitor_error = []
        monitor_last_success = [time.monotonic()]
        def guard():
            check_stop(cfg, root/'STOP-NO_SEND')
            if time.monotonic()-last_guard[0] >= .25:
                storage.verify_usb(cfg)
                journal.verify()
                verify_zero_tx(before, can_counters())
                last_guard[0] = time.monotonic()
        def monitor():
            while not monitor_closed.wait(.1):
                try:
                    guard()
                    monitor_last_success[0] = time.monotonic()
                    if stop_event.is_set():
                        model.cancel()
                except Exception as exc:
                    monitor_error.append(type(exc).__name__+': '+str(exc))
                    stop_event.set()
                    model.cancel()
                    return
        monitor_thread = threading.Thread(target=monitor, name='v2-runtime-guard', daemon=True)
        monitor_thread.start()
        def fast_guard():
            require(not monitor_error and not stop_event.is_set(), 'Background guard stopped/faulted')
            require(time.monotonic()-monitor_last_success[0] <= .75, 'USB/STOP monitor stalled')
            if feedback_cache is not None:
                feedback_cache.check_alive()
            if evidence is not None:
                evidence.check_alive()
        try:
            evidence = SessionEvidence(cfg, run_dir)
            evidence.start()
            hub.begin_owned_recording('V2 NO_SEND ' + args.prompt)
            result['recording_ready'] = wait_owned_recording_ready(hub, stop_event, external_guard=guard)
            episode_journal = H50USBJournal(cfg, hub.owned_path)
            if args.mode == 'compare-first':
                result['comparison'] = paired_first_packets(model, hub, episode_journal,
                    prompt=args.prompt, trial_index=args.trial_index, session_prefix=run_id,
                    stop_event=stop_event, save=save, external_guard=guard)
            else:
                from .v2_core import V2Core
                from .feedback_cache import FeedbackCache
                feedback_cache = FeedbackCache(hub).start()
                feedback_cache.wait_ready(timeout_s=3., stop_event=stop_event)
                domain = capture_clock_domain()
                model.acquire(run_id, args.trial_index)
                captured = hub.capture(args.prompt, episode_journal)
                core = V2Core(captured.payload['state'], binding['bounds_rad_m'], captured.anchor_ns,
                    clock_domain=(domain['host_id'], domain['boot_id'], domain['clock_name']),
                    mode=args.bootstrap, trial_index=args.trial_index,
                    solver_dir=binding['solver_dir'], gripper_lookahead_ms=args.gripper_lookahead_ms,
                    timeqp_instance_id=binding['timeqp']['instance_id'])
                result['position_contract'] = core.position_contract.receipt()
                result['continuous'] = continuous_window(core, model, hub, episode_journal,
                    prompt=args.prompt, duration=args.duration, stop_event=stop_event,
                    external_guard=fast_guard, save=save, initial_capture=captured,
                    feedback_provider=feedback_cache.read_latest, evidence=evidence)
            require(not monitor_error and not stop_event.is_set(), 'NO_SEND stopped/guard failed')
            result['status'] = 'REAL_IO_NO_SEND_COMPLETED_NOT_MOTION_ADMISSION'
        except BaseException as exc:
            error = exc
            result.update(status='FAILED_RETAINED', error=type(exc).__name__+': '+str(exc))
        finally:
            result['model_drain'] = drain_before_unlock(model, emit=emit)
            if feedback_cache is not None:
                while True:
                    try:
                        result['feedback_cache'] = feedback_cache.close(timeout_s=3.)
                        break
                    except RuntimeError:
                        emit({'event':'FEEDBACK_HTTP_DRAIN_RETAINS_OWNERSHIP'})
            monitor_closed.set()
            monitor_thread.join(3.)
            result['monitor_errors'] = monitor_error
            try:
                if hub.owned_path is not None:
                    result['recording'] = hub.end_owned_recording()
                require(result.get('feedback_cache', {}).get('healthy', True), 'Feedback sampler failed')
                result['zero_tx'] = verify_zero_tx(before, can_counters())
                result['hub_zero_after'] = _hub_no_send(hub.status())
                require(not monitor_thread.is_alive(), 'Guard monitor did not exit')
                require(source_files(root) == binding['runtime_sources'], 'Runtime source changed')
                verify_files(binding['canonical_root'], binding['canonical_sources'])
                verify_files(binding['schema_root'], binding['schema_sources'])
                require(sha(binding['config_path']) == binding['config_sha256'], 'Original config changed')
                result['protected_files_unchanged'] = True
            except BaseException as exc:
                error = error or exc
                result.update(status='FAILED_RETAINED', finalization_error=type(exc).__name__+': '+str(exc))
            if evidence is not None:
                try:
                    result['evidence'] = evidence.close()
                    require(result['evidence'].get('complete') is True, 'NO_SEND evidence incomplete')
                except BaseException as exc:
                    error = error or exc
                    result.update(status='FAILED_RETAINED', evidence_error=repr(exc))
            else:
                error = error or RuntimeError('Evidence session did not start')
                result.update(status='FAILED_RETAINED', evidence_error='Evidence session did not start')
            if 'continuous' in result:
                result['continuous']['evidence_finalization_pending'] = False
                result['continuous']['evidence_complete'] = result.get('evidence', {}).get('complete', False)
                if error:
                    result['continuous']['status'] = 'NO_SEND_FAILED_OR_CANCELLED'
            try:
                if 'continuous' in result:
                    save('continuous-summary.json', result['continuous'])
                save('operation.json', result)
            finally:
                try:
                    if episode_journal is not None:
                        episode_journal.close()
                finally:
                    journal.close()
        emit(result)
        if error:
            raise RuntimeError('NO_SEND failed; inspect its preserved operation receipt') from error
        return result
