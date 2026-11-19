# PC PowerShell：在正式项目根目录运行；统一入口自行通过 SSH 调度。
# .\2026-10-06-15-00-AgileX.cmd model-plan
# 本模块不是 PC 直接推理入口。模型启动、影子测试与版本说明见 README.md。
# 导入不会加载模型或开 CAN；ContinuousPlan 可由离线审阅工具独立调试。
# 离线审阅见 pc-receipts/2026-10-07-14-02-执行层与适配层审阅/START-HERE.md。
"""Authenticated step4000 policy RPC and exact, isolated model staging.

This module NEVER imports CAN, ROS, a robot SDK or a camera driver. A shared
sensor owner supplies real RGB snapshots. ``shadow`` cannot move a robot.
Model service reuses the reviewed 2026-10-05 RTC source byte-for-byte; inference
time RTC is VJP guidance inside denoising, not interpolation of output actions.
The service keeps original BF16 backbone / FP32 action weights, with the
unchanged text embedding table gathered on CPU. Runtime 5 denoising steps is a
candidate; the checkpoint reference sampler used 10. No silent fallback.
"""
from __future__ import annotations

import argparse
import base64
import bisect
import copy
from dataclasses import dataclass
import hashlib
import http.client
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
from typing import Callable
import urllib.parse
import uuid

USB = Path('/media/agliex/UBUNTU 22_0')
EXPECTED_HOST = 'agliex-NUC14RVK-B'
PYTHON = '/home/agliex/miniconda3/envs/openpi/bin/python'
ORIGINAL_BUNDLE = USB / '2026-10-03-00-44-40-TIGER-PI05-USB'
LEGACY_RTC = USB / '2026-10-05-19-35-RTC-THREE-STAGE/2026-10-05-17-05-rtc-policy-server.py'
SERVER_SHA = '5b38f890d0053fb4b381513e6e7150b113cc892a5084277436352b73122e52f6'
COMPLETE_SHA = '178cd011538468f5b4b155f3766e2c8f97dbff4dea26d94b3a0efb089a618948'
DELIVERY_SHA = '51ba5c9e016530fa9dd2eeb170c35aa09db156fde5e7baa731f70a3b89c24e8d'
EXPORT_SHA = 'e4d222f0e79dcfa57679347644e8f3dd5d6cf40b695d4dfc7aef75a1f612368e'
CONFIG_SHA = 'b11c9b25ed45d3da0c4353b55c0e87d5da577afbeb76628a92ef755b46aac5c0'
TOKEN_SHA = '8986bb4f423f07f8c7f70d0dbe3526fb2316056c17bae71b1ea975e77a168fc6'
SOURCE_SHA = 'c03121dd3c91d0c0f029dd749ce142cd33282ac23ad674838996a4b663bb9600'
HELPER_SHA = 'e9394fe3af46d1c07d6228e3ad91b79b4fdb313feb0f49932f11d5570d0f881b'
COMMIT = '215abfb217dbac7d5f1273282331b9b1866c0479'
PROTOCOL = 'tiger-pi05-shadow-v1'
MODEL_ID = 'pi05-right-block-step4000-b11c9b25ed45'
RTC_ALGORITHM = 'tiger-vjp-rtc-decreasing-time-v1'
UNITS = ['rad'] * 6 + ['m']
RAW_BYTES = 224 * 224 * 3
MAX_JSON_REPLY = 128 * 1024
PINNED_VERSIONS = {'jax': '0.5.3', 'jaxlib': '0.5.3', 'jax-cuda12-plugin': '0.5.3',
                   'flax': '0.10.2', 'orbax-checkpoint': '0.11.13'}


class PolicyError(RuntimeError):
    """An explicit policy/input/source contract failure, never a zero fallback."""


def require(ok, message):
    if not ok:
        raise PolicyError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def finite_vector(value, length=7):
    require(isinstance(value, (list, tuple)) and len(value) == length,
            'Expected %d physical real values' % length)
    require(all(type(x) in (int, float) and math.isfinite(x) and abs(x) <= 1e30 for x in value),
            'Nonfinite/non-real or non-float32-representable physical value')
    return [float(x) for x in value]


def validate_actions(value):
    require(isinstance(value, (list, tuple)) and len(value) == 15,
            'Model output must have H15 rows; do not pad, trim or invent actions')
    return [finite_vector(row) for row in value]


def validate_prefix(prefix):
    if prefix is None:
        return None
    require(isinstance(prefix, dict), 'RTC prefix must be a dictionary')
    previous = validate_actions(prefix.get('previous_actions'))
    valid, delay, end = (prefix.get(k) for k in
                         ('previous_valid_steps', 'delay_steps', 'execution_horizon_steps'))
    require(all(type(x) is int for x in (valid, delay, end)) and 0 <= delay < end <= valid <= 15,
            'RTC requires 0 <= delay < attention end <= actual valid prefix <= 15')
    require(all(x == 0 for row in previous[valid:] for x in row),
            'Unknown prefix tail must be zero-weight zero padding; do not repeat last target')
    return {'previous_actions': previous, 'previous_valid_steps': valid,
            'delay_steps': delay, 'execution_horizon_steps': end}


@dataclass(frozen=True)
class ReferenceNode:
    t: float
    q: tuple
    v: tuple


class ContinuousPlan:
    """Monotonic continuous q/v reference with immutable committed RTC prefix.

    Bounds are REQUIRED caller inputs from verified hardware configuration;
    this class never guesses controller limits. Joint reference30deg/s and
    50deg/s2 are caps on generated commands, not measured motor acceleration.
    Gripper target passes through without a custom speed cap. Raw model values
    outside physical bounds are rejected, never clipped into a pretend result.
    No GPU, state feedback, path collision test, robot action or disk writing.
    """
    def __init__(self, nodes, execution_hz=18, send_hz=50):
        require(len(nodes) >= 2 and all(a.t < b.t for a, b in zip(nodes, nodes[1:])),
                'Continuous reference timestamps must increase')
        for node in nodes:
            require(type(node.t) in (int, float) and math.isfinite(node.t), 'Finite reference node time required')
            finite_vector(node.q)
            finite_vector(node.v, length=6)
        require(execution_hz in (18, 30) and type(execution_hz) is int, 'Explicit18/30Hz label clock required')
        require(type(send_hz) in (int, float) and 20 <= send_hz <= 100 and math.isfinite(send_hz),
                'Reference send clock must be20..100Hz')
        self.nodes = list(nodes)
        self.times = [node.t for node in nodes]
        self.execution_hz = execution_hz
        self.send_hz = send_hz

    @property
    def end(self):
        return self.nodes[-1].t

    def sample(self, when):
        require(type(when) in (int, float) and math.isfinite(when) and
                self.times[0] - 1e-8 <= when <= self.end + 1e-8,
                'Timed reference exhausted/stale; never replay last target')
        when = min(self.end, max(self.times[0], when))
        index = min(len(self.nodes)-2, max(0, bisect.bisect_right(self.times, when)-1))
        a, b = self.nodes[index], self.nodes[index+1]
        dt, elapsed = b.t-a.t, when-a.t
        acceleration = [(v1-v0)/dt for v0, v1 in zip(a.v, b.v)]
        q = [a.q[j] + a.v[j]*elapsed + .5*acceleration[j]*elapsed**2 for j in range(6)]
        q.append(a.q[6] + (b.q[6]-a.q[6])*elapsed/dt)
        velocity = [a.v[j]+acceleration[j]*elapsed for j in range(6)]
        return q, velocity

    def rtc_prefix(self, observation_time, delay_steps=8, attention_extra=3):
        require(type(observation_time) in (int, float) and math.isfinite(observation_time),
                'RTC observation time must be finite monotonic time')
        require(type(delay_steps) is int and 0 <= delay_steps <= 14 and
                type(attention_extra) is int and attention_extra >= 1, 'Invalid RTC commitment parameters')
        prior = [[0.]*7 for _ in range(15)]
        valid = 0
        for index in range(15):
            when = observation_time + index/self.execution_hz
            if when < self.times[0]-1e-8 or when > self.end+1e-8:
                break
            prior[index] = self.sample(when)[0]
            valid += 1
        require(delay_steps < valid, 'Accepted reference queue cannot cover committed inference delay')
        prefix = {'previous_actions': prior, 'previous_valid_steps': valid,
                  'delay_steps': delay_steps, 'execution_horizon_steps': min(valid, delay_steps+attention_extra),
                  'committed_until': observation_time+delay_steps/self.execution_hz,
                  'execution_label_hz': self.execution_hz}
        validate_prefix(prefix)
        return prefix

    @classmethod
    def build(cls, actions, observation_time, start_time, q, joint_bounds_rad, *, v=None,
              old=None, execution_hz=18, send_hz=50, speed_deg_s=30., accel_deg_s2=50.,
              gripper_bounds_m=(-.0013, .1)):
        """Return (new accepted reference, peak deviation from raw model deg).

        New action index0 belongs to observation_time, never response time.
        Replan start is the frozen prefix cut; preserve unexecuted history before cut,
        take old q/v at cut, and only install before that cut. Caller enforces
        receive/generation deadline and exclusive writer ownership.
        """
        actions = validate_actions(actions)
        require(execution_hz in (18, 30) and type(execution_hz) is int, 'Explicit18/30Hz label clock required')
        require(type(send_hz) in (int, float) and math.isfinite(send_hz) and 20 <= send_hz <= 100,
                'Send clock must be20..100Hz')
        require(all(type(t) in (int, float) and math.isfinite(t) for t in (observation_time, start_time)),
                'Finite monotonic clock required')
        end = observation_time + 15/execution_hz
        require(observation_time-1e-8 <= start_time < end, 'No future observation-aligned action horizon remains')
        require(len(joint_bounds_rad) == 6 and all(len(pair) == 2 and
                all(type(x) in (int, float) and math.isfinite(x) for x in pair) and pair[0] < pair[1]
                for pair in joint_bounds_rad), 'Six actual queried radian limit pairs required')
        require(len(gripper_bounds_m) == 2 and all(type(x) in (int, float) and math.isfinite(x)
                for x in gripper_bounds_m) and gripper_bounds_m[0] < gripper_bounds_m[1], 'Invalid gripper bounds')
        q = finite_vector(q)
        for stage, action_index, row in [('anchor', None, q)] + [
                ('model_action', index, row) for index, row in enumerate(actions)]:
            for dimension, (lo, hi) in enumerate([*joint_bounds_rad, gripper_bounds_m]):
                if not lo <= row[dimension] <= hi:
                    detail = {'stage': stage, 'action_index': action_index,
                              'dimension_index': dimension, 'dimension': 'J%d' % (dimension + 1) if dimension < 6 else 'gripper',
                              'value': row[dimension], 'bounds': [lo, hi], 'unit': 'rad' if dimension < 6 else 'm'}
                    error = PolicyError('Actual anchor/raw model target outside verified command bounds; no clipping: '
                                        + json.dumps(detail, separators=(',', ':')))
                    error.bounds_failure = detail
                    raise error
        require(all(type(x) in (int, float) and math.isfinite(x) and x > 0
                    for x in (speed_deg_s, accel_deg_s2)), 'Positive finite reference caps required')
        speed, accel = math.radians(speed_deg_s), math.radians(accel_deg_s2)
        velocity = finite_vector(v or [0.]*6, length=6)
        require(all(abs(x) <= speed+1e-12 for x in velocity), 'Reference velocity exceeds configured cap')
        if old is not None:
            require(old.execution_hz == execution_hz and old.send_hz == send_hz,
                    'Do not change reference clock during an accepted plan')
            prior_q, prior_v = old.sample(start_time)
            require(max(abs(a-b) for a, b in zip(q, prior_q)) < 1e-9 and
                    max(abs(a-b) for a, b in zip(velocity, prior_v)) < 1e-9,
                    'Replan anchor must be exactly old committed q/v; no discontinuity')
        nodes = [ReferenceNode(start_time, tuple(q), tuple(velocity))]
        peak, when = 0., start_time
        while end-when > 1e-8:
            dt = min(1/send_hz, end-when)
            raw_index = min(14., max(0., (when+dt-observation_time)*execution_hz))
            lower, upper = int(raw_index), min(14, int(raw_index)+1)
            fraction = raw_index-lower
            desired = [a+(b-a)*fraction for a, b in zip(actions[lower], actions[upper])]
            next_q, next_v = [], []
            for index, (lo, hi) in enumerate(joint_bounds_rad):
                current, vel = q[index], velocity[index]
                error = desired[index]-current
                wanted = math.copysign(min(speed, math.sqrt(2*accel*abs(error))), error) if error else 0.
                low = max(-speed, vel-accel*dt, 2*(lo-current)/dt-vel)
                high = min(speed, vel+accel*dt, 2*(hi-current)/dt-vel)
                disc_hi = (accel*dt)**2 + 8*accel*(hi-current-.5*vel*dt)
                disc_lo = (accel*dt)**2 + 8*accel*(current-lo+.5*vel*dt)
                high = min(high, max(0., (-accel*dt+math.sqrt(max(0., disc_hi)))/2))
                low = max(low, min(0., (accel*dt-math.sqrt(max(0., disc_lo)))/2))
                require(low <= high+1e-12, 'No feasible bounded reference continuation at J%d' % (index+1))
                nv = min(high, max(low, wanted))
                nq = current+.5*(vel+nv)*dt
                require(lo <= nq <= hi, 'Reference integration crosses actual command bound')
                next_q.append(nq)
                next_v.append(nv)
            next_q.append(desired[6])
            peak = max(peak, max(abs(a-b) for a, b in zip(next_q[:6], desired[:6])))
            q, velocity, when = next_q, next_v, when+dt
            nodes.append(ReferenceNode(when, tuple(q), tuple(velocity)))
        if old is not None:
            # Past executed nodes are not RTC evidence for a new observation.
            # Keep one interpolation anchor at/before that observation and all
            # still-committed nodes before cut, without accumulating a whole run.
            history_start = max(0, bisect.bisect_right(old.times, observation_time) - 1)
            nodes = [node for node in old.nodes[history_start:] if node.t < start_time-1e-9]+nodes
        return cls(nodes, execution_hz, send_hz), math.degrees(peak)


def _image_payload(image, codec):
    if codec == 'raw' and hasattr(image, 'shape'):
        require(tuple(image.shape) == (224, 224, 3) and str(image.dtype) == 'uint8',
                'RGB array must be uint8 [224,224,3]; resize in shared owner')
        image = image.tobytes(order='C')
    require(isinstance(image, (bytes, bytearray, memoryview)), 'Image must be real shared RGB bytes')
    data = bytes(image)
    if codec == 'raw':
        require(len(data) == RAW_BYTES, 'Raw RGB requires exactly 224*224*3 bytes')
    else:
        require(33 <= len(data) <= 262144 and data[:8] == b'\x89PNG\r\n\x1a\n' and
                data[8:16] == b'\0\0\0\rIHDR' and struct.unpack('!II', data[16:24]) == (224, 224)
                and data[24:26] == bytes((8, 2)), 'PNG must be 224x224 8-bit RGB')
    return data


def copy_source_timestamps(timestamps):
    """Bounded provenance copy; old packets remain explicitly legacy packets."""
    require(isinstance(timestamps, dict) and bool(timestamps), 'Actual source timestamps required')
    try:
        encoded = json.dumps(timestamps, separators=(',', ':'), allow_nan=False)
        require(len(encoded.encode('utf-8')) <= 12288, 'Oversized source timing metadata')
    except (ValueError, TypeError, RecursionError) as exc:
        raise PolicyError('Invalid source timing metadata') from exc
    result = copy.deepcopy(timestamps)
    if 'timing_v1' not in result:
        return result
    timing = result['timing_v1']
    require(isinstance(timing, dict) and timing.get('schema') == 'tiger-observation-timing-v1',
            'Unsupported observation timing schema')
    mode = timing.get('selection_mode')
    require(mode in ('independent_latest', 'query_grid'), 'Unsupported observation selection mode')
    aligned = mode == 'query_grid'
    require(timing.get('alignment_applied') is aligned, 'Observation alignment declaration differs from mode')
    domain = timing.get('clock_domain')
    require(isinstance(domain, dict) and bool(domain), 'Timing clock domain missing')
    acquired, ready = (timing.get(key) for key in ('observation_acquired_monotonic_ns', 'packet_ready_monotonic_ns'))
    require(type(acquired) is int and type(ready) is int and 0 <= acquired <= ready,
            'Invalid observation acquisition/packet times')
    sources = timing.get('sources')
    require(isinstance(sources, dict) and set(sources) == {'front', 'right', 'q12', 'q34', 'q56', 'gripper'},
            'Timing must explicitly describe all six sources')
    for name, source in sources.items():
        if source is None:  # Explicitly unknown legacy source; never backfill.
            require(not aligned, 'Aligned source cannot be unknown: ' + name)
            continue
        require(isinstance(source, dict), 'Missing source timing object: ' + name)
        require({'sample_id', 'source_monotonic_ns_estimate', 'available_monotonic_ns',
                 'source_timestamp_kind'}.issubset(source), 'Missing source timing fields: ' + name)
        source_ns, available = source['source_monotonic_ns_estimate'], source['available_monotonic_ns']
        for stamp in (source_ns, available):
            require(stamp is None or (type(stamp) is int and 0 <= stamp <= ready),
                    'Invalid/future source timing: ' + name)
        require(source_ns is None or available is None or source_ns <= available,
                'Source available before its source estimate: ' + name)
        if aligned:
            require(source.get('clock_domain_ref') == 'observation' and source.get('provenance_complete') is True
                    and source.get('mapping_valid') is True, 'Aligned source provenance incomplete: ' + name)
        else:
            require('clock_domain' in source and (source['clock_domain'] is None or source['clock_domain'] == domain),
                    'Source timing clock domain differs: ' + name)
    if aligned:
        require(domain.get('identity_verified') is True and domain.get('clock_name') == 'monotonic_ns'
                and all(isinstance(domain.get(key), str) and domain[key] for key in ('host_id', 'boot_id', 'session_id')),
                'Aligned observation requires verified host/boot/session domain')
        receipt = timing.get('selection_receipt')
        anchor = timing.get('selection_anchor_monotonic_ns')
        require(isinstance(receipt, dict) and receipt.get('schema') == 'tiger-runtime-alignment-v1'
                and receipt.get('ready') is True and not receipt.get('failures')
                and receipt.get('payload_validation') == 'runtime_rgb_shape_type_and_finite_7d_verified',
                'Aligned observation lacks a successful runtime selection receipt')
        selected_at = receipt.get('selection_now_ns')
        require(type(anchor) is int and type(selected_at) is int and 0 <= anchor <= selected_at <= ready
                and anchor == acquired and receipt.get('anchor_ns') == anchor, 'Invalid selection anchor/availability clock')
        selected = receipt.get('samples')
        limits = receipt.get('policy')
        require(isinstance(selected, dict) and set(selected) == set(sources) and isinstance(limits, dict),
                'Selection receipt/source set differs')
        for key in ('max_sample_age_ns', 'max_camera_skew_ns', 'max_state_skew_ns'):
            require(type(limits.get(key)) is int and limits[key] >= 0, 'Invalid declared alignment tolerance')
        for name, source in sources.items():
            measured = selected[name]
            require(isinstance(measured, dict) and measured.get('sample_id') == source['sample_id']
                    and measured.get('source_ns') == source['source_monotonic_ns_estimate']
                    and measured.get('available_ns') == source['available_monotonic_ns'], 'Selection identity differs: ' + name)
            source_ns, available = source['source_monotonic_ns_estimate'], source['available_monotonic_ns']
            require(type(source_ns) is int and type(available) is int and 0 <= source_ns <= anchor
                    and source_ns <= available <= selected_at
                    and selected_at - source_ns <= limits['max_sample_age_ns'], 'Selected source violates causal freshness: ' + name)
        for names, key in ((('front', 'right'), 'max_camera_skew_ns'),
                           (('q12', 'q34', 'q56', 'gripper'), 'max_state_skew_ns')):
            stamps = [sources[name]['source_monotonic_ns_estimate'] for name in names]
            require(max(stamps) - min(stamps) <= limits[key], 'Selected sources violate group skew')
    return result


def snapshot_to_observation(snapshot, session_id=None):
    """Consume existing shared snapshots; no capture/resize/state substitution.

    New schema: state_right_rad_m[7], front_png224/right_png224 bytes OR
    front_rgb224/right_rgb224 uint8 RGB arrays/bytes; source_timestamps dict;
    observation_monotonic float. Old {header, blobs, observation_monotonic} works.
    ``front`` maps to trained base_0_rgb; ``right`` to right_wrist_0_rgb.
    """
    require(isinstance(snapshot, dict), 'Shared snapshot must be a dictionary')
    if 'header' in snapshot:
        header = dict(snapshot['header'])
        require(len(snapshot.get('blobs', ())) == 2, 'Need front and right camera payloads')
        images = snapshot['blobs']
        codec = snapshot.get('codec', 'png')
    else:
        timestamps = snapshot.get('source_timestamps')
        require(isinstance(timestamps, dict) and bool(timestamps), 'Actual source timestamps required')
        header = {'state': snapshot.get('state_right_rad_m'), 'source_timestamps': timestamps,
                  'seed': snapshot.get('seed', 42), 'session_id': session_id or snapshot.get('session_id') or uuid.uuid4().hex,
                  'request_id': snapshot.get('request_id') or uuid.uuid4().hex}
        if 'front_png224_b64' in snapshot and 'right_png224_b64' in snapshot:
            require(all(isinstance(snapshot[k], str) and len(snapshot[k]) <= 349528 for k in
                        ('front_png224_b64', 'right_png224_b64')), 'Oversized/non-string base64 shared PNG')
            try:
                codec, images = 'png', [base64.b64decode(snapshot[k], validate=True) for k in
                                        ('front_png224_b64', 'right_png224_b64')]
            except (ValueError, TypeError) as exc:
                raise PolicyError('Invalid base64 shared PNG payload') from exc
        elif 'front_png224' in snapshot and 'right_png224' in snapshot:
            codec, images = 'png', [snapshot['front_png224'], snapshot['right_png224']]
        else:
            codec, images = 'raw', [snapshot.get('front_rgb224'), snapshot.get('right_rgb224')]
    require(codec in ('raw', 'png'), 'Unsupported codec')
    header['state'] = finite_vector(header.get('state'))
    require(isinstance(header.get('source_timestamps'), dict) and bool(header['source_timestamps']),
            'Observation source timestamps required; cannot hide stale input')
    # The pinned server rejects new top-level header fields, but echoes this
    # existing object exactly. Keep versioned timing here as the single source.
    header['source_timestamps'] = copy_source_timestamps(header['source_timestamps'])
    for k in ('request_id', 'session_id'):
        require(isinstance(header.get(k), str) and 1 <= len(header[k]) <= 128 and
                all(c.isascii() and (c.isalnum() or c in '_.:-') for c in header[k]), 'Invalid '+k)
    require(type(header.get('seed')) is int and 0 <= header['seed'] < 2**32, 'Seed must be uint32')
    mono = snapshot.get('observation_monotonic')
    require(type(mono) in (int, float) and math.isfinite(mono), 'Actual observation monotonic timestamp required')
    timing = header['source_timestamps'].get('timing_v1')
    if timing is not None:
        require(round(mono * 1e9) == timing['packet_ready_monotonic_ns'],
                'Observation packet-ready clock differs from source timing metadata')
    return {'header': header, 'blobs': [_image_payload(x, codec) for x in images],
            'codec': codec, 'observation_monotonic': float(mono)}


class PolicyClient:
    """One-request-at-a-time loopback client. Validates checkpoint and response identity."""
    def __init__(self, url='http://127.0.0.1:18002', timeout=5, execution_hz=18,
                 expected_server_sha=SERVER_SHA):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost') and
                parsed.path in ('', '/') and not parsed.username and not parsed.password and not parsed.query,
                'Policy endpoint must be local HTTP; use SSH tunnelling for remote services')
        require(execution_hz in (18, 30) and type(execution_hz) is int, 'Execution Hz must be explicit18 or30')
        require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0, 'Invalid RPC timeout')
        self.connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 18002, timeout=timeout)
        self.execution_hz = execution_hz
        self.lock = threading.Lock()
        self.session_id = uuid.uuid4().hex
        self.connection.request('GET', '/health')
        response = self.connection.getresponse()
        self._health = self._read(response)
        require(response.status == 200 and self._health.get('ready') is True and
                self._health.get('robot_command_sent') is False, 'Policy service not ready/hardware-free')
        require(expected_server_sha == SERVER_SHA and self._health.get('server_code_sha256') == SERVER_SHA,
                'RTC server source differs from reviewed original')
        for k, expected in {'protocol': PROTOCOL, 'selected_step': 4000, 'model_id': MODEL_ID,
                            'config_sha256': CONFIG_SHA, 'rtc_implementation': RTC_ALGORITHM,
                            'horizon': 15, 'physical_action_dim': 7, 'action_semantics': 'absolute command target',
                            'units': UNITS}.items():
            require(self._health.get(k) == expected, 'Policy health identity/semantics mismatch: '+k)

    def health(self):
        require(self.lock.acquire(blocking=False), 'Cannot query health during an in-flight request on same client')
        try:
            self.connection.request('GET', '/health')
            response = self.connection.getresponse()
            result = self._read(response)
            require(response.status == 200, 'Policy health query rejected')
            for k in ('model_id', 'protocol', 'selected_step', 'config_sha256', 'server_instance_id',
                      'server_code_sha256', 'rtc_implementation'):
                require(result.get(k) == self._health.get(k), 'Policy service changed: '+k)
            require(result.get('robot_command_sent') is False, 'Unexpected command-producing policy')
            return result
        finally:
            self.lock.release()

    @staticmethod
    def _read(response):
        data = response.read(MAX_JSON_REPLY + 1)
        require(len(data) <= MAX_JSON_REPLY, 'Oversized policy reply')
        def reject(value):
            raise PolicyError('Nonfinite JSON reply: '+value)
        try:
            result = json.loads(data, parse_constant=reject)
            require(isinstance(result, dict), 'Policy reply must be a JSON object')
            return result
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise PolicyError('Malformed policy JSON reply') from exc

    def set_timeout(self, seconds):
        require(type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0, 'Invalid timeout')
        self.connection.timeout = seconds
        if self.connection.sock is not None:
            self.connection.sock.settimeout(seconds)

    def infer(self, snapshot, prefix=None):
        observation = snapshot_to_observation(snapshot, self.session_id)
        header = {k: observation['header'][k] for k in ('request_id', 'session_id', 'state', 'source_timestamps', 'seed')}
        images = observation['blobs']
        prior = validate_prefix(prefix)
        header.update(protocol=PROTOCOL, codec=observation['codec'], head_bytes=len(images[0]),
                      right_bytes=len(images[1]), rtc_enabled=prior is not None, execution_label_hz=self.execution_hz)
        if prior is not None:
            header.update(prior)
        raw = json.dumps(header, separators=(',', ':'), allow_nan=False).encode('utf-8')
        require(len(raw) <= 16384, 'Oversized policy header')
        body = struct.pack('!I', len(raw)) + raw + images[0] + images[1]
        require(self.lock.acquire(blocking=False), 'Concurrent infer calls on one client are unsupported')
        started = time.monotonic()
        try:
            self.connection.request('POST', '/infer', body, {'Content-Type': 'application/octet-stream'})
            response = self.connection.getresponse()
            result = self._read(response)
        finally:
            self.lock.release()
        elapsed = time.monotonic() - started
        require(response.status == 200, 'Inference rejected: '+str(result))
        for k in ('model_id', 'protocol', 'selected_step', 'config_sha256', 'server_instance_id',
                  'server_code_sha256', 'rtc_implementation'):
            require(result.get(k) == self._health.get(k), 'Policy response source changed: '+k)
        require(result.get('robot_command_sent') is False, 'Unexpected robot-writing policy service')
        require(result.get('request_id') == header['request_id'] and result.get('session_id') == header['session_id'],
                'Policy request/session identity mismatch')
        require(result.get('horizon') == 15 and result.get('label_hz') == 30 and result.get('units') == UNITS and
                result.get('action_semantics') == 'absolute command target', 'Returned action semantics differ')
        require(result.get('source_timestamps') == header['source_timestamps'], 'Returned source timestamps changed')
        result['actions'] = validate_actions(result.get('actions'))
        rtc = result.get('rtc', {})
        require(rtc.get('enabled') is (prior is not None) and rtc.get('algorithm') == RTC_ALGORITHM and
                rtc.get('execution_label_hz') == self.execution_hz, 'RTC execution contract changed')
        if prior is not None:
            for k in ('previous_valid_steps', 'delay_steps', 'execution_horizon_steps'):
                require(rtc.get(k) == prior[k], 'RTC prefix echo mismatch: '+k)
        return result, elapsed

    def close(self):
        self.connection.close()


def shadow(client, snapshot_provider: Callable[[], dict], samples=3, event_callback=None):
    """Run actual live/output-only baseline tests; never provides a motion sink.

    Measures first request separately. No invented executed RTC prefix: live RTC
    prefixes belong to a genuine accepted execution queue in the hardware owner.
    """
    require(type(samples) is int and 1 <= samples <= 100, 'Shadow sample count1..100')
    records = []
    for index in range(samples):
        obs = snapshot_to_observation(snapshot_provider(), client.session_id)
        requested = time.monotonic()
        result, rpc_s = client.infer(obs)
        record = {'index': index, 'first_request': index == 0, 'rpc_s': rpc_s,
                  'source_age_at_request_s': requested - obs['observation_monotonic'],
                  'source_to_reply_s': time.monotonic() - obs['observation_monotonic'],
                  'result': result, 'robot_command_sent_by_shadow': False,
                  'physical_task_success': 'NOT_ASSESSED'}
        require(record['source_age_at_request_s'] >= 0, 'Future observation time on local clock')
        records.append(record)
        if event_callback:
            event_callback('shadow_sample', record)
    return {'status': 'PASS_FINITE_REAL_OBSERVATION_OUTPUT_ONLY', 'samples': records,
            'robot_command_sent': False, 'physical_task_success': 'NOT_ASSESSED',
            'measured_steady_rpc_s': [r['rpc_s'] for r in records[1:]]}


def _model_storage_module():
    # Also supports the existing isolated "-I policy.py" CLI and file-based
    # integrity tests, neither of which requires ambient PYTHONPATH.
    name = '_agilex_model_storage_v1'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('model_storage.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def model_storage_profile(root=None):
    module = _model_storage_module()
    legacy = dict(hostname=EXPECTED_HOST, usb=USB.as_posix(), usb_uuid='161A-2718',
                  original_bundle=ORIGINAL_BUNDLE.as_posix(), rtc_server=LEGACY_RTC.as_posix())
    try:
        if root is None:
            return module.ModelStorageProfile(**legacy, remote_bundle=None, explicit=False, config_path=None)
        return module.read_profile(root, legacy)
    except module.ModelStorageError as exc:
        raise PolicyError(str(exc)) from exc


def check_usb(path, root=None, profile=None):
    module = _model_storage_module()
    try:
        return module.verify_profile(profile if profile is not None else model_storage_profile(root), path, root)
    except module.ModelStorageError as exc:
        raise PolicyError(str(exc)) from exc


def _model_input(profile, path, label):
    module = _model_storage_module()
    try:
        module.contained_path(profile, path)
    except module.ModelStorageError as exc:
        raise PolicyError(str(exc)) from exc
    require(Path(path).is_file() and not Path(path).is_symlink(),
            'Missing/unsafe ' + label + '; model is not staged/ready: ' + str(path))
    return Path(path)


def load_exact(path, sha, name):
    require(Path(path).is_file() and not Path(path).is_symlink() and digest(path) == sha, 'Source checksum mismatch: '+str(path))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _new_json(path, value, root=None, profile=None):
    check_usb(path, root, profile)
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def model_plan(root):
    """Pure plan. No model load, extraction, subprocess or device access."""
    root = Path(root)
    profile = model_storage_profile(root)
    target = Path(profile.remote_bundle) if profile.remote_bundle is not None else root
    return {'selected_step': 4000, 'model_id': MODEL_ID, 'openpi_commit': COMMIT,
            'server_source': profile.rtc_server, 'server_sha256': SERVER_SHA,
            'original_bundle': profile.original_bundle, 'storage_profile': profile.describe(),
            'python': PYTHON, 'export_dir': (target / 'model-cache/delivered/model').as_posix(),
            'source_root_kind': 'verified original source in tmpfs; not installed act/openpi checkout',
            'sampler_steps_candidate': 5, 'sampler_steps_checkpoint_reference': 10,
            'original_precision': 'BF16 vision-language backbone, FP32 action expert; exact CPU text-table gather',
            'execution_hz': 18, 'training_hz': 30, 'horizon': 15, 'physical_dim': 7,
            'scope': 'right arm only; front + right camera RGB',
            'foreground_service_command': [PYTHON, '-I', '-B', (target / 'agilex_control/policy.py').as_posix(),
                                           'serve', '--root', target.as_posix(), '--num-steps', '5'],
            'stage_command': ['/usr/bin/python3', '-I', '-B', (target / 'agilex_control/policy.py').as_posix(),
                              'stage', '--root', target.as_posix()],
            'robot_commands_sent': False}


def stage_model(root):
    """Verify original archive and restore to NEW mounted USB files only.

    Safe restoration is the existing authenticated delivery implementation.
    No download, install, SDK, mode change, model import, or motion. First run
    reads 6.79GB shards twice (shard + streamed-file verification); can take time.
    Partial failures are preserved and never mistaken for a completed model.
    """
    root = Path(root).resolve()
    profile = model_storage_profile(root)
    check_usb(root, root, profile)
    original, rtc_server = Path(profile.original_bundle), Path(profile.rtc_server)
    complete_path = _model_input(profile, original / 'COMPLETE.json', 'original COMPLETE receipt')
    _model_input(profile, original / '2026-10-02-23-35-delivery.py', 'pinned delivery helper')
    _model_input(profile, rtc_server, 'pinned RTC server')
    require(digest(complete_path) == COMPLETE_SHA, 'Original COMPLETE receipt differs')
    delivery = load_exact(original / '2026-10-02-23-35-delivery.py', DELIVERY_SHA, '_unified_exact_delivery')
    complete = delivery.validate_complete(delivery.load_json(complete_path))
    require(complete['selected_step'] == 4000, 'Wrong source checkpoint')
    require(max(x['bytes'] for x in complete['files'].values()) < 4 * 1024**3 - 1,
            'Source member exceeds vfat4GB file limit')
    cache = root / 'model-cache'
    destination = cache / 'delivered'
    marker = cache / 'stage-verified.json'
    check_usb(destination / 'model', root, profile)
    check_usb(marker, root, profile)
    if marker.is_file():
        receipt = json.loads(marker.read_text(encoding='utf-8'))
        require(receipt.get('status') == 'PASS' and receipt.get('complete_sha256') == COMPLETE_SHA,
                'Existing stage receipt identity mismatch')
        # Validate actual export files, not just receipt presence, on every reuse.
        print('MODEL_STAGE: verifying existing export files; no extraction or robot commands', flush=True)
        server = load_exact(rtc_server, SERVER_SHA, '_unified_exact_rtc_server')
        server.verified_export(destination / 'model')
        return receipt
    require(not destination.exists(), 'Incomplete stage exists; preserve and inspect it before retry')
    require(shutil.disk_usage(root).free > complete['archive']['bytes'] + 1024**3, 'USB free space insufficient')
    cache.mkdir(parents=True, exist_ok=True)
    print('MODEL_STAGE: verifying original archive shards, then extracting and verifying each member; '
          'USB reads may take minutes. No download or robot commands. Archive bytes=' +
          str(complete['archive']['bytes']), flush=True)
    result = delivery.verify_bundle(original, destination, before_write=lambda: check_usb(destination, root, profile))
    require(digest(destination / 'model/export_manifest.json') == EXPORT_SHA, 'Staged export receipt mismatch')
    server = load_exact(rtc_server, SERVER_SHA, '_unified_exact_rtc_server')
    print('MODEL_STAGE: extraction complete; checking exported weight identity', flush=True)
    proof = server.verified_export(destination / 'model')
    result.update(complete_sha256=COMPLETE_SHA, export_dir=str(destination / 'model'),
                  export_proof=proof, robot_command_sent=False, originals_changed=False)
    _new_json(marker, result, root, profile)
    print('MODEL_STAGE: all checks passed; ready for explicit model-start', flush=True)
    return result


def _extract_source(archive, destination):
    """Bounded source extraction into new tmpfs, including validated symlinks."""
    require(destination.parent == Path('/dev/shm') and not destination.exists(), 'New tmpfs source destination required')
    require(subprocess.check_output(['findmnt', '-n', '-o', 'FSTYPE', '--target', '/dev/shm'], text=True).strip() == 'tmpfs',
            'Source staging must be RAM tmpfs')
    with tarfile.open(archive, 'r:') as tar:
        members = tar.getmembers()
        require(len(members) <= 20000, 'Source member count bound exceeded')
        entries, total = {}, 0
        import posixpath
        for member in members:
            parts = member.name.split('/')
            require(member.name and not member.name.startswith('/') and '\\' not in member.name and
                    all(p not in ('', '.', '..') for p in parts) and member.name not in entries,
                    'Unsafe/duplicate source member: '+member.name)
            require(member.isfile() or member.isdir() or member.issym(), 'Unsupported source member type')
            entries[member.name] = member
            if member.isfile():
                total += member.size
                require(member.size >= 0 and total <= 1024**3, 'Source payload bound exceeded')
            if member.issym():
                target = posixpath.normpath(posixpath.join(str(PurePosixPath(member.name).parent), member.linkname))
                require(member.linkname and not member.linkname.startswith('/') and '\\' not in member.linkname and
                        target != '..' and not target.startswith(('../', '/')), 'Unsafe source symlink')
        for name in entries:
            for parent in PurePosixPath(name).parents:
                if str(parent) == '.':
                    break
                require(str(parent) not in entries or entries[str(parent)].isdir(), 'Source symlink/file ancestor refused')
        require(shutil.disk_usage('/dev/shm').free > total + 256 * 1024**2, 'RAM source staging space insufficient')
        destination.mkdir(mode=0o700)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
            elif member.isfile():
                with tar.extractfile(member) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst, 1024**2)
        for member in members:
            if member.issym():
                os.symlink(member.linkname, destination.joinpath(*PurePosixPath(member.name).parts))
    return {'source_root': str(destination), 'ordinary_bytes': total, 'members': len(members)}


def serve_model(root, num_steps=5, port=18002):
    """Foreground exact model service; lock/GPU ownership handled by root launcher."""
    root = Path(root).resolve()
    profile = model_storage_profile(root)
    check_usb(root, root, profile)
    rtc_server = Path(profile.rtc_server)
    export = root / 'model-cache/delivered/model'
    marker = root / 'model-cache/stage-verified.json'
    if profile.explicit:
        # Fail missing migration inputs before package/model import or GPU use.
        for path, label in ((marker, 'authenticated stage receipt'), (rtc_server, 'pinned RTC server'),
                            (export / 'export_manifest.json', 'export manifest'),
                            (export / ('openpi-source-' + COMMIT + '.tar'), 'pinned source tar'),
                            (export / '2026-10-02-23-55-inference-smoke.py', 'pinned model helper'),
                            (export / 'paligemma_tokenizer.model', 'tokenizer')):
            _model_input(profile, path, label)
        receipt = json.loads(marker.read_text(encoding='utf-8'))
        require(receipt.get('status') == 'PASS' and receipt.get('complete_sha256') == COMPLETE_SHA,
                'Existing stage receipt identity mismatch')
    require(sys.version_info[:2] == (3, 11), 'Use exact OpenPI Python3.11 interpreter')
    require(num_steps in (5, 10) and type(num_steps) is int, 'Sampler must be5 candidate or10 reference')
    require(type(port) is int and 1024 <= port <= 65535, 'Invalid local service port')
    versions = {k: importlib.metadata.version(k) for k in PINNED_VERSIONS}
    require(versions == PINNED_VERSIONS, 'OpenPI JAX/Flax/Orbax runtime version drift: '+str(versions))
    require(marker.is_file(), 'Run authenticated model staging first: ' + str(marker))
    print(json.dumps({'event': 'VERIFYING_MODEL_SOURCES',
                      'scope': 'manifest identity, pinned source, helper and tokenizer; weights are checked by ModelEngine next',
                      'robot_command_sent': False}), flush=True)
    server = load_exact(rtc_server, SERVER_SHA, '_unified_exact_rtc_server')
    require(export.is_dir() and not export.is_symlink() and
            not any(parent.is_symlink() for parent in (export.parent, export.parent.parent)),
            'Export directory path must not be a symbolic link')
    require(digest(export / 'export_manifest.json') == EXPORT_SHA, 'Export manifest identity mismatch')
    # The pinned ModelEngine starts with verified_export(self.export), before
    # helper import, parameter restore or GPU placement. Do not read/hash every
    # weight twice here: source/helper/token identities below are checked before
    # use, and the full per-file verification remains mandatory in that engine.
    require(digest(export / ('openpi-source-'+COMMIT+'.tar')) == SOURCE_SHA, 'Pinned source tar hash differs')
    source = Path('/dev/shm') / ('tiger-unified-openpi-'+uuid.uuid4().hex[:12])
    extraction = _extract_source(export / ('openpi-source-'+COMMIT+'.tar'), source)
    helper = load_exact(export / '2026-10-02-23-55-inference-smoke.py', HELPER_SHA, '_unified_model_helper')
    source_proof = helper.verify_source(source, COMMIT)
    # Isolated interpreter ignores ambient PYTHONPATH; insert only authenticated source.
    require(not any(k == 'openpi' or k.startswith('openpi.') for k in sys.modules), 'OpenPI already imported before source attestation')
    sys.path[:0] = [str(source / 'src'), str(source / 'packages/openpi-client/src')]
    require(helper.installed_source_root() == source, 'Restored pinned source does not own OpenPI import')
    cache = root / 'model-cache/runtime-cache'
    check_usb(cache, root, profile)
    for name in ('big_vision', 'tmp', 'cuda', 'jax', 'xdg'):
        check_usb(cache / name, root, profile)
        (cache / name).mkdir(parents=True, exist_ok=True)
    token = cache / 'big_vision/paligemma_tokenizer.model'
    check_usb(token, root, profile)
    if not token.exists():
        with (export / 'paligemma_tokenizer.model').open('rb') as src, token.open('xb') as dst:
            shutil.copyfileobj(src, dst)
    require(digest(token) == TOKEN_SHA, 'Runtime tokenizer differs')
    # Scoped to this foreground process. Do not source ROS/Conda shell startup.
    os.environ.update(CUDA_VISIBLE_DEVICES='0', JAX_PLATFORMS='cuda',
                      XLA_PYTHON_CLIENT_PREALLOCATE='false', XLA_PYTHON_CLIENT_ALLOCATOR='platform',
                      PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1', OPENPI_DATA_HOME=str(cache),
                      XDG_CACHE_HOME=str(cache/'xdg'), CUDA_CACHE_PATH=str(cache/'cuda'),
                      JAX_COMPILATION_CACHE_DIR=str(cache/'jax'), TMPDIR=str(cache/'tmp'), WANDB_MODE='disabled')
    print(json.dumps({'event': 'VERIFYING_LOADING_WARMING_ORIGINAL', 'versions': versions, 'source': source_proof,
                      'extraction': extraction, 'num_steps': num_steps,
                      'scope': 'one full export verification, parameter restore/GPU placement and base/RTC warmup; port binds only after all complete',
                      'robot_command_sent': False}), flush=True)
    engine = server.ModelEngine(export, seed=42, num_steps=num_steps, max_guidance_weight=5.0, rtc_warmup=True)
    service = server.PolicyServer(('127.0.0.1', port), engine)
    print(json.dumps({'event': 'READY', **service.identity(), 'warmup': engine.warmup,
                      'verification': engine.verification, 'robot_command_sent': False}), flush=True)
    try:
        service.serve_forever(poll_interval=0.25)
    finally:
        service.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'stage', 'serve'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--num-steps', type=int, choices=(5, 10), default=5)
    parser.add_argument('--port', type=int, default=18002)
    args = parser.parse_args()
    if args.command == 'serve':
        serve_model(args.root, args.num_steps, args.port)
    else:
        result = model_plan(args.root) if args.command == 'plan' else stage_model(args.root)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
