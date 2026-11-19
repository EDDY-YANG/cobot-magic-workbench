# 完整单行命令：Windows PC PowerShell；无需预定义变量，复制时去掉行首 #。
# 按需选择一条命令，不要把所有示例一起执行。
# # 本文件是传输模块，经统一入口调用；sync 只同步新库源码。
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' sync
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' stop
# 本轮仅补充命令注释；未运行测试、SSH、模型、相机或机械臂。
"""Windows transport: source synchronization and explicit SSH delegation.

Does not hold credentials, execute camera/CAN code locally, or silently synchronize
on a motion command. Sync writes only this new source bundle on verified USB.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile

ENTRY = "2026-10-06-14-05-agilex.py"
CONFIG = "2026-10-06-14-05-config.json"
SUFFIXES = {".py", ".json", ".md", ".ps1", ".sh", ".toml", ".code-workspace", ".cmd",
            ".html", ".css", ".js", ".mjs", ".urdf", ".stl", ".txt"}
MAX_TRANSFER_HEADER_BYTES = 4 * 1024 * 1024

# Fixed remote program: cfg/manifests travel through stdin, never the Windows
# process command line. readline consumes exactly one JSON line, leaving the
# binary tar stream at its original first byte.
TRANSFER_HEADER_READER = """
import hashlib,json,os,re,socket,subprocess,sys,tarfile
from pathlib import Path,PurePosixPath
def read_transfer_header(source):
 raw=source.readline(4194305)
 if len(raw)>4194304 or not raw.endswith(b'\\n'): raise RuntimeError('Invalid/oversized transfer header')
 def pairs(items):
  result={}
  for key,value in items:
   if key in result: raise RuntimeError('Duplicate transfer header key')
   result[key]=value
  return result
 def invalid(value): raise RuntimeError('Nonfinite transfer header value')
 try: header=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=invalid)
 except (ValueError,UnicodeError,RecursionError) as exc: raise RuntimeError('Invalid transfer JSON header') from exc
 if not isinstance(header,dict) or header.get('schema')!='tiger-source-transfer-v1': raise RuntimeError('Wrong transfer header schema')
 cfg,expected=header.get('cfg'),header.get('manifest')
 if not isinstance(cfg,dict) or any(not isinstance(cfg.get(k),str) or not cfg[k] for k in ('hostname','usb','usb_uuid','remote_bundle')): raise RuntimeError('Invalid transfer configuration')
 if not isinstance(expected,dict): raise RuntimeError('Invalid source manifest')
 for name,digest in expected.items():
  if not isinstance(name,str) or not name or '\\\\' in name or '\\x00' in name: raise RuntimeError('Invalid source member name')
  parts=PurePosixPath(name)
  if parts.is_absolute() or '..' in parts.parts or parts.as_posix()!=name or name=='.': raise RuntimeError('Unsafe source member path')
  if not isinstance(digest,str) or re.fullmatch('[0-9a-f]{64}',digest) is None: raise RuntimeError('Invalid source digest')
 return cfg,expected
cfg,expected=read_transfer_header(sys.stdin.buffer)
"""

TRANSFER_STORAGE_GUARDS = """
if socket.gethostname()!=cfg['hostname']: raise RuntimeError('Wrong host')
usb=Path(cfg['usb'])
if not os.path.ismount(usb): raise RuntimeError('USB not mounted; no fallback')
if subprocess.check_output(['findmnt','-n','-o','UUID','--target',str(usb)],text=True).strip().upper()!=cfg['usb_uuid'].upper(): raise RuntimeError('USB UUID mismatch')
root=Path(cfg['remote_bundle'])
if not usb.is_absolute() or not root.is_absolute() or usb not in root.parents: raise RuntimeError('New source must remain under USB')
try: root.resolve().relative_to(usb.resolve())
except ValueError: raise RuntimeError('Source path escaped selected USB')
"""


def source_transfer_header(cfg, manifest):
    raw = json.dumps({'schema': 'tiger-source-transfer-v1', 'cfg': cfg, 'manifest': manifest},
                     ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
    if len(raw) > MAX_TRANSFER_HEADER_BYTES:
        raise RuntimeError('Transfer header exceeds bounded stdin protocol')
    return raw


def source_bytes(root, name, cfg=None):
    if name == CONFIG and cfg and cfg.get('usb_selection'):
        return (json.dumps(cfg, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    return (Path(root) / name).read_bytes()


def sources(root, cfg=None):
    result = {}
    root = Path(root)
    excluded = {"runs", "model-cache", "__pycache__", ".git", "pc-receipts", "upstream", "models", "node_modules"}
    for directory, subdirs, filenames in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in excluded and not name.startswith(".venv")]
        for filename in filenames:
            path = Path(directory) / filename
            if path.suffix.lower() in SUFFIXES:
                name = path.relative_to(root).as_posix()
                result[name] = hashlib.sha256(source_bytes(root, name, cfg)).hexdigest()
    return dict(sorted(result.items()))


def ssh_base(cfg):
    if sys.platform == "win32":
        executable = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/OpenSSH/ssh.exe"
    elif sys.platform.startswith("linux"):
        executable = Path("/usr/bin/ssh")
    else:
        raise RuntimeError("Unsupported operator platform; no SSH fallback attempted")
    if not executable.is_file():
        raise RuntimeError("Native OpenSSH is missing; no installation attempted")
    return [str(executable), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", cfg["ssh_alias"]]


def remote_python(cfg, code):
    encoded = base64.b64encode(code.encode()).decode()
    return shlex.join(["/usr/bin/python3", "-I", "-B", "-c", "import base64;exec(base64.b64decode(" + repr(encoded) + "))"])


def discover_storage(cfg):
    """Probe real robot USB metadata before a source bundle exists; read-only."""
    module = (Path(__file__).parent / 'usb_storage.py').read_text(encoding='utf-8')
    program = module + '\n' + """
base_cfg=BASE_CONFIG
result=discover_usb_storage(base_cfg['hostname'])
for candidate in result['candidates']:
 if candidate['eligible']:
  selected=config_for_storage(base_cfg,candidate)
  bundle=Path(selected['remote_bundle'])
  candidate['runtime_bundle_exists']=bundle.exists()
  candidate['runtime_bundle_is_dir']=bundle.is_dir()
print(json.dumps(result,ensure_ascii=False,allow_nan=False))
""".replace('BASE_CONFIG', repr(cfg))
    command = shlex.join(['/usr/bin/python3', '-I', '-B', '-c',
                         "import sys;exec(compile(sys.stdin.read(),'usb-read-only-discovery','exec'))"])
    run = subprocess.run([*ssh_base(cfg), command], input=program.encode('utf-8'),
                         capture_output=True, timeout=18)
    if run.returncode:
        raise RuntimeError('USB discovery failed: ' + run.stderr.decode('utf-8', 'replace')[-1500:])
    result = json.loads(run.stdout.decode('utf-8'))
    if result.get('schema') != 'tiger-usb-inventory-v1' or result.get('hostname') != cfg['hostname']:
        raise RuntimeError('USB discovery identity/schema mismatch')
    return result


def remote_web_command(cfg):
    """The checked delivered config contains the frozen selected USB identity."""
    return shlex.join(['env', '-u', 'PYTHONPATH', '-u', 'LD_PRELOAD',
        'LD_LIBRARY_PATH=' + cfg['hardware_library_path'], 'PYTHONNOUSERSITE=1', 'PYTHONUTF8=1',
        cfg['hardware_python'], '-I', '-B', cfg['remote_bundle'] + '/2026-10-06-15-57-web.py', '--agent'])


def _selected_transfer_guards(cfg):
    if not cfg.get('usb_selection'):
        return ''
    # This fixed stdlib module is executed from stdin-supplied trusted source,
    # before any writes; it does not depend on a preexisting remote bundle.
    return """
usb_guard_source=cfg.pop('_usb_guard_source',None)
if not isinstance(usb_guard_source,str): raise RuntimeError('Selected USB guard source missing')
usb_guard_ns={}
exec(compile(usb_guard_source,'trusted-usb-source-guard','exec'),usb_guard_ns)
mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(usb),'-o','TARGET,SOURCE,FSTYPE,UUID,OPTIONS,MAJ:MIN'],text=True,timeout=5))['filesystems'][0]
if mount.get('target')!=str(usb) or 'rw' not in mount.get('options','').split(','): raise RuntimeError('USB mount not writable/exact')
usb_guard_ns['verify_frozen_identity'](cfg,mount)
"""


def _transfer_cfg(cfg):
    if not cfg.get('usb_selection'):
        return cfg
    return {**cfg, '_usb_guard_source':
            (Path(__file__).parent / 'usb_storage.py').read_text(encoding='utf-8')}


def sync(cfg, root, *, require_absent=False):
    manifest = sources(root, cfg)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name in manifest:
            data = source_bytes(root, name, cfg)
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o644
            archive.addfile(entry, io.BytesIO(data))
    code = TRANSFER_HEADER_READER + TRANSFER_STORAGE_GUARDS + _selected_transfer_guards(cfg) + """
for proc in Path('/proc').iterdir():
 if not proc.name.isdigit(): continue
 try: args=(proc/'cmdline').read_bytes().split(bytes([0]))
 except (PermissionError,FileNotFoundError,ProcessLookupError): continue
 if any(arg in (str(root/'2026-10-06-14-05-agilex.py').encode(), str(root/'2026-10-06-15-57-web.py').encode()) for arg in args):
  raise RuntimeError('This source bundle is running; stop its services/control before sync')
root_fd=None
if cfg.get('usb_selection'):
 # Anchor every directory/file on this actual USB filesystem. If it disappears,
 # operations fail or use the still-open filesystem; they cannot fall through
 # to the internal directory underneath the former mountpoint.
 usb_fd=os.open(usb,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:
  dev=os.fstat(usb_fd).st_dev
  if str(os.major(dev))+':'+str(os.minor(dev))!=cfg['usb_selection']['major_minor']: raise RuntimeError('USB changed before source initialization')
  cursor=os.dup(usb_fd)
  parts=root.relative_to(usb).parts
  for index,part in enumerate(parts):
   try: os.mkdir(part,dir_fd=cursor)
   except FileExistsError:
    if cfg.get('_initialize_absent_only') and index==len(parts)-1: raise RuntimeError('Runtime bundle already exists; explicit sync required')
   following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=cursor)
   if os.fstat(following).st_dev!=dev:
    os.close(following);os.close(cursor)
    raise RuntimeError('Nested source directory crossed outside selected USB filesystem')
   os.close(cursor);cursor=following
  root_fd=cursor
 finally: os.close(usb_fd)
else:
 root.mkdir(parents=True,exist_ok=not cfg.get('_initialize_absent_only'))
def anchored_member(name,write=False,data=None):
 cursor=os.dup(root_fd)
 try:
  parts=name.split('/')
  for part in parts[:-1]:
   if write:
    try: os.mkdir(part,dir_fd=cursor)
    except FileExistsError: pass
   following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=cursor)
   if os.fstat(following).st_dev!=dev:
    os.close(following)
    raise RuntimeError('Nested source member crossed outside selected USB filesystem')
   os.close(cursor);cursor=following
  flags=(os.O_WRONLY|os.O_CREAT) if write else os.O_RDONLY
  descriptor=os.open(parts[-1],flags|os.O_NOFOLLOW,0o644,dir_fd=cursor)
  if os.fstat(descriptor).st_dev!=dev:
   os.close(descriptor)
   raise RuntimeError('Source file crossed outside selected USB filesystem')
  with os.fdopen(descriptor,'wb' if write else 'rb') as handle:
   if write:
    os.ftruncate(handle.fileno(),0)
    handle.write(data)
   else: return handle.read()
 finally: os.close(cursor)
seen=set()
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as tar:
 for entry in tar:
  if not entry.isfile() or entry.name not in expected or entry.name in seen: raise RuntimeError('Unexpected/duplicate source member')
  seen.add(entry.name)
  p=root.joinpath(*entry.name.split('/'))
  if p.is_symlink() or any(x.is_symlink() for x in p.parents if x!=usb.parent): raise RuntimeError('Symlink source refused')
  data=tar.extractfile(entry).read()
  if hashlib.sha256(data).hexdigest()!=expected[entry.name]: raise RuntimeError('Transfer digest differs')
  if root_fd is not None: anchored_member(entry.name,True,data)
  else:
   p.parent.mkdir(parents=True,exist_ok=True)
   p.write_bytes(data)
if seen!=set(expected): raise RuntimeError('Delivery incomplete: missing archive members')
actual={name:hashlib.sha256(anchored_member(name) if root_fd is not None else (root/name).read_bytes()).hexdigest() for name in expected}
if root_fd is not None: os.close(root_fd)
if actual!=expected: raise RuntimeError('Delivery incomplete')
print(json.dumps({'status':'SOURCE_SYNCED_NO_ROBOT_COMMAND','files':len(actual),'root':str(root),'hashes':actual}))
"""
    compile(code, "agilex-source-sync-remote", "exec")
    transfer_cfg = _transfer_cfg(cfg)
    if require_absent:
        transfer_cfg = {**transfer_cfg, '_initialize_absent_only': True}
    result = subprocess.run([*ssh_base(cfg), remote_python(cfg, code)],
                            input=source_transfer_header(transfer_cfg, manifest) + stream.getvalue(),
                            capture_output=True, timeout=90)
    print(result.stdout.decode("utf-8", "replace"))
    if result.returncode:
        print(result.stderr.decode("utf-8", "replace"), file=sys.stderr)
    return result.returncode


def check_sources(cfg, root):
    expected = sources(root, cfg)
    code = TRANSFER_HEADER_READER + TRANSFER_STORAGE_GUARDS + _selected_transfer_guards(cfg) + """
actual={}
for name in expected:
 p=root.joinpath(*name.split('/'))
 if p.is_symlink() or any(x.is_symlink() for x in p.parents if x!=usb.parent): raise RuntimeError('Symlink source refused')
 actual[name]=hashlib.sha256(p.read_bytes()).hexdigest()
print(json.dumps(actual))
"""
    compile(code, 'agilex-source-check-remote', 'exec')
    run = subprocess.run([*ssh_base(cfg), remote_python(cfg, code)], input=source_transfer_header(_transfer_cfg(cfg), expected),
                         capture_output=True, timeout=20)
    if run.returncode or json.loads(run.stdout.decode('utf-8')) != expected:
        raise RuntimeError("PC/robot source mismatch or not yet synchronized. Run 'sync' explicitly. "
                           + run.stderr.decode('utf-8', 'replace')[-2000:])


def prepare_session_config(cfg, root):
    """On explicit connect, refresh only an idle bundle's storage binding.

    Source/model/camera/control changes still require explicit synchronization.
    This is not invoked by discovery or automatic page refresh.
    """
    if not cfg.get('usb_selection'):
        return {'status': 'LEGACY_EXPLICIT_USB_CONFIG', 'changed': False}
    expected = sources(root, cfg)
    if CONFIG not in expected:
        raise RuntimeError('Runtime configuration is missing from source manifest')
    code = TRANSFER_HEADER_READER + TRANSFER_STORAGE_GUARDS + _selected_transfer_guards(cfg) + """
config_name='2026-10-06-14-05-config.json'
root_fd=usb_guard_ns['open_usb_directory'](root,cfg)
root_device=os.fstat(root_fd).st_dev
def read_member(name,limit=None):
 cursor=os.dup(root_fd)
 try:
  parts=name.split('/')
  for part in parts[:-1]:
   following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=cursor)
   if os.fstat(following).st_dev!=root_device:
    os.close(following)
    raise RuntimeError('Prepared source directory crossed outside selected USB filesystem')
   os.close(cursor);cursor=following
  descriptor=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=cursor)
  if os.fstat(descriptor).st_dev!=root_device:
   os.close(descriptor)
   raise RuntimeError('Prepared source file crossed outside selected USB filesystem')
  with os.fdopen(descriptor,'rb') as handle: return handle.read() if limit is None else handle.read(limit)
 finally: os.close(cursor)
def require_idle_bundle():
 import errno
 entry_names={'2026-10-06-14-05-agilex.py','2026-10-06-15-57-web.py'}
 for proc in Path('/proc').iterdir():
  if not proc.name.isdigit(): continue
  try: args=(proc/'cmdline').read_bytes().split(bytes([0]))
  except (PermissionError,FileNotFoundError,ProcessLookupError): continue
  if any(Path(os.fsdecode(arg)).name in entry_names for arg in args if arg):
   raise RuntimeError('A control bundle is running; cannot refresh storage identity')
 port=cfg.get('hub_port')
 if type(port) is not int or not 1<=port<=65535: raise RuntimeError('Invalid Hub port; cannot verify idle storage')
 with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:
  probe.settimeout(.3)
  result=probe.connect_ex(('127.0.0.1',port))
  if result==0:
   raise RuntimeError('Shared Hub is listening; cannot refresh storage identity')
  if result!=errno.ECONNREFUSED: raise RuntimeError('Shared Hub idle state could not be verified')
try:
 actual_sources={}
 for name,digest in expected.items():
  if name==config_name: continue
  actual_sources[name]=hashlib.sha256(read_member(name)).hexdigest()
 previous_bytes=read_member(config_name,1048577)
 if len(previous_bytes)>1048576: raise RuntimeError('Runtime configuration is oversized')
 def pairs(items):
  result={}
  for key,value in items:
   if key in result: raise RuntimeError('Duplicate runtime configuration key')
   result[key]=value
  return result
 previous=json.loads(previous_bytes.decode('utf-8'),object_pairs_hook=pairs)
 usb_guard_ns['validate_session_config_refresh'](previous,cfg,actual_sources,expected)
 target_bytes=(json.dumps(cfg,ensure_ascii=False,indent=2,allow_nan=False)+'\\n').encode('utf-8')
 if hashlib.sha256(target_bytes).hexdigest()!=expected[config_name]: raise RuntimeError('Prepared config differs from trusted manifest')
 changed=previous_bytes!=target_bytes
 if changed:
  require_idle_bundle()
  # Recheck current mount/device immediately before writing; all output is
  # anchored on the original root descriptor, never the mountpoint beneath it.
  mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(usb),'-o','TARGET,SOURCE,FSTYPE,UUID,OPTIONS,MAJ:MIN'],text=True,timeout=5))['filesystems'][0]
  if mount.get('target')!=str(usb) or str(mount.get('uuid','')).upper()!=cfg['usb_uuid'].upper() or 'rw' not in mount.get('options','').split(','): raise RuntimeError('USB changed before session preparation')
  usb_guard_ns['verify_frozen_identity'](cfg,mount)
  name='.session-config-'+str(os.getpid())+'-'+os.urandom(8).hex()+'.tmp'
  temporary=False
  try:
   descriptor=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root_fd)
   temporary=True
   if os.fstat(descriptor).st_dev!=root_device:
    os.close(descriptor)
    raise RuntimeError('Prepared configuration crossed outside selected USB filesystem')
   with os.fdopen(descriptor,'wb') as handle:
    handle.write(target_bytes);handle.flush();os.fsync(handle.fileno())
   require_idle_bundle()
   os.replace(name,config_name,src_dir_fd=root_fd,dst_dir_fd=root_fd)
   temporary=False
  finally:
   if temporary:
    try: os.unlink(name,dir_fd=root_fd)
    except OSError: pass
 if hashlib.sha256(read_member(config_name)).hexdigest()!=expected[config_name]: raise RuntimeError('Session config final readback failed')
 print(json.dumps({'status':'SESSION_STORAGE_IDENTITY_REFRESHED' if changed else 'SESSION_CONFIG_CURRENT',
  'changed':changed,'source_files_verified':len(expected),'only_configuration_written':changed,'robot_command_sent':False}))
finally: os.close(root_fd)
"""
    compile(code, 'usb-session-config-prepare-remote', 'exec')
    run = subprocess.run([*ssh_base(cfg), remote_python(cfg, code)],
        input=source_transfer_header(_transfer_cfg(cfg), expected), capture_output=True, timeout=30)
    if run.returncode:
        raise RuntimeError('Session USB config preparation refused: ' +
                           run.stderr.decode('utf-8', 'replace')[-1800:])
    receipt = json.loads(run.stdout.decode('utf-8'))
    if receipt.get('status') not in ('SESSION_CONFIG_CURRENT', 'SESSION_STORAGE_IDENTITY_REFRESHED'):
        raise RuntimeError('Invalid session USB preparation receipt')
    return receipt


def main(cfg, root):
    arguments = sys.argv[1:]
    if not arguments or arguments in (["-h"], ["--help"]):
        from .cli import parser
        parser(cfg).print_help()
        print("PC additional command: sync (copy only new code to verified USB)")
        return 0
    if arguments == ["sync"]:
        return sync(cfg, root)
    if arguments == ["stop"]:
        # Independent, self-contained RAM latch. An edited local file must not
        # prevent stopping the already running version on the robot.
        code = """
import datetime,json,os,socket,subprocess
from pathlib import Path
cfg=CONFIG
if socket.gethostname()!=cfg['hostname']: raise RuntimeError('Wrong host')
runtime=Path('/run/user')/str(os.getuid())/'tiger-agilex-unified'
runtime.mkdir(mode=0o700,parents=True,exist_ok=True)
stamp=datetime.datetime.now(datetime.timezone.utc).isoformat()+'\\n'
(runtime/'STOP').write_text(stamp,encoding='utf-8')
saved,error=False,None
try:
 usb=Path(cfg['usb'])
 if not os.path.ismount(usb): raise RuntimeError('USB not mounted')
 if subprocess.check_output(['findmnt','-n','-o','UUID','--target',str(usb)],text=True).strip()!=cfg['usb_uuid']: raise RuntimeError('USB UUID differs')
 root=Path(cfg['remote_bundle'])
 if usb not in root.parents or not root.is_dir(): raise RuntimeError('Expected USB source folder missing')
 (root/'STOP').write_text(stamp,encoding='utf-8');saved=True
except Exception as exc: error=repr(exc)
print(json.dumps({'status':'SOFTWARE_STOP_LATCHED','runtime_stop_written':True,'usb_stop_written':saved,'usb_error':error,'motors_disabled':False,'last_accepted_target_may_finish':True}))
""".replace("CONFIG", repr(cfg))
        return subprocess.run([*ssh_base(cfg), remote_python(cfg, code)]).returncode
    check_sources(cfg, root)
    remote = shlex.join(["env", "-u", "PYTHONPATH", "-u", "LD_PRELOAD",
        "LD_LIBRARY_PATH=" + cfg["hardware_library_path"], "PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1",
        cfg["hardware_python"], "-I", "-B", cfg["remote_bundle"] + "/" + ENTRY, *arguments])
    if "--json" not in arguments:
        print(f"[SSH] {cfg['ssh_alias']}：{' '.join(arguments)}", flush=True)
    # Ctrl+C disconnect does not prove robot stop. The remote command receives a
    # terminal signal only when delivered; stop is an independent explicit command.
    return subprocess.run([*ssh_base(cfg), remote]).returncode
