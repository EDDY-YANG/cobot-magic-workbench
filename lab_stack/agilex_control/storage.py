# 完整单行命令：Windows PC PowerShell；无需预定义变量，复制时去掉行首 #。
# 按需选择一条命令，不要把所有示例一起执行。
# # 存储与停止闩锁模块，经统一入口调用；不直接运行本文件。
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' stop
# # 查看实际状态并准备新会话后，显式清除两处停止闩锁：
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' clear-stop
# # clear-stop 本身不移动，也不恢复电机/故障。
# 本轮仅补充命令注释；未运行测试、SSH、模型、相机或机械臂。
"""Verified USB evidence, advisory ownership, and a persistent software stop latch."""
import datetime as dt
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
import socket
import subprocess
import uuid
import sys
from contextlib import contextmanager
from .usb_storage import verify_frozen_identity, require_usb_path, open_usb_directory

CHINA = dt.timezone(dt.timedelta(hours=8))
_EPISODE_LABEL = re.compile(r'([0-9]{6,7})-(A|B|C|D|random|recovery|test)')


def validate_episode_label(value):
    """Validate a frozen collection ordinal/category, never an arbitrary path."""
    match = _EPISODE_LABEL.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError('Invalid numbered episode label')
    ordinal, category = int(match[1]), match[2]
    if not 1 <= ordinal <= 1000000 or value != f'{ordinal:06d}-{category}':
        raise ValueError('Invalid numbered episode label')
    return ordinal, category


def now():
    return dt.datetime.now(CHINA).isoformat()


def verify_usb(cfg):
    if socket.gethostname() != cfg["hostname"]:
        raise RuntimeError("Wrong host; refusing robot/storage access")
    root = Path(cfg["usb"])
    if not root.is_mount():
        raise RuntimeError("USB is not actually mounted; no internal-disk fallback")
    mount = json.loads(subprocess.check_output(
        ["findmnt", "-J", "-T", str(root), "-o", "TARGET,SOURCE,FSTYPE,UUID,OPTIONS,MAJ:MIN"],
        text=True, timeout=5))["filesystems"][0]
    if (mount["target"] != str(root) or str(mount.get("uuid", "")).upper() != cfg["usb_uuid"]
            or "rw" not in mount.get("options", "").split(",")):
        raise RuntimeError("USB identity or writable mount mismatch: " + repr(mount))
    verify_frozen_identity(cfg, mount)
    return root


def write_json(path, value, cfg):
    root = verify_usb(cfg)
    path = Path(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("Evidence must be within the verified USB")
    if cfg.get('usb_selection'):
        parent_fd = open_usb_directory(path.parent, cfg)
        name = '.' + path.name + '-' + uuid.uuid4().hex + '.tmp'
        temporary_exists = False
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent_fd)
            temporary_exists = True
            if os.fstat(descriptor).st_dev != os.fstat(parent_fd).st_dev:
                os.close(descriptor)
                raise RuntimeError('Evidence file crossed outside selected USB filesystem')
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            verify_usb(cfg)
            os.replace(name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temporary_exists = False
        finally:
            if temporary_exists:
                try:
                    os.unlink(name, dir_fd=parent_fd)
                except OSError:
                    pass
            os.close(parent_fd)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                prefix='.'+path.name+'-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        verify_usb(cfg)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Session:
    """Event logs, not frame-rate trace logging. Raw streams use a separate queue."""
    def __init__(self, cfg, operation, *, episode_label=None):
        self.cfg = copy.deepcopy(cfg)
        cfg = self.cfg
        self._save_defer_depth = 0
        if episode_label is not None:
            if operation != 'episode':
                raise ValueError('Numbered episode label requires episode operation')
            validate_episode_label(episode_label)
        root = verify_usb(cfg)
        label = '' if episode_label is None else '-' + episode_label
        name = dt.datetime.now(CHINA).strftime("%Y-%m-%d-%H-%M-%S") + "-" + operation + label + "-" + uuid.uuid4().hex[:8]
        self.path = require_usb_path(Path(cfg["remote_bundle"]) / "runs" / name, cfg, root)
        if cfg.get('usb_selection'):
            descriptor = open_usb_directory(self.path, cfg, create=True, exclusive_final=True)
            os.close(descriptor)
        else:
            self.path.mkdir(parents=True, exist_ok=False)
        self.report = {"operation": operation, "started_at": now(), "status": "STARTED",
                       "raw_storage": str(self.path), "events": [], "software_version": "0.1.0",
                       "storage_identity": {"mountpoint": cfg.get('usb'), "uuid": cfg.get('usb_uuid'),
                           "selection": copy.deepcopy(cfg.get('usb_selection'))}}
        self.save()

    def event(self, name, **fields):
        self.report["events"].append({"at": now(), "event": name, **fields})
        self.save()

    def save(self):
        if self._save_defer_depth:
            return  # Bounded in-memory policy evidence; recorder writes separately.
        write_json(self.path / "operation.json", self.report, self.cfg)

    @contextmanager
    def defer_persistence(self):
        """Keep phase events in RAM until the authorized motion handoff ends.

        This does not suspend the independent raw recorder. The final save may
        fail and remains an evidence failure; no internal-disk fallback exists.
        """
        self._save_defer_depth += 1
        try:
            yield self
        finally:
            self._save_defer_depth -= 1
            if not self._save_defer_depth:
                original = sys.exc_info()[1]
                try:
                    self.save()
                except Exception as exc:
                    if original is None:
                        raise
                    self.report["deferred_evidence_write_error"] = repr(exc)

    def finish(self, status, **fields):
        self.report.update(status=status, ended_at=now(), **fields)
        self.save()
        return self.report


class Ownership:
    """An advisory process lock. Cannot block legacy programs or CAN leaders."""
    def __init__(self, name):
        self.name, self.handle = name, None

    def __enter__(self):
        import fcntl
        directory = Path("/run/user") / str(os.getuid()) / "tiger-agilex-unified"
        directory.mkdir(mode=0o700, exist_ok=True)
        self.handle = (directory / (self.name + ".lock")).open("a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise RuntimeError("Operation already owned: " + self.name)
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps({"pid": os.getpid(), "name": self.name, "at": now()}))
        self.handle.flush()
        return self

    def __exit__(self, *_):
        if self.handle:
            self.handle.close()


def latch_path(cfg):
    return Path(cfg["remote_bundle"]) / "STOP"


def runtime_latch_path():
    """RAM-backed stop channel still works after USB removal; not a data store."""
    directory = Path("/run/user") / str(os.getuid()) / "tiger-agilex-unified"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / "STOP"


def stop_requested(cfg):
    return runtime_latch_path().exists() or latch_path(cfg).exists()


def stop_status(cfg):
    """Read both latches without creating directories or changing robot state."""
    result = {"runtime": None, "usb": None, "latched": None, "checked_at": now()}
    try:
        paths = {"runtime": Path("/run/user") / str(os.getuid()) / "tiger-agilex-unified/STOP",
                 "usb": latch_path(cfg)}
        for key, path in paths.items():
            try:
                path.stat()
                result[key] = True
            except FileNotFoundError:
                result[key] = False
        result["latched"] = any(result[key] for key in paths)
    except Exception as exc:
        result["error"] = repr(exc)
    return result


def require_not_stopped(cfg):
    if stop_requested(cfg):
        raise RuntimeError("STOP latch present. Use clear-stop only when ready for a new session")


def stop(cfg):
    if socket.gethostname() != cfg["hostname"]:
        raise RuntimeError("Wrong host; refusing stop channel write")
    runtime_latch_path().write_text(now() + "\n", encoding="utf-8")
    usb_saved, usb_error = False, None
    try:
        verify_usb(cfg)
        latch_path(cfg).write_text(now() + "\n", encoding="utf-8")
        usb_saved = True
    except Exception as exc:
        usb_error = repr(exc)
    return {"status": "SOFTWARE_STOP_LATCHED", "motors_disabled": False,
            "runtime_stop_written": True, "usb_stop_written": usb_saved, "usb_error": usb_error,
            "warning": "No new targets from this library; last accepted controller target can finish. Not hardware E-stop."}


def clear_stop(cfg):
    verify_usb(cfg)
    latch_path(cfg).unlink(missing_ok=True)
    runtime_latch_path().unlink(missing_ok=True)
    return {"status": "LATCH_CLEARED", "motion_started": False}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
