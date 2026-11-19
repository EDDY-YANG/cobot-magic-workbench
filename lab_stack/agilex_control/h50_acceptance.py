"""Audit finished real NO_SEND trials and publish metadata under a new lease.

No camera/CAN reader, SDK writer, or inference is created here. Outputs stay in
the verified episode's USB evidence directory. Native media export uses Linux
directory descriptors; detach cannot turn an output mkdir into an SSD write.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from .h50_approved_contract import require_approved_config, loads_unique_json, acceptance_semantics

POST_SAVE_FIELDS = {'result_ram_accepted_monotonic_ns', 'end_to_end_result_ram_ms',
                    'deadline_met', 'ready_for_commit_monotonic_ns', 'end_to_end_ready_ms'}
MAX_JSON = 16 * 1024 * 1024


def _finish_recording_event(session, step, finalized=None, error=None):
    """Close the existing ordered metadata publisher after the real USB audit.

    Offline re-audits have no live publisher and cannot change the live task.
    Artifact preservation is separate from performance/physical task success.
    """
    publisher = getattr(session, '_task_publisher', None)
    if publisher is None:
        return {'status': 'OFFLINE_NO_LIVE_PUBLISHER'}
    compact = finalized.get('compact', {}) if isinstance(finalized, dict) else {}
    saved = compact.get('usb_finalflush_validated') is True and error is None
    event = 'recording_completed' if saved else 'recording_failed'
    evidence = {'saved': saved, 'requires_qc': False if saved else True,
        'usb_finalflush_validated': saved, 'checkpoint_step': step,
        'owned_episode': compact.get('episode'), 'artifact_audit_report': compact.get('report'),
        'audit_status': compact.get('status', 'FAILED'),
        'scope': 'artifact preservation only; not policy performance or physical task success'}
    if error is not None:
        evidence['reason'] = str(error)[:500]
    elif not saved:
        evidence['reason'] = 'Independent USB file/native media audit did not pass'
    delivery = {'event': event, 'receipt': evidence, 'status': 'PENDING'}
    session.report['h50_final_recording_event'] = delivery
    try:
        require(getattr(publisher, 'closed', False) is False,
                'recording metadata publisher closed before independent artifact audit')
        publisher.publish(event, **evidence)
    finally:
        delivery['delivery'] = publisher.close()
    status = delivery['delivery']
    require(status.get('errors') == [] and status.get('pending') == 0 and status.get('worker_stopped') is True,
            'final recording metadata delivery incomplete')
    delivery['status'] = 'DELIVERED' if status.get('enabled') is True else 'DISABLED'
    return delivery


def _finalizes_recording_event(function):
    @wraps(function)
    def finalize(cfg, session, step, *args, **kwargs):
        try:
            result = function(cfg, session, step, *args, **kwargs)
        except BaseException as exc:
            try:
                _finish_recording_event(session, step, error=type(exc).__name__ + ': ' + str(exc))
            except Exception as delivery_error:
                session.report['h50_final_recording_event_error'] = repr(delivery_error)
            raise  # Preserve the original audit/USB error.
        delivery = _finish_recording_event(session, step, finalized=result)
        result['full']['final_recording_event'] = delivery
        return result
    return finalize


def require(condition, message):
    if not condition:
        raise ValueError('H50 acceptance: ' + message)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def percentile(values, percent):
    ordered = sorted(values)
    require(bool(ordered), 'no measured latency samples')
    index = (len(ordered) - 1) * percent / 100
    low = int(index)
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (index - low)


def _hooks():
    from .storage import verify_usb, open_usb_directory, write_json
    from .h50_runtime import ModelRuntime, validate_health, robot_preparation_source
    from .remote_fp32 import validate_snapshot, validate_result
    from .time_contract import capture_clock_domain
    from . import native_capture_audit
    return dict(verify=verify_usb, open_dir=open_usb_directory, write=write_json,
                runtime=ModelRuntime, health=validate_health, snapshot=validate_snapshot,
                result=validate_result, clock=capture_clock_domain, native=native_capture_audit.check_recording,
                preparation_source=robot_preparation_source)


class USBAnchor:
    def __init__(self, cfg, episode, hooks):
        self.cfg, self.h, self.episode = cfg, hooks, Path(episode)
        root = Path(cfg['usb'])
        require(cfg.get('usb_selection') and self.episode.is_absolute() and root.is_absolute(),
                'frozen absolute USB identity required')
        require('..' not in self.episode.parts and self.episode != root and self.episode.is_relative_to(root),
                'episode outside selected USB')
        require(os.name == 'posix' and Path('/proc/self/fd').is_dir(), 'Linux anchored storage required')
        self.fd = self.output_fd = None
        self.h['verify'](cfg)
        self.fd = self.h['open_dir'](self.episode, cfg)
        self.identity = os.fstat(self.fd)
        self.output = None

    def verify(self):
        require(Path(self.h['verify'](self.cfg)) == Path(self.cfg['usb']), 'USB verifier root differs')
        fd = self.h['open_dir'](self.episode, self.cfg)
        try:
            current = os.fstat(fd)
            require((current.st_dev, current.st_ino) == (self.identity.st_dev, self.identity.st_ino),
                    'episode directory detached or replaced')
        finally:
            os.close(fd)

    def relative(self, path):
        path = Path(path)
        if path.is_absolute():
            require(path.is_relative_to(self.episode), 'evidence path outside episode')
            path = path.relative_to(self.episode)
        require(path.parts and '..' not in path.parts, 'invalid evidence-relative path')
        return path

    def _open(self, path):
        path = self.relative(path)
        fd = os.dup(self.fd)
        try:
            for part in path.parts[:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = following
                require(os.fstat(fd).st_dev == self.identity.st_dev, 'nested filesystem differs')
            result = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            info = os.fstat(result)
            if info.st_dev != self.identity.st_dev or not stat.S_ISREG(info.st_mode):
                os.close(result)
                raise ValueError('Evidence is not a regular file on the selected USB')
            return result
        finally:
            os.close(fd)

    def read(self, path, *, limit=MAX_JSON, flush=False):
        self.verify()
        descriptor = self._open(path)
        with os.fdopen(descriptor, 'rb') as handle:
            before = os.fstat(handle.fileno())
            require(before.st_size <= limit, 'evidence file exceeds bounded read')
            if flush:
                os.fsync(handle.fileno())
            data = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
        require(len(data) <= limit and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), 'source changed during read')
        return data

    def json(self, path):
        raw = self.read(path)
        return loads_unique_json(raw), hashlib.sha256(raw).hexdigest()

    def journal_names(self):
        self.verify()
        return sorted(name for name in os.listdir(self.fd)
                      if re.fullmatch(r'\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-fp32-shadow-[0-9a-f]{32}', name))

    def create_output(self):
        self.verify()
        leaf = datetime.now().strftime('%Y-%m-%d-%H-%M-%S') + '-h50-acceptance-' + uuid.uuid4().hex[:8]
        self.output = self.episode / 'evidence' / leaf
        self.output_fd = self.h['open_dir'](self.output, self.cfg, create=True, exclusive_final=True)
        require(os.fstat(self.output_fd).st_dev == self.identity.st_dev, 'output filesystem differs')
        return self.output

    def write(self, name, value):
        self.verify()
        require(self.output is not None and Path(name).name == name, 'output must be a leaf under anchored evidence')
        path = self.output / name
        self.h['write'](path, value, self.cfg)
        self.verify()
        return str(path)

    def close(self):
        for fd in (self.output_fd, self.fd):
            if fd is not None:
                os.close(fd)
        self.output_fd = self.fd = None


def zero_weight_proof(health):
    proof = health.get('zero_weight_equivalence', {})
    require(proof.get('schema') == 'tiger-native32-zero-weight-equivalence-v1' and proof.get('pass') is True,
            'actual worker zero-weight equivalence proof missing')
    require(all(finite(proof.get(key)) and proof[key] == 0 for key in
                ('absolute_tolerance', 'relative_tolerance', 'max_absolute_difference')),
            'worker equivalence must be bitwise, zero tolerance')
    require(proof.get('implementation_sha256') == health.get('rtc', {}).get('implementation_sha256'),
            'worker equivalence uses another RTC implementation')
    for key in ('native_raw_bytes_sha256', 'rtc_raw_bytes_sha256', 'native_array_file_sha256', 'rtc_array_file_sha256'):
        require(isinstance(proof.get(key), str) and re.fullmatch('[0-9a-f]{64}', proof[key]),
                'worker equivalence array hash missing: ' + key)
    require(proof['native_raw_bytes_sha256'] == proof['rtc_raw_bytes_sha256'], 'raw native/RTC bytes differ')
    return deepcopy(proof)


def gpu_proof(receipt, source):
    release = receipt.get('lease_release', {})
    require(release.get('released') is True and release.get('session_id') == source, 'original model lease was not released')
    gpu = release.get('gpu_telemetry', {})
    require(gpu.get('schema') == 'tiger-whole-gpu-memory-v1' and gpu.get('status') == 'PASS'
            and gpu.get('session_id') == source and gpu.get('source') == 'NVML device0 whole memory',
            'actual source-lease whole-device NVML evidence missing/failed')
    require(gpu.get('complete_ring_coverage') is True and gpu.get('sample_fresh') is True
            and type(gpu.get('samples')) is int and gpu['samples'] > 0
            and gpu.get('failed_samples') == 0 and gpu.get('model_failed_samples') == 0,
            'NVML samples incomplete or include failures')
    for key in ('last_sample_age_s', 'max_sample_gap_s'):
        require(finite(gpu.get(key)) and 0 <= gpu[key] <= .3, 'NVML freshness/gap failed: ' + key)
    require(gpu.get('sample_period_s') == .1 and gpu.get('sample_freshness_limit_s') == .3, 'NVML sampling contract differs')
    for key in ('lease_peak_mib', 'model_peak_mib'):
        require(finite(gpu.get(key)) and gpu[key] > 0, 'NVML peak unknown: ' + key)
    for key in ('csv_sha256', 'json_sha256'):
        require(isinstance(gpu.get(key), str) and re.fullmatch('[0-9a-f]{64}', gpu[key]), 'NVML receipt hash absent')
    # These files are on the model server; /acceptance/submit rehashes them.
    return deepcopy(gpu), max(gpu['lease_peak_mib'], gpu['model_peak_mib'])


def check_run_summary(receipt, step):
    require(receipt.get('schema') == 'tiger-h50-async-run-v1' and receipt.get('mode') == 'NO_SEND'
            and receipt.get('status') in ('NO_SEND_COMPLETED_REQUIRES_ROOT_AUDIT', 'NO_SEND_COMPLETED_WITH_FAILURES')
            and receipt.get('step') == step,
            'run was not a completed NO_SEND audit candidate')
    require(receipt.get('requested_measured') == 100 and receipt.get('requested_warmup') == 3
            and receipt.get('completed_measured') == 100, 'not exactly3 warmup and100 measured samples')
    approved = require_approved_config(receipt.get('contract', {}))
    require(receipt.get('timing_basis') == approved['commit_timing_basis']
            and receipt.get('storage_semantic_version') == approved['storage_semantic_version'],
            'old durable-time semantics cannot be used as new RAM commit-ready evidence')
    require(receipt.get('cleanup_errors') == [] and receipt.get('module_CAN_api_calls') == 0
            and receipt.get('model_outputs_never_dispatched') is True
            and receipt.get('physical_unsent_reference_never_replaced_by_model') is True,
            'NO_SEND/cleanup guarantees missing')
    delta, before, after = receipt.get('interface_tx_delta'), receipt.get('interface_tx_before'), receipt.get('interface_tx_after')
    require(isinstance(delta, dict) and bool(delta) and isinstance(before, dict) and isinstance(after, dict)
            and set(delta) == set(before) == set(after), 'interface TX counter sets incomplete')
    for role, change in delta.items():
        require(type(change) is int and change == 0 and type(before[role].get('tx_packets')) is int
                and after[role].get('tx_packets') == before[role]['tx_packets'], 'TX changed/unknown: ' + role)
    require(receipt.get('sender_stopped') is True and not receipt.get('sender_failure'), 'NO_SEND sender/evidence thread incomplete')
    for first, last in (('record_before_started_ns', 'record_before_completed_ns'),
                        ('post_record_started_ns', 'post_record_completed_ns')):
        require(type(receipt.get(first)) is int and type(receipt.get(last)) is int
                and receipt[last] - receipt[first] >= 2_000_000_000, 'actual2s pre/post recording interval incomplete')
    final = receipt.get('recording_finalization', {})
    require(final.get('completed') is True and final.get('healthy') is True, 'native recording not completely finalized')
    events = receipt.get('events_final', {})
    require(events.get('finished') is True and events.get('writer_alive') is False
            and events.get('timing_evidence_incomplete') is False and events.get('queued') == 0
            and type(events.get('written')) is int and events['written'] > 0 and events.get('accepted') == events['written'],
            'asynchronous reference/decision log was not completely flushed')
    require(all(events.get(key) == 0 for key in ('overflows', 'rejected', 'sequence_gaps', 'tail_unwritten_sequences', 'producer_errors'))
            and not events.get('writer_error') and events.get('flush_timeout') is False, 'async event evidence has errors')
    return events


def audit_dryrun_preparation(cfg, receipt, attempt, prepared, raw, anchor, expected_source,
                             commit, previous_reference=None):
    """Rebuild the diagnostic moving chain; physical output remains UNSENT HOLD."""
    from .h50_async import Node, Plan, tail_plan, label_offset_ns,gripper_command_projection,joint_command_projection
    from .hardware import DOCUMENTED_COMMAND_LIMITS_DEG
    scope = 'DIAGNOSTIC_CHAIN_REAL_PLANS_NO_PHYSICAL_COMMIT'
    source = {'executor': expected_source['h50_deployment_sha256'], 'core': expected_source['h50_async_sha256']}
    for value in (receipt, attempt, prepared):
        require(value.get('plan_preparation_scope') == scope and value.get('plan_preparation_source_sha256') == source,
                'old HOLD-only or stale executor/core preparation evidence')
    index = attempt['request_index']
    phase = 'NATIVE_BOOTSTRAP' if index == 0 else 'RTC_CONTINUATION'
    for value in (attempt, prepared, commit):
        require(value.get('inference_phase') == phase and value.get('reference_scope') == 'DIAGNOSTIC_UNSENT_REFERENCE',
                'diagnostic phase/reference scope differs')
    require(attempt.get('plan_preparation_status') == 'COMPLETE', 'real diagnostic tail-plan preparation incomplete')
    context = attempt.get('rtc_context', {})
    approved = require_approved_config(cfg)
    old, candidate = prepared.get('old_reference', {}), prepared.get('candidate_reference', {})
    require(prepared.get('schema') == 'tiger-h50-prepared-reference-v2'
            and canonical(context) == canonical(prepared.get('rtc_context'))
            and context.get('observation_monotonic_ns') == anchor
            and context.get('cut_monotonic_ns') == anchor + label_offset_ns(12)
            and context.get('inference_phase') == phase
            and context.get('reference_scope') == 'DIAGNOSTIC_UNSENT_REFERENCE'
            and context.get('execution_contract_version') == approved['execution_contract_version']
            and context.get('startup_semantic_version') == approved['startup_semantic_version']
            and context.get('no_send_semantic_version') == approved['no_send_semantic_version']
            and context.get('timing_contract_sha256') == expected_source['approved_contract_sha256']
            and prepared.get('old_plan_id') == context.get('old_plan_id') == old.get('plan_id')
            and prepared.get('candidate_plan_id') == candidate.get('plan_id'),
            'diagnostic reference/cut/source binding differs')
    if previous_reference is None:
        require(canonical(old) == canonical(receipt.get('initial_planning_reference')), 'native bootstrap lacks its original planning HOLD')
        require(all(row.get('q_deg_m') == receipt['initial_actual_q_deg_m'] and row.get('v_deg_m_s') == [0.] * 7
                    for row in old.get('nodes', [])), 'native bootstrap did not start from its explicit HOLD reference')
    else:
        require(canonical(old) == canonical(previous_reference), 'RTC old plan is not the previously accepted diagnostic candidate')
    physical = prepared.get('physical_unsent_hold_reference', {})
    require(canonical(physical) == canonical(receipt.get('initial_physical_reference'))
            and canonical(physical) == canonical(receipt.get('physical_final_unsent_reference'))
            and len(physical.get('nodes', [])) >= 2
            and all(row.get('q_deg_m') == receipt['initial_actual_q_deg_m'] and row.get('v_deg_m_s') == [0.] * 7
                    for row in physical['nodes']), 'physical NO_SEND reference was replaced by diagnostic movement')
    old_plan = Plan([Node(row['t'], tuple(row['q_deg_m']), tuple(row['v_deg_m_s'])) for row in old.get('nodes', [])], plan_id=old.get('plan_id'))
    nodes = candidate.get('nodes', [])
    require(isinstance(candidate.get('plan_id'), str) and candidate['plan_id'] != old_plan.id and len(nodes) > 2,
            'complete diagnostic moving candidate missing')
    actual = Plan([Node(row['t'], tuple(row['q_deg_m']), tuple(row['v_deg_m_s'])) for row in nodes], plan_id=candidate['plan_id'])
    if index > 0:
        prefix = []
        for offset in range(35):
            q, _ = old_plan.sample((anchor + label_offset_ns(offset)) / 1e9)
            prefix.append([math.radians(x) for x in q[:6]] + [q[6]])
        require(canonical(context.get('previous_actions')) == canonical(prefix), 'RTC prefix does not come from the previous diagnostic plan at real observation time')
    require(receipt.get('bounds_source') == 'SDK_DOCUMENTED_ONLY_NO_QUERY_NO_SEND', 'unexpected NO_SEND bound source')
    bounds = [list(pair) for pair in DOCUMENTED_COMMAND_LIMITS_DEG] + [list(cfg['gripper_bounds_m'])]
    projection=gripper_command_projection(raw,bounds)
    require(canonical(prepared.get('gripper_command_projection',[]))==canonical(projection)
            and canonical(attempt.get('gripper_command_projection',[]))==canonical(projection),
            'gripper raw/command projection evidence differs')
    joint_projection=joint_command_projection(raw,bounds)
    require(canonical(prepared.get('joint_command_projection',[]))==canonical(joint_projection)
            and canonical(attempt.get('joint_command_projection',[]))==canonical(joint_projection),
            'joint raw/command projection evidence differs')
    expected = tail_plan(old_plan, raw, anchor / 1e9, context['cut_monotonic_ns'] / 1e9, bounds, observation_ns=anchor)
    require(len(actual.nodes) == len(expected.nodes), 'diagnostic node count differs from real tail preparation')
    for have, want in zip(actual.nodes, expected.nodes):
        require(max(abs(a - b) for a, b in zip((have.t, *have.q, *have.v), (want.t, *want.q, *want.v))) <= 1e-8,
                'diagnostic candidate differs from reconstructed tail-plan/governor')
    decision = commit.get('decision_monotonic_ns')
    require(commit.get('schema') == 'tiger-h50-plan-commit-v1' and commit.get('accepted') is True
            and commit.get('diagnostic_only') is True and commit.get('physical_dispatch_performed') is False
            and commit.get('request_index') == index and commit.get('observation_index') == attempt['observation_index']
            and commit.get('old_plan_id') == old_plan.id and commit.get('candidate_plan_id') == actual.id
            and commit.get('active_plan_id') == actual.id and commit.get('accepted_plan_count') == index + 1
            and commit.get('rejected_plan_count') == 0 and type(decision) is int
            and attempt['commit_ready_monotonic_ns'] <= decision <= context['deadline_monotonic_ns']
            and decision < context['cut_monotonic_ns']
            and commit.get('deadline_monotonic_ns') == context['deadline_monotonic_ns']
            and commit.get('cut_monotonic_ns') == context['cut_monotonic_ns']
            and canonical(commit) == canonical(attempt.get('plan_commit')), 'diagnostic candidate was not accepted before its real deadline/cut')
    return fingerprint(candidate)


def audit_attempts(cfg, step, receipt, store, checker, hooks):
    approved = require_approved_config(cfg)
    deadline_ns = round(approved['reply_deadline_ms'] * 1_000_000)
    preparation_source = hooks['preparation_source']()
    attempts = receipt.get('attempts')
    require(isinstance(attempts, list) and len(attempts) == 103, 'exactly103 real attempts required; failures cannot be padded')
    source = receipt.get('lease', {}).get('session_id')
    trial = receipt.get('trial_index')
    require(isinstance(source, str) and source and type(trial) is int and trial >= 0, 'original lease/trial identity missing')
    measured, warmup, rows, first_health, previous_reference = [], [], [], None, None
    clock = hooks['clock']()
    journal = type('ReadOnlyJournal', (), {})()
    journal.episode_path = store.episode
    journal.storage_binding = {'mountpoint': cfg['usb'], 'uuid': cfg['usb_uuid'], 'major_minor': cfg['usb_selection']['major_minor']}
    journal.verify = store.verify
    for ordinal, attempt in enumerate(attempts):
        require(attempt.get('warmup') is (ordinal < 3) and attempt.get('status') in
                ('NO_SEND_DIAGNOSTIC_PLAN_COMMITTED',)
                and attempt.get('deadline_met') is True,
                'attempt incomplete or warmup partition differs: ' + str(ordinal)
                + ' (status=' + str(attempt.get('status'))[:100] + '; error=' + str(attempt.get('error'))[:500] + ')')
        prediction = attempt.get('prediction', {})
        require(prediction.get('status') == 'DEPLOYMENT_PREDICTION_BUFFERED' and prediction.get('module_dispatched_commands') == 0,
                'prediction not completely buffered or dispatched')
        index, request_id = prediction.get('request_index'), prediction.get('request_id')
        require(type(index) is int and index == ordinal and index == attempt.get('request_index')
                and prediction.get('trial_index') == trial and prediction.get('trial_seed') == 42 + trial,
                'request/trial/seed identity differs')
        require(isinstance(request_id, str) and re.fullmatch('[0-9a-f]{32}', request_id), 'invalid recorded request UUID')
        directory = store.relative(prediction.get('journal_directory', ''))
        require(len(directory.parts) == 1 and directory.name.endswith(request_id), 'journal not an original direct episode child')
        original_input, input_sha = store.json(directory / 'input.json')
        original_result, result_sha = store.json(directory / 'result.json')
        timing, timing_sha = store.json(directory / 'ram-ready-timing.json')
        expected_result = {k: v for k, v in prediction.items() if k not in POST_SAVE_FIELDS}
        require(canonical(original_result) == canonical(expected_result), 'persisted result differs from returned prediction')
        require(original_input.get('request_id') == request_id and original_result.get('request_id') == request_id
                and original_input.get('checkpoint_step') == step, 'input/result step/request binding differs')
        snapshot = original_input.get('snapshot', {})
        anchor = snapshot.get('packet_ready_monotonic_ns')
        sent = prediction.get('client_sent_monotonic_ns')
        require(type(anchor) is int and type(sent) is int and sent >= anchor, 'actual source/request timestamps missing')
        frozen, payload, ages = hooks['snapshot'](snapshot, original_input.get('prompt'), journal, clock, now_ns=sent)
        rtc = original_input.get('rtc', {})
        enabled = ordinal > 0
        previous_steps = 35 if enabled else 0
        require(rtc.get('enabled') is enabled and rtc.get('previous_valid_steps') == previous_steps
                and type(rtc.get('native_training_hz')) is int and type(rtc.get('execution_label_hz')) is int
                and rtc.get('native_training_hz') == approved['native_training_hz']
                and rtc.get('execution_label_hz') == approved['action_hz']
                and len(rtc.get('previous_actions', [])) == previous_steps
                and rtc.get('trial_index') == trial and rtc.get('request_index') == index,
                'first warmup must be native disabled/0; remaining102 must use enabled35-prefix RTC')
        wire_keys = ('schema', 'enabled', 'previous_actions', 'previous_valid_steps', 'freeze_steps',
                     'execution_label_hz', 'native_training_hz', 'beta', 'trial_index', 'request_index')
        require(canonical(rtc) == canonical({key: attempt.get('rtc_context', {}).get(key) for key in wire_keys}),
                'original native/RTC wire differs from local causal context')
        payload['rtc'] = rtc
        require(prediction.get('snapshot_sha256') == fingerprint(frozen)
                and prediction.get('request_payload_sha256') == fingerprint(payload), 'exact model input hashes differ')
        require(ages['front_age_s'] <= .25 and ages['right_age_s'] <= .25, 'actual input camera stale')
        for role in ('front', 'right'):
            frame = snapshot['frames'][role]
            native = checker.frames[role].get(frame['wall_ns'])
            require(native is not None and native['sha256'] == frame['sha256'], 'model JPEG is not the exact recorded native frame: ' + role)
        before, after = prediction.get('health_before'), prediction.get('health_after')
        before_fp, after_fp = hooks['health'](before, step), hooks['health'](after, step)
        require(before_fp == after_fp == prediction.get('policy_fingerprint'), 'actual before/after model identities differ')
        if first_health is None:
            first_health = before
        require(before_fp == hooks['health'](first_health, step), 'model identity changed within103 calls')
        zero_weight_proof(before)
        response = prediction.get('response', {})
        hooks['result'](response)
        require(response.get('contract_fingerprint') == before.get('acceptance_binding', {}).get('contract_fingerprint'),
                'actual model response execution contract fingerprint differs')
        returned = response.get('rtc', {})
        require(returned.get('enabled') is enabled and returned.get('previous_valid_steps') == previous_steps
                and type(returned.get('native_training_hz')) is int and type(returned.get('execution_label_hz')) is int
                and returned.get('native_training_hz') == approved['native_training_hz']
                and returned.get('execution_label_hz') == approved['action_hz']
                and returned.get('request_index') == index and returned.get('trial_index') == trial
                and returned.get('trial_seed') == 42 + trial
                and returned.get('previous_actions_sha256') == fingerprint(rtc['previous_actions']), 'RTC response prefix/seed identity differs')
        ready, accepted = attempt.get('commit_ready_monotonic_ns'), prediction.get('result_ram_accepted_monotonic_ns')
        require(type(ready) is int and type(accepted) is int and anchor <= accepted <= prediction.get('ready_for_commit_monotonic_ns', 0) <= ready,
                'RAM validation/commit-ready ordering missing or reversed')
        latency = (ready - anchor) / 1e6
        checked = attempt.get('commit_check_monotonic_ns')
        require(type(checked) is int and checked >= ready, 'actual commit check timestamp missing/reversed')
        require(finite(attempt.get('packet_to_commit_ready_ms')) and abs(latency - attempt['packet_to_commit_ready_ms']) <= 1e-6
                and attempt['deadline_met'] is (ready <= anchor + deadline_ns and checked <= anchor + deadline_ns),
                'packet-to-RAM-commit-ready latency or deadline classification differs')
        require(timing.get('request_id') == request_id and timing.get('result_ram_accepted_monotonic_ns') == accepted
                and timing.get('timing_basis') == cfg['h50_deployment']['commit_timing_basis'], 'actual RAM-ready sidecar differs')
        prepared, prepared_sha = store.json(Path('evidence') / ('h50-prepared-%06d.json' % attempt['observation_index']))
        require(prepared.get('request_id') == request_id and prepared.get('scope') == 'VALIDATED_NOT_COMMITTED'
                and canonical(prepared.get('raw_h50')) == canonical(response['actions'])
                and prepared.get('source_packet_ready_monotonic_ns') == anchor, 'complete prepared H50/reference not persisted')
        commit_name = 'h50-commit-%06d.json' % attempt['observation_index']
        require(prepared.get('commit_decision_file') == commit_name, 'diagnostic commit sidecar binding missing')
        commit, commit_sha = store.json(Path('evidence') / commit_name)
        dryrun_sha = audit_dryrun_preparation(cfg, receipt, attempt, prepared, response['actions'], anchor,
                                            preparation_source, commit, previous_reference)
        previous_reference = prepared['candidate_reference']
        (warmup if ordinal < 3 else measured).append((index, latency))
        rows.append({'ordinal': ordinal, 'request_index': index, 'request_id': request_id,
                     'input_sha256': input_sha, 'result_sha256': result_sha, 'ram_timing_sha256': timing_sha,
                     'prepared_reference_sha256': prepared_sha, 'diagnostic_candidate_sha256': dryrun_sha,
                     'commit_sha256': commit_sha, 'inference_phase': attempt['inference_phase'],
                     'packet_to_commit_ready_ms': latency})
    indices = [row['request_index'] for row in rows]
    require(indices == list(range(103)), 'native/RTC request indices are not exact0..102')
    require(canonical(previous_reference) == canonical(receipt.get('diagnostic_final_reference'))
            and receipt.get('diagnostic_reference_plan_count') == 103
            and receipt.get('native_bootstrap_calls') == 1 and receipt.get('rtc_continuation_calls') == 102,
            'diagnostic final chain/counts differ from all103 accepted plans')
    require(canonical(receipt.get('measured_commit_ready_latencies_ms')) == canonical([v for _, v in measured]),
            'summary latency list does not contain the exact100 validated RAM-ready measurements')
    return dict(rows=rows, health=deepcopy(first_health), source=source, trial=trial,
                robot_preparation_source=preparation_source,
                all_commit_deadlines_met=all(row['deadline_met'] is True for row in attempts),
                metrics={'raw_latencies_ms': [v for _, v in measured],
                         'rtc_call_request_indices': [i for i, _ in measured],
                         'warmup_request_indices': [i for i, _ in warmup]})


def audit_events(store, receipt, events, *, acceptance=False):
    from .h50_async import prepare_output_command,OUTPUT_COMMAND_POLICY
    raw = store.read(events['path'], limit=256 * 1024 * 1024, flush=True)
    records = [loads_unique_json(line) for line in raw.splitlines() if line.strip()]
    require(len(records) >= 2 and records[0].get('schema') == 'tiger-host-can-tx-evidence-v1'
            and records[-1].get('schema') == 'tiger-host-can-tx-evidence-summary-v1', 'async log header/final footer missing')
    body = records[1:-1]
    require(len(body) == events['written'] and [row.get('sequence') for row in body] == list(range(1, len(body) + 1)),
            'async evidence sequence/count incomplete')
    payloads = [row.get('record') for row in body]
    require(all(isinstance(row, dict) for row in payloads), 'actual async record envelope missing')
    decisions = [row for row in payloads if row.get('kind') == 'RTC_QUEUE_DECISION']
    expected = {row.get('observation_index'): row.get('status') for row in receipt.get('attempts', [])
                if row.get('observation_index') is not None}
    require((not acceptance or len(decisions) == len(expected) == 103)
            and len(decisions) == len(expected)
            and {row.get('request_index') for row in decisions} == set(expected)
            and all(row.get('status') == expected[row.get('request_index')] for row in decisions),
            'async queue decisions incomplete or differ from actual attempts')
    require(any(row.get('kind') in ('NO_SEND_UNSENT_HOLD_AND_ACTUAL', 'ACCEPTED_REFERENCE_AND_ACTUAL') for row in payloads),
            'actual/reference observer log missing')
    if acceptance:
        hold_rows = [row for row in payloads if row.get('kind') == 'NO_SEND_UNSENT_HOLD_AND_ACTUAL']
        require(bool(hold_rows) and all(row.get('physical_dispatch_performed') is False
                and row.get('reference_q_deg_m') == receipt.get('initial_actual_q_deg_m')
                and row.get('reference_v_deg_m_s') == [0.] * 7 for row in hold_rows),
                'physical NO_SEND observer reference is not the original unsent HOLD')
    output_checks=diagnostic_output_checks=upper_clamps=lower_clamps=joint_upper_clamps=joint_lower_clamps=0
    if acceptance or receipt.get('output_command_policy') is not None:
        require(receipt.get('output_command_policy')==OUTPUT_COMMAND_POLICY,'shared final output converter evidence missing')
        bounds=receipt.get('output_command_bounds_deg_m')
        require(isinstance(bounds,list) and len(bounds)==7,'output command bounds missing')
        for row in payloads:
            if row.get('kind') not in ('NO_SEND_UNSENT_HOLD_AND_ACTUAL','ACCEPTED_REFERENCE_AND_ACTUAL'):continue
            expected=prepare_output_command(row.get('reference_q_deg_m'),bounds)
            require(canonical(row.get('output_conversion'))==canonical(expected),'final sampled output conversion differs')
            output_checks+=1;upper_clamps+=int(expected['gripper_upper_clamped']);lower_clamps+=int(expected['gripper_lower_clamped'])
            joint_upper_clamps+=sum(p['clamp_side']=='upper' for p in expected['joint_projection'])
            joint_lower_clamps+=sum(p['clamp_side']=='lower' for p in expected['joint_projection'])
            if row.get('kind')=='NO_SEND_UNSENT_HOLD_AND_ACTUAL':
                if row.get('diagnostic_reference_phase')=='ABORT':continue
                expected=prepare_output_command(row.get('diagnostic_reference_q_deg_m'),bounds)
                require(canonical(row.get('diagnostic_output_conversion'))==canonical(expected),'diagnostic output bypassed shared converter')
                diagnostic_output_checks+=1;upper_clamps+=int(expected['gripper_upper_clamped']);lower_clamps+=int(expected['gripper_lower_clamped'])
                joint_upper_clamps+=sum(p['clamp_side']=='upper' for p in expected['joint_projection'])
                joint_lower_clamps+=sum(p['clamp_side']=='lower' for p in expected['joint_projection'])
        require(output_checks>0 and (not acceptance or diagnostic_output_checks==output_checks),
                'shared output preflight did not cover all no-send ticks')
    return {'path': events['path'], 'sha256': hashlib.sha256(raw).hexdigest(), 'records': len(body),
            'queue_decisions': len(decisions), 'fsync_after_writer_closed': True,
            'output_command_checks':output_checks,'diagnostic_output_checks':diagnostic_output_checks,
            'gripper_upper_clamps':upper_clamps,'gripper_lower_clamps':lower_clamps,
            'joint_upper_clamps':joint_upper_clamps,'joint_lower_clamps':joint_lower_clamps}


def _real_paths(value, mappings):
    if isinstance(value, dict):
        return {key: _real_paths(item, mappings) for key, item in value.items()}
    if isinstance(value, list):
        return [_real_paths(item, mappings) for item in value]
    if isinstance(value, str):
        for temporary, real in mappings:
            if value == temporary or value.startswith(temporary + '/'):
                return real + value[len(temporary):]
    return value


def audit_journal_final(cfg, receipt, store):
    approved = require_approved_config(cfg)
    proof = receipt.get('journal_final', {})
    require(proof.get('schema') == 'tiger-h50-async-usb-journal-v1'
            and proof.get('persistence_mode') == approved['persistence_mode']
            and proof.get('status') == 'USB_FLUSH_COMPLETE' and proof.get('complete') is True,
            'bounded RAM journal has not completed final USB flush')
    require(proof.get('worker_alive') is False and proof.get('unflushed_data_is_only_in_volatile_ram') is False
            and proof.get('error') is None and proof.get('pending_records') == 0 and proof.get('pending_bytes') == 0,
            'journal still has a writer, error or unflushed volatile records')
    accepted, written, files = proof.get('accepted'), proof.get('written'), proof.get('files')
    require(type(accepted) is int and accepted >= 0 and type(written) is int and accepted == written
            and isinstance(files, list) and len(files) == written, 'journal accepted/written/files count differs')
    require(proof.get('limit_bytes') == approved['journal_buffer_limit_bytes']
            and proof.get('limit_records') == approved['journal_buffer_limit_records'], 'RAM journal limits differ')
    for key, limit in (('peak_pending_bytes', approved['journal_buffer_limit_bytes']),
                       ('peak_pending_records', approved['journal_buffer_limit_records'])):
        require(type(proof.get(key)) is int and 0 <= proof[key] <= limit, 'RAM journal peak/accounting invalid: ' + key)
    expected_binding = {'mountpoint': cfg['usb'], 'uuid': cfg['usb_uuid'], 'major_minor': cfg['usb_selection']['major_minor']}
    require(proof.get('storage_binding') == expected_binding, 'final journal USB binding differs')
    finish = proof.get('flush_finished_monotonic_ns')
    require(type(finish) is int and finish > 0, 'final USB flush timestamp absent')
    require([row.get('sequence') for row in files] == list(range(1, written + 1)), 'USB journal sequence missing')
    paths = set(); previous = 0
    for row in files:
        path = store.relative(row.get('path', '')); key = str(path)
        require(key not in paths, 'journal manifest path repeats'); paths.add(key)
        raw = store.read(path)
        require(type(row.get('bytes')) is int and len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row.get('sha256'),
                'final USB file bytes/hash differs: ' + key)
        when = row.get('persisted_monotonic_ns')
        require(type(when) is int and previous < when <= finish, 'USB persistence time order differs'); previous = when
    stored, stored_sha = store.json(proof.get('report_path', ''))
    require(canonical(stored) == canonical(proof), 'final journal proof file differs from returned close receipt')
    return {'status': 'PASS', 'files': written, 'proof_sha256': stored_sha,
            'proof_path': proof['report_path'], 'manifest_paths': sorted(paths),
            'usb_finalflush_validated': True, 'storage_semantic_version': approved['storage_semantic_version']}


def audit_kernel_can_timing(native):
    operation = native.get('recorded_operation', {})
    timing = operation.get('raw_CAN_timing', {})
    counts = operation.get('raw_CAN_counts')
    kernel, legacy = timing.get('kernel_receipt_enqueued_counts'), timing.get('legacy_estimated_enqueued_counts')
    require(timing.get('schema') == 'tiger-native-can-kernel-time-v1'
            and timing.get('timestamp_basis') == 'linux_so_timestampns_kernel_receipt'
            and timing.get('header_preserves_kernel_wall_ns') is True
            and type(timing.get('timestamp_error_count')) is int and timing['timestamp_error_count'] == 0
            and isinstance(counts, dict) and bool(counts) and isinstance(kernel, dict) and kernel == counts
            and all(type(value) is int and value > 0 for value in counts.values())
            and isinstance(legacy, dict) and all(type(value) is int and value == 0 for value in legacy.values())
            and timing.get('hardware_generation_timestamp_available') is False,
            'native raw CAN kernel timestamp provenance/count audit failed; original streams are preserved')
    return deepcopy(timing)


def _collect_artifacts(cfg, step, receipt, store, hooks):
    native, checker = hooks['native'](store.episode, root_fd=store.fd, cameras=('front', 'right'), arms=('right',),
        feedback_age=.2, camera_age=.25, image_size=(640, 480), export_dir=store.output,
        output_fd=store.output_fd, return_checker=True)
    mappings = [(str(Path('/proc/self/fd') / str(store.output_fd)), str(store.output)),
                (str(Path('/proc/self/fd') / str(store.fd)), str(store.episode))]
    artifacts = {'schema': 'tiger-h50-run-artifacts-v1', 'status': 'FAIL', 'step': step,
                 'original_run_status': receipt.get('status'),
                 'scope': 'artifact integrity only; original policy/physical outcome is unchanged',
                 'native_audit': _real_paths(native, mappings)}
    store.write('native-recording-audit.json', artifacts['native_audit'])
    native_errors = [row for row in native.get('errors', []) if row.get('kind') != 'review_export_failed']
    export_errors = [row for row in native.get('errors', []) if row.get('kind') == 'review_export_failed']
    require(not native_errors and native.get('source_stat_unchanged') is True,
            'native original recording audit failed: ' + str(native_errors[:3])[:900])
    require(not export_errors and native.get('status') == 'PASS'
            and len(native.get('review_exports', {}).get('videos', [])) == 2,
            'MP4/CSV derivative export audit failed: ' + str(export_errors[:3])[:900])
    artifacts['raw_CAN_kernel_timing_audit'] = audit_kernel_can_timing(native)
    final = receipt.get('recording_finalization', {})
    require(final.get('completed') is True and final.get('healthy') is True, 'original owned recording not finalized')
    original, original_sha = store.json(Path('evidence') / 'h50-async-run.json')
    require(canonical(original) == canonical(receipt), 'final source run receipt differs from persisted original')
    artifacts['source_run_file_sha256'] = original_sha
    artifacts['journal_audit'] = audit_journal_final(cfg, receipt, store)
    journals = []
    span = native.get('state_monotonic_span_ns')
    require(isinstance(span, list) and len(span) == 2 and all(type(v) is int and v > 0 for v in span),
            'native state monotonic coverage missing')
    for name in store.journal_names():
        original_input, input_sha = store.json(Path(name) / 'input.json')
        original_result, result_sha = store.json(Path(name) / 'result.json')
        request_id = name.rsplit('-', 1)[-1]
        require(original_input.get('request_id') == original_result.get('request_id') == request_id,
                'original journal identity differs: ' + name)
        matched = next((row for row in receipt.get('attempts', [])
                        if row.get('prediction', {}).get('request_id') == request_id), {})
        returned = matched.get('prediction')
        if returned is not None:
            expected = {key: value for key, value in returned.items() if key not in POST_SAVE_FIELDS}
            require(canonical(expected) == canonical(original_result), 'returned prediction differs from original result')
        snapshot = original_input.get('snapshot', {})
        anchor = snapshot.get('packet_ready_monotonic_ns')
        domain = snapshot.get('clock_domain', {})
        require({key: domain.get(key) for key in ('host_id', 'boot_id', 'clock_name')} == native.get('state_clock_domain'),
                'model/native monotonic clock identities differ')
        finish = (returned or original_result).get('ready_for_commit_monotonic_ns', original_result.get('client_finished_monotonic_ns'))
        for key in ('commit_ready_monotonic_ns', 'finished_monotonic_ns'):
            if type(matched.get(key)) is int and type(finish) is int:
                finish = max(finish, matched[key])
        require(type(anchor) is int and type(finish) is int and span[0] <= anchor <= finish <= span[1],
                'native state recording does not enclose a model attempt')
        journals.append({'directory': str(store.episode / name), 'request_id': request_id,
                         'input_sha256': input_sha, 'result_sha256': result_sha,
                         'recorded_status': original_result.get('status'), 'response_present': 'response' in original_result,
                         'native_time_coverage': True})
        manifest = set(artifacts['journal_audit']['manifest_paths'])
        require(all(str(Path(name) / leaf) in manifest for leaf in ('input.json', 'result.json', 'ram-ready-timing.json')),
                'an original journal file is absent from finalflush manifest')
    expected_ids = {row['prediction']['request_id'] for row in receipt.get('attempts', []) if row.get('prediction', {}).get('request_id')}
    require(expected_ids <= {row['request_id'] for row in journals}, 'returned model result lacks its original USB journal')
    artifacts['model_journals'] = journals
    events = receipt.get('events_final', {})
    require(events.get('finished') is True and events.get('writer_alive') is False
            and events.get('timing_evidence_incomplete') is False and events.get('queued') == 0
            and events.get('accepted') == events.get('written'), 'asynchronous evidence log not completely closed')
    artifacts['async_events'] = audit_events(store, receipt, events)
    artifacts['exports'] = artifacts['native_audit']['review_exports']
    artifacts['status'] = 'PASS'
    artifacts['usb_finalflush_validated'] = True
    store.verify()
    return artifacts, checker


@_finalizes_recording_event
def finalize_run_artifacts(cfg, session, step, receipt):
    """Per-run MP4/CSV/file preservation; never a100-call policy acceptance."""
    require_approved_config(cfg)
    hooks = _hooks(); store = USBAnchor(cfg, receipt.get('owned_recording_path', ''), hooks)
    full = {'schema': 'tiger-h50-run-artifacts-v1', 'status': 'FAIL', 'step': step,
            'original_run_status': receipt.get('status'), 'errors': [],
            'scope': 'artifact preservation only; not policy/physical task success'}
    try:
        store.create_output()
        try:
            artifacts, _ = _collect_artifacts(cfg, step, receipt, store, hooks)
            full.update(artifacts)
        except Exception as exc:
            full['errors'].append(type(exc).__name__ + ': ' + str(exc))
        report = store.write('run-artifacts.json', full)
        compact = {'schema': 'tiger-h50-run-artifacts-summary-v1', 'status': full['status'], 'step': step,
                   'validated': full['status'] == 'PASS', 'report': report, 'episode': str(store.episode),
                   'exports': compact_exports(full.get('exports')), 'original_run_status': receipt.get('status'),
                   'errors': [error[:240] for error in full['errors'][:2]],
                   'usb_finalflush_validated': full.get('usb_finalflush_validated') is True,
                   'scope': 'preservation only; not policy/physical success'}
        store.write('summary.json', compact)
        return {'compact': compact, 'full': full}
    finally:
        store.close()


def compact_exports(exports):
    if not isinstance(exports, dict):
        return None
    return {'joint_csv': exports.get('joint_csv', {}).get('path'),
            'videos': {row.get('role'): row.get('path') for row in exports.get('videos', [])}}


@_finalizes_recording_event
def finalize_acceptance(cfg, session, step, no_motion_receipt):
    """Return compact/full. Audit failures save FAIL; lost USB raises, never falls back."""
    approved = require_approved_config(cfg)
    require(type(step) is int and step in approved['allowed_steps'], 'unsupported checkpoint')
    hooks = _hooks(); receipt = deepcopy(no_motion_receipt)
    episode = receipt.get('owned_recording_path')
    require(isinstance(episode, str) and episode, 'no real owned recording to audit')
    store = USBAnchor(cfg, episode, hooks)
    full = {'schema': 'tiger-h50-acceptance-audit-v1', 'status': 'FAIL', 'step': step,
            'source_run_session_id': receipt.get('lease', {}).get('session_id'), 'source_trial_index': receipt.get('trial_index'),
            'scope': 'real NO_SEND USB file/latency audit; not physical tracking or grasp success',
            'errors': [], 'source_receipt_sha256': fingerprint(receipt), 'publication': {'status': 'NOT_ATTEMPTED'}}
    full['source_run_diagnostics'] = {'status': receipt.get('status'),
        'timing_basis': receipt.get('timing_basis'), 'storage_semantic_version': receipt.get('storage_semantic_version'),
        'requested_measured': receipt.get('requested_measured'), 'measured_commit_ready_p95_ms': receipt.get('measured_commit_ready_p95_ms'),
        'measured_missing_latency_count': receipt.get('measured_missing_latency_count'),
        'attempt_errors': [{'request_index': row.get('request_index'), 'status': row.get('status'), 'error': row.get('error')}
                           for row in receipt.get('attempts', []) if row.get('error')][:8]}
    candidate = None
    try:
        store.create_output()
        artifacts, checker = _collect_artifacts(cfg, step, receipt, store, hooks)
        full['run_artifacts'] = artifacts; full['native_audit'] = artifacts['native_audit']
        native = artifacts['native_audit']
        events = check_run_summary(receipt, step)
        persisted_run, persisted_sha = store.json(Path('evidence') / 'h50-async-run.json')
        require(canonical(persisted_run) == canonical(receipt), 'final source run receipt was not durably preserved unchanged')
        full['source_run_file_sha256'] = persisted_sha
        proof = audit_attempts(cfg, step, receipt, store, checker, hooks)
        full['attempt_audit'] = proof['rows']; full['async_events'] = audit_events(store, receipt, events, acceptance=True)
        gpu, peak = gpu_proof(receipt, proof['source']); full['server_gpu_evidence'] = gpu
        zero = zero_weight_proof(proof['health']); full['server_zero_weight_evidence'] = zero
        latencies = proof['metrics']['raw_latencies_ms']; p95, maximum = percentile(latencies, 95), max(latencies)
        binding = proof['health']['acceptance_binding']
        require(binding.get('semantics') == acceptance_semantics(), 'actual model acceptance binding uses a different execution/timing/storage contract')
        candidate = {'schema': 'tiger-fp32-h50-rtc-acceptance-v1', 'status': 'PASS', 'step': step,
            'plan_preparation_scope': 'DIAGNOSTIC_CHAIN_REAL_PLANS_NO_PHYSICAL_COMMIT',
            'robot_preparation_source': proof['robot_preparation_source'],
            **deepcopy(binding), 'source_run_session_id': proof['source'], 'source_trial_index': proof['trial'],
            'metrics': proof['metrics'],
            'requirements': {'warmup_calls': approved['acceptance_warmup_calls'], 'measured_calls': approved['acceptance_measured_calls'],
                             'p95_budget_ms': approved['acceptance_p95_ms'], 'max_budget_ms': approved['acceptance_max_ms'],
                             'whole_gpu_budget_mib': approved['whole_gpu_budget_mib']},
            'results': {'mode': 'native32_bootstrap_then_rtc', 'warmup_calls': 3, 'measured_calls': 100, 'vjp_enabled_calls': 100,
                        'native_bootstrap_calls': 1, 'rtc_continuation_calls': 102,
                        'bootstrap_accepted_before_deadline': True, 'diagnostic_reference_chain_validated': True,
                        'latency_ms': {'p95': p95, 'max': maximum}, 'whole_gpu_peak_mib': peak,
                        'all_actions_float32_50x7': True, 'zero_weight_same_rng_noise_equivalent': True,
                        'robot_chain_audits_passed': proof['all_commit_deadlines_met'], 'timing_basis': approved['commit_timing_basis'],
                        'usb_finalflush_validated': artifacts['journal_audit']['usb_finalflush_validated']},
            'evidence': {'usb_episode': str(store.episode), 'usb_audit': str(store.output / 'full-audit.json'),
                         'usb_uuid': cfg['usb_uuid'], 'async_event_sha256': full['async_events']['sha256'],
                         'native_operation_sha256': native['operation_sha256'],
                         'journal_finalflush_proof_path': artifacts['journal_audit']['proof_path'],
                         'journal_finalflush_proof_sha256': artifacts['journal_audit']['proof_sha256'],
                         'server_gpu_csv_sha256': gpu['csv_sha256'], 'server_gpu_json_sha256': gpu['json_sha256']}}
        if p95 > approved['acceptance_p95_ms'] or maximum > approved['acceptance_max_ms'] or peak > approved['whole_gpu_budget_mib'] or not proof['all_commit_deadlines_met']:
            candidate['status'] = 'FAIL'
            full['errors'].append('Actual packet-to-validated-RAM-ready latency or whole-device NVML peak exceeded budget')
        full.update(candidate=deepcopy(candidate), status=candidate['status'])
    except Exception as exc:
        full['errors'].append(type(exc).__name__ + ': ' + str(exc))
    try:
        report = store.write('full-audit.json', full)  # Durable complete USB audit precedes publication.
        if candidate is not None:
            runtime = hooks['runtime'](cfg, step, mode='shadow')
            try:
                health = runtime.ensure_ready()
                require(health.get('acceptance_binding') == {key: candidate[key] for key in ('contract_fingerprint', 'identity', 'semantics')},
                        'model/code binding changed before acceptance publication')
                runtime.acquire(Path(session.path).name + '-publish', trial_index=receipt['trial_index'])
                store.verify()
                manager = runtime.submit_acceptance(candidate)
                full['publication'] = {'status': manager.get('status'), 'manager_receipt': manager}
                full['status'] = 'PASS' if full['status'] == 'PASS' and manager.get('status') == 'PASS' else 'FAIL'
            except Exception as exc:
                full['status'] = 'FAIL'; full['publication']['status'] = 'FAILED_OR_UNCONFIRMED'
                full['errors'].append('Publication: ' + type(exc).__name__ + ': ' + str(exc))
            finally:
                try: full['publication_lease_release'] = runtime.close()
                except Exception as exc:
                    full['status'] = 'FAIL'; full['errors'].append('Publication close: ' + type(exc).__name__ + ': ' + str(exc))
        compact = {'schema': 'tiger-h50-acceptance-summary-v1', 'status': full['status'], 'step': step,
                   'validated': full['status'] == 'PASS', 'report': report, 'episode': str(store.episode),
                   'manager_status': full['publication'].get('status'),
                   'exports': compact_exports(full.get('run_artifacts', {}).get('exports')),
                   'usb_finalflush_validated': full.get('run_artifacts', {}).get('usb_finalflush_validated') is True,
                   'errors': [error[:240] for error in full['errors'][:2]]}
        full['compact'] = compact
        store.write('full-audit.json', full)
        store.write('summary.json', compact)
        return {'compact': compact, 'full': full}
    finally:
        store.close()
