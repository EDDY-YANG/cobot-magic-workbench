"""Linux persistent receive-only agent over pinned SSH stdio.

Only an explicit validated job invokes the existing CLI. This module contains
no SDK motion calls; no camera device is opened by the monitoring connection.
Do not run directly. The timestamped web entry selects the correct interpreter.
"""
import base64
from collections import deque
import copy
import json
import math
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from .storage import Ownership, verify_usb, stop, stop_status
from .web_backend import (build_command, parse_receipt, stamp, ValidationError,
                          aged_snapshot, capabilities, fp32_shadow_health_view,
                          h50_health_view, h50_action_gate)


def model_process_snapshot(root):
    """Check the registered child identity; process liveness is not readiness.

    Reuse the launcher's PID, command and Linux start-ticks checks, so a reused
    PID cannot be labelled as a model that is still loading. No service action.
    """
    from .cli import _registered_child
    try:
        saved, alive = _registered_child(root, "model-serve")
        return {"registered": saved is not None, "alive": alive,
                "identity_verified": True, "fresh": True, "age_s": 0.,
                "stale_after_s": 3., "pid": saved.get("pid") if saved else None,
                "log": saved.get("log") if saved else None}
    except Exception as exc:
        return {"registered": None, "alive": None, "identity_verified": False,
                "fresh": True, "age_s": 0., "stale_after_s": 3., "error": str(exc)}


class RobotAgent:
    HUB_REFRESH_PERIOD_S = .05
    MODEL_REFRESH_PERIOD_S = .5
    FP32_SHADOW_REFRESH_PERIOD_S = 1.
    STORAGE_REFRESH_PERIOD_S = 5.
    MODEL_HEALTH_STALE_AFTER_S = 3.
    STORAGE_STALE_AFTER_S = 15.

    def __init__(self, cfg, root):
        from .sensors import camera_inventory, ros_diagnostics
        self.cfg, self.root = cfg, root
        usb_started = time.monotonic()
        verify_usb(cfg)
        self.lock, self.output_lock = threading.RLock(), threading.Lock()
        self.jobs, self.observers = deque(maxlen=32), {}
        self.cached, self.cache_at, self.closing = {}, time.monotonic(), threading.Event()
        self._component_refresh = {}
        self.inventory = camera_inventory()
        self.ros = ros_diagnostics()
        self.usb = {}
        selected = cfg.get("usb_selection", {})
        self._publish_component("usb", {"verified": True, "path": cfg["usb"],
            "uuid": cfg["usb_uuid"], "label": selected.get("label"),
            "display_name": selected.get("display_name"), "selection_frozen": bool(selected),
            "checked_at": stamp(), "age_s": 0.,
            "fresh": True, "stale_after_s": self.STORAGE_STALE_AFTER_S},
            usb_started, self.STORAGE_REFRESH_PERIOD_S)
        self.cached["gripper_coordinate_review"] = {
            "pending": True, "fresh": False, "reason": "Coordinate review not read yet"}
        self.cached["model"] = {"api_reachable": False, "ready": False,
            "health_age_s": None, "health_stale_after_s": self.MODEL_HEALTH_STALE_AFTER_S,
            "health_fresh": False, "process": {"fresh": False,
                "identity_verified": False, "alive": None}}
        self.cached['fp32_shadow'] = fp32_shadow_health_view(error='独立4090 health尚未读取')
        self.cached['h50_deployment'] = h50_health_view(error='4090 H50 manager尚未读取')
        self._observer_retry_at, self._observer_cleanup_pending = {}, {}
        for role in cfg["can"]:
            self.observers[role] = {"fresh": False, "observer_start_failed": True,
                                    "observer_start_attempts": 0, "q_deg": None}
        self._retry_observer_starts()
        self._refresh_threads = [
            threading.Thread(target=self._refresh, daemon=True, name="read-only-hub-status"),
            threading.Thread(target=self._metadata, daemon=True, name="read-only-service-metadata"),
            threading.Thread(target=self._refresh_fp32_shadow, daemon=True, name="read-only-fp32-shadow-health")]
        for worker in self._refresh_threads:
            worker.start()

    def _retry_observer_starts(self):
        """Retry failed initialization only; never hide a running receiver fault.

        A single lifecycle lock serializes creation, publication and close. The
        receive-only factory rechecks the registered USB identity every attempt.
        No state GET creates sockets, and each role is attempted at most once
        per five seconds even if a constructor/start fails immediately.
        """
        from .sensors import CanObserver
        with self.lock:
            if self.closing.is_set():
                return
            for role, entry in self.cfg["can"].items():
                previous = self.observers.get(role)
                if (not isinstance(previous, dict) or not previous.get("observer_start_failed")
                        or role in self._observer_cleanup_pending):
                    continue
                now = time.monotonic()
                if now < self._observer_retry_at.get(role, 0.):
                    continue
                self._observer_retry_at[role] = now + 5.
                attempts = previous.get("observer_start_attempts", 0) + 1
                observer = None
                try:
                    observer = CanObserver(**entry)
                    observer.start()
                    self.observers[role] = observer
                    self._observer_retry_at.pop(role, None)
                except Exception as exc:
                    failed = {"fresh": False, "feedback_fresh": False,
                              "observer_running": False, "q_deg": None,
                              "observer_start_failed": True,
                              "observer_start_attempts": attempts,
                              "observer_error": repr(exc),
                              "observer_retry_interval_s": 5.}
                    if observer is not None:
                        try:
                            observer.stop()
                        except Exception as cleanup_exc:
                            # Preserve the object for final cleanup; never open
                            # a second socket when its predecessor may still live.
                            self._observer_cleanup_pending[role] = observer
                            failed.update(observer_retry_blocked=True,
                                          observer_cleanup_error=repr(cleanup_exc))
                    self.observers[role] = failed

    def _stop_observers(self):
        with self.lock:
            # Seal retries even when cleanup is invoked outside run().
            self.closing.set()
            observers = [value for value in self.observers.values()
                         if not isinstance(value, dict)]
            observers += list(self._observer_cleanup_pending.values())
        failures = []
        for observer in observers:
            try:
                observer.stop()
            except Exception as exc:
                failures.append(repr(exc))
        if failures:
            raise RuntimeError("Receive-only observer cleanup incomplete: " + "; ".join(failures))

    def _metadata(self):
        from .sensors import camera_inventory, ros_diagnostics
        last_usb, last_devices = None, time.monotonic()
        while not self.closing.is_set():
            started = time.monotonic()
            self._retry_observer_starts()
            if self.closing.is_set():
                break
            self._refresh_service("model", self.cfg["model_port"], "/health",
                                  self.MODEL_REFRESH_PERIOD_S)
            if not self.closing.is_set() and (last_usb is None or time.monotonic() - last_usb >= self.STORAGE_REFRESH_PERIOD_S):
                self._refresh_storage()
                last_usb = time.monotonic()
            if not self.closing.is_set() and time.monotonic() - last_devices >= 10.:
                try:
                    inventory, ros = camera_inventory(), ros_diagnostics()
                    with self.lock:
                        if not self.closing.is_set():
                            self.inventory, self.ros = inventory, ros
                except Exception as exc:
                    with self.lock:
                        if not self.closing.is_set():
                            self.inventory = {"error": repr(exc), "checked_at": stamp()}
                last_devices = time.monotonic()
            # Skip missed periods; slow I/O cannot cause a catch-up burst.
            self.closing.wait(max(0., self.MODEL_REFRESH_PERIOD_S - (time.monotonic() - started)))

    def _get(self, port, route, binary=False):
        request = urllib.request.Request(f"http://127.0.0.1:{port}" + route)
        with urllib.request.urlopen(request, timeout=.8) as response:
            return response.read() if binary else json.load(response)

    def _probe_fp32_shadow(self):
        """One rate-limited GET loop for both H50 views; never launch a model."""
        started = time.monotonic()
        settings = self.cfg.get('fp32_shadow', {})
        h50_settings = self.cfg.get('h50_deployment', {})
        h50_enabled = isinstance(h50_settings, dict) and h50_settings.get('enabled') is True
        h50 = None
        try:
            if not h50_enabled and (not isinstance(settings, dict) or settings.get('enabled') is not True):
                raise ValueError('4090 FP32影子路由未启用')
            port = h50_settings.get('model_port') if h50_enabled else settings.get('health_port', 18883)
            if type(port) is not int or port != 18883:
                raise ValueError('4090影子路由仅接受已冻结端口18883')
            health = self._get(port, '/health')  # Existing GET has a 0.8s timeout.
            current = fp32_shadow_health_view(health)
            if h50_enabled:
                status = self._get(port, '/status')
                catalog = self._get(port, '/catalog')
                acceptance, acceptance_error = None, None
                selected = status.get('selected_step') if isinstance(status, dict) else None
                if type(selected) is int and selected in (2000, 5000, 10000, 15000, 20000):
                    try:
                        acceptance = self._get(port, '/acceptance?step=' + str(selected))
                    except Exception as exc:
                        acceptance_error = str(exc)
                h50 = h50_health_view(health, status, catalog, acceptance=acceptance,
                                       acceptance_error=acceptance_error)
        except Exception as exc:
            current = fp32_shadow_health_view(error=str(exc))
            if h50_enabled:
                h50 = h50_health_view(error=str(exc))
        self._publish_component('fp32_shadow', current, started, self.FP32_SHADOW_REFRESH_PERIOD_S)
        self._publish_component('h50_deployment', h50 or h50_health_view(error='H50部署路由未启用'),
                                started, self.FP32_SHADOW_REFRESH_PERIOD_S)

    def _refresh_fp32_shadow(self):
        # Independent from Hub's 50ms refresh and the legacy model health loop.
        while not self.closing.is_set():
            started = time.monotonic()
            self._probe_fp32_shadow()
            self.closing.wait(max(0., self.FP32_SHADOW_REFRESH_PERIOD_S - (time.monotonic() - started)))

    def _publish_component(self, key, value, started, period):
        """Publish one completed probe, preserving its conservative start anchor."""
        completed = time.monotonic()
        with self.lock:
            if self.closing.is_set():
                return
            previous = self._component_refresh.get(key, {})
            if key == "usb":
                self.usb = value
            else:
                self.cached[key] = value
            self._component_refresh[key] = {
                "sequence": previous.get("sequence", 0) + 1,
                "sampled_at": started,
                "request_duration_s": max(0., completed - started),
                "period_s": period}

    def _refresh_service(self, key, port, route, period):
        started = time.monotonic()
        response_received = False
        try:
            current = self._get(port, route)
            response_received = True
            if key == "hub" and self.cfg.get("usb_selection"):
                from .hub import validate_hub_storage
                validate_hub_storage(self.cfg, current)
            current = {**current, "api_reachable": True}
        except Exception as exc:
            # A failed probe never republishes old measurements as a new sample.
            current = {"api_reachable": False, "error": str(exc)}
            if key == "hub" and response_received:
                # A foreign/unbound producer is not ours to reuse or retarget.
                # Keep its occupied-owner evidence even while reporting it as
                # unusable for this selected USB's recording contract.
                current["owner_present"] = True
        if key == "model":
            current.update(health_age_s=0., health_stale_after_s=self.MODEL_HEALTH_STALE_AFTER_S,
                           health_fresh=current["api_reachable"])
            current["process"] = model_process_snapshot(self.root)
        self._publish_component(key, current, started, period)

    def _refresh_storage(self):
        started = time.monotonic()
        usb = {"path": self.cfg["usb"], "uuid": self.cfg["usb_uuid"],
               "age_s": 0., "stale_after_s": self.STORAGE_STALE_AFTER_S}
        selected = self.cfg.get("usb_selection", {})
        usb.update(label=selected.get("label"), display_name=selected.get("display_name"),
                   selection_frozen=bool(selected))
        try:
            path = verify_usb(self.cfg)
            usage = shutil.disk_usage(path)
            usb.update(verified=True, fresh=True, error=None,
                       free_bytes=usage.free, capacity_bytes=usage.total)
        except Exception as exc:
            usb.update(verified=False, fresh=False, error=str(exc),
                       free_bytes=None, capacity_bytes=None)
        usb["checked_at"] = stamp()
        self._publish_component("usb", usb, started, self.STORAGE_REFRESH_PERIOD_S)
        if self.closing.is_set():
            return
        from .gripper_calibration import coordinate_review
        started = time.monotonic()
        try:
            if usb["verified"] is not True:
                raise RuntimeError("USB identity unavailable; coordinate review unverified")
            review = coordinate_review(self.cfg)
            review = {**review, "fresh": not bool(review.get("read_error"))}
        except Exception as exc:
            review = {"pending": True, "fresh": False, "read_error": str(exc),
                      "reason": "Calibration coordinate review unknown"}
        review.update(age_s=0., stale_after_s=self.STORAGE_STALE_AFTER_S)
        self._publish_component("gripper_coordinate_review", review, started,
                                self.STORAGE_REFRESH_PERIOD_S)

    def _refresh(self):
        # Reads the existing Hub only; never creates a camera or CAN producer.
        while not self.closing.is_set():
            started = time.monotonic()
            self._refresh_service("hub", self.cfg["hub_port"], "/status?compact=1",
                                  self.HUB_REFRESH_PERIOD_S)
            self.closing.wait(max(0., self.HUB_REFRESH_PERIOD_S - (time.monotonic() - started)))

    def _cached_components(self, now=None):
        with self.lock:
            cached, usb = copy.deepcopy(self.cached), copy.deepcopy(self.usb)
            metadata = copy.deepcopy(getattr(self, "_component_refresh", None))
            cache_at = self.cache_at
            # Production age includes lock contention and snapshot copying.
            # Explicit now is reserved for deterministic in-process fixtures.
            if now is None:
                now = time.monotonic()
        # Older in-process callers/test fixtures predate component anchors.
        if metadata is None:
            return {"usb": usb, **aged_snapshot(cached, now - cache_at)}
        value = {}
        refresh = {"schema": "tiger-web-state-refresh-v1"}
        for key, period in (("hub", self.HUB_REFRESH_PERIOD_S),
                            ("model", self.MODEL_REFRESH_PERIOD_S),
                            ('fp32_shadow', self.FP32_SHADOW_REFRESH_PERIOD_S),
                            ('h50_deployment', self.FP32_SHADOW_REFRESH_PERIOD_S)):
            refresh[key] = {"sequence": 0, "age_s": None,
                            "request_duration_s": None, "period_s": period}
        for key, source in {**cached, "usb": usb}.items():
            component = metadata.get(key)
            if component is None:
                value[key] = source
                continue
            elapsed = max(0., now - component["sampled_at"])
            value.update(aged_snapshot({key: source}, elapsed))
            refresh[key] = {"sequence": component["sequence"], "age_s": elapsed,
                "request_duration_s": component["request_duration_s"],
                "period_s": component["period_s"]}
        value["state_refresh"] = refresh
        return value

    def _join_refresh_threads(self):
        # Never join while holding the publication/lifecycle lock. An OS/file
        # probe can outlive this bounded wait, but closing forbids late writes.
        deadline = time.monotonic() + 1.
        for worker in getattr(self, "_refresh_threads", ()):
            if worker is not threading.current_thread():
                worker.join(timeout=max(0., deadline - time.monotonic()))

    def state(self):
        arms = {}
        arms_started = time.monotonic()
        with self.lock:
            observers = list(self.observers.items())
        for role, observer in observers:
            state = dict(observer) if isinstance(observer, dict) else observer.control_snapshot()
            state["joint_age_s"] = state.get("age_joint_s")
            state["age_s"] = state.get("age_joint_s")
            state["sample_monotonic_s"] = time.monotonic()
            sampled_wall = time.time()
            for source_key, target_key in (("age_status_s", "status_receive_wall_time_s"),
                                           ("age_motor_s", "motor_receive_wall_time_s")):
                age = state.get(source_key)
                state[target_key] = sampled_wall - age if type(age) in (int, float) else None
            status_frame = state.get("raw", {}).get("0x2a1", {})
            state["status_receipt_monotonic_s"] = status_frame.get("monotonic_s")
            # High-rate UI does not need raw frames or unused command-ID rows.
            # Full diagnostics remain available through the explicit CLI job.
            from .gripper_state import decode_gripper_feedback
            gripper_frame = state.get("raw", {}).get("0x2a8", {})
            gripper_age = state.get("age_gripper_s")
            try:
                gripper_info = decode_gripper_feedback(gripper_frame.get("payload_hex"))
                gripper_info.update(age_s=gripper_age, received_monotonic_s=gripper_frame.get("monotonic_s"),
                    fresh=(not state.get("observer_error") and not state.get("timestamp_failures")
                           and type(gripper_age) in (int, float)
                           and math.isfinite(gripper_age) and 0 <= gripper_age <= state.get("stale_after_s", .2)))
            except (TypeError, ValueError) as exc:
                gripper_info = {"fresh": False, "status_hex": None, "age_s": gripper_age,
                                "decode_error": str(exc), "healthy": None, "enabled": None}
            state["gripper_feedback"] = gripper_info
            state.pop("raw", None)
            state["external_commands"] = {key: row for key, row in state.get("external_commands", {}).items()
                                          if row.get("count", 0)}
            arms[role] = state
        with self.lock:
            jobs = copy.deepcopy(list(self.jobs))
            inventory, ros = copy.deepcopy(self.inventory), copy.deepcopy(self.ros)
        for job in jobs:
            job["output"] = job.get("output", "")[-4096:]
            receipt = job.get("result")
            from .collection_commands import bounded_collection_receipt
            if receipt and len(json.dumps(receipt)) > 8192 and not bounded_collection_receipt(receipt):
                job["result"] = {"report": receipt.get("report"), "details_in_usb_report": True,
                                 "result_keys": list(receipt.get("result", {}))}
        # These filesystem checks retain their meaning but cannot hold the Hub
        # publication lock. Age the direct feedback after any filesystem delay,
        # including local submit() callers that do not pass through the PC.
        stop_latch = stop_status(self.cfg)
        marker_present = (self.root / "model-cache/stage-verified.json").is_file()
        services = self._cached_components()
        arms = aged_snapshot({"arms": arms}, time.monotonic() - arms_started)["arms"]
        return {"updated_at": stamp(), "identity": {"hostname": socket.gethostname(), "uid": os.getuid()},
            "status": {"hostname": socket.gethostname(), "arms": arms, "camera_inventory": inventory,
                       "ros": ros, "CAN_commands_sent_by_this_diagnostic": 0},
            "stop_latch": stop_latch,
            "model_stage": {"marker_present": marker_present,
                            "note": "Presence is a prerequisite, not weight verification; loading rechecks actual files"},
            "jobs": jobs, **services}

    def submit(self, action, params):
        argv = build_command(action, params, self.cfg)
        verify_usb(self.cfg)
        snapshot = self.state()
        gate = capabilities(snapshot, True, self.cfg).get(action, {})
        if gate.get("available") is not True:
            raise ValidationError(gate.get("reason") or "功能尚未就绪")
        if action in ('h50_load', 'h50_shadow', 'h50_acceptance', 'h50_deploy'):
            reason = h50_action_gate(snapshot, self.cfg, action, params)
            if reason:
                raise ValidationError(reason)
        if action.startswith('move_') and '--close-gripper' in argv:
            close_gate = capabilities(self.state(), True, self.cfg).get('gripper_close', {})
            if close_gate.get('available') is not True:
                raise ValidationError(close_gate.get('reason') or '夹爪闭合准备条件未就绪')
        if action in ("task_review_success", "task_review_failure"):
            # Annotation only. No CLI child, writer lock or SDK is acquired.
            from .hub import rpc
            sid = params["session_id"]
            current = self.state().get("hub", {}).get("task_state", {}).get("session_id")
            if sid != current:
                raise ValidationError("任务已切换；请刷新后复核当前任务")
            result = rpc(self.cfg, "/task/review", {"session_id": sid,
                "verdict": action.removeprefix("task_review_"), "reviewer": "网页操作者人工复核",
                "evidence_refs": ["operator_note:" + params["note"].strip()]}, timeout=8)
            job = {"id": uuid.uuid4().hex[:12], "action": action, "state": "succeeded", "argv": argv,
                "created_at": stamp(), "finished_at": stamp(), "output": "", "result": result,
                "report": result.get("report"), "error": None, "CAN_motion_commands_sent": 0}
            with self.lock:
                self.jobs.append(job)
            return copy.deepcopy(job)
        with self.lock:
            # Validation/storage inspection may have overlapped stdin EOF or
            # explicit close. Registration must share the lifecycle lock.
            if self.closing.is_set():
                raise ValidationError("SSH 操作通道正在关闭，拒绝新任务")
            if any(j["state"] in ("queued", "running") for j in self.jobs):
                raise ValidationError("AgileX 当前已有任务，禁止并发控制")
            job = {"id": uuid.uuid4().hex[:12], "action": action, "state": "queued", "argv": argv,
                   "created_at": stamp(), "output": "", "result": None, "error": None}
            self.jobs.append(job)
        threading.Thread(target=self._job, args=(job,), daemon=True, name="unified-cli-job").start()
        return copy.deepcopy(job)

    def _job(self, job):
        from .cli import clean_env, ENTRY
        command = [self.cfg["hardware_python"], "-I", "-B", str(self.root / ENTRY), *job["argv"]]
        try:
            # Existing CLI owns CAN writer locks, actual bounds and motion gates.
            with self.lock:
                # Keep final check + Popen + running registration indivisible
                # with close. A queued worker cannot start after channel loss.
                if self.closing.is_set():
                    job.update(state="blocked", error="SSH 操作通道已关闭；CLI 未启动", finished_at=stamp())
                    return
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=clean_env(self.cfg, "hardware"))
                job.update(state="running", pid=process.pid)
            for line in process.stdout:
                with self.lock:
                    job["output"] = (job["output"] + line)[-65536:]
            rc = process.wait()
            receipt = parse_receipt(job["output"])
            with self.lock:
                job.update(state="succeeded" if rc == 0 else "failed", returncode=rc,
                    result=receipt, report=receipt.get("report"), error=receipt.get("error") if rc else None,
                    finished_at=stamp())
                if job["action"].startswith("mode_"):
                    mode_receipt = receipt.get("mode_receipt") or receipt.get("result", {}).get("mode_receipt")
                    if mode_receipt is None and receipt.get("result", {}).get("operation") in ("inspect", "takeover", "reset", "high-follow", "standby", "disable"):
                        mode_receipt = receipt["result"]
                    if mode_receipt is not None:
                        from .mode_state import compact_mode_receipt
                        job["mode_receipt"] = compact_mode_receipt(mode_receipt)
                if rc and not job["error"]:
                    job["error"] = job["output"][-2000:] or f"CLI exit {rc}"
                if job["action"] in ("status", "diagnose") and receipt.get("result", {}).get("ros"):
                    self.ros = receipt["result"]["ros"]
        except Exception as exc:
            with self.lock:
                job.update(state="failed", error=repr(exc), finished_at=stamp())

    def _begin_close(self, require_idle=False):
        """Seal submission/launch before checking whether a local stop is needed.

        Normal operator disconnect refuses any registered work. Unexpected EOF
        cancels pending launches and latches cease-new-targets for active work;
        it never resets, disables motors, returns the robot, or clears a latch.
        """
        with self.lock:
            active = any(j["state"] in ("queued", "running") for j in self.jobs)
            if require_idle and active:
                raise ValidationError("请先停止并等待当前任务完成，再断开监控")
            self.closing.set()
        if active:
            # New launch is already forbidden. The existing CLI observes this
            # RAM latch even if USB was removed; no hardware stop is claimed.
            return stop(self.cfg)
        return None

    def dispatch(self, request):
        op = request.get("op")
        if op == "state":
            return self.state()
        if op == "submit":
            return self.submit(request.get("action"), request.get("params"))
        if op == "stop":
            return stop(self.cfg)
        if op == "image":
            role = request.get("role")
            if role not in ("front", "right", "left"):
                raise ValidationError("未知相机")
            return {"jpeg_b64": base64.b64encode(self._get(self.cfg["hub_port"], "/image/" + role, binary=True)).decode()}
        if op == "close":
            self._begin_close(require_idle=True)
            return {"status": "RECEIVE_ONLY_AGENT_CLOSING_NO_MOTION"}
        raise ValidationError("未知远端操作")

    def run(self):
        def respond(request):
            try:
                response = {"id": request.get("id"), "ok": True, "result": self.dispatch(request)}
            except Exception as exc:
                response = {"id": request.get("id"), "ok": False, "error": str(exc)}
            with self.output_lock:
                print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)

        try:
            for line in sys.stdin:
                if len(line) > 16384:
                    raise ValidationError("SSH 请求过大")
                request = json.loads(line)
                # Stop uses a separate worker and never waits for image/CLI work.
                worker = threading.Thread(target=respond, args=(request,), daemon=True)
                worker.start()
                if request.get("op") == "close":
                    # Give the explicit close worker time to write its receipt,
                    # instead of returning immediately after it set closing.
                    worker.join(timeout=1)
                    if self.closing.is_set():
                        break
        finally:
            try:
                self._begin_close()
            finally:
                try:
                    self._stop_observers()
                finally:
                    self._join_refresh_threads()


def run_agent(cfg, root):
    with Ownership("web-agent"):
        RobotAgent(cfg, root).run()
