# Copyright (c) 2026 Yang Taige
# SPDX-License-Identifier: MIT
"""Workbench orchestration for right-side scheme-one takeover."""
from copy import deepcopy
import threading
import time

from .right_dagger import SCHEMA
from .web_backend import WebApp, ValidationError

ROUTES = {"precision_deploy", "v2_deploy"}
ACTIVE = {"queued", "running", "unknown"}
DAGGER_ACTIONS = {"dagger_prepare", "dagger_intervene", "dagger_resume", "dagger_finish"}
PHASES = {
    "idle": "待准备", "preparing": "准备右后臂", "ready": "右后臂已准备",
    "policy": "模型执行", "waiting_policy": "等待模型停止", "taking_control": "交接控制权",
    "human": "人工遥操作", "restoring": "恢复后臂控制", "finishing": "结束并保存",
    "saved": "纠正片段已保存", "ended": "本次接管已结束", "fault": "已中止",
    "unavailable": "等待设备模块",
}


class DaggerWorkbench(WebApp):
    def __init__(self, *args, **kwargs):
        self.dagger_jobs = {}
        self.intervention_requests = set()
        self.dagger_error = None
        self.dagger_reply = None
        self.resume_pending = None
        self.dagger_action_lock = threading.Lock()
        super().__init__(*args, **kwargs)
        self.dagger_quit = threading.Event()
        self.dagger_supervisor = threading.Thread(target=self._supervise_dagger, daemon=True,
                                                  name="right-dagger-supervisor")
        self.dagger_supervisor.start()

    def _device(self, state):
        value = state.get("right_dagger") or {}
        reply = getattr(self, "dagger_reply", None)
        if reply and reply["received_at"] > getattr(self, "cache_at", 0):
            value = reply["value"]
        if not state.get("connected") or state.get("cache_residence_s", 999) > .75:
            return {**value, "phase": "offline", "available": False}
        return {**value, "available": value.get("schema") == SCHEMA and value.get("enabled") is True}

    def _runner(self, job):
        if not job or self.precision is None:
            return None
        return self.precision.v2 if job["action"] == "v2_deploy" else self.precision.io

    def _running_policy(self):
        with self.lock:
            active = [j for j in self.jobs if j.get("state") in ACTIVE]
            return active[0] if len(active) == 1 and active[0]["action"] in ROUTES else None

    def _cancel_policy(self, job):
        runner = self._runner(job)
        if runner is None:
            raise ValidationError("模型运行器不可用")
        with runner.lock:
            child = runner.child
            if child is None or child.poll() is not None:
                return
            if not child.stdin or child.stdin.closed:
                return
            if job["action"] == "v2_deploy":
                runner.speed.close("右侧 DAgger 接管")
            child.stdin.close()

    def _device_request(self, action, binding=None, **data):
        result = self.transport.request("right_dagger", action=action, binding=binding, data=data)
        if not isinstance(result, dict) or result.get("schema") != SCHEMA:
            raise ValidationError("机器人端右侧 DAgger 接口版本不匹配")
        self.dagger_reply = {"received_at": time.monotonic(), "value": deepcopy(result)}
        return result

    def snapshot(self):
        state = super().snapshot()
        device = self._device(state)
        phase = device.get("phase", "unavailable")
        job = self._running_policy()
        matching = bool(job and job["id"] == device.get("policy_job_id"))
        available = device.get("available") is True
        error = self.dagger_error or device.get("error") or device.get("receipt_error")
        notes = {
            "offline": "机器人离线。等待实际启动周期、USB、源码和接口核对。",
            "unavailable": "机器人端右侧接管模块尚未接入。",
            "idle": "准备按钮会切换、使能并对齐右后臂。点击即确认右侧独立接线、机械臂可靠承托且运动范围已清空。",
            "ready": "在模型部署页启动任务，右后臂将接收本轮模型的同一组关节目标。",
            "policy": "右后臂同步本轮模型目标。接管后等待原执行器和录制收尾。",
            "waiting_policy": "原运行器正在取消。控制权交接等待实际退出、写入锁释放和前后臂对齐。",
            "taking_control": "核对前后位置与夹爪开度，切换右后臂为主臂并准备纠正录制。",
            "human": ("右后主臂输入正在驱动右前臂。过期或不完整的目标停止更新。"
                      if device.get("human_inputs", 0) else "后臂角色配置已应答，等待第一组有效人工输入。"),
            "restoring": "保存当前纠正片段并恢复后臂跟随准备。新一轮推理从实际观测重新开始。",
            "saved": ("原生纠正片段及输入证据已完成保存。任务成功需人工评价。"
                      if device.get("rear_role_restored") else "纠正数据已保存，右后臂角色恢复仍需核对。"),
        }
        state["dagger"] = {
            "schema": SCHEMA, "phase": PHASES.get(phase, "机器人离线" if phase == "offline" else phase),
            "phase_code": phase, "binding": device.get("binding"),
            "policy_job_id": device.get("policy_job_id"), "arms": device.get("arms", {}),
            "policy": "本轮运行中" if matching else "等待模型任务",
            "record": "纠正录制中" if phase == "human" else "已保存" if device.get("saved") else "待录制",
            "note": error or notes.get(phase, "等待读取实际控制状态。"),
            "error": error, "episode": device.get("episode") or device.get("last_episode"),
            "can_prepare": available and phase in ("idle", "saved", "ended", "fault", "unavailable") and not job,
            "can_enter": available and phase == "policy" and matching and job.get("state") == "running",
            "can_resume": available and phase == "human" and not job,
            "can_finish": available and phase in ("ready", "human", "fault") and not job,
            "hardware_validated": device.get("hardware_validated") is True,
            "human_inputs": device.get("human_inputs", 0),
            "native_record_saved": device.get("native_record_saved", False),
        }
        return state

    def submit(self, action, params):
        if not self.dagger_action_lock.acquire(blocking=False):
            raise ValidationError("上一项右侧接管操作仍在提交")
        try:
            return self._submit_locked(action, params)
        finally:
            self.dagger_action_lock.release()

    def _submit_locked(self, action, params):
        if action not in DAGGER_ACTIONS:
            device = self._device(super().snapshot())
            phase = device.get("phase")
            permitted = {"status", "record_list", "collection_list", "precision_load", "precision_release"}
            if phase in ("preparing", "policy", "waiting_policy", "taking_control", "human", "restoring", "finishing"):
                if action not in permitted:
                    raise ValidationError("右侧 DAgger 正在运行，请先结束本次接管")
            if phase == "ready" and action not in permitted | ROUTES:
                raise ValidationError("后臂同步会话已准备，请启动模型或结束该会话")
            return super().submit(action, params)
        if not isinstance(params, dict):
            raise ValidationError("接管参数需要对象")
        view = self.snapshot()["dagger"]
        expected = {"onsite_confirmed"} if action == "dagger_prepare" else {"binding", "policy_job_id"}
        if set(params) != expected:
            raise ValidationError("接管请求参数不匹配")
        if action != "dagger_prepare" and (params["binding"] != view["binding"]
                or params["policy_job_id"] != view["policy_job_id"]):
            raise ValidationError("右侧控制会话或模型任务已经改变，请刷新状态")
        self.dagger_error = None
        if action == "dagger_prepare":
            if not view["can_prepare"] or params["onsite_confirmed"] is not True:
                raise ValidationError("当前不能准备右后臂")
            self._device_request("prepare", onsite_confirmed=True)
        elif action == "dagger_intervene":
            if not view["can_enter"]:
                raise ValidationError("本轮模型任务暂不能交接")
            job = self._running_policy()
            self.intervention_requests.add(job["id"])
            self._cancel_policy(job)
            self._device_request("intervene", params["binding"], job_id=job["id"])
        elif action == "dagger_resume":
            if self.resume_pending:
                return self.snapshot()["dagger"]
            if not view["can_resume"]:
                raise ValidationError("当前没有可继续的人工纠正会话")
            previous = self.dagger_jobs.get(view["policy_job_id"])
            if previous is None:
                raise ValidationError("缺少原模型任务参数")
            self._device_request("resume", params["binding"], job_id=view["policy_job_id"])
            self.resume_pending = {**deepcopy(previous), "binding": params["binding"], "source_job_id": view["policy_job_id"]}
        else:
            if not view["can_finish"]:
                raise ValidationError("当前交接正在进行，请等状态更新")
            self.resume_pending = None
            self._device_request("finish", params["binding"], job_id=view["policy_job_id"])
        return self.snapshot()["dagger"]

    def _run(self, job):
        bound = None
        if job["action"] in ROUTES:
            device = self._device(super().snapshot())
            if device.get("phase") == "ready" and device.get("available"):
                try:
                    bound = self._device_request("bind_policy", device["binding"],
                                                 job_id=job["id"], task=job["params"]["task"])
                    self.dagger_jobs[job["id"]] = {"action": job["action"], "params": deepcopy(job["params"]),
                                                   "binding": device["binding"]}
                    while len(self.dagger_jobs) > 64:
                        self.dagger_jobs.pop(next(iter(self.dagger_jobs)))
                except Exception as exc:
                    try:
                        self._device_request("policy_finished", device["binding"], job_id=job["id"])
                    except Exception as cleanup_error:
                        self.dagger_error = "模型未启动，后臂会话需核对：" + str(cleanup_error)
                    with self.lock:
                        job.update(state="blocked", error="右后臂准备检查失败：" + str(exc))
                    self.operation.release()
                    return
        try:
            super()._run(job)
        finally:
            if bound:
                try:
                    self._device_request("policy_finished", bound["binding"], job_id=job["id"])
                except Exception as exc:
                    self.dagger_error = "模型已退出，设备交接回执待核对：" + str(exc)
                finally:
                    self.intervention_requests.discard(job["id"])

    def _supervise_dagger(self):
        while not self.dagger_quit.wait(.1):
            try:
                state = super().snapshot()
                device = self._device(state)
                job = self._running_policy()
                if job and job["id"] in self.dagger_jobs and (
                        job["id"] in self.intervention_requests or not device.get("available")
                        or device.get("phase") in ("fault", "unavailable", "idle")):
                    self._cancel_policy(job)
                pending = self.resume_pending
                if pending and device.get("binding") == pending["binding"]:
                    if device.get("policy_job_id") != pending["source_job_id"]:
                        self.resume_pending = None
                        self.dagger_error = "恢复请求的模型任务已改变"
                    elif device.get("phase") == "fault":
                        self.resume_pending = None
                        self.dagger_error = device.get("error")
                    elif device.get("phase") == "ready" and not job:
                        self.resume_pending = None
                        self.submit(pending["action"], pending["params"])
            except Exception as exc:
                self.dagger_error = str(exc)

    def close(self):
        self.dagger_quit.set()
        self.dagger_supervisor.join(timeout=1)
        return super().close()
