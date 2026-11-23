"""Offline contracts for scheme-one control. Every hardware boundary is a test double."""
from collections import deque
from copy import deepcopy
from enum import Enum
import io
import json
from pathlib import Path
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, MagicMock, patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lab_stack"))
from agilex_control.hardware import HardwareBlocked, PiperAdapter
from agilex_control.right_dagger import (SCHEMA, RightDaggerService, aligned,
                                         command_input, validate_record_context, reserved_can_roles)
from agilex_control.right_dagger_web import DaggerWorkbench
from agilex_control.sensors import CanObserver
from agilex_control.web_backend import WebApp, make_server


class Observer:
    def __init__(self, now=10., local=False):
        self.frames = {cid: (now, struct.pack(">ii", 1000, 2000)) for cid in (0x155, 0x156, 0x157)}
        self.frames[0x159] = (now, (10000).to_bytes(4, "big", signed=True) + bytes((0, 10, 1, 0)))
        self.origin = local

    def latest_frame(self, cid):
        return self.frames.get(cid)

    def latest_origin(self, cid):
        return self.origin


class InputTests(unittest.TestCase):
    def test_fresh_leader_input(self):
        value = command_input(Observer(), 9., False, 10.05)
        self.assertEqual(value["q_deg"], [1., 2.] * 3)
        self.assertEqual(value["grip_m"], .01)

    def test_policy_mirror_requires_local_origin(self):
        self.assertFalse(command_input(Observer(local=False), 9., True, 10.05))
        self.assertIn("q_deg", command_input(Observer(local=True), 9., True, 10.05))

    def test_old_transition_frames_and_stale_input_are_excluded(self):
        self.assertFalse(command_input(Observer(), 10., False, 10.05))
        self.assertFalse(command_input(Observer(), 9., False, 10.3))

    def test_incoherent_joint_group_is_excluded_independently_of_gripper(self):
        obs = Observer()
        obs.frames[0x157] = (9.91, obs.frames[0x157][1])
        value = command_input(obs, 9., False, 10.05)
        self.assertNotIn("q_deg", value)
        self.assertIn("grip_m", value)

    def test_partial_new_group_cannot_mix_with_previous_group(self):
        obs = Observer()
        previous = command_input(obs, 9., False, 10.01)["joint_stamps"]
        obs.frames[0x155] = (10.02, struct.pack(">ii", 9000, 9000))
        self.assertNotIn("q_deg", command_input(obs, 9., False, 10.03, previous))
        obs.frames[0x156] = (10.021, obs.frames[0x156][1])
        obs.frames[0x157] = (10.022, obs.frames[0x157][1])
        value = command_input(obs, 9., False, 10.03, previous)
        self.assertEqual(value["q_deg"][:2], [9., 9.])

    def test_missing_joint_pair_has_no_zero_fallback(self):
        obs = Observer()
        del obs.frames[0x156]
        self.assertNotIn("q_deg", command_input(obs, 9., False, 10.05))

    def test_gripper_angle_units_and_zero_commands_are_rejected(self):
        obs = Observer()
        obs.frames[0x159] = (10., bytes.fromhex("0000000000000400"))
        self.assertNotIn("grip_m", command_input(obs, 9., False, 10.05))
        obs.frames[0x159] = (10., bytes.fromhex("0000000000000101"))
        self.assertNotIn("grip_m", command_input(obs, 9., False, 10.05))

    def test_pair_alignment_checks_joint_and_gripper(self):
        good = {"q_deg": [0.] * 6, "grip_m": .02}
        self.assertEqual(aligned(good, good)["joint_error_deg"], 0.)
        for bad in ({"q_deg": [3.] * 6, "grip_m": .02},
                    {"q_deg": [0.] * 6, "grip_m": .03},
                    {"q_deg": None, "grip_m": .02}):
            with self.assertRaises(HardwareBlocked):
                aligned(good, bad)

    def test_borrowed_adapter_is_reserved_from_its_former_arm(self):
        cfg = {"can": {"left": {"expected_serial": "rear"}, "right": {"expected_serial": "front"}},
               "right_dagger": {"enabled": True, "rear": {"expected_serial": "rear"}}}
        self.assertEqual(reserved_can_roles(cfg), {"left"})
        cfg["right_dagger"]["enabled"] = False
        self.assertEqual(reserved_can_roles(cfg), set())

    def test_new_interface_requires_explicit_serial(self):
        with self.assertRaises(ValueError):
            CanObserver("can0")
        observer = CanObserver("can0", "explicit-test-serial")
        self.assertEqual(observer.interface, "can0")


class Result(Enum):
    SEND_MESSAGE_SUCCESS = 1


class RoleTests(unittest.TestCase):
    def writer(self):
        writer = PiperAdapter({"interface": "test", "leader_isolated": True}, lambda: {})
        writer.sdk = Mock()
        writer._allowed_ids = frozenset({0x470})
        writer._connection_scope = "role"
        writer._transport = Mock(CAN_STATUS=Result)
        writer._original_send = Mock(return_value=Result.SEND_MESSAGE_SUCCESS)
        return writer

    def test_role_transport_permits_exactly_one_fa_or_fc(self):
        for role in (0xFA, 0xFC):
            writer = self.writer()
            writer._checked_send(0x470, bytes((role, 0, 0, 0, 0, 0, 0, 0)))
            with self.assertRaises(HardwareBlocked):
                writer._checked_send(0x470, bytes((role, 0, 0, 0, 0, 0, 0, 0)))
            self.assertEqual(writer._original_send.call_count, 1)

    def test_role_writer_rejects_offsets_motion_and_enable(self):
        for cid, payload in ((0x470, bytes.fromhex("fa10000000000000")),
                             (0x471, bytes.fromhex("0702000000000000")),
                             (0x155, bytes(8))):
            writer = self.writer()
            with self.assertRaises(HardwareBlocked):
                writer._checked_send(cid, payload)
            writer._original_send.assert_not_called()

    def test_role_ack_must_follow_this_request(self):
        writer = self.writer()
        reply = writer.configure_role("leader", lambda cid: (time.monotonic(), bytes.fromhex("7000000000000000")))
        writer.sdk.MasterSlaveConfig.assert_called_once_with(0xFA, 0, 0, 0)
        self.assertTrue(reply["feedback_still_required"])
        writer = self.writer()
        with self.assertRaises(HardwareBlocked):
            writer.configure_role("follower", lambda cid: (0., bytes.fromhex("7000000000000000")), timeout_s=.01)

    def test_motion_heartbeat_requires_current_can_feedback(self):
        writer = self.writer()
        writer._connection_scope = "motion"
        writer.feedback = Mock(return_value={})
        writer.require_no_foreign_commands = Mock()
        writer.keep_control_mode()
        writer.feedback.assert_called_once_with(require_can=True)
        writer.sdk.MotionCtrl_2.assert_called_once_with(1, 1, 40, 0)
        writer.sdk.reset_mock()
        writer.feedback.side_effect = HardwareBlocked("CAN mode lost")
        with self.assertRaises(HardwareBlocked):
            writer.keep_control_mode()
        writer.sdk.MotionCtrl_2.assert_not_called()

    def test_stop_guard_prevents_sdk_transport(self):
        writer = self.writer()
        writer.send_guard = Mock(side_effect=HardwareBlocked("STOP"))
        with self.assertRaises(HardwareBlocked):
            writer._checked_send(0x470, bytes.fromhex("fa00000000000000"))
        writer._original_send.assert_not_called()


def bare_service(phase="ready"):
    service = RightDaggerService.__new__(RightDaggerService)
    service.enabled = True
    service.lock = threading.RLock()
    service.commands = queue.Queue(8)
    service.state = {"schema": SCHEMA, "phase": phase, "binding": "a" * 32, "policy_job_id": "b" * 12}
    service.snapshot = lambda: deepcopy(service.state)
    service._require_aligned = Mock(return_value={})
    return service


class SessionTests(unittest.TestCase):
    def test_prepare_retry_does_not_queue_another_role_write(self):
        service = bare_service("idle")
        service.request("prepare", onsite_confirmed=True)
        service.request("prepare", onsite_confirmed=True)
        self.assertEqual(service.commands.qsize(), 1)

    def test_wrong_binding_rejected(self):
        for operation in ("bind_policy", "intervene", "finish", "resume", "policy_finished"):
            service = bare_service()
            with self.assertRaises(HardwareBlocked):
                service.request(operation, "stale")

    def test_intervention_waits_for_matching_policy_exit(self):
        service = bare_service("policy")
        service.request("intervene", "a" * 32, job_id="b"*12)
        self.assertEqual(service.state["phase"], "waiting_policy")
        self.assertTrue(service.commands.empty())
        with self.assertRaises(HardwareBlocked):
            service.request("policy_finished", "a" * 32, job_id="c" * 12)
        service.request("policy_finished", "a" * 32, job_id="b" * 12)
        self.assertEqual(service.state["phase"], "taking_control")
        self.assertEqual(service.commands.get_nowait()[0], "policy_finished")

    def test_policy_exit_race_still_queues_the_matching_handover(self):
        service = bare_service("ready")
        service.request("intervene", "a"*32, job_id="b"*12)
        self.assertEqual(service.state["phase"], "taking_control")
        self.assertEqual(service.commands.get_nowait()[0], "policy_finished")

    def test_intervention_requires_a_real_policy_binding(self):
        service = bare_service("ready")
        service.state["policy_job_id"] = None
        with self.assertRaises(HardwareBlocked):
            service.request("intervene", "a"*32, job_id=None)
        self.assertTrue(service.commands.empty())

    def test_model_binding_requires_preparation_and_alignment(self):
        service = bare_service("idle")
        with self.assertRaises(HardwareBlocked):
            service.request("bind_policy", "a" * 32, job_id="b" * 12, task="task")
        service.state["phase"] = "ready"
        service._require_aligned.side_effect = HardwareBlocked("misaligned")
        with self.assertRaises(HardwareBlocked):
            service.request("bind_policy", "a" * 32, job_id="b" * 12, task="task")
        self.assertEqual(service.state["phase"], "ready")

    def test_resume_preserves_rear_owner(self):
        service = bare_service()
        service.front_lock = MagicMock()
        rear_owner = Mock()
        service.resources = rear_owner
        service._release_front_lock()
        rear_owner.close.assert_not_called()
        self.assertIsNone(service.front_lock)

    def test_finish_retry_cannot_relabel_incomplete_expert_log_as_saved(self):
        service = bare_service("fault")
        service.state["human_inputs"] = 1
        service.session = Mock(report={"phase": "fault", "expert_evidence": {"timing_evidence_incomplete": True}})
        service._finish_record = Mock(return_value={"finalization": {"healthy": True}})
        service.front_writer = None
        service.heartbeats = set()
        service.record_path = "/test/episode"
        service._release = Mock(return_value=True)
        service._set = Mock()
        service._finish()
        self.assertEqual(service._set.call_args.args[0], "ended")
        self.assertFalse(service._set.call_args.kwargs["saved"])
        self.assertTrue(service._set.call_args.kwargs["native_record_saved"])

    def test_inactive_state_does_not_poll_or_repeat_failed_io(self):
        service = bare_service("fault")
        service._check = Mock(side_effect=AssertionError("should stay inactive"))
        service._tick()
        service._check.assert_not_called()

    def test_lost_recording_blocks_human_forwarding(self):
        service = bare_service("human")
        service._check = Mock()
        service.capture_state = lambda: {"age_s": 0., "api_reachable": True, "recording": {"active": False}}
        with self.assertRaisesRegex(HardwareBlocked, "Correction producer"):
            service._tick()

    def test_gripper_only_input_keeps_verified_control_mode(self):
        service = bare_service("human")
        service._check = Mock()
        service._bounded_human_input = Mock()
        service.rear = Observer(now=time.monotonic())
        service.rear.interface = "test-rear"
        service.rear.frames = {0x159: service.rear.frames[0x159]}
        service._front = Mock(return_value=Mock(control_snapshot=Mock(return_value={})))
        service.capture_state = lambda: {"api_reachable": True, "age_s": 0., "recording": {
            "active": True, "path": "/test/episode", "right_dagger": {"control_session": "a"*32}}}
        service.record_path = "/test/episode"
        service.after = time.monotonic() - 1.
        service.last_input = {}
        service.last_command = {}
        service.front_writer = Mock()
        service.log = Mock(enqueue=Mock(return_value=True))
        service.session = Mock(report={})
        with patch("agilex_control.right_dagger.require_gripper_feedback"):
            service._tick()
        service.front_writer.keep_control_mode.assert_called_once()
        service.front_writer.send_gripper_m.assert_called_once_with(.01, require_enabled=True)
        service.front_writer.send_joint_target_deg.assert_not_called()

    def test_native_correction_requires_right_only_scope_and_usb_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "input.ndjson"
            log.write_text("")
            (root / "operation.json").write_text(json.dumps({"schema": SCHEMA, "policy_job_id": "b"*12,
                                                            "control_session": "a"*32}))
            cfg = {"capture_profile": "right_only_v1",
                   "can": {"right": {"expected_serial": "test"}},
                   "cameras": {"front": "f", "right": "r"}}
            data = {"task": "task", "dagger_context": {"schema": SCHEMA, "policy_job_id": "b"*12,
                    "control_session": "a"*32, "expert_log": str(log), "side": "right"}}
            with patch("agilex_control.right_dagger.verify_usb", return_value=root):
                self.assertEqual(validate_record_context(cfg, data), data["dagger_context"])
                data["dagger_context"]["side"] = "left"
                with self.assertRaises(ValueError):
                    validate_record_context(cfg, data)


class Bridge:
    def __init__(self, cache):
        self.cache = cache
        self.calls = []

    def request(self, op, **kwargs):
        self.calls.append((op, deepcopy(kwargs)))
        assert op == "right_dagger"
        current = self.cache["right_dagger"]
        action = kwargs["action"]
        if action == "prepare":
            current.update(phase="preparing", binding="a"*32)
        elif action == "intervene":
            current["phase"] = "waiting_policy"
        return deepcopy(current)


def bare_web(cache):
    app = DaggerWorkbench.__new__(DaggerWorkbench)
    app.lock = threading.RLock()
    app.dagger_action_lock = threading.Lock()
    app.dagger_jobs = {}
    app.intervention_requests = set()
    app.dagger_error = None
    app.resume_pending = None
    app.jobs = []
    app.precision = None
    app.transport = Bridge(cache)
    app.operation = threading.Lock()
    app.token = "offline-test-token"
    app.root = Path(__file__).resolve().parents[1] / "lab_stack"
    return app


class WebContractTests(unittest.TestCase):
    def cache(self):
        return {"connected": True, "cache_residence_s": 0.,
                "right_dagger": {"schema": SCHEMA, "enabled": True, "phase": "idle",
                                  "binding": None, "hardware_validated": False}}

    def test_stale_robot_disables_every_action(self):
        cache = self.cache()
        cache["cache_residence_s"] = 2.
        app = bare_web(cache)
        with patch.object(WebApp, "snapshot", side_effect=lambda _: deepcopy(cache), autospec=True):
            view = app.snapshot()["dagger"]
        self.assertFalse(any(view[key] for key in ("can_prepare", "can_enter", "can_resume", "can_finish")))
        self.assertFalse(view["hardware_validated"])

    def test_accepted_prepare_blocks_model_even_before_poll_cache_updates(self):
        cache = self.cache()
        app = bare_web(cache)
        with patch.object(WebApp, "snapshot", side_effect=lambda _: deepcopy(cache), autospec=True):
            app.submit("dagger_prepare", {"onsite_confirmed": True})
            cache["right_dagger"]["phase"] = "idle"
            with patch.object(WebApp, "submit") as regular:
                with self.assertRaises(ValueError):
                    app.submit("precision_deploy", {})
                regular.assert_not_called()

    def test_http_jobs_uses_same_csrf_and_remote_rpc(self):
        cache = self.cache()
        app = bare_web(cache)
        with patch.object(WebApp, "snapshot", side_effect=lambda _: deepcopy(cache), autospec=True):
            server = make_server(app, 0)
            port = server.server_port
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{port}/api/jobs"
                payload = json.dumps({"action": "dagger_prepare", "params": {"onsite_confirmed": True}}).encode()
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
                self.assertEqual(app.transport.calls, [])
                req.add_header("Origin", f"http://127.0.0.1:{port}")
                req.add_header("X-Tiger-Token", app.token)
                with urllib.request.urlopen(req) as response:
                    result = json.load(response)
                self.assertEqual(result["job"]["phase_code"], "preparing")
                self.assertEqual(app.transport.calls[0], ("right_dagger",
                    {"action": "prepare", "binding": None, "data": {"onsite_confirmed": True}}))
            finally:
                server.shutdown()
                worker.join(2)
                server.server_close()

    def test_intervention_closes_only_matching_running_policy_pipe(self):
        cache = self.cache()
        cache["right_dagger"].update(phase="policy", binding="a"*32, policy_job_id="b"*12)
        app = bare_web(cache)
        child = subprocess.Popen([sys.executable, "-c", "import sys;sys.stdin.read()"],
                                 stdin=subprocess.PIPE)
        runner = Mock()
        runner.lock = threading.Lock()
        runner.child = child
        app.precision = Mock(io=runner)
        app.jobs = [{"id": "b"*12, "action": "precision_deploy", "state": "running"}]
        try:
            with patch.object(WebApp, "snapshot", side_effect=lambda _: deepcopy(cache), autospec=True):
                app.submit("dagger_intervene", {"binding": "a"*32, "policy_job_id": "b"*12})
            child.wait(timeout=2)
            self.assertEqual(cache["right_dagger"]["phase"], "waiting_policy")
            self.assertIn("b"*12, app.intervention_requests)
            self.assertNotIn("policy_finished", [x[1]["action"] for x in app.transport.calls])
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(2)

    def test_previous_model_epoch_cannot_cancel_new_task(self):
        cache = self.cache()
        cache["right_dagger"].update(phase="policy", binding="a"*32, policy_job_id="b"*12)
        app = bare_web(cache)
        with patch.object(WebApp, "snapshot", side_effect=lambda _: deepcopy(cache), autospec=True):
            with self.assertRaises(ValueError):
                app.submit("dagger_intervene", {"binding": "a"*32, "policy_job_id": "c"*12})
        self.assertFalse(app.transport.calls)

    def test_foreign_session_cannot_cancel_policy(self):
        cache = self.cache()
        cache["right_dagger"].update(phase="policy", binding="a"*32, policy_job_id="b"*12)
        app = bare_web(cache)
        with patch.object(WebApp, "snapshot", side_effect=lambda _: deepcopy(cache), autospec=True):
            with self.assertRaises(ValueError):
                app.submit("dagger_intervene", {"binding": "c"*32, "policy_job_id": "b"*12})
        self.assertFalse(app.transport.calls)


if __name__ == "__main__":
    unittest.main()
