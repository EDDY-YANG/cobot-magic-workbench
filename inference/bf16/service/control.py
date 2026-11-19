"""Independent BF16 service control. Default is a side-effect-free dry run.

No automatic FP32 pause, process-name killing, SIGKILL, model selection, robot
command, or production source/config write. Online use requires a separate
explicit flag. Importing this module performs no host/network/device action.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import errno
import hashlib
import json
import math
import os
from pathlib import Path,PurePosixPath
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
CONFIG_SCHEMA='tiger-bf16-rtc-service-config-v1'
NONCE_ENV='TIGER_BF16_SERVICE_NONCE'
IDENTITY_KEYS=('host','boot_id','pid','start_ticks','cmdline_sha256','identity_kind')
HANDOVER_SCHEMA='tiger-bf16-manual-fp32-handover-v1'
LAUNCH_SCHEMA='tiger-bf16-controller-launch-v1'


def require(ok,message):
    if not ok:raise RuntimeError(message)


def sha(data):return hashlib.sha256(data).hexdigest()


def python_entry(argv):
    if not isinstance(argv,list) or not argv:return None
    index=1
    while index<len(argv) and argv[index] in ('-I','-B','-u'):index+=1
    return argv[index] if index<len(argv) and not argv[index].startswith('-') else None


def posix_path(value):
    require(isinstance(value,str) and value.startswith('/') and '\\' not in value and '\x00' not in value,'absolute Linux path required')
    p=PurePosixPath(value)
    require('..' not in p.parts and str(p)==value,'normalized Linux path required')
    return p


def under(path,root):
    p,r=posix_path(path),posix_path(root)
    require(p!=r and p.is_relative_to(r),'path must be below the independent experiment root')
    return str(p)


def load_config(path):
    cfg=json.loads(Path(path).read_text(encoding='utf-8'))
    require(cfg.get('schema')==CONFIG_SCHEMA,'service configuration schema differs')
    for key in ('remote_root','python','bf16_source_root'):posix_path(cfg[key])
    require(cfg.get('bind')=='127.0.0.1' and cfg.get('port')==18960,'only independent loopback18960 is supported')
    require(cfg.get('lease_ttl_s')==2.0,'original two-second lease contract required')
    require(isinstance(cfg.get('host'),str) and cfg['host'],'explicit physical host required')
    for key in ('root','old_root','launch_script','control_script'):posix_path(cfg['production'][key])
    require(cfg['production']['manager_url']=='http://127.0.0.1:18882','original manager endpoint differs')
    require(isinstance(cfg.get('allowed_steps'),list) and cfg['allowed_steps'] and all(type(x) is int and x>0 for x in cfg['allowed_steps']),'allowed steps missing')
    return cfg


def validate_identity(value):
    return isinstance(value,dict) and all(k in value for k in IDENTITY_KEYS) and value['identity_kind']=='LinuxProcV1' and \
        type(value['pid']) is int and value['pid']>1 and type(value['start_ticks']) is int and value['start_ticks']>=0 and \
        all(isinstance(value[k],str) and value[k] for k in ('host','boot_id')) and \
        isinstance(value['cmdline_sha256'],str) and len(value['cmdline_sha256'])==64


def identity_matches(a,b):
    return validate_identity(a) and validate_identity(b) and all(a[k]==b[k] for k in IDENTITY_KEYS)


def dry_plan(action,cfg,*,step=None,run_dir=None,handover=None):
    """Uses PurePosixPath; never resolve Linux paths against a Windows drive."""
    require(action in ('inspect','prepare-handover','start','status','stop','rollback','accept'),'unsupported action')
    if step is not None:require(type(step) is int and step in cfg['allowed_steps'],'unsupported explicit checkpoint')
    if run_dir is not None:under(run_dir,str(posix_path(cfg['remote_root'])/'runs'))
    if handover is not None:under(handover,cfg['remote_root'])
    return {'status':'DRY_RUN_NO_REMOTE_NO_MODEL_NO_GPU','action':action,'host':cfg['host'],
        'remote_root':cfg['remote_root'],'step':step,'run_dir':run_dir,'handover_receipt':handover,
        'online_requires':'--execute-online','automatic_FP32_pause':False,
        'original_manual_stop_argv':[cfg['python'],'-I','-B',cfg['production']['control_script'],'stop'] if action=='prepare-handover' else None,
        'restore_step_source':'only the captured handover receipt; never a default',
        'GPU_or_robot_action_performed':False,'production_source_mutation_allowed':False}


class LinuxRuntime:
    """All actual side effects live here and are instantiated only online."""
    def __init__(self,cfg,config_path):self.cfg,self.config_path=cfg,Path(config_path)
    def boot_id(self):return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    def check_environment(self):
        require(os.name=='posix' and socket.gethostname()==self.cfg['host'],'wrong physical host/OS')
        require(Path(sys.executable).resolve()==Path(self.cfg['python']).resolve(),'wrong absolute interpreter')
        require(Path(__file__).resolve().parents[1]==Path(self.cfg['remote_root']).resolve(),'controller is not this isolated remote bundle')
        raw=subprocess.run(['findmnt','-J','-T',self.cfg['remote_root'],'-o','TARGET,UUID'],capture_output=True,text=True,check=True,timeout=10)
        mounts=json.loads(raw.stdout)['filesystems']
        require(len(mounts)==1 and mounts[0].get('target')==self.cfg['mount']['target'] and mounts[0].get('uuid')==self.cfg['mount']['uuid'],'physical SSD identity differs')
        manifest=self.read_json(str(Path(self.cfg['remote_root'])/'source_manifest.json'))
        require(manifest.get('schema')=='tiger-bf16-service-source-manifest-v1','source manifest required')
        for name,expected in manifest['files'].items():
            p=PurePosixPath(name);require(not p.is_absolute() and '..' not in p.parts,'invalid source manifest path')
            path=Path(self.cfg['remote_root'])/name
            require(not path.is_symlink() and path.resolve().is_relative_to(Path(self.cfg['remote_root']).resolve()),'source symlink/escape refused')
            require(sha(path.read_bytes())==expected,'isolated source SHA differs: '+name)
        for name,expected in self.cfg['dependencies_sha256'].items():
            p=PurePosixPath(name);require(not p.is_absolute() and '..' not in p.parts,'invalid dependency path')
            path=Path(self.cfg['bf16_source_root'])/name
            require(path.resolve().is_relative_to(Path(self.cfg['bf16_source_root']).resolve()),'dependency path escape')
            require(sha(path.read_bytes())==expected,'existing BF16 dependency SHA differs: '+name)
        return {'host':socket.gethostname(),'mount':mounts[0],'python':sys.executable,'source_files_verified':len(manifest['files'])}
    def read_json(self,path):return json.loads(Path(path).read_text(encoding='utf-8'))
    def exists(self,path):return Path(path).exists()
    def write_new(self,path,value):
        under(str(path),self.cfg['remote_root']);p=Path(path)
        require(p.resolve().is_relative_to(Path(self.cfg['remote_root']).resolve()) and not any(parent.is_symlink() for parent in p.parents if parent.is_relative_to(Path(self.cfg['remote_root']))),'new receipt resolved outside isolated root')
        p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with p.open('x',encoding='utf-8') as f:json.dump(value,f,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
    def make_run(self,path):
        under(path,str(posix_path(self.cfg['remote_root'])/'runs'));p=Path(path)
        require(p.resolve().is_relative_to(Path(self.cfg['remote_root']).resolve()) and not p.parent.is_symlink(),'run parent symlink/escape refused')
        p.parent.mkdir(exist_ok=True,mode=0o700);p.mkdir(mode=0o700)
    def file_sha(self,path):return sha(Path(path).read_bytes())
    def identity(self,pid):
        try:
            p=Path('/proc')/str(pid);tail=(p/'stat').read_text().rsplit(') ',1)[1].split()
            if tail[0] in ('Z','X'):return None
            result={'host':socket.gethostname(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'pid':pid,'start_ticks':int(tail[19]),'cmdline_sha256':sha((p/'cmdline').read_bytes()),'identity_kind':'LinuxProcV1'}
            if int((p/'stat').read_text().rsplit(') ',1)[1].split()[19])!=result['start_ticks']:raise RuntimeError('PID identity changed')
            return result
        except FileNotFoundError:return None
    def command(self,pid):return [x.decode() for x in (Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0') if x]
    def nonce(self,pid):
        values=(Path('/proc')/str(pid)/'environ').read_bytes().split(b'\0');prefix=(NONCE_ENV+'=').encode()
        return next((x[len(prefix):].decode() for x in values if x.startswith(prefix)),None)
    def http_get(self,url):
        require(url.startswith(('http://127.0.0.1:18882/','http://127.0.0.1:18960/')),'only bound loopback metadata endpoint allowed')
        try:
            with urllib.request.urlopen(url,timeout=2) as response:
                data=response.read(4*1024*1024+1);require(len(data)<=4*1024*1024,'metadata reply too large');return json.loads(data)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason,ConnectionRefusedError) or isinstance(exc.reason,OSError) and exc.reason.errno==errno.ECONNREFUSED:return None
            raise  # A timeout/invalid HTTP reply is NOT proof that a service is absent.
    def locks_free(self):
        import fcntl
        paths=[Path(self.cfg['production'][key])/'FP32_GPU_OWNER.lock' for key in ('old_root','root')]
        try:
            with ExitStack() as scope:
                for path in paths:
                    fd=os.open(path,os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
                    stream=scope.enter_context(os.fdopen(fd,'r+'))
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                return True
        except BlockingIOError:return False
    def protected_hashes(self):
        prod=self.cfg['production'];paths=set()
        for key in ('root','old_root'):paths.update((Path(prod[key])/'scripts').glob('*.py'))
        paths.update(p for p in (Path(prod['root'])/'source').rglob('*') if p.is_file() and p.suffix in ('.py','.toml','.lock','.json'))
        require(paths,'production source set missing')
        required={'production_model_supervisor.py':Path(prod['root'])/'scripts/model_supervisor.py',
            'production_worker_entry.py':Path(prod['root'])/'scripts/worker_entry.py',
            'production_supervisor_control.py':Path(prod['control_script']),'production_launch_supervisor.py':Path(prod['launch_script'])}
        for frozen,path in required.items():require(self.file_sha(path)==self.file_sha(ROOT/'frozen'/frozen),'production frozen source differs: '+str(path))
        return {str(p):self.file_sha(p) for p in sorted(paths)}
    def verify_stopped_selection(self,captured):
        path=self.cfg['production']['root']+'/receipts/supervisor-state.json';saved=self.read_json(path)
        expected=captured['status'];worker=captured['worker_identity'];actual=saved.get('worker')
        require(saved.get('schema')=='tiger-fp32-model-supervisor-v1' and saved.get('selected_step')==expected.get('selected_step') and
                isinstance(expected.get('selection_id'),str) and bool(expected['selection_id']) and saved.get('selection_id')==expected['selection_id'],
                'original checkpoint/selection changed after handover snapshot; do not restore an earlier step')
        require(isinstance(actual,dict) and actual.get('pid')==worker['pid'] and actual.get('boot_id')==worker['boot_id'] and
                str(actual.get('start_ticks'))==str(worker['start_ticks']) and actual.get('cmdline_sha256')==worker['cmdline_sha256'],
                'original worker identity changed after handover snapshot')
        return {'source_path':path,'source_sha256':self.file_sha(path),'selected_step':saved['selected_step'],
                'selection_id':saved['selection_id'],'meaning':'read-only saved selection identity; not a live lock or idle proof'}
    def spawn(self,argv,run_dir,nonce):
        with (Path(run_dir)/'service.log').open('xb',buffering=0) as log:
            return subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,cwd=self.cfg['remote_root'],
                start_new_session=True,env={**os.environ,NONCE_ENV:nonce,'PYTHONDONTWRITEBYTECODE':'1', 'XDG_CACHE_HOME':str(ROOT/'cache'), 'TMPDIR':str(ROOT/'tmp'), 'TRITON_CACHE_DIR':str(ROOT/'cache/triton'), 'JAX_COMPILATION_CACHE_DIR':str(ROOT/'cache/jax')})
    def signal_owned(self,launch):
        identity=launch['identity'];pid=identity['pid']
        require(hasattr(os,'pidfd_open') and hasattr(signal,'pidfd_send_signal'),'Linux pidfd required; no PID-only TERM fallback')
        require(identity_matches(self.identity(pid),identity) and self.command(pid)==launch['argv'] and self.nonce(pid)==launch['nonce'],'service role/identity changed; no signal sent')
        fd=os.pidfd_open(pid,0)
        try:
            require(identity_matches(self.identity(pid),identity) and self.command(pid)==launch['argv'] and self.nonce(pid)==launch['nonce'],'service identity changed after pidfd open')
            signal.pidfd_send_signal(fd,signal.SIGTERM,None,0)
        finally:os.close(fd)
    def original_launch(self,step):
        argv=[self.cfg['python'],'-I','-B',self.cfg['production']['launch_script'],'--step',str(step)]
        result=subprocess.run(argv,capture_output=True,text=True,timeout=30)
        require(result.returncode==0,'original launch refused/failed; preserve output in new receipt: '+result.stderr[-2048:])
        return json.loads(result.stdout.strip())
    def original_identity(self,raw):
        require(isinstance(raw,dict) and type(raw.get('pid')) is int,'original process receipt missing')
        actual=self.identity(raw['pid'])
        if actual is None:return None
        require(actual['boot_id']==raw.get('boot_id') and str(actual['start_ticks'])==str(raw.get('start_ticks')) and
                actual['cmdline_sha256']==raw.get('cmdline_sha256'),'original PID reused/changed; preserve unknown owner')
        return actual
    def production_state(self):
        root=self.cfg['production']['root'];instance=root+'/receipts/manager-instance.json'
        saved=self.read_json(instance) if self.exists(instance) else None
        manager=self.original_identity(saved) if saved else None
        status=self.http_get(self.cfg['production']['manager_url']+'/status')
        if status is None:
            require(manager is None,'tracked original manager alive but endpoint unavailable')
            state_file=root+'/receipts/supervisor-state.json'
            if self.exists(state_file):
                worker=self.read_json(state_file).get('worker')
                require(worker is None or self.original_identity(worker) is None,'original GPU worker still alive')
            return None
        require(manager is not None and status.get('schema')=='tiger-fp32-model-supervisor-v1','untracked/foreign manager preserved')
        require(str(posix_path(root)/'scripts/model_supervisor.py')==python_entry(self.command(manager['pid'])),'original entrypoint mismatch')
        health=self.http_get(self.cfg['production']['manager_url']+'/health')
        worker=status.get('worker');worker_identity=self.original_identity(worker) if worker else None
        return {'manager_identity':manager,'worker_identity':worker_identity,'status':status,'health':health}
    def pause(self,seconds):time.sleep(seconds)
    def now(self):return time.monotonic()


class Controller:
    def __init__(self,cfg,config_path,runtime):self.cfg,self.config_path,self.r=cfg,str(config_path),runtime
    def inspect(self):
        env=self.r.check_environment();production=self.r.production_state()
        return {'status':'READ_ONLY_INSPECTION','environment':env,'production':production,'GPU_owner_locks_free':self.r.locks_free(),
                'automatic_FP32_pause':False,'model_or_robot_action_performed':False}
    def prepare_handover(self):
        self.r.check_environment();before=self.r.production_state();require(before is not None,'original FP32 must be READY to prepare its restoration receipt')
        status,health=before['status'],before['health'];step=status.get('selected_step')
        require(validate_identity(before.get('manager_identity')) and validate_identity(before.get('worker_identity')) and
                before['manager_identity']['boot_id']==self.r.boot_id()==before['worker_identity']['boot_id'],
                'current-boot original manager/worker identity required')
        require(status.get('status')=='READY' and status.get('busy') is False and status.get('lease',{}).get('active') is False,'original service is not READY idle/unleased; preserve it')
        require(isinstance(status.get('selection_id'),str) and bool(status['selection_id']),'original live selection identity required')
        require(step in self.cfg['allowed_steps'] and health and health.get('ready') is True and
                health.get('policy',{}).get('precision')=='float32' and health['policy'].get('checkpoint_step')==step,'original FP32 health/step not bound')
        value={'schema':HANDOVER_SCHEMA,'status':'MANUAL_HANDOVER_PREPARED_NOT_PAUSED','handover_id':uuid.uuid4().hex,
            'host':self.cfg['host'],'config_sha256':self.r.file_sha(self.config_path),'captured_step':step,
            'original':before,'production_sha256':self.r.protected_hashes(),'automatic_pause_performed':False,
            'manual_stop_argv':[self.cfg['python'],'-I','-B',self.cfg['production']['control_script'],'stop'],
            'warning':'Run the original controller manually only after independently confirming current idle/no lease; this snapshot is not an atomic pause authorization.'}
        path=str(posix_path(self.cfg['remote_root'])/'receipts'/('handover-'+value['handover_id']+'.json'));self.r.write_new(path,value)
        return {**value,'handover_receipt':path}
    def _handover(self,path):
        under(path,self.cfg['remote_root']);value=self.r.read_json(path)
        require(value.get('schema')==HANDOVER_SCHEMA and value.get('status')=='MANUAL_HANDOVER_PREPARED_NOT_PAUSED' and
            value.get('host')==self.cfg['host'] and value.get('config_sha256')==self.r.file_sha(self.config_path),'invalid/foreign handover receipt')
        require(value.get('captured_step') in self.cfg['allowed_steps'],'captured step missing; never restore a default')
        require(value.get('original',{}).get('manager_identity',{}).get('boot_id')==self.r.boot_id() and
                value.get('original',{}).get('worker_identity',{}).get('boot_id')==self.r.boot_id(),
                'handover belongs to an old boot; no automatic use/restoration')
        require(self.r.protected_hashes()==value['production_sha256'],'original source changed; never overwrite or restore through changed source')
        return value
    def start(self,step,*,handover=None):
        self.r.check_environment();require(type(step) is int and step in self.cfg['allowed_steps'],'explicit allowed BF16 step required')
        require(self.r.production_state() is None,'original FP32 is still running; no automatic pause. PrepareHandover then use its original manual stop command.')
        require(self.r.locks_free(),'existing GPU owner locks are occupied; preserve the owner')
        previous=self._handover(handover) if handover else None
        stopped_selection=self.r.verify_stopped_selection(previous['original']) if previous else None
        nonce=uuid.uuid4().hex;run_dir=str(posix_path(self.cfg['remote_root'])/'runs'/(time.strftime('%Y-%m-%d-%H-%M-%S')+'-'+nonce[:12]))
        self.r.make_run(run_dir)
        argv=[self.cfg['python'],'-I','-B',str(posix_path(self.cfg['remote_root'])/'service/server.py'),
            '--config',self.config_path,'--run-dir',run_dir,'--step',str(step),'--execute-online']
        base={'schema':LAUNCH_SCHEMA,'nonce':nonce,'host':self.cfg['host'],'entrypoint':str(posix_path(self.cfg['remote_root'])/'service/server.py'),
            'config_sha256':self.r.file_sha(self.config_path),'step':step,'run_dir':run_dir,'argv':argv,
            'handover_receipt':handover,'handover_sha256':self.r.file_sha(handover) if previous else None,
            'original_stopped_selection':stopped_selection,
            'status':'SERVICE_STARTING_NOT_YET_READY','automatic_FP32_pause':False}
        if previous:
            self.r.write_new(str(posix_path(self.cfg['remote_root'])/'receipts'/('handover-use-'+previous['handover_id']+'.json')),
                {'run_dir':run_dir,'nonce':nonce,'handover_sha256':base['handover_sha256']})
        self.r.write_new(run_dir+'/controller-intent.json',base)
        try:child=self.r.spawn(argv,run_dir,nonce)
        except BaseException as exc:
            failed={'status':'SPAWN_OUTCOME_UNCONFIRMED','run_dir':run_dir,'nonce':nonce,'error_type':type(exc).__name__,
                'warning':'No PID-only cleanup or automatic FP32 restore; preserve this new directory and inspect ownership.'}
            self.r.write_new(run_dir+'/controller-start-failed.json',failed);return failed
        self.r.write_new(run_dir+'/controller-child.json',{'pid':child.pid,'nonce':nonce,'argv':argv})
        deadline=self.r.now()+5;identity=None
        while self.r.now()<deadline:
            identity=self.r.identity(child.pid)
            if identity is not None and self.r.command(child.pid)==argv and self.r.nonce(child.pid)==nonce:break
            if child.poll() is not None:break
            self.r.pause(.02)
        if identity is None or self.r.command(child.pid)!=argv or self.r.nonce(child.pid)!=nonce:
            failed={'status':'STARTUP_IDENTITY_UNCONFIRMED','run_dir':run_dir,'nonce':nonce,'spawned_pid':child.pid,
                'warning':'Intent/child/log evidence retained. Stop can recover only from a matching service owner.json; no PID-only signal/automatic restore.'}
            self.r.write_new(run_dir+'/controller-start-failed.json',failed);return failed
        base['identity']=identity;self.r.write_new(run_dir+'/controller-launch.json',base)
        return base
    def _launch(self,run_dir):
        under(run_dir,str(posix_path(self.cfg['remote_root'])/'runs'))
        path=run_dir+'/controller-launch.json'
        if self.r.exists(path):value=self.r.read_json(path)
        else:
            value=self.r.read_json(run_dir+'/controller-intent.json');owner=self.r.read_json(run_dir+'/owner.json')
            require(owner.get('nonce')==value.get('nonce') and owner.get('entrypoint')==value.get('entrypoint') and
                owner.get('run_dir')==run_dir and owner.get('config_sha256')==self.r.file_sha(self.config_path),'partial startup owner cannot be recovered safely')
            value={**value,'identity':owner.get('identity'),'status':'RECOVERED_EXACT_OWNER_FROM_STARTUP_INTENT'}
        require(value.get('schema')==LAUNCH_SCHEMA and value.get('host')==self.cfg['host'] and value.get('run_dir')==run_dir and
                value.get('entrypoint')==str(posix_path(self.cfg['remote_root'])/'service/server.py') and validate_identity(value.get('identity')) and
                value.get('config_sha256')==self.r.file_sha(self.config_path) and isinstance(value.get('nonce'),str) and len(value['nonce'])==32,'invalid service ownership receipt')
        require(value['identity']['boot_id']==self.r.boot_id() and value['identity']['host']==self.cfg['host'],
                'old-boot or different-host service receipt; automatic control/restoration refused')
        expected=[self.cfg['python'],'-I','-B',value['entrypoint'],'--config',self.config_path,'--run-dir',run_dir,'--step',str(value['step']),'--execute-online']
        require(value['argv']==expected,'service argv/entry scope differs')
        return value
    def status(self,run_dir):
        self.r.check_environment();launch=self._launch(run_dir);actual=self.r.identity(launch['identity']['pid'])
        alive=identity_matches(actual,launch['identity'])
        require(actual is None or alive,'PID changed; foreign process preserved')
        endpoint=self.r.http_get('http://127.0.0.1:18960/status') if alive else None
        if endpoint is not None:require(endpoint.get('service_nonce')==launch['nonce'] and
            endpoint.get('config_sha256')==self.r.file_sha(self.config_path),'port belongs to a different BF16 instance/config')
        return {'status':'SERVICE_PROCESS_ALIVE' if alive else 'SERVICE_PROCESS_EXITED','run_dir':run_dir,
            'identity':launch['identity'],'service':endpoint,'GPU_owner_locks_free':self.r.locks_free(),
            'readiness_requires_service_audit':True,'robot_motion_supported':False}
    def stop(self,run_dir,*,timeout_s=30.):
        require(type(timeout_s) in (int,float) and math.isfinite(timeout_s) and 0<timeout_s<=900,'bounded finite stop timeout required')
        self.r.check_environment();launch=self._launch(run_dir);actual=self.r.identity(launch['identity']['pid'])
        require(actual is None or identity_matches(actual,launch['identity']),'PID reused or new owner; no signal')
        if actual is not None:self.r.signal_owned(launch)
        deadline=self.r.now()+timeout_s
        while True:
            actual=self.r.identity(launch['identity']['pid']);closed_path=run_dir+'/closed.json'
            closed=self.r.read_json(closed_path) if self.r.exists(closed_path) else None
            exited=actual is None or not identity_matches(actual,launch['identity'])
            confirmed=exited and isinstance(closed,dict) and closed.get('status')=='CLOSED' and closed.get('nonce')==launch['nonce'] and \
                identity_matches(closed.get('identity'),launch['identity']) and closed.get('locks_released') is True and closed.get('worker_stopped') is True and closed.get('inflight') is False
            if confirmed and self.r.locks_free():
                value={'status':'BF16_SERVICE_CLOSED_CONFIRMED','run_dir':run_dir,'nonce':launch['nonce'],'identity':launch['identity'],
                    'process_exited':True,'GPU_owner_locks_free':True,'closed_receipt':closed,'original_FP32_changed':False}
                self.r.write_new(run_dir+'/controller-stop-'+uuid.uuid4().hex+'.json',value);return value
            if self.r.now()>=deadline:return {'status':'STOP_UNCONFIRMED_RESTORE_BLOCKED','run_dir':run_dir,
                'process_exited':exited,'closed_receipt_observed':closed is not None,'no_forced_kill':True,'original_FP32_changed':False}
            self.r.pause(.1)
    def rollback(self,run_dir,*,timeout_s=30.,restore_timeout_s=900.):
        self.r.check_environment()
        require(type(restore_timeout_s) in (int,float) and math.isfinite(restore_timeout_s) and 0<restore_timeout_s<=1800,'bounded finite restore timeout required')
        launch=self._launch(run_dir);require(launch.get('handover_receipt'),'no captured original service; rollback cannot invent a model/step')
        before=self._handover(launch['handover_receipt'])
        self.r.verify_stopped_selection(before['original'])
        require(launch['handover_sha256']==self.r.file_sha(launch['handover_receipt']),'handover receipt changed')
        stopped=self.stop(run_dir,timeout_s=timeout_s)
        require(stopped['status']=='BF16_SERVICE_CLOSED_CONFIRMED','BF16 exit/GPU release unconfirmed; FP32 restore prohibited')
        require(self.r.production_state() is None,'a new original manager owner exists; preserve it, do not restart/select/stop')
        require(self.r.locks_free(),'GPU owner changed before restoration; preserve it')
        require(self.r.protected_hashes()==before['production_sha256'],'original source changed before restoration')
        marker=run_dir+'/fp32-restore-attempt.json';require(not self.r.exists(marker),'restoration was already attempted; inspect its preserved receipt, never duplicate start')
        step=before['captured_step'];self.r.write_new(marker,{'status':'RESTORE_ATTEMPT_BEGIN','captured_step':step,'nonce':launch['nonce']})
        started=self.r.original_launch(step);expected_pid=started.get('pid');require(type(expected_pid) is int and expected_pid>1,'original launch returned no child PID')
        deadline=self.r.now()+restore_timeout_s
        while self.r.now()<deadline:
            state=self.r.production_state()
            if state is not None:
                require(state['manager_identity']['pid']==expected_pid,'different manager won ownership; preserved without control')
                status,health=state['status'],state['health']
                if status.get('status')=='FAILED':raise RuntimeError('original FP32 restore failed; preserve original logs and new receipt')
                if status.get('status')=='READY':
                    require(status.get('selected_step')==step and status.get('busy') is False and status.get('lease',{}).get('active') is False and
                        health and health.get('ready') is True and health.get('policy',{}).get('precision')=='float32' and health['policy'].get('checkpoint_step')==step,'restored FP32 identity/step/health differs')
                    require(self.r.protected_hashes()==before['production_sha256'],'original source drift after restore')
                    value={'status':'ORIGINAL_FP32_RESTORED_READY','captured_step':step,'original':state,'BF16_closed':stopped,
                        'production_source_hashes_unchanged':True,'original_config_modified':False}
                    self.r.write_new(run_dir+'/fp32-restored.json',value);return value
            self.r.pause(1.)
        return {'status':'FP32_RESTORE_UNCONFIRMED','captured_step':step,'new_owner_not_overwritten':True,'restore_attempt_receipt':marker}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('inspect','prepare-handover','start','status','stop','rollback','accept'))
    parser.add_argument('--config',type=Path,default=ROOT/'service_config.json');parser.add_argument('--step',type=int)
    parser.add_argument('--run-dir');parser.add_argument('--handover');parser.add_argument('--timeout-s',type=float,default=30.)
    parser.add_argument('--restore-timeout-s',type=float,default=900.);parser.add_argument('--execute-online',action='store_true')
    args=parser.parse_args();cfg=load_config(args.config)
    plan=dry_plan(args.action,cfg,step=args.step,run_dir=args.run_dir,handover=args.handover)
    if not args.execute_online:print(json.dumps(plan,ensure_ascii=False));return
    require(args.timeout_s>0 and args.restore_timeout_s>0,'positive bounded wait budgets required')
    runtime=LinuxRuntime(cfg,args.config);control=Controller(cfg,args.config,runtime)
    if args.action=='inspect':result=control.inspect()
    elif args.action=='prepare-handover':result=control.prepare_handover()
    elif args.action=='start':result=control.start(args.step,handover=args.handover)
    elif args.action=='status':result=control.status(args.run_dir)
    elif args.action=='stop':result=control.stop(args.run_dir,timeout_s=args.timeout_s)
    elif args.action=='rollback':result=control.rollback(args.run_dir,timeout_s=args.timeout_s,restore_timeout_s=args.restore_timeout_s)
    else:
        runtime.check_environment();require(args.run_dir,'explicit current service run-dir required for acceptance')
        current=control.status(args.run_dir)
        require(current['service'] and current['service'].get('step',current['service'].get('selected_step'))==5000,
                'historical acceptance golden is step5000 only; other service steps remain unaccepted')
        completed=subprocess.run([cfg['python'],'-I','-B',str(posix_path(cfg['remote_root'])/'service/acceptance.py'),
            '--config',str(args.config),'--run-dir',args.run_dir,'--execute-online'],check=False)
        raise SystemExit(completed.returncode)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    if result['status'] in ('STOP_UNCONFIRMED_RESTORE_BLOCKED','FP32_RESTORE_UNCONFIRMED','STARTUP_IDENTITY_UNCONFIRMED','SPAWN_OUTCOME_UNCONFIRMED'):raise SystemExit(2)


if __name__=='__main__':main()
