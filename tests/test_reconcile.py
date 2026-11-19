"""CPU-only injected recovery tests. No SSH, model, robot or service calls."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from server.reconcile import Reconciler, Journal, LegacyAdapter, RecoveryBlocked, idle_reason


def quiet_state():
    return {"connected": True, "jobs": [],
            "hub": {"api_reachable": True, "recording": {"active": False, "saving": False}},
            "precision_workbench": {"fresh": True, "models": {
                name: {"status": "STOPPED", "ready": False, "busy": False, "lease_active": False}
                for name in ("FP32", "BF16")}, "robot_profile": {"ready": True}, "v2_profile": {"ready": True}}}


class Fake:
    def __init__(self):
        self.observation = {"state": quiet_state(), "generation": "server-boot/robot-boot",
                            "paths": {"timeqp": "READY"}}
        self.calls = []
        self.crash = False
        self.bad_source = False
    def observe(self):
        return deepcopy(self.observation)
    def verify_environment(self):
        self.calls.append("verify")
        if self.bad_source:
            raise RecoveryBlocked("APPROVED_SOURCE_CHANGED")
    def prepare_session(self):
        self.calls.append("prepare")
        if self.crash:
            raise TimeoutError("ambiguous fixture outcome")
        return {"status": "LOCAL_WEB_AND_EXISTING_SESSION_READY"}
    def ensure_timeqp(self):
        self.calls.append("timeqp")
    def ensure_model_path(self, precision):
        self.calls.append(precision)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = Fake()
        self.journal = Journal(self.tmp.name)
        self.engine = Reconciler(self.adapter, self.journal)
    def tearDown(self):
        self.tmp.cleanup()

    def test_default_is_read_only_plan(self):
        self.adapter.observation["state"]["connected"] = False
        result = self.engine.cycle()
        self.assertEqual(result["status"], "PLAN_ONLY")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.journal.rows, {})

    def test_blocked_history_requires_finished_at_before_it_counts_as_terminal(self):
        self.adapter.observation["state"]["jobs"] = [{
            "id": "finished-blocked", "state": "blocked",
            "finished_at": "2026-10-28T17:00:00+08:00"}]
        self.assertEqual(self.engine.cycle(True)["status"], "READY")
        for finished in (None, "", "not-a-time", "2026-10-28T17:00:00"):
            self.adapter.observation["state"]["jobs"] = [{
                "id": "unfinished-blocked", "state": "blocked", "finished_at": finished}]
            with self.subTest(finished_at=finished):
                self.assertEqual(self.engine.cycle(True)["reason_code"], "TASK_ACTIVE_OR_UNKNOWN")
        self.assertEqual(self.adapter.calls, [])

    def test_active_unknown_and_recording_are_deferred(self):
        original = quiet_state()
        changes = [
            lambda s: s.update(jobs=None),
            lambda s: s.update(jobs=[{"state": "unknown"}]),
            lambda s: s["hub"]["recording"].update(active=True),
            lambda s: s["hub"]["recording"].pop("saving"),
            lambda s: s["precision_workbench"]["models"]["FP32"].update(lease_active=True),
            lambda s: s["precision_workbench"]["models"].pop("BF16")]
        for change in changes:
            state = deepcopy(original)
            change(state)
            self.adapter.observation["state"] = state
            with self.subTest(state=state):
                self.assertEqual(self.engine.cycle(True)["status"], "DEFERRED")
        self.assertEqual(self.adapter.calls, [])

    def test_reviewed_boot_preparation_not_replayed_while_cached_error_remains(self):
        self.adapter.observation["state"]["precision_workbench"]["v2_profile"]["error"] = "V2 host rebooted"
        self.assertEqual(self.engine.cycle(True)["status"], "TRANSITION_COMPLETED_REOBSERVE")
        self.assertEqual(self.engine.cycle(True)["status"], "DEFERRED")
        self.assertEqual(self.adapter.calls.count("prepare"), 1)

    def test_unknown_post_outcome_survives_restart_and_is_not_replayed(self):
        self.adapter.observation["state"]["connected"] = False
        self.adapter.crash = True
        self.assertEqual(self.engine.cycle(True)["status"], "UNKNOWN")
        restarted = Reconciler(self.adapter, Journal(self.tmp.name))
        self.assertEqual(restarted.cycle(True)["reason_code"], "PREVIOUS_OUTCOME_UNKNOWN_NO_REPLAY")
        self.assertEqual(self.adapter.calls.count("prepare"), 1)
        self.adapter.observation["state"]["connected"] = True
        self.assertEqual(restarted.cycle(True)["status"], "READY")
        self.assertEqual(self.adapter.calls.count("prepare"), 1)

    def test_timeqp_absence_is_repaired_without_any_model_action(self):
        self.adapter.observation["paths"]["timeqp"] = "ABSENT"
        self.assertEqual(self.engine.cycle(True)["status"], "TRANSITION_COMPLETED_REOBSERVE")
        self.assertEqual(self.adapter.calls, ["verify", "timeqp"])

    def test_models_off_do_not_request_loading_or_model_path(self):
        self.assertEqual(self.engine.cycle(True)["status"], "READY")
        self.assertEqual(self.adapter.calls, [])

    def test_loaded_idle_model_path_uses_only_existing_route(self):
        self.adapter.observation["state"]["precision_workbench"]["models"]["FP32"].update(status="READY", ready=True)
        self.adapter.observation["paths"]["FP32"] = "ABSENT"
        self.assertEqual(self.engine.cycle(True)["status"], "TRANSITION_COMPLETED_REOBSERVE")
        self.assertEqual(self.adapter.calls, ["verify", "FP32"])

    def test_unknown_port_blocks_and_is_not_replaced(self):
        self.adapter.observation["paths"]["timeqp"] = "BLOCKED"
        result = self.engine.cycle(True)
        self.assertEqual(result["reason_code"], "UNKNOWN_OR_MISMATCHED_ENDPOINT")
        self.assertEqual(self.adapter.calls, [])

    def test_source_conflict_prevents_any_mutation(self):
        self.adapter.observation["paths"]["timeqp"] = "ABSENT"
        self.adapter.bad_source = True
        self.assertEqual(self.engine.cycle(True)["reason_code"], "APPROVED_SOURCE_CHANGED")
        self.assertEqual(self.adapter.calls, ["verify"])
        self.assertEqual(self.journal.rows, {})

    def test_later_hub_loss_can_recover_after_previous_postcondition_observed(self):
        self.adapter.observation["state"]["connected"] = False
        self.assertEqual(self.engine.cycle(True)["status"], "TRANSITION_COMPLETED_REOBSERVE")
        self.adapter.observation["state"]["connected"] = True
        self.assertEqual(self.engine.cycle(True)["status"], "READY")
        self.adapter.observation["state"]["hub"]["api_reachable"] = False
        self.assertEqual(self.engine.cycle(True)["status"], "TRANSITION_COMPLETED_REOBSERVE")
        self.assertEqual(self.adapter.calls.count("prepare"), 2)

    def test_timeqp_pending_rechecks_owned_resource_without_replaying_posts(self):
        self.adapter.observation["paths"]["timeqp"] = "ABSENT"
        key = self.adapter.observation["generation"] + ":timeqp:"
        self.journal.put(key, {"state": "PENDING"})
        self.assertEqual(self.engine.cycle(True)["status"], "TRANSITION_COMPLETED_REOBSERVE")
        self.assertEqual(self.adapter.calls, ["verify", "timeqp"])

    def test_owned_registry_creates_only_absent_registered_resource(self):
        adapter = LegacyAdapter({"legacy_origin": "http://127.0.0.1:8873"}, self.tmp.name)
        adapter.run = Mock(side_effect=["LoadState=not-found\nActiveState=inactive\nMainPID=0\n", ""])
        adapter._start_owned("timeqp", ["/fixture/python", "/fixture/server.py"])
        self.assertTrue((Path(self.tmp.name) / "owned-timeqp.local.json").is_file())
        self.assertEqual(adapter.run.call_args_list[1].args[0][0], "systemd-run")
        self.assertNotIn("kill", str(adapter.run.call_args_list))

    def test_unknown_existing_unit_is_not_adopted_or_killed(self):
        adapter = LegacyAdapter({"legacy_origin": "http://127.0.0.1:8873"}, self.tmp.name)
        adapter.run = Mock(return_value="LoadState=loaded\nActiveState=active\nMainPID=42\n")
        with self.assertRaisesRegex(RecoveryBlocked, "UNIT_OCCUPIED_WITHOUT_REGISTRY"):
            adapter._start_owned("timeqp", ["/fixture/python"])
        self.assertEqual(adapter.run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
