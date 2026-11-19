"""Bounded host-send evidence. No socket, SDK, disk or thread work on import.

The producer freezes small records and uses put_nowait. Only the writer thread
serializes JSON and writes the caller's verified USB destination. This proves
host API attempts/returns, not CAN arbitration, motor receipt or motion.
"""
from collections.abc import Mapping
from enum import Enum
import itertools
import json
import math
from pathlib import Path
import queue
import threading
import time
from types import MappingProxyType

from .storage import verify_usb


def freeze_evidence(value, *, _depth=0, _budget=None):
    """Immutable bounded metadata snapshot; deliberately no JSON serialization."""
    if _budget is None:
        _budget = [1024, 16384]
    _budget[0] -= 1
    _budget[1] -= 64  # Conservative accounting charge per metadata node.
    if _depth > 8 or _budget[0] < 0 or _budget[1] < 0:
        raise ValueError('TX evidence metadata exceeds bounded depth/item count')
    if isinstance(value, Enum):
        return freeze_evidence({'enum_type': type(value).__name__, 'name': value.name, 'value': value.value},
                               _depth=_depth + 1, _budget=_budget)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('Nonfinite TX evidence value')
        return value
    if isinstance(value, (str, bytes)):
        _budget[1] -= len(value) * (4 if isinstance(value, str) else 1)
        if len(value) > 2048 or _budget[1] < 0:
            raise ValueError('TX evidence string/bytes exceeds bounded length')
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) or len(key) > 128 for key in value):
            raise ValueError('TX evidence keys must be bounded strings')
        _budget[1] -= sum(len(key) * 4 + 64 for key in value)
        if _budget[1] < 0:
            raise ValueError('TX evidence mapping exceeds accounting budget')
        return MappingProxyType({key: freeze_evidence(child, _depth=_depth + 1, _budget=_budget)
                                 for key, child in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze_evidence(child, _depth=_depth + 1, _budget=_budget) for child in value)
    raise ValueError('Unsupported TX evidence value type: ' + type(value).__name__)


def _jsonable(value):
    if isinstance(value, Mapping):
        return {key: _jsonable(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(child) for child in value]
    if isinstance(value, bytes):
        return {'hex': value.hex(), 'length': len(value)}
    return value


class TxEvidenceWriter:
    """One bounded queue and one optional writer. All failures remain observable.

    capacity=4096 is a candidate RAM budget, not a tested realtime guarantee.
    start() and close() belong outside the send loop. close() never masks a
    caller's transport exception; its returned status must enter the run report.
    """
    def __init__(self, cfg, path, *, capacity=4096, verify_storage=None):
        if type(capacity) is not int or not 1 <= capacity <= 16384:
            raise ValueError('TX evidence capacity must be within 1..16384')
        self.cfg, self.path, self.capacity = dict(cfg), Path(path), capacity
        self._verify_storage = verify_storage or verify_usb
        self.queue = queue.Queue(maxsize=capacity)
        # Sequence assignment and FIFO insertion form one producer transaction.
        # Metadata freezing is bounded; no disk I/O or queue wait holds this lock.
        self._enqueue_lock = threading.RLock()
        self._sequence = itertools.count(1)
        self._last_attempted_sequence = 0
        self._last_written_sequence = 0
        self._accepted = self._written = self._overflows = self._rejected = self._sequence_gaps = 0
        self._producer_errors = 0
        self._producer_error = self._writer_error = None
        self._flush_timeout = False
        self._closed = False
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._opened = False
        self._finished = False

    def mark_incomplete(self, reason):
        with self._enqueue_lock:
            self._producer_errors += 1
            self._producer_error = str(reason)[:1024]

    def enqueue(self, record):
        with self._enqueue_lock:
            sequence = next(self._sequence)
            self._last_attempted_sequence = sequence
            if self._closed or self._writer_error is not None:
                self._rejected += 1
                return False
            try:
                item = freeze_evidence({'sequence': sequence, 'record': record})
                self.queue.put_nowait(item)
                self._accepted += 1
                return True
            except queue.Full:
                self._overflows += 1
            except Exception as exc:
                self.mark_incomplete(type(exc).__name__ + ': ' + str(exc))
        return False

    def start(self, *, wait_s=1.0):
        if self._thread is not None or self._closed:
            raise RuntimeError('TX evidence writer already started/closed')
        self._thread = threading.Thread(target=self._write, name='usb-tx-evidence-writer', daemon=True)
        self._thread.start()
        self._ready.wait(max(0., min(float(wait_s), 5.)))
        return self.status()

    def _verify_path(self):
        root = Path(self._verify_storage(self.cfg)).resolve()
        if not self.path.resolve().is_relative_to(root):
            raise RuntimeError('TX evidence path is outside the verified USB')

    def _write(self):
        handle = None
        try:
            self._verify_path()
            handle = self.path.open('x', encoding='utf-8', buffering=65536)
            self._opened = True
            handle.write(json.dumps({'schema': 'tiger-host-can-tx-evidence-v1',
                'capacity': self.capacity, 'capacity_status': 'candidate_not_realtime_certified',
                'meaning': 'host SDK call evidence; not motor acceptance or bus-wire timestamps'}) + '\n')
            self._ready.set()
            check_at = time.monotonic() + 1.
            flush_at = time.monotonic() + .5
            while not self._stop.is_set() or not self.queue.empty():
                try:
                    item = self.queue.get(timeout=.05)
                except queue.Empty:
                    continue
                now = time.monotonic()
                if now >= check_at:
                    self._verify_path()
                    check_at = now + 1.
                sequence = item['sequence']
                if sequence != self._last_written_sequence + 1:
                    self._sequence_gaps += max(1, sequence - self._last_written_sequence - 1)
                handle.write(json.dumps(_jsonable(item), separators=(',', ':'), allow_nan=False) + '\n')
                self._last_written_sequence = sequence
                self._written += 1
                if now >= flush_at:
                    handle.flush()
                    flush_at = now + .5
            self._verify_path()
            handle.write(json.dumps({'schema': 'tiger-host-can-tx-evidence-summary-v1',
                                     'final_flush_and_close_result': 'reported by caller in operation receipt',
                                     **self.status()}, separators=(',', ':'), allow_nan=False) + '\n')
            handle.flush()
        except Exception as exc:
            self._writer_error = type(exc).__name__ + ': ' + str(exc)
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception as exc:
                    self._writer_error = (self._writer_error or '') + '; close: ' + repr(exc)
            self._finished = True
            self._ready.set()

    def close(self, *, timeout_s=5.):
        with self._enqueue_lock:
            self._closed = True
            self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0., min(float(timeout_s), 15.)))
            self._flush_timeout = self._thread.is_alive()
        elif self._accepted:
            self.mark_incomplete('Writer never started; queued evidence not persisted')
        return self.status()

    def status(self):
        tail = max(0, self._last_attempted_sequence - self._last_written_sequence) if self._closed else 0
        incomplete = bool(self._overflows or self._rejected or self._producer_errors or self._writer_error
                          or self._sequence_gaps or self._flush_timeout or (self._closed and tail))
        return {'path': str(self.path), 'capacity': self.capacity, 'capacity_status': 'candidate',
            'per_record_accounting_limit_bytes': 16384,
            'queue_accounting_limit_bytes': self.capacity * 16384,
            'accounting_is_not_measured_python_heap': True,
            'opened': self._opened, 'finished': self._finished,
            'writer_alive': self._thread is not None and self._thread.is_alive(),
            'accepted': self._accepted, 'written': self._written, 'queued': self.queue.qsize(),
            'overflows': self._overflows, 'rejected': self._rejected,
            'sequence_gaps': self._sequence_gaps, 'tail_unwritten_sequences': tail,
            'last_attempted_sequence': self._last_attempted_sequence,
            'last_written_sequence': self._last_written_sequence,
            'producer_errors': self._producer_errors, 'producer_error': self._producer_error,
            'writer_error': self._writer_error, 'flush_timeout': self._flush_timeout,
            'timing_evidence_incomplete': incomplete}
