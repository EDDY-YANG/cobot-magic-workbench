"""Read completed native recording files; never open cameras, CAN or network.

PASS concerns recorded-file integrity/freshness, not physical success or proof
that no other process transmitted CAN. MP4s/CSVs are optional review derivatives.
Pillow is required for complete JPEG decoding. Review export uses existing PyAV
for timestamped H.264 encoding and existing ffmpeg for full decode validation.
Nothing is installed or silently substituted.

Core copied from the actually accepted VFR checker SHA-256
84a0c34118c6b59a4623942746ae5266bb5a1c1af278f43b295797a03edc1d55.
New export entry requires already-open USB directory descriptors. No mkdir or
arbitrary output CLI remains; /proc/self/fd anchors cannot fall onto an SSD.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
from fractions import Fraction
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import struct
import subprocess

HEADER = struct.Struct('<QI')
MAX_PAYLOAD = 4 * 1024 * 1024
COMMAND_IDS = {0x150, 0x151, 0x152, 0x154, 0x155, 0x156, 0x157, 0x159}
VIDEO_TIME_BASE = Fraction(1, 1_000_000)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def vector(value, size=6):
    return isinstance(value, list) and len(value) == size and all(finite(v) for v in value)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()


def signature(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def derivative_path(root, output):
    """Only new review artifacts under this episode/evidence; no SSD fallback."""
    original = Path(output).absolute()
    candidate = original.resolve()
    evidence = root / 'evidence'
    if not candidate.is_relative_to(evidence) or candidate == root:
        raise ValueError('Review artifacts must stay inside this episode/evidence directory')
    cursor = original
    while cursor != root and cursor != cursor.parent:
        if cursor.exists() and cursor.is_symlink():
            raise ValueError('Review output contains a symlink')
        cursor = cursor.parent
    return candidate


class Checker:
    def __init__(self, root, cameras, arms, feedback_age, camera_age, image_size, *, root_fd=None):
        self.root = Path(root)
        if root_fd is None:
            if self.root.is_symlink():
                raise ValueError('Use the actual episode directory, not a navigation symlink')
            self.root = self.root.resolve(strict=True)
        else:
            if os.name != 'posix' or not Path('/proc/self/fd').is_dir():
                raise RuntimeError('Anchored native audit requires Linux procfs')
            os.fstat(root_fd)
            self.root = Path('/proc/self/fd') / str(root_fd)
        self.cameras, self.arms = tuple(cameras), tuple(arms)
        self.feedback_age, self.camera_age = feedback_age, camera_age
        self.image_size = tuple(image_size)
        self.errors, self.warnings = [], []
        self.error_counts = collections.Counter()
        self.frames = {role: {} for role in self.cameras}
        self.streams, self.states = {}, []
        self.previous = {}
        self.can_counts = {role: collections.Counter() for role in self.arms}
        self.command_targets = {role: collections.Counter() for role in self.arms}
        self.initial = {}
        self.socket_rx = {role: [] for role in self.arms}
        self.state_clock_domain = None

    def error(self, kind, detail):
        self.error_counts[kind] += 1
        if len(self.errors) < 40:
            self.errors.append({'kind': kind, 'detail': str(detail)})

    def chunks(self, path, stream_name, callback):
        h = hashlib.sha256()
        count, first, last = 0, None, None
        with path.open('rb') as handle:
            while True:
                header = handle.read(HEADER.size)
                if not header:
                    break
                h.update(header)
                if len(header) != HEADER.size:
                    self.error('truncated_header', path.name)
                    break
                stamp, length = HEADER.unpack(header)
                if not 0 < length <= MAX_PAYLOAD:
                    self.error('invalid_payload_length', (path.name, length))
                    break
                offset = handle.tell()
                payload = handle.read(length)
                h.update(payload)
                if len(payload) != length:
                    self.error('truncated_payload', (path.name, count))
                    break
                if stamp <= self.previous.get(stream_name, 0):
                    self.error('nonincreasing_timestamp', (path.name, count, stamp))
                self.previous[stream_name] = stamp
                first = stamp if first is None else first
                last = stamp
                try:
                    callback(stamp, payload, path, offset)
                except Exception as exc:
                    self.error('record_decode_failure', (path.name, count, repr(exc)))
                count += 1
        self.streams[path.name] = {'records': count, 'bytes': path.stat().st_size,
            'sha256': digest(path), 'first_timestamp_ns': first, 'last_timestamp_ns': last}

    def camera_record(self, role, stamp, payload, path, offset):
        from PIL import Image
        if not payload.startswith(b'\xff\xd8') or not payload.endswith(b'\xff\xd9'):
            raise ValueError('Missing JPEG boundaries')
        with Image.open(io.BytesIO(payload)) as image:
            image.load()  # Complete entropy decoding, not only JPEG marker checks.
            if image.format != 'JPEG' or image.size != self.image_size:
                raise ValueError('Unexpected JPEG format or geometry: ' + str(image.size))
            if image.mode not in ('RGB', 'L'):
                raise ValueError('Unexpected JPEG mode: ' + image.mode)
        if stamp in self.frames[role]:
            self.error('duplicate_camera_stamp', (role, stamp))
        self.frames[role][stamp] = {'path': str(path), 'offset': offset, 'length': len(payload),
                                   'sha256': hashlib.sha256(payload).hexdigest()}

    def can_record(self, role, stamp, payload, path, offset):
        if not 4 <= len(payload) <= 12:
            raise ValueError('Raw CAN payload must contain uint32 id and 0..8 data bytes')
        cid = struct.unpack('<I', payload[:4])[0]
        self.can_counts[role][cid] += 1

    def state_record(self, stamp, payload, path, offset):
        value = json.loads(payload, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
        if value.get('sample_wall_ns') != stamp:
            self.error('state_timestamp_mismatch', stamp)
        sensors = value.get('sensors', {})
        index = value.get('camera_source_index_v1', {})
        domain = index.get('clock_domain', {})
        if (index.get('schema') != 'tiger-recorded-camera-source-index-v1'
                or domain.get('identity_verified') is not True):
            self.error('camera_source_domain_missing_or_invalid', stamp)
        sample_mono = index.get('sample_monotonic_ns')
        references = value.get('camera_sample_ns', {})
        row = {'state_timestamp_ns': stamp, 'state_index': len(self.states), 'sample_monotonic_ns': sample_mono}
        if (type(sample_mono) is not int or sample_mono <= 0 or self.states
                and type(self.states[-1]['sample_monotonic_ns']) is int
                and sample_mono <= self.states[-1]['sample_monotonic_ns']):
            self.error('state_monotonic_time_missing_or_reversed', stamp)
        clock = {key: domain.get(key) for key in ('host_id', 'boot_id', 'clock_name')}
        if self.state_clock_domain is None:
            self.state_clock_domain = clock
        if clock != self.state_clock_domain or not all(isinstance(value, str) and value for value in clock.values()):
            self.error('state_clock_domain_missing_or_changed', stamp)
        for role in self.arms:
            arm = sensors.get('arms', {}).get(role, {})
            q, grip = arm.get('q_deg'), arm.get('grip_m')
            overflow = arm.get('socket_rx_overflow_packets')
            if type(overflow) is not int or overflow < 0:
                self.error('CAN_socket_RX_counter_missing', (role, stamp))
            else:
                prior = self.socket_rx[role]
                if prior and overflow < prior[-1]:
                    self.error('CAN_socket_RX_counter_reversed', (role, stamp))
                prior.append(overflow)
            ages = [arm.get(k) for k in ('age_joint_s', 'age_status_s', 'age_gripper_s')]
            if (not vector(q) or not finite(grip) or arm.get('feedback_fresh') is not True
                    or arm.get('observer_running') is not True or arm.get('observer_error')
                    or not arm.get('status_hex')
                    or not all(finite(a) and 0 <= a <= self.feedback_age for a in ages)):
                self.error('actual_feedback_missing_or_stale', (role, stamp))
            row[role + '_q_deg'] = q if vector(q) else [None] * 6
            row[role + '_grip_m'] = grip
            row[role + '_feedback_fresh'] = arm.get('feedback_fresh')
            target = arm.get('action_targets') or {}
            target_ok = vector(target.get('q_deg')) and finite(target.get('grip_m'))
            target_ages = list(target.get('pair_ages_s') or []) + [target.get('grip_age_s')]
            target_fresh = target_ok and len(target_ages) == 4 and all(
                finite(a) and 0 <= a <= .1 for a in target_ages)
            self.command_targets[role]['samples'] += 1
            self.command_targets[role]['finite_targets'] += int(target_ok)
            self.command_targets[role]['targets_with_age_le_100ms'] += int(target_fresh)
            row[role + '_target_q_deg'] = target.get('q_deg') if target_ok else [None] * 6
            row[role + '_target_grip_m'] = target.get('grip_m')
            row[role + '_target_fresh_100ms'] = target_fresh
        for role in self.cameras:
            ref = references.get(role)
            frame = self.frames[role].get(ref)
            if frame is None or type(ref) is not int or not 0 < ref <= stamp:
                self.error('camera_reference_unresolved_or_future', (role, stamp, ref))
            elif (stamp - ref) / 1e9 > self.camera_age:
                self.error('camera_reference_stale', (role, stamp, ref))
            source = index.get('cameras', {}).get(role, {})
            timing = source.get('source_timing', {})
            mono, available = timing.get('source_monotonic_ns_estimate'), source.get('available_monotonic_ns')
            if (source.get('wall_ns') != ref or timing.get('clock_domain') != domain
                    or timing.get('mapping_valid') is not True
                    or not all(type(v) is int for v in (mono, available, sample_mono))
                    or not (0 <= mono <= available <= sample_mono)
                    or (sample_mono - mono) / 1e9 > self.camera_age):
                self.error('camera_source_provenance_invalid', (role, stamp))
            row[role + '_frame_timestamp_ns'] = ref
            row[role + '_jpeg_sha256'] = None if frame is None else frame['sha256']
        self.states.append(row)

    def check(self):
        operation = self.root / 'operation.json'
        paths = [operation, *sorted(self.root.glob('*.bin'))]
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise ValueError('Input is missing or a symlink: ' + str(path))
            self.initial[path.name] = signature(path)
        report = json.loads(operation.read_text(encoding='utf-8-sig'))
        if report.get('operation') != 'episode' or report.get('format') != 'tiger-native-sensors-v1':
            self.error('operation_format_invalid', report.get('format'))
        if report.get('status') != 'RECORDED_NATIVE_REVIEW_REQUIRES_QC':
            self.error('operation_not_finalized_successfully', report.get('status'))
        if report.get('failure') or report.get('queue_overflows') != 0 or report.get('fsync_requested') is not True:
            self.error('recording_failure_overflow_or_unflushed', {
                k: report.get(k) for k in ('failure', 'queue_overflows', 'fsync_requested')})
        if report.get('required_camera_roles') != list(self.cameras) or report.get('required_arm_roles') != list(self.arms):
            self.error('capture_roles_differ', (report.get('required_camera_roles'), report.get('required_arm_roles')))
        groups = collections.defaultdict(list)
        for path in paths[1:]:
            match = re.fullmatch(r'(.+)-(\d{4})\.bin', path.name)
            if not match:
                self.error('unknown_bin_name', path.name)
                continue
            groups[match[1]].append((int(match[2]), path))
        expected = ['state', *self.cameras, *('can-' + role for role in self.arms)]
        if set(groups) != set(expected):
            self.error('stream_set_differs', {'actual': sorted(groups), 'expected': expected})
        for name, files in groups.items():
            files.sort()
            if [n for n, _ in files] != list(range(len(files))):
                self.error('stream_part_sequence_gap', name)
        # Parse cameras first, enabling exact state-to-original-frame references.
        for role in self.cameras:
            for _, path in groups.get(role, []):
                self.chunks(path, role, lambda *args, r=role: self.camera_record(r, *args))
        for role in self.arms:
            for _, path in groups.get('can-' + role, []):
                self.chunks(path, 'can-' + role, lambda *args, r=role: self.can_record(r, *args))
        for _, path in groups.get('state', []):
            self.chunks(path, 'state', self.state_record)
        if not self.states:
            self.error('empty_state_stream', 'No decodable state samples')
        actual_cameras = {role: len(self.frames[role]) for role in self.cameras}
        actual_can = {role: sum(self.can_counts[role].values()) for role in self.arms}
        if report.get('samples') != len(self.states):
            self.error('sample_count_mismatch', (report.get('samples'), len(self.states)))
        if report.get('camera_frame_counts') != actual_cameras or any(v == 0 for v in actual_cameras.values()):
            self.error('camera_count_mismatch_or_empty', (report.get('camera_frame_counts'), actual_cameras))
        if report.get('raw_CAN_counts') != actual_can or any(v == 0 for v in actual_can.values()):
            self.error('can_count_mismatch_or_empty', (report.get('raw_CAN_counts'), actual_can))
        raw_bytes = sum(path.stat().st_size for path in paths[1:])
        if report.get('raw_saved_bytes') != raw_bytes:
            self.error('raw_byte_count_mismatch', (report.get('raw_saved_bytes'), raw_bytes))
        quality = report.get('quality_summary', {})
        for key in ('gripper_fault_samples', 'joint_fault_messages', 'arm_error_messages'):
            if quality.get(key, 0):
                self.error('recorded_hardware_fault', (key, quality[key]))
        if quality.get('gripper_unknown_samples', 0):
            self.warnings.append('Recorded gripper status includes unknown samples; inspect raw evidence')
        intervals = [(b['state_timestamp_ns'] - a['state_timestamp_ns']) / 1e9
                     for a, b in zip(self.states, self.states[1:])]
        feedback = {}
        socket_rx = {}
        for role in self.arms:
            values = self.socket_rx[role]
            delta = values[-1] - values[0] if values else None
            socket_rx[role] = {'first': values[0] if values else None, 'last': values[-1] if values else None,
                               'delta': delta, 'samples': len(values),
                               'scope': 'recorded native state coverage only; historical cumulative baseline retained'}
            if len(values) != len(self.states) or delta != 0:
                self.error('CAN_socket_RX_new_overflow_or_incomplete', role)
            valid = [r[role + '_q_deg'] for r in self.states if vector(r[role + '_q_deg'])]
            feedback[role] = {'valid_rows': len(valid), 'first_q_deg': valid[0] if valid else None,
                'last_q_deg': valid[-1] if valid else None,
                'observed_joint_span_deg': [max(q[k] for q in valid) - min(q[k] for q in valid)
                                             for k in range(6)] if valid else None}
        return {'schema': 'tiger-native-recording-file-audit-v1', 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'episode_path': str(self.root), 'operation_sha256': digest(operation), 'recorded_operation': report,
            'scope': 'completed native files only; not training acceptance, hardware success, or proof of no CAN transmission',
            'samples': len(self.states), 'camera_frame_counts': actual_cameras, 'raw_CAN_counts': actual_can,
            'socket_rx_overflow_counters': socket_rx, 'state_clock_domain': self.state_clock_domain,
            'state_monotonic_span_ns': [self.states[0]['sample_monotonic_ns'], self.states[-1]['sample_monotonic_ns']] if self.states else None,
            'raw_saved_bytes': raw_bytes, 'files': self.streams, 'feedback': feedback,
            'state_timing': {'intervals': len(intervals), 'max_interval_s': max(intervals) if intervals else None,
                'median_interval_s': statistics.median(intervals) if intervals else None,
                'gaps_over_50ms': sum(v > .05 for v in intervals)},
            'command_evidence': {'bus_observed_ids': {r: {hex(k): v for k, v in self.can_counts[r].items()} for r in self.arms},
                'control_id_counts': {r: {hex(k): v for k, v in self.can_counts[r].items() if k in COMMAND_IDS} for r in self.arms},
                'state_action_targets': {r: dict(v) for r, v in self.command_targets.items()},
                'finite_target_coverage_complete': all(v['samples'] > 0 and v['samples'] == v['finite_targets']
                                                      for v in self.command_targets.values()),
                'sender_identity_verified': False, 'no_CAN_transmission_proven': False,
                'required_external_evidence': 'physical-interface TX counters before/after plus actual process/owner audit; filtered RX is insufficient'},
            'hardware_exposure_timestamp_verified': False, 'model_or_device_execution_by_analyzer': False}

    def unchanged(self):
        current = {'operation.json', *(p.name for p in self.root.glob('*.bin'))}
        if current != set(self.initial):
            self.error('input_file_set_changed', 'Source changed during analysis')
        for name, before in self.initial.items():
            path = self.root / name
            if not path.exists() or path.is_symlink() or signature(path) != before:
                self.error('input_file_changed', name)


def source_video_pts(timestamps_ns):
    """Map actual state deltas to microsecond PTS; never invent a 25/30Hz grid."""
    if not timestamps_ns or any(type(v) is not int for v in timestamps_ns):
        raise ValueError('Video requires actual integer state timestamps')
    first = timestamps_ns[0]
    result = [(stamp - first + 500) // 1000 for stamp in timestamps_ns]
    if any(b <= a for a, b in zip(timestamps_ns, timestamps_ns[1:])):
        raise ValueError('Source video timestamps are not strictly increasing')
    if any(b <= a for a, b in zip(result, result[1:])):
        raise ValueError('Source timestamps collide at microsecond video resolution; no invented spacing')
    return result


def validate_video_pts(video, timestamps_ns, image_size):
    """Decode every frame and compare rational container PTS with source time."""
    import av
    first = timestamps_ns[0]
    count, max_error, previous = 0, Fraction(0), None
    time_bases = set()
    with av.open(str(video), mode='r') as container:
        for frame in container.decode(video=0):
            if count >= len(timestamps_ns) or frame.pts is None or frame.time_base is None:
                raise RuntimeError('Decoded MP4 frame count/PTS differs from state samples')
            if (frame.width, frame.height) != tuple(image_size):
                raise RuntimeError('Decoded MP4 geometry differs from original frames')
            actual = frame.pts * Fraction(frame.time_base)
            expected = Fraction(timestamps_ns[count] - first, 1_000_000_000)
            error = abs(actual - expected)
            if error > VIDEO_TIME_BASE or previous is not None and actual <= previous:
                raise RuntimeError('MP4 presentation timestamp does not match actual state time')
            max_error = max(max_error, error)
            previous = actual
            time_bases.add(str(frame.time_base))
            count += 1
    if count != len(timestamps_ns):
        raise RuntimeError('Decoded MP4 is missing recorded state presentation frames')
    return {'decoded_frames': count, 'expected_frames': len(timestamps_ns),
            'decoded_time_bases': sorted(time_bases),
            'maximum_absolute_source_timestamp_error_ns': float(max_error * 1_000_000_000),
            'accepted_timestamp_error_ns': 1000,
            'presentation_timestamp_validation': 'PASS'}


def encode_video_pyav(checker, role, video):
    """One presentation frame per recorded state, preserving irregular timing."""
    import av
    from PIL import Image
    stamps = [row['state_timestamp_ns'] for row in checker.states]
    pts = source_video_pts(stamps)
    # rate supplies the final-frame nominal duration; actual frame times come
    # exclusively from PTS/time_base. No CFR resampler or duplication is used.
    with Path(video).open('xb') as handle:
        with av.open(handle, mode='w', format='mp4') as container:
            stream = container.add_stream('libx264', rate=30)
            stream.width, stream.height = checker.image_size
            stream.pix_fmt = 'yuv420p'
            stream.time_base = VIDEO_TIME_BASE
            stream.codec_context.time_base = VIDEO_TIME_BASE
            stream.options = {'crf': '18', 'preset': 'veryfast', 'bf': '0'}
            for row, timestamp in zip(checker.states, pts):
                reference = checker.frames[role][row[role + '_frame_timestamp_ns']]
                with Path(reference['path']).open('rb') as source:
                    source.seek(reference['offset'])
                    payload = source.read(reference['length'])
                if hashlib.sha256(payload).hexdigest() != reference['sha256']:
                    raise RuntimeError('Original JPEG changed before video encoding')
                with Image.open(io.BytesIO(payload)) as image:
                    image.load()
                    frame = av.VideoFrame.from_image(image.convert('RGB'))
                frame.pts = timestamp
                frame.time_base = VIDEO_TIME_BASE
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        handle.flush()
        os.fsync(handle.fileno())
    validation = validate_video_pts(video, stamps, checker.image_size)
    return {'encoder': 'PyAV/libx264', 'pyav_version': av.__version__,
            'library_versions': av.library_versions, 'configured_time_base': str(VIDEO_TIME_BASE),
            'source_frames': len(stamps), 'PTS': 'round((state_wall_ns-first_state_wall_ns)/1000)',
            'frame_timestamps_are_CFR': False, **validation}


def full_decode_command(executable, video):
    # FFmpeg4.2 otherwise defaults the null encoder to 1/framerate and can
    # round distinct VFR frames onto the same DTS. Preserve input time base.
    # https://ffmpeg.org/doxygen/4.2/ffmpeg_8c_source.html (init_encoder_time_base)
    return [str(executable), '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-i', str(video), '-vsync', '0', '-enc_time_base', '-1', '-f', 'null', '-']


def full_decode_video(executable, video, output_fd):
    # System FFmpeg must resolve its system libraries, not the caller's Conda
    # libraries. Sanitize this child only; leave the current Python untouched.
    child_env = os.environ.copy()
    child_env.pop('LD_LIBRARY_PATH', None)
    child_env.pop('LD_PRELOAD', None)
    return subprocess.run(full_decode_command(executable, video), capture_output=True,
                          text=True, timeout=120, pass_fds=(output_fd,), env=child_env)


def export_review(checker, output, ffmpeg, *, output_fd=None):
    """VFR H.264 review derivatives; originals stay untouched and remain truth."""
    executable = ffmpeg or shutil.which('ffmpeg')
    if not executable or not Path(executable).is_file():
        raise RuntimeError('Existing ffmpeg required for MP4 export; nothing is installed automatically')
    if output_fd is None or os.name != 'posix':
        raise RuntimeError('Exports require a verified, already-open USB output directory')
    os.fstat(output_fd)
    output = Path('/proc/self/fd') / str(output_fd)
    columns = ['state_index', 'state_timestamp_ns', 'sample_monotonic_ns']
    for role in checker.arms:
        columns += [role + '_q' + str(k + 1) + '_deg' for k in range(6)]
        columns += [role + '_grip_m', role + '_feedback_fresh']
        columns += [role + '_target_q' + str(k + 1) + '_deg' for k in range(6)]
        columns += [role + '_target_grip_m', role + '_target_fresh_100ms']
    for role in checker.cameras:
        columns += [role + '_frame_timestamp_ns', role + '_jpeg_sha256']
    csv_path = output / 'joint-state-and-observed-targets.csv'
    with csv_path.open('x', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for source in checker.states:
            row = dict(source)
            for role in checker.arms:
                q, target = row.pop(role + '_q_deg'), row.pop(role + '_target_q_deg')
                row.update({role + '_q' + str(k + 1) + '_deg': q[k] for k in range(6)})
                row.update({role + '_target_q' + str(k + 1) + '_deg': target[k] for k in range(6)})
            writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())
    videos = []
    for role in checker.cameras:
        video = output / (role + '.mp4')
        encoded = encode_video_pyav(checker, role, video)
        if not video.is_file() or video.stat().st_size == 0:
            raise RuntimeError('MP4 encoder did not create a real video: ' + role)
        decoded = full_decode_video(executable, video, output_fd)
        log = output / (role + '-ffmpeg-decode.log')
        with log.open('x', encoding='utf-8') as handle:
            handle.write(decoded.stdout + decoded.stderr)
            handle.flush()
            os.fsync(handle.fileno())
        if decoded.returncode or decoded.stderr.strip():
            raise RuntimeError('MP4 complete ffmpeg decode failed: ' + role + ': ' + decoded.stderr)
        videos.append({'role': role, 'path': str(video), 'bytes': video.stat().st_size, 'sha256': digest(video),
                       'full_decode_returncode': decoded.returncode, 'timing_and_encoder': encoded})
    return {'joint_csv': {'path': str(csv_path), 'rows': len(checker.states), 'sha256': digest(csv_path)},
            'videos': videos, 'timing': 'VFR: one frame per actual state, PTS from original nanosecond state deltas rounded to microseconds and verified after mux; final frame nominal duration 1/30s',
            'pixels': 'H.264 review derivative only; original native JPEGs remain the source for model/data evaluation'}


def check_recording(root, *, cameras=('front', 'right'), arms=('right',), feedback_age=.2,
                    camera_age=.5, image_size=(640, 480), export_dir=None, ffmpeg=None,
                    root_fd=None, output_fd=None, return_checker=False):
    checker = Checker(root, cameras, arms, feedback_age, camera_age, image_size, root_fd=root_fd)
    result = checker.check()
    if export_dir is not None and not checker.errors:
        try:
            result['review_exports'] = export_review(checker, export_dir, ffmpeg, output_fd=output_fd)
        except Exception as exc:
            checker.error('review_export_failed', repr(exc))
    checker.unchanged()
    result.update(status='PASS' if not checker.errors else 'FAIL', errors=checker.errors,
                  error_counts=dict(checker.error_counts), warnings=checker.warnings,
                  source_stat_unchanged=not any(k.startswith('input_') for k in checker.error_counts),
                  jpeg_decode='Pillow full decode of every stored JPEG',
                  export_requested=export_dir is not None)
    return (result, checker) if return_checker else result

