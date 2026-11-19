"""Bounded asynchronous evidence for the isolated V2 long-window runner.

Producers freeze bounded metadata and enqueue without JSON or filesystem I/O.
Workers batch durable chunks through H50USBJournal's original held USB fds.
Queue acceptance is NOT persistence; only a complete close receipt proves drain.
No robot, socket, model, storage discovery or worker is opened on import.
"""
from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
from types import MappingProxyType
import numpy as np

from agilex_control.tx_evidence import TxEvidenceWriter, _jsonable


from .packet_images import PacketImages, prepare_snapshot
from .evidence_reader import SCHEMA, DESCRIPTOR_SCHEMA, CHUNK_SCHEMA, iter_evidence_records
TICK_RECORD_LIMIT = 256 * 1024
PACKET_RECORD_LIMIT = 1024 * 1024
QUEUE_RECORD_LIMIT = 256
QUEUE_BYTE_LIMIT = 32 * 1024 * 1024
BATCH_BYTE_LIMIT = 4 * 1024 * 1024
MAX_CHUNKS = 20000


class _FairDiskGate:
    """FIFO handoff between evidence workers; never used by control producers.

    H50USBJournal's underlying RLock protects its held descriptors but does not
    promise fairness. A ticket holder cannot immediately re-enter ahead of an
    already waiting worker. One slow filesystem call can still cause a bounded
    queue to fill: this is scheduling fairness, not a disk-latency guarantee.
    """
    def __init__(self):
        self.condition = threading.Condition()
        self.next_ticket = self.serving = 0
        self.waiting = self.peak_waiting = 0
        self.completed = self.failed = 0
        self.samples = deque(maxlen=4096)
        self.peak_wait_ns = self.peak_io_ns = 0

    def run(self, operation, function, *args):
        queued_ns = time.monotonic_ns()
        with self.condition:
            ticket = self.next_ticket
            self.next_ticket += 1
            self.waiting += 1
            self.peak_waiting = max(self.peak_waiting, self.waiting)
            while ticket != self.serving:
                self.condition.wait()
            self.waiting -= 1
        acquired_ns = time.monotonic_ns()
        succeeded = False
        try:
            value = function(*args)
            succeeded = True
        finally:
            finished_ns = time.monotonic_ns()
            timing = {'ticket': ticket, 'operation': operation,
                      'worker': threading.current_thread().name,
                      'queued_ns': queued_ns, 'acquired_ns': acquired_ns,
                      'finished_ns': finished_ns, 'succeeded': succeeded,
                      'lock_wait_ms': (acquired_ns - queued_ns) / 1e6,
                      'io_call_ms': (finished_ns - acquired_ns) / 1e6}
            with self.condition:
                self.samples.append(timing)
                self.completed += 1
                self.failed += not succeeded
                self.peak_wait_ns = max(self.peak_wait_ns, acquired_ns - queued_ns)
                self.peak_io_ns = max(self.peak_io_ns, finished_ns - acquired_ns)
                self.serving += 1
                self.condition.notify_all()
        return value, timing

    def status(self):
        """Finalization-only summary; no sorting on the control path."""
        with self.condition:
            samples = list(self.samples)
            result = {'ordering': 'FIFO_TICKET', 'completed_calls': self.completed,
                      'failed_calls': self.failed, 'waiting': self.waiting,
                      'peak_waiting': self.peak_waiting, 'samples_retained': len(samples),
                      'sample_capacity': self.samples.maxlen,
                      'peak_lock_wait_ms': self.peak_wait_ns / 1e6,
                      'peak_io_call_ms': self.peak_io_ns / 1e6,
                      'statistics_scope': 'worker journal calls; not control-loop timing or physical latency'}
        for key in ('lock_wait_ms', 'io_call_ms'):
            values = sorted(row[key] for row in samples)
            result[key + '_p95_retained'] = None if not values else values[min(len(values) - 1, int(.95 * len(values)))]
        result['recent_calls'] = samples[-16:]
        return result


def _freeze(value, *, byte_limit, node_limit=16384, max_depth=24):
    """Bounded immutable snapshot; accounting is deliberately NOT measured RSS."""
    budget = [node_limit, byte_limit]

    def exceeded(metric, actual, limit, path):
        field = '$' + ''.join(f'[{k}]' if isinstance(k, int) else '.' + k for k in path)
        raise ValueError(f'V2 evidence {metric} at {field}: {actual} > {limit}')

    def visit(item, depth, path):
        budget[0] -= 1
        budget[1] -= 64
        if depth > max_depth:
            exceeded('nesting depth', depth, max_depth, path)
        if budget[0] < 0:
            exceeded('nodes', node_limit - budget[0], node_limit, path)
        if budget[1] < 0:
            exceeded('metadata bytes', byte_limit - budget[1], byte_limit, path)
        if isinstance(item, Enum):
            return visit({'enum_type': type(item).__name__, 'name': item.name, 'value': item.value}, depth + 1, path)
        # NumPy scalar arithmetic may occur in rare rejection diagnostics.
        # Normalize numeric scalars without changing the value or consuming an
        # extra accounting node. Arrays and arbitrary objects remain rejected.
        if isinstance(item, np.generic) and item.dtype.kind in ('b', 'i', 'u', 'f'):
            if item.dtype.kind == 'f' and item.dtype.itemsize > 8:
                raise ValueError('Unsupported V2 evidence floating precision: ' + str(item.dtype))
            item = item.item()
        if item is None or type(item) in (bool, int):
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError('Nonfinite V2 evidence metadata')
            return item
        if isinstance(item, (str, bytes)):
            budget[1] -= len(item) * (4 if isinstance(item, str) else 1)
            if budget[1] < 0:
                exceeded('string accounting bytes', byte_limit - budget[1], byte_limit, path)
            return item
        if isinstance(item, Mapping):
            if any(not isinstance(k, str) or len(k) > 256 for k in item):
                raise ValueError('V2 evidence keys must be bounded strings')
            budget[1] -= sum(len(k) * 4 + 64 for k in item)
            if budget[1] < 0:
                exceeded('key accounting bytes', byte_limit - budget[1], byte_limit, path)
            return MappingProxyType({k: visit(v, depth + 1, path + (k,)) for k, v in item.items()
                                     if k.lower() not in ('lease_token', 'token', 'service_nonce')})
        if isinstance(item, (list, tuple)):
            return tuple(visit(v, depth + 1, path + (index,)) for index, v in enumerate(item))
        raise ValueError('Unsupported V2 evidence metadata type: ' + type(item).__name__)

    frozen = visit(value, 0, ())
    return frozen, byte_limit - budget[1]


class _ChunkStore:
    """Only called by worker threads or finalization, never control producers."""
    def __init__(self, disk, root, stream, gate):
        self.disk, self.root, self.stream = disk, Path(root), stream
        self.gate = gate
        self.chunks = []
        self.written = 0

    def write(self, rows):
        if not rows:
            return
        if len(self.chunks) >= MAX_CHUNKS:
            raise RuntimeError('Evidence chunk manifest capacity exhausted')
        header = {'schema': CHUNK_SCHEMA, 'stream': self.stream,
                  'first_sequence': rows[0]['sequence'], 'count': len(rows)}
        lines = [json.dumps(header, separators=(',', ':'), allow_nan=False)]
        lines.extend(json.dumps(_jsonable(row), ensure_ascii=False, separators=(',', ':'),
                                allow_nan=False) for row in rows)
        raw = ('\n'.join(lines) + '\n').encode('utf-8')
        name = f'{self.stream}-{len(self.chunks):06d}.jsonl'
        _, timing = self.gate.run('write_' + self.stream, self.disk.write_bytes, self.root / name, raw)
        self.chunks.append({'path': name, 'count': len(rows), 'bytes': len(raw),
                            'first_sequence': rows[0]['sequence'],
                            'last_sequence': rows[-1]['sequence'],
                            'sha256': hashlib.sha256(raw).hexdigest(),
                            'persistence_timing': timing})
        self.written += len(rows)


class _AnchoredTxEvidence(TxEvidenceWriter):
    """Canonical TX enqueue/freeze/sequence logic with held-fd chunk persistence.

    The canonical path-opening writer is intentionally overridden: each chunk
    uses the already verified H50USBJournal, so detached USB cannot fall back to
    an internal directory. The canonical 4096 queue budget is unchanged.
    """
    def __init__(self, cfg, run_dir, disk, gate, *, capacity=4096):
        super().__init__(cfg, Path(run_dir) / 'host-CAN-events.json', capacity=capacity)
        self.store = _ChunkStore(disk, run_dir, 'tx', gate)
        self.gate = gate
        self.peak_queued = 0

    def __call__(self, record):
        return self.enqueue(record)

    def __len__(self):
        return self._accepted

    def enqueue(self, record):
        accepted = super().enqueue(record)
        self.peak_queued = max(self.peak_queued, self.queue.qsize())
        return accepted

    def _write(self):
        batch = []
        try:
            self.gate.run('verify_tx_start', self.store.disk.verify)
            self._opened = True
            self._ready.set()
            flush_at = time.monotonic() + .5
            while not self._stop.is_set() or not self.queue.empty():
                try:
                    item = self.queue.get(timeout=.05)
                    batch.append(item)
                except queue.Empty:
                    pass
                now = time.monotonic()
                if batch and (len(batch) >= 512 or now >= flush_at):
                    self._persist(batch)
                    batch.clear()
                    # A slow durable write must not create an overdue timer
                    # which immediately fsyncs another one-record chunk.
                    flush_at = time.monotonic() + .5
                # The disk guard also runs while the queue is idle.
                if now >= flush_at:
                    self.gate.run('verify_tx_idle', self.store.disk.verify)
                    flush_at = time.monotonic() + .5
            if batch:
                self._persist(batch)
            self.gate.run('verify_tx_finish', self.store.disk.verify)
        except BaseException as exc:
            self._writer_error = type(exc).__name__ + ': ' + str(exc)
        finally:
            self._finished = True
            self._ready.set()

    def _persist(self, rows):
        self.store.write(rows)
        for item in rows:
            sequence = item['sequence']
            if sequence != self._last_written_sequence + 1:
                self._sequence_gaps += max(1, sequence - self._last_written_sequence - 1)
            self._last_written_sequence = sequence
            self._written += 1
            self.queue.task_done()

    def status(self):
        result = super().status()
        result.update(storage='ANCHORED_ASYNC_USB_CHUNKS', peak_queued=self.peak_queued,
                      worker_batch_record_limit=512,
                      worker_batch_accounting_limit_bytes=512 * 16384)
        return result


class SessionEvidence:
    """Owned evidence session. Constructor and start must precede control loops.

    _test_journal_factory is an explicit CPU fixture hook, not a production
    fallback. Such receipts carry synthetic_fixture=True. Production always
    constructs H50USBJournal with the unchanged selected USB configuration.
    """
    def __init__(self, cfg, run_dir, *, _test_journal_factory=None,
                 _test_queue_records=None, _test_queue_bytes=None, _test_close_timeout=None):
        self.cfg, self.run_dir = deepcopy(cfg), Path(run_dir)
        self.synthetic = _test_journal_factory is not None
        if not self.synthetic and any(x is not None for x in
                                      (_test_queue_records, _test_queue_bytes, _test_close_timeout)):
            raise ValueError('Test limits require an explicit synthetic journal')
        if _test_journal_factory is None:
            from agilex_control.h50_journal import H50USBJournal
            _test_journal_factory = H50USBJournal
        self.disk = _test_journal_factory(cfg, self.run_dir)
        self.capacity = _test_queue_records or QUEUE_RECORD_LIMIT
        self.byte_limit = _test_queue_bytes or QUEUE_BYTE_LIMIT
        self.close_timeout = 15. if _test_close_timeout is None else float(_test_close_timeout)
        self.pending = queue.Queue(maxsize=self.capacity)
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.ready = threading.Event()
        self.failed = threading.Event()
        self.started = False
        self.thread = None
        self.final = None
        self.errors = []
        self.accepted = {'ticks': 0, 'packets': 0}
        self.written = {'ticks': 0, 'packets': 0}
        self.pending_bytes = self.peak_bytes = self.peak_records = 0
        self.rejected = self.overflows = 0
        self.io_gate = _FairDiskGate()
        self.stores = {k: _ChunkStore(self.disk, self.run_dir, k, self.io_gate) for k in self.accepted}
        self.tx_sink = _AnchoredTxEvidence(cfg, self.run_dir, self.disk, self.io_gate)

    def _fail(self, reason):
        with self.lock:
            self.failed.set()
            if len(self.errors) < 16:
                self.errors.append(str(reason)[:1024])

    @property
    def counts(self):
        with self.lock:
            return {**self.accepted, 'tx': self.tx_sink.status()['accepted']}

    @property
    def tick_count(self):
        return self.accepted['ticks']

    @property
    def packet_count(self):
        return self.accepted['packets']

    def start(self):
        if self.started or self.closing.is_set():
            raise RuntimeError('V2 evidence already started/closed')
        self.started = True
        self.thread = threading.Thread(target=self._run, name='v2-USB-chunk-evidence', daemon=True)
        self.thread.start()
        self.ready.wait(5.)
        self.tx_sink.start(wait_s=5.)
        self.check_alive()
        if not self.ready.is_set() or not self.tx_sink.status()['opened']:
            self._fail('Evidence worker did not become ready')
            raise RuntimeError(self.errors[0])
        return self

    def check_alive(self):
        if self.failed.is_set():
            raise RuntimeError('V2 evidence failed: ' + '; '.join(self.errors))
        if self.closing.is_set() or not self.started:
            raise RuntimeError('V2 evidence is not open')
        status = self.tx_sink.status()
        if status['timing_evidence_incomplete']:
            self._fail('Canonical TX evidence incomplete: ' + str(status.get('writer_error') or
                        status.get('producer_error') or 'overflow/rejection/sequence gap'))
            raise RuntimeError(self.errors[-1])
        if not self.thread.is_alive() or not status['writer_alive']:
            self._fail('Evidence worker exited unexpectedly')
            raise RuntimeError(self.errors[-1])

    def _append(self, stream, row, limit):
        self.check_alive()
        attachment = None
        try:
            if stream == 'packets' and 'captured_snapshot' in row:
                images = row['captured_snapshot']
                if not isinstance(images, PacketImages):
                    images = prepare_snapshot(images)
                if isinstance(images, PacketImages):
                    row = {**row, 'captured_snapshot': images.snapshot}
                    attachment = (images.filename, images.data)
            frozen, charge = _freeze(row, byte_limit=limit)
            if attachment is not None:
                # The independently bounded payload still counts against total RAM.
                charge += len(attachment[1]) + 4 * len(attachment[0]) + 128
        except BaseException as exc:
            self._fail(type(exc).__name__ + ': ' + str(exc))
            raise
        with self.lock:
            self.check_alive()
            if self.pending_bytes + charge > self.byte_limit:
                self.overflows += 1
                self._fail('V2 evidence byte budget exhausted; no record silently dropped')
                raise RuntimeError(self.errors[-1])
            sequence = self.accepted[stream] + 1
            try:
                self.pending.put_nowait((stream, sequence, frozen, charge, attachment))
            except queue.Full:
                self.overflows += 1
                self._fail('V2 evidence queue full; no record silently dropped')
                raise RuntimeError(self.errors[-1]) from None
            self.accepted[stream] = sequence
            self.pending_bytes += charge
            self.peak_bytes = max(self.peak_bytes, self.pending_bytes)
            self.peak_records = max(self.peak_records, self.pending.qsize())
            return {'sequence': sequence, 'ram_accepted': True, 'persisted': False,
                    'accounting_bytes': charge}

    def append_tick(self, row):
        return self._append('ticks', row, TICK_RECORD_LIMIT)

    def append_packet(self, row):
        return self._append('packets', row, PACKET_RECORD_LIMIT)

    def _run(self):
        batches = {k: [] for k in self.stores}
        charges = {k: 0 for k in self.stores}
        try:
            self.io_gate.run('verify_streams_start', self.disk.verify)
            self.ready.set()
            flush_at = time.monotonic() + .5
            while not self.closing.is_set() or not self.pending.empty():
                try:
                    stream, sequence, row, charge, attachment = self.pending.get(timeout=.05)
                    if attachment is not None:
                        filename, raw = attachment
                        self.io_gate.run('write_packet_images', self._persist_images, filename, raw)
                        del raw, attachment
                    batches[stream].append({'sequence': sequence, 'record': row})
                    charges[stream] += charge
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now >= flush_at or any(len(v) >= 128 for v in batches.values()) or sum(charges.values()) >= BATCH_BYTE_LIMIT:
                    self._flush(batches, charges)
                    self.io_gate.run('verify_streams_batch', self.disk.verify)
                    flush_at = time.monotonic() + .5
            self._flush(batches, charges)
            self.io_gate.run('verify_streams_finish', self.disk.verify)
        except BaseException as exc:
            self._fail(type(exc).__name__ + ': ' + str(exc))
        finally:
            self.ready.set()

    def _persist_images(self, filename, raw):
        path = self.run_dir / filename
        self.disk.write_bytes(path, raw)
        self.disk.verify()
        if self.synthetic:
            handle = path.open('rb')
        else:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.disk.episode_fd)
            handle = os.fdopen(fd, 'rb')
        with handle:
            if handle.read(len(raw) + 1) != raw:
                raise ValueError('V2 packet image USB readback mismatch: ' + filename)
        self.disk.verify()

    def _flush(self, batches, charges):
        for stream, rows in batches.items():
            if rows:
                self.stores[stream].write(rows)
                with self.lock:
                    self.written[stream] += len(rows)
                    self.pending_bytes -= charges[stream]
                for _ in rows:
                    self.pending.task_done()
                rows.clear()
                charges[stream] = 0

    def close(self):
        """Idempotent final drain, never upgrades timeout or failure to success."""
        if self.final is not None:
            if not (self.thread and self.thread.is_alive()) and not self.tx_sink.status()['writer_alive']:
                self.disk.close()
            return deepcopy(self.final)
        with self.lock:
            self.closing.set()
        if self.thread is not None:
            self.thread.join(self.close_timeout)
        tx = self.tx_sink.close(timeout_s=self.close_timeout)
        alive = bool(self.thread is not None and self.thread.is_alive())
        if alive or tx['writer_alive']:
            self._fail('Evidence drain timed out; unflushed data may remain volatile')
        if not self.started:
            self._fail('Evidence session never started')
        complete = (not self.failed.is_set() and not alive and not tx['writer_alive']
                    and not tx['timing_evidence_incomplete'] and self.pending_bytes == 0
                    and self.accepted == self.written and tx['accepted'] == tx['written'])
        streams = {name: {'accepted': self.accepted[name], 'written': self.written[name],
                          'chunks': deepcopy(store.chunks)} for name, store in self.stores.items()}
        streams['tx'] = {**tx, 'chunks': deepcopy(self.tx_sink.store.chunks)}
        result = {'schema': SCHEMA, 'complete': complete, 'synthetic_fixture': self.synthetic,
                  'counts': self.counts, 'streams': streams, 'manifest': str(self.run_dir / 'evidence-manifest.json'),
                  'failure_reasons': list(self.errors), 'queue_capacity_records': self.capacity,
                  'queue_accounting_limit_bytes': self.byte_limit, 'pending_accounting_bytes': self.pending_bytes,
                  'peak_pending_accounting_bytes': self.peak_bytes, 'peak_queued_records': self.peak_records,
                  'tick_record_accounting_limit_bytes': TICK_RECORD_LIMIT,
                  'packet_record_accounting_limit_bytes': PACKET_RECORD_LIMIT,
                  'packet_image_storage': 'exact JPEG bytes in checksummed, run-relative files before packet chunks',
                  'batch_accounting_flush_trigger_bytes': BATCH_BYTE_LIMIT,
                  'worker_flush_interval_s': .5,
                  'flush_deadline_origin': 'completion_of_previous_flush',
                  'persistence_io': self.io_gate.status(),
                  'accounting_is_not_measured_python_heap_or_RSS': True,
                  'overflows': self.overflows, 'storage_binding': deepcopy(getattr(self.disk, 'storage_binding', {})),
                  'meaning': 'Recorded software/host evidence; no assertion of motor receipt, physical stop or task success'}
        if not alive and not tx['writer_alive']:
            try:
                self.disk.verify()
                for stream, name in [('ticks', 'physical-ticks.json'), ('packets', 'physical-policy-packets.json'),
                                     ('tx', 'host-CAN-events.json'), ('ticks', 'continuous-ticks.json'),
                                     ('packets', 'continuous-policy-packets.json'), ('packets', 'continuous-packets.json')]:
                    self.disk.write_json(self.run_dir / name,
                        {'schema': DESCRIPTOR_SCHEMA, 'stream': stream, 'manifest': 'evidence-manifest.json',
                         'count': streams[stream]['written'], 'is_record_list': False,
                         'reader': 'src.long_window_evidence.iter_evidence_records'})
                # Manifest is last: all chunks and descriptors must be durable first.
                self.disk.write_json(self.run_dir / 'evidence-manifest.json', result)
            except BaseException as exc:
                result['complete'] = False
                result['proof_write_error'] = type(exc).__name__ + ': ' + str(exc)
                result['failure_reasons'].append(result['proof_write_error'])
            finally:
                self.disk.close()
        self.final = deepcopy(result)
        return deepcopy(self.final)

