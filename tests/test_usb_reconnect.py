"""Regression and refusal tests for explicit USB reconnect; no hardware access."""
import copy
from collections import deque
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lab_stack"))
from agilex_control import web_backend
from agilex_control.usb_storage import config_for_storage

spec = importlib.util.spec_from_file_location("usb_recovery", ROOT / "tools/workbench_usb_recovery.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def candidate(device="/dev/sdb1", physical="/sys/devices/usb2/2-8", uuid="AAAA-BBBB"):
    return dict(uuid=uuid, mountpoint="/media/robot/AAAA-BBBB",
                source=device, device=device, major_minor="8:17" if device.endswith("b1") else "8:1",
                physical_usb_path=physical, eligible=True, filesystem="exfat",
                runtime_bundle_exists=True)


def inventory(row=None):
    return {"schema": "tiger-usb-inventory-v1", "checked_at": "fixture",
            "candidates": [candidate() if row is None else row]}


class Transport:
    def __init__(self):
        self.calls, self.inventories = [], []
        self.connect_error = None
        self.close_error = None

    def discover_storage(self):
        self.calls.append("discover")
        return copy.deepcopy(self.inventories.pop(0) if self.inventories else inventory())

    def close(self):
        self.calls.append("close")
        if self.close_error:
            raise self.close_error

    def connect(self):
        self.calls.append("connect")
        if self.connect_error:
            raise self.connect_error
        return {"usb": {"verified": True}, "identity": {"hostname": "fixture-robot"}}


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        class App(web_backend.WebApp):
            pass
        self.App = App
        self.lease_active = False
        self.probe_calls = 0
        self.probe_error = None
        self.prepare_error = None

        @contextmanager
        def probe(backend, cfg, root):
            self.probe_calls += 1
            if self.probe_error:
                raise self.probe_error
            self.lease_active = True
            try:
                yield SimpleNamespace(proof={"idle": True, "fixture": True}, check=lambda budget: None)
            finally:
                self.lease_active = False

        def prepare(cfg, root):
            self.assertTrue(self.lease_active, "Storage refresh must occur under remote owner lease")
            if self.prepare_error:
                raise self.prepare_error
            return {"status": "SESSION_STORAGE_IDENTITY_REFRESHED"}

        def check(cfg, root):
            self.assertTrue(self.lease_active, "Source readback must finish before releasing owner lease")

        self.backend = SimpleNamespace(WebApp=App, stamp=web_backend.stamp,
                                       pc=SimpleNamespace(prepare_session_config=Mock(side_effect=prepare),
                                                          check_sources=Mock(side_effect=check)))
        recovery.install(self.backend, probe=probe)
        app = object.__new__(App)
        app.lock, app.operation = threading.RLock(), threading.Lock()
        app.storage_scan_lock = threading.Lock()
        app.connected, app.precision = False, None
        app.base_cfg = {"hostname": "fixture-robot", "usb": "/media/robot/AAAA-BBBB",
                        "usb_uuid": "AAAA-BBBB", "remote_bundle": "/media/robot/AAAA-BBBB/runtime"}
        old = candidate("/dev/sda1", "/sys/devices/usb1/1-6")
        app.cfg = config_for_storage(app.base_cfg, old)
        app.cached = {"hub": {"api_reachable": True, "producer": {"pid": 42}}}
        app.cache_at = time.monotonic() - 300
        app.root = ROOT / "lab_stack"
        app.storage = {"status": "lost", "selected": old, "runtime_ready": False,
                       "candidates": [candidate()], "scanning": False}
        app.storage_scan_at = None
        app.jobs = deque([{"id": "old-failure", "state": "blocked", "action": "connect",
                           "error": "old USB identity lost"}])
        app.events = deque()
        app.transport = Transport()
        self.app = app

    def connect(self):
        job = {"id": str(len(self.app.jobs)), "action": "connect", "state": "queued"}
        self.app.jobs.append(job)
        self.assertTrue(self.app.operation.acquire(blocking=False))
        self.app._run(job)
        self.assertFalse(self.app.operation.locked())
        return job

    def test_explicit_reconnect_after_real_idle_preserves_failure_history(self):
        job = self.connect()
        self.assertEqual(job["state"], "succeeded")
        self.assertTrue(self.app.connected)
        self.assertTrue(self.app.storage["runtime_ready"])
        self.assertEqual(self.app.cfg["usb_selection"]["device"], "/dev/sdb1")
        self.assertEqual(self.app.jobs[0]["error"], "old USB identity lost")
        self.assertEqual(self.probe_calls, 1)
        self.assertFalse(self.app._usb_recovery_pending)
        self.assertFalse(self.lease_active)
        self.assertEqual(self.app.transport.calls, ["discover", "close", "discover", "connect"])

    def test_automatic_scan_does_not_close_or_probe_or_unlock(self):
        self.app._scan_storage()
        self.assertEqual(self.app.storage["status"], "lost")
        self.assertEqual(self.probe_calls, 0)
        self.assertEqual(self.app.transport.calls, ["discover"])

    def test_same_exact_reinserted_identity_can_leave_lost_only_on_reconnect(self):
        old = self.app.storage["selected"]
        self.app.transport.inventories = [inventory(old), inventory(old)]
        self.assertEqual(self.connect()["state"], "succeeded")
        self.assertEqual(self.app.storage["status"], "selected")

    def test_live_owner_and_unreachable_probe_both_leave_session_locked(self):
        for error in (RuntimeError("camera-owner busy"), subprocess.TimeoutExpired("ssh", 18)):
            with self.subTest(error=str(error)):
                self.probe_error = error
                job = self.connect()
                self.assertEqual(job["state"], "blocked")
                self.assertFalse(self.app.connected)
                self.assertEqual(self.app.storage["status"], "lost")
                self.assertTrue(self.app._storage_locked())
                self.backend.pc.prepare_session_config.assert_not_called()
                self.assertNotIn("connect", self.app.transport.calls)

    def test_unknown_or_running_control_job_blocks_before_any_remote_work(self):
        for state in ("queued", "running", "unknown"):
            with self.subTest(state=state):
                busy = {"id": "control", "action": "deploy", "state": state}
                self.app.jobs.append(busy)
                self.assertEqual(self.connect()["state"], "blocked")
                self.app.jobs.remove(busy)
                self.assertEqual(self.app.transport.calls, [])

    def test_bridge_close_must_be_confirmed(self):
        self.app.transport.close_error = RuntimeError("bridge exit unknown")
        self.assertEqual(self.connect()["state"], "blocked")
        self.assertEqual(self.probe_calls, 0)
        self.assertEqual(self.app.storage["status"], "lost")

    def test_changed_mount_during_probe_does_not_write_configuration(self):
        changed = candidate("/dev/sdc1", "/sys/devices/usb3/3-2")
        self.app.transport.inventories = [inventory(), inventory(changed)]
        self.assertEqual(self.connect()["state"], "blocked")
        self.backend.pc.prepare_session_config.assert_not_called()
        self.assertFalse(self.lease_active)

    def test_prepare_failure_keeps_lost_and_retry_repeats_owner_check(self):
        self.prepare_error = RuntimeError("owner appeared or source changed")
        self.assertEqual(self.connect()["state"], "blocked")
        self.assertFalse(self.app.connected)
        self.assertEqual(self.app.storage["status"], "lost")
        self.assertFalse(self.lease_active)
        self.prepare_error = None
        self.assertEqual(self.connect()["state"], "succeeded")
        self.assertEqual(self.probe_calls, 2)

    def test_original_connect_failure_keeps_lost_for_next_attempt(self):
        self.app.transport.connect_error = RuntimeError("final mount/source check failed")
        self.assertEqual(self.connect()["state"], "failed")
        self.assertFalse(self.app.connected)
        self.assertFalse(self.app.storage["runtime_ready"])
        self.assertEqual(self.app.storage["status"], "lost")
        self.app.storage_scan_at = None
        self.app._scan_storage()
        self.assertEqual(self.app.storage["status"], "lost")
        self.app.transport.connect_error = None
        self.assertEqual(self.connect()["state"], "succeeded")
        self.assertEqual(self.probe_calls, 2)

    def test_retry_in_original_handler_release_window_still_requires_lease(self):
        # Original _run releases operation before the adapter restores lost.
        self.app.storage["status"] = "selected"
        self.app._usb_recovery_pending = True
        self.probe_error = RuntimeError("new writer owns the device")
        self.assertEqual(self.connect()["state"], "blocked")
        self.assertEqual(self.probe_calls, 1)
        self.assertNotIn("connect", self.app.transport.calls)
        self.assertTrue(self.app._storage_locked())

    def test_normal_connection_does_not_enter_recovery(self):
        self.app.storage["status"] = "selected"
        self.app.storage_scan_at = time.monotonic()
        self.assertEqual(self.connect()["state"], "succeeded")
        self.assertEqual(self.probe_calls, 0)
        self.assertEqual(self.app.transport.calls, ["connect"])


class SelectionTests(unittest.TestCase):
    def test_wrong_readonly_duplicate_and_multiple_disks_refused(self):
        original = candidate()
        cases = [
            {"candidates": []},
            inventory(candidate(uuid="OTHER-UUID")),
            inventory({**candidate(), "eligible": False}),
            {"candidates": [candidate(), candidate()]},
            {"candidates": [candidate(), candidate(uuid="OTHER-UUID")]},
            inventory({**candidate(), "mountpoint": "/another/mount"}),
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                recovery.same_original_drive(data, original)

    def test_expired_or_exited_lease_refuses_write_window(self):
        alive = SimpleNamespace(poll=lambda: None)
        lease = recovery.IdleLease(alive, time.monotonic() - 60, {})
        with self.assertRaises(RuntimeError):
            lease.check(31)
        exited = recovery.IdleLease(SimpleNamespace(poll=lambda: 1), time.monotonic(), {})
        with self.assertRaises(RuntimeError):
            exited.check(1)

    def test_wrong_host_remote_probe_fails_before_accessing_any_device(self):
        run = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "tools/usb_reconnect_probe.py")],
                             input='{"cfg":{"hostname":"nonexistent-test-robot"},"manifest":{},"lease_id":"' + "a"*32 + '"}\n',
                             text=True, capture_output=True, timeout=3)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("Wrong robot host", run.stderr)
        self.assertEqual(run.stdout, "")


if __name__ == "__main__":
    unittest.main()
