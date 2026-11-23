"""Offline workflow with scripted hardware and capture boundaries."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_right_dagger import Observer
from agilex_control import right_dagger as dg
from agilex_control.hardware import DOCUMENTED_COMMAND_LIMITS_DEG, HardwareBlocked, validate_target_deg
from agilex_control.web_agent import RobotAgent


class TestObserver(Observer):
    __test__ = False
    registry = {}

    def __init__(self, interface, expected_serial, stale_after=.2):
        self.interface = interface
        self.serial = expected_serial
        self.frames = {}
        self.origin = False
        self.q = [0., 20., -20., 0., 0., 0.]
        self.g = .03
        self.enabled = True
        self.registry[interface] = self

    def start(self):
        return self

    def stop(self):
        pass

    def control_snapshot(self):
        now = time.monotonic()
        return {"interface": self.interface, "q_deg": self.q[:], "grip_m": self.g,
                "enabled": [self.enabled]*6, "motor_flags": [0x40]*6,
                "status_hex": "0100010000000000", "fresh": True, "feedback_fresh": True,
                "age_joint_s": 0., "age_motor_s": 0., "age_status_s": 0., "age_gripper_s": 0.,
                "identity": {"identity_verified": True, "serial": self.serial},
                "raw": {hex(cid): {"monotonic_s": now} for cid in (0x2A1,0x2A5,0x2A6,0x2A7,0x2A8)}}

    def inject_target(self, q, g, local):
        import struct
        now = time.monotonic()
        self.origin = local
        for i, cid in enumerate((0x155,0x156,0x157)):
            self.frames[cid] = (now, struct.pack(">ii", round(q[2*i]*1000), round(q[2*i+1]*1000)))
        self.frames[0x159] = (now, round(g*1e6).to_bytes(4, "big", signed=True) + bytes((0,10,1,0)))


class Writer:
    calls = []

    def __init__(self, cfg, snapshot, event, send_guard=None):
        self.cfg, self.snapshot, self.guard = cfg, snapshot, send_guard
        self.observer = TestObserver.registry[cfg["interface"]]

    def connect_for_apply(self, **scope):
        self.calls.append(("connect", self.cfg["interface"], scope))

    def close(self):
        self.calls.append(("close", self.cfg["interface"]))

    def configure_role(self, role, frame, companion_tick=None):
        self.guard()
        self.calls.append(("role", self.cfg["interface"], role))
        return {"ack_monotonic_s": time.monotonic(), "role_requested": role}

    def query_limits(self, frame, companion_tick=None):
        return {"limits": [{"minimum_deg": lo, "maximum_deg": hi} for lo,hi in DOCUMENTED_COMMAND_LIMITS_DEG]}

    def handoff_can_movej(self, companion_tick=None):
        self.guard()

    def feedback(self, **kwargs):
        return self.snapshot()

    def keep_control_mode(self):
        self.guard()
        self.calls.append(("heartbeat", self.cfg["interface"]))

    def prime_measured_hold(self):
        self.calls.append(("prime", self.cfg["interface"]))
        return self.snapshot()

    def enable(self, companion_tick=None):
        self.calls.append(("enable", self.cfg["interface"]))

    def execute_pose(self, q, **kwargs):
        self.guard()
        kwargs["companion_tick"]()
        self.observer.q = list(validate_target_deg(q, self.cfg["command_limits_deg"]))

    def send_joint_target_deg(self, q):
        self.guard()
        self.observer.q = list(validate_target_deg(q, self.cfg["command_limits_deg"]))
        self.calls.append(("joint", self.cfg["interface"], q[:]))

    def send_gripper_m(self, g, **kwargs):
        self.guard()
        self.observer.g = g
        self.calls.append(("gripper", self.cfg["interface"], g))


class Owner:
    held = {}

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        if self.name in self.held:
            raise RuntimeError("Writer already held")
        self.held[self.name] = self
        return self

    def __exit__(self, *args):
        if self.held.get(self.name) is self:
            del self.held[self.name]


class Receipt:
    sequence = 0

    def __init__(self, cfg, operation):
        self.cfg = cfg
        type(self).sequence += 1
        self.path = Path(cfg["usb"]) / str(self.sequence)
        self.path.mkdir()
        self.report = {}

    def save(self):
        pass

    def event(self, name, **fields):
        self.report.setdefault("events", []).append((name, fields))


class Evidence:
    def __init__(self, cfg, path):
        self.path = path
        self.path.write_text("offline hardware test evidence\n")
        self.records = []

    def start(self):
        pass

    def enqueue(self, data):
        self.records.append(deepcopy(data))
        return True

    def status(self):
        return {"opened": True, "writer_error": None}

    def close(self):
        return {"timing_evidence_incomplete": False}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        Owner.held = {}
        Writer.calls = []
        TestObserver.registry = {}
        self.front = TestObserver("front-test", "front-serial")
        cfg = {
            "usb": str(self.root), "remote_bundle": str(self.root), "capture_profile": "right_only_v1",
            "can": {"right": {"interface": "front-test", "expected_serial": "front-serial"}},
            "cameras": {"front": "front-camera", "right": "right-camera"},
            "native_speed_percent": 40, "gripper_bounds_m": [0., .1], "gripper_effort_mNm": 1000,
            "pose_speed_deg_s": 10,
            "right_dagger": {"enabled": True, "physical_isolation_confirmed": True,
                             "rear": {"interface": "rear-test", "expected_serial": "rear-serial"}}
        }
        self.capture = {"active": False}
        self.saved_paths = []
        patches = [
            patch.object(dg, "CanObserver", TestObserver), patch.object(dg, "PiperAdapter", Writer),
            patch.object(dg, "Ownership", Owner), patch.object(dg, "Session", Receipt),
            patch.object(dg, "TxEvidenceWriter", Evidence), patch.object(dg, "stop_requested", return_value=False),
            patch.object(dg, "verify_usb", return_value=self.root),
            patch.object(dg, "require_gripper_feedback", return_value={"healthy": True, "enabled": True}),
            patch.object(dg, "adapter_identity", side_effect=lambda interface, serial:
                         {"interface": interface, "serial": serial, "identity_verified": True}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.service = dg.RightDaggerService(cfg, lambda: self.front, self.capture_state)
        self.service._hub = self.hub
        self.addCleanup(self.service.close)
        self.agent = RobotAgent.__new__(RobotAgent)
        self.agent.closing = threading.Event()
        self.agent.right_dagger = self.service

    def capture_state(self):
        return {"api_reachable": True, "age_s": 0., "recording": deepcopy(self.capture)}

    def hub(self, path="/status?compact=1", payload=None, *, maintain=True):
        if path.startswith("/status"):
            return {"recording": deepcopy(self.capture), "task_state": {"active": False}}
        if path == "/record/start":
            self.capture = {"active": True, "path": str(self.root/"episode"),
                            "right_dagger": deepcopy(payload["dagger_context"])}
            return deepcopy(self.capture)
        if path == "/record/stop":
            assert payload["expected_path"] == self.capture["path"]
            self.capture["active"] = False
            self.saved_paths.append(payload["expected_path"])
            return {"finalization": {"healthy": True, "completed": True, "owned_path": payload["expected_path"]}}
        raise AssertionError(path)

    def request(self, action, binding=None, **data):
        # Same JSON-compatible request envelope used by the SSH bridge.
        return self.agent.dispatch({"id": "offline-rpc", "op": "right_dagger",
                                    "action": action, "binding": binding, "data": data})

    def wait_phase(self, expected):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            value = self.service.snapshot()
            if value["phase"] == expected:
                return value
            if value["phase"] == "fault":
                self.fail(value.get("error"))
            time.sleep(.01)
        self.fail("phase timed out: " + repr(self.service.snapshot()))

    def prepare(self):
        self.request("prepare", onsite_confirmed=True)
        return self.wait_phase("ready")["binding"]

    def test_prepare_model_mirror_human_save_resume(self):
        binding = self.prepare()
        rear_calls = [x[0] for x in Writer.calls if x[1] == "rear-test"]
        self.assertLess(rear_calls.index("prime"), rear_calls.index("enable"))
        self.request("bind_policy", binding, job_id="b"*12, task="offline task")
        self.front.inject_target(self.front.q, self.front.g, True)
        time.sleep(.04)
        self.assertTrue(any(x[:2] == ("joint", "rear-test") for x in Writer.calls))
        self.request("intervene", binding, job_id="b"*12)
        self.assertEqual(self.service.snapshot()["phase"], "waiting_policy")
        self.assertNotIn(("role", "rear-test", "leader"), Writer.calls)
        self.request("policy_finished", binding, job_id="b"*12)
        self.wait_phase("human")
        rear = TestObserver.registry["rear-test"]
        q = self.front.q[:]
        q[0] += .1
        rear.q = q[:]
        rear.inject_target(q, rear.g, False)
        time.sleep(.04)
        self.assertTrue(any(x[:2] == ("joint", "front-test") for x in Writer.calls))
        self.request("resume", binding, job_id="b"*12)
        self.wait_phase("ready")
        self.assertTrue(self.saved_paths)
        self.assertEqual(set(Owner.held), {"writer-rear-test"})
        self.assertEqual(Writer.calls.count(("role", "rear-test", "leader")), 1)
        self.assertEqual(Writer.calls.count(("role", "rear-test", "follower")), 2)

    def test_front_owner_must_exit_before_human_role(self):
        binding = self.prepare()
        self.request("bind_policy", binding, job_id="b"*12, task="offline task")
        self.request("intervene", binding, job_id="b"*12)
        with Owner("writer-front-test"):
            self.request("policy_finished", binding, job_id="b"*12)
            until = time.monotonic()+2
            while self.service.snapshot()["phase"] != "fault" and time.monotonic()<until:
                time.sleep(.01)
            self.assertEqual(self.service.snapshot()["phase"], "fault")
            self.assertNotIn(("role", "rear-test", "leader"), Writer.calls)
            self.assertIn("writer-front-test", Owner.held)

    def test_foreign_or_malformed_rpc_is_rejected(self):
        with self.assertRaises(ValueError):
            self.agent.dispatch({"id": "x", "op": "right_dagger", "action": "prepare",
                                 "data": {"onsite_confirmed": True}, "unexpected": True})
        self.assertEqual(self.service.snapshot()["phase"], "idle")


if __name__ == "__main__":
    unittest.main()
