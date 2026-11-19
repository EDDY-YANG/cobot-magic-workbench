"""Explicit real-USB pressure test using fixed historical payload sizes.

No CAN socket, Hub, camera, model, controller or actuator interface is imported.
Records retain ORIGINAL source timestamps/IDs. These files are storage-load
repetitions, never a new robot trajectory, live sensor stream or motion proof.
"""
import argparse
from collections import deque
from contextlib import ExitStack, contextmanager
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from src.evidence_reader import iter_evidence_records, DESCRIPTOR_SCHEMA
from src.runtime_guard import can_counters, verify_zero_tx

SCOPE = 'HISTORICAL_PAYLOAD_STORAGE_PRESSURE_ONLY_NOT_TRAJECTORY_OR_ROBOT_MOTION'
PERIOD_NS = 20_000_000
OWNER_NAMES = ('h50-deployment', 'fp32-shadow-run', 'writer-can_right')


def require(value, message):
    if not value:
        raise RuntimeError(message)


def canonical_bytes(row):
    return json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf8')


def largest_samples(path, count):
    """Read descriptor chunks incrementally, retaining only the largest K rows.

    Old monolithic JSON arrays are deliberately refused: the pressure source is
    the failed streaming runtime, and loading its entire history is unnecessary.
    """
    path = Path(path)
    require(not path.is_symlink() and path.resolve(strict=True).is_relative_to(path.parent.resolve(strict=True)),
            'Source descriptor must stay in its selected historical run')
    require(path.stat().st_size <= 64 * 1024, 'Bounded evidence descriptor required')
    raw = path.read_bytes(); document = json.loads(raw)
    require(isinstance(document, dict) and document.get('schema') == DESCRIPTOR_SCHEMA,
            'Pressure source must be a streamed descriptor, not whole-run JSON')
    heap = []; rows_seen = 0; source_bytes = 0
    for index, row in enumerate(iter_evidence_records(path, require_complete=False)):
        require(isinstance(row, dict), 'Historical record must be an object')
        encoded = canonical_bytes(row); size = len(encoded)
        require(size <= 2 * 1024 * 1024, 'Historical record exceeds source scan budget')
        rows_seen += 1; source_bytes += size
        item = (size, index, row, hashlib.sha256(encoded).hexdigest())
        if len(heap) < count:
            heapq.heappush(heap, item)
        elif item[:2] > heap[0][:2]:
            heapq.heapreplace(heap, item)
    require(len(heap) == count, 'Insufficient historical records for fixed pressure payload')
    selected = sorted(heap, reverse=True)
    return [item[2] for item in selected], {
        'descriptor': str(path), 'descriptor_sha256': hashlib.sha256(raw).hexdigest(),
        'rows_scanned': rows_seen, 'serialized_source_bytes_scanned': source_bytes,
        'selection': 'LARGEST_COMPACT_JSON_RECORDS_FROM_COMPLETE_DURABLE_PREFIX',
        'selected': [{'source_zero_based_index': index, 'compact_json_bytes': size,
                      'payload_sha256': digest} for size, index, _, digest in selected],
        'source_complete_required': False,
    }


def select_payloads(source_run):
    source_run = Path(source_run)
    tick, tick_proof = largest_samples(source_run / 'physical-ticks.json', 1)
    packet, packet_proof = largest_samples(source_run / 'physical-policy-packets.json', 1)
    tx, tx_proof = largest_samples(source_run / 'host-CAN-events.json', 12)
    return {'tick': tick[0], 'packet': packet[0], 'tx': tx}, {
        'tick': tick_proof, 'packet': packet_proof, 'tx': tx_proof,
        'fixed_payloads_repeated': True, 'trajectory_replayed': False,
        'original_timestamps_and_ids_preserved': True,
        'largest_by_serialized_size_is_not_a_physical_worst_case_claim': True,
    }


def run_pressure(evidence, payloads, duration, stop, *, clock=time, emit=lambda _: None):
    """Wall-clock pacing, injectable clock only for explicit CPU unit tests."""
    require(type(duration) in (int, float) and math.isfinite(duration) and 1 <= duration <= 1800,
            'Storage test duration must be a finite 1..1800 seconds')
    start = clock.monotonic_ns(); end = start + round(duration * 1e9)
    tick_index = packet_index = tx_count = 0
    gaps = deque(maxlen=8192); lateness = deque(maxlen=8192)
    last = None; peak_gap_ns = 0; late_cycles = 0; next_progress_s = 10
    while clock.monotonic_ns() < end:
        require(not stop.is_set(), 'Storage pressure test cancelled')
        now = clock.monotonic_ns(); due = start + tick_index * PERIOD_NS
        if now < due:
            stop.wait(min(.005, (due-now)/1e9)); continue
        evidence.check_alive()
        delay = now-due
        # Failure is retained instead of slowing the producer and falsely
        # calling an under-rate stream a successful 50Hz pressure test.
        require(delay < 100_000_000, 'Pressure producer more than 100ms behind schedule')
        lateness.append(delay/1e6); late_cycles += int(delay >= PERIOD_NS)
        if last is not None:
            gap = now-last; peak_gap_ns = max(peak_gap_ns, gap); gaps.append(gap/1e6)
        last = now
        evidence.append_tick(payloads['tick'])
        for row in payloads['tx']:
            require(evidence.tx_sink.enqueue(row) is True, 'Pressure TX evidence enqueue refused')
            tx_count += 1
        if now >= start + round(packet_index * 14 * 1e9 / 18):
            evidence.append_packet(payloads['packet']); packet_index += 1
        tick_index += 1
        elapsed_now = (clock.monotonic_ns()-start)/1e9
        if elapsed_now >= next_progress_s:
            emit({'event': 'CPU_STORAGE_REPLAY_PROGRESS', 'elapsed_s': round(elapsed_now, 3),
                  'ticks': tick_index, 'packets': packet_index, 'tx_records': tx_count,
                  'peak_queued_records': getattr(evidence, 'peak_records', None),
                  'robot_commands_sent': 0})
            next_progress_s += 10
    evidence.check_alive()
    expected = math.ceil(duration * 50)
    require(tick_index == expected, 'Pressure tick count differs from the requested full-rate window')
    elapsed = (clock.monotonic_ns()-start)/1e9
    def stats(values):
        ordered = sorted(values)
        if not ordered:
            return None
        return {'sample_count': len(ordered), 'max': ordered[-1],
                'p95': ordered[min(len(ordered)-1, math.ceil(.95*len(ordered))-1)]}
    return {'scope': SCOPE, 'requested_duration_s': duration, 'actual_producer_wall_s': elapsed,
            'producer_start_monotonic_ns': start, 'producer_end_monotonic_ns': clock.monotonic_ns(),
            'tick_count': tick_index, 'packet_count': packet_index, 'tx_record_count': tx_count,
            'target_tick_hz': 50, 'target_packet_hz': 18/14, 'tx_records_per_tick': 12,
            'observed_tick_hz': tick_index/elapsed, 'late_by_at_least_20ms_cycles': late_cycles,
            'maximum_actual_tick_gap_ms': peak_gap_ns/1e6,
            'tick_gap_ms': stats(gaps), 'schedule_lateness_ms': stats(lateness),
            'metric_sample_scope': 'LAST_8192_RECORDS_PLUS_FULL_MAX_GAP_AND_COUNTS'}


def validate_output(output, runtime_root, usb_root):
    output = Path(output); runtime_root = Path(runtime_root).resolve(strict=True)
    usb_root = Path(usb_root).resolve(strict=True)
    require(output.is_absolute() and '..' not in output.parts, 'Explicit absolute output required')
    require(output.parent == runtime_root / 'runs', 'Output must be a new direct runtime/runs child')
    require(re.fullmatch(r'\d{4}(?:-\d{2}){5}-CPU-STORAGE-REPLAY(?:-[0-9a-f]{8})?', output.name),
            'Output requires YYYY-MM-DD-HH-MM-SS-CPU-STORAGE-REPLAY name')
    require(not output.exists() and not output.is_symlink(), 'Output already exists; no retry or overwrite')
    require(runtime_root.is_relative_to(usb_root) and output.parent.resolve(strict=True).is_relative_to(usb_root),
            'Runtime/output must remain on selected USB')
    return output


@contextmanager
def storage_ownership(storage, cfg):
    """Hold the same three advisory locks, without a model lease or CAN writer."""
    with ExitStack() as stack:
        for name in OWNER_NAMES:
            stack.enter_context(storage.Ownership(name))
        storage.verify_usb(cfg)
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--config-path', type=Path)
    parser.add_argument('--canonical-root', type=Path)
    parser.add_argument('--duration', type=float, default=120.)
    parser.add_argument('--execute-storage-test', action='store_true')
    args = parser.parse_args(argv)
    if not args.execute_storage_test:
        print(json.dumps({'status': 'INSPECT_ONLY_NO_STORAGE_OR_HARDWARE_ACTION', 'scope': SCOPE,
                          'target_duration_s': args.duration, 'model_calls': 0, 'CAN_sockets': 0}))
        return 0
    require(sys.platform == 'linux', 'Real USB pressure test requires staged Linux host')
    require(all((args.source_run, args.output, args.config_path, args.canonical_root)),
            'Explicit source, new output, canonical root and config paths required')
    require(math.isfinite(args.duration) and 1 <= args.duration <= 1800, 'Duration must be finite 1..1800 seconds')
    require(Path(sys.executable).absolute().is_relative_to(ROOT), 'Use the new isolated runtime interpreter')
    canonical = args.canonical_root.resolve(strict=True)
    cfg = json.loads(args.config_path.read_text(encoding='utf8'))
    require(Path(cfg['remote_bundle']).resolve(strict=True) == canonical, 'Canonical/config root mismatch')
    sys.path.insert(0, str(canonical))
    from agilex_control import storage
    from agilex_control.usb_storage import open_usb_directory
    from agilex_control.h50_journal import H50USBJournal
    from src.long_window_evidence import SessionEvidence
    require(Path(storage.__file__).resolve().is_relative_to(canonical), 'Foreign canonical storage package')
    usb_root = Path(storage.verify_usb(cfg)).resolve(strict=True)
    require(ROOT.is_relative_to(usb_root) and os.stat(ROOT).st_dev == os.stat(usb_root).st_dev,
            'New runtime must be on the verified USB')
    with storage_ownership(storage, cfg):
        source = args.source_run.resolve(strict=True)
        require(source.is_relative_to(usb_root) and source != ROOT and not args.source_run.is_symlink(),
                'Historical source must be an explicit USB run')
        output = validate_output(args.output, ROOT, usb_root)
        payloads, selection = select_payloads(source)
        storage.verify_usb(cfg)
        before = can_counters()
        descriptor = open_usb_directory(output, cfg, create=True, exclusive_final=True); os.close(descriptor)
        journal = H50USBJournal(cfg, output)
        evidence = None; stop = threading.Event(); error = None
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, lambda *_: stop.set())
        started = time.monotonic()
        report = {'schema': 'v2-real-usb-historical-payload-pressure-v1', 'scope': SCOPE,
                  'status': 'STARTED', 'source_run': str(source), 'run_dir': str(output),
                  'selection': selection, 'requested_duration_s': args.duration,
                  'real_USB_writer': True, 'synthetic_journal': False,
                  'original_source_times_rewritten': False, 'live_trajectory_or_sensor_evidence': False,
                  'all_enqueued_records_are_historical_repetitions': True,
                  'native_camera_recording_load_included': False,
                  'physical_tracking_or_1800s_motion_validated': False,
                  'model_calls': 0, 'CAN_socket_created': False, 'Hub_recording_started': False,
                  'exclusive_advisory_locks': list(OWNER_NAMES), 'model_lease_acquired': False, 'STOP_cleared': False,
                  'robot_commands_sent': 0, 'interface_before': before,
                  'usb_identity': {'mountpoint': str(usb_root), 'uuid': cfg['usb_uuid'],
                                   'major_minor': cfg['usb_selection']['major_minor']}}
        try:
            journal.write_json(output/'storage-pressure.json', report)
            evidence = SessionEvidence(cfg, output)
            evidence.start()
            report['producer'] = run_pressure(evidence, payloads, args.duration, stop,
                emit=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
        except BaseException as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        finally:
            if evidence is not None:
                try:
                    report['evidence'] = evidence.close()
                    require(report['evidence'].get('complete') is True, 'Storage evidence incomplete')
                    require(report['evidence'].get('synthetic_fixture') is False, 'Real USB test cannot use synthetic journal')
                except BaseException as exc:
                    error = error or 'Evidence close: ' + repr(exc)
            try:
                report['interface_after'] = can_counters()
                report['zero_tx'] = verify_zero_tx(before, report['interface_after'])
                storage.verify_usb(cfg)
            except BaseException as exc:
                error = error or 'Read-only final verification: ' + repr(exc)
            report.update(status='REAL_USB_STORAGE_PRESSURE_COMPLETE_NOT_MOTION_ACCEPTANCE' if error is None else 'FAILED_RETAINED',
                          error=error, actual_total_wall_s=time.monotonic()-started)
            try:
                journal.write_json(output/'storage-pressure.json', report)
            finally:
                journal.close()
        print(json.dumps({'status': report['status'], 'run_dir': str(output), 'error': error,
                          'scope': SCOPE, 'model_calls': 0, 'robot_commands_sent': 0}, ensure_ascii=False))
        return 0 if error is None else 1


if __name__ == '__main__':
    raise SystemExit(main())
