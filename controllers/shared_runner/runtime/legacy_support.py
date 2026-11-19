"""Reviewed pure helpers extracted from prior NO_SEND runner; no I/O on import."""
from contextlib import ExitStack,contextmanager
from copy import deepcopy
import hashlib,importlib,json,os,socket,sys,threading,time
from pathlib import Path
OWNER_NAMES=("h50-deployment","fp32-shadow-run","writer-can_right")
def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf8')).hexdigest()


def require(ok,message):
    if not ok:raise RuntimeError(message)


def file_sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_sources(root,snapshot,config_path=None):
    """File reads only; no package import, flock, SDK, lease or service call."""
    root=Path(root).resolve(strict=True)
    binding=json.loads(Path(snapshot).read_text(encoding="utf-8")) if not isinstance(snapshot,dict) else deepcopy(snapshot)
    changes=[]
    expected=binding["source_sha256"]
    require(isinstance(expected,dict) and expected,"captured canonical source manifest required")
    for name,sha in expected.items():
        path=root/name
        path.resolve(strict=True).relative_to(root)
        require(not path.is_symlink(),"canonical source symlink refused")
        if file_sha(path)!=sha:changes.append(name)
    require(not changes,"canonical source changed: "+repr(changes))
    current={p.relative_to(root).as_posix() for p in (root/"agilex_control").rglob("*.py")}
    require(current==set(expected),"canonical Python file set differs from live snapshot")
    config=Path(config_path) if config_path else root/"2026-10-06-14-05-config.json"
    require(file_sha(config)==binding["config_sha256"],"actual production configuration differs from snapshot")
    cfg=json.loads(config.read_text(encoding="utf-8"))
    require(Path(cfg["remote_bundle"]).resolve()==root,"do not rebase production Hub storage binding")
    return cfg,{"canonical_root":str(root),"config_sha256":binding["config_sha256"],
        "source_sha256":expected,"canonical_source_set_sha256":digest(expected),"snapshot_host":binding["host"]}


def import_canonical(root):
    sys.dont_write_bytecode=True
    root=Path(root).resolve();sys.path.insert(0,str(root))
    module=importlib.import_module("agilex_control.h50_deployment")
    require(Path(module.__file__).resolve()==root/"agilex_control/h50_deployment.py","foreign canonical package already imported")
    return module


def current_loaded_model(cfg):
    """GET only, called AFTER all three canonical operation locks."""
    remote=importlib.import_module("agilex_control.remote_fp32")
    api=remote.FP32ShadowClient("http://127.0.0.1:18883",timeout_s=.8)
    status=api._request("GET","/status")
    require(status.get("status")=="READY" and status.get("busy") is False and status.get("lease",{}).get("active") is False,
            "current model is unavailable or belongs to another active session")
    step=status.get("selected_step")
    require(type(step) is int and step in (2000,5000,10000,15000,20000),"explicit currently loaded checkpoint missing")
    return step,status


@contextmanager
def owned_model_scope(storage,cfg,*,discover=current_loaded_model,expected_step=None):
    """One implementation of locks-before-model-read/lease, also fixture tested."""
    with ExitStack() as locks:
        for name in OWNER_NAMES:locks.enter_context(storage.Ownership(name))
        storage.verify_usb(cfg)
        step,status=discover(cfg)
        require(expected_step is None or expected_step==step,'requested model differs; no switch is allowed')
        yield step,status


def watch_stdin_eof(stream,stop_event,on_event=None):
    # Do not hold Python's BufferedReader lock in a daemon during shutdown.
    # Real Linux stdin uses a raw fd; CPU fixtures may provide an in-memory stream.
    try:fd=stream.fileno()
    except (AttributeError,OSError):fd=None
    if fd is None:
        while stream.read(1):pass
    else:
        import select
        while not stop_event.is_set():
            ready,_,_=select.select([fd],[],[],.2)
            if ready and not os.read(fd,1):break
        if stop_event.is_set():return
    if on_event:on_event({'kind':'STDIN_OWNER_EOF','monotonic_ns':time.monotonic_ns()})
    stop_event.set()


def loaded_runtime_factory(cfg,step):
    module=importlib.import_module("agilex_control.h50_runtime")
    class LoadedOnlyRuntime(module.ModelRuntime):
        def ensure_ready(self,*args,**kwargs):
            raise RuntimeError("NO_SEND loaded-only provider forbids ensure_ready/select")
        def bind_loaded(self):
            status=self._call("GET","/status")
            require(status.get("status")=="READY" and status.get("selected_step")==self.step and status.get("busy") is False
                    and status.get("lease",{}).get("active") is False,"loaded model identity/owner changed")
            health=self._call("GET","/health")
            self._fingerprint=module.validate_health(health,self.step)
            self._acceptance_binding=deepcopy(health["acceptance_binding"])
            return health
    return LoadedOnlyRuntime(cfg,step,mode="deployment")


def stable_anchor(resource,*,clock=time.monotonic,wait=time.sleep,timeout=5.):
    """Same .3s/.5deg/2mm position window; not measured motor acceleration."""
    began=clock();history=[]
    while True:
        resource.guard();now=clock();require(now-began<=timeout,"actual startup position did not stabilize")
        state=resource.cache.control_snapshot()
        resource.h['feedback_guard'](state,max_age_s=.2,require_enabled=False,require_can=False)
        resource.h['gripper_guard'](state,max_age_s=.2,require_enabled=False,now_monotonic=now)
        values=[*state['q_deg'],state['grip_m']];history.append((now,values))
        history=[row for row in history if now-row[0]<=.35]
        if len(history)>1 and now-history[0][0]>=.3 and all(
                max(q[j] for _,q in history)-min(q[j] for _,q in history)<=([.5]*6+[.002])[j] for j in range(7)):
            return values,{"window_s":now-history[0][0],"samples":len(history),
                "position_threshold_deg_m":[.5]*6+[.002],"reference_initial_v_a":"zero initial HOLD, not measured motor v/a"}
        wait(.02)


def math_finite(value):
    import math
    return type(value) in (int,float) and math.isfinite(value)


def bounded_counts(setting,count=None,warmup=None):
    measured=setting['acceptance_measured_calls'] if count is None else count
    warmed=setting['acceptance_warmup_calls'] if warmup is None else warmup
    require(type(warmed) is int and 0<=warmed<=setting['acceptance_warmup_calls'] and
            type(measured) is int and 1<=measured<=setting['acceptance_measured_calls'],'bounded NO_SEND count/warmup required')
    full=measured==setting['acceptance_measured_calls'] and warmed==setting['acceptance_warmup_calls']
    return measured,warmed,'h50-acceptance' if full else 'h50-shadow'


def close_publisher_after_audit(resource):
    """No PASS receipt may precede the last observational worker's closure."""
    if resource.publisher is None:return None
    try:
        final=resource.publisher.close()
        resource.session.report['experiment_publisher_close']=final
        require(isinstance(final,dict) and final.get('worker_stopped') is True and final.get('pending')==0 and not final.get('errors'),
                'task metadata publisher did not finish healthy and empty')
        resource.receipt['task_publisher_closed']=True
        return final
    except BaseException as exc:
        resource.receipt['cleanup_errors'].append('task publisher close: '+repr(exc))
        resource.receipt['status']='FAILED_NO_AUTOMATIC_RECOVERY'
        resource.session.report['experiment_publisher_close_error']=repr(exc)
        raise


def completed_window_status(attempts,expected_calls,worker_failure=None):
    """A controlled stop after a failed packet is not a completed count window."""
    complete=(len(attempts)==expected_calls and expected_calls>0 and not worker_failure
        and all(row.get('status')=='NO_SEND_DIAGNOSTIC_PLAN_COMMITTED'
                and row.get('deadline_met') is True for row in attempts))
    return ('NO_SEND_WINDOW_COMPLETED_REQUIRES_PROFILE_AUDIT' if complete
            else 'NO_SEND_TERMINATED_WITH_FAILURES')


def result_exit_code(admission,*,primary_error=False,full_window=False):
    """A failed short run is still failed; cleanup is not profile acceptance."""
    if primary_error or not isinstance(admission,dict):return 2
    status=admission.get('status')
    if status=='NO_SEND_PROFILE_FAILED' or admission.get('would_be_live_status')=='NO_SEND_PROFILE_FAILED':return 2
    if full_window and admission.get('validated') is not True:return 2
    if status=='NO_SEND_PROFILE_VALIDATED':return 0 if admission.get('validated') is True else 2
    return 0 if status=='TECHNICAL_CHECK_ONLY' else 2


def admission_summary(receipt,artifacts,setting,binding):
    measured=[row for row in receipt['attempts'] if not row['warmup']]
    durations=[row['packet_to_commit_ready_ms'] for row in measured if 'packet_to_commit_ready_ms' in row]
    def pct(values,f):
        if not values:return None
        values=sorted(values);p=(len(values)-1)*f;i=int(p)
        return values[i]+(values[min(i+1,len(values)-1)]-values[i])*(p-i)
    counts=len(receipt['attempts'])==setting['acceptance_warmup_calls']+setting['acceptance_measured_calls'] and len(measured)==setting['acceptance_measured_calls']
    decisions=all(row['status']=='NO_SEND_DIAGNOSTIC_PLAN_COMMITTED' and row.get('deadline_met') is True for row in receipt['attempts'])
    p95,peak=pct(durations,.95),max(durations) if durations else None
    enough=counts and len(durations)==len(measured)
    budget=enough and p95<=setting['acceptance_p95_ms'] and peak<=setting['acceptance_max_ms']
    stored=artifacts.get('compact',{}).get('validated') is True and artifacts.get('compact',{}).get('usb_finalflush_validated') is True
    no_tx=all(value==0 for value in receipt.get('interface_tx_delta',{}).values()) and bool(receipt.get('interface_tx_delta'))
    criteria=enough and decisions and budget and stored and no_tx and not receipt.get('cleanup_errors') and receipt.get('status')=='NO_SEND_WINDOW_COMPLETED_REQUIRES_PROFILE_AUDIT'
    synthetic=binding.get('evidence_kind')!='LIVE_EXISTING_PRODUCER_NO_SEND'
    passed=criteria and not synthetic
    failed_attempts=[row.get('request_index',row.get('observation_index')) for row in receipt['attempts']
        if row.get('status')!='NO_SEND_DIAGNOSTIC_PLAN_COMMITTED' or row.get('deadline_met') is not True]
    run_failed=(not receipt['attempts'] or bool(failed_attempts) or bool(receipt.get('cleanup_errors'))
        or receipt.get('status')!='NO_SEND_WINDOW_COMPLETED_REQUIRES_PROFILE_AUDIT'
        or not stored or not no_tx)
    live_status=('NO_SEND_PROFILE_VALIDATED' if passed else
        'NO_SEND_PROFILE_FAILED' if run_failed or enough else 'TECHNICAL_CHECK_ONLY')
    failure_times=[(row['finished_monotonic_ns']-row['packet_ready_monotonic_ns'])/1e6
        for row in receipt['attempts'] if row.get('status')!='NO_SEND_DIAGNOSTIC_PLAN_COMMITTED'
        and type(row.get('finished_monotonic_ns')) is int
        and type(row.get('packet_ready_monotonic_ns')) is int
        and row['finished_monotonic_ns']>=row['packet_ready_monotonic_ns']]
    return {'schema':'isolated-live-nosend-admission-v1','status':'SYNTHETIC_OFFLINE_TEST_ONLY' if synthetic else live_status,
        'validated':bool(passed),'mode':'NO_SEND','binding':binding,'model_fingerprint':receipt.get('model_fingerprint'),
        'would_be_live_status':live_status,'failed_request_indices':failed_attempts,
        'failed_attempt_observation_to_finish_ms':failure_times,
        'synthetic_fixture':synthetic,'numerical_and_storage_criteria_would_pass':bool(criteria),
        'bounds_deg_m':receipt.get('output_command_bounds_deg_m'),'measured_calls':len(measured),'total_calls':len(receipt['attempts']),
        'latency_ms':{'p95':p95,'max':peak},'original_criteria':{key:setting[key] for key in ('acceptance_measured_calls','acceptance_warmup_calls','acceptance_p95_ms','acceptance_max_ms')},
        'source_run_status':receipt.get('status'),'storage_validated':stored,'TX_delta_zero':no_tx,
        'manager_acceptance_modified':False,'old_controller_acceptance_reused':False,
        'APPLY_supported_or_authorized_by_this_runner':False,'physical_closed_loop_validated':False}


def identity_from_proc_fields(host,pid,boot_id,stat_text,cmdline):
    require(isinstance(host,str) and host and type(pid) is int and pid>0 and isinstance(boot_id,str) and boot_id,'valid process identity required')
    ticks=int(stat_text.rsplit(') ',1)[1].split()[19])
    require(ticks>=0 and isinstance(cmdline,bytes),'valid Linux stat/command bytes required')
    return {'host':host,'pid':pid,'boot_id':boot_id,'start_ticks':ticks,
            'cmdline_sha256':hashlib.sha256(cmdline).hexdigest(),'identity_kind':'LinuxProcV1'}


def process_identity():
    proc=Path('/proc/self');raw=(proc/'cmdline').read_bytes()
    identity=identity_from_proc_fields(socket.gethostname(),os.getpid(),Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                                      (proc/'stat').read_text(),raw)
    return {'event':'EXPERIMENT_RUNNER_STARTED',**identity,'identity':identity,'mode':'NO_SEND'}
