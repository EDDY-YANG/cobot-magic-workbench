"""Single HTTP sampler of the already-running Hub; no device reader exists.

The control thread only reads a copied RAM snapshot. CAN source timestamps,
component revision, values and provenance remain exactly those returned by
ExistingHub.feedback(). Repeated/old revisions are not fabricated or removed.
"""
from collections import deque
from copy import deepcopy
import math
import threading
import time


class FeedbackCacheError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise FeedbackCacheError(message)


def statistics(values):
    ordered = sorted(values)
    if not ordered:
        return {'n': 0}
    def percentile(fraction):
        index = (len(ordered)-1)*fraction
        lo = int(math.floor(index)); hi = int(math.ceil(index))
        return ordered[lo]+(ordered[hi]-ordered[lo])*(index-lo)
    return {'n': len(ordered), 'p50': percentile(.5), 'p95': percentile(.95), 'max': ordered[-1]}


class FeedbackCache:
    """One sampler, fixed20ms target cadence, no concurrent/catch-up requests.

    Start after the owned recorder becomes ready. Close before stopping the
    recording or releasing resource locks. ExistingHub.feedback owns and closes
    its bounded HTTP call; close() waits for that call to actually return. A
    close timeout never declares the worker closed and may be retried while the
    caller retains ownership. No private socket from another owner is closed.
    """
    def __init__(self, hub, *, period_s=.02, timing_capacity=4096):
        require(callable(getattr(hub, 'feedback', None)), 'Existing Hub feedback callable required')
        require(type(period_s) in (int, float) and period_s == .02, 'Current sampler target is exactly50Hz')
        require(type(timing_capacity) is int and 8 <= timing_capacity <= 16384, 'Bounded timing capacity required')
        self.hub = hub
        self.period_ns = 20_000_000
        self.lock = threading.Lock()
        self.quit = threading.Event()
        self.ready = threading.Event()
        self.done = threading.Event()
        self.thread = None
        self._latest = None
        self.failure = None
        self.polls = self.completed_polls = self.missed_slots = 0
        self.published_monotonic_ns = None
        self.last_start_ns = None
        self.intervals_ms = deque(maxlen=timing_capacity)
        self.http_ms = deque(maxlen=timing_capacity)
        self.close_timeouts = 0

    def start(self):
        with self.lock:
            require(self.thread is None and not self.quit.is_set(), 'Feedback cache cannot restart')
            self.thread = threading.Thread(target=self._run, name='v2-existing-hub-feedback-cache', daemon=False)
            self.thread.start()
        return self

    def _run(self):
        scheduled = time.monotonic_ns()
        try:
            while not self.quit.is_set():
                delay = max(0., (scheduled-time.monotonic_ns())/1e9)
                if self.quit.wait(delay):
                    break
                started = time.monotonic_ns()
                with self.lock:
                    self.polls += 1
                    if self.last_start_ns is not None:
                        self.intervals_ms.append((started-self.last_start_ns)/1e6)
                    self.last_start_ns = started
                try:
                    # Only this thread calls feedback(). It uses the existing
                    # Hub's HTTP cache; no camera/CAN device is opened here.
                    value = self.hub.feedback()
                    require(isinstance(value, dict), 'Existing Hub feedback object required')
                    frozen = deepcopy(value)
                    published = time.monotonic_ns()
                    with self.lock:
                        self.completed_polls += 1
                        self._latest = frozen
                        self.published_monotonic_ns = published
                    self.ready.set()
                finally:
                    finished = time.monotonic_ns()
                    with self.lock:
                        self.http_ms.append((finished-started)/1e6)
                scheduled += self.period_ns
                if finished >= scheduled:
                    skipped = (finished-scheduled)//self.period_ns+1
                    scheduled += skipped*self.period_ns
                    with self.lock:
                        self.missed_slots += int(skipped)
        except BaseException as exc:
            with self.lock:
                if self.failure is None:
                    self.failure = {'type': type(exc).__name__, 'message': str(exc)[:500],
                                    'monotonic_ns': time.monotonic_ns()}
            self.ready.set()
        finally:
            self.done.set()

    def check_alive(self):
        with self.lock:
            require(self.thread is not None, 'Feedback cache has not started')
            require(self.failure is None, 'Feedback cache background failure: '+str(self.failure))
            require(not self.quit.is_set() and not self.done.is_set(), 'Feedback cache is stopped/closing')

    def wait_ready(self, timeout_s=3., *, stop_event=None):
        require(type(timeout_s) in (int, float) and math.isfinite(timeout_s) and 0 < timeout_s <= 10.,
                'Bounded startup wait required')
        deadline = time.monotonic()+timeout_s
        while True:
            if stop_event is not None and stop_event.is_set():
                raise FeedbackCacheError('Stopped while awaiting first feedback')
            self.check_alive()
            if self.ready.is_set():
                return self.read_latest()
            remaining = deadline-time.monotonic()
            require(remaining > 0, 'No validated feedback before startup deadline')
            self.ready.wait(min(.02, remaining))

    def _snapshot_reference(self):
        with self.lock:
            require(self.thread is not None, 'Feedback cache has not started')
            require(self.failure is None, 'Feedback cache background failure: '+str(self.failure))
            require(not self.quit.is_set() and not self.done.is_set(), 'Feedback cache is stopped/closing')
            require(self._latest is not None, 'First validated feedback not available')
            snapshot = self._latest
        return snapshot

    def read_latest(self):
        """RAM only. Return an owned copy; consumers may adjust age fields."""
        snapshot = self._snapshot_reference()
        # Snapshot object is never mutated by the sampler after publication.
        # Copy outside the short mutex; readers cannot mutate the cached object.
        return deepcopy(snapshot)

    def read_control(self):
        """Copy only the unchanged values/timestamps consumed by core.tick().

        The full SDK/status/raw-CAN feedback remains available to the writer's
        existing guards through read_latest(). No freshness or identity check
        is replaced by this small projection.
        """
        snapshot = self._snapshot_reference()
        return deepcopy({key: snapshot[key] for key in
                         ('q_rad_m', 'source_ns', 'source_ns_by_component')})

    def close(self, timeout_s=3.):
        require(type(timeout_s) in (int, float) and math.isfinite(timeout_s) and 0 < timeout_s <= 10.,
                'Bounded close wait required')
        self.quit.set()
        worker = self.thread
        if worker is not None:
            require(threading.current_thread() is not worker, 'Sampler cannot join itself')
            worker.join(timeout_s)
            if worker.is_alive():
                with self.lock:
                    self.close_timeouts += 1
                raise FeedbackCacheError('Feedback HTTP sampler has not drained; retain owner and retry close')
        with self.lock:
            return {'stopped': True, 'http_worker_drained': True,
                    'sampler_threads': 0 if worker is None else 1,
                    'polls': self.polls, 'completed_polls': self.completed_polls,
                    'failure': deepcopy(self.failure), 'healthy': self.failure is None,
                    'close_timeouts': self.close_timeouts, 'missed_sample_slots': self.missed_slots,
                    'target_hz': 50, 'http_duration_ms': statistics(list(self.http_ms)),
                    'sample_interval_ms': statistics(list(self.intervals_ms)),
                    'timing_ring_capacity': self.http_ms.maxlen,
                    'last_publication_monotonic_ns': self.published_monotonic_ns,
                    'source_timestamps_rewritten': False, 'device_readers_created': 0,
                    'robot_commands': 0}
