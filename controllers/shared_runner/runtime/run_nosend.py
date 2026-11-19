"""Independent loaded-FP32 + guided-RTC smoothing NO_SEND entry.

Default inspection constructs no resource, performs no connection, and cannot
start a model or robot. The explicit future online command runs on AgileX with
a fresh reviewed canonical source/config/boot snapshot and actual USB selection.
"""
from pathlib import Path
from copy import deepcopy
import argparse,importlib,json,os,signal,socket,sys,threading,time,traceback
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from runtime import legacy_support as support
from runtime.provider import resource_class,locked_existing_session
from runtime.execution import Predictions,prepare_controlled_stop

PROFILE_ID='isolated-bf16-fixed-clock-smoothing-nosend-v1'

def source_bindings():
    return {p.relative_to(ROOT).as_posix():support.file_sha(p)
            for folder in ('src','runtime','frozen_governor','frozen_rtc','bf16_service_contract')
            for p in sorted((ROOT/folder).rglob('*.py'))}

def run_window(resource,measured,warmup):
    r=resource;r.predictions=Predictions(r,warmup+measured,warmup).start()
    deadline=time.monotonic()+(warmup+measured+4)*14/18+10
    while not r.predictions.complete.is_set() and r.queue.phase in ('READY','ACTIVE'):
        r.guard();r.sender.check_alive();r.predictions.check_deadline(time.monotonic_ns())
        support.require(time.monotonic()<deadline,'NO_SEND observation window exceeded')
        time.sleep(.01)
    r.guard();r.sender.check_alive()
    if r.queue.phase in ('READY','ACTIVE'):prepare_controlled_stop(r)
    settle=time.monotonic()+5
    while r.queue.phase=='STOPPING_REFERENCE':
        r.guard();r.sender.check_alive();support.require(time.monotonic()<settle,'diagnostic reference stop timed out')
        time.sleep(.02)
    support.require(r.queue.phase=='REFERENCE_STOPPED','diagnostic queue aborted; no physical stop claim')
    r.receipt['status']=support.completed_window_status(r.receipt['attempts'],measured+warmup,r.predictions.failure)

def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=('inspect','NO_SEND','APPLY'),default='inspect')
    p.add_argument('--execute-online',action='store_true')
    p.add_argument('--canonical-root',type=Path);p.add_argument('--snapshot',type=Path)
    p.add_argument('--config-file',type=Path);p.add_argument('--step',type=int)
    p.add_argument('--count',type=int);p.add_argument('--warmup',type=int)
    p.add_argument('--task');p.add_argument('--stop-on-stdin-eof',action='store_true')
    args=p.parse_args(argv)
    if args.mode=='APPLY':p.error('APPLY is unavailable; this entry cannot construct a hardware writer')
    if args.execute_online and args.mode!='NO_SEND':p.error('--execute-online requires explicit --mode NO_SEND')
    if args.execute_online and not(args.canonical_root and args.snapshot):p.error('online requires exact canonical root and fresh reviewed snapshot')
    return args

def main(argv=None):
    args=parse_args(argv)
    if not args.execute_online:
        print(json.dumps({'status':'OFFLINE_INSPECTION_ONLY','profile_id':PROFILE_ID,
            'requested_mode':args.mode,'execution_started':False,'remote_connections':0,
            'model_calls':0,'hardware_calls':0,'APPLY_supported':False,
            'runtime_entry_implemented':True,'live_prerequisites_verified':False,
            'live_prerequisites':['fresh reviewed host/boot/source/config snapshot','selected verified writable robot USB',
                'canonical hardware Python with NumPy/SciPy','existing healthy Hub producers',
                'independent BF16 H50 model','three operation locks free'],
            'new_BF16_service_validation_included':True},ensure_ascii=False))
        return 0
    support.require(os.name=='posix','real NO_SEND runs on the existing Linux robot host')
    snapshot=json.loads(args.snapshot.read_text(encoding='utf8'))
    cfg,source=support.verify_sources(args.canonical_root,snapshot,args.config_file)
    support.require(socket.gethostname()==cfg['hostname']==source['snapshot_host'],'wrong real host')
    support.require(snapshot.get('boot_id')==Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                    'fresh current boot snapshot required; do not reuse historical runtime identity')
    reviewed=json.loads((ROOT/'bindings/reviewed-production.json').read_text(encoding='utf8'))
    support.require(Path(sys.executable).absolute()==Path(reviewed['isolated_python']), 'use exact independent robot environment')
    support.require(Path(sys.prefix)==Path(reviewed['isolated_python']).parents[1],'independent interpreter prefix differs')
    for rel,sha in source['source_sha256'].items():
        support.require(support.file_sha(ROOT/'canonical'/rel)==sha,'frozen isolated controller source differs: '+rel)
    cfg['hardware_python']=reviewed['isolated_python']
    canonical=support.import_canonical(ROOT/'canonical')
    storage=importlib.import_module('agilex_control.storage')
    contract=importlib.import_module('agilex_control.h50_approved_contract')
    measured,warmup,operation=support.bounded_counts(canonical._settings(cfg),args.count,args.warmup)
    binding={'source':source,'snapshot_sha256':support.file_sha(args.snapshot),'snapshot_boot_id':snapshot['boot_id'],
        'experiment_source_sha256':source_bindings(),'profile_id':PROFILE_ID,'mode':'NO_SEND',
        'evidence_kind':'LIVE_EXISTING_PRODUCER_NO_SEND','planner_transport':'LOCAL_CPU',
        'model_scope':'INDEPENDENT_BF16_SERVICE_KEEP_ORIGINAL_FLOAT32_ACTION_INTERFACE','APPLY_supported':False,
        'task':args.task,'step':args.step}
    stop_event=threading.Event();cancellations=[]
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop_event.set())
    if args.stop_on_stdin_eof:
        threading.Thread(target=support.watch_stdin_eof,args=(sys.stdin.buffer,stop_event,cancellations.append),
            daemon=True,name='isolated-operator-stdin-eof').start()
    print(json.dumps(support.process_identity()),flush=True)
    # All model GET/lease and recording operations occur within these three
    # canonical locks and after actual USB verification. No /select endpoint.
    from runtime.bf16_model import BF16Runtime,current_loaded_model,health_descriptor
    from runtime.bf16_bridge import BF16ContextBridge
    with locked_existing_session(storage,cfg,expected_step=args.step,discover=current_loaded_model) as (selected_cfg,step,status):
        cfg=selected_cfg
        binding['runtime_usb_selection']=deepcopy(cfg['usb_selection'])
        session_cfg=deepcopy(cfg);session_cfg['remote_bundle']=str(ROOT)
        session=storage.Session(session_cfg,'isolated-bf16-smoothed-nosend')
        from runtime import bf16_trials as trials
        allocation=trials.allocate_trial(cfg,step,session.path.name,operation)
        session.report.update(experimental_profile=binding,trial_allocation=allocation,loaded_model_status=status)
        cls=resource_class(canonical,contract.APPROVED_CONTRACT_SHA256,runtime_factory=BF16Runtime,
                           health_descriptor=health_descriptor,bridge_type=BF16ContextBridge)
        r=cls(cfg,session,stop_event,step,allocation['trial_index'],{'profile_id':PROFILE_ID,'task':args.task},binding)
        r.receipt['cancellation_events']=cancellations
        primary=None;admission=None;artifacts={}
        try:
            with session.defer_persistence():r.start();run_window(r,measured,warmup)
        except BaseException as exc:
            primary=exc;r.receipt.update(status='FAILED_NO_AUTOMATIC_RECOVERY',error=repr(exc),traceback=traceback.format_exc())
        finally:
            try:r.close(primary)
            except BaseException as exc:
                if primary is None:primary=exc
                r.receipt['status']='FAILED_NO_AUTOMATIC_RECOVERY'
        try:
            support.require(r.owned_path is not None,'no owned recording to audit')
            finalizer=importlib.import_module('agilex_control.h50_acceptance').finalize_run_artifacts
            artifacts=finalizer(cfg,session,step,r.receipt)  # Never finalize_acceptance or manager publication.
            support.close_publisher_after_audit(r)
            admission=support.admission_summary(r.receipt,artifacts,r.setting,binding)
            storage.write_json(session.path/'experiment-nosend-admission.json',admission,cfg)
            session.report.update(h50_artifact_finalization=artifacts.get('full'),experiment_no_send_admission=admission)
        except BaseException as exc:
            session.report['experiment_artifact_error']=repr(exc)
            if primary is None:primary=exc
        finally:
            if r.publisher is not None and not r.receipt.get('task_publisher_closed'):
                try:support.close_publisher_after_audit(r)
                except BaseException as exc:
                    if primary is None:primary=exc
            session.finish('FAILED' if primary or not admission or not admission['validated'] else 'NO_SEND_PROFILE_VALIDATED')
        if primary is None and admission and admission['validated']:
            from runtime.web_entry import pointer_path
            storage.write_json(pointer_path(step,r.task),{'step':step,'task':r.task,
                'admission':str(session.path/'experiment-nosend-admission.json'),
                'admission_sha256':support.file_sha(session.path/'experiment-nosend-admission.json'),
                'model_fingerprint':admission['model_fingerprint'],'validated':True},cfg)
        output={'status':admission['status'] if admission else 'FAILED','report':str(session.path/'operation.json'),
            'episode':r.owned_path,'admission':str(session.path/'experiment-nosend-admission.json') if admission else None,
            'error':repr(primary) if primary else None,'SDK_instances':0,'APPLY_supported':False,
            'physical_closed_loop_validated':False}
        print(json.dumps(output,ensure_ascii=False),flush=True)
        return support.result_exit_code(admission,primary_error=primary is not None,
            full_window=measured==r.setting['acceptance_measured_calls'] and warmup==r.setting['acceptance_warmup_calls'])

if __name__=='__main__':raise SystemExit(main())
