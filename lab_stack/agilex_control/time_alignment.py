"""Pure, bounded selector for causal sensor timestamp selection.

The runtime_alignment bridge connects this pure selector to existing producers;
this module itself performs no I/O. It cannot certify exposure synchronization.
Timestamps must already
belong to one explicit host/boot/session/monotonic clock domain; do not fabricate
an available time for old recordings to claim runtime causality.

Payloads are retained BY REFERENCE, never copied, interpolated or synthesized.
The producer must keep each payload immutable until the selected consumer has
released it. The frozen envelopes only protect metadata, not mutable payloads.
The owner must serialize add/select calls (this class is not thread-safe).

Run the offline fixture tests from the project root, with no devices involved:
# & 'C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python312\\python.exe' -X utf8 -B -m unittest discover -s tests -p 'test_2026_10_12_18_24_time_alignment.py' -v
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


STREAMS = ("front", "right", "q12", "q34", "q56", "gripper")
STATE_STREAMS = ("q12", "q34", "q56", "gripper")
CAMERA_STREAMS = ("front", "right")
NS_PER_MS = 1_000_000


def _is_ns(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class ClockDomain:
    """Identity of one monotonic timeline, not a wall-clock offset guess."""

    host_id: str
    boot_id: str
    session_id: str
    clock_name: str = "perf_counter_ns"

    def __post_init__(self):
        for name in ("host_id", "boot_id", "session_id", "clock_name"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError("ClockDomain requires a nonempty " + name)


@dataclass(frozen=True)
class StreamSample:
    """Unmodified source and first-availability times plus original payload."""

    sample_id: str
    source_ns: int
    available_ns: int
    clock_domain: ClockDomain
    payload: Any = field(compare=False, repr=False)
    source_kind: str = "software_source"


@dataclass(frozen=True)
class OfflineAlignmentPolicy:
    """Explicit OFFLINE candidates from the old training contract, not CAN limits.

    max_sample_age_ns is selection_now - source, not anchor - source. Equality
    is allowed at every threshold. No default inference delay is hidden here.
    """

    max_sample_age_ns: int = 50 * NS_PER_MS
    max_camera_skew_ns: int = 40 * NS_PER_MS
    max_state_skew_ns: int = 10 * NS_PER_MS

    def __post_init__(self):
        for name in ("max_sample_age_ns", "max_camera_skew_ns", "max_state_skew_ns"):
            if not _is_ns(getattr(self, name)):
                raise ValueError(name + " must be a nonnegative integer nanosecond count")


@dataclass(frozen=True)
class AlignmentFailure:
    code: str
    streams: Tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    code: str
    evicted_sample_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AlignmentResult:
    ready: bool
    anchor_ns: int
    selection_now_ns: int
    clock_domain: ClockDomain
    samples: Dict[str, StreamSample]
    anchor_age_ns: Dict[str, int]
    selection_age_ns: Dict[str, int]
    camera_skew_ns: Optional[int]
    state_skew_ns: Optional[int]
    policy: OfflineAlignmentPolicy
    failures: Tuple[AlignmentFailure, ...]

    def receipt(self):
        """Small JSON-serializable timing receipt; never includes sensor payloads."""
        return {
            "ready": self.ready,
            "offline_candidate_only": True,
            "hardware_exposure_synchronization_verified": False,
            "payload_validation": "not_assessed_by_timestamp_selector",
            "physical_deployment_ready": False,
            "offline_policy": {"age_reference": "selection_now_minus_source",
                               "max_sample_age_ns": self.policy.max_sample_age_ns,
                               "max_camera_skew_ns": self.policy.max_camera_skew_ns,
                               "max_state_skew_ns": self.policy.max_state_skew_ns},
            "anchor_ns": self.anchor_ns if _is_ns(self.anchor_ns) else None,
            "selection_now_ns": self.selection_now_ns if _is_ns(self.selection_now_ns) else None,
            "time_inputs_valid": _is_ns(self.anchor_ns) and _is_ns(self.selection_now_ns),
            "clock_domain": {"host_id": self.clock_domain.host_id,
                             "boot_id": self.clock_domain.boot_id,
                             "session_id": self.clock_domain.session_id,
                             "clock_name": self.clock_domain.clock_name},
            "samples": {
                stream: {"sample_id": sample.sample_id, "source_ns": sample.source_ns,
                         "available_ns": sample.available_ns, "source_kind": sample.source_kind}
                for stream, sample in self.samples.items()
            },
            "anchor_age_ns": dict(self.anchor_age_ns),
            "selection_age_ns": dict(self.selection_age_ns),
            "camera_skew_ns": self.camera_skew_ns,
            "state_skew_ns": self.state_skew_ns,
            "overall_source_skew_ns": (max(sample.source_ns for sample in self.samples.values()) -
                                       min(sample.source_ns for sample in self.samples.values())) if self.samples else None,
            "camera_state_max_skew_ns": max((abs(self.samples[camera].source_ns - self.samples[state].source_ns)
                for camera in CAMERA_STREAMS for state in STATE_STREAMS), default=None) if self.samples else None,
            "failures": [{"code": failure.code, "streams": failure.streams,
                          "detail": failure.detail} for failure in self.failures],
        }


class TimeAlignmentBuffer:
    """Six bounded histories; deterministic latest coherent causal selection.

    Late arrival is allowed: histories are ordered by ORIGINAL source time.
    Overflow evicts the oldest source, even if that sample just arrived. Exact
    duplicate IDs do not update first availability. Identity conflicts and
    clock-domain conflicts latch a blocker; create a new buffer for a new boot
    or session rather than continuing with old data. Dedupe covers retained
    history; an evicted old frame keeps its old source time if seen again.
    """

    def __init__(self, clock_domain, capacity_per_stream=32, policy=None):
        if not isinstance(clock_domain, ClockDomain):
            raise TypeError("clock_domain must be ClockDomain")
        if not isinstance(capacity_per_stream, int) or isinstance(capacity_per_stream, bool) or capacity_per_stream < 1:
            raise ValueError("capacity_per_stream must be a positive integer")
        self.clock_domain = clock_domain
        self.capacity_per_stream = capacity_per_stream
        self.policy = policy if policy is not None else OfflineAlignmentPolicy()
        if not isinstance(self.policy, OfflineAlignmentPolicy):
            raise TypeError("policy must be OfflineAlignmentPolicy")
        self._history = {stream: [] for stream in STREAMS}
        self._highest_source = {stream: None for stream in STREAMS}
        self._stats = Counter()
        self._latched = {}
        self._last_anchor = None
        self._last_now = None

    @staticmethod
    def _sample_key(sample):
        # Source first; earliest genuine availability wins equal-source ties.
        return (sample.source_ns, -sample.available_ns, sample.sample_id)

    def _reject(self, code, streams, detail, latch=False):
        self._stats[code] += 1
        if latch:
            self._latched[code] = AlignmentFailure(code, tuple(streams), detail)
        return IngestResult(False, code)

    def add(self, stream, sample):
        if stream not in self._history:
            return self._reject("UNKNOWN_STREAM", (str(stream),), "Only six registered real sensor streams are accepted")
        if not isinstance(sample, StreamSample):
            return self._reject("INVALID_SAMPLE", (stream,), "Expected a StreamSample envelope")
        if sample.clock_domain != self.clock_domain:
            return self._reject("MULTIPLE_CLOCK_DOMAINS", (stream,), "Different host/boot/session/clock; restart the buffer", True)
        if not _is_ns(sample.source_ns) or not _is_ns(sample.available_ns) or sample.source_ns > sample.available_ns:
            return self._reject("INVALID_SOURCE_AVAILABLE_ORDER", (stream,), "Require integer 0 <= source_ns <= available_ns", True)
        if not isinstance(sample.sample_id, str) or not sample.sample_id or not isinstance(sample.source_kind, str) or not sample.source_kind:
            return self._reject("INVALID_SAMPLE_ID_OR_SOURCE_KIND", (stream,), "Identity and timestamp provenance are required", True)
        history = self._history[stream]
        for old in history:
            if old.sample_id == sample.sample_id:
                if (old.source_ns, old.available_ns, old.source_kind) != (sample.source_ns, sample.available_ns, sample.source_kind) or old.payload is not sample.payload:
                    return self._reject("DUPLICATE_ID_CHANGED", (stream,), "A retained source identity cannot be restamped or replaced", True)
                return self._reject("DUPLICATE_UNCHANGED", (stream,), "Duplicate does not refresh source or availability")
            if old.source_ns == sample.source_ns:
                return self._reject("SOURCE_TIMESTAMP_COLLISION", (stream,), "Distinct identities claim one source timestamp", True)
        highest = self._highest_source[stream]
        if highest is not None and sample.source_ns < highest:
            self._stats["OUT_OF_ORDER"] += 1
        self._highest_source[stream] = max(sample.source_ns, highest if highest is not None else sample.source_ns)
        history.append(sample)
        history.sort(key=self._sample_key)
        evicted = tuple(item.sample_id for item in history[:-self.capacity_per_stream])
        if evicted:
            del history[:-self.capacity_per_stream]
            self._stats["OVERFLOW_EVICTED"] += len(evicted)
        retained = any(item is sample for item in history)
        self._stats["RETAINED" if retained else "LATE_SAMPLE_EVICTED"] += 1
        return IngestResult(retained, "RETAINED" if retained else "LATE_SAMPLE_EVICTED", evicted)

    def diagnostics(self):
        return {"counts": dict(self._stats), "sizes": {key: len(value) for key, value in self._history.items()},
                "capacity_per_stream": self.capacity_per_stream, "latched_codes": sorted(self._latched)}

    @classmethod
    def _coherent_group(cls, eligible, streams, max_skew_ns):
        # Sweep possible oldest-source boundaries, newest first. At a boundary,
        # the newest sample in its window is optimal for each stream. This avoids
        # a four-way Cartesian product while finding older valid alternatives.
        boundaries = sorted({sample.source_ns for stream in streams for sample in eligible[stream]}, reverse=True)
        for lower in boundaries:
            chosen = {}
            for stream in streams:
                candidates = [sample for sample in eligible[stream] if lower <= sample.source_ns <= lower + max_skew_ns]
                if not candidates:
                    break
                chosen[stream] = max(candidates, key=cls._sample_key)
            if len(chosen) == len(streams):
                return chosen
        return None

    def select(self, anchor_ns, selection_now_ns, clock_domain):
        """Select original samples only; supplied anchor is never silently moved.

        A rejected result returns no assembled samples. Failure details and
        rejection counters remain available; failed inputs are never upgraded
        to sensor success. Repeating equal snapshot times is allowed.
        """
        failures = list(self._latched.values())
        if clock_domain != self.clock_domain:
            failures.append(AlignmentFailure("MULTIPLE_CLOCK_DOMAINS", STREAMS, "Selection clock differs from buffer clock"))
        valid_times = _is_ns(anchor_ns) and _is_ns(selection_now_ns)
        if not valid_times or anchor_ns > selection_now_ns:
            failures.append(AlignmentFailure("INVALID_SNAPSHOT_TIME", STREAMS, "Require integer 0 <= anchor <= selection_now"))
        elif ((self._last_anchor is not None and anchor_ns < self._last_anchor)
              or (self._last_now is not None and selection_now_ns < self._last_now)):
            failures.append(AlignmentFailure("SNAPSHOT_TIME_REVERSED", STREAMS, "Anchor and selection time must not move backwards"))
        if failures:
            return self._result(anchor_ns, selection_now_ns, {}, failures)
        self._last_anchor, self._last_now = anchor_ns, selection_now_ns
        eligible = {}
        for stream, history in self._history.items():
            eligible[stream] = [sample for sample in history if sample.source_ns <= anchor_ns
                                and sample.available_ns <= selection_now_ns
                                and selection_now_ns - sample.source_ns <= self.policy.max_sample_age_ns]
            if not eligible[stream]:
                if not history:
                    code, detail = "MISSING_STREAM", "No real sample was received"
                else:
                    future_source = sum(sample.source_ns > anchor_ns for sample in history)
                    future_available = sum(sample.available_ns > selection_now_ns for sample in history)
                    stale = sum(selection_now_ns - sample.source_ns > self.policy.max_sample_age_ns for sample in history)
                    code = "NO_CAUSAL_FRESH_SAMPLE"
                    detail = "rejected source_after_anchor=%d, available_after_selection=%d, stale=%d" % (future_source, future_available, stale)
                failures.append(AlignmentFailure(code, (stream,), detail))
        if failures:
            return self._result(anchor_ns, selection_now_ns, {}, failures)
        selected = {}
        for streams, max_skew, code in ((CAMERA_STREAMS, self.policy.max_camera_skew_ns, "CAMERA_SKEW_EXCEEDED"),
                                         (STATE_STREAMS, self.policy.max_state_skew_ns, "STATE_COMPONENT_SKEW_EXCEEDED")):
            group = self._coherent_group(eligible, streams, max_skew)
            if group is None:
                failures.append(AlignmentFailure(code, streams, "No original-sample combination fits the offline skew window"))
            else:
                selected.update(group)
        return self._result(anchor_ns, selection_now_ns, {} if failures else selected, failures)

    def _result(self, anchor, now, selected, failures):
        def skew(streams):
            values = [selected[stream].source_ns for stream in streams]
            return max(values) - min(values)
        return AlignmentResult(not failures, anchor, now, self.clock_domain, dict(selected),
                               {stream: anchor - sample.source_ns for stream, sample in selected.items()},
                               {stream: now - sample.source_ns for stream, sample in selected.items()},
                               skew(CAMERA_STREAMS) if selected else None,
                               skew(STATE_STREAMS) if selected else None, self.policy, tuple(failures))
