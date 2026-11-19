"""GPU ownership gate for the isolated offline experiment; inert on import.

This gate never stops a process, changes a model, or calls an HTTP endpoint.
The existing FP32 locks are opened without creation/truncation and held for
the caller's entire GPU scope. A lease release alone cannot release them.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
from typing import Callable


EXPECTED_HOST = "user-MS-7E06"
EXPECTED_MOUNT = "/data/4090-1tb"
EXPECTED_UUID = "e76043b6-4ab6-42a5-8b06-4963ee92a567"
DATA_ROOT = Path(EXPECTED_MOUNT) / "RobotData/03_Files_and_Code"
LOCK_PATHS = (
    DATA_ROOT / "2026-10-19-15-38-Pi05-FP32-Deployment/FP32_GPU_OWNER.lock",
    DATA_ROOT / "2026-10-19-18-12-H50-Model-Supervisor/FP32_GPU_OWNER.lock",
)
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_SOURCE = EXPERIMENT_ROOT / "frozen_source/gpu_inventory.py"
INVENTORY_SHA256 = "b4a46e34ef9db999b41cd50209ef14398f16ecc5e5149271b84a32313152a6d9"


class OfflineGuardBlocked(RuntimeError):
    """Ownership or identity was not established; receipt is JSON serializable."""

    def __init__(self, code, message, receipt):
        super().__init__(message)
        self.code = code
        self.receipt = receipt
        receipt.update(status="BLOCKED", reason_code=code, reason=message)


def _mount_info():
    raw = subprocess.check_output(
        ["findmnt", "-J", "-T", str(EXPERIMENT_ROOT), "-o", "TARGET,UUID"],
        text=True, timeout=10,
    )
    filesystems = json.loads(raw).get("filesystems", [])
    if len(filesystems) != 1:
        raise ValueError("Expected one experiment filesystem")
    return filesystems[0]


def _flock(stream, acquire):
    # Linux-only import is deliberately deferred; CPU tests inject fake locks.
    import fcntl
    operation = fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN
    fcntl.flock(stream.fileno(), operation)


def _open_existing_lock(path):
    # O_RDWR plus fdopen('r+') is existing-file r+ semantics. No O_CREAT,
    # O_TRUNC, writes, unlink or renaming; O_NOFOLLOW rejects lock symlinks.
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        stream = os.fdopen(descriptor, "r+", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise
    try:
        identity = os.fstat(stream.fileno())
        current = os.lstat(path)
        if not stat.S_ISREG(identity.st_mode) or stat.S_ISLNK(current.st_mode):
            raise ValueError("Existing GPU lock must be a regular non-symlink file")
        if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("GPU lock identity changed during open")
    except BaseException:
        stream.close()
        raise
    return stream


@dataclass(frozen=True)
class _Dependencies:
    """Test seam only. Production callers use the fixed host/mount/locks."""

    hostname: Callable = socket.gethostname
    mount_info: Callable = _mount_info
    lock_paths: tuple = LOCK_PATHS
    open_lock: Callable = _open_existing_lock
    flock: Callable = _flock
    inventory: Callable | None = None


def _inventory(source, expected_sha256):
    source = Path(source)
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("A frozen inventory SHA-256 is required")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise ValueError("Frozen GPU inventory SHA-256 differs")
    spec = importlib.util.spec_from_file_location("tiger_bf16_frozen_inventory", source)
    if spec is None or spec.loader is None:
        raise ValueError("Frozen inventory cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "inventory", None)):
        raise ValueError("Frozen inventory has no read-only inventory() interface")
    # Reuse the exact frozen classification; do not recreate/expand a whitelist.
    return module.inventory()


def _read_inventory(dependencies, source, expected_sha256, receipt):
    try:
        value = (dependencies.inventory() if dependencies.inventory is not None
                 else _inventory(source, expected_sha256))
        if not isinstance(value, dict):
            raise ValueError("GPU inventory must return an object")
        receipt["gpu_inventory"] = value
        if value.get("new_model_allowed") is not True:
            raise OfflineGuardBlocked("GPU_OWNER_PRESENT", "Existing or unverified GPU owner is preserved", receipt)
        if value.get("unknown_compute_owners") != [] or not isinstance(value.get("processes"), list):
            raise ValueError("GPU inventory permission contradicts owner evidence")
        return value
    except OfflineGuardBlocked:
        raise
    except Exception as exc:
        raise OfflineGuardBlocked("GPU_INVENTORY_UNAVAILABLE", type(exc).__name__ + ": GPU ownership could not be verified", receipt) from exc


def _unlock(stream, flock):
    try:
        flock(stream, False)
    finally:
        stream.close()


@contextmanager
def acquire_gpu_scope(*, inventory_source=None, expected_inventory_sha256=None, _dependencies=None):
    """Acquire both existing FP32 locks, then verify frozen GPU owner inventory.

    Success permits only the caller's explicitly authorized offline GPU work;
    it never authorizes robot access. All handles are released on every exit.
    A conflict raises OfflineGuardBlocked without changing the existing owner.
    """
    dependencies = _dependencies or _Dependencies()
    source = INVENTORY_SOURCE if inventory_source is None else Path(inventory_source)
    digest = INVENTORY_SHA256 if expected_inventory_sha256 is None else expected_inventory_sha256
    receipt = {
        "schema": "tiger-bf16-offline-gpu-guard-v1", "status": "CHECKING",
        "hardware_commands": 0, "http_requests": 0, "model_started_by_guard": False,
        "existing_owners_stopped": False, "locks_held": False, "locks": [],
        "inventory_source": str(source), "inventory_source_sha256": digest,
    }
    with ExitStack() as stack:
        try:
            receipt["host"] = dependencies.hostname()
            if receipt["host"] != EXPECTED_HOST:
                raise OfflineGuardBlocked("WRONG_HOST", "Expected host " + EXPECTED_HOST, receipt)
            receipt["mount"] = dependencies.mount_info()
            if receipt["mount"].get("target") != EXPECTED_MOUNT or receipt["mount"].get("uuid") != EXPECTED_UUID:
                raise OfflineGuardBlocked("WRONG_FILESYSTEM", "Experiment mountpoint/UUID differs", receipt)
            if len(dependencies.lock_paths) != 2 or len(set(map(str, dependencies.lock_paths))) != 2:
                raise OfflineGuardBlocked("INVALID_LOCK_SET", "Two distinct existing FP32 owner locks are required", receipt)
            for path in dependencies.lock_paths:
                try:
                    stream = dependencies.open_lock(path)
                except Exception as exc:
                    raise OfflineGuardBlocked("LOCK_UNAVAILABLE", "Existing GPU lock cannot be opened: " + str(path), receipt) from exc
                stack.callback(stream.close)
                try:
                    dependencies.flock(stream, True)
                except Exception as exc:
                    raise OfflineGuardBlocked("GPU_LOCK_BUSY", "Existing GPU owner lock is held or unavailable: " + str(path), receipt) from exc
                stack.callback(_unlock, stream, dependencies.flock)
                identity = os.fstat(stream.fileno())
                current = os.lstat(path)
                if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                    raise OfflineGuardBlocked("LOCK_IDENTITY_CHANGED", "GPU lock pathname changed", receipt)
                receipt["locks"].append({"path": str(path), "device": identity.st_dev, "inode": identity.st_ino})
            receipt["locks_held"] = True
            _read_inventory(dependencies, source, digest, receipt)
        except OfflineGuardBlocked:
            receipt["locks_held"] = False
            raise
        except Exception as exc:
            receipt["locks_held"] = False
            raise OfflineGuardBlocked("GUARD_CHECK_FAILED", type(exc).__name__ + ": host/mount/lock check failed", receipt) from exc
        receipt["status"] = "GPU_SCOPE_ACQUIRED_NO_MODEL_STARTED"
        try:
            yield receipt
        finally:
            receipt["locks_held"] = False


def probe_only(**kwargs):
    """Return READY/BLOCKED without model loading or persisting GPU logs.

    If lock contention prevents ownership, a frozen read-only inventory is
    attached for diagnosis only; it cannot override the failed lock decision.
    """
    try:
        with acquire_gpu_scope(**kwargs) as receipt:
            pass
        receipt.update(status="READY_NO_MODEL_STARTED", locks_released=True)
        return receipt
    except OfflineGuardBlocked as exc:
        receipt = exc.receipt
        if exc.code == "GPU_LOCK_BUSY" and "gpu_inventory" not in receipt:
            dependencies = kwargs.get("_dependencies") or _Dependencies()
            source = kwargs.get("inventory_source") or INVENTORY_SOURCE
            digest = kwargs.get("expected_inventory_sha256") or INVENTORY_SHA256
            try:
                _read_inventory(dependencies, source, digest, receipt)
            except OfflineGuardBlocked as inventory_error:
                receipt["inventory_diagnostic_error"] = inventory_error.code
            # Inventory is diagnostic and must never replace the lock failure.
            receipt.update(status="BLOCKED", reason_code=exc.code, reason=str(exc),
                           inventory_is_diagnostic_only=True)
        receipt["locks_released"] = True
        return receipt


if __name__ == "__main__":
    value = probe_only()
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2))
    raise SystemExit(0 if value["status"] == "READY_NO_MODEL_STARTED" else 2)
