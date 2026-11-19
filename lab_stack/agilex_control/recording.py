# 完整单行命令：Windows PC PowerShell；无需预定义变量，复制时去掉行首 #。
# 按需选择一条命令，不要把所有示例一起执行。
# # 录制模块，要求共享 hub 已运行；不直接运行本文件。
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' record-start --task 'right-arm red-block pick and place'
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' record-stop
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' record-list
# # record-stop 停止录制，不停止机械臂。
# 本轮仅补充命令注释；未运行测试、SSH、模型、相机或机械臂。
"""Bounded asynchronous capture of real sensor samples into USB chunk files.

Native capture is not relabelled as a LeRobot dataset. A sample retains each
camera's own software timestamp, actual feedback age and observed command age.
Missing action targets remain missing; no past/future interpolation is hidden.
"""
import json
import copy
import math
import os
from pathlib import Path
import queue
import struct
import threading
import hashlib
import time
from .storage import Session, verify_usb

from .recording_lifecycle import OWNED_RECORDING_STOP_PROTOCOL
from .capture_profile import contract, ALL, DAGGER
_UNSPECIFIED_STOP_OWNER = object()
RECORDING_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class CaptureQueue(queue.Queue):
    """Bound both queued samples and conservative payload accounting."""
    def __init__(self, maxsize=600, byte_limit=128 * 1024 * 1024):
        super().__init__(maxsize=maxsize)
        self.byte_limit = byte_limit
        self.queued_bytes = 0
        self.peak_bytes = 0
        self.peak_samples = 0
        self.max_wait_s = 0.0

    @staticmethod
    def cost(value):
        if isinstance(value, bytes):
            return len(value) + 64
        if isinstance(value, str):
            return len(value.encode("utf-8")) + 64
        if isinstance(value, dict):
            return 128 + sum(CaptureQueue.cost(k) + CaptureQueue.cost(v) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return 64 + sum(CaptureQueue.cost(v) for v in value)
        return 32

    def put(self, item, block=True, timeout=None):
        size = 0 if item is None else self.cost(item)
        if size > self.byte_limit:
            raise queue.Full("Capture sample exceeds byte budget")
        with self.not_full:
            deadline = None if timeout is None else time.monotonic() + timeout
            while self._qsize() >= self.maxsize or self.queued_bytes + size > self.byte_limit:
                if not block:
                    raise queue.Full
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Full
                self.not_full.wait(remaining)
            self._put((item, size, time.monotonic()))
            self.queued_bytes += size
            self.peak_bytes = max(self.peak_bytes, self.queued_bytes)
            self.peak_samples = max(self.peak_samples, self._qsize())
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def _get(self):
        item, size, enqueued = super()._get()
        self.queued_bytes -= size
        self.max_wait_s = max(self.max_wait_s, time.monotonic() - enqueued)
        return item

    def metrics(self):
        with self.mutex:
            return {"sample_limit": self.maxsize, "byte_limit": self.byte_limit,
                    "queued_bytes": self.queued_bytes, "peak_bytes": self.peak_bytes,
                    "peak_samples": self.peak_samples, "max_wait_s": self.max_wait_s,
                    "accounting": "payload plus conservative object allowance; not process RSS"}


class ChunkFinalizer:
    """One background owner flushes and closes detached chunk handles."""
    def __init__(self, max_pending=8):
        self.queue = queue.Queue()
        self.slots = threading.BoundedSemaphore(max_pending)
        self.lock = threading.Lock()
        self.pending = self.submitted = self.completed = 0
        self.peak_pending = 0
        self.max_flush_s = 0.0
        self.errors = []
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="usb-chunk-finalizer", daemon=False)
        self.thread.start()

    def submit(self, handle, chunk_name=None):
        # Backpressure stays on the writer; the sampler retains its own bounded queue.
        self.slots.acquire()
        with self.lock:
            self.pending += 1
            self.submitted += 1
            self.peak_pending = max(self.peak_pending, self.pending)
        self.queue.put((handle, chunk_name))

    def _run(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            handle, chunk_name = item
            began = time.monotonic()
            error = None
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except Exception as exc:
                error = repr(exc)
            finally:
                try:
                    handle.close()
                except Exception as exc:
                    error = (error + "; " if error else "") + repr(exc)
                with self.lock:
                    self.max_flush_s = max(self.max_flush_s, time.monotonic() - began)
                    if error and len(self.errors) < 16:
                        self.errors.append({"chunk": str(chunk_name or getattr(handle, "name", "fd")),
                                            "error": error})
                    self.pending -= 1
                    self.completed += 1
                self.slots.release()

    def check(self):
        with self.lock:
            if self.errors:
                raise RuntimeError("USB chunk finalization failed: " + repr(self.errors))

    def finish(self):
        if not self.closed:
            self.closed = True
            self.queue.put(None)
        self.thread.join()
        self.check()

    def metrics(self):
        with self.lock:
            return {"submitted": self.submitted, "completed": self.completed,
                    "pending": self.pending, "peak_pending": self.peak_pending,
                    "max_flush_s": self.max_flush_s, "errors": list(self.errors)}


class ChunkStream:
    def __init__(self, root, name, maximum=512 * 1024 * 1024, *, storage_guard=None, finalizer=None):
        self.root, self.name, self.maximum = Path(root), name, maximum
        self.storage_guard = storage_guard
        self.finalizer = finalizer
        if self.storage_guard is not None:
            self.storage_guard()
        if self.root.is_symlink():
            raise RuntimeError('Raw stream directory must not be a symlink')
        self.root_device = self.root.stat().st_dev
        self.handle, self.size, self.part = None, 0, 0

    def _open_part(self):
        # Recheck before every new file, including rollover. Existing descriptors
        # stay bound to the original filesystem; no bare mount-directory fallback.
        if self.storage_guard is not None:
            self.storage_guard()
        if self.root.is_symlink() or self.root.stat().st_dev != self.root_device:
            raise RuntimeError('Recording filesystem changed; refusing stream rollover')
        name = f'{self.name}-{self.part:04d}.bin'
        if os.open in os.supports_dir_fd:
            directory = os.open(self.root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
            try:
                if os.fstat(directory).st_dev != self.root_device:
                    raise RuntimeError('Recording directory moved to another filesystem')
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=directory)
                try:
                    if os.fstat(fd).st_dev != self.root_device:
                        raise RuntimeError('Raw stream file is outside the frozen recording filesystem')
                    handle = os.fdopen(fd, 'wb')
                except BaseException:
                    os.close(fd)
                    raise
            finally:
                os.close(directory)
            return handle
        # Windows offline tools also use this codec. Mounted identity is still
        # checked by the supplied guard; production Linux uses the dir-fd path.
        handle = (self.root / name).open('xb')
        if os.fstat(handle.fileno()).st_dev != self.root_device:
            handle.close()
            raise RuntimeError('Raw stream file is outside the frozen recording filesystem')
        return handle

    def append(self, timestamp_ns, payload):
        size = 12 + len(payload)
        if size > self.maximum:
            raise RuntimeError("Single record exceeds USB chunk limit")
        if self.handle is None or self.size + size > self.maximum:
            self.close()
            self.handle = self._open_part()
            self.part += 1
            self.size = 0
        self.handle.write(struct.pack("<QI", timestamp_ns, len(payload)))
        self.handle.write(payload)
        self.size += size

    def close(self):
        if self.handle:
            handle = self.handle
            self.handle = None
            if self.finalizer is not None:
                self.finalizer.submit(handle, f"{self.name}-{self.part - 1:04d}.bin")
                return
            try:
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()


class Recorder:
    def __init__(self, cfg, snapshot, frames, *, dagger_job_id=None):
        self.cfg, self.snapshot, self.frames = copy.deepcopy(cfg), snapshot, frames
        self.source_cfg = cfg  # Detect device/profile changes without retargeting storage.
        self.dagger_job_id = dagger_job_id
        self.profile_override = DAGGER if dagger_job_id else None
        self.capture_contract = contract(self.cfg, self.profile_override)
        self.arm_roles = self.capture_contract['required_arm_roles']
        self.camera_roles = self.capture_contract['required_camera_roles']
        self.lock = threading.Lock()
        self.session, self.thread, self.sample_thread, self.active = None, None, None, False
        self.error, self.samples, self.overflows = None, 0, 0
        self.queue = CaptureQueue()
        self.can_queue = queue.Queue(maxsize=50000)
        self.can_counts = {}
        self.camera_counts = {}
        self.stop_event = threading.Event()
        self.started_monotonic = self.sampling_ended_monotonic = None
        self.final_receipt_written = False
        self.finalizer = None
        self.max_write_s = 0.0
        self.collection_v1 = None
        self.collection_bound = False
        self.raw_saved_bytes = 0
        self.quality_summary = {}
        self._accepted_frame_lock = threading.Lock()
        self._native_frame_cache = None
        self._can_enqueue_lock = threading.Lock()
        self._can_kernel_counts, self._can_legacy_counts = {}, {}
        self._can_timing_errors = 0

    def _enqueue_capture(self, item):
        """Publish only original frames already accepted by the native queue.

        JPEG bytes are immutable and retained unchanged. Copy metadata, never
        RGB arrays or an extra camera read. The single-slot private cache is
        replaced atomically and consumers receive independent metadata copies.
        Queue acceptance is not a disk-persistence claim.
        """
        images = {role: {key: copy.deepcopy(value) for key, value in frame.items() if key != 'rgb'}
                  for role, frame in item['images'].items()}
        queued = dict(item)
        queued['images'] = images
        cached_images = copy.deepcopy(images)
        with self._accepted_frame_lock:
            if not self.active or self.stop_event.is_set() or self.error or self.session is None:
                raise RuntimeError('Native recording is no longer accepting sensor samples')
            self.queue.put_nowait(queued)
            self._native_frame_cache = {
                'episode_path': str(self.session.path), 'sample_wall_ns': item['sample_wall_ns'],
                'accepted_monotonic_ns': time.monotonic_ns(), 'frames': cached_images}

    def latest_native_frames(self, expected_path):
        """Return latest native-accepted originals for this active episode only."""
        with self._accepted_frame_lock:
            current = None if self.session is None else str(self.session.path)
            cached = self._native_frame_cache
            if (not self.active or self.stop_event.is_set() or self.error or self.overflows or
                    current != expected_path or cached is None or cached['episode_path'] != expected_path):
                raise RuntimeError('No healthy native-accepted frame cache for the active recording episode')
            return copy.deepcopy(cached)

    def _capture(self):
        """Reject missing/stale configured inputs; never synthesize a sample."""
        if not self.arm_roles or not self.camera_roles:
            raise RuntimeError('Recording requires registered arm and camera inputs')
        if (contract(self.cfg, self.profile_override) != self.capture_contract or
                contract(getattr(self, 'source_cfg', self.cfg), self.profile_override) != self.capture_contract):
            raise RuntimeError('Capture profile/devices changed; restart the sensor service before recording')
        images, sensors = self.frames(), self.snapshot()
        sample_monotonic_ns = time.monotonic_ns()
        wall_ns = time.time_ns()
        errors = []
        for role in self.arm_roles:
            entry = self.cfg['can'][role]
            arm = sensors.get("arms", {}).get(role, {})
            q = arm.get("q_deg")
            ages = [arm.get(key) for key in ("age_joint_s", "age_status_s", "age_gripper_s")]
            finite = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            if (arm.get("feedback_fresh") is not True or arm.get("observer_running") is not True
                    or arm.get("observer_error") or not isinstance(q, (list, tuple)) or len(q) != 6
                    or not all(finite(value) for value in q) or not finite(arm.get("grip_m"))
                    or not arm.get("status_hex")
                    or not all(finite(age) and 0 <= age <= entry.get("stale_after", .2) for age in ages)):
                errors.append("arm %s feedback missing/stale (ages=%r, error=%r)" % (role, ages, arm.get("observer_error")))
        for role in self.camera_roles:
            status = sensors.get("camera_status", {}).get(role, {})
            frame = images.get(role)
            age = status.get("age_s")
            if (status.get("fresh") is not True or status.get("thread_alive") is not True
                    or status.get("error") or not isinstance(age, (int, float)) or not math.isfinite(age)
                    or not 0 <= age <= .5 or not isinstance(frame, dict)):
                errors.append("camera %s missing/stale (age=%r, error=%r)" % (role, age, status.get("error")))
                continue
            stamp, jpeg = frame.get("wall_ns"), frame.get("jpeg")
            if (not isinstance(stamp, int) or isinstance(stamp, bool) or not 0 < stamp <= wall_ns
                    or wall_ns - stamp > 500_000_000 or not isinstance(jpeg, bytes) or not jpeg):
                errors.append("camera %s has no fresh nonempty JPEG/timestamp" % role)
        if errors:
            raise RuntimeError("Recording inputs not ready: " + "; ".join(errors))
        sensors = copy.deepcopy(sensors)
        sensors['arms'] = {role: sensors['arms'][role] for role in self.arm_roles}
        sensors['camera_status'] = {role: sensors['camera_status'][role] for role in self.camera_roles}
        images = {role: images[role] for role in self.camera_roles}
        item = {"sample_wall_ns": wall_ns, "sensors": copy.deepcopy(sensors),
                "images": {role: dict(frame) if isinstance(frame, dict) else frame for role, frame in images.items()}}
        # Preserve metadata at capture, before the asynchronous writer. Older
        # producers are left legacy; no read/write time becomes first available.
        if any(isinstance(frame, dict) and 'source_timing' in frame for frame in images.values()):
            item['camera_source_index_v1'] = {
                'schema': 'tiger-recorded-camera-source-index-v1',
                'sample_monotonic_ns': sample_monotonic_ns,
                'clock_domain': copy.deepcopy(sensors.get('clock_domain')),
                'cameras': {role: {key: copy.deepcopy(frame.get(key)) for key in
                    ('wall_ns', 'frame_id', 'source_timing', 'available_monotonic_ns')}
                    for role, frame in images.items() if isinstance(frame, dict)}}
        return item

    def start(self, task, collection_v1=None, on_created=None):
        with self.lock:
            if collection_v1 is not None:
                from .collection import validate_metadata
                collection_v1 = validate_metadata(collection_v1)
                if collection_v1.get('capture_profile', ALL) != self.capture_contract['profile']:
                    raise ValueError('Frozen batch capture profile differs from current recorder')
                if task != collection_v1['task']:
                    raise ValueError('Task differs from frozen collection metadata')
                if self.collection_v1 == collection_v1 and self.session is not None:
                    if not self.collection_bound:
                        if on_created is None or self.active:
                            raise RuntimeError('Unbound trial cannot be replayed without verified identity binding')
                        if not self.final_receipt_written:
                            self.error = self.error or 'Previous trial initialization did not complete; no capture started'
                            self.sampling_ended_monotonic = time.monotonic()
                            self.session.finish('RECORDING_FAILED',samples=0,camera_frame_counts={},raw_CAN_counts={},
                                queue_overflows=0,failure=self.error,quality_summary=copy.deepcopy(self.quality_summary))
                            self.final_receipt_written = True
                        on_created(str(self.session.path))
                        self.collection_bound = True
                    return {**self.status(), 'start_idempotent_replay':True}
            if self.active or any(thread is not None and thread.is_alive() for thread in (self.thread, self.sample_thread)):
                raise RuntimeError("Recording already active or previous writer/producer still stopping")
            if not isinstance(task, str) or not task.strip():
                raise ValueError("Recording task must be nonempty text")
            # Check before creating an episode directory; enumerated devices
            # and cached stale state do not constitute recording readiness.
            self._capture()
            if collection_v1 is None:
                self.session = Session(self.cfg, "episode")
            else:
                self.session = Session(self.cfg, "episode", episode_label=
                    f"{collection_v1['ordinal']:06d}-{collection_v1['category']}")
            self.error, self.samples, self.overflows = None, 0, 0
            self.can_counts, self.camera_counts = {}, {}
            self.collection_v1 = copy.deepcopy(collection_v1)
            self.collection_bound = False
            self.started_monotonic = time.monotonic()
            self.sampling_ended_monotonic = None
            self.final_receipt_written = False
            self.raw_saved_bytes = 0
            self.finalizer = None
            self.max_write_s = 0.0
            with self._can_enqueue_lock:
                self._can_kernel_counts, self._can_legacy_counts = {}, {}
                self._can_timing_errors = 0
            self.quality_summary = {'schema':'tiger-recording-quality-v1',
                'scope':'stored raw CAN messages; zero counts do not prove physical success',
                'gripper_fault_samples':0, 'gripper_unknown_samples':0,
                'gripper_messages_by_role':{role:0 for role in self.arm_roles}, 'joint_fault_messages':0,
                'arm_error_messages':0,
                'coordinate_pending':None}
            self.session.report.update(task=task, format="tiger-native-sensors-v1",
                requested_sample_hz=30, rerun_display_hz=5, hardware_exposure_timestamp_available=False,
                chunk_header="little-endian uint64 wall_timestamp_ns, uint32 payload_length; raw CAN uses original SO_TIMESTAMPNS kernel receipt",
                raw_CAN_timing=self.can_timing_snapshot(),
                channels="profile-scoped real JPEG/state/raw CAN; excluded channels remain absent",
                capture_profile=self.capture_contract['profile'],
                capture_contract=copy.deepcopy(self.capture_contract),
                required_camera_roles=list(self.camera_roles), required_arm_roles=list(self.arm_roles),
                optional_camera_source_index_schema="tiger-recorded-camera-source-index-v1",
                source_timing_scope="producer metadata only; missing fields remain unknown; no common selection",
                training_dataset_verified=False)
            if self.dagger_job_id:
                self.session.report['dagger'] = {
                    'policy_job_id': self.dagger_job_id,
                    'segment': 'correction_candidate', 'action_source': 'oem_bus_target',
                    'expert_labels_verified': False,
                    'sender_authenticated': False, 'physical_task_success': 'unreviewed'}
            if collection_v1 is not None:
                self.session.report['collection_v1'] = copy.deepcopy(collection_v1)
            for field in ('capture_coordinate_reference', 'capture_image_reference'):
                if field in self.source_cfg:
                    self.session.report[field] = copy.deepcopy(self.source_cfg[field])
            try:
                from .gripper_calibration import coordinate_review
                if 'remote_bundle' in self.cfg:
                    self.quality_summary['coordinate_pending'] = coordinate_review(self.cfg).get('pending')
                self.session.save()
                if on_created is not None:
                    on_created(str(self.session.path))
            except Exception as exc:
                self.error = 'Collection identity initialization/binding failed before capture: '+repr(exc)
                self.sampling_ended_monotonic = time.monotonic()
                try:
                    self.session.finish('RECORDING_FAILED', samples=0, camera_frame_counts={},raw_CAN_counts={},
                        queue_overflows=0,failure=self.error,quality_summary=copy.deepcopy(self.quality_summary))
                    self.final_receipt_written = True
                except Exception as save_error:
                    self.error += '; failure receipt write: '+repr(save_error)
                raise
            self.collection_bound = True
            with self._accepted_frame_lock:
                self._native_frame_cache = None
            self.active, self.error, self.samples, self.overflows = True, None, 0, 0
            self.queue = CaptureQueue()
            self.can_queue = queue.Queue(maxsize=50000)
            self.can_counts = {}
            self.camera_counts = {}
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._write, name="usb-episode-writer", daemon=False)
            self.sample_thread = threading.Thread(target=self._sample, name="sensor-sample-30Hz", daemon=True)
            try:
                self.thread.start()
                self.sample_thread.start()
            except Exception as exc:
                self.error = 'Recording thread startup failed: '+repr(exc)
                self.sampling_ended_monotonic = time.monotonic()
                self.stop_event.set()
                if self.sample_thread.ident is None:
                    self.sample_thread = None
                if self.thread.ident is None:
                    self.thread = None
                    self.active = False
                    try:
                        self.session.finish('RECORDING_FAILED',samples=0,camera_frame_counts={},raw_CAN_counts={},
                            queue_overflows=0,failure=self.error,quality_summary=copy.deepcopy(self.quality_summary))
                        self.final_receipt_written = True
                    except Exception as save_error:
                        self.error += '; failure receipt write: '+repr(save_error)
                else:
                    # The sampler never started: release the already-running
                    # writer so it records failure and closes its own streams.
                    self.queue.put_nowait(None)
                    self.thread.join(timeout=15)
                raise
            return self.status()

    def enqueue_can(self, role, cid, payload, received_monotonic):
        """Legacy fixture compatibility, never the production Hub CAN route.

        Such records are explicitly marked estimated/mixed and cannot satisfy
        the kernel-timestamp recording contract. There is no fallback to this
        method from enqueue_can_source().
        """
        if role not in self.arm_roles or not self.active or self.stop_event.is_set():
            return
        wall_ns = time.time_ns() - int(max(0., time.monotonic() - received_monotonic) * 1e9)
        self._enqueue_can_record(role, cid, payload, wall_ns, kernel=False)

    def enqueue_can_source(self, role, cid, payload, source_timing):
        """Preserve the exact integer kernel receipt, with no clock conversion.

        SO_TIMESTAMPNS is a host receipt, not a device generation timestamp.
        Real source reversals are kept; the native audit still rejects them.
        """
        if role not in self.arm_roles or not self.active or self.stop_event.is_set():
            return
        stamp = source_timing.get('kernel_wall_ns') if isinstance(source_timing, dict) else None
        if (not isinstance(source_timing, dict)
                or source_timing.get('source_timestamp_kind') != 'linux_so_timestampns_kernel_receipt'
                or type(stamp) is not int or not 0 < stamp < 2 ** 64):
            with self._can_enqueue_lock:
                self._can_timing_errors += 1
            message = 'Raw CAN source timing missing/invalid; exact integer SO_TIMESTAMPNS receipt required'
            self.error = (self.error + '; ' if self.error else '') + message
            self.stop_event.set()
            return
        self._enqueue_can_record(role, cid, payload, stamp, kernel=True)

    def _enqueue_can_record(self, role, cid, payload, stamp, *, kernel):
        try:
            # The short lock orders enqueue and its counters only. Disk writes
            # stay in the existing writer thread and never acquire this lock.
            with self._can_enqueue_lock:
                if not self.active or self.stop_event.is_set():
                    return
                self.can_queue.put_nowait((role, stamp, struct.pack("<I", cid) + payload))
                counts = self._can_kernel_counts if kernel else self._can_legacy_counts
                counts[role] = counts.get(role, 0) + 1
        except queue.Full:
            self.overflows += 1
            self.error = "Raw CAN queue overflow; recording incomplete"
            self.stop_event.set()

    def can_timing_snapshot(self):
        with self._can_enqueue_lock:
            legacy = dict(self._can_legacy_counts)
            exact = not any(legacy.values()) and not self._can_timing_errors
            return {'schema': 'tiger-native-can-kernel-time-v1',
                    'timestamp_basis': ('linux_so_timestampns_kernel_receipt' if exact
                                        else 'legacy_estimated_or_invalid'),
                    'header_preserves_kernel_wall_ns': exact,
                    'kernel_receipt_enqueued_counts': dict(self._can_kernel_counts),
                    'legacy_estimated_enqueued_counts': legacy,
                    'timestamp_error_count': self._can_timing_errors,
                    'hardware_generation_timestamp_available': False}

    def _sample(self):
        next_tick = time.monotonic()
        while not self.stop_event.is_set():
            try:
                # Choose already captured images before stamping this sample.
                # Component timestamps remain unchanged; no future image is
                # assigned to an earlier state sample during export.
                self._enqueue_capture(self._capture())
            except queue.Full:
                self.overflows += 1
                self.error = "Recording queue full; episode incomplete, never silently discarded"
                self.stop_event.set()
            except Exception as exc:
                self.error = repr(exc)
                self.stop_event.set()
            next_tick += 1 / 30
            delay = next_tick - time.monotonic()
            if delay <= 0:
                next_tick = time.monotonic()
            else:
                self.stop_event.wait(delay)
        # A failed disk writer may have exited with a full queue. Do not leave
        # an untracked producer blocked forever or reuse it for a new episode.
        self.sampling_ended_monotonic = time.monotonic()
        # Finish any enqueue admitted before stop before publishing the writer
        # sentinel. New enqueues recheck stop under the same short RAM-only lock.
        with self._can_enqueue_lock:
            pass
        while self.thread is not None and self.thread.is_alive():
            try:
                self.queue.put(None, timeout=.1)
                break
            except queue.Full:
                continue

    def _write(self):
        streams = {}
        self.finalizer = ChunkFinalizer()
        def stream(name):
            if name not in streams:
                streams[name] = ChunkStream(self.session.path, name, storage_guard=lambda: verify_usb(self.cfg), finalizer=self.finalizer)
            return streams[name]
        last_stamp = {}
        check_at = 0
        try:
            while True:
                item = self.queue.get()
                self.finalizer.check()
                write_began = time.monotonic()
                if time.monotonic() >= check_at:
                    verify_usb(self.cfg)
                    check_at = time.monotonic() + 1
                # Drain all remaining CAN only after the sampler has stopped.
                can_budget = None if item is None else 256
                can_written = 0
                while can_budget is None or can_written < can_budget:
                    try:
                        role, stamp, payload = self.can_queue.get_nowait()
                    except queue.Empty:
                        break
                    if role not in self.arm_roles:
                        raise RuntimeError('Raw CAN channel differs from frozen capture profile: ' + str(role))
                    stream("can-"+role).append(stamp,payload)
                    can_written += 1
                    self.raw_saved_bytes += 12+len(payload)
                    self.can_counts[role] = self.can_counts.get(role, 0) + 1
                    cid = struct.unpack('<I',payload[:4])[0] if len(payload)>=4 else None
                    if cid == 0x2A8:
                        counts = self.quality_summary['gripper_messages_by_role']
                        counts[role] = counts.get(role,0)+1
                        if len(payload)!=12:
                            self.quality_summary['gripper_unknown_samples'] += 1
                        else:
                            if payload[11] != 0:
                                self.quality_summary['gripper_unknown_samples'] += 1
                            if payload[10] & 0x3F:
                                self.quality_summary['gripper_fault_samples'] += 1
                    elif cid in range(0x261,0x267) and len(payload)==12 and payload[9] & 0xBF:
                        self.quality_summary['joint_fault_messages'] += 1
                    elif cid == 0x2A1 and len(payload)==12 and int.from_bytes(payload[10:12],'big'):
                        self.quality_summary['arm_error_messages'] += 1
                if item is None:
                    break
                images = item.pop("images")
                if set(images) != set(self.camera_roles):
                    raise RuntimeError('Image channels differ from frozen capture profile')
                for role, frame in images.items():
                    if frame is None:
                        continue
                    stamp, jpg = frame["wall_ns"], frame["jpeg"]
                    item.setdefault("camera_sample_ns", {})[role] = stamp
                    if last_stamp.get(role) == stamp:
                        continue  # Repeated latest-frame sample is indexed, not duplicated on disk.
                    stream(role).append(stamp,jpg)
                    self.raw_saved_bytes += 12+len(jpg)
                    last_stamp[role] = stamp
                    self.camera_counts[role] = self.camera_counts.get(role, 0) + 1
                payload = json.dumps(item, separators=(",", ":"), allow_nan=False).encode()
                stream("state").append(item["sample_wall_ns"],payload)
                self.raw_saved_bytes += 12+len(payload)
                self.samples += 1
                self.max_write_s = max(self.max_write_s, time.monotonic() - write_began)
        except Exception as exc:
            self.error = repr(exc)
            self.stop_event.set()
            # Unblock producer sentinel during disk failure; never call this successful.
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
        finally:
            for stream in streams.values():
                try:
                    stream.close()
                except Exception as exc:
                    self.error = (self.error + "; " if self.error else "") + "Chunk flush failed: " + repr(exc)
            try:
                self.finalizer.finish()
                verify_usb(self.cfg)
            except Exception as exc:
                self.error = (self.error + "; " if self.error else "") + repr(exc)
            if not self.error:
                missing = (["camera " + role for role in self.camera_roles if not self.camera_counts.get(role)]
                           + ["raw CAN " + role for role in self.arm_roles if not self.can_counts.get(role)])
                if not self.samples or missing:
                    self.error = "Empty/incomplete recording: samples=%s; missing channels=%s" % (self.samples, missing)
            try:
                for role in self.arm_roles:
                    if not self.quality_summary.get('gripper_messages_by_role',{}).get(role):
                        self.quality_summary['gripper_unknown_samples'] += 1
                self.session.finish("RECORDED_NATIVE_REVIEW_REQUIRES_QC" if not self.error else "RECORDING_FAILED",
                    samples=self.samples, camera_frame_counts=self.camera_counts, raw_CAN_counts=self.can_counts,
                    raw_CAN_timing=self.can_timing_snapshot(),
                    queue_overflows=self.overflows, failure=self.error,
                    quality_summary=copy.deepcopy(self.quality_summary), raw_saved_bytes=self.raw_saved_bytes,
                    elapsed_s=self.status()['elapsed_s'], fsync_requested=True,
                    recorder_io=self.io_metrics())
                self.final_receipt_written = True
            except Exception as exc:
                self.error = "Evidence finalize failed: " + repr(exc)
            self.active = False

    def stop(self, *, expected_path=_UNSPECIFIED_STOP_OWNER):
        with self.lock:
            owned = expected_path is not _UNSPECIFIED_STOP_OWNER
            current = None if self.session is None else str(self.session.path)
            if owned and (not isinstance(expected_path,str) or not expected_path):
                raise ValueError('Owned recording stop needs a nonempty expected_path')
            if owned and expected_path != current:
                raise RuntimeError('Recording owner/path changed; refusing stop of another episode')
            self.stop_event.set()
            if self.sample_thread:
                self.sample_thread.join(timeout=5)
                if self.sample_thread.is_alive():
                    raise RuntimeError("Sensor producer still stopping; not saved yet")
            if self.thread:
                self.thread.join(timeout=15)
                if self.thread.is_alive():
                    raise RuntimeError("Writer still flushing; not saved yet")
            receipt = self.status()
            if owned:
                receipt.update(stop_owner_verified=True,stopped_expected_path=expected_path)
            return receipt

    def io_metrics(self):
        return {"schema": "tiger-recorder-async-flush-v1",
                "loaded_source_sha256": RECORDING_SOURCE_SHA256,
                "capture_buffer": self.queue.metrics(),
                "can_batch_limit": 256, "max_write_cycle_s": self.max_write_s,
                "chunk_bytes": 512 * 1024 * 1024,
                "finalizer": self.finalizer.metrics() if self.finalizer is not None else None}

    def status(self):
        finished = self.sampling_ended_monotonic
        elapsed = (None if self.started_monotonic is None else
                   max(0.,(time.monotonic() if finished is None else finished)-self.started_monotonic))
        busy = any(t is not None and t.is_alive() for t in (self.thread,self.sample_thread))
        return {"owned_stop_protocol":OWNED_RECORDING_STOP_PROTOCOL,
                "dagger_job_id": self.dagger_job_id,
                "recorder_io": self.io_metrics(),
                "capture_profile": self.capture_contract['profile'],
                "capture_contract": copy.deepcopy(self.capture_contract),
                "active": self.active, "samples": self.samples, "error": self.error,
                "queue_depth": self.queue.qsize(), "overflows": self.overflows,
                "raw_CAN_queue_depth": self.can_queue.qsize(), "raw_CAN_counts": dict(self.can_counts),
                "camera_frame_counts": dict(self.camera_counts),
                "elapsed_s":elapsed, "raw_saved_bytes":self.raw_saved_bytes,
                "saving":self.stop_event.is_set() and busy,
                "finalized":self.final_receipt_written and not busy and not self.active,
                "collection_v1":copy.deepcopy(self.collection_v1),
                "quality_summary":copy.deepcopy(self.quality_summary),
                "status": None if self.session is None else self.session.report.get("status"),
                "path": None if self.session is None else str(self.session.path)}


def iter_chunk(path):
    """Strict portable reader; truncated or oversized records are errors."""
    with Path(path).open("rb") as source:
        while header := source.read(12):
            if len(header) != 12:
                raise ValueError("Truncated chunk header")
            stamp, length = struct.unpack("<QI", header)
            if length > 16 * 1024 * 1024:
                raise ValueError("Oversized chunk record")
            payload = source.read(length)
            if len(payload) != length:
                raise ValueError("Truncated chunk payload")
            yield stamp, payload
