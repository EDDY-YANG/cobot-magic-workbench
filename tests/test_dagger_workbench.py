"""Offline regressions for the web owner and receipt checks. No SDK access."""
import io
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lab_stack'))
from agilex_control.dagger_workbench import DaggerWorkbench, WebApp, ValidationError, arm_readback


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.app = DaggerWorkbench.__new__(DaggerWorkbench)
        self.app.lock = RLock()
        self.app.intervention = None
        self.job = dict(id='aabbccddeeff', action='precision_deploy', state='running')
        self.child = SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
        self.app.jobs = [self.job]
        self.app.precision = SimpleNamespace(io=SimpleNamespace(lock=RLock(), child=self.child))
        self.state = dict(connected=True, cache_residence_s=0, hub={})
        self.app.snapshot = lambda: {'dagger': self.app.dagger_state(self.state)}
        self.app._event = lambda text: None

    def click(self, action, binding=None):
        return self.app.submit(action, {'binding': binding or self.job['id']})

    def test_stale_and_duplicate_intervention(self):
        with self.assertRaises(ValidationError):
            self.click('dagger_intervene','old')
        self.assertFalse(self.child.stdin.closed)
        self.click('dagger_intervene')
        self.assertTrue(self.child.stdin.closed)
        self.assertTrue(self.click('dagger_intervene')['can_finish'])

    def test_wait_and_finish_without_claiming_capture(self):
        self.click('dagger_intervene')
        self.job['state'] = 'failed'
        self.assertEqual(self.app.snapshot()['dagger']['phase'],'等待现场交接')
        view = self.click('dagger_finish')
        self.assertIn('未生成',view['note'])
        self.assertEqual(self.click('dagger_finish')['phase'],view['phase'])

    def test_save_only_the_bound_dual_arm_episode(self):
        self.click('dagger_intervene')
        self.job['state'] = 'failed'
        cap = dict(active=True, dagger_job_id=self.job['id'], path='/fixture/session',
                   owned_stop_protocol='tiger-owned-recording-stop-v1',
                   capture_contract=dict(required_arm_roles=['left','right']))
        self.state['hub']['recording'] = cap
        self.assertEqual(self.app.snapshot()['dagger']['record'],'正在录制')
        cap['path'] = '/fixture/another-session'
        with self.assertRaises(ValidationError):
            self.click('dagger_finish')
        cap['path'] = '/fixture/session'
        with patch.object(WebApp, 'submit', return_value={'id':'save'}) as stop:
            self.click('dagger_finish')
            self.click('dagger_finish')
            stop.assert_called_once_with('record_stop', {'episode':'/fixture/session'})
        job = dict(id='save',action='record_stop',state='succeeded',result={})
        self.app.jobs.append(job)
        self.assertEqual(self.app.snapshot()['dagger']['phase'],'保存结果待核实')
        job['result'] = dict(finalization=dict(owned_path=cap['path'],healthy=True,completed=True))
        self.assertEqual(self.app.snapshot()['dagger']['phase'],'人工片段已保存')

    def test_target_feedback_agreement_is_not_source_identity(self):
        arm = dict(identity=dict(identity_verified=True),feedback_fresh=True,q_deg=[0]*6,
                   grip_m=.02,age_joint_s=.01,age_gripper_s=.01,
                   action_targets=dict(q_deg=[1]*6,grip_m=.02,pair_ages_s=[.01]*3,grip_age_s=.01))
        self.assertEqual(arm_readback(arm,True)['status'],'总线目标接近')
        arm['action_targets']['pair_ages_s'][1] = 1
        self.assertIsNone(arm_readback(arm,True)['q'])
        self.assertIsNone(arm_readback(arm,False)['g'])

if __name__ == '__main__':
    unittest.main()
