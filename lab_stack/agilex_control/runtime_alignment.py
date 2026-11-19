"""Bounded runtime bridge for the existing camera/CAN producers.

No device access or disk writes. One lock serializes ingest and selection.
Original image bytes and CAN bytes are immutable; requests never restamp or
re-ingest cached observations. Software source alignment is not exposure sync.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import math
import struct
import threading
import time

from .time_alignment import (ClockDomain, OfflineAlignmentPolicy, StreamSample,
                             TimeAlignmentBuffer, STREAMS)
from .gripper_state import decode_gripper_feedback


CAN_STREAMS = {0x2A5: 'q12', 0x2A6: 'q34', 0x2A7: 'q56', 0x2A8: 'gripper'}


@dataclass(frozen=True)
class CameraPayload:
    rgb_bytes: bytes
    shape: tuple
    wall_ns: int
    source_perf_ns: int
    frame_id: int


@dataclass(frozen=True)
class CanPayload:
    data: bytes
    kernel_wall_ns: int


def _ns(value):
    return type(value) is int and value >= 0


class RuntimeAlignment:
    def __init__(self, clock_domain, config=None):
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self._stream_revisions = Counter()
        self._latest_sources = {}
        self.domain_record = deepcopy(clock_domain)
        self.domain = None
        self.buffer = None
        self.counts = Counter()
        self.last_ingest_failure = None
        self.last_selection = None
        self._latched = {}
        settings = config or {}
        if not isinstance(settings, dict):
            raise ValueError('observation_alignment must be an object')
        self.capacity = settings.get('capacity_per_stream', 32)
        if type(self.capacity) is not int or not 1 <= self.capacity <= 128:
            raise ValueError('Alignment capacity_per_stream must be an integer from 1 to 128')
        thresholds = {}
        for key, default in (('max_sample_age_ms', 50), ('max_camera_skew_ms', 40), ('max_state_skew_ms', 10)):
            value = settings.get(key, default)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError('Alignment ' + key + ' must be finite nonnegative milliseconds')
            thresholds[key.replace('_ms', '_ns')] = round(value * 1_000_000)
        self.policy = OfflineAlignmentPolicy(**thresholds)
        try:
            self.domain = self._domain(clock_domain)
            self.buffer = TimeAlignmentBuffer(self.domain, self.capacity, self.policy)
        except (ValueError, TypeError, KeyError) as exc:
            self._latched['CLOCK_DOMAIN_UNVERIFIED'] = str(exc)

    @staticmethod
    def _domain(record):
        if (not isinstance(record, dict) or record.get('identity_verified') is not True
                or record.get('clock_name') != 'monotonic_ns'):
            raise ValueError('A verified host/boot/capture monotonic_ns clock domain is required')
        return ClockDomain(**{key: record[key] for key in ('host_id', 'boot_id', 'session_id', 'clock_name')})

    def _reject(self, stream, code, detail, latch=False):
        self.counts['ingest_rejected'] += 1
        self.counts[code] += 1
        self.last_ingest_failure = {'stream': stream, 'code': code, 'detail': str(detail)}
        if latch:
            self._latched[code] = str(detail)
            self.condition.notify_all()
        return False

    def _sample(self, stream, timing, payload):
        if self.buffer is None:
            return self._reject(stream, 'CLOCK_DOMAIN_UNVERIFIED', 'Runtime capture identity is unavailable')
        if (not isinstance(timing, dict) or timing.get('provenance_complete') is not True
                or timing.get('mapping_valid') is not True):
            return self._reject(stream, 'SOURCE_EVIDENCE_UNAVAILABLE', 'Original source provenance is missing or invalid')
        try:
            domain = self._domain(timing.get('clock_domain'))
        except (ValueError, TypeError, KeyError) as exc:
            return self._reject(stream, 'SOURCE_DOMAIN_UNVERIFIED', exc)
        if domain != self.domain:
            return self._reject(stream, 'MULTIPLE_CLOCK_DOMAINS', 'Host/boot/capture changed; start a new runtime', True)
        sample = StreamSample(timing.get('sample_id'), timing.get('source_monotonic_ns_estimate'),
                              timing.get('available_monotonic_ns'), domain, payload,
                              timing.get('source_timestamp_kind'))
        outcome = self.buffer.add(stream, sample)
        self.counts['ingest_' + outcome.code] += 1
        if outcome.accepted:
            self.counts['ingest_accepted'] += 1
            previous = self._latest_sources.get(stream)
            if previous is None or sample.source_ns >= previous.source_ns:
                self._latest_sources[stream] = sample
            self._stream_revisions[stream] += 1
            self.condition.notify_all()
        else:
            self.counts['ingest_rejected'] += 1
        return outcome.accepted

    def ingest_camera(self, role, frame):
        """One call per newly published original frame; no camera is opened."""
        if role not in ('front', 'right'):
            return False
        # Byte freezing is outside the short selector lock. A readonly NumPy
        # view in inference will be backed by bytes, not a mutable shared array.
        try:
            import numpy as np
            rgb, timing = frame['rgb'], frame['source_timing']
            if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.shape != (480, 640, 3):
                raise ValueError('Expected actual 480x640 RGB uint8 camera payload')
            if (not _ns(frame.get('wall_ns')) or not _ns(timing.get('source_perf_ns'))
                    or type(frame.get('frame_id')) is not int or frame['frame_id'] < 1
                    or timing.get('frame_id') != frame['frame_id']
                    or timing.get('source_wall_ns_estimate') != frame['wall_ns']
                    or timing.get('available_monotonic_ns') != frame.get('available_monotonic_ns')):
                raise ValueError('Camera payload identity/timestamp differs from its source evidence')
            if timing.get('source_timestamp_kind') != 'lerobot_latest_timestamp_host_software':
                raise ValueError('Camera source timestamp kind is unrecognized')
            payload = CameraPayload(rgb.tobytes(order='C'), rgb.shape, frame['wall_ns'],
                                    timing['source_perf_ns'], frame['frame_id'])
        except Exception as exc:
            with self.lock:
                return self._reject(role, 'INVALID_CAMERA_PAYLOAD', exc)
        with self.lock:
            return self._sample(role, timing, payload)

    def ingest_can(self, cid, payload, timing):
        """Existing right observer passes its exact bytes and atomic metadata."""
        stream = CAN_STREAMS.get(cid)
        if stream is None:
            return False
        with self.lock:
            if (not isinstance(payload, bytes) or len(payload) != 8 or not isinstance(timing, dict)
                    or not _ns(timing.get('kernel_wall_ns'))
                    or timing.get('source_timestamp_kind') != 'linux_so_timestampns_kernel_receipt'):
                return self._reject(stream, 'INVALID_CAN_PAYLOAD', 'Exact eight-byte kernel-stamped feedback required')
            return self._sample(stream, timing, CanPayload(payload, timing['kernel_wall_ns']))

    def _policy_receipt(self):
        return {'age_reference': 'selection_now_minus_source',
                'max_sample_age_ns': self.policy.max_sample_age_ns,
                'max_camera_skew_ns': self.policy.max_camera_skew_ns,
                'max_state_skew_ns': self.policy.max_state_skew_ns}

    def _latest_receipt(self, now_ns):
        return {stream: ({'sample_id': sample.sample_id, 'source_ns': sample.source_ns,
                          'available_ns': sample.available_ns, 'source_age_ns': now_ns - sample.source_ns,
                          'available_age_ns': now_ns - sample.available_ns}
                         if (sample := self._latest_sources.get(stream)) is not None else None)
                for stream in STREAMS}

    def select(self, purpose='shadow', *, wait_ms=0, deadline_monotonic_ns=None):
        """Bounded, explicit requery with a NEW anchor after real publication.

        Condition waiting releases the runtime lock. Each failed attempt is
        retained; neither old samples nor a failed anchor are refreshed.
        wait_ms is a candidate setting (0 default, at most 20 ms), not a proven
        hardware recovery budget. The caller may supply a tighter absolute
        monotonic deadline derived from its actually accepted reference.
        """
        if (isinstance(wait_ms, bool) or not isinstance(wait_ms, (int, float))
                or not math.isfinite(wait_ms) or not 0 <= wait_ms <= 20):
            raise ValueError('alignment_wait_ms must be finite from 0 to 20 ms')
        if deadline_monotonic_ns is not None and not _ns(deadline_monotonic_ns):
            raise ValueError('Query deadline must be nonnegative monotonic integer ns')
        with self.condition:
            samples, receipt = self._select_locked(purpose)
            started_ns = receipt['anchor_ns']
            limit = started_ns + round(wait_ms * 1e6)
            if deadline_monotonic_ns is not None:
                limit = min(limit, deadline_monotonic_ns)
            attempts = []
            terminal = None
            while True:
                if wait_ms > 0 and receipt['selection_now_ns'] > limit:
                    samples = {}
                    receipt.update(ready=False, failures=receipt['failures'] + [{
                        'code': 'QUERY_WAIT_DEADLINE_EXHAUSTED', 'streams': list(STREAMS),
                        'detail': 'Final attempt completed outside the effective wait budget'}])
                    terminal = 'QUERY_WAIT_DEADLINE_EXHAUSTED'
                if deadline_monotonic_ns is not None and receipt['selection_now_ns'] > deadline_monotonic_ns:
                    samples = {}
                    receipt.update(ready=False, failures=receipt['failures'] + [{
                        'code': 'QUERY_COVERAGE_DEADLINE_EXHAUSTED', 'streams': list(STREAMS),
                        'detail': 'Actual query time exceeds caller accepted-reference coverage deadline'}])
                    terminal = 'QUERY_COVERAGE_DEADLINE_EXHAUSTED'
                if not receipt['ready']:
                    receipt['latest_sources'] = self._latest_receipt(receipt['selection_now_ns'])
                attempts.append(deepcopy(receipt))
                if receipt['ready'] or wait_ms == 0 or terminal is not None:
                    break
                if self._latched or self.buffer is not None and self.buffer.diagnostics()['latched_codes']:
                    terminal = 'SOURCE_CONTRACT_LATCHED'
                    break
                remaining = limit - time.monotonic_ns()
                if remaining <= 0:
                    terminal = 'QUERY_WAIT_DEADLINE_EXHAUSTED'
                    break
                if len(attempts) >= 32:
                    terminal = 'QUERY_ATTEMPT_LIMIT_EXHAUSTED'
                    break
                failed_streams = set(stream for failure in receipt['failures']
                                     for stream in failure.get('streams', ()) if stream in STREAMS) or set(STREAMS)
                observed = {stream: self._stream_revisions[stream] for stream in failed_streams}
                notified = self.condition.wait_for(lambda: bool(self._latched) or any(
                    self._stream_revisions[stream] != revision for stream, revision in observed.items()),
                    timeout=remaining / 1e9)
                if not notified:
                    terminal = 'QUERY_WAIT_DEADLINE_EXHAUSTED'
                    break
                # A wake after the deadline is not permission for a late sample.
                if time.monotonic_ns() >= limit:
                    terminal = 'QUERY_WAIT_DEADLINE_EXHAUSTED'
                    break
                samples, receipt = self._select_locked(purpose + '_retry')
            finished_ns = (receipt['selection_now_ns'] if wait_ms == 0 else time.monotonic_ns())
            receipt['query_retry'] = {'request_start_monotonic_ns': started_ns,
                'finished_monotonic_ns': finished_ns, 'requested_wait_ms': wait_ms,
                'effective_deadline_monotonic_ns': limit,
                'attempt_count': len(attempts), 'failed_attempt_count': sum(not item['ready'] for item in attempts),
                'request_to_final_anchor_ns': receipt['anchor_ns'] - started_ns,
                'total_query_ns': finished_ns - started_ns, 'terminal_reason': terminal}
            receipt['attempts'] = attempts
            if terminal is not None:
                samples = {}
                receipt['latest_sources'] = self._latest_receipt(finished_ns)
                receipt['latest_sources_observed_monotonic_ns'] = finished_ns
                receipt.update(ready=False, failures=receipt['failures'] + [{
                    'code': terminal, 'streams': list(STREAMS), 'detail': 'Bounded requery stopped; no latest fallback'}])
            self.counts['query_calls'] += 1
            self.counts['query_ready' if receipt['ready'] else 'query_failed'] += 1
            self.last_selection = deepcopy(receipt)
            return samples, receipt

    def _select_locked(self, purpose):
        anchor = time.monotonic_ns()
        selected_at = time.monotonic_ns()
        self.counts['selection_requests'] += 1
        self.counts['selection_' + purpose] += 1
        receipt = {'schema': 'tiger-runtime-alignment-v1', 'ready': False,
                   'selection_mode': 'query_grid', 'anchor_ns': anchor,
                   'selection_now_ns': selected_at, 'policy': self._policy_receipt(),
                   'samples': {}, 'anchor_age_ns': {}, 'selection_age_ns': {},
                   'camera_skew_ns': None, 'state_skew_ns': None,
                   'hardware_exposure_synchronization_verified': False,
                   'physical_deployment_ready': False,
                   'payload_validation': 'unavailable', 'failures': []}
        samples = {}
        if self._latched:
            receipt['failures'] = [{'code': code, 'streams': list(STREAMS), 'detail': detail}
                                   for code, detail in sorted(self._latched.items())]
        else:
            result = self.buffer.select(anchor, selected_at, self.domain)
            pure = result.receipt()
            for key in ('ready', 'samples', 'anchor_age_ns', 'selection_age_ns',
                        'camera_skew_ns', 'state_skew_ns', 'overall_source_skew_ns',
                        'camera_state_max_skew_ns', 'failures'):
                receipt[key] = pure[key]
            samples = result.samples
            if result.ready:
                try:
                    self._validate_selected(samples)
                except (ValueError, TypeError) as exc:
                    samples = {}
                    receipt.update(ready=False, failures=[{'code': 'SELECTED_PAYLOAD_INVALID',
                        'streams': list(STREAMS), 'detail': str(exc)}])
                else:
                    receipt['payload_validation'] = 'runtime_rgb_shape_type_and_finite_7d_verified'
        self.counts['selection_ready' if receipt['ready'] else 'selection_failed'] += 1
        self.last_selection = deepcopy(receipt)
        return dict(samples), receipt

    @staticmethod
    def _validate_selected(samples):
        if set(samples) != set(STREAMS):
            raise ValueError('Selection must include each of the six streams exactly once')
        for role in ('front', 'right'):
            payload = samples[role].payload
            if (not isinstance(payload, CameraPayload) or type(payload.rgb_bytes) is not bytes
                    or payload.shape != (480, 640, 3) or len(payload.rgb_bytes) != 480 * 640 * 3):
                raise ValueError('Selected RGB payload is malformed')
        values = []
        for stream in ('q12', 'q34', 'q56', 'gripper'):
            payload = samples[stream].payload
            if not isinstance(payload, CanPayload) or type(payload.data) is not bytes or len(payload.data) != 8:
                raise ValueError('Selected feedback payload is malformed')
            if stream == 'gripper' and decode_gripper_feedback(payload.data)['linear_position_available'] is not True:
                raise ValueError('Selected gripper feedback is not linear width mode')
            values.extend([decode_gripper_feedback(payload.data)['aperture_m']] if stream == 'gripper'
                          else [math.radians(value / 1000) for value in struct.unpack('>ii', payload.data)])
        if len(values) != 7 or not all(math.isfinite(value) for value in values):
            raise ValueError('Selected state must be seven finite measured values')

    @staticmethod
    def state_vector(samples):
        RuntimeAlignment._validate_selected(samples)
        return [math.radians(value / 1000) for stream in ('q12', 'q34', 'q56')
                for value in struct.unpack('>ii', samples[stream].payload.data)] + [
                    decode_gripper_feedback(samples['gripper'].payload.data)['aperture_m']]

    @staticmethod
    def compact_sources(samples):
        result = {}
        for stream, sample in samples.items():
            source = {'sample_id': sample.sample_id,
                      'source_monotonic_ns_estimate': sample.source_ns,
                      'available_monotonic_ns': sample.available_ns,
                      'source_timestamp_kind': sample.source_kind,
                      'clock_domain_ref': 'observation', 'mapping_valid': True,
                      'provenance_complete': True}
            if isinstance(sample.payload, CameraPayload):
                source.update(source_perf_ns=sample.payload.source_perf_ns, frame_id=sample.payload.frame_id)
            else:
                source['kernel_wall_ns'] = sample.payload.kernel_wall_ns
            result[stream] = source
        return result

    def diagnostics(self):
        with self.lock:
            return {'schema': 'tiger-runtime-alignment-diagnostics-v1',
                    'clock_domain': deepcopy(self.domain_record), 'counts': dict(self.counts),
                    'buffer': None if self.buffer is None else self.buffer.diagnostics(),
                    'latched_failures': dict(self._latched),
                    'last_ingest_failure': deepcopy(self.last_ingest_failure),
                    'last_selection': deepcopy(self.last_selection)}
