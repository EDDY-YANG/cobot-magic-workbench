# Copyright (c) 2026 Yang Taige
# SPDX-License-Identifier: MIT
"""Recover an expired USB selection only at an explicit, idle reconnect.

Installed in the server entry adapter. It does not replace robot source, mutate
STOP, start/stop a producer, or change the original configuration/source guards.
"""
import copy
from contextlib import contextmanager
import hashlib
import queue
import threading
import json
from pathlib import Path
import subprocess
import time
import uuid

ACTIVE = frozenset(("queued", "running", "unknown"))
IDENTITY = ("uuid", "mountpoint", "source", "device", "major_minor", "physical_usb_path")


def same_original_drive(inventory, selected):
    """Require one unambiguous eligible instance of the original filesystem."""
    rows = inventory.get("candidates", [])
    eligible = [row for row in rows if row.get("eligible") is True]
    matches = [row for row in rows if row.get("uuid") == selected.get("uuid")]
    if (len(eligible) != 1 or len(matches) != 1 or matches[0] is not eligible[0]
            or eligible[0].get("mountpoint") != selected.get("mountpoint")
            or any(not isinstance(eligible[0].get(k), str) or not eligible[0][k] for k in IDENTITY)):
        raise RuntimeError("原 U 盘未唯一恢复为可写挂载；请核对原盘，不能自动切换其他盘")
    return eligible[0]


class IdleLease:
    def __init__(self, process, started, proof):
        self.process, self.started, self.proof = process, started, proof

    def check(self, required_s=1):
        if self.process.poll() is not None or time.monotonic() + required_s > self.started + 80:
            raise RuntimeError("远端空闲租约已结束或时间不足；停止恢复 USB 绑定")


@contextmanager
def probe_idle(backend, cfg, root):
    program = Path(__file__).with_name("usb_reconnect_probe.py").read_text(encoding="utf-8")
    lease_id = uuid.uuid4().hex
    payload = {"cfg": cfg, "manifest": backend.pc.sources(root, cfg), "lease_id": lease_id}
    started = time.monotonic()
    process = subprocess.Popen(
        [*backend.pc.ssh_base(cfg), backend.pc.remote_python(cfg, program)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    acquired = False
    try:
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()
        response = queue.Queue(maxsize=1)
        def receive():
            try:
                response.put(process.stdout.readline(65537))
            except Exception as exc:
                response.put(exc)
        threading.Thread(target=receive, daemon=True, name="usb-idle-lease").start()
        try:
            line = response.get(timeout=18)
        except queue.Empty as exc:
            raise RuntimeError("USB 重连预检超时；保留阻挡，请检查连接后重试") from exc
        if isinstance(line, Exception):
            raise line
        if not line:
            raise RuntimeError("旧采集/控制仍在占用，或 USB 身份预检失败；请先结束任务并停止共享采集")
        result = json.loads(line)
        if (result.get("schema") != "tiger-usb-reconnect-idle-v1"
                or result.get("lease_id") != lease_id or result.get("lease_timeout_s") != 90
                or result.get("hostname") != cfg["hostname"] or result.get("idle") is not True
                or result.get("selection") != cfg["usb_selection"]
                or result.get("source_files_verified") != len(payload["manifest"]) - 1):
            raise RuntimeError("USB 重连预检返回不完整，保留阻挡")
        uuid.UUID(result["boot_id"])
        if not isinstance(result.get("physical_serial"), str) or not result["physical_serial"]:
            raise RuntimeError("USB 物理身份未读回，保留阻挡")
        lease = IdleLease(process, started, result)
        lease.check(1)
        acquired = True
        yield lease
    finally:
        if process.poll() is None:
            try:
                process.stdin.write(json.dumps({"release": lease_id}) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        try:
            _, stderr = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
            raise RuntimeError("SSH 空闲租约退出未确认；保留阻挡")
        if acquired and process.returncode:
            raise RuntimeError("远端空闲租约未正常结束：" + stderr[-1200:])


def recover_before_connect(app, job, backend, probe=probe_idle):
    """The existing operation mutex is already held by WebApp.submit."""
    with app.lock:
        if app.storage.get("status") != "lost" and not getattr(app, "_usb_recovery_pending", False):
            return None
        if app.connected:
            raise RuntimeError("请先断开当前监控会话；不能在线更换 USB 身份")
        if any(j is not job and j.get("state") in ACTIVE for j in app.jobs):
            raise RuntimeError("仍有未结束或状态未知的任务，不能恢复 USB 会话")
        app._usb_recovery_pending = True
        selected = copy.deepcopy(app.storage.get("selected"))
        if not selected:
            raise RuntimeError("原 USB 身份缺失，请先核对选盘")
    # Serialize with discovery; automatic polling never calls this recovery.
    if not app.storage_scan_lock.acquire(timeout=20):
        raise RuntimeError("USB 扫描尚未结束，请稍后重试连接")
    try:
        inventory = app.transport.discover_storage()
        candidate = same_original_drive(inventory, selected)
        from agilex_control.usb_storage import config_for_storage
        cfg = config_for_storage(app.base_cfg, candidate)
        # A live bridge may own receivers even after the page lost its connection.
        # close() itself refuses an unconfirmed exit. It never stops the Hub.
        app.transport.close()
        with probe(backend, cfg, app.root) as lease:
            # A fresh inventory must still identify the same original filesystem.
            latest = app.transport.discover_storage()
            current = same_original_drive(latest, selected)
            if any(current.get(k) != candidate.get(k) for k in IDENTITY):
                raise RuntimeError("USB 在恢复检查期间再次变化，请保持原盘连接后重试")
            # Storage-only writes run under the remote owner locks. The existing
            # preparation routine still verifies source, mount and idle state.
            lease.check(31)
            prepared = backend.pc.prepare_session_config(cfg, app.root)
            lease.check(22)
            backend.pc.check_sources(cfg, app.root)
            lease.check(1)
            proof = {**lease.proof, "preparation": prepared}
        # The remote lease exited before the new web-agent acquires its own lock.
        with app.lock:
            if app.connected or any(j is not job and j.get("state") in ACTIVE for j in app.jobs):
                raise RuntimeError("恢复期间出现新的任务或连接，保留原会话阻挡")
            app.cached = {}
            app.cache_at = time.monotonic()
            app._apply_storage(current)
            app.storage.update(candidates=copy.deepcopy(latest["candidates"]),
                               checked_at=latest.get("checked_at"),
                               inventory_schema=latest.get("schema"), error=None,
                               reason="旧会话已退出；正在通过原流程核对源码与当前 USB 身份")
            app.storage_scan_at = time.monotonic()
            job["storage_recovery"] = proof
        return proof
    finally:
        app.storage_scan_lock.release()


def install(backend, probe=probe_idle):
    """Keep original handlers, job history, DAgger subclass and release assets."""
    cls = backend.WebApp
    if getattr(cls, "_usb_reconnect_installed", False):
        return
    original, original_locked, original_snapshot = cls._run, cls._storage_locked, cls.snapshot
    loaded = {"schema": "tiger-usb-reconnect-extension-v1",
              "module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "probe_sha256": hashlib.sha256(Path(__file__).with_name("usb_reconnect_probe.py").read_bytes()).hexdigest()}

    def locked(self, allow_connect=False):
        return bool(getattr(self, "_usb_recovery_pending", False)
                    or original_locked(self, allow_connect=allow_connect))

    def snapshot(self):
        result = original_snapshot(self)
        result["usb_reconnect"] = {**loaded,
                                  "pending": bool(getattr(self, "_usb_recovery_pending", False))}
        return result

    def run(self, job):
        recovered = False
        if job.get("action") == "connect":
            try:
                recovered = recover_before_connect(self, job, backend, probe=probe) is not None
            except Exception as exc:
                with self.lock:
                    job.update(state="blocked", error=str(exc), finished_at=backend.stamp())
                self._event(str(exc), "error")
                self.operation.release()
                return
        original(self, job)
        if recovered:
            with self.lock:
                if job.get("state") == "succeeded" and self.connected:
                    self._usb_recovery_pending = False
                else:
                    self.storage.update(status="lost", runtime_ready=False,
                                        reason="本次 USB 重连未完成；再次连接时将重新核对真实占用")
                    self._usb_recovery_pending = True

    cls._run, cls._storage_locked, cls.snapshot = run, locked, snapshot
    cls._usb_reconnect_installed = True
