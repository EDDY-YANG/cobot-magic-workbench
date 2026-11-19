"""Independent BF16 + smoothing bounded 1..1800-second APPLY; default is inert inspection."""
from pathlib import Path
from copy import deepcopy
from collections import deque
import argparse,importlib,json,math,os,signal,socket,sys,threading,time,traceback
ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from runtime import legacy_support as support
from runtime.provider import resource_class,locked_existing_session
from runtime.execution import Predictions,prepare_controlled_stop
PROFILE_ID='isolated-bf16-fixed-clock-smoothing-apply-v1'

def validate_gate(gate,source,boot):
    support.require(gate.get('status')=='NO_SEND_PROFILE_VALIDATED' and gate.get('validated') is True and
        gate.get('synthetic_fixture') is False and gate.get('storage_validated') is True and gate.get('TX_delta_zero') is True,
        'complete real BF16 smoothed NO_SEND with saved evidence required')
    support.require(gate['binding']['snapshot_boot_id']==boot and gate['binding']['source']==source,
                    'NO_SEND host boot/production configuration/source changed')
    support.require(gate['total_calls']==103 and gate['measured_calls']==100 and
        gate['latency_ms']['p95']<=550 and gate['latency_ms']['max']<=600,'live NO_SEND timing/count criteria not satisfied')
    checked={}
    for rel,h in gate['binding']['experiment_source_sha256'].items():
        if rel.startswith(('src/','frozen_governor/','frozen_rtc/','bf16_service_contract/')) or rel in (
                'runtime/bf16_model.py','runtime/bf16_bridge.py','runtime/bf16_transport.py'):
            support.require(support.file_sha(ROOT/rel)==h,'accepted planning/model protocol changed: '+rel);checked[rel]=h
    support.require(len(checked)>=18,'incomplete algorithm/RTC binding')
    return checked

def stationary_position_result(history,target):
    """Position settling is independent of gripper target attainment/contact success."""
    if len(history)<2 or history[-1][0]-history[0][0]<.3:return None
    q=history[-1][1]
    if len(target)!=7 or any(len(row)!=7 or not all(math.isfinite(x) for x in row) for _,row in history):
        raise RuntimeError('invalid measured settling positions')
    if not all(math.isfinite(x) for x in target):raise RuntimeError('invalid settling command target')
    stable=all(max(row[j] for _,row in history)-min(row[j] for _,row in history)<=([.5]*6+[.002])[j] for j in range(7))
    joints_close=all(abs(q[j]-target[j])<=5. for j in range(6))
    if not (stable and joints_close):return None
    return {'actual_q_deg_m':list(q),'command_target_deg_m':list(target),
        'position_stable_window_s':history[-1][0]-history[0][0],
        'actual_velocity_sensor_verified':False,'joint_target_reached':True,
        'gripper_target_reached':abs(q[6]-target[6])<=.002,
        'gripper_target_error_m':q[6]-target[6],
        'gripper_stop_basis':'FRESH_POSITION_STABLE_NOT_TARGET_ATTAINMENT',
        'grasp_or_contact_success':'OPERATOR_REVIEW_REQUIRED'}


def settle_actual(r,timeout=5.):
    end=time.monotonic()+timeout;history=deque()
    while time.monotonic()<end:
        r.guard();r.sender.check_alive();now=time.monotonic()
        if r.queue.phase=='REFERENCE_STOPPED':
            s=r.cache.control_snapshot()
            r.h['feedback_guard'](s,max_age_s=.2,require_enabled=True,require_can=True)
            r.h['gripper_guard'](s,max_age_s=.2,require_enabled=True,now_monotonic=now)
            q=[*s['q_deg'],s['grip_m']];history.append((now,q))
            while history and now-history[0][0]>.35:history.popleft()
            target=r.queue.sample_command(time.monotonic_ns()).target_rad_m
            target=[math.degrees(float(x)) for x in target[:6]]+[float(target[6])]
            result=stationary_position_result(history,target)
            if result is not None:return result
        time.sleep(.02)
    raise RuntimeError('reference brake / actual-position settle timed out')


def window_schedule(duration):
    support.require(type(duration) in (int,float) and math.isfinite(duration) and 1<=duration<=1800,
                    'duration must be finite and within 1..1800 seconds')
    duration_ns=round(duration*1e9)
    lead=min(500_000_000,max(100_000_000,duration_ns-700_000_000))
    # Preserve R14/18. Leave the real 600ms inference budget plus 100ms start margin.
    calls=max(1,(duration_ns-lead-700_000_000)*18//14_000_000_000+1)
    return duration_ns,lead,int(calls)

def run_window(r,duration):
    duration_ns,lead,calls=window_schedule(duration)
    began=time.monotonic_ns();end=began+duration_ns
    r.receipt.update(policy_window_started_ns=began,requested_duration_s=duration,scheduled_model_calls=calls)
    r.predictions=Predictions(r,calls,0).start()
    while time.monotonic_ns()<end-lead:
        r.guard();r.sender.check_alive();r.predictions.check_deadline(time.monotonic_ns())
        support.require(r.queue.phase in ('READY','ACTIVE'),'reference queue aborted or stopped early')
        support.require(r.predictions.failure is None,'prediction failed: '+str(r.predictions.failure))
        time.sleep(.005)
    support.require(r.predictions.complete.is_set(),'last inference did not drain before scheduled stop preparation')
    support.require(len(r.receipt['attempts'])==calls and all(x['status']=='BF16_SMOOTHED_PLAN_COMMITTED' and
        x['deadline_met'] is True for x in r.receipt['attempts']),'not all scheduled model plans were committed')
    # Prepare before the selected end, using its accepted q/v; keep the prefix unchanged.
    # No extension of the model window and no late-reference backdating.
    prepare_controlled_stop(r,start_ns=end)
    while time.monotonic_ns()<end:
        r.guard();r.sender.check_alive();time.sleep(.005)
    r.receipt['policy_window_ended_ns']=time.monotonic_ns()
    r.receipt['actual_stop']=settle_actual(r)
    r.receipt['normal_stop']['physical_stop_validated']=True
    r.receipt['normal_stop']['physical_stop_evidence']='fresh position settled for >=300ms; not measured motor velocity/acceleration'
    r.receipt['status']='BF16_SMOOTHED_WINDOW_COMPLETED_REQUIRES_ARTIFACT_AUDIT'

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute-online',action='store_true');p.add_argument('--canonical-root',type=Path)
    p.add_argument('--snapshot',type=Path);p.add_argument('--config-file',type=Path)
    p.add_argument('--admission',type=Path);p.add_argument('--admission-sha256')
    p.add_argument('--step',type=int,default=5000);p.add_argument('--task')
    p.add_argument('--duration',type=float,default=30.)
    for flag in ('leader-isolated','onsite-confirmed','current-start-confirmed','stop-on-stdin-eof'):p.add_argument('--'+flag,action='store_true')
    return p

def main(argv=None):
    args=parser().parse_args(argv)
    window_schedule(args.duration)
    if not args.execute_online:
        print(json.dumps({'status':'OFFLINE_INSPECTION_ONLY','profile_id':PROFILE_ID,'duration_s':args.duration,'max_duration_s':1800,
            'execution_started':False,'no_auto_enable_or_return_to_zero':True}));return 0
    support.require(all((args.canonical_root,args.snapshot,args.admission,args.admission_sha256,
        args.leader_isolated,args.onsite_confirmed,args.current_start_confirmed)),'explicit snapshot, live admission and current-start authorization required')
    snapshot=json.loads(args.snapshot.read_text());cfg,source=support.verify_sources(args.canonical_root,snapshot,args.config_file)
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    support.require(socket.gethostname()==cfg['hostname']==source['snapshot_host'] and boot==snapshot['boot_id'],'host/boot identity differs')
    reviewed=json.loads((ROOT/'bindings/reviewed-production.json').read_text())
    support.require(Path(sys.executable).absolute()==Path(reviewed['isolated_python']) and
        Path(sys.prefix)==Path(reviewed['isolated_python']).parents[1],'exact independent interpreter required')
    for rel,h in source['source_sha256'].items():support.require(support.file_sha(ROOT/'canonical'/rel)==h,'isolated canonical source differs')
    canonical=support.import_canonical(ROOT/'canonical')
    storage=importlib.import_module('agilex_control.storage')
    contract=importlib.import_module('agilex_control.h50_approved_contract')
    from runtime.bf16_model import BF16Runtime,current_loaded_model,health_descriptor,bf16_fingerprint
    from runtime.bf16_bridge import BF16ContextBridge
    from runtime import bf16_trials as trials
    storage.verify_usb(cfg)
    support.require(args.admission.resolve().is_relative_to(Path(cfg['usb']).resolve()),'admission must be on the selected USB')
    support.require(support.file_sha(args.admission)==args.admission_sha256,'reviewed admission SHA differs')
    gate=json.loads(args.admission.read_text());algorithm=validate_gate(gate,source,boot)
    support.require(gate['binding']['task']==args.task and gate['binding']['step']==args.step,'NO_SEND prompt/step differs')
    cfg['hardware_python']=reviewed['isolated_python']
    stop_event=threading.Event();cancellations=[]
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop_event.set())
    if args.stop_on_stdin_eof:
        threading.Thread(target=support.watch_stdin_eof,args=(sys.stdin.buffer,stop_event,cancellations.append),daemon=True).start()
    print(json.dumps({**support.process_identity(),'mode':'APPLY','duration_s':args.duration}),flush=True)
    with locked_existing_session(storage,cfg,expected_step=args.step,discover=current_loaded_model) as (cfg,step,status):
        from runtime.bf16_transport import BoundedHTTP
        support.require(bf16_fingerprint(BoundedHTTP(18961).request('GET','/health'),step)==gate['model_fingerprint'],
                        'BF16 model changed since NO_SEND acceptance')
        binding={'profile_id':PROFILE_ID,'mode':'APPLY','apply_gate_verified':True,'source':source,
            'admission':str(args.admission),'admission_sha256':args.admission_sha256,'validated_algorithm_sha256':algorithm,
            'scope':'manual duration experiment; original FP32 acceptance and parameters untouched'}
        session_cfg=deepcopy(cfg);session_cfg['remote_bundle']=str(ROOT)
        session=storage.Session(session_cfg,'isolated-bf16-smoothed-apply')
        allocation=trials.allocate_trial(cfg,step,session.path.name,'deploy-h50')
        session.report.update(experimental_profile=binding,trial_allocation=allocation)
        cls=resource_class(canonical,contract.APPROVED_CONTRACT_SHA256,runtime_factory=BF16Runtime,
            health_descriptor=health_descriptor,bridge_type=BF16ContextBridge,apply=True)
        options={'profile_id':PROFILE_ID,'task':args.task,'duration_s':args.duration,'leader_isolated':args.leader_isolated,
            'onsite_confirmed':args.onsite_confirmed,'current_start_confirmed':args.current_start_confirmed}
        r=cls(cfg,session,stop_event,step,allocation['trial_index'],options,binding);primary=None;artifacts=None
        r.receipt['cancellation_events']=cancellations
        try:
            with session.defer_persistence():r.start();run_window(r,args.duration)
        except BaseException as exc:
            primary=exc;r.receipt.update(status='FAILED_NO_AUTOMATIC_RECOVERY',error=repr(exc),traceback=traceback.format_exc())
        finally:
            try:r.close(primary)
            except BaseException as exc:
                if primary is None:primary=exc
                r.receipt['status']='FAILED_NO_AUTOMATIC_RECOVERY'
        try:
            support.require(r.owned_path is not None,'no owned recording to audit')
            from runtime.long_run_audit import finalize_run_artifacts
            artifacts=finalize_run_artifacts(cfg,session,step,r.receipt)
            support.close_publisher_after_audit(r)
            support.require(artifacts.get('compact',{}).get('validated') is True,'saved physical run artifact audit failed')
        except BaseException as exc:
            session.report['experiment_artifact_error']=repr(exc)
            if primary is None:primary=exc
        finally:
            if r.publisher is not None and not r.receipt.get('task_publisher_closed'):
                try:support.close_publisher_after_audit(r)
                except BaseException as exc:
                    if primary is None:primary=exc
        good=primary is None and not r.receipt['cleanup_errors'] and r.receipt['status']=='BF16_SMOOTHED_WINDOW_COMPLETED_REQUIRES_ARTIFACT_AUDIT'
        status='BF16_SMOOTHED_WINDOW_COMPLETED_AND_SAVED' if good else 'FAILED_NO_AUTOMATIC_RECOVERY'
        session.finish(status);stop_event.set()
        print(json.dumps({'status':status,'report':str(session.path/'operation.json'),'episode':r.owned_path,
            'error':repr(primary) if primary else None,'packets_sent':r.receipt.get('packets_sent'),
            'original_FP32_configuration_modified':False}),flush=True)
        return 0 if good else 2
if __name__=='__main__':raise SystemExit(main())
