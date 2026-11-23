# Copyright (c) 2026 Yang Taige
# SPDX-License-Identifier: MIT
"""Bounded remote metadata probe. No camera/CAN sockets or robot commands."""
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import select
import sys
from contextlib import ExitStack
import fcntl


def main():
    raw = sys.stdin.buffer.readline(4194305)
    if len(raw) > 4194304 or not raw.endswith(b"\n"):
        raise RuntimeError("Invalid reconnect request")
    data = json.loads(raw)
    cfg, expected = data["cfg"], data["manifest"]
    lease_id = data["lease_id"]
    if not isinstance(lease_id, str) or len(lease_id) != 32:
        raise RuntimeError("Invalid lease ID")
    if socket.gethostname() != cfg["hostname"]:
        raise RuntimeError("Wrong robot host")
    root = Path(cfg["remote_bundle"])
    if not root.is_absolute() or root.resolve() != root or Path(cfg["usb"]) not in root.parents:
        raise RuntimeError("Runtime path redirected")
    config_name = "2026-10-06-14-05-config.json"
    actual = {}
    for name in expected:
        if name == config_name:
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Invalid source member")
        path = root / relative
        if path.resolve() != path:
            raise RuntimeError("Runtime source redirected")
        actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Only import modules after their delivered bytes match the server manifest.
    required = {name: digest for name, digest in expected.items() if name != config_name}
    if actual != required:
        raise RuntimeError("Robot source differs; explicit source review required")
    sys.path.insert(0, str(root))
    from agilex_control.storage import verify_usb
    from agilex_control.usb_storage import validate_session_config_refresh
    previous = json.loads((root / config_name).read_text())
    validate_session_config_refresh(previous, cfg, actual, expected)
    verify_usb(cfg)
    physical = Path(cfg["usb_selection"]["physical_usb_path"])
    serial = (physical / "serial").read_text().strip()
    if not serial:
        raise RuntimeError("USB physical serial unavailable")

    # Existing producer/writer locks, held during the idle observation. These
    # remain advisory, matching the existing runtime ownership contract.
    names = {"camera-owner", "web-agent", "service-launch-hub-serve"}
    interfaces = {entry["interface"] for entry in cfg["can"].values()}
    interfaces.update(p.name for p in Path("/sys/class/net").iterdir() if p.name.startswith("can"))
    names.update("writer-" + interface for interface in interfaces)
    directory = Path("/run/user") / str(os.getuid()) / "tiger-agilex-unified"
    directory.mkdir(mode=0o700, exist_ok=True)
    with ExitStack() as stack:
        for name in sorted(names):
            if "/" in name or ".." in name:
                raise RuntimeError("Invalid owner lock")
            handle = stack.enter_context((directory / (name + ".lock")).open("a+"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("生产者仍在占用 " + name + "；请先结束任务并停止共享采集") from exc
        with socket.socket() as probe:
            probe.settimeout(.5)
            outcome = probe.connect_ex(("127.0.0.1", cfg["hub_port"]))
        if outcome != errno.ECONNREFUSED:
            raise RuntimeError("共享采集仍在运行或无法确认退出；不能解除 USB 会话锁")
        entries = {"2026-10-06-14-05-agilex.py", "2026-10-06-15-57-web.py",
                   "run_apply.py", "entry_apply.py", "run_nosend.py", "fp32_entry.py"}
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                if proc.stat().st_uid != os.getuid():
                    continue
                args = (proc / "cmdline").read_bytes().split(b"\0")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as exc:
                raise RuntimeError("Cannot verify process ownership") from exc
            if any(Path(os.fsdecode(arg)).name in entries for arg in args if arg):
                raise RuntimeError("控制或采集会话尚未退出；不能重新绑定 USB")
        verify_usb(cfg)
        print(json.dumps({
            "schema": "tiger-usb-reconnect-idle-v1", "idle": True,
            "lease_id": lease_id, "lease_timeout_s": 90,
            "hostname": socket.gethostname(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "selection": cfg["usb_selection"], "physical_serial": serial,
            "source_files_verified": len(actual), "checked_locks": sorted(names),
            "CAN_commands_sent": 0, "producers_started_or_stopped": 0,
        }, ensure_ascii=False), flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], 90)
        if not ready:
            raise RuntimeError("Idle lease expired")
        release = json.loads(sys.stdin.buffer.readline(1025))
        if release != {"release": lease_id}:
            raise RuntimeError("Idle lease release missing or invalid")


if __name__ == "__main__":
    main()
