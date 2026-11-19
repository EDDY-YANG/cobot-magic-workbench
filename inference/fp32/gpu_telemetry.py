"""Read-only whole-device NVML memory sampler;100ms RAM, one close-time save."""
import csv
import ctypes
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid


class Memory(ctypes.Structure):
    _fields_ = [('total', ctypes.c_ulonglong), ('free', ctypes.c_ulonglong), ('used', ctypes.c_ulonglong)]


class NVMLMemory:
    def __init__(self):
        self.lib = ctypes.CDLL('libnvidia-ml.so.1')
        self.lib.nvmlInit_v2.restype = ctypes.c_int
        self.lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
        self.lib.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
        self.lib.nvmlDeviceGetMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Memory)]
        self.lib.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
        if self.lib.nvmlInit_v2() != 0:
            raise RuntimeError('NVML initialization failed')
        self.handle = ctypes.c_void_p()
        if self.lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self.handle)) != 0:
            self.lib.nvmlShutdown()
            raise RuntimeError('NVML device0 handle unavailable')
    def __call__(self):
        memory = Memory()
        code = self.lib.nvmlDeviceGetMemoryInfo(self.handle, ctypes.byref(memory))
        if code != 0:
            raise RuntimeError('NVML memory query failed with code%d' % code)
        return {'total_bytes': memory.total, 'free_bytes': memory.free, 'used_bytes': memory.used}
    def close(self):
        self.lib.nvmlShutdown()


class Telemetry:
    def __init__(self, directory, *, reader=None, maximum_samples=30000):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.rows = deque(maxlen=maximum_samples)
        self.sequence = 0
        self.stop_event = threading.Event()
        self.reader, self.error = reader, None
        self.model_marker = None
        self.model_peak = None
        self.model_peak_sample = None
        self.model_errors = 0
        self.model_step = None
        if self.reader is None:
            try:
                self.reader = NVMLMemory()
            except Exception as exc:
                self.error = type(exc).__name__ + ': NVML whole-device telemetry unavailable'
        self.thread = threading.Thread(target=self._run, daemon=True, name='whole-GPU-memory-100ms')

    def start(self):
        if self.reader is not None:
            self.sample()
            self.thread.start()
        return self

    def sample(self):
        row = {'monotonic_ns': time.monotonic_ns()}
        try:
            value = self.reader()
            if not isinstance(value, dict) or any(type(value.get(k)) is not int or value[k] < 0
                    for k in ('total_bytes', 'free_bytes', 'used_bytes')) or value['used_bytes'] > value['total_bytes']:
                raise ValueError('Invalid whole-device NVML memory values')
            row.update(value, error=None)
        except Exception as exc:
            row.update(total_bytes=None, free_bytes=None, used_bytes=None,
                       error=type(exc).__name__ + ': NVML sample failed')
        with self.lock:
            self.sequence += 1
            row['sequence'] = self.sequence
            self.rows.append(row)
            if self.model_marker is not None:
                if row['error'] is not None:
                    self.model_errors += 1
                elif self.model_peak is None or row['used_bytes'] > self.model_peak:
                    self.model_peak = row['used_bytes']
                    self.model_peak_sample = dict(row)

    def _run(self):
        while not self.stop_event.wait(.1):
            self.sample()

    def mark(self):
        with self.lock:
            return {'sequence': self.sequence, 'monotonic_ns': time.monotonic_ns()}

    def begin_model(self, step):
        if self.model_marker is not None:
            self.finish(self.model_marker, 'model-load-step%d' % self.model_step)
        with self.lock:
            self.model_marker = self.mark()
            self.model_step, self.model_peak, self.model_errors, self.model_peak_sample = step, None, 0, None

    def status(self):
        with self.lock:
            latest = self.rows[-1] if self.rows else None
            age = (time.monotonic_ns() - latest['monotonic_ns']) / 1e9 if latest else None
            fresh = latest is not None and latest.get('error') is None and age is not None and 0 <= age <= .3
            return {'schema': 'tiger-whole-gpu-memory-v1', 'source': 'NVML device0 whole memory',
                    'status': 'AVAILABLE' if self.reader is not None and fresh else 'UNAVAILABLE',
                    'error': self.error or (latest.get('error') if latest else None), 'sample_fresh': fresh,
                    'last_sample_age_s': age, 'sample_freshness_limit_s': .3,
                    'sample_period_s': .1, 'samples_seen': self.sequence, 'model_step': self.model_step,
                    'model_peak_mib': None if self.model_peak is None else self.model_peak / 1048576,
                    'model_failed_samples': self.model_errors,
                    'limitation': '100ms sampled whole-device peak; sub-sample transients are not observed.'}

    def finish(self, marker, session_id):
        with self.lock:
            rows = [dict(row) for row in self.rows if row['sequence'] > marker['sequence']]
            end_sequence = self.sequence
            model_peak, model_errors = self.model_peak, self.model_errors
            model_peak_sample = None if self.model_peak_sample is None else dict(self.model_peak_sample)
        last_age = (time.monotonic_ns() - rows[-1]['monotonic_ns']) / 1e9 if rows else None
        max_gap = max(((b['monotonic_ns'] - a['monotonic_ns']) / 1e9 for a, b in zip(rows, rows[1:])), default=0.)
        # Instrument quality guard, not a robot command/latency parameter.
        fresh = last_age is not None and 0 <= last_age <= .3 and max_gap <= .3
        complete = bool(rows) and rows[0]['sequence'] == marker['sequence'] + 1 and rows[-1]['sequence'] == end_sequence
        failures = sum(row['error'] is not None for row in rows)
        used = [row['used_bytes'] for row in rows if row['used_bytes'] is not None]
        status = 'PASS' if self.reader is not None and complete and fresh and failures == 0 and model_errors == 0 and model_peak is not None else 'FAIL'
        key = uuid.uuid4().hex
        csv_path = self.directory / ('gpu-telemetry-%s.csv' % key)
        with csv_path.open('x', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['sequence', 'monotonic_ns', 'total_bytes', 'free_bytes', 'used_bytes', 'error'])
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        result = {'schema': 'tiger-whole-gpu-memory-v1', 'status': status, 'session_id': session_id,
                  'sample_period_s': .1, 'start_sequence': marker['sequence'], 'end_sequence': end_sequence,
                  'samples': len(rows), 'failed_samples': failures, 'complete_ring_coverage': complete,
                  'last_sample_age_s': last_age, 'sample_fresh': fresh, 'sample_freshness_limit_s': .3,
                  'lease_peak_mib': max(used) / 1048576 if used else None,
                  'model_peak_mib': model_peak / 1048576 if model_peak is not None else None,
                  'model_failed_samples': model_errors, 'source': 'NVML device0 whole memory',
                  'model_peak_sample': model_peak_sample,
                  'max_sample_gap_s': max_gap,
                  'csv_file': str(csv_path), 'csv_sha256': hashlib.sha256(csv_path.read_bytes()).hexdigest(),
                  'limitation': '100ms sampled whole-device peak; unseen sub-sample transients are not ruled out.'}
        json_path = self.directory / ('gpu-telemetry-%s.json' % key)
        with json_path.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        result.update(json_file=str(json_path), json_sha256=hashlib.sha256(json_path.read_bytes()).hexdigest())
        return result

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        if self.model_marker is not None:
            self.finish(self.model_marker, 'model-load-step%d-final' % self.model_step)
        if self.reader is not None and callable(getattr(self.reader, 'close', None)):
            self.reader.close()
