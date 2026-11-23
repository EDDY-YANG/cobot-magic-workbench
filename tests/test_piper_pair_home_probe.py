import unittest
from tools.probe_piper_pair_home import ProbeError, probe_source

# Synthetic source fixtures exercise isolation/rejection, not hardware behavior.
FIXTURE = """
import impossible_module_that_must_not_be_imported
class C_PiperInterface_V2:
    def __init__(self):
        raise RuntimeError("The SDK constructor must not run")
    def ReqMasterArmMoveToHome(self, mode):
        message = Message()
        message.arbitration_id = 0x191
        if mode == 0:
            message.data = [1, 0, 0, 0, 0, 0, 0, 0]
        elif mode == 1:
            message.data = [1, 1, 1, 0, 0, 0, 0, 0]
        elif mode == 2:
            message.data = [1, 0, 1, 0, 0, 0, 0, 0]
        self.__arm_can.SendCanMessage(message.arbitration_id, message.data)
"""


class PairHomeProbeTests(unittest.TestCase):
    def test_import_and_constructor_are_not_executed(self):
        result = probe_source(FIXTURE)
        self.assertEqual(len(result["cases"]), 3)
        self.assertFalse(result["physical_motion_tested"])

    def test_changed_frame_is_rejected(self):
        with self.assertRaises(ProbeError):
            probe_source(FIXTURE.replace("0x191", "0x155"))

    def test_import_inside_method_is_rejected(self):
        with self.assertRaises(ProbeError):
            probe_source(FIXTURE.replace("message = Message()", "import socket"))

    def test_extra_frame_is_rejected(self):
        with self.assertRaises(ProbeError):
            probe_source(FIXTURE.replace(
                "self.__arm_can.SendCanMessage(message.arbitration_id, message.data)",
                "self.__arm_can.SendCanMessage(message.arbitration_id, message.data)\n"
                "        self.__arm_can.SendCanMessage(message.arbitration_id, message.data)"))

    def test_missing_method_is_rejected(self):
        with self.assertRaises(ProbeError):
            probe_source("class C_PiperInterface_V2: pass")
