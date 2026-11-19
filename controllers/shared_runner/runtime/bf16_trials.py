"""Durable per-checkpoint trial allocation on the selected USB only.

Normal first allocation is trial 0 / seed 42. Reservations are immutable;
uncertain crash reservations are burned, never reused. A different USB is a
different ledger: this module does not claim unshared volumes coordinate.
Import does not inspect storage, create directories, or allocate a trial.
"""
from contextlib import AbstractContextManager
from copy import deepcopy
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

STEPS=(2000,5000,10000,15000,20000)
MAX_INDEX=1_000_000  # Match the runtime/manager lease protocol ceiling.
SCHEMA='tiger-h50-trial-ledger-v1'
RESERVATION=re.compile(r'trial-([0-9]{10})\.json\Z')
TEMPORARY=re.compile(r'\.counter-[0-9a-f]{32}\.tmp\Z')


def _require(ok,reason):
    if not ok:raise RuntimeError('H50 trial allocation: '+reason)


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode('utf-8')


def _loads(raw):
    if __package__:
        from agilex_control.h50_approved_contract import loads_unique_json
    else:
        from h50_approved_contract import loads_unique_json
    result=loads_unique_json(raw.decode('utf-8'))
    _require(isinstance(result,dict),'ledger JSON must be an object')
    return result


class _USBStore(AbstractContextManager):
    def __init__(self,cfg,step):
        from agilex_control.storage import verify_usb
        from agilex_control.usb_storage import open_usb_directory
        self.cfg=deepcopy(cfg);self.verify=verify_usb;self.open_dir=open_usb_directory
        self.directory=Path(__file__).resolve().parents[1]/'trial-ledger'/('checkpoint-%08d'%step)
        self.fd=self.lockfd=None
    def guard(self):
        root=Path(self.verify(self.cfg))
        _require(root==Path(self.cfg['usb']),'USB verifier returned another root')
        if self.fd is not None:
            device=os.fstat(self.fd).st_dev
            _require('%d:%d'%(os.major(device),os.minor(device))==self.cfg['usb_selection']['major_minor'],
                     'anchored USB device changed')
    def _open(self,name,flags,mode=0o600):
        _require(isinstance(name,str) and name not in ('','.','..') and '/' not in name and '\\' not in name,'unsafe ledger filename')
        fd=os.open(name,flags|os.O_NOFOLLOW,mode,dir_fd=self.fd)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_dev!=os.fstat(self.fd).st_dev:
            os.close(fd);raise RuntimeError('Trial file crossed selected filesystem or is not a regular file')
        return fd
    def __enter__(self):
        import fcntl
        _require(bool(self.cfg.get('usb_selection')),'frozen USB selection required; no legacy fallback')
        try:
            self.guard();self.fd=self.open_dir(self.directory,self.cfg,create=True)
            self.lockfd=self._open('.allocator.lock',os.O_RDWR|os.O_CREAT)
            deadline=time.monotonic()+10
            while True:
                try:fcntl.flock(self.lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    _require(time.monotonic()<deadline,'allocator busy; no trial assigned')
                    time.sleep(.01)
            self.guard();return self
        except BaseException:self.__exit__(None,None,None);raise
    def __exit__(self,*_args):
        for key in ('lockfd','fd'):
            descriptor=getattr(self,key,None)
            if descriptor is not None:os.close(descriptor);setattr(self,key,None)
    def names(self):return os.listdir(self.fd)
    def assert_file(self,name):
        descriptor=self._open(name,os.O_RDONLY);os.close(descriptor)
    def read(self,name):
        descriptor=self._open(name,os.O_RDONLY)
        with os.fdopen(descriptor,'rb') as handle:raw=handle.read(65537)
        _require(len(raw)<=65536,'ledger file exceeds size bound')
        return raw
    def create(self,name,value):
        self.guard();descriptor=self._open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL)
        # On failure leave the reservation/temp as evidence. Never erase and
        # retry an uncertain index; the next successful allocation burns it.
        with os.fdopen(descriptor,'wb') as handle:
            handle.write(_json(value)+b'\n');handle.flush();os.fsync(handle.fileno())
    def replace_state(self,value):
        name='.counter-'+uuid.uuid4().hex+'.tmp'
        self.create(name,value);self.guard()
        os.replace(name,'counter.json',src_dir_fd=self.fd,dst_dir_fd=self.fd)
        self.durable()
    def durable(self):
        self.guard()
        try:os.fsync(self.fd)
        except OSError as exc:
            unsupported={errno.EINVAL,getattr(errno,'ENOTSUP',errno.EINVAL),getattr(errno,'EOPNOTSUPP',errno.EINVAL)}
            if exc.errno not in unsupported:raise
            # GNU sync -f uses syncfs on this already anchored directory, not
            # a pathname that could have fallen back to the internal SSD.
            completed=subprocess.run(['sync','-f','/proc/self/fd/%d'%self.fd],pass_fds=(self.fd,),
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
            _require(completed.returncode==0,'selected USB filesystem sync failed')
        self.guard()


def _open_store(cfg,step):return _USBStore(cfg,step)


def allocate_trial(cfg,step,session_id,operation):
    """Reserve once before runtime/recording. Failures consume or burn indices.

    A returned receipt is durable. Allocation is not evidence that a model call
    or physical trial actually ran; its immutable run operation proves that.
    """
    _require(type(step) is int and step in STEPS,'unsupported checkpoint')
    _require(isinstance(session_id,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',session_id),'invalid session identity')
    _require(operation in ('h50-shadow','h50-acceptance','deploy-h50'),'operation cannot allocate a trial')
    selected=cfg.get('usb_selection')
    _require(isinstance(selected,dict) and selected.get('schema')=='tiger-usb-selection-v1','frozen selected USB required')
    volume=str(cfg.get('usb_uuid','')).upper()
    _require(volume and str(selected.get('uuid','')).upper()==volume,'selected volume identity mismatch')
    with _open_store(cfg,step) as store:
        store.guard();names=store.names();indices=[]
        for name in names:
            match=RESERVATION.fullmatch(name)
            if match:
                store.assert_file(name);indices.append(int(match.group(1)))
            elif name in ('.allocator.lock','counter.json') or TEMPORARY.fullmatch(name):
                store.assert_file(name)
            else:raise RuntimeError('Unexpected trial-ledger object; inspect before allocating: '+name)
        indices.sort()
        _require(not indices or (indices[0]==0 and len(indices)==indices[-1]+1),'trial history has gaps; refusing reset/reuse')
        if 'counter.json' not in names:
            _require(not indices,'counter missing with existing reservations; refusing history reset')
            state={'schema':SCHEMA,'checkpoint_step':step,'usb_uuid':volume,'next_index':0,'last_reservation_sha256':None}
            store.replace_state(state)
        else:state=_loads(store.read('counter.json'))
        _require(state.get('schema')==SCHEMA and type(state.get('checkpoint_step')) is int and state['checkpoint_step']==step and
                 state.get('usb_uuid')==volume,'ledger checkpoint/volume identity mismatch')
        next_index=state.get('next_index')
        _require(type(next_index) is int and 0<=next_index<=MAX_INDEX+1,'invalid durable next index')
        observed_next=indices[-1]+1 if indices else 0
        _require(next_index<=observed_next,'committed reservation history missing; refusing reuse')
        if next_index:
            committed_name='trial-%010d.json'%(next_index-1)
            committed_raw=store.read(committed_name);committed=_loads(committed_raw)
            _require(committed.get('schema')=='tiger-h50-trial-reservation-v1' and
                     committed.get('checkpoint_step')==step and committed.get('trial_index')==next_index-1 and
                     committed.get('usb_uuid')==volume and
                     hashlib.sha256(committed_raw).hexdigest()==state.get('last_reservation_sha256'),
                     'last committed reservation changed/corrupt')
        index=max(next_index,observed_next)
        _require(index<=MAX_INDEX,'trial seed/index range exhausted')
        reservation={'schema':'tiger-h50-trial-reservation-v1','checkpoint_step':step,'trial_index':index,
            'trial_seed':42+index,'usb_uuid':volume,'session_id':session_id,'operation':operation,
            'reserved_at_unix_ns':time.time_ns(),'allocation_id':uuid.uuid4().hex,
            'burned_uncertain_reservations':{'first':next_index,'count':index-next_index},
            'scope':'Monotonic on this selected USB ledger; reservation is not proof of inference/motion.'}
        filename='trial-%010d.json'%index
        store.create(filename,reservation);store.durable()
        raw=store.read(filename)
        new_state={'schema':SCHEMA,'checkpoint_step':step,'usb_uuid':volume,'next_index':index+1,
                   'last_reservation_sha256':hashlib.sha256(raw).hexdigest()}
        store.replace_state(new_state);store.guard()
        return {**reservation,'reservation_path':str(store.directory/filename),
                'ledger_path':str(store.directory/'counter.json'),'durable':True}
