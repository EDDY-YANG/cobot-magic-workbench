"""Synthetic PC-only control tests. No SSH/model/GPU/real process is touched."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from service import control as c


def ident(pid,command='synthetic'):
    return {'host':'user-MS-7E06','boot_id':'SYNTHETIC_BOOT','pid':pid,'start_ticks':pid*10,
        'cmdline_sha256':hashlib.sha256(command.encode()).hexdigest(),'identity_kind':'LinuxProcV1'}


def original(step=15000,pid=201):
    return {'manager_identity':ident(pid),'worker_identity':ident(pid+1),
        'status':{'schema':'tiger-fp32-model-supervisor-v1','status':'READY','selected_step':step,'selection_id':'SYNTHETIC_SELECTION_'+str(step),
                  'worker':ident(pid+1),'busy':False,'lease':{'active':False}},
        'health':{'ready':True,'policy':{'precision':'float32','checkpoint_step':step}}}


class FakeRuntime:
    def __init__(self,cfg):
        self.cfg=cfg;self.files={};self.directories=set();self.clock=0.;self.production=original();self.lock_free=False
        self.protected={'SYNTHETIC_PROTECTED_SOURCE':'a'*64};self.ids={};self.commands={};self.nonces={};self.calls=[]
        self.saved_selection=deepcopy(self.production['status'])
        self.spawn_failure=False;self.bad_handshake=False;self.release_on_term=True;self.closed_worker_stopped=True
        self.current_boot='SYNTHETIC_BOOT'
    def boot_id(self):return self.current_boot
    def check_environment(self):self.calls.append('environment');return {'scope':'SYNTHETIC_NO_HOST_PROBE'}
    def production_state(self):return deepcopy(self.production)
    def locks_free(self):return self.lock_free
    def protected_hashes(self):return dict(self.protected)
    def verify_stopped_selection(self,captured):
        # Exercise the real comparison code with a fake read-only state file.
        runtime=c.LinuxRuntime(self.cfg,'SYNTHETIC_CONFIG')
        runtime.read_json=lambda path:deepcopy(self.saved_selection)
        runtime.file_sha=lambda path:'f'*64
        return runtime.verify_stopped_selection(captured)
    def file_sha(self,path):return hashlib.sha256(json.dumps(self.files.get(str(path),{'synthetic_path':str(path)}),sort_keys=True).encode()).hexdigest()
    def read_json(self,path):return deepcopy(self.files[str(path)])
    def exists(self,path):return str(path) in self.files
    def write_new(self,path,value):
        if str(path) in self.files:raise FileExistsError('synthetic existing receipt preserved')
        self.files[str(path)]=deepcopy(value)
    def make_run(self,path):
        if path in self.directories:raise FileExistsError(path)
        self.directories.add(path)
    def spawn(self,argv,run_dir,nonce):
        self.calls.append('spawn_BF16_SYNTHETIC_ONLY')
        if self.spawn_failure:raise RuntimeError('SYNTHETIC spawn failed')
        self.ids[101]=ident(101,json.dumps(argv));self.commands[101]=list(argv);self.nonces[101]=nonce;self.lock_free=False
        if self.bad_handshake:self.commands[101]=['SYNTHETIC_UNEXPECTED_ENTRYPOINT']
        return SimpleNamespace(pid=101,poll=lambda:None if self.ids.get(101) else 1)
    def identity(self,pid):return deepcopy(self.ids.get(pid))
    def command(self,pid):return list(self.commands[pid])
    def nonce(self,pid):return self.nonces.get(pid)
    def signal_owned(self,launch):
        pid=launch['identity']['pid']
        if not c.identity_matches(self.ids.get(pid),launch['identity']) or self.commands[pid]!=launch['argv'] or self.nonces[pid]!=launch['nonce']:
            raise RuntimeError('SYNTHETIC changed role/identity; no signal')
        self.calls.append('SIGTERM_OWNED_SYNTHETIC')
        if self.release_on_term:
            self.ids.pop(pid,None);self.lock_free=True
            self.files[launch['run_dir']+'/closed.json']={'status':'CLOSED','nonce':launch['nonce'],'identity':launch['identity'],
                'locks_released':True,'worker_stopped':self.closed_worker_stopped,'inflight':False}
    def original_launch(self,step):
        self.calls.append(('restore_original_captured_step',step));self.production=original(step,pid=301)
        self.lock_free=False;return {'pid':301,'status':'STARTING_SYNTHETIC_ONLY'}
    def http_get(self,url):
        for name,value in self.files.items():
            if name.endswith('/controller-launch.json'):
                return {'service_nonce':value['nonce'],'config_sha256':self.file_sha(self.cfg['remote_root']+'/service_config.json'),'step':value['step'],'status':'READY'}
        return None
    def now(self):return self.clock
    def pause(self,seconds):self.clock+=seconds


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.scope=ExitStack();self.addCleanup(self.scope.close)
        def forbidden(*args,**kwargs):raise AssertionError('control CPU tests prohibit actual external operations')
        for owner,name in ((socket,'create_connection'),(socket.socket,'connect'),(subprocess,'Popen'),(subprocess,'run'),(urllib.request,'urlopen')):
            self.scope.enter_context(patch.object(owner,name,forbidden))
        self.cfg=c.load_config(ROOT/'service_config.json');self.runtime=FakeRuntime(self.cfg)
        self.config_path=self.cfg['remote_root']+'/service_config.json'
        self.control=c.Controller(self.cfg,self.config_path,self.runtime)
    def prepared(self):return self.control.prepare_handover()['handover_receipt']
    def started(self,step=10000):
        handover=self.prepared();self.runtime.production=None;self.runtime.lock_free=True
        return self.control.start(step,handover=handover)

    def test_default_dry_plan_has_no_side_effect_and_preserves_linux_paths(self):
        result=c.dry_plan('start',self.cfg,step=10000)
        self.assertEqual(result['status'],'DRY_RUN_NO_REMOTE_NO_MODEL_NO_GPU')
        self.assertTrue(result['remote_root'].startswith('/data/'));self.assertNotIn('C:',result['remote_root'])
        self.assertEqual(self.runtime.calls,[])
    def test_prepare_captures_actual_step_and_never_pauses_original(self):
        value=self.control.prepare_handover()
        self.assertEqual(value['captured_step'],15000);self.assertFalse(value['automatic_pause_performed'])
        self.assertEqual(self.runtime.production['status']['status'],'READY')
        self.assertEqual(value['manual_stop_argv'][-1],'stop');self.assertNotIn('spawn_BF16_SYNTHETIC_ONLY',self.runtime.calls)
    def test_active_original_or_lease_blocks_without_auto_pause(self):
        with self.assertRaisesRegex(RuntimeError,'still running'):self.control.start(10000)
        self.runtime.production['status']['lease']['active']=True
        with self.assertRaisesRegex(RuntimeError,'idle/unleased'):self.control.prepare_handover()
        self.assertNotIn('spawn_BF16_SYNTHETIC_ONLY',self.runtime.calls)
    def test_occupied_gpu_locks_preserve_owner(self):
        self.runtime.production=None
        with self.assertRaisesRegex(RuntimeError,'locks are occupied'):self.control.start(10000)
        self.assertNotIn('spawn_BF16_SYNTHETIC_ONLY',self.runtime.calls)
    def test_start_owns_exact_nonce_command_and_has_starting_not_ready_state(self):
        value=self.started();run=value['run_dir']
        self.assertEqual(value['status'],'SERVICE_STARTING_NOT_YET_READY');self.assertEqual(value['step'],10000)
        self.assertEqual(value['argv'][3],self.cfg['remote_root']+'/service/server.py')
        self.assertEqual(value['config_sha256'],self.runtime.file_sha(self.config_path));self.assertEqual(len(value['nonce']),32)
        self.assertIn(run+'/controller-intent.json',self.runtime.files);self.assertIn(run+'/controller-child.json',self.runtime.files)
    def test_partial_identity_failure_retains_known_child_and_run_directory(self):
        handover=self.prepared();self.runtime.production=None;self.runtime.lock_free=True;self.runtime.bad_handshake=True
        value=self.control.start(10000,handover=handover);run=value['run_dir']
        self.assertEqual(value['status'],'STARTUP_IDENTITY_UNCONFIRMED');self.assertEqual(value['spawned_pid'],101)
        self.assertIn(run+'/controller-intent.json',self.runtime.files);self.assertIn(run+'/controller-child.json',self.runtime.files)
        self.assertIn(run+'/controller-start-failed.json',self.runtime.files)
        self.assertNotIn('SIGTERM_OWNED_SYNTHETIC',self.runtime.calls)
        with self.assertRaises(KeyError):self.control.stop(run,timeout_s=.1)
    def test_partial_start_can_recover_only_matching_server_owner_receipt(self):
        self.runtime.production=None;self.runtime.lock_free=True;self.runtime.bad_handshake=True
        failed=self.control.start(10000);run=failed['run_dir'];intent=self.runtime.files[run+'/controller-intent.json']
        self.runtime.commands[101]=intent['argv']
        self.runtime.files[run+'/owner.json']={**{key:intent[key] for key in ('nonce','entrypoint','run_dir','config_sha256','step')},'identity':self.runtime.ids[101]}
        self.assertEqual(self.control.stop(run,timeout_s=.1)['status'],'BF16_SERVICE_CLOSED_CONFIRMED')
    def test_spawn_failure_preserves_intent_and_blocks_unproven_restore(self):
        handover=self.prepared();self.runtime.production=None;self.runtime.lock_free=True;self.runtime.spawn_failure=True
        value=self.control.start(10000,handover=handover)
        self.assertEqual(value['status'],'SPAWN_OUTCOME_UNCONFIRMED')
        self.assertIn(value['run_dir']+'/controller-intent.json',self.runtime.files)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='restore_original_captured_step' for x in self.runtime.calls))
    def test_stop_uses_only_exact_service_and_confirms_exit_and_closed_and_locks(self):
        value=self.started();stopped=self.control.stop(value['run_dir'],timeout_s=.1)
        self.assertEqual(stopped['status'],'BF16_SERVICE_CLOSED_CONFIRMED');self.assertEqual(self.runtime.calls.count('SIGTERM_OWNED_SYNTHETIC'),1)
        self.assertTrue(stopped['process_exited']);self.assertTrue(stopped['GPU_owner_locks_free'])
    def test_pid_reuse_or_nonce_change_never_signals_foreign_owner(self):
        value=self.started();self.runtime.ids[101]=ident(101,'DIFFERENT_PROCESS')
        with self.assertRaisesRegex(RuntimeError,'PID reused'):self.control.stop(value['run_dir'],timeout_s=.1)
        self.assertNotIn('SIGTERM_OWNED_SYNTHETIC',self.runtime.calls)
    def test_locks_released_but_worker_false_is_not_confirmed_and_never_restores(self):
        value=self.started();self.runtime.closed_worker_stopped=False
        with self.assertRaisesRegex(RuntimeError,'restore prohibited'):self.control.rollback(value['run_dir'],timeout_s=.1)
        self.assertFalse(any(isinstance(x,tuple) for x in self.runtime.calls))
    def test_inflight_process_timeout_has_no_kill_or_restore(self):
        value=self.started();self.runtime.release_on_term=False
        result=self.control.stop(value['run_dir'],timeout_s=.1)
        self.assertEqual(result['status'],'STOP_UNCONFIRMED_RESTORE_BLOCKED');self.assertTrue(result['no_forced_kill'])
        self.assertIsNotNone(self.runtime.ids[101])
    def test_rollback_restores_captured_15000_not_new_bf16_10000_or_5000(self):
        value=self.started(10000);restored=self.control.rollback(value['run_dir'],timeout_s=.1,restore_timeout_s=1.)
        self.assertEqual(restored['status'],'ORIGINAL_FP32_RESTORED_READY');self.assertEqual(restored['captured_step'],15000)
        self.assertIn(('restore_original_captured_step',15000),self.runtime.calls)
    def test_new_original_owner_and_production_source_drift_block_restore(self):
        value=self.started();self.runtime.production=original(20000,401)
        with self.assertRaisesRegex(RuntimeError,'new original manager owner'):self.control.rollback(value['run_dir'],timeout_s=.1)
        self.assertFalse(any(isinstance(x,tuple) for x in self.runtime.calls))
    def test_source_changed_handover_cannot_be_used(self):
        handover=self.prepared();self.runtime.production=None;self.runtime.lock_free=True;self.runtime.protected['SYNTHETIC_PROTECTED_SOURCE']='b'*64
        with self.assertRaisesRegex(RuntimeError,'source changed'):self.control.start(10000,handover=handover)
    def test_changed_checkpoint_or_selection_after_manual_handover_is_rejected(self):
        for key,value in (('selected_step',20000),('selection_id','SYNTHETIC_NEW_SELECTION')):
            with self.subTest(field=key):
                runtime=FakeRuntime(self.cfg);control=c.Controller(self.cfg,self.config_path,runtime)
                handover=control.prepare_handover()['handover_receipt'];runtime.production=None;runtime.lock_free=True
                runtime.saved_selection[key]=value
                with self.assertRaisesRegex(RuntimeError,'selection changed'):control.start(10000,handover=handover)
                self.assertNotIn('spawn_BF16_SYNTHETIC_ONLY',runtime.calls)
    def test_old_boot_handover_cannot_start_or_restore_previous_step(self):
        handover=self.prepared();self.runtime.production=None;self.runtime.lock_free=True;self.runtime.current_boot='SYNTHETIC_NEW_BOOT'
        with self.assertRaisesRegex(RuntimeError,'old boot'):self.control.start(10000,handover=handover)
        self.assertNotIn('spawn_BF16_SYNTHETIC_ONLY',self.runtime.calls)
    def test_old_boot_service_receipt_does_not_confirm_stop_or_trigger_restore(self):
        value=self.started();self.runtime.signal_owned(value);self.runtime.current_boot='SYNTHETIC_NEW_BOOT'
        with self.assertRaisesRegex(RuntimeError,'old-boot'):self.control.stop(value['run_dir'],timeout_s=.1)
        with self.assertRaisesRegex(RuntimeError,'old-boot'):self.control.rollback(value['run_dir'],timeout_s=.1)
        self.assertFalse(any(isinstance(x,tuple) for x in self.runtime.calls))
    def test_no_handover_never_invents_restoration_step(self):
        self.runtime.production=None;self.runtime.lock_free=True;value=self.control.start(10000)
        with self.assertRaisesRegex(RuntimeError,'cannot invent'):self.control.rollback(value['run_dir'])
    def test_original_entry_must_be_actual_script_not_a_metadata_argument(self):
        entry=self.cfg['production']['root']+'/scripts/model_supervisor.py'
        self.assertEqual(c.python_entry([self.cfg['python'],'-I','-B',entry,'--initial-step','15000']),entry)
        self.assertNotEqual(c.python_entry([self.cfg['python'],'foreign.py','--metadata',entry]),entry)
    def test_absolute_path_validation_rejects_escape_and_windows_rebase(self):
        for path in ('/data/../etc','C:/data','/data\\bad'):
            with self.assertRaises(RuntimeError):c.posix_path(path)
        with self.assertRaises(RuntimeError):c.dry_plan('stop',self.cfg,run_dir=self.cfg['production']['root'])
    def test_actual_pidfd_primitive_rechecks_identity_after_open_without_pid_kill(self):
        value=self.started();runtime=c.LinuxRuntime(self.cfg,self.config_path);saved=value['identity'];changed={**saved,'start_ticks':saved['start_ticks']+1}
        with patch.object(runtime,'identity',side_effect=[saved,changed]),patch.object(runtime,'command',return_value=value['argv']),\
             patch.object(runtime,'nonce',return_value=value['nonce']),patch.object(c.os,'pidfd_open',create=True,return_value=17),\
             patch.object(c.signal,'pidfd_send_signal',create=True) as send,patch.object(c.os,'close') as close,patch.object(c.os,'kill') as legacy_kill:
            with self.assertRaisesRegex(RuntimeError,'after pidfd open'):runtime.signal_owned(value)
            send.assert_not_called();legacy_kill.assert_not_called();close.assert_called_once_with(17)


    def test_changed_worker_identity_after_handover_is_rejected(self):
        handover=self.prepared();self.runtime.production=None;self.runtime.lock_free=True
        self.runtime.saved_selection['worker']['start_ticks']+=1
        with self.assertRaisesRegex(RuntimeError,'worker identity changed'):self.control.start(10000,handover=handover)
        self.assertNotIn('spawn_BF16_SYNTHETIC_ONLY',self.runtime.calls)

    def test_status_binds_exact_service_nonce_and_raw_config_file_sha(self):
        value=self.started();result=self.control.status(value['run_dir'])
        self.assertEqual(result['service']['service_nonce'],value['nonce'])
        self.runtime.http_get=lambda url:{'service_nonce':'f'*32,'config_sha256':value['config_sha256'],'step':10000}
        with self.assertRaisesRegex(RuntimeError,'different BF16 instance'):self.control.status(value['run_dir'])


if __name__=='__main__':unittest.main()
