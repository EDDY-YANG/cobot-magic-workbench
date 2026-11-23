# Copyright (c) 2026 Yang Taige
# SPDX-License-Identifier: MIT
import unittest
from tools.restore_right_leader import Transport

class FakeSocket:
    def __init__(self): self.frames=[]
    def send(self,frame):
        self.frames.append(frame)
        return len(frame)

class RestoreTests(unittest.TestCase):
    def test_query_then_one_fa(self):
        sock=FakeSocket();t=Transport(sock,lambda:None)
        t.SendCanMessage(0x4af,bytes.fromhex("0100000000000000"))
        t.phase="role"
        t.SendCanMessage(0x470,bytes.fromhex("fa00000000000000"))
        self.assertEqual(len(sock.frames),2)
        self.assertEqual(t.sent,{"0x4af":1,"0x470":1})
    def test_output_role_is_rejected(self):
        t=Transport(FakeSocket(),lambda:None);t.phase="role"
        with self.assertRaises(RuntimeError):
            t.SendCanMessage(0x470,bytes.fromhex("fc00000000000000"))
    def test_targets_and_enable_rejected(self):
        for cid in (0x151,0x155,0x191,0x471):
            t=Transport(FakeSocket(),lambda:None);t.phase="role"
            with self.assertRaises(RuntimeError):t.SendCanMessage(cid,bytes(8))
    def test_repeat_role_rejected(self):
        t=Transport(FakeSocket(),lambda:None);t.phase="role"
        t.SendCanMessage(0x470,bytes.fromhex("fa00000000000000"))
        with self.assertRaises(RuntimeError):
            t.SendCanMessage(0x470,bytes.fromhex("fa00000000000000"))
