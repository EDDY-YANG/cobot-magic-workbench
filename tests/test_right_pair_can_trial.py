# Copyright (c) 2026 Yang Taige
# SPDX-License-Identifier: MIT
import struct
import unittest
from tools.right_pair_can_trial import allowed_frame,stationary_groups,Blocked,QIDS


class PairTrialTests(unittest.TestCase):
    def test_role_whitelist_rejects_teaching_and_reset(self):
        self.assertTrue(allowed_frame("role",0x470,bytes.fromhex("fc00000000000000")))
        self.assertFalse(allowed_frame("role",0x470,bytes.fromhex("fa00000000000000")))
        self.assertFalse(allowed_frame("role",0x150,bytes(8)))

    def test_targets_must_match_planned_reference(self):
        q=[1,2,-3,4,5,6]
        self.assertTrue(allowed_frame("track",0x155,struct.pack(">ii",1000,2000),q))
        self.assertFalse(allowed_frame("track",0x155,struct.pack(">ii",0,0),q))
        self.assertFalse(allowed_frame("observe",0x155,bytes(8),[0]*6))

    def test_mode_must_be_position_mode_and_fixed_native_speed(self):
        self.assertTrue(allowed_frame("track",0x151,bytes.fromhex("01010a0000000000")))
        self.assertFalse(allowed_frame("track",0x151,bytes.fromhex("010164ad00000000")))

    def fixture(self,moving=False):
        rows=[]
        for i in range(120):
            t=i/100
            for cid in QIDS:
                base=(10,20) if cid==QIDS[0] else ((-15,10) if cid==QIDS[1] else (10,10))
                offset=.533 if i%2 else 0
                if moving:offset+=t*3
                rows.append((t,cid,(base[0]+offset,base[1])))
        return rows

    def test_static_anonymous_pair_is_not_treated_as_one_moving_arm(self):
        q,groups=stationary_groups(self.fixture(),0,1.19)
        self.assertAlmostEqual(q[0],10.2665)
        self.assertEqual(groups["0x2a5"]["anonymous_group_count"],2)

    def test_moving_group_is_rejected_without_widening_stationarity(self):
        with self.assertRaises(Blocked):stationary_groups(self.fixture(True),0,1.19)

    def test_temporally_replaced_group_is_rejected(self):
        rows=self.fixture()
        rows=[(t,c,(q[0]+1 if t>.6 else q[0],q[1])) for t,c,q in rows]
        with self.assertRaises(Blocked):stationary_groups(rows,0,1.19)
