import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"lab_stack"))
from agilex_control.can_zero import selection, require_selection, view
from agilex_control.web_backend import build_command, ValidationError
from agilex_control.hardware import PiperAdapter, HardwareBlocked

def fixture(roles=("LE","RE","LT","RT")):
    cfg={"can":{},"pose_speed_deg_s":10}
    states={}
    for i,r in enumerate(roles):
        cfg["can"][r]={"interface":"can_"+r,"expected_serial":str(i+100)}
        states[r]={"interface":"can_"+r,"identity":{"identity_verified":True,"serial":str(i+100)},
           "status_hex":"0100010000000000","status":{"ctrl_mode":1,"arm_status":0,"mode_feed":1,
           "teach_status":0,"motion_status":0,"trajectory_num":0,"err_code":0},
           "age_status_s":.01,"age_motor_s":.01,"age_joint_s":.01,"stale_after_s":.2,
           "fresh":True,"enabled":[True]*6,"motor_flags":[64]*6,"q_deg":[0]*6,
           "timestamp_failures":0,"observer_error":None,"external_commands":{}}
    return cfg,states

class CanZeroTests(unittest.TestCase):
    def test_select_four_registered_arms(self):
        cfg,s=fixture()
        self.assertEqual(selection(cfg,s)["targets"],["LE","LT","RE","RT"])
    def test_standby_is_skipped_and_named(self):
        cfg,s=fixture()
        s["RT"]["status_hex"]="0000000000000000"
        s["RT"]["status"].update(ctrl_mode=0,mode_feed=0)
        v=selection(cfg,s)
        self.assertNotIn("RT",v["targets"]); self.assertIn("RT",v["skipped"])
    def test_stale_mode_does_not_select(self):
        cfg,s=fixture(("right",));s["right"]["age_status_s"]=.3
        self.assertEqual(selection(cfg,s)["targets"],[])
    def test_identity_mismatch_blocks_selected_can_arm(self):
        cfg,s=fixture(("right",));s["right"]["identity"]["serial"]="other"
        self.assertIn("right",selection(cfg,s)["blocked"])
    def test_disabled_can_arm_blocks_instead_of_auto_enable_or_skip(self):
        cfg,s=fixture();s["RT"]["enabled"][2]=False
        with self.assertRaises(ValueError): require_selection(cfg,s,["LE","LT","RE","RT"])
    def test_duplicate_interface_rejected(self):
        cfg,s=fixture();cfg["can"]["RT"]["interface"]=cfg["can"]["RE"]["interface"]
        with self.assertRaises(ValueError):selection(cfg,s)
    def test_duplicate_serial_rejected(self):
        cfg,s=fixture();cfg["can"]["RT"]["expected_serial"]=cfg["can"]["RE"]["expected_serial"]
        with self.assertRaises(ValueError):selection(cfg,s)
    def test_changed_target_set_rejected(self):
        cfg,s=fixture()
        with self.assertRaises(ValueError):require_selection(cfg,s,["RE"])
    def test_raw_decoded_mismatch_is_not_fresh(self):
        cfg,s=fixture(("right",));s["right"]["status"]["ctrl_mode"]=0
        self.assertEqual(selection(cfg,s)["targets"],[])
    def test_foreign_control_blocks(self):
        cfg,s=fixture(("right",))
        s["right"]["external_commands"]={"0x155":{"count":1,"last_age_s":.01}}
        self.assertIn("right",selection(cfg,s)["blocked"])
    def test_stop_blocks_view(self):
        cfg,s=fixture()
        v=view({"status":{"arms":s},"usb":{"verified":True},"stop_latch":{"latched":True}},cfg,True)
        self.assertFalse(v["available"])
    def test_command_keeps_explicit_frozen_targets(self):
        cfg,s=fixture()
        args=build_command("move_zero_can",{"arms":["RE","LE"],"speed_deg_s":4,
          "leader_isolated":True,"path_reviewed":True},cfg)
        self.assertEqual(args[:7],["move","zero","--arm","can-movej","--expected-arms","LE","RE"])
        self.assertNotIn("--close-gripper",args);self.assertIn("--apply",args)
    def test_plan_does_not_apply(self):
        cfg,s=fixture()
        args=build_command("move_zero_can_plan",{"arms":["RE"]},cfg)
        self.assertIn("--json",args);self.assertNotIn("--apply",args)
    def test_gripper_payload_rejected(self):
        cfg,s=fixture()
        with self.assertRaises(ValidationError):
            build_command("move_zero_can",{"arms":["RE"],"close_gripper":True},cfg)
    def test_legacy_zero_still_targets_right(self):
        cfg,states=fixture(("right","left"))
        args=build_command("move_zero",{"arm":"right","close_gripper":False,
           "leader_isolated":True,"path_reviewed":True},cfg)
        self.assertEqual(args[:4],["move","zero","--arm","right"])
        self.assertNotIn("--expected-arms",args)
    def test_unknown_arm_rejected(self):
        cfg,s=fixture()
        with self.assertRaises(ValidationError):build_command("move_zero_can_plan",{"arms":["unbound"]},cfg)
    def test_strict_pose_mode_loss_stops_before_handoff(self):
        obj=PiperAdapter.__new__(PiperAdapter)
        obj._require_connected=Mock()
        obj.require_leader_isolation=Mock()
        obj.require_no_foreign_commands=Mock()
        obj.feedback=Mock(side_effect=HardwareBlocked("mode changed"))
        obj.handoff_can_movej=Mock()
        obj.send_joint_target_deg=Mock()
        with self.assertRaises(HardwareBlocked):
            obj.execute_pose([0]*6,path_reviewed=True,require_existing_can=True)
        self.assertTrue(obj.feedback.call_args.kwargs.get("require_can"))
        obj.handoff_can_movej.assert_not_called()
        obj.send_joint_target_deg.assert_not_called()

if __name__=="__main__":unittest.main()
