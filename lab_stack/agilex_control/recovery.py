# Shared-hub recovery state management; no standalone hardware entry point.
# PC offline tests, from the project folder:
# python -X utf8 -B -m unittest discover -s tests -p 'test_2026_10_08_18_03_recovery.py' -v
"""Read-only failure candidates and evidence-gated recovery bookkeeping.

This module opens no device, sends no command, and writes no file. A pause
request is NOT a robot stop. The caller owns any real stop/retreat/retry and
must supply independently verified receipts through trusted internal calls.
Receipt dictionaries are an evidence contract, not an authentication mechanism;
never expose confirmation methods directly to untrusted browser JSON.

Color candidates have no verified identity, depth, or metric calibration. Their
heuristics can only flag a suspected problem, never confirm grasp/task success.
"""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import math
import threading
import time


LABELS = {
    "observing": "观察中 · 尚无疑似失败标记",
    "suspected": "发现疑似问题 · 等待复核",
    "pause_requested": "已记录暂停请求 · 尚未确认机器人停止",
    "awaiting_safe_retreat": "已核对停止回执 · 等待安全退让证据",
    "reobserve": "已核对退让回执 · 重新观察目标",
    "retry_ready": "重试前证据已核对 · 等待独立启动",
    "retrying": "已收到实际重试启动回执 · 结果待核对",
    "needs_operator": "需要现场处理 · 不自动继续",
}
DEFAULTS = {
    "mode": "monitor_only", "max_retries": 1,
    "freshness_limit_s": 1.0, "frame_max_age_s": 0.5,
    "max_compare_gap_s": 1.5, "stagnation_window_s": 6.0,
    "missed_grasp_window_s": 3.0, "min_samples": 6,
    "joint_stable_deg": 0.4, "gripper_stable_m": 0.002,
    "centroid_stable_norm": 0.015, "area_stable_fraction": 0.01,
    "gripper_close_m": 0.003, "joint_departure_deg": 3.0,
    "receipt_max_age_s": 2.0,
}
AUTOMATION_GAPS = [
    "尚未验收可执行的安全退让轨迹与碰撞检查。",
    "尚无经过标定的目标空间位置与重抓目标。",
    "颜色候选、夹爪闭合和程序结束不能独立证明持物或放置成功。",
]
STATE_GAPS = {
    "observing": ["连续的新鲜图像与真实关节／夹爪反馈。"],
    "suspected": ["现场或独立视觉复核；疑似线索不等于抓取失败。"],
    "pause_requested": ["真实执行回执：已停止新目标，且机械臂已静止。"],
    "awaiting_safe_retreat": ["已审阅的安全退让路径、实际终点与静止回执。"],
    "reobserve": ["新鲜的目标可见性与独立身份确认。"],
    "retry_ready": ["由独立执行入口返回的实际重试启动回执。"],
    "retrying": ["独立的持物证据；随后仍须单独核对放置结果。"],
    "needs_operator": ["现场处理及重新审阅；不能自动恢复。"],
}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class RecoveryEvidenceError(ValueError):
    """Evidence was missing, stale, replayed, or invalid for the current state."""


class RecoverySupervisor:
    """Bounded in-memory FSM; all transitions are records, never robot commands."""

    def __init__(self, config=None):
        source = config or {}
        source = source.get("recovery", source)
        self.config = {**DEFAULTS, **{k: source[k] for k in DEFAULTS if k in source}}
        if self.config["mode"] != "monitor_only":
            raise ValueError("Only monitor_only is implemented; automatic recovery is not validated")
        for key, value in self.config.items():
            if key != "mode" and (not _number(value) or value <= 0):
                raise ValueError("Invalid recovery parameter: " + key)
        for key in ("max_retries", "min_samples"):
            if not isinstance(self.config[key], int) or isinstance(self.config[key], bool):
                raise ValueError(key + " must be an integer")
        if self.config["min_samples"] > 64:
            raise ValueError("min_samples exceeds bounded observation buffer")
        self._lock = threading.RLock()
        self._samples = deque(maxlen=64)
        self._history = deque(maxlen=16)
        self._receipt_ids = deque(maxlen=64)
        self._context = {"deployment_active": False, "front_camera_stationary_verified": False,
                         "session_id": None}
        self._state, self._entered, self._retry_count = "observing", None, 0
        self._candidate, self._last_sources, self._last_close = None, None, None
        self._updated, self._valid_at, self._sample_count = None, None, 0
        self._source_timestamps = {}
        self._evidence = ["仅监测与记录，尚无真实观测样本。"]

    def set_context(self, context):
        """Caller sets actual deployment activity; this does not start/stop it.

        New sessions clear temporal detector cues but preserve unresolved events
        and retry accounting. Merely changing a session ID cannot reset recovery.
        """
        context = context or {}
        with self._lock:
            before = dict(self._context)
            for key in ("deployment_active", "front_camera_stationary_verified"):
                if key in context:
                    self._context[key] = context[key] is True
            if "session_id" in context:
                sid = context["session_id"]
                self._context["session_id"] = str(sid)[:160] if sid is not None else None
            if self._context != before:
                self._clear_window()

    def _clear_window(self, keep_sources=False):
        self._samples.clear()
        if not keep_sources:
            self._last_sources = None
        self._last_close = None
        self._valid_at = None

    def _transition(self, state, reason, now):
        self._state, self._entered = state, now
        self._history.append({"state": state, "label": LABELS[state],
                              "at": datetime.now(timezone.utc).isoformat(), "reason": reason})
        self._evidence = [reason]

    def _sample(self, stage, arm, now):
        if stage.get("fresh") is not True or not _number(stage.get("age_s")) or not 0 <= stage["age_s"] <= self.config["freshness_limit_s"]:
            raise ValueError("阶段观察源缺失或过期，不累计失败线索。")
        stamps = stage.get("source_timestamps", {})
        camera_stamps = tuple((stamps.get(role) or {}).get("stamp_perf") for role in ("front", "right"))
        if not all(_number(s) and 0 <= now - s <= self.config["frame_max_age_s"] for s in camera_stamps):
            raise ValueError("相机采集时间戳缺失、过期或无效。")
        if abs(camera_stamps[0] - camera_stamps[1]) > 0.2:
            raise ValueError("相机采集时间不同步，不累计失败线索。")
        q, grip = arm.get("q_deg"), arm.get("grip_m")
        limit = arm.get("stale_after_s", 0.2)
        if not _number(limit) or limit <= 0:
            limit = 0.2
        ages_ok = all(_number(arm.get(k)) and 0 <= arm[k] <= limit for k in ("age_joint_s", "age_gripper_s"))
        if arm.get("fresh") is not True or not ages_ok or not isinstance(q, (list, tuple)) or len(q) != 6 or not all(_number(v) for v in q) or not _number(grip):
            raise ValueError("真实关节或夹爪反馈无效／过期。")
        raw = arm.get("raw", {})
        feedback_stamps = tuple((raw.get(cid) or {}).get("monotonic_s") for cid in ("0x2a5", "0x2a6", "0x2a7", "0x2a8"))
        if not all(_number(s) for s in feedback_stamps):
            raise ValueError("缺少真实关节／夹爪收包时间戳，不能去重。")
        sources = camera_stamps + feedback_stamps
        if self._last_sources and any(a <= b for a, b in zip(sources, self._last_sources)):
            raise ValueError("重复或回退的源时间戳，不重复累计失败线索。")
        # CAN timestamps use monotonic; camera timestamps use perf_counter.
        # We only compare each source with itself, never subtract the two clocks.
        red = ((stage.get("candidates", {}).get("front") or {}).get("red") or {})
        selected = red.get("selected") or {}
        center, area = selected.get("centroid_norm"), selected.get("area_fraction")
        if red.get("status") != "candidate" or not isinstance(center, (list, tuple)) or len(center) != 2 or not all(_number(v) and 0 <= v <= 1 for v in center) or not _number(area) or not 0 < area <= 1:
            raise ValueError("头部图像没有单一可跟踪的红色候选；不推断失败。")
        self._last_sources = sources
        self._source_timestamps = deepcopy(stamps)
        return {"time": now, "q": list(q), "grip": grip, "center": list(center), "area": area}

    @staticmethod
    def _span(samples, key, count):
        return max(max(s[key][i] for s in samples) - min(s[key][i] for s in samples) for i in range(count))

    def _suspect(self, kind, reason, now):
        self._candidate = {"kind": kind, "label": reason, "confirmed_failure": False,
                           "detected_perf_s": now, "source_timestamps": deepcopy(self._source_timestamps)}
        state = "needs_operator" if self._retry_count >= self.config["max_retries"] else "suspected"
        self._transition(state, reason + (" 重试计数已达上限。" if state == "needs_operator" else ""), now)

    def observe(self, stage_snapshot, actual_arm, now_perf=None, context=None):
        """At <=2 Hz, consume actual shared snapshots; never stop or retry.

        No-progress is only an absence-of-visible-change heuristic, not a test
        of task completion. A post-closing displacement comparison additionally
        requires a verified stationary front camera; wrist motion is not used.
        """
        now = time.perf_counter() if now_perf is None else now_perf
        if not _number(now):
            raise ValueError("now_perf must be finite")
        with self._lock:
            if context is not None:
                self.set_context(context)
            self._updated = now
            if self._context["deployment_active"] is not True:
                self._clear_window()
                self._evidence = ["部署未运行，不生成失败候选；历史事件保留供复盘。"]
                return self.snapshot(now)
            try:
                sample = self._sample(stage_snapshot or {}, actual_arm or {}, now)
            except (ValueError, TypeError, AttributeError) as exc:
                self._clear_window(keep_sources=True)
                self._evidence = [str(exc)]
                return self.snapshot(now)
            previous = self._samples[-1] if self._samples else None
            if previous and not 0 < now - previous["time"] <= self.config["max_compare_gap_s"]:
                self._samples.clear()
                self._last_close = None
                previous = None
            if previous and previous["grip"] - sample["grip"] >= self.config["gripper_close_m"]:
                self._last_close = previous
            if previous and sample["grip"] - previous["grip"] > self.config["gripper_stable_m"]:
                self._last_close = None
            self._samples.append(sample)
            self._sample_count += 1
            self._valid_at = now
            self._evidence = ["连续新鲜图像与真实关节／夹爪反馈；颜色候选身份未经确认。"]
            if self._state not in ("observing", "retrying"):
                return self.snapshot(now)
            window_s = self.config["stagnation_window_s"]
            # Retain the sample immediately before the window edge at 2 Hz.
            window = [s for s in self._samples if now - s["time"] <= window_s + self.config["max_compare_gap_s"]]
            if len(window) >= self.config["min_samples"] and now - window[0]["time"] >= window_s:
                stable = (self._span(window, "q", 6) <= self.config["joint_stable_deg"] and
                          max(s["grip"] for s in window) - min(s["grip"] for s in window) <= self.config["gripper_stable_m"] and
                          self._span(window, "center", 2) <= self.config["centroid_stable_norm"] and
                          max(s["area"] for s in window) - min(s["area"] for s in window) <= self.config["area_stable_fraction"])
                if stable:
                    self._suspect("possible_no_progress", "疑似无进展：持续 %.1f 秒未观察到明显关节、夹爪或头部红色候选变化；也可能为计划内等待，不自动停止。" % (now - window[0]["time"]), now)
                    return self.snapshot(now)
            close = self._last_close
            if close and self._context["front_camera_stationary_verified"] is True:
                elapsed = now - close["time"]
                post = [s for s in self._samples if s["time"] >= close["time"]]
                if elapsed > 2 * self.config["missed_grasp_window_s"]:
                    self._last_close = None
                elif elapsed >= self.config["missed_grasp_window_s"] and len(post) >= self.config["min_samples"]:
                    departed = max(abs(a - b) for a, b in zip(sample["q"], close["q"])) >= self.config["joint_departure_deg"]
                    unchanged = self._span(post, "center", 2) <= self.config["centroid_stable_norm"] and max(s["area"] for s in post) - min(s["area"] for s in post) <= self.config["area_stable_fraction"]
                    if departed and unchanged:
                        self._suspect("possible_missed_grasp", "疑似未带走目标：实际夹爪闭合后关节已变化，固定头部相机的红色候选仍近似不变；颜色身份未验证，需人工复核。", now)
            return self.snapshot(now)

    def _receipt(self, evidence, expected_state, required, now):
        if self._state != expected_state:
            raise RecoveryEvidenceError("Unexpected recovery state: " + self._state)
        evidence = evidence or {}
        stamp, receipt_id = evidence.get("observed_perf_s"), evidence.get("receipt_id")
        if evidence.get("trusted") is not True or not isinstance(receipt_id, str) or not receipt_id.strip() or not isinstance(evidence.get("source"), str) or not evidence["source"].strip():
            raise RecoveryEvidenceError("Independent trusted receipt, source and receipt_id required")
        if not _number(stamp) or not 0 <= now - stamp <= self.config["receipt_max_age_s"] or self._entered is not None and stamp < self._entered:
            raise RecoveryEvidenceError("Receipt is stale, future-dated or precedes the current state")
        if receipt_id in self._receipt_ids or not all(evidence.get(key) is True for key in required):
            raise RecoveryEvidenceError("Receipt is replayed or required physical evidence is unverified")
        self._receipt_ids.append(receipt_id)

    def _confirm(self, evidence, expected, required, next_state, reason, now_perf):
        now = time.perf_counter() if now_perf is None else now_perf
        if not _number(now):
            raise RecoveryEvidenceError("Invalid receipt clock")
        with self._lock:
            self._receipt(evidence, expected, required, now)
            self._transition(next_state, reason + " 回执：" + evidence["receipt_id"], now)
            return self.snapshot(now)

    def request_pause(self, request_id, reason="用户请求复核疑似问题", now_perf=None):
        """Records intent only; the execution owner must independently stop."""
        now = time.perf_counter() if now_perf is None else now_perf
        with self._lock:
            if not _number(now) or not isinstance(request_id, str) or not request_id.strip() or self._state not in ("observing", "suspected", "retrying"):
                raise RecoveryEvidenceError("Pause request invalid for current state")
            self._transition("pause_requested", str(reason)[:400] + "；请求 " + request_id[:160] + " 尚非停止回执。", now)
            return self.snapshot(now)

    def confirm_paused(self, evidence, now_perf=None):
        return self._confirm(evidence, "pause_requested", ("new_targets_stopped", "stationary"),
                             "awaiting_safe_retreat", "已独立核对停止新目标及静止", now_perf)

    def confirm_retreat(self, evidence, now_perf=None):
        return self._confirm(evidence, "awaiting_safe_retreat", ("path_reviewed", "retreat_safe_verified", "endpoint_verified", "stationary"),
                             "reobserve", "已核对独立执行的安全退让回执；本模块未执行退让", now_perf)

    def confirm_visibility(self, evidence, now_perf=None):
        return self._confirm(evidence, "reobserve", ("target_visible", "target_identity_verified"),
                             "retry_ready", "已核对新鲜目标可见性及独立身份确认", now_perf)

    def confirm_retry_started(self, evidence, now_perf=None):
        now = time.perf_counter() if now_perf is None else now_perf
        with self._lock:
            if self._retry_count >= self.config["max_retries"]:
                self._transition("needs_operator", "重试次数已达上限；不允许自动恢复。", now)
                raise RecoveryEvidenceError("Retry budget exhausted")
            self._confirm(evidence, "retry_ready", ("execution_started", "new_session_verified"),
                          "retrying", "收到独立执行入口的实际重试启动回执", now)
            self._retry_count += 1
            self._clear_window()
            return self.snapshot(now)

    def confirm_grasp(self, evidence, now_perf=None):
        with self._lock:
            self._confirm(evidence, "retrying", ("object_held_verified", "independent_of_gripper_and_stage"),
                          "observing", "已收到独立持物证据；放置结果仍未确认", now_perf)
            self._candidate = None
            self._clear_window()
            return self.snapshot(now_perf)

    def require_operator(self, reason, now_perf=None):
        now = time.perf_counter() if now_perf is None else now_perf
        if not _number(now):
            raise RecoveryEvidenceError("Invalid clock")
        with self._lock:
            self._transition("needs_operator", str(reason)[:400], now)
            return self.snapshot(now)

    def snapshot(self, now_perf=None):
        now = time.perf_counter() if now_perf is None else now_perf
        with self._lock:
            age = None if self._valid_at is None or not _number(now) else now - self._valid_at
            fresh = self._context["deployment_active"] is True and age is not None and 0 <= age <= self.config["freshness_limit_s"]
            return {"schema": "tiger-recovery-supervisor-v1", "state": self._state,
                    "label": LABELS[self._state], "mode": "monitor_only",
                    "automatic_recovery_validated": False, "commands_sent": 0,
                    "task_result": "unverified", **self._context,
                    "fresh": fresh, "age_s": age, "freshness_limit_s": self.config["freshness_limit_s"],
                    "sample_count": self._sample_count, "window_samples": len(self._samples),
                    "candidate": deepcopy(self._candidate), "evidence": list(self._evidence),
                    "missing_evidence": list(STATE_GAPS[self._state]),
                    "automation_gaps": list(AUTOMATION_GAPS),
                    "retry_count": self._retry_count, "max_retries": self.config["max_retries"],
                    "source_timestamps": deepcopy(self._source_timestamps),
                    "history": deepcopy(list(self._history))}
