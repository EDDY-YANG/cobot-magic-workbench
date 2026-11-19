"""Windows/PC independent BF16 entry. Default prints a plan, never connects.

Explicit --execute-online is required for every SSH/upload/service operation.
No SSH configuration edits, passwords, model weights, or automatic FP32 pause.
"""
from __future__ import annotations
import argparse
import base64
import gzip
import hashlib
import inspect
import io
import json
import os
from pathlib import Path,PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from service.control import load_config,dry_plan,posix_path,under

MANIFEST_SCHEMA='tiger-bf16-service-source-manifest-v1'
SOURCE_SUFFIXES={'.py','.json','.md','.ps1'}
FORBIDDEN_PARTS={'receipts','runs','outputs','dataset','weights','models','.git','__pycache__'}
MAX_PAYLOAD_BYTES=32*1024*1024
DEFAULT_SSH_TARGET='lab-4090'
DEFAULT_SSH_CONFIG=Path('C:/Users/Administrator/.ssh/lab-4090-only/config')


def strict_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    def bad(value):raise ValueError('nonfinite JSON value '+value)
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=bad)


def checked_manifest(value):
    if not isinstance(value,dict) or value.get('schema')!=MANIFEST_SCHEMA or not isinstance(value.get('files'),dict) or not value['files']:
        raise ValueError('exact root-frozen source manifest required')
    for name,digest in value['files'].items():
        if not isinstance(name,str) or '\\' in name or '\x00' in name:raise ValueError('invalid source member name')
        p=PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or str(p)!=name or not p.parts or any(part in FORBIDDEN_PARTS for part in p.parts):raise ValueError('non-source or escaping manifest member')
        if name=='source_manifest.json' or p.suffix.lower() not in SOURCE_SUFFIXES:raise ValueError('source-only members required; manifest never self-hashes')
        if not isinstance(digest,str) or re.fullmatch('[0-9a-f]{64}',digest) is None:raise ValueError('invalid source SHA256')
    if 'service_config.json' not in value['files'] or 'service/server.py' not in value['files'] or 'service/control.py' not in value['files']:
        raise ValueError('config, server and control must be source-bound')
    return value


def build_source_payload(root,manifest_path):
    root=Path(root).resolve();manifest_path=Path(manifest_path)
    if manifest_path.resolve()!=root/'source_manifest.json':raise ValueError('manifest must be this bundle root source_manifest.json')
    raw=manifest_path.read_bytes();manifest=checked_manifest(strict_json(raw));files={};total=0
    for name,expected in manifest['files'].items():
        path=root/name
        if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('source symlink or escape refused')
        parent=path.parent
        while parent!=root:
            if parent.is_symlink():raise ValueError('source parent symlink refused')
            parent=parent.parent
        data=path.read_bytes();total+=len(data)
        if len(data)>8*1024*1024 or total>MAX_PAYLOAD_BYTES:raise ValueError('source package size exceeds bounded source-only budget')
        if hashlib.sha256(data).hexdigest()!=expected:raise ValueError('source SHA differs: '+name)
        files[name]=data
    files['source_manifest.json']=raw
    buffer=io.BytesIO()
    with gzip.GzipFile(fileobj=buffer,mode='wb',mtime=0) as compressed:
        with tarfile.open(fileobj=compressed,mode='w') as archive:
            for name,data in sorted(files.items()):
                info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o600;info.mtime=0;info.uid=info.gid=0
                archive.addfile(info,io.BytesIO(data))
    payload=buffer.getvalue()
    if len(payload)>MAX_PAYLOAD_BYTES:raise ValueError('compressed source package too large')
    return payload,{'payload_sha256':hashlib.sha256(payload).hexdigest(),'manifest_sha256':hashlib.sha256(raw).hexdigest(),
        'source_file_count':len(manifest['files']),'source_bytes':total,'payload_bytes':len(payload),
        'manifest_in_payload':True,'weights_or_runtime_data_in_payload':False}


def unpack_source_payload(payload,remote_root,manifest_sha256):
    """Shared by actual remote bootstrap and local temporary-directory tests.

    Validate everything before exclusive mkdir. No extractall, links, absolute
    members, overwrite, merge, automatic retry, or removal of partial targets.
    """
    if len(payload)>MAX_PAYLOAD_BYTES:raise ValueError('payload size limit')
    with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as archive:
        members=[];total_size=0
        for member in archive:
            total_size+=member.size
            if len(members)>=4096 or total_size>MAX_PAYLOAD_BYTES:raise ValueError('source archive item/size limit')
            members.append(member)
        names=[member.name for member in members]
        if len(names)!=len(set(names)) or 'source_manifest.json' not in names:raise ValueError('duplicate/missing manifest member')
        for member in members:
            p=PurePosixPath(member.name)
            if not member.isfile() or member.issym() or member.islnk() or p.is_absolute() or '..' in p.parts or '\\' in member.name or str(p)!=member.name:
                raise ValueError('unsafe archive member/link')
            if member.size<0 or member.size>8*1024*1024:raise ValueError('member source size limit')
        if sum(member.size for member in members)>MAX_PAYLOAD_BYTES:raise ValueError('unpacked source size limit')
        raw=archive.extractfile('source_manifest.json').read()
        if hashlib.sha256(raw).hexdigest()!=manifest_sha256:raise ValueError('manifest transport digest differs')
        manifest=checked_manifest(strict_json(raw))
        if set(names)!=set(manifest['files'])|{'source_manifest.json'}:raise ValueError('unexpected/missing archive members')
        contents={member.name:archive.extractfile(member).read() for member in members}
        for name,expected in manifest['files'].items():
            if hashlib.sha256(contents[name]).hexdigest()!=expected:raise ValueError('payload file SHA differs: '+name)
    root=Path(remote_root)
    root.mkdir(mode=0o700)  # Exclusive: existing or partially installed root is preserved.
    for name,data in sorted(contents.items()):
        target=root/name
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):raise ValueError('target parent escaped; partial root preserved')
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(descriptor,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    for name,expected in manifest['files'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('installed source SHA differs; partial root preserved')
    return {'status':'SOURCE_ONLY_UPLOAD_SHA_VERIFIED','remote_root':str(root),'source_files_verified':len(manifest['files']),
            'manifest_sha256':manifest_sha256,'existing_root_overwritten':False,'runtime_started':False}


def remote_upload_script():
    preamble='''import base64,hashlib,io,json,os,re,socket,subprocess,sys,tarfile\nfrom pathlib import Path,PurePosixPath\n'''
    constants='MANIFEST_SCHEMA='+repr(MANIFEST_SCHEMA)+'\nSOURCE_SUFFIXES='+repr(SOURCE_SUFFIXES)+'\nFORBIDDEN_PARTS='+repr(FORBIDDEN_PARTS)+'\nMAX_PAYLOAD_BYTES='+str(MAX_PAYLOAD_BYTES)+'\n'
    main='''
request=json.loads(base64.b64decode(sys.argv[1]))
if socket.gethostname()!=request['host']:raise RuntimeError('wrong physical host')
if Path(sys.executable).resolve()!=Path(request['python']).resolve():raise RuntimeError('wrong absolute interpreter')
root=PurePosixPath(request['remote_root'])
if not root.is_absolute() or '..' in root.parts or str(root)!=request['remote_root']:raise RuntimeError('invalid new remote root')
mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(root.parent),'-o','TARGET,UUID'],text=True,timeout=10))['filesystems']
if len(mount)!=1 or mount[0].get('target')!=request['mount']['target'] or mount[0].get('uuid')!=request['mount']['uuid']:raise RuntimeError('wrong physical SSD')
payload=sys.stdin.buffer.read(MAX_PAYLOAD_BYTES+1)
if len(payload)!=request['payload_bytes'] or hashlib.sha256(payload).hexdigest()!=request['payload_sha256']:raise RuntimeError('incomplete/corrupt upload; no automatic retry')
result=unpack_source_payload(payload,str(root),request['manifest_sha256'])
cfg=json.loads((Path(str(root))/'service_config.json').read_text())
if any(cfg.get(k)!=request[k] for k in ('remote_root','host','python','mount')):raise RuntimeError('uploaded config differs; installed target preserved for review')
print(json.dumps(result),flush=True)
'''
    return preamble+constants+inspect.getsource(strict_json)+'\n'+inspect.getsource(checked_manifest)+'\n'+inspect.getsource(unpack_source_payload)+'\n'+main


def ssh_prefix(target,ssh_config=None,host_name=None):
    if not isinstance(target,str) or not target or target.startswith('-') or re.fullmatch(r'[A-Za-z0-9_.@:\[\]-]+',target) is None:
        raise ValueError('explicit SSH alias/address required; configuration is never modified')
    args=['ssh','-T']
    if ssh_config is not None:
        config=Path(ssh_config)
        if not config.is_file():raise ValueError('explicit existing SSH configuration file missing')
        args+=['-F',str(config)]
    if host_name is not None:
        if not isinstance(host_name,str) or re.fullmatch(r'[A-Za-z0-9_.:\[\]-]+',host_name) is None or host_name.startswith('-'):
            raise ValueError('invalid explicit HostName override')
        args+=['-o','HostName='+host_name]
    # Existing -F file supplies its pinned HostKeyAlias/key. Never edit it or
    # replace it with an IP-based trust-on-first-use/accept-new policy.
    return args+['-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',
                 '-o','ServerAliveInterval=10','-o','ServerAliveCountMax=3',target]


def make_remote_command(cfg,action,*,step=None,run_dir=None,handover=None):
    dry_plan(action,cfg,step=step,run_dir=run_dir,handover=handover)
    args=[cfg['python'],'-I','-B',str(posix_path(cfg['remote_root'])/'service/control.py'),action,
        '--config',str(posix_path(cfg['remote_root'])/'service_config.json'),'--execute-online']
    if step is not None:args+=['--step',str(step)]
    if run_dir is not None:args+=['--run-dir',run_dir]
    if handover is not None:args+=['--handover',handover]
    return args


def dispatch(args,*,run=subprocess.run):
    cfg=load_config(args.config)
    if args.action=='upload':
        plan={'status':'DRY_RUN_NO_REMOTE_NO_MODEL_NO_GPU','action':'upload','remote_root':cfg['remote_root'],
              'source_manifest':str(args.manifest),'new_root_exclusive':True,'weights_uploaded':False}
    else:plan=dry_plan(args.action,cfg,step=args.step,run_dir=args.run_dir,handover=args.handover)
    plan.update(ssh_target=args.ssh_target,ssh_config=str(args.ssh_config) if args.ssh_config else None,
                host_name=args.host_name,online_requires='--execute-online')
    if not args.execute_online:return plan
    prefix=ssh_prefix(args.ssh_target,args.ssh_config,args.host_name)
    if args.action=='upload':
        payload,binding=build_source_payload(ROOT,args.manifest)
        request={**binding,**{key:cfg[key] for key in ('host','python','mount','remote_root')}}
        encoded=base64.b64encode(json.dumps(request,separators=(',',':')).encode()).decode()
        command=[cfg['python'],'-I','-B','-c',remote_upload_script(),encoded]
        try:result=run(prefix+[shlex.join(command)],input=payload,capture_output=True,timeout=180,shell=False)
        except (subprocess.TimeoutExpired,OSError) as exc:return {'status':'UPLOAD_OUTCOME_UNKNOWN_CHECK_REMOTE_DO_NOT_RETRY_OVERWRITE','error_type':type(exc).__name__,'binding':binding}
    else:
        command=make_remote_command(cfg,args.action,step=args.step,run_dir=args.run_dir,handover=args.handover)
        try:result=run(prefix+[shlex.join(command)],stdin=subprocess.DEVNULL,capture_output=True,timeout=1000,shell=False)
        except (subprocess.TimeoutExpired,OSError) as exc:return {'status':'REMOTE_ACTION_OUTCOME_UNKNOWN','action':args.action,'error_type':type(exc).__name__,
            'warning':'Inspect this experiment receipt/owner; do not duplicate start or assume cleanup/restoration.'}
    if args.action=='upload' and result.returncode==255:
        return {'status':'UPLOAD_OUTCOME_UNKNOWN_CHECK_REMOTE_DO_NOT_RETRY_OVERWRITE','binding':binding,'remote_root':cfg['remote_root'],
                'returncode':result.returncode,'warning':'SSH transport exited without a confirmed installation result; existing/partial target is never overwritten.'}
    stdout=result.stdout.decode('utf-8',errors='strict') if isinstance(result.stdout,bytes) else result.stdout
    lines=[line for line in stdout.splitlines() if line.strip()]
    value=strict_json(lines[-1]) if lines else {'status':'REMOTE_REPLY_MISSING'}
    return {'status':'REMOTE_COMMAND_COMPLETED' if result.returncode==0 else 'REMOTE_COMMAND_FAILED',
        'action':args.action,'returncode':result.returncode,'result':value,
        'stderr_tail':(result.stderr.decode(errors='replace') if isinstance(result.stderr,bytes) else result.stderr)[-4096:]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('inspect','upload','prepare-handover','start','status','stop','rollback','accept'))
    parser.add_argument('--config',type=Path,default=ROOT/'service_config.json');parser.add_argument('--manifest',type=Path,default=ROOT/'source_manifest.json')
    parser.add_argument('--ssh-target',default=DEFAULT_SSH_TARGET);parser.add_argument('--ssh-config',type=Path,default=DEFAULT_SSH_CONFIG)
    parser.add_argument('--host-name');parser.add_argument('--step',type=int);parser.add_argument('--run-dir');parser.add_argument('--handover')
    parser.add_argument('--execute-online',action='store_true');args=parser.parse_args()
    value=dispatch(args);print(json.dumps(value,ensure_ascii=False,allow_nan=False))
    if value['status'] not in ('DRY_RUN_NO_REMOTE_NO_MODEL_NO_GPU','REMOTE_COMMAND_COMPLETED'):raise SystemExit(2)


if __name__=='__main__':main()
