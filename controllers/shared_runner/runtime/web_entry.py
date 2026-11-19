"""Fixed web-to-robot dispatch. Read actions cannot create a hardware writer."""
from pathlib import Path
from contextlib import ExitStack
import argparse,hashlib,importlib,json,math,os,select,socket,subprocess,sys,threading,time,urllib.request
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from runtime import legacy_support as support

def emit(value):print(json.dumps(value,ensure_ascii=False,allow_nan=False),flush=True)
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def context():
    reviewed=json.loads((ROOT/'bindings/reviewed-production.json').read_text())
    canonical=Path(reviewed['canonical_root']);snapshot=ROOT/'receipts/live-source-snapshot.json'
    cfg,source=support.verify_sources(canonical,snapshot)
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    snap=json.loads(snapshot.read_text())
    support.require(socket.gethostname()==reviewed['expected_host']==snap['host'] and boot==snap['boot_id'],'robot host/boot changed')
    support.require(Path(sys.executable).absolute()==Path(reviewed['isolated_python']),'independent interpreter required')
    stage=json.loads((ROOT/'receipts/stage-receipt.json').read_text())
    done=json.loads((ROOT/'receipts/stage-complete.json').read_text())
    support.require(done.get('complete') is True and done['stage_receipt_sha256']==sha(ROOT/'receipts/stage-receipt.json')
        and done['source_snapshot_sha256']==sha(snapshot),'staged runtime proof changed')
    for rel,h in stage['member_sha256'].items():support.require(sha(ROOT/rel)==h,'isolated runtime source changed: '+rel)
    sys.path.insert(0,str(ROOT/'canonical'))
    storage=importlib.import_module('agilex_control.storage')
    support.require(Path(storage.__file__).resolve()==ROOT/'canonical/agilex_control/storage.py','foreign storage module')
    storage.verify_usb(cfg)
    return cfg,source,boot,storage,canonical,snapshot

def pointer_path(step,task):
    return ROOT/'receipts'/('admission-%d-%s.json'%(step,hashlib.sha256(task.encode()).hexdigest()[:16]))

def admissions(cfg,source,boot):
    from runtime.run_apply import validate_gate
    result=[]
    for path in sorted((ROOT/'receipts').glob('admission-*.json'))[-64:]:
        try:
            p=json.loads(path.read_text());admit=Path(p['admission'])
            support.require(admit.is_relative_to(ROOT/'runs') and sha(admit)==p['admission_sha256'],'admission path/SHA differs')
            gate=json.loads(admit.read_text());validate_gate(gate,source,boot)
            support.require(gate['binding']['task']==p['task'] and gate['binding']['step']==p['step']
                and gate['model_fingerprint']==p['model_fingerprint'],'admission task/step/model differs')
            result.append({**p,'validated':True})
        except Exception:
            continue  # Obsolete evidence remains on disk; never promotes a stale receipt.
    return result

def get_health(port):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/health'%port,timeout=1.) as r:
            raw=r.read(2*1024*1024+1)
        support.require(len(raw)<=2*1024*1024,'health too large')
        return json.loads(raw)
    except (OSError,ValueError):return None

def guard(cfg,storage):
    from agilex_control.hub import rpc
    stop=threading.Event()
    threading.Thread(target=support.watch_stdin_eof,args=(sys.stdin.buffer,stop),daemon=True).start()
    with ExitStack() as held:
        for name in support.OWNER_NAMES:held.enter_context(storage.Ownership(name))
        def check():
            storage.verify_usb(cfg)
            state=rpc(cfg,'/status',timeout=1.)
            record=state.get('recording',{})
            support.require(record.get('active') is False and record.get('saving') is False,'recording/saving owner exists')
        check();emit({'status':'ROBOT_MODEL_SWITCH_LOCKS_HELD','identity':support.process_identity()['identity']})
        while not stop.wait(1.):check()
    return 0

def fp32(action,args,cfg,storage,canonical):
    # The child uses the untouched original interpreter/CLI. EOF only latches
    # STOP for this active child; it never signals an untracked PID or clears STOP.
    command=[cfg['hardware_python'],'-I','-B',str(ROOT/'runtime/fp32_entry.py'),
        'deploy-h50' if action=='fp32-deploy' else 'h50-acceptance','--step',str(args.step)]
    if action=='fp32-deploy':command+=['--task',args.task,'--duration',str(args.duration),'--apply','--leader-isolated','--path-reviewed']
    child=subprocess.Popen(command,stdin=subprocess.DEVNULL)
    eof=threading.Event();closing=threading.Event()
    threading.Thread(target=support.watch_stdin_eof,args=(sys.stdin.buffer,eof),daemon=True).start()
    stopped=False
    while child.poll() is None:
        if eof.wait(.1) and not stopped:
            storage.stop(cfg);stopped=True
    closing.set();eof.set()
    return child.returncode

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('inspect','ports','endpoints','guard','acceptance','deploy','fp32-acceptance','fp32-deploy'))
    p.add_argument('--step',type=int,choices=(2000,5000,10000,15000,20000))
    p.add_argument('--task');p.add_argument('--duration',type=float,default=30.)
    for name in ('leader-isolated','onsite-confirmed','current-start-confirmed'):p.add_argument('--'+name,action='store_true')
    args=p.parse_args(argv)
    cfg,source,boot,storage,canonical,snapshot=context()
    if args.action=='inspect':
        emit({'ready':True,'root':str(ROOT),'boot_id':boot,'usb_uuid':cfg['usb_uuid'],
            'admissions':admissions(cfg,source,boot),'max_duration_s':1800,'robot_motion_started':False});return 0
    if args.action=='ports':
        ports=[]
        for port in (18883,18961):
            with socket.socket() as s:
                s.settimeout(.2)
                if s.connect_ex(('127.0.0.1',port))==0:ports.append(port)
        emit({'listening_ports':ports});return 0
    if args.action=='endpoints':emit({'FP32':get_health(18883),'BF16':get_health(18961)});return 0
    if args.action=='guard':return guard(cfg,storage)
    from agilex_control.h50_prompt import validate_prompt
    support.require(args.step is not None,'explicit step required');validate_prompt(args.task)
    from runtime.run_apply import window_schedule
    window_schedule(args.duration)
    deploy=args.action in ('deploy','fp32-deploy')
    if deploy:support.require(args.leader_isolated and args.onsite_confirmed and args.current_start_confirmed,'current onsite confirmations required')
    if args.action.startswith('fp32-'):return fp32(args.action,args,cfg,storage,canonical)
    common=['--execute-online','--canonical-root',str(canonical),'--snapshot',str(snapshot),
        '--step',str(args.step),'--task',args.task,'--stop-on-stdin-eof']
    if args.action=='acceptance':
        from runtime.run_nosend import main as nosend
        return nosend(['--mode','NO_SEND','--count','100','--warmup','3',*common])
    found=[a for a in admissions(cfg,source,boot) if a['step']==args.step and a['task']==args.task]
    support.require(len(found)==1,'matching actual NO_SEND admission required')
    a=found[0]
    from runtime.run_apply import main as apply
    return apply([*common,'--duration',str(args.duration),'--admission',a['admission'],
        '--admission-sha256',a['admission_sha256'],'--leader-isolated','--onsite-confirmed','--current-start-confirmed'])

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:emit({'status':'BLOCKED','error':str(exc)});raise SystemExit(2)
