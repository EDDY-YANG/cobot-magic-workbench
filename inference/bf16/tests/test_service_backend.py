"""CPU-only guard/closure tests. Every online/model primitive is mocked."""
from contextlib import contextmanager,redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from service import backend as b,contract as c,server as s
from test_service import SyntheticBackend,make_payload,NONCE


class BackendGuardTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch('subprocess.run',side_effect=AssertionError('OFFLINE: external command forbidden')).start()
        patch('socket.create_connection',side_effect=AssertionError('OFFLINE: external connection forbidden')).start()

    def test_default_dry_run_does_not_guard_load_listen_or_write_run_files(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-service-dry-') as folder:
            run=Path(folder)/'not-created';out=io.StringIO()
            with patch.object(sys,'argv',['server.py','--config',str(ROOT/'service_config.json'),'--run-dir',str(run),'--step','5000']),patch.object(s,'verify_online_environment') as guard,patch.object(s,'online_backend') as factory,redirect_stdout(out):
                s.main()
            self.assertEqual(json.loads(out.getvalue())['status'],'DRY_RUN_NO_MODEL_NO_LISTENER')
            guard.assert_not_called();factory.assert_not_called();self.assertFalse(run.exists())
        self.assertNotIn('jax',sys.modules)

    def test_real_model_requires_explicit_flag_linux_and_scope_capability(self):
        cfg,_=b.read_config(ROOT/'service_config.json')
        with patch.object(b,'load_module') as imported:
            with self.assertRaisesRegex(RuntimeError,'execute-online'):
                with b.online_backend(cfg,'/never-accessed',5000):pass
            with patch.object(b.platform,'system',return_value='Windows'):
                with self.assertRaisesRegex(RuntimeError,'Linux4090'):
                    with b.online_backend(cfg,'/never-accessed',5000,execute_online=True):pass
            imported.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'scope capability'):b.RealBackend(cfg,'/never-accessed',5000)
        self.assertNotIn('jax',sys.modules)

    def test_dependency_digest_and_new_audit_path_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-dependencies-') as folder:
            root=Path(folder);source=root/'old-frozen';source.mkdir();path=source/'example.py';path.write_bytes(b'SYNTHETIC SOURCE')
            self.assertEqual(b.verify_dependencies(source,{'example.py':c.file_sha(path)}),1)
            with self.assertRaises(RuntimeError):b.verify_dependencies(source,{'example.py':'0'*64})
            run=root/'new-run';run.mkdir()
            self.assertEqual(b.independent_audit_path(run,run/'audit'),(run/'audit').resolve())
            for target in (source/'audit',run,run/'other'):
                with self.assertRaises(ValueError):b.independent_audit_path(run,target)
            self.assertEqual(path.read_bytes(),b'SYNTHETIC SOURCE')

    def test_two_gpu_locks_must_succeed_before_model_constructor(self):
        cfg,_=b.read_config(ROOT/'service_config.json')
        paths=tuple(str(Path(b.EXPECTED_MOUNT)/'RobotData/03_Files_and_Code'/name) for name in b.GPU_LOCK_RELATIVE)
        @contextmanager
        def busy(**kwargs):raise RuntimeError('SYNTHETIC existing GPU owner');yield
        guard=SimpleNamespace(LOCK_PATHS=paths,acquire_gpu_scope=busy)
        with patch.object(b,'verify_online_environment',return_value={'fixture':'SYNTHETIC'}),patch.object(b,'load_module',return_value=guard) as loaded,patch.object(b,'RealBackend') as constructed:
            with self.assertRaisesRegex(RuntimeError,'existing GPU owner'):
                with b.online_backend(cfg,'/synthetic/run',5000,execute_online=True):pass
            constructed.assert_not_called();self.assertEqual(loaded.call_count,1)
            self.assertEqual(Path(loaded.call_args.args[1]).name,'offline_guard.py')

    def test_partial_construction_failure_is_not_confirmed_worker_stopped(self):
        cfg,_=b.read_config(ROOT/'service_config.json');order=[];lifecycle={}
        @contextmanager
        def scope(**kwargs):
            order.append('locks-held')
            try:yield {'locks_held':True}
            finally:order.append('locks-released')
        paths=tuple(str(Path(b.EXPECTED_MOUNT)/'RobotData/03_Files_and_Code'/name) for name in b.GPU_LOCK_RELATIVE)
        guard=SimpleNamespace(LOCK_PATHS=paths,acquire_gpu_scope=scope)
        class Partial:
            def __init__(self,*args,**kwargs):pass
            def load(self):order.append('constructing');raise RuntimeError('SYNTHETIC partial constructor')
            def close(self):order.append('partial-close-unconfirmed');return False
        with patch.object(b,'verify_online_environment',return_value={}),patch.object(b,'load_module',return_value=guard),patch.object(b,'RealBackend',Partial):
            with self.assertRaises(RuntimeError):
                with b.online_backend(cfg,'/synthetic/run',5000,execute_online=True,lifecycle=lifecycle):pass
        self.assertEqual(order,['locks-held','constructing','partial-close-unconfirmed','locks-released'])
        self.assertFalse(lifecycle['worker_stopped']);self.assertTrue(lifecycle['locks_released'])

    def test_listener_exception_drains_blocked_backend_before_scope_close(self):
        backend=SyntheticBackend();backend.block=True;scope_closed=threading.Event();observed=[];workers=[];errors=[];captured={}
        @contextmanager
        def scope(cfg,run,step,**kwargs):
            life=kwargs['lifecycle'];life.update(locks_released=False,worker_stopped=False)
            try:yield backend
            finally:
                observed.append(('scope-close',captured['api'].inflight,backend.returned.is_set()))
                backend.stopped=True;scope_closed.set();life.update(locks_released=True,worker_stopped=True)
        class Listener:
            def handle_request(self):
                api=captured['api'];key=api.acquire('synthetic-main',5000,2,service_nonce=NONCE)
                def invoke():
                    try:api.infer(make_payload(),key['session_id'],key['lease_token'])
                    except BaseException as exc:errors.append(exc)
                worker=threading.Thread(target=invoke);workers.append(worker);worker.start()
                if not backend.entered.wait(1):raise AssertionError('synthetic backend was never reached')
                def release_later():
                    time.sleep(.03);observed.append(('before-release',scope_closed.is_set()));backend.release.set()
                release=threading.Thread(target=release_later);workers.append(release);release.start()
                raise RuntimeError('SYNTHETIC listener exception')
            def server_close(self):observed.append(('listener-close',))
        original=s.BF16Service
        def api_factory(*args,**kwargs):captured['api']=original(*args,**kwargs);return captured['api']
        with tempfile.TemporaryDirectory(prefix='synthetic-bf16-main-') as folder:
            run=Path(folder);argv=['server.py','--config','unused.json','--run-dir',str(run),'--step','5000','--execute-online']
            identity={'host':'synthetic_host','pid':99,'start_ticks':123,'boot_id':'synthetic','cmdline_sha256':'c'*64,'identity_kind':'LinuxProcV1'}
            try:
                with patch.object(sys,'argv',argv),patch.dict(os.environ,{'TIGER_BF16_SERVICE_NONCE':NONCE}),patch.object(s,'read_config',return_value=({},'d'*64)),patch.object(s,'verify_online_environment',return_value={}),patch.object(s,'process_identity',return_value=identity),patch.object(s,'online_backend',scope),patch.object(s,'BF16Service',side_effect=api_factory),patch.object(s,'make_server',return_value=Listener()),redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit:s.main()
                    self.assertEqual(exit.exception.code,2)
            finally:
                backend.release.set()
                for thread in workers:thread.join(2)
            self.assertIn(('before-release',False),observed)
            self.assertIn(('scope-close',False,True),observed)
            self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],c.APIError)
            closed=json.loads((run/'closed.json').read_text(encoding='utf8'))
            self.assertTrue(closed['locks_released']);self.assertTrue(closed['worker_stopped']);self.assertFalse(closed['inflight'])
        self.assertNotIn('jax',sys.modules)


if __name__=='__main__':unittest.main()
