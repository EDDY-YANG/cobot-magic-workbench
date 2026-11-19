# PC project terminal; pure clock fixtures, no SSH or devices:
# python -X utf8 -B -m unittest discover -s tests -p 'test_2026_10_12_18_24_time_contract.py' -v
"""Bracketed host-clock estimates; never manufacture a device exposure time.

Capture an anchor BEFORE encoding. A source perf-counter stamp maps through the
same anchor to host monotonic and readable wall estimates, so codec duration
cannot change that source. Cross-host/boot/session identity must be supplied by
the owner before using these estimates for a common observation.
"""
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import socket
import sys
import time
import uuid


def capture_clock_domain(session_id=None):
    """Read this producer's host/boot identity once; never invent missing IDs.

    A random capture session identifies this producer lifetime, not a machine
    or boot. Linux machine/boot IDs are independent of a readable hostname.
    Other platforms remain explicitly unverified for cross-stream alignment.
    """
    if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
        raise ValueError('session_id must be a nonempty string')
    result = {'host_id': None, 'boot_id': None, 'session_id': session_id or str(uuid.uuid4()),
              'clock_name': 'monotonic_ns', 'identity_verified': False,
              'hostname': socket.gethostname(), 'unavailable_reason': None}
    if not sys.platform.startswith('linux'):
        result['unavailable_reason'] = 'Linux machine and boot identity unavailable on this platform'
        return result
    errors = []
    try:
        machine_id = Path('/etc/machine-id').read_text(encoding='ascii').strip()
        if re.fullmatch(r'[0-9a-fA-F]{32}', machine_id) is None or int(machine_id, 16) == 0:
            raise ValueError('Invalid Linux machine-id')
        result['host_id'] = 'linux-machine-id-sha256:' + hashlib.sha256(machine_id.lower().encode('ascii')).hexdigest()
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append('host identity: ' + str(exc))
    try:
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
        parsed = uuid.UUID(boot_id)
        if parsed.int == 0 or str(parsed) != boot_id.lower():
            raise ValueError('Invalid Linux boot_id')
        result['boot_id'] = str(parsed)
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append('boot identity: ' + str(exc))
    result['identity_verified'] = not errors
    result['unavailable_reason'] = '; '.join(errors) or None
    return result


def _ns(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(label + " must be nonnegative integer nanoseconds")
    return value


@dataclass(frozen=True)
class HostClockAnchor:
    perf_ns: int
    monotonic_ns: int
    wall_ns: int
    bracket_ns: int

    def __post_init__(self):
        for key in ('perf_ns', 'monotonic_ns', 'wall_ns', 'bracket_ns'):
            _ns(getattr(self, key), key)

    def map_perf_seconds(self, source_seconds):
        if (isinstance(source_seconds, bool) or not isinstance(source_seconds, (int, float))
                or not math.isfinite(source_seconds) or source_seconds < 0):
            raise ValueError('Real finite nonnegative perf-counter source required')
        source_ns = round(source_seconds * 1_000_000_000)
        age_ns = self.perf_ns - source_ns
        if age_ns < 0:
            raise ValueError('Future source stamp; no clipping or timestamp refresh')
        source_wall = self.wall_ns - age_ns
        source_monotonic = self.monotonic_ns - age_ns
        if min(source_wall, source_monotonic) < 0:
            raise ValueError('Source stamp incompatible with this host clock anchor')
        return {'schema': 'tiger-source-time-estimate-v1',
            'source_perf_ns': source_ns, 'source_monotonic_ns_estimate': source_monotonic,
            'source_wall_ns_estimate': source_wall,
            'source_age_at_anchor_ns': age_ns, 'clock_read_bracket_ns': self.bracket_ns,
            'source_timestamp_kind': 'lerobot_latest_timestamp_host_software',
            'hardware_exposure_timestamp_available': False,
            'clock_domain_identity_verified': False,
            'limitations': 'Clock reads are bracketed estimates; physical acquisition latency and wall-clock steps unverified.'}

    def map_kernel_wall_ns(self, kernel_wall_ns):
        """Map the actual integer SO_TIMESTAMPNS receipt without clipping.

        The bracket bounds local clock-read duration, not the total timestamp
        error. A wall-clock step between receipt and this anchor is unverified.
        """
        _ns(kernel_wall_ns, 'kernel_wall_ns')
        age_ns = self.wall_ns - kernel_wall_ns
        if age_ns < 0:
            raise ValueError('Future kernel wall timestamp; source cannot be clipped')
        mapped_ns = self.monotonic_ns - age_ns
        if mapped_ns < 0:
            raise ValueError('Kernel wall timestamp incompatible with this monotonic anchor')
        return {'schema': 'tiger-can-source-time-v1', 'kernel_wall_ns': kernel_wall_ns,
                'source_monotonic_ns_estimate': mapped_ns,
                'source_timestamp_kind': 'linux_so_timestampns_kernel_receipt',
                'source_age_at_anchor_ns': age_ns, 'clock_read_bracket_ns': self.bracket_ns,
                'mapping_anchor_wall_ns': self.wall_ns,
                'mapping_anchor_monotonic_ns': self.monotonic_ns,
                'mapping_valid': True, 'mapping_error_ns': None,
                'hardware_generation_timestamp_available': False,
                'wall_clock_steps_verified': False,
                'limitations': 'Host kernel receipt, not device generation time; total mapping error and intervening wall-clock steps are unverified.'}


def capture_host_clock_anchor():
    """Local reads only. No hardware, clock setting, network or disk access."""
    perf_before = time.perf_counter_ns()
    mono_before = time.monotonic_ns()
    wall = time.time_ns()
    mono_after = time.monotonic_ns()
    perf_after = time.perf_counter_ns()
    if mono_after < mono_before or perf_after < perf_before:
        raise ValueError('Monotonic/performance clock read reversed')
    return HostClockAnchor((perf_before+perf_after)//2, (mono_before+mono_after)//2,
                           wall, max(perf_after-perf_before, mono_after-mono_before))
