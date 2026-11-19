"""FP32 journal anchored to one verified USB mount for the entire trial.

The full UUID/serial/sysfs verification runs at creation and in the executor's
250ms USB monitor. Every write independently checks the original kernel mount
ID and held directory devices. File creation is relative to held USB descriptors,
so detachment can fail a write but cannot redirect it to the NUC's internal disk.
No sensor or command operation exists here.
"""
from copy import deepcopy
from datetime import datetime,timezone
import json,os,re,stat,uuid,threading,queue,time,hashlib
from functools import wraps
from pathlib import Path


def require(ok,message):
    if not ok:raise RuntimeError('H50 anchored journal: '+message)


def locked(method):
    @wraps(method)
    def run(self,*args,**kwargs):
        with self.lock:return method(self,*args,**kwargs)
    return run


def mount_identity(root):
    """Kernel RAM read, not a subprocess or a cached assertion of USB presence."""
    escaped=str(root).replace('\\','\\134').replace(' ','\\040').replace('\t','\\011').replace('\n','\\012')
    matches=[]
    for line in Path('/proc/self/mountinfo').read_text(encoding='utf-8').splitlines():
        fields=line.split()
        if len(fields)>6 and fields[4]==escaped:
            require('rw' in fields[5].split(','),'selected mount is not writable')
            matches.append(tuple(fields[:6]))
    require(len(matches)==1,'selected mount absent, stacked, or ambiguous')
    return matches[0]


class H50USBJournal:
    def __init__(self,cfg,expected_episode_path):
        from .storage import verify_usb
        from .usb_storage import open_usb_directory
        self.lock=threading.RLock()
        self.cfg=deepcopy(cfg);self.episode_path=Path(expected_episode_path)
        root=Path(verify_usb(self.cfg))
        require(self.episode_path.is_absolute() and self.episode_path!=root and
                self.episode_path.is_relative_to(root) and '..' not in self.episode_path.parts,
                'episode outside selected USB')
        self.storage_binding={'mountpoint':str(root),'uuid':cfg['usb_uuid'],
                              'major_minor':cfg['usb_selection']['major_minor']}
        self.root=root;self.root_fd=self.episode_fd=None
        try:
            self.identity=mount_identity(root)
            self.root_fd=open_usb_directory(root,cfg)
            self.episode_fd=open_usb_directory(self.episode_path,cfg)
            self.device=os.fstat(self.root_fd).st_dev
            require('%s:%s'%(os.major(self.device),os.minor(self.device))==self.storage_binding['major_minor'],
                    'original descriptor is not selected device')
            self.verify()
        except BaseException:self.close();raise

    @locked
    def verify(self):
        require(self.root_fd is not None and self.episode_fd is not None,'journal is closed')
        require(mount_identity(self.root)==self.identity,'selected kernel mount changed or detached')
        root_info=os.stat(self.root,follow_symlinks=False)
        require(stat.S_ISDIR(root_info.st_mode) and os.path.samestat(root_info,os.fstat(self.root_fd)),
                'selected mount path changed')
        require(os.fstat(self.episode_fd).st_dev==self.device,'episode descriptor changed filesystem')
        current=os.dup(self.root_fd)
        try:
            for part in self.episode_path.relative_to(self.root).parts:
                following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
                os.close(current);current=following
                require(os.fstat(current).st_dev==self.device,'episode path crossed filesystem')
            require(os.path.samestat(os.fstat(current),os.fstat(self.episode_fd)),
                    'episode path no longer refers to the held recording directory')
        finally:os.close(current)

    @locked
    def _directory(self,path,*,create=False,exclusive=False):
        self.verify();path=Path(path)
        require(path.is_absolute() and path.is_relative_to(self.episode_path),'directory outside episode')
        parts=path.relative_to(self.episode_path).parts
        require(all(p not in ('.','..','') for p in parts),'invalid relative directory')
        current=os.dup(self.episode_fd)
        try:
            for index,part in enumerate(parts):
                if create:
                    try:
                        os.mkdir(part,mode=0o700,dir_fd=current)
                        os.fsync(current)
                    except FileExistsError:
                        if exclusive and index==len(parts)-1:raise
                following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
                if os.fstat(following).st_dev!=self.device:
                    os.close(following);raise RuntimeError('H50 journal nested mount changed filesystem')
                os.close(current);current=following
            result,current=current,None
            return result
        finally:
            if current is not None:os.close(current)

    @locked
    def write_json(self,path,value):
        raw=(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)+'\n').encode('utf-8')
        self.write_bytes(path,raw)

    @locked
    def write_bytes(self,path,raw):
        path=Path(path);require(path.name not in ('','.','..') and isinstance(raw,bytes),'invalid file name or bytes')
        parent=self._directory(path.parent);temporary='.'+path.name+'-'+uuid.uuid4().hex+'.tmp';exists=False
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent);exists=True
            if os.fstat(fd).st_dev!=self.device:
                os.close(fd);raise RuntimeError('H50 journal output crossed filesystem')
            with os.fdopen(fd,'wb') as handle:
                handle.write(raw);handle.flush();os.fsync(handle.fileno())
            self.verify()
            os.replace(temporary,path.name,src_dir_fd=parent,dst_dir_fd=parent);exists=False
            os.fsync(parent)
            self.verify()
        finally:
            if exists:
                try:os.unlink(temporary,dir_fd=parent)
                except OSError:pass
            os.close(parent)

    @locked
    def begin(self,request_id,value):
        require(isinstance(request_id,str) and re.fullmatch('[0-9a-f]{32}',request_id),'generated UUID required')
        stamp=datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M-%S')
        directory=self.episode_path/(stamp+'-fp32-shadow-'+request_id)
        fd=self._directory(directory,create=True,exclusive=True);os.close(fd)
        self.write_json(directory/'input.json',value)
        return directory

    @locked
    def finish(self,directory,value):
        directory=Path(directory)
        require(directory.parent==self.episode_path and directory.name.endswith(value['request_id']),
                'result directory binding mismatch')
        self.write_json(directory/'result.json',value)

    @locked
    def close(self):
        for key in ('episode_fd','root_fd'):
            fd=getattr(self,key,None)
            if fd is not None:os.close(fd);setattr(self,key,None)


class H50BufferedJournal:
    """Immutable bounded RAM handoff, ordered USB writer, mandatory final flush.

    Enqueue is not a persistence claim. The caller may commit a validated plan
    after enqueue, as explicitly authorized by Tiger. Only a complete close
    receipt establishes that all accepted files reached the selected USB.
    """
    def __init__(self,cfg,expected_episode_path):
        from .h50_approved_contract import require_approved_config
        settings=require_approved_config(cfg)
        require(settings['persistence_mode']=='bounded_ram_async_usb','explicit deferred-persistence contract required')
        self.cfg=deepcopy(cfg);self.episode_path=Path(expected_episode_path)
        self.disk=H50USBJournal(cfg,expected_episode_path);self.storage_binding=deepcopy(self.disk.storage_binding)
        self.root=self.disk.root;self.mount_identity=self.disk.identity
        self.root_stat=os.fstat(self.disk.root_fd);self.episode_stat=os.fstat(self.disk.episode_fd)
        self.limit_bytes=settings['journal_buffer_limit_bytes'];self.limit_records=settings['journal_buffer_limit_records']
        self.timeout=settings['usb_flush_timeout_s']
        self.lock=threading.RLock();self.pending=queue.Queue();self.closing=threading.Event();self.failed=threading.Event()
        self.accepted=self.written=self.pending_bytes=self.peak_bytes=self.peak_records=0
        self.error=None;self.failed_item=None;self.files=[];self.paths=set();self.final=None
        self.thread=threading.Thread(target=self._run,name='h50-bounded-ram-to-USB',daemon=True);self.thread.start()

    def check_alive(self):
        require(not self.failed.is_set(),'USB persistence worker failed: '+str(self.error))
        require(not self.closing.is_set(),'journal is closing')

    def verify(self):
        # Do not acquire the disk writer's lock: fsync must not delay inference.
        # Immutable identities were captured from its already-verified held fds.
        self.check_alive()
        require(mount_identity(self.root)==self.mount_identity,'selected mount changed/detached')
        require(os.path.samestat(os.stat(self.root,follow_symlinks=False),self.root_stat),
                'selected root path changed')
        current=os.stat(self.episode_path,follow_symlinks=False)
        require(stat.S_ISDIR(current.st_mode) and os.path.samestat(current,self.episode_stat) and
                self.episode_path.resolve(strict=True)==self.episode_path,'episode path changed or redirected')

    def _enqueue(self,path,value,*,create_directory=False):
        raw=(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)+'\n').encode('utf-8')
        path=Path(path)
        require(path.is_absolute() and path.is_relative_to(self.episode_path) and '..' not in path.parts,
                'queued file outside episode')
        with self.lock:
            self.check_alive()
            require(str(path) not in self.paths,'duplicate journal file; append-only records required')
            count=self.accepted-self.written
            if self.pending_bytes+len(raw)>self.limit_bytes or count>=self.limit_records:
                self.error='RAM evidence buffer full; no record silently dropped';self.failed.set()
                raise RuntimeError('H50 journal RAM evidence buffer full')
            self.accepted+=1;sequence=self.accepted;self.pending_bytes+=len(raw);self.paths.add(str(path))
            self.peak_bytes=max(self.peak_bytes,self.pending_bytes);self.peak_records=max(self.peak_records,count+1)
            self.pending.put_nowait((sequence,path,raw,create_directory))
        return {'sequence':sequence,'ram_accepted':True,'persisted':False,'bytes':len(raw)}

    def begin(self,request_id,value):
        require(isinstance(request_id,str) and re.fullmatch('[0-9a-f]{32}',request_id),'generated UUID required')
        stamp=datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M-%S')
        directory=self.episode_path/(stamp+'-fp32-shadow-'+request_id)
        self._enqueue(directory/'input.json',value,create_directory=True)
        return directory

    def finish(self,directory,value):
        directory=Path(directory)
        require(directory.parent==self.episode_path and directory.name.endswith(value['request_id']),
                'result directory binding mismatch')
        self._enqueue(directory/'result.json',value)

    def write_json(self,path,value):
        return self._enqueue(path,value)

    def _run(self):
        while not self.closing.is_set() or not self.pending.empty():
            try:item=self.pending.get(timeout=.05)
            except queue.Empty:continue
            sequence,path,raw,create=item
            try:
                if create:
                    fd=self.disk._directory(path.parent,create=True,exclusive=True);os.close(fd)
                self.disk.write_bytes(path,raw)
                entry={'sequence':sequence,'path':str(path),'bytes':len(raw),
                       'sha256':hashlib.sha256(raw).hexdigest(),'persisted_monotonic_ns':time.monotonic_ns()}
                with self.lock:
                    self.files.append(entry);self.written+=1;self.pending_bytes-=len(raw)
            except BaseException as exc:
                with self.lock:self.error=repr(exc);self.failed_item=item;self.failed.set()
                return  # Retain the failed item and pending immutable bytes in RAM.
            finally:self.pending.task_done()

    def status(self):
        with self.lock:
            return {'schema':'tiger-h50-async-usb-journal-v1','persistence_mode':'bounded_ram_async_usb',
                    'accepted':self.accepted,'written':self.written,'pending_records':self.accepted-self.written,
                    'pending_bytes':self.pending_bytes,'peak_pending_bytes':self.peak_bytes,
                    'peak_pending_records':self.peak_records,'limit_bytes':self.limit_bytes,
                    'limit_records':self.limit_records,'error':self.error,'worker_alive':self.thread.is_alive(),
                    'unflushed_data_is_only_in_volatile_ram':self.accepted!=self.written}

    def close(self):
        if self.final is not None:
            if not self.thread.is_alive():self.disk.close()
            return deepcopy(self.final)  # Never upgrade a timeout/failure to PASS.
        self.closing.set();self.thread.join(self.timeout)
        result=self.status();result['flush_finished_monotonic_ns']=time.monotonic_ns()
        complete=(not self.thread.is_alive() and self.error is None and self.accepted==self.written and self.pending_bytes==0)
        result.update(status='USB_FLUSH_COMPLETE' if complete else 'FAILED_USB_NOT_FULLY_SAVED',complete=complete,
                      files=deepcopy(self.files),storage_binding=deepcopy(self.storage_binding))
        proof=self.episode_path/'evidence'/'h50-journal-persistence.json';result['report_path']=str(proof)
        if not self.thread.is_alive():
            try:self.disk.write_json(proof,result)
            except BaseException as exc:
                result.update(status='FAILED_USB_NOT_FULLY_SAVED',complete=False,proof_write_error=repr(exc))
            finally:self.disk.close()
        self.final=deepcopy(result)
        return result
