# 完整单行命令：Windows PC PowerShell；无需预定义变量，复制时去掉行首 #。
# 按需选择一条命令，不要把所有示例一起执行。
# # 只读模块；通过统一入口接收真实反馈并保存诊断，不直接运行本文件。
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' status
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' diagnose --seconds 3
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' teleop-status
# 本轮仅补充命令注释；未运行测试、SSH、模型、相机或机械臂。
"""Receive-only Piper V2 feedback and honest, bounded link diagnostics.

No SDK instance, CAN send, camera open, ROS node or disk write exists here.
Wire layout was checked against installed piper-sdk 0.6.2 on 2026-10-06.
Kernel receive timestamps, not callback time, determine CAN freshness.
"""
from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from .time_contract import capture_clock_domain, capture_host_clock_anchor
from .gripper_state import decode_gripper_feedback

CAN_SERIALS = {"can_right": "001D00235631511820313857", "can_left": "002100425443570A20393433"}
CAMERA_SERIALS = {"front": "CC1T35300NZ", "right": "CC1T353005P", "left": "CC1T35300R3"}
JOINT_IDS = (0x2A5, 0x2A6, 0x2A7)
SOURCE_STREAM_IDS = {'q12': 0x2A5, 'q34': 0x2A6, 'q56': 0x2A7, 'gripper': 0x2A8}
MOTOR_IDS = tuple(range(0x261, 0x267))
# Firmware supports +0x10/+0x20 control offsets through0x470. Observe
# Cartesian/joint/gripper IDs in all three ranges, without inferring source.
COMMAND_IDS = tuple(base + index for base in (0x150, 0x160, 0x170) for index in range(10)) + (0x470,)
OBSERVED_IDS = JOINT_IDS + MOTOR_IDS + COMMAND_IDS + (0x251, 0x2A1, 0x2A8, 0x2A9, 0x473)
CAN_EFF, CAN_RTR, CAN_ERR = 0x80000000, 0x40000000, 0x20000000
COUNTERS = ("rx_packets", "rx_bytes", "rx_errors", "rx_dropped", "tx_packets", "tx_bytes", "tx_errors", "tx_dropped")
CTRL_NAMES = {0: "STANDBY", 1: "CAN_CTRL", 2: "TEACHING_MODE", 3: "ETHERNET_CONTROL_MODE", 4: "WIFI_CONTROL_MODE", 5: "REMOTE_CONTROL_MODE", 6: "LINKAGE_TEACHING_INPUT_MODE", 7: "OFFLINE_TRAJECTORY_MODE"}
ARM_NAMES = {0: "NORMAL", 1: "EMERGENCY_STOP", 2: "NO_SOLUTION", 3: "SINGULARITY_POINT", 4: "TARGET_POS_EXCEEDS_LIMIT", 5: "JOINT_COMMUNICATION_ERR", 6: "JOINT_BRAKE_NOT_RELEASED", 7: "COLLISION_OCCURRED", 8: "OVERSPEED_DURING_TEACHING_DRAG", 9: "JOINT_STATUS_ERR", 10: "OTHER_ERR", 11: "TEACHING_RECORD", 12: "TEACHING_EXECUTION", 13: "TEACHING_PAUSE", 14: "MAIN_CONTROLLER_NTC_OVER_TEMPERATURE", 15: "RELEASE_RESISTOR_NTC_OVER_TEMPERATURE"}


def _read(path):
    return Path(path).read_text(encoding="utf-8").strip()


def _gripper_width(latest, can_id=0x2A8):
    """Keep unsupported V3 units unknown rather than labelling degrees as metres."""
    if can_id not in latest:
        return None
    raw = latest[can_id][1]
    if can_id == 0x2A8:
        return decode_gripper_feedback(raw)['aperture_m']
    # V3 command Byte6 encodes width/angle; Byte7 is the zero flag.
    return struct.unpack('>i',raw[:4])[0]/1e6 if len(raw)==8 and raw[6] in (0,1,2,3) else None


def adapter_identity(interface, expected_serial=None):
    """Verify real USB adapter identity before opening its receive socket."""
    if interface not in CAN_SERIALS:
        raise ValueError("Only registered can_right/can_left interfaces are supported")
    device = Path("/sys/class/net") / interface / "device"
    resolved = device.resolve(strict=True)
    serial = None
    for parent in (resolved, *resolved.parents):
        source = parent / "serial"
        if source.is_file():
            serial = _read(source)
            break
    expected = expected_serial or CAN_SERIALS[interface]
    if serial != expected:
        raise RuntimeError("CAN adapter identity mismatch: %s actual=%r expected=%r" % (interface, serial, expected))
    return {"interface": interface, "serial": serial, "sysfs_device": str(resolved), "identity_verified": True}


def interface_counters(interface):
    base = Path("/sys/class/net") / interface / "statistics"
    return {key: int(_read(base / key)) for key in COUNTERS}


def decode_status(payload):
    if len(payload) != 8:
        raise ValueError("Piper status payload must contain 8 bytes")
    return {"ctrl_mode": payload[0], "ctrl_mode_name": CTRL_NAMES.get(payload[0], "UNKNOWN"),
            "arm_status": payload[1], "arm_status_name": ARM_NAMES.get(payload[1], "UNKNOWN"),
            "mode_feed": payload[2], "teach_status": payload[3], "motion_status": payload[4],
            "trajectory_num": payload[5], "err_code": int.from_bytes(payload[6:8], "big"),
            "angle_limit_joints": [j + 1 for j in range(6) if payload[6] & (1 << j)],
            "communication_error_joints": [j + 1 for j in range(6) if payload[7] & (1 << j)]}


def _stats(samples):
    ordered = sorted(samples)
    if not ordered:
        return {"samples": 0, "minimum_s": None, "p50_s": None, "p95_s": None, "maximum_s": None}
    return {"samples": len(ordered), "minimum_s": ordered[0], "p50_s": ordered[len(ordered) // 2],
            "p95_s": ordered[min(len(ordered) - 1, math.ceil(.95 * len(ordered)) - 1)], "maximum_s": ordered[-1]}


class CanObserver:
    """One independent receive-only SocketCAN subscriber; no hardware writer.

    Snapshots retain last observed values alongside their age, never extrapolate
    missing packets and never treat a stale last value as current feedback.
    Interface-wide TX counters cannot attribute other writers; observed control
    IDs indicate bus traffic, not the originating leader/process identity.
    """
    def __init__(self, interface="can_right", expected_serial=None, stale_after=.2, clock_domain=None):
        if interface not in CAN_SERIALS:
            raise ValueError("Unknown CAN interface")
        if not math.isfinite(stale_after) or not 0 < stale_after <= 2:
            raise ValueError("stale_after must be finite, >0 and <=2 seconds")
        self.interface = interface
        self.expected_serial = expected_serial or CAN_SERIALS[interface]
        self.stale_after = float(stale_after)
        self.clock_domain = deepcopy(clock_domain)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._socket = None
        self._started = None
        self._identity = None
        self._counter_before = None
        self._error = None
        self._latest = {}
        self._latest_timing = {}
        self._counts = Counter()
        self._gaps = {cid: deque(maxlen=256) for cid in OBSERVED_IDS}
        self._max_gaps = Counter()
        self._gap_exceed = Counter()
        self._groups = {"joints": JOINT_IDS, "actions": (0x155, 0x156, 0x157)}
        self._group_seen = {key: {} for key in self._groups}
        self._group_last = {}
        self._group_gaps = {key: deque(maxlen=256) for key in self._groups}
        self._group_count = Counter()
        self._group_skew = {}
        self._malformed = 0
        self._error_frames = 0
        self._timestamp_failures = 0
        self._socket_drops = 0
        self._recent_errors = deque(maxlen=16)
        self._callback = None
        self._source_callback = None
        self._source_callback_error = None

    def start(self, callback=None, *, source_callback=None):
        """Start receiver; optional nonblocking callback(cid, bytes, monotonic_s).

        A recording callback must enqueue only: a slow callback can lose frames.
        None is the default; no per-frame writes are performed by this module.
        """
        if self._thread is not None:
            raise RuntimeError("Observer cannot be started twice")
        if self.clock_domain is None:
            self.clock_domain = capture_clock_domain()
        self._identity = adapter_identity(self.interface, self.expected_serial)
        self._counter_before = interface_counters(self.interface)
        receiver = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            receiver.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_TIMESTAMPNS", 35), 1)
            receiver.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_RXQ_OVFL", 40), 1)
            receiver.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, 0)
            receiver.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                                b"".join(struct.pack("=II", cid, CAN_EFF | CAN_RTR | 0x7FF) for cid in OBSERVED_IDS))
            receiver.setsockopt(socket.SOL_CAN_RAW, 2, 0x1FFFFFFF)  # CAN_RAW_ERR_FILTER
            receiver.bind((self.interface,))
            receiver.settimeout(.05)
        except Exception:
            receiver.close()
            raise
        self._socket = receiver
        self._callback = callback
        self._source_callback = source_callback
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="can_observer_" + self.interface, daemon=True)
        self._thread.start()
        return self

    def feed_frame(self, cid, payload, received_at, *, source_timing=None):
        """Decode a payload; three-argument legacy fixtures have unknown timing.

        Optional source evidence is committed atomically with the payload.
        Evidence failures are reported separately from existing control guards;
        they never manufacture a kernel receipt from a callback timestamp.
        """
        if len(payload) != 8 or not math.isfinite(received_at):
            with self._lock:
                self._malformed += 1
            return
        if cid & CAN_ERR:
            with self._lock:
                self._error_frames += 1
                self._recent_errors.append({"id": hex(cid), "payload_hex": payload.hex(), "monotonic_s": received_at})
            return
        if cid & (CAN_EFF | CAN_RTR) or cid not in OBSERVED_IDS:
            return
        with self._lock:
            previous = self._latest.get(cid)
            if previous is not None:
                gap = received_at - previous[0]
                if gap < 0:
                    self._timestamp_failures += 1
                    return
                self._gaps[cid].append(gap)
                self._max_gaps[cid] = max(self._max_gaps[cid], gap)
                if gap > self.stale_after:
                    self._gap_exceed[cid] += 1
            evidence = self._bind_source_timing(cid, self._counts[cid] + 1, source_timing)
            self._latest[cid] = (received_at, bytes(payload))
            self._latest_timing[cid] = evidence
            self._counts[cid] += 1
            for key, ids in self._groups.items():
                if cid not in ids or not all(item in self._latest for item in ids):
                    continue
                if not all(self._counts[item] > self._group_seen[key].get(item, 0) for item in ids):
                    continue
                stamps = [self._latest[item][0] for item in ids]
                stamp = max(stamps)
                if key in self._group_last:
                    self._group_gaps[key].append(stamp - self._group_last[key])
                self._group_last[key] = stamp
                self._group_skew[key] = max(stamps) - min(stamps)
                self._group_seen[key] = {item: self._counts[item] for item in ids}
                self._group_count[key] += 1
        if self._callback is not None:
            self._callback(cid, bytes(payload), received_at)
        if self._source_callback is not None:
            try:
                self._source_callback(cid, bytes(payload), deepcopy(evidence))
            except Exception as exc:
                # Alignment is a consumer of this observer. A publication
                # failure must be visible without rewriting old freshness.
                with self._lock:
                    self._source_callback_error = repr(exc)

    def _bind_source_timing(self, cid, sequence, source_timing):
        """Called under _lock, once per accepted payload, never by a reader."""
        domain = deepcopy(self.clock_domain)
        verified = (isinstance(domain, dict) and domain.get('identity_verified') is True
                    and domain.get('clock_name') == 'monotonic_ns'
                    and all(isinstance(domain.get(key), str) and domain[key].strip()
                            for key in ('host_id', 'boot_id', 'session_id')))
        session_id = domain.get('session_id') if isinstance(domain, dict) else None
        evidence = {'schema': 'tiger-can-source-time-v1', 'kernel_wall_ns': None,
                    'source_monotonic_ns_estimate': None, 'available_monotonic_ns': None,
                    'source_timestamp_kind': 'unavailable', 'sample_id': None,
                    'local_receive_sequence': sequence, 'clock_domain': domain,
                    'clock_domain_identity_verified': bool(verified),
                    'clock_read_bracket_ns': None, 'mapping_error_ns': None,
                    'mapping_valid': False, 'provenance_complete': False,
                    'hardware_generation_timestamp_available': False,
                    'wall_clock_steps_verified': False, 'unavailable_reason': None}
        if isinstance(session_id, str) and session_id:
            evidence['sample_id'] = '%s:%s:%03x:%d' % (session_id, self.interface, cid, sequence)
        if not isinstance(source_timing, dict):
            evidence['unavailable_reason'] = 'No kernel source metadata supplied; legacy decoded payload only'
            return evidence
        # This is availability at the shared cache, distinct from kernel receipt.
        evidence['available_monotonic_ns'] = time.monotonic_ns()
        reasons = []
        for key in ('kernel_wall_ns', 'source_monotonic_ns_estimate', 'clock_read_bracket_ns',
                    'source_age_at_anchor_ns', 'mapping_anchor_wall_ns', 'mapping_anchor_monotonic_ns'):
            value = source_timing.get(key)
            evidence[key] = value if type(value) is int and value >= 0 else None
        kind = source_timing.get('source_timestamp_kind')
        if isinstance(kind, str) and kind:
            evidence['source_timestamp_kind'] = kind
        required = ('kernel_wall_ns', 'source_monotonic_ns_estimate', 'clock_read_bracket_ns')
        mapped_valid = (source_timing.get('mapping_valid') is True and
                        all(evidence.get(key) is not None for key in required) and
                        kind == 'linux_so_timestampns_kernel_receipt')
        if not mapped_valid:
            reason = source_timing.get('unavailable_reason')
            reasons.append(reason if isinstance(reason, str) and reason else 'Kernel source mapping missing or invalid')
        if (evidence['source_monotonic_ns_estimate'] is not None and
                evidence['source_monotonic_ns_estimate'] > evidence['available_monotonic_ns']):
            mapped_valid = False
            reasons.append('Mapped source occurs after first availability')
        if not verified:
            reasons.append('Host/boot/capture clock domain unverified')
        supplied_domain = source_timing.get('clock_domain')
        if supplied_domain is not None and supplied_domain != domain:
            mapped_valid = False
            reasons.append('Supplied source clock domain differs from this observer')
        evidence['mapping_valid'] = mapped_valid
        evidence['provenance_complete'] = mapped_valid and bool(verified)
        evidence['unavailable_reason'] = '; '.join(reasons) or None
        evidence['limitations'] = 'Host kernel receipt, not device generation time; mapping error and intervening wall-clock steps unverified.'
        return evidence

    @staticmethod
    def _source_timing_snapshot(timing):
        # A never-observed stream stays explicitly unavailable, with no new time.
        return {stream: deepcopy(timing.get(cid, {'schema': 'tiger-can-source-time-v1',
                    'kernel_wall_ns': None, 'source_monotonic_ns_estimate': None,
                    'available_monotonic_ns': None, 'source_timestamp_kind': 'unavailable',
                    'sample_id': None, 'local_receive_sequence': None, 'clock_domain': None,
                    'clock_domain_identity_verified': False, 'clock_read_bracket_ns': None,
                    'mapping_error_ns': None, 'mapping_valid': False, 'provenance_complete': False,
                    'hardware_generation_timestamp_available': False, 'wall_clock_steps_verified': False,
                    'unavailable_reason': 'No payload observed for this stream'}))
                for stream, cid in SOURCE_STREAM_IDS.items()}

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    frame, ancillary, flags, _address = self._socket.recvmsg(16, socket.CMSG_SPACE(16) + socket.CMSG_SPACE(4))
                except socket.timeout:
                    continue
                receive_stamp = None
                kernel_wall_ns = None
                for level, kind, data in ancillary:
                    if level == socket.SOL_SOCKET and kind == getattr(socket, "SO_TIMESTAMPNS", 35) and len(data) >= 16:
                        seconds, nanos = struct.unpack("=qq", data[:16])
                        receive_stamp = seconds + nanos / 1e9
                        if seconds >= 0 and 0 <= nanos < 1_000_000_000:
                            kernel_wall_ns = seconds * 1_000_000_000 + nanos
                    elif level == socket.SOL_SOCKET and kind == getattr(socket, "SO_RXQ_OVFL", 40) and len(data) >= 4:
                        with self._lock:
                            self._socket_drops = struct.unpack("=I", data[:4])[0]
                if receive_stamp is None or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                    raise RuntimeError("Kernel timestamp missing or CAN frame/control data truncated")
                wall_age = time.time() - receive_stamp
                if wall_age < -.05:
                    raise RuntimeError("Kernel timestamp is in the future; freshness cannot be established")
                received_at = time.monotonic() - max(0, wall_age)
                try:
                    source_timing = capture_host_clock_anchor().map_kernel_wall_ns(kernel_wall_ns)
                except (TypeError, ValueError) as exc:
                    # Keep the old control freshness contract. New source
                    # evidence remains invalid; it cannot enter alignment.
                    source_timing = {'kernel_wall_ns': kernel_wall_ns,
                                     'source_timestamp_kind': 'linux_so_timestampns_kernel_receipt',
                                     'mapping_valid': False, 'unavailable_reason': str(exc)}
                if len(frame) != 16:
                    with self._lock:
                        self._malformed += 1
                    continue
                cid, dlc, payload = struct.unpack("=IB3x8s", frame)
                if dlc != 8:
                    with self._lock:
                        self._malformed += 1
                    continue
                self.feed_frame(cid, payload, received_at, source_timing=source_timing)
        except Exception as exc:
            if not self._stop.is_set():
                with self._lock:
                    self._error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            self._stop.set()

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            latest = dict(self._latest)
            source_timing = self._source_timing_snapshot(self._latest_timing)
            ages = {cid: max(0, now - stamp) for cid, (stamp, _payload) in latest.items()}
            counts = dict(self._counts)
            gaps = {hex(cid): {**_stats(self._gaps[cid]), "maximum_since_open_s": self._max_gaps[cid] or None,
                              "gaps_over_stale_threshold": self._gap_exceed[cid]} for cid in OBSERVED_IDS}
            group = {key: {**_stats(self._group_gaps[key]), "completed_groups": self._group_count[key],
                           "latest_component_skew_s": self._group_skew.get(key)} for key in self._groups}
            error = self._error
            malformed, error_frames, socket_drops, timestamp_failures = self._malformed, self._error_frames, self._socket_drops, self._timestamp_failures
            recent_errors = list(self._recent_errors)
            source_callback_error = self._source_callback_error
        def age(ids):
            return max(ages[cid] for cid in ids) if all(cid in ages for cid in ids) else None
        def joints(ids):
            return [value / 1000 for cid in ids for value in struct.unpack(">ii", latest[cid][1])] if all(cid in latest for cid in ids) else None
        status_payload = latest.get(0x2A1, (None, None))[1]
        motor_flags = [latest[cid][1][5] for cid in MOTOR_IDS] if all(cid in latest for cid in MOTOR_IDS) else None
        joint_age = age(JOINT_IDS)
        status_age = age((0x2A1,))
        required_age = age(JOINT_IDS + (0x2A1, 0x2A8))
        feedback_fresh = required_age is not None and required_age <= self.stale_after and not error and not timestamp_failures
        result = {"interface": self.interface, "identity": self._identity, "source_timing": source_timing,
                  "source_callback_error": source_callback_error,
                  "observer_running": self._thread is not None and self._thread.is_alive(),
                  "observer_error": error, "duration_s": None if self._started is None else max(0, now - self._started),
                  "q_deg": joints(JOINT_IDS), "grip_m": _gripper_width(latest),
                  "age_joint_s": joint_age, "age_status_s": status_age, "age_gripper_s": age((0x2A8,)), "age_motor_s": age(MOTOR_IDS),
                  "status_hex": status_payload.hex() if status_payload is not None else None,
                  "status": decode_status(status_payload) if status_payload is not None else None,
                  "motor_flags": motor_flags, "enabled": [bool(flag & 64) for flag in motor_flags] if motor_flags is not None else None,
                  "motor_fault_bits": [flag & 0xBF for flag in motor_flags] if motor_flags is not None else None,
                  "feedback_fresh": feedback_fresh, "fresh": feedback_fresh, "stale_after_s": self.stale_after,
                  "freshness_basis": "Linux SO_TIMESTAMPNS receipt adjusted to monotonic; hardware generation timestamp unavailable",
                  "required_missing_ids": [hex(cid) for cid in JOINT_IDS + (0x2A1, 0x2A8) if cid not in latest],
                  "per_id_counts": {hex(cid): counts.get(cid, 0) for cid in OBSERVED_IDS},
                  "per_id_observed_hz": {hex(cid): counts.get(cid, 0) / (now - self._started)
                                         if self._started is not None and now > self._started else None for cid in OBSERVED_IDS},
                  "per_id_age_s": {hex(cid): ages.get(cid) for cid in OBSERVED_IDS},
                  "per_id_gap_stats": gaps, "group_gap_stats": group, "malformed_frames": malformed,
                  "error_frames": error_frames, "recent_error_frames": recent_errors, "timestamp_failures": timestamp_failures,
                  "socket_rx_overflow_packets": socket_drops, "CAN_commands_sent_by_this_observer": 0,
                  "external_commands": {hex(cid): {"count": counts.get(cid, 0), "last_age_s": ages.get(cid),
                                                "payload_hex": latest[cid][1].hex() if cid in latest else None} for cid in COMMAND_IDS},
                  "command_origin_note": "Control IDs are bus-observed. They cannot identify the sender or prove physical leader isolation.",
                  "action_targets": {"q_deg": joints((0x155, 0x156, 0x157)),
                                     "grip_m": _gripper_width(latest,0x159),
                                     "pair_ages_s": [ages.get(cid) for cid in (0x155, 0x156, 0x157)], "grip_age_s": ages.get(0x159)},
                  "raw_0x2a9": latest[0x2A9][1].hex() if 0x2A9 in latest else None,
                  "raw": {hex(cid): {"monotonic_s": stamp, "payload_hex": payload.hex(), "count": counts.get(cid, 0)}
                          for cid, (stamp, payload) in latest.items()},
                  "raw_0x2a9_note": "Observed without interpreting unverified semantics", "history_scope": "Since this observer opened; gap statistics retain last256 intervals and lifetime maxima. No sequence numbers; missing frame count cannot be proven."}
        try:
            counters = interface_counters(self.interface)
            result["interface_counters"] = counters
            if self._counter_before is not None:
                delta = {key: counters[key] - self._counter_before[key] for key in COUNTERS}
                result["interface_counter_delta"] = delta
                result["interface_counters_reset"] = any(value < 0 for value in delta.values())
        except Exception as exc:
            result["interface_counters_error"] = str(exc)
        return result

    def control_snapshot(self, now=None):
        """Copy current feedback for control without diagnostics or filesystem I/O.

        Field semantics match ``snapshot``: missing values stay None, stale
        values retain their measured age, and raw receipts retain timestamps.
        In particular, a mode handoff can require a newly received 0x2A1 frame
        instead of accepting the status cached before its command was sent.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            latest = dict(self._latest)
            source_timing = self._source_timing_snapshot(self._latest_timing)
            counts = dict(self._counts)
            error = self._error
            timestamp_failures = self._timestamp_failures
            error_frames = self._error_frames
            socket_drops = self._socket_drops
            identity = None if self._identity is None else dict(self._identity)
            source_callback_error = self._source_callback_error
        ages = {cid: max(0, now - stamp) for cid, (stamp, _payload) in latest.items()}

        def age(ids):
            return max(ages[cid] for cid in ids) if all(cid in ages for cid in ids) else None

        def joints(ids):
            return [value / 1000 for cid in ids for value in struct.unpack(">ii", latest[cid][1])] if all(cid in latest for cid in ids) else None

        status_payload = latest.get(0x2A1, (None, None))[1]
        motor_flags = [latest[cid][1][5] for cid in MOTOR_IDS] if all(cid in latest for cid in MOTOR_IDS) else None
        required_age = age(JOINT_IDS + (0x2A1, 0x2A8))
        feedback_fresh = required_age is not None and required_age <= self.stale_after and not error and not timestamp_failures
        return {"interface": self.interface, "identity": identity, "source_timing": source_timing,
                "source_callback_error": source_callback_error,
                "observer_running": self._thread is not None and self._thread.is_alive(),
                "observer_error": error, "duration_s": None if self._started is None else max(0, now - self._started),
                "q_deg": joints(JOINT_IDS),
                "grip_m": _gripper_width(latest),
                "age_joint_s": age(JOINT_IDS), "age_status_s": age((0x2A1,)),
                "age_gripper_s": age((0x2A8,)), "age_motor_s": age(MOTOR_IDS),
                "status_hex": status_payload.hex() if status_payload is not None else None,
                "status": decode_status(status_payload) if status_payload is not None else None,
                "motor_flags": motor_flags, "enabled": [bool(flag & 64) for flag in motor_flags] if motor_flags is not None else None,
                "motor_fault_bits": [flag & 0xBF for flag in motor_flags] if motor_flags is not None else None,
                "feedback_fresh": feedback_fresh, "fresh": feedback_fresh,
                "stale_after_s": self.stale_after,
                "freshness_basis": "Linux SO_TIMESTAMPNS receipt adjusted to monotonic; hardware generation timestamp unavailable",
                "required_missing_ids": [hex(cid) for cid in JOINT_IDS + (0x2A1, 0x2A8) if cid not in latest],
                "timestamp_failures": timestamp_failures, "CAN_commands_sent_by_this_observer": 0,
                "error_frames": error_frames, "socket_rx_overflow_packets": socket_drops,
                "external_commands": {hex(cid): {"count": counts.get(cid, 0), "last_age_s": ages.get(cid),
                                                "payload_hex": latest[cid][1].hex() if cid in latest else None} for cid in COMMAND_IDS},
                "command_origin_note": "Control IDs are bus-observed. They cannot identify the sender or prove physical leader isolation.",
                "action_targets": {"q_deg": joints((0x155, 0x156, 0x157)),
                                   "grip_m": _gripper_width(latest,0x159),
                                   "pair_ages_s": [ages.get(cid) for cid in (0x155, 0x156, 0x157)], "grip_age_s": ages.get(0x159)},
                "raw": {hex(cid): {"monotonic_s": stamp, "payload_hex": payload.hex(), "count": counts.get(cid, 0)}
                        for cid, (stamp, payload) in latest.items()}}

    def latest_frame(self, can_id):
        """Return receipt timestamp and copied bytes; None means never observed."""
        with self._lock:
            item = self._latest.get(can_id)
            return None if item is None else (item[0], bytes(item[1]))

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=.5)
        if self._socket is not None:
            self._socket.close()
        return self.snapshot()


def observe_dual_can(duration=3, stale_after=.2):
    if not math.isfinite(duration) or not 0 < duration <= 60:
        raise ValueError("diagnostic duration must be finite and between0 and60 seconds")
    observers, result = {}, {}
    try:
        for role, interface in (("right", "can_right"), ("left", "can_left")):
            try:
                observers[role] = CanObserver(interface, stale_after=stale_after).start()
            except Exception as exc:
                result[role] = {"interface": interface, "feedback_fresh": False, "observer_error": "%s: %s" % (type(exc).__name__, exc), "CAN_commands_sent_by_this_observer": 0}
        time.sleep(duration)
        result.update({role: observer.snapshot() for role, observer in observers.items()})
    finally:
        for observer in observers.values():
            observer.stop()
    return result


def camera_inventory():
    """Enumerate identity and /proc owners without opening any video device."""
    result = {}
    for role, serial in CAMERA_SERIALS.items():
        entries = sorted(Path("/dev/v4l/by-id").glob("*" + serial + "-video-index0"))
        nodes = []
        for link in entries:
            try:
                node = link.resolve(strict=True)
                device = (Path("/sys/class/video4linux") / node.name / "device").resolve(strict=True)
                actual = None
                for parent in (device, *device.parents):
                    if (parent / "serial").is_file():
                        actual = _read(parent / "serial")
                        break
                nodes.append({"by_id": str(link), "device": str(node), "serial": actual, "identity_verified": actual == serial})
            except Exception as exc:
                nodes.append({"by_id": str(link), "identity_verified": False, "error": str(exc)})
        result[role] = {"expected_serial": serial, "capture_nodes": nodes, "enumerated": bool(nodes), "opened_by_this_diagnostic": False,
                        "live_frames_verified": False, "live_status": "UNKNOWN_REQUIRES_SHARED_PRODUCER_FRAME_TIMESTAMP", "open_owners": []}
    denied = 0
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            descriptors = list((proc / "fd").iterdir())
        except (PermissionError, OSError):
            denied += 1
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if not target.startswith("/dev/video"):
                continue
            for data in result.values():
                if any(node.get("device") == target for node in data["capture_nodes"]):
                    try:
                        executable = os.readlink(proc / "exe")
                    except OSError:
                        executable = None
                    owner = {"pid": int(proc.name), "fd": descriptor.name, "device": target, "executable": executable}
                    if owner not in data["open_owners"]:
                        data["open_owners"].append(owner)
    return {"cameras": result, "inaccessible_process_fd_directories": denied,
            "owner_coverage": "Partial when any process FD directory is inaccessible; an open FD does not prove fresh frames"}


def shared_sensor_status(url="http://127.0.0.1:8781/status", timeout=2):
    """Read the shared producer's API; never create a second camera reader."""
    if url != "http://127.0.0.1:8781/status":
        raise ValueError("Shared sensor status must use registered loopback port8781")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise RuntimeError("Shared sensor status exceeds bounded2MB response")
        data = json.loads(payload)
        return {"api_reachable": True, "url": url, "reported": data, "browser_display_verified": False}
    except Exception as exc:
        return {"api_reachable": False, "url": url, "error": "%s: %s" % (type(exc).__name__, exc), "browser_display_verified": False}


def _run_bounded(argv, timeout=3):
    clean_env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "LANG": "C.UTF-8"}
    try:
        run = subprocess.run(argv, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout, env=clean_env)
        return {"exit_code": run.returncode, "stdout": run.stdout[:48000], "stderr": run.stderr[:8000], "truncated": len(run.stdout) > 48000 or len(run.stderr) > 8000}
    except Exception as exc:
        return {"exit_code": None, "error": "%s: %s" % (type(exc).__name__, exc)}


def ros_diagnostics():
    """Bounded ROS master introspection in isolated system Python, no ROS node.

    Master registrations are not proof of topic freshness/latency or of a clean
    CAN ownership graph. An absent master is reported as unavailable, not PASS.
    """
    script = r'''
import json, socket, sys, xmlrpc.client
socket.setdefaulttimeout(1.0)
r={"python":sys.executable,"node_created":False,"master_uri":"http://127.0.0.1:11311","topic_samples_received":0}
try:
 p=xmlrpc.client.ServerProxy(r["master_uri"])
 c,m,state=p.getSystemState("/tiger_unified_readonly_diagnostic")
 r.update(master_reachable=True,return_code=c,message=m,system_state=state)
 if c==1:
  c2,m2,topics=p.getPublishedTopics("/tiger_unified_readonly_diagnostic","")
  r.update(published_topics=topics if c2==1 else None,published_topics_return_code=c2)
  r["registered_nodes"]=sorted(set(n for group in state for topic,names in group for n in names))
except Exception as e:r.update(master_reachable=False,error=type(e).__name__+": "+str(e))
print(json.dumps(r))
'''
    receipt = _run_bounded(["/usr/bin/python3", "-I", "-B", "-c", script], timeout=4)
    try:
        observed = json.loads(receipt.get("stdout", ""))
    except Exception:
        observed = {"master_reachable": False, "probe_output_parse_failed": True}
    return {**observed, "probe": receipt, "ros_installation_present": Path("/opt/ros/noetic/setup.bash").is_file(),
            "ROS_channel_clean_proven": False, "assessment": "MASTER_REGISTRATIONS_ONLY_TOPIC_FRESHNESS_UNVERIFIED" if observed.get("master_reachable") else "ROS_MASTER_UNAVAILABLE",
            "note": "Native SocketCAN control can work without ROS. No master is not a proof of a broken native link or zero external CAN writers."}


def diagnostics(duration=3, stale_after=.2, shared_url="http://127.0.0.1:8781/status"):
    """Return current link receipt; caller owns validated USB persistence."""
    arms = observe_dual_can(duration=duration, stale_after=stale_after)
    network = {interface: _run_bounded(["/usr/sbin/ip" if Path("/usr/sbin/ip").is_file() else "/bin/ip", "-s", "-d", "link", "show", interface]) for interface in CAN_SERIALS}
    return {"schema": "tiger.unified.sensors.v1", "sampled_wall_time_s": time.time(), "hostname": socket.gethostname(),
            "arms": arms, "interfaces": network, "camera_inventory": camera_inventory(), "shared_producer": shared_sensor_status(shared_url),
            "ros": ros_diagnostics(), "rerun_ports": _run_bounded(["/bin/ss", "-ltn"]),
            "CAN_commands_sent_by_this_diagnostic": 0, "cameras_opened_by_this_diagnostic": 0,
            "assessment": "LINK_OBSERVATION_ONLY_NOT_MOTION_CLEARANCE", "duration_requested_s": duration,
            "notes": ["No bus sequence number: time gaps and observed drops do not prove an exact missing-frame count.",
                      "Interface counters include all sockets/processes, not just this observer.",
                      "A camera serial/device/open FD is not a live frame check; use shared producer timestamp/frames."]}
