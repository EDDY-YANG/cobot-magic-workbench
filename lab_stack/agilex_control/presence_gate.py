"""Causal, offline-only person/hand pause and re-observation state machine.

This module has no device, network, controller or motion interface. Its permit is
only a replay decision to be plotted against original video PTS. Detection is not
a certified protective sensor, and a clear image does not establish an empty
physical workspace. Re-observation only proves a new image receipt was supplied;
it does not establish standstill, holding, path safety or physical stop success.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import math
import re


SCHEMA = "tiger-presence-replay-gate-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EPS = 1e-9


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _frame_id(value):
    return (_text(value) or type(value) is int and value >= 0)


@dataclass(frozen=True)
class PresenceGateConfig:
    """Engineering replay parameters, not validated protective timing limits."""

    clear_confirm_s: float = 1.0
    max_clear_gap_s: float = 0.25
    max_age_s: float = 0.25
    min_clear_samples: int = 3

    def __post_init__(self):
        for name in ("clear_confirm_s", "max_clear_gap_s"):
            value = getattr(self, name)
            if not _finite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not _finite(self.max_age_s) or self.max_age_s < 0:
            raise ValueError("max_age_s must be finite and nonnegative")
        if type(self.min_clear_samples) is not int or self.min_clear_samples < 2:
            raise ValueError("min_clear_samples must be an integer of at least two")


class PresenceReplayGate:
    """Consume detector results and same-source fresh image receipts.

    ``observe`` accepts a dict with session_id, clip_id, clock_domain (nonempty
    strings), frame_id (nonnegative int or nonempty string), source_pts_s,
    evaluated_at_pts_s, valid=True, persons=[] and hands=[]. Each detection is
    ``{bbox_xyxy: [x1,y1,x2,y2], score: 0..1}``. Lists contain the detector's
    already thresholded positive candidates; this gate never drops a candidate.
    Optional semantic_presence is exactly 'present', 'absent' or 'uncertain'. A
    semantic positive needs no fabricated localization; real detection boxes
    still prevent clear confirmation even if the classifier says 'absent'.
    Uncertain/invalid semantic values fail closed. Omitting the field preserves
    the original box-only behavior, so a semantic pipeline must supply it on
    every observation rather than silently falling back on classifier failure.
    image_sha256 is required to complete re-observation. Both times must use the
    same original video PTS domain; PC wall time is deliberately absent.

    After continuous clear samples, a request is issued. A subsequent valid clear
    frame must first pass observe(), then its matching receipt may be supplied to
    reobserve(). Neither initial clear nor disappearance directly grants even
    replay eligibility. External latches persist across clips and require an
    explicit unlock() call; disappearance never clears them.
    """

    def __init__(self, config=None):
        self.config = PresenceGateConfig() if config is None else config
        if not isinstance(self.config, PresenceGateConfig):
            raise TypeError("config must be PresenceGateConfig")
        self._context = None
        self._generation = 0
        self._request_counter = 0
        self._event_sequence = 0
        self._latches = set()
        self._events = []
        self._reset_context()

    def _reset_context(self):
        self.state = "PAUSED"
        self.reason = "awaiting_valid_observation"
        self._last_source_pts = None
        self._last_evaluated_pts = None
        self._seen_frame_ids = set()
        self._latest_sample = None
        self._persons, self._hands = [], []
        self._semantic_presence = None
        self._clear_start = None
        self._clear_count = 0
        self._last_clear_pts = None
        self._request = None
        self._reobservation = None

    def _event(self, name, **extra):
        self._event_sequence += 1
        self._events.append({"sequence": self._event_sequence, "event": name, **extra})

    def _transition(self, state, reason):
        previous = self.state
        self.state, self.reason = state, reason
        if previous != state:
            self._event("state_changed", previous=previous, state=state, reason=reason)

    def _pause(self, reason):
        self._clear_start = None
        self._clear_count = 0
        self._last_clear_pts = None
        self._request = None
        self._reobservation = None
        self._transition("PAUSED", reason)

    def _reject(self, reason):
        self._latest_sample = None
        self._pause(reason)
        self._event("input_rejected", reason=reason)
        return self.snapshot()

    def lock(self, reason):
        """Latch an external replay fault; never sends an emergency-stop command."""
        if not _text(reason):
            raise ValueError("external latch reason must be a nonempty string")
        self._events = []
        self._latches.add(reason)
        self._pause("external_latch")
        self._event("external_latch_set", latch=reason)
        return self.snapshot()

    def unlock(self, reason):
        """Record explicit external latch release, requiring a new clear sequence.

        This does not clear, acknowledge or reset any real hardware fault.
        """
        if not _text(reason):
            raise ValueError("external latch reason must be a nonempty string")
        self._events = []
        existed = reason in self._latches
        self._latches.discard(reason)
        self._pause("external_latch" if self._latches else "external_release_requires_new_observation")
        self._latest_sample = None
        self._event("external_latch_release_recorded", latch=reason, was_set=existed)
        return self.snapshot()

    @staticmethod
    def _boxes(value):
        if not isinstance(value, list):
            raise ValueError("missing_or_invalid_detection_lists")
        result = []
        for row in value:
            if not isinstance(row, dict):
                raise ValueError("invalid_detection_box")
            box, score = row.get("bbox_xyxy"), row.get("score")
            if (not isinstance(box, (list, tuple)) or len(box) != 4
                    or not all(_finite(v) for v in box)
                    or min(box) < 0 or box[2] <= box[0] or box[3] <= box[1]
                    or not _finite(score) or not 0 <= score <= 1):
                raise ValueError("invalid_detection_box")
            result.append({"bbox_xyxy": list(box), "score": score})
        return result

    def observe(self, sample):
        """Evaluate one original-frame detector result, never interpolated frames."""
        self._events = []
        self._persons, self._hands = [], []
        self._semantic_presence = None
        if not isinstance(sample, dict):
            return self._reject("invalid_sample")
        identity = tuple(sample.get(key) for key in ("session_id", "clip_id", "clock_domain"))
        if not all(_text(value) for value in identity) or not _frame_id(sample.get("frame_id")):
            return self._reject("missing_source_identity")
        if self._context is None or identity[:2] != self._context[:2]:
            previous = self._context
            self._context = identity
            self._generation += 1
            self._reset_context()
            self._event("source_context_reset", previous=list(previous) if previous else None,
                        current=list(identity))
        elif identity[2] != self._context[2]:
            return self._reject("clock_domain_changed")

        pts, evaluated = sample.get("source_pts_s"), sample.get("evaluated_at_pts_s")
        if not _finite(pts) or not _finite(evaluated) or min(pts, evaluated) < 0:
            return self._reject("invalid_source_time")
        if pts > evaluated + _EPS:
            return self._reject("future_source_frame")
        if self._last_evaluated_pts is not None and evaluated < self._last_evaluated_pts - _EPS:
            return self._reject("evaluation_time_reversed")
        frame_id = sample["frame_id"]
        if frame_id in self._seen_frame_ids:
            return self._reject("duplicate_frame_id")
        if self._last_source_pts is not None and pts <= self._last_source_pts + _EPS:
            return self._reject("non_increasing_source_pts")
        self._seen_frame_ids.add(frame_id)
        self._last_source_pts = pts
        self._last_evaluated_pts = evaluated
        if evaluated - pts > self.config.max_age_s + _EPS:
            return self._reject("stale_source_frame")
        if sample.get("valid") is not True or sample.get("error") not in (None, ""):
            return self._reject("detector_error_or_invalid_input")
        try:
            persons = self._boxes(sample.get("persons"))
            hands = self._boxes(sample.get("hands"))
        except ValueError as exc:
            return self._reject(str(exc))
        self._persons, self._hands = persons, hands
        if "semantic_presence" in sample:
            tag = sample["semantic_presence"]
            if not isinstance(tag, str) or tag not in ("present", "absent", "uncertain"):
                return self._reject("invalid_semantic_presence")
            self._semantic_presence = tag
            if tag == "uncertain":
                return self._reject("semantic_presence_uncertain")
        self._latest_sample = deepcopy({key: sample.get(key) for key in (
            "session_id", "clip_id", "clock_domain", "frame_id", "source_pts_s",
            "evaluated_at_pts_s", "image_sha256", "semantic_presence")})
        if self._semantic_presence == "present":
            self._pause("semantic_presence_detected")
            self._event("presence_pause", kind="semantic_presence", frame_id=frame_id, source_pts_s=pts)
            return self.snapshot()
        if persons or hands:
            kind = "person_and_hand" if persons and hands else "person" if persons else "hand"
            self._pause(f"{kind}_detected")
            self._event("presence_pause", kind=kind, frame_id=frame_id, source_pts_s=pts)
            return self.snapshot()
        if self._latches:
            self._pause("external_latch")
            return self.snapshot()

        if self._last_clear_pts is not None and pts - self._last_clear_pts > self.config.max_clear_gap_s + _EPS:
            source_gap_s = pts - self._last_clear_pts
            self._pause("clear_sequence_gap")
            self._event("clear_sequence_restarted", source_gap_s=source_gap_s)
        if self._clear_start is None:
            self._clear_start = pts
            self._clear_count = 0
        self._clear_count += 1
        self._last_clear_pts = pts
        duration = pts - self._clear_start
        if self.state == "RESUME_ELIGIBLE":
            self.reason = "fresh_clear_continues"
        elif self.state == "REOBSERVE_REQUIRED":
            self.reason = "awaiting_new_scene_receipt"
        elif (duration + _EPS >= self.config.clear_confirm_s
              and self._clear_count >= self.config.min_clear_samples):
            self._request_counter += 1
            self._request = {
                "request_id": f"presence-reobserve-{self._generation}-{self._request_counter}",
                "session_id": identity[0], "clip_id": identity[1], "clock_domain": identity[2],
                "requested_after_pts_s": pts, "requested_after_frame_id": frame_id,
                "required": "new_valid_clear_original_frame_and_sha256_receipt",
                "does_not_verify": ["holding", "standstill", "path_safety", "physical_stop"],
            }
            self._transition("REOBSERVE_REQUIRED", "clear_confirmed_reobserve_required")
            self._event("reobserve_requested", **deepcopy(self._request))
        else:
            self._transition("CLEAR_CONFIRMING", "collecting_continuous_clear_frames")
        return self.snapshot()

    def reobserve(self, receipt):
        """Consume a matching receipt for a newly observed original clear image."""
        self._events = []
        if self.state != "REOBSERVE_REQUIRED" or self._request is None or self._latches:
            return self._reject("reobserve_not_requested_or_external_latch")
        if not isinstance(receipt, dict) or receipt.get("scene_reobserved") is not True:
            return self._reject("invalid_reobserve_receipt")
        if (not _frame_id(receipt.get("frame_id"))
                or not _finite(receipt.get("source_pts_s"))
                or not all(_text(receipt.get(key)) for key in ("session_id", "clip_id", "clock_domain"))):
            return self._reject("invalid_reobserve_source_identity")
        if receipt.get("request_id") != self._request["request_id"]:
            return self._reject("reobserve_request_mismatch")
        current = self._latest_sample
        if current is None or self._persons or self._hands:
            return self._reject("reobserve_current_frame_not_clear")
        for key in ("session_id", "clip_id", "clock_domain", "frame_id", "source_pts_s", "image_sha256"):
            if receipt.get(key) != current.get(key):
                return self._reject("reobserve_source_mismatch")
        sha = receipt.get("image_sha256")
        if not isinstance(sha, str) or _SHA256.fullmatch(sha) is None:
            return self._reject("reobserve_missing_image_sha256")
        if current["source_pts_s"] <= self._request["requested_after_pts_s"] + _EPS:
            return self._reject("reobserve_frame_not_newer_than_request")
        self._reobservation = deepcopy(receipt)
        self._transition("RESUME_ELIGIBLE", "fresh_scene_receipt_accepted_replay_only")
        self._event("reobserve_receipt_accepted", request_id=receipt["request_id"],
                    frame_id=receipt["frame_id"], source_pts_s=receipt["source_pts_s"], image_sha256=sha)
        return self.snapshot()

    def tick(self, evaluated_at_pts_s, clock_domain):
        """Expire missing input using advancing video PTS, never PC wall time.

        A replay consumer must call this when source time advances without a
        detector result. snapshot() itself deliberately never reads a clock.
        """
        self._events = []
        if self._context is None or clock_domain != self._context[2]:
            return self._reject("watchdog_missing_or_wrong_clock_domain")
        if not _finite(evaluated_at_pts_s) or evaluated_at_pts_s < 0:
            return self._reject("invalid_watchdog_time")
        if self._last_evaluated_pts is not None and evaluated_at_pts_s < self._last_evaluated_pts - _EPS:
            return self._reject("evaluation_time_reversed")
        self._last_evaluated_pts = evaluated_at_pts_s
        if self._last_source_pts is None or evaluated_at_pts_s - self._last_source_pts > self.config.max_age_s + _EPS:
            return self._reject("missing_or_stale_detector_result")
        return self.snapshot()

    def snapshot(self):
        """Return an immutable-by-copy, display-oriented offline decision."""
        clear_s = (self._last_clear_pts - self._clear_start
                   if self._clear_start is not None and self._last_clear_pts is not None else 0.0)
        eligible = self.state == "RESUME_ELIGIBLE" and not self._latches
        return {
            "schema": SCHEMA, "state": self.state, "reason": self.reason,
            "session_id": self._context[0] if self._context else None,
            "clip_id": self._context[1] if self._context else None,
            "clock_domain": self._context[2] if self._context else None,
            "source_pts_s": self._last_source_pts,
            "evaluated_at_pts_s": self._last_evaluated_pts,
            "current_frame_id": self._latest_sample["frame_id"] if self._latest_sample else None,
            "persons": deepcopy(self._persons), "hands": deepcopy(self._hands),
            "semantic_presence": self._semantic_presence,
            "person_count": len(self._persons), "hand_count": len(self._hands),
            "continuous_clear_s": max(0.0, clear_s), "clear_sample_count": self._clear_count,
            "reobserve_request": deepcopy(self._request),
            "reobservation_receipt": deepcopy(self._reobservation),
            "external_latches": sorted(self._latches), "events": deepcopy(self._events),
            "event_sequence": self._event_sequence, "config": asdict(self.config),
            "replay_resume_eligible": eligible, "replay_permission": eligible,
            "replay_only": True, "motion_authority": False, "commands_sent": 0,
            "physical_stop_verified": False,
        }
