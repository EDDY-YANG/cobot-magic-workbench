# Shared task-state bookkeeping; no standalone hardware entry point.
# PC terminal, from the project directory (no SSH or devices):
# python -X utf8 -B -m unittest discover -s tests -p 'test_2026_10_09_task_state.py' -v
"""Read-only task supervision with separate observation and execution evidence.

This module opens no device, starts no worker, sends no command and writes no
file. Execution events are an internal caller contract, not authenticated robot
receipts: do not expose arbitrary event injection through a browser endpoint.
Explicit operator reviews are allowed, but remain operator provenance rather
than being relabelled as sensor/model confirmations. Candidate evidence never
confirms a physical outcome. Recovery is deliberately monitor-only.
"""
from collections import deque
from copy import deepcopy
import json
import math
import threading
import time


NODES = (
    ("prepare", "准备姿态"), ("approach", "接近目标"), ("grasp", "夹爪闭合"),
    ("verify_grasp", "核验持物"), ("transport", "转运候选"),
    ("release", "释放候选"), ("verify_place", "核验放置"),
    ("return", "正常回位"), ("finalize", "录制收尾"),
)
STAGE_NODES = {"approach_candidate": "approach", "gripper_closing": "grasp",
               "transport_candidate": "transport", "release_candidate": "release"}
EVENT_ALIASES = {"prepare": "prepare_started", "policy": "policy_started",
                 "return": "return_started", "recordpending": "recording_pending"}
EVENTS = frozenset(("deployment_started", "prepare_started", "prepare_completed", "policy_started",
                   "policy_completed", "deadline", "return_started",
                   "return_completed", "return_failed", "return_skipped", "fault",
                   "cancelled", "recording_pending", "recording_completed",
                   "recording_failed"))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _text(value, name, limit=400):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(name + " must be nonempty bounded text")
    return value.strip()


def _result(status="unverified"):
    return {"status": status, "provenance": None, "evidence": None}


def validate_operator_review(verdict, reviewer, evidence_refs):
    if verdict not in ("success", "failure", "inconclusive"):
        raise ValueError("Review verdict must be success, failure or inconclusive")
    reviewer = _text(reviewer, "reviewer", 160)
    if not isinstance(evidence_refs, (list, tuple)) or not 1 <= len(evidence_refs) <= 16:
        raise ValueError("Review requires one to sixteen evidence references")
    return reviewer, [_text(value, "evidence reference", 1024) for value in evidence_refs]


def validate_execution_event(event, receipt):
    """Validate before changing a task identity or renewing its owner lease."""
    if not isinstance(event, str):
        raise ValueError("Execution event must be text")
    event = EVENT_ALIASES.get(event, event)
    if event not in EVENTS or not isinstance(receipt, dict):
        raise ValueError("Unknown execution event or invalid receipt")
    try:
        encoded = json.dumps(receipt, allow_nan=False, ensure_ascii=False)
        if len(encoded) > 8192:
            raise ValueError("Receipt too large; use a report reference")
        evidence = json.loads(encoded)
    except (ValueError, TypeError) as exc:
        raise ValueError("Receipt must be bounded finite JSON") from exc
    if event == "return_completed" and evidence.get("endpoint_verified") is not True:
        raise ValueError("Return completion requires a verified actual endpoint receipt")
    if event == "recording_completed" and evidence.get("saved") is not True:
        raise ValueError("Recording completion requires a saved receipt")
    return event, evidence


class TaskStateMachine:
    """Thread-safe, bounded observation/FSM snapshots, never an action executor."""

    def __init__(self, cfg=None):
        source = (cfg or {}).get("task_state", cfg or {})
        self.freshness_limit_s = source.get("freshness_limit_s", 1.0)
        self.classifier_candidate_threshold = source.get("classifier_candidate_threshold", .8)
        if not _number(self.classifier_candidate_threshold) or not .5 <= self.classifier_candidate_threshold <= 1:
            raise ValueError("classifier_candidate_threshold must be finite and within .5..1")
        if not _number(self.freshness_limit_s) or self.freshness_limit_s <= 0:
            raise ValueError("freshness_limit_s must be positive and finite")
        if source.get("mode", "monitor_only") != "monitor_only":
            raise ValueError("Only monitor_only task supervision is implemented")
        if source.get("max_retries", 1) != 1 or isinstance(source.get("max_retries", 1), bool):
            raise ValueError("This implementation exposes a one-retry budget only")
        self._lock = threading.RLock()
        self._revision = 0
        self._session = None
        self._active = False
        self._history = deque(maxlen=64)
        self._reset()

    def _reset(self):
        self._phase = "idle"
        self._updated = None
        self._stage = {"phase": "unknown", "label": "无法确认阶段", "fresh": False}
        self._perception = {"held": "unknown", "placed": "unknown", "fresh": False}
        self._results = {key: _result("unverified" if key == "physical_task" else "not_started")
                         for key in ("physical_task", "execution", "return", "recording")}
        self._operator_review = None
        self._failure_reason = None
        self._nodes = {key: "not_observed" for key, _ in NODES}
        self._history.clear()

    def _event(self, kind, detail=None):
        self._revision += 1
        self._history.append({"revision": self._revision, "kind": kind,
                              "session_id": self._session, "at_monotonic": time.monotonic(),
                              "wall_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                              "detail": deepcopy(detail)})

    def _session_check(self, session_id):
        if self._session is None or session_id != self._session:
            raise ValueError("Task session mismatch; stale events cannot update the current task")

    def set_context(self, session_id, active):
        sid = _text(session_id, "session_id", 160)
        if type(active) is not bool:
            raise ValueError("active must be boolean")
        with self._lock:
            if sid != self._session:
                if self._active:
                    raise ValueError("An active task owns this state machine")
                self._reset()
                self._session = sid
            if self._active != active or not self._history:
                self._active = active
                self._event("context", {"active": active})
            return self.snapshot()

    def lose_owner(self, reason):
        """Expire observation ownership only; do not infer or command a motor stop."""
        with self._lock:
            self._active = False
            self._phase = "attention"
            self._failure_reason = reason
            for key in ("execution", "return"):
                if self._results[key]["status"] in ("checking", "preparing", "running"):
                    self._results[key] = {"status": "unverified", "provenance": "owner_lease_expired",
                        "evidence": {"reason": reason, "motion_stop_confirmed": False}}
            self._event("owner_lease_expired", {"reason": reason, "commands_sent": 0})
            return self.snapshot()

    def _fresh_observation(self, stage, arm):
        if not isinstance(stage, dict) or not isinstance(arm, dict):
            return False
        age = stage.get("age_s")
        if stage.get("fresh") is not True or not _number(age) or not 0 <= age <= self.freshness_limit_s:
            return False
        q, grip, limit = arm.get("q_deg"), arm.get("grip_m"), arm.get("stale_after_s", .2)
        return (arm.get("fresh") is True and _number(limit) and 0 < limit <= 1
                and isinstance(q, (list, tuple)) and len(q) == 6 and all(_number(x) for x in q)
                and _number(grip) and all(_number(arm.get(k)) and 0 <= arm[k] <= limit
                                        for k in ("age_joint_s", "age_gripper_s")))

    def update(self, stage_snapshot, actual_arm, perception_snapshot=None, now_perf=None):
        now = time.perf_counter() if now_perf is None else now_perf
        if not _number(now):
            raise ValueError("now_perf must be finite")
        stage = stage_snapshot if isinstance(stage_snapshot, dict) else {}
        perception = perception_snapshot if isinstance(perception_snapshot, dict) else {}
        with self._lock:
            valid = (self._active and self._fresh_observation(stage, actual_arm)
                     and (self._updated is None or now >= self._updated))
            phase = stage.get("phase", "unknown") if valid else "unknown"
            if not isinstance(phase, str) or phase not in STAGE_NODES and phase != "observing":
                phase = "unknown"
            self._stage = {"phase": phase,
                           "label": str(stage.get("label", "无法确认阶段"))[:160] if valid and phase != "unknown" else "无法确认阶段",
                           "fresh": bool(valid), "source": "shared_rgb_actual_joint_heuristic",
                           "task_result": "unverified"}
            age = perception.get("age_s")
            p_fresh = (valid and perception.get("fresh") is True and _number(age)
                       and 0 <= age <= self.freshness_limit_s)
            # These remain candidates even if an input dictionary asserts success.
            held = perception.get("held", perception.get("grasp_state", "unknown")) if p_fresh else "unknown"
            placed = perception.get("placed", perception.get("place_state", "unknown")) if p_fresh else "unknown"
            self._perception = {
                "held": held if held in ("held", "not_held", "unknown") else "unknown",
                "placed": placed if placed in ("placed", "not_placed", "unknown") else "unknown",
                "fresh": bool(p_fresh), "evidence_level": "candidate",
                "confirmed": False, "source": str(perception.get("source", "unverified_observer"))[:160],
            }
            classifier = perception.get("classifier", {})
            neural = perception.get("neural")
            if (p_fresh and isinstance(neural, dict) and neural.get("fresh") is True and
                    (neural.get("matches_request_source") is True or
                     "matches_request_source" not in neural and neural.get("matches_latest_source") is True)
                    and ("task_session_id" not in neural or neural["task_session_id"] == self._session)):
                output = neural.get("classifier")
                output = output if isinstance(output, dict) else {}
                body = output.get("response", output) if isinstance(output, dict) else {}
                body = body if isinstance(body, dict) else {}
                probabilities = body.get("probabilities", {})
                probabilities = probabilities if isinstance(probabilities, dict) else {}
                if (output.get("status") == "evidence" and
                        all(_number(probabilities.get(k)) and 0 <= probabilities[k] <= 1
                            for k in ("grasped", "empty", "uncertain"))):
                    held_probability, empty_probability = probabilities["grasped"], probabilities["empty"]
                    self._perception["held"] = ("held" if held_probability >= self.classifier_candidate_threshold else
                                                  "not_held" if empty_probability >= self.classifier_candidate_threshold else "unknown")
                    self._perception["classifier"] = {"probability": held_probability,
                        "label": self._perception["held"], "source": "hil_classifier_request_source_candidate",
                        "evidence_level": "candidate", "confirmed": False,
                        "candidate_threshold": self.classifier_candidate_threshold, "model_id": body.get("model_id"),
                        "source_stamps": deepcopy(neural.get("source_stamps")), "age_s": neural.get("age_s"),
                        "source_age_s": neural.get("source_image_age_s", neural.get("age_s")),
                        "sequence": neural.get("sequence"), "task_session_id": neural.get("task_session_id"),
                        "valid_until_perf": neural.get("valid_until_perf"),
                        "joint_input": "RGB-only classifier; current joints are freshness gate only"}
                placement = neural.get("placement")
                placement = placement if isinstance(placement, dict) else {}
                place_body = placement.get("response", {}) if isinstance(placement, dict) else {}
                place_body = place_body if isinstance(place_body, dict) else {}
                place_probabilities = place_body.get("probabilities", {})
                place_probabilities = place_probabilities if isinstance(place_probabilities, dict) else {}
                if (placement.get("status") == "evidence" and all(_number(place_probabilities.get(k))
                    and 0 <= place_probabilities[k] <= 1 for k in ("placed", "not_placed", "uncertain"))):
                    self._perception["placed"] = ("placed" if place_probabilities["placed"] >= self.classifier_candidate_threshold
                        else "not_placed" if place_probabilities["not_placed"] >= self.classifier_candidate_threshold else "unknown")
                    self._perception["placement_classifier"] = {"probability": place_probabilities["placed"],
                        "model_id": place_body.get("model_id"), "evidence_level": "candidate", "confirmed": False,
                        "source_stamps": deepcopy(neural.get("source_stamps")), "age_s": neural.get("age_s"),
                        "source_age_s": neural.get("source_image_age_s", neural.get("age_s")),
                        "sequence": neural.get("sequence"), "task_session_id": neural.get("task_session_id"),
                        "valid_until_perf": neural.get("valid_until_perf"),
                        "joint_input": "RGB-only classifier; current joints are freshness gate only"}
            if p_fresh and isinstance(classifier, dict):
                probability = classifier.get("probability")
                if _number(probability) and 0 <= probability <= 1:
                    self._perception["classifier"] = {
                        "probability": probability, "label": str(classifier.get("label", "unknown"))[:160],
                        "source": str(classifier.get("source", "unverified_classifier"))[:160],
                        "evidence_level": "candidate", "confirmed": False,
                    }
            self._updated = now
            # Observations never overrule a lifecycle fault, return or finalization.
            if self._phase in ("idle", "unknown", "observing", *STAGE_NODES.values(), "policy", "verify_grasp", "verify_place") and self._active:
                new_phase = STAGE_NODES.get(phase, phase)
                if new_phase in self._nodes:
                    self._nodes[new_phase] = "candidate"
                if self._perception["held"] != "unknown" and new_phase == "grasp":
                    new_phase = "verify_grasp"
                if self._perception["placed"] != "unknown" and new_phase == "release":
                    new_phase = "verify_place"
                if self._phase != new_phase:
                    self._phase = new_phase
                    if new_phase in self._nodes:
                        self._nodes[new_phase] = "candidate"
                    self._event("observation_phase", {"phase": new_phase})
                else:
                    self._revision += 1
            else:
                self._revision += 1
            return self.snapshot(now)

    def record_execution_event(self, session_id, event, receipt):
        """Record an internal execution receipt, not a browser assertion of motion."""
        event, evidence = validate_execution_event(event, receipt)
        with self._lock:
            self._session_check(session_id)
            # Late notifications after a terminal policy fault cannot restart it.
            if event == "recording_pending" and self._results["recording"]["status"] in ("completed", "failed"):
                return self.snapshot()
            if (self._results["execution"]["status"] in ("failed", "cancelled")
                    and event in ("deployment_started", "prepare_started", "prepare_completed", "policy_started", "policy_completed", "deadline")):
                self._event("late_execution_event_ignored", {"event": event, "receipt": evidence})
                return self.snapshot()
            status, key = None, None
            if event == "deployment_started":
                self._phase, key, status = "preflight", "execution", "checking"
            elif event.startswith("prepare_"):
                self._phase = "prepare"
                self._nodes["prepare"] = "completed" if event.endswith("completed") else "running"
                key, status = "execution", "preparing"
            elif event == "policy_started":
                self._phase, key, status = "policy", "execution", "running"
            elif event in ("policy_completed", "deadline"):
                self._phase, key, status = "policy", "execution", "completed"
            elif event.startswith("return_"):
                self._phase = "return"
                key = "return"
                status = {"return_started": "running", "return_completed": "completed",
                          "return_failed": "failed", "return_skipped": "skipped"}[event]
                self._nodes["return"] = status
            elif event.startswith("recording_"):
                self._phase = "finalize"
                key = "recording"
                status = {"recording_pending": "pending", "recording_completed": "completed",
                          "recording_failed": "failed"}[event]
                self._nodes["finalize"] = status
            elif event in ("fault", "cancelled"):
                self._phase = "attention"
                self._failure_reason = str(evidence.get("reason", evidence.get("error", event)))[:1000]
                if evidence.get("area") == "return":
                    key, status = "return", "failed" if event == "fault" else "skipped"
                    self._nodes["return"] = status
                else:
                    key, status = "execution", "failed" if event == "fault" else "cancelled"
            if key:
                # Do not erase a failed/cancelled policy with a late completion.
                if key != "execution" or self._results[key]["status"] not in ("failed", "cancelled"):
                    self._results[key] = {"status": status, "provenance": "execution_receipt", "evidence": evidence}
            self._event("execution_event", {"event": event, "receipt": evidence})
            return self.snapshot()

    def review_outcome(self, session_id, verdict, reviewer, evidence_refs):
        """Explicit human review, never a synthetic sensor/model confirmation."""
        reviewer, refs = validate_operator_review(verdict, reviewer, evidence_refs)
        with self._lock:
            self._session_check(session_id)
            review = {"verdict": verdict, "reviewer": reviewer, "evidence_refs": refs,
                      "provenance": "operator_review", "reviewed_at_unix_s": time.time()}
            self._operator_review = review
            self._results["physical_task"] = {"status": "unverified" if verdict == "inconclusive" else verdict,
                                               "provenance": "operator_review", "evidence": deepcopy(review)}
            self._event("operator_review", review)
            return self.snapshot()

    def snapshot(self, now_perf=None):
        now = time.perf_counter() if now_perf is None else now_perf
        if not _number(now):
            raise ValueError("now_perf must be finite")
        with self._lock:
            age = None if self._updated is None else now - self._updated
            fresh = self._active and age is not None and 0 <= age <= self.freshness_limit_s and self._stage["fresh"]
            stage, perception = deepcopy(self._stage), deepcopy(self._perception)
            phase = self._phase
            for head, verdict in (("classifier", "held"), ("placement_classifier", "placed")):
                candidate = perception.get(head)
                deadline = candidate.get("valid_until_perf") if isinstance(candidate, dict) else None
                if _number(deadline) and now >= deadline:
                    perception[verdict] = "unknown"
                    perception.pop(head, None)
            if not fresh:
                stage.update(phase="unknown", label="无法确认阶段", fresh=False)
                perception.update(held="unknown", placed="unknown", fresh=False)
                perception.pop("classifier", None)
                perception.pop("placement_classifier", None)
                if phase in ("observing", *STAGE_NODES.values()):
                    phase = "unknown"
            blockers = ["自动退让路径与入口范围尚未实机验收", "目标定位与抓持证据尚未通过独立验收"]
            if not fresh:
                blockers.insert(0, "缺少当前会话的新鲜观测")
            if self._failure_reason:
                blockers.insert(0, "执行故障/取消仍需独立处理")
            intent = "operator_review" if fresh and perception["held"] == "not_held" else None
            return {"schema": "tiger-task-state-v1", "revision": self._revision,
                    "source": "task_state_machine", "freshness_limit_s": self.freshness_limit_s,
                    "session_id": self._session, "active": self._active, "phase": phase,
                    "stage_label": stage["label"], "fresh": bool(fresh), "age_s": age,
                    "model_stage": stage, "perception": perception,
                    "operator_review": deepcopy(self._operator_review),
                    "results": deepcopy(self._results), "failure_reason": self._failure_reason,
                    "recovery": {"mode": "monitor_only", "max_retries": 1, "retry_count": 0,
                                 "eligible": False, "intent": intent, "blockers": blockers,
                                 "commands_sent": 0, "automatic_recovery_validated": False},
                    "graph": {"nodes": [{"id": key, "label": label, "status": self._nodes[key],
                                          "active": phase == key} for key, label in NODES],
                              "edges": [{"from": NODES[i][0], "to": NODES[i + 1][0]}
                                        for i in range(len(NODES) - 1)],
                              "meaning": "候选阶段与执行回执；图中连线不证明物理动作已完成"},
                    "history": deepcopy(list(self._history)), "commands_sent": 0}
