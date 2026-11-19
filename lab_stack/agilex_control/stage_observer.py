# Shared-hub observation module; not a standalone robot-control program.
# PC offline checks (from this repository):
# python -X utf8 -B -m unittest discover -s tests -p 'test_2026_10_08_13_48_stage_observer.py' -v
"""Bounded, read-only RGB/joint heuristics; never a task-success detector.

The caller owns camera capture and rate limiting. This class opens no device,
sends no command and writes no file. Synthetic test scenes are not deployment
evidence. HSV regions are color candidates, not semantic object identities.
"""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import math
import threading
import time


LABELS = {
    "unknown": "无法确认阶段",
    "observing": "观察中 · 未确认任务阶段",
    "approach_candidate": "可能正在逼近红色目标",
    "gripper_closing": "夹爪正在闭合 · 抓取未确认",
    "transport_candidate": "可能携物移动 · 持物未确认",
    "release_candidate": "夹爪正在张开 · 放置未确认",
}
LIMITATIONS = [
    "颜色区域只是候选；未使用物体语义识别或手眼标定。",
    "夹爪闭合和关节运动不能证明抓起；张开不能证明放入桶。",
    "遮挡、多个同色物体、光照变化或慢采样可能使阶段无法确认。",
    "观察器只读，不改变模型动作、RTC、CAN 或机器人运行状态。",
]
DEFAULTS = {
    "frame_max_age_s": 0.5,
    "freshness_limit_s": 1.0,
    "max_camera_skew_s": 0.2,
    "max_compare_gap_s": 1.5,
    "min_area_fraction": 0.0015,
    "max_area_fraction": 0.60,
    "ambiguity_ratio": 0.35,
    "gripper_change_m": 0.002,
    "joint_change_deg": 0.6,
    "approach_area_ratio": 1.15,
    "wrist_stability_norm": 0.045,
    "cue_lifetime_s": 4.0,
    "min_trend_samples": 2,
    "image_width": 320,
}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class StageObserver:
    """Consume fresh shared RGB and actual feedback; cache a JSON-only result."""

    def __init__(self, config=None):
        source = config or {}
        source = source.get("stage_observer", source)
        self.config = dict(DEFAULTS)
        self.config.update({key: source[key] for key in DEFAULTS if key in source})
        for key, value in self.config.items():
            if not _number(value) or value <= 0:
                raise ValueError("Invalid stage observer parameter: " + key)
        if not 0 < self.config["ambiguity_ratio"] <= 1:
            raise ValueError("ambiguity_ratio must be in (0, 1]")
        self._lock = threading.RLock()
        self._history = deque(maxlen=16)
        self._previous = None
        self._last_close = None
        self._trend_phase = None
        self._trend_count = 0
        self._updated = None
        self._count = 0
        self._state = self._base()

    def _base(self):
        return {
            "schema": "tiger-stage-observer-v1", "phase": "unknown",
            "label": LABELS["unknown"], "evidence": ["尚无观察样本。"],
            "limitations": list(LIMITATIONS), "task_result": "unverified",
            "model_subtask": None, "source": "rgb_and_joint_heuristic",
            "method": "HSV color candidates + temporal RGB/actual-joint cues; read-only",
            "fresh": False, "age_s": None,
            "freshness_limit_s": self.config["freshness_limit_s"],
            "sample_count": 0, "history": [], "source_timestamps": {},
            "candidates": {}, "motion_hint": "unknown", "gripper_hint": "unknown",
        }

    def _colors(self, rgb):
        # Import lazily: importing the control package needs no image dependency.
        import cv2
        import numpy as np

        if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3 or min(rgb.shape[:2]) < 2:
            raise ValueError("RGB must be a nonempty uint8 HxWx3 frame")
        h, w = rgb.shape[:2]
        scale = min(1.0, self.config["image_width"] / w)
        image = cv2.resize(rgb, (max(2, round(w * scale)), max(2, round(h * scale))), interpolation=cv2.INTER_AREA) if scale < 1 else rgb
        hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
        masks = {
            "red": cv2.bitwise_or(cv2.inRange(hsv, (0, 100, 60), (10, 255, 255)), cv2.inRange(hsv, (170, 100, 60), (179, 255, 255))),
            "green": cv2.inRange(hsv, (35, 60, 40), (90, 255, 255)),
        }
        ih, iw = image.shape[:2]
        results = {}
        for color, mask in masks.items():
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            candidates = []
            for contour in contours:
                area = float(cv2.contourArea(contour)) / (ih * iw)
                if not self.config["min_area_fraction"] <= area <= self.config["max_area_fraction"]:
                    continue
                moments = cv2.moments(contour)
                if moments["m00"] <= 0:
                    continue
                x, y, bw, bh = cv2.boundingRect(contour)
                candidates.append({"area_fraction": round(area, 6),
                                   "centroid_norm": [moments["m10"] / moments["m00"] / iw, moments["m01"] / moments["m00"] / ih],
                                   "bbox_norm": [x / iw, y / ih, bw / iw, bh / ih]})
            candidates.sort(key=lambda item: item["area_fraction"], reverse=True)
            ambiguous = len(candidates) > 1 and candidates[1]["area_fraction"] >= candidates[0]["area_fraction"] * self.config["ambiguity_ratio"]
            results[color] = {
                "status": "ambiguous" if ambiguous else "candidate" if candidates else "missing",
                "count": len(candidates), "selected": candidates[0] if candidates and not ambiguous else None,
                "regions": candidates[:4], "semantic_identity_verified": False,
            }
        return results

    def _clear_cues(self):
        self._previous = None
        self._last_close = None
        self._trend_phase = None
        self._trend_count = 0

    def _finish(self, result, now, phase, evidence):
        result.update(phase=phase, label=LABELS[phase], evidence=evidence,
                      sample_count=self._count, task_result="unverified")
        if not self._history or self._history[-1]["phase"] != phase:
            self._history.append({"phase": phase, "label": LABELS[phase],
                                  "at": datetime.now(timezone.utc).isoformat(),
                                  "reason": evidence[0] if evidence else ""})
        result["history"] = list(self._history)
        result["age_s"] = 0.0
        self._updated = now
        self._state = result
        return deepcopy(result)

    def update(self, frames, arm, now_perf=None):
        """Frames: role -> {rgb, stamp_perf, wall_ns}; arm uses actual q/grip.

        Clock contract: ``stamp_perf`` and ``now_perf`` are perf_counter seconds
        from this process. CAN feedback ages are copied from the shared reader.
        The caller should schedule at <=2 Hz, outside inference/CAN threads.
        """
        now = time.perf_counter() if now_perf is None else now_perf
        if not _number(now):
            raise ValueError("now_perf must be finite")
        with self._lock:
            self._count += 1
            result = self._base()
            evidence, stamps = [], []
            frames, arm = frames or {}, arm or {}
            for role in ("front", "right"):
                frame = frames.get(role) or {}
                stamp = frame.get("stamp_perf")
                wall_ns = frame.get("wall_ns")
                result["source_timestamps"][role] = {
                    "wall_ns": wall_ns if _number(wall_ns) else None,
                    "stamp_perf": stamp if _number(stamp) else None,
                }
                if not _number(stamp) or not 0 <= now - stamp <= self.config["frame_max_age_s"]:
                    evidence.append(role + " 相机缺失、过期或时间戳无效。")
                    continue
                stamps.append(stamp)
                try:
                    result["candidates"][role] = self._colors(frame.get("rgb"))
                except (ValueError, TypeError, ImportError) as exc:
                    evidence.append(role + " 图像不可分析：" + str(exc))
            q, grip = arm.get("q_deg"), arm.get("grip_m")
            q_valid = isinstance(q, (list, tuple)) and len(q) == 6 and all(_number(v) for v in q)
            arm_limit = arm.get("stale_after_s", 0.2)
            arm_limit = arm_limit if _number(arm_limit) and arm_limit > 0 else 0.2
            ages_valid = all(_number(arm.get(key)) and 0 <= arm[key] <= arm_limit for key in ("age_joint_s", "age_gripper_s"))
            if not arm.get("fresh", False) or not q_valid or not _number(grip) or not ages_valid:
                evidence.append("右臂真实关节或夹爪反馈缺失／过期。")
            if len(stamps) == 2 and abs(stamps[0] - stamps[1]) > self.config["max_camera_skew_s"]:
                evidence.append("两路相机时间差超过观察器阈值。")
            if evidence:
                self._clear_cues()
                return self._finish(result, now, "unknown", evidence)
            result["fresh"] = True
            colors = result["candidates"]
            if any(colors[role]["red"]["status"] == "ambiguous" for role in ("front", "right")):
                self._clear_cues()
                return self._finish(result, now, "unknown", ["出现多个相近大小红色候选；不能确定目标身份。"])
            red = {role: colors[role]["red"]["selected"] for role in ("front", "right")}
            if not any(red.values()):
                self._clear_cues()
                return self._finish(result, now, "unknown", ["两路图像均未发现可用红色候选；可能遮挡或未识别。"])
            current = {"time": now, "q": list(q), "grip": grip, "red": red,
                       "stamps": tuple(stamps)}
            prev = self._previous
            self._previous = current
            evidence = ["已读取两路新鲜 RGB 与右臂实际反馈；颜色候选身份未经确认。"]
            if prev is None or not 0 < now - prev["time"] <= self.config["max_compare_gap_s"]:
                self._last_close = None
                self._trend_count = 0
                return self._finish(result, now, "observing", evidence + ["等待连续观测以判断变化。"])
            if any(current_stamp <= previous_stamp for current_stamp, previous_stamp in zip(stamps, prev["stamps"])):
                # Repeated frames cannot become additional temporal evidence.
                self._clear_cues()
                return self._finish(result, now, "unknown", ["相机尚未提供两路新的图像；不重复累计阶段证据。"])
            grip_delta = grip - prev["grip"]
            joint_delta = max(abs(a - b) for a, b in zip(q, prev["q"]))
            moving = joint_delta >= self.config["joint_change_deg"]
            result["motion_hint"] = "actual_joint_change" if moving else "no_resolved_joint_change"
            result["gripper_hint"] = "closing" if grip_delta <= -self.config["gripper_change_m"] else "opening" if grip_delta >= self.config["gripper_change_m"] else "no_resolved_change"
            result["measurements"] = {"sample_interval_s": now - prev["time"],
                                      "max_joint_delta_deg": joint_delta,
                                      "gripper_delta_m": grip_delta}
            phase = "observing"
            if result["gripper_hint"] == "closing":
                self._last_close = now
                phase = "gripper_closing"
                evidence.append("实际夹爪开度正在减小；这不证明已夹住方块。")
            elif result["gripper_hint"] == "opening":
                phase = "release_candidate"
                self._last_close = None
                evidence.append("实际夹爪开度正在增大；尚无方块入桶证据。")
            elif red["right"] and prev["red"]["right"] and moving:
                cur_red, old_red = red["right"], prev["red"]["right"]
                distance = math.dist(cur_red["centroid_norm"], old_red["centroid_norm"])
                ratio = cur_red["area_fraction"] / old_red["area_fraction"]
                recent_close = self._last_close is not None and now - self._last_close <= self.config["cue_lifetime_s"]
                front_motion = red["front"] and prev["red"]["front"] and math.dist(red["front"]["centroid_norm"], prev["red"]["front"]["centroid_norm"]) >= 0.015
                if recent_close and front_motion and distance <= self.config["wrist_stability_norm"] and 0.8 <= ratio <= 1.25:
                    phase = "transport_candidate"
                    evidence.append("近期发生闭合，红色候选在头部视野移动、在腕部视野较稳定；持物仍需确认。")
                elif ratio >= self.config["approach_area_ratio"]:
                    phase = "approach_candidate"
                    evidence.append("关节发生变化且腕部红色候选面积增大；可能靠近，也可能是视角变化。")
            # Multi-frame trends need repeated independent observations. Actual
            # gripper changes remain reported immediately as measurements.
            if phase in ("approach_candidate", "transport_candidate"):
                self._trend_count = self._trend_count + 1 if phase == self._trend_phase else 1
                self._trend_phase = phase
                if self._trend_count < self.config["min_trend_samples"]:
                    phase = "observing"
                    evidence.append("变化趋势尚未达到连续样本要求。")
            else:
                self._trend_phase, self._trend_count = None, 0
            return self._finish(result, now, phase, evidence)

    def snapshot(self, now_perf=None):
        """Return a detached cached result, invalidating stale display claims."""
        now = time.perf_counter() if now_perf is None else now_perf
        if not _number(now):
            raise ValueError("now_perf must be finite")
        with self._lock:
            result = deepcopy(self._state)
            age = None if self._updated is None else now - self._updated
            result["age_s"] = age
            if age is None or not _number(age) or not 0 <= age <= self.config["freshness_limit_s"]:
                result.update(fresh=False, phase="unknown", label=LABELS["unknown"],
                              evidence=["阶段观察已过期或尚未运行；历史记录不能代表当前场景。"],
                              motion_hint="unknown", gripper_hint="unknown")
            return result
