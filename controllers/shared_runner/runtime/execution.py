"""Single-owner diagnostic H50 execution; final commands terminate in RAM.

No SDK, socket, SSH, model loader or hardware writer is imported here. A real
resource provider supplies an already-loaded ModelRuntime and existing Hub.
"""
from copy import deepcopy
import threading,time
import numpy as np
from src.command_pipeline import LinearCurve,Plan,Span,SmoothingQueue,label_ns,NS
from runtime.frozen_legacy.live_bridge import LiveContextBridge
from runtime.legacy_support import require,digest

PERIOD_NS=20_000_000

def make_queue(anchor_deg_m,bounds_deg_m,origin_ns,trial_index):
    scale=np.asarray([np.pi/180]*6+[1.])
    anchor=np.asarray(anchor_deg_m)*scale
    bounds=np.asarray(bounds_deg_m)*scale[:,None]
    curve=LinearCurve((origin_ns,origin_ns+label_ns(50)),np.array([anchor,anchor]))
    return SmoothingQueue(Plan((Span(curve.times_ns[0],curve.times_ns[-1],curve),)),bounds,trial_index=trial_index)

def runtime_context(ctx):
    return {**ctx.wire(),'observation_monotonic_ns':ctx.observation_ns,
        'cut_monotonic_ns':ctx.cut_ns,'deadline_monotonic_ns':ctx.observation_ns+600_000_000,
        'old_plan_id':ctx.old_plan.plan_id,'context_id':ctx.nonce,'generation':ctx.generation,
        'inference_phase':'NATIVE_BOOTSTRAP' if ctx.bootstrap else 'RTC_CONTINUATION'}

def failure_event(r,index,status,error=None):
    accepted=r.events.enqueue({'kind':'RTC_QUEUE_DECISION','request_index':index,'status':status,
        'physical_dispatch_performed_by_prediction':False,'experimental_profile':r.profile_id,
        'error':error})
    require(accepted is not False,'decision evidence enqueue rejected')

class Predictions:
    """One model call in flight; no remote planner or state-reset fallback."""
    def __init__(self,resource,total,warmup,*,clock_ns=time.monotonic_ns,close_join_s=4.):
        self.r,self.total,self.warmup,self.clock_ns=resource,total,warmup,clock_ns
        self.pending=resource.core.LatestObservation();self.quit=threading.Event();self.complete=threading.Event()
        self.failure=None;self.inflight_deadline_ns=None;self.current_context=None
        require(0<close_join_s<=4.,'bounded normal thread join timeout required')
        self.close_join_s=close_join_s
        self.fetcher=threading.Thread(target=self._fetch,daemon=True,name='isolated-existing-hub-observation')
        self.worker=threading.Thread(target=self._infer,daemon=True,name='isolated-single-loaded-model-call')
    def start(self):self.fetcher.start();self.worker.start();return self
    def _fetch(self):
        origin=self.clock_ns();slot=0
        try:
            while not self.quit.is_set() and self.r.queue.phase in ('READY','ACTIVE'):
                due=self.r.core.replan_due_ns(origin,slot)
                if self.quit.wait(max(0.,(due-self.clock_ns())/NS)):break
                self.r.guard();self.pending.put(self.r.rpc(self.r.cfg,'/observation/fp32',timeout=.25))
                slot=self.r.core.next_replan_slot(origin,self.clock_ns(),slot)
        except BaseException as exc:
            self.failure=repr(exc);self.r.queue.abort('OBSERVATION_FAILED: '+repr(exc))
        finally:self.pending.close()
    def _infer(self):
        try:
            for index in range(self.total):
                if self.quit.is_set() or self.r.queue.phase not in ('READY','ACTIVE'):break
                snapshot=self.pending.take()
                if snapshot is None:break
                self.process(snapshot,index)
                if self.r.queue.phase not in ('READY','ACTIVE'):break
        except BaseException as exc:
            self.failure=repr(exc);self.r.queue.abort('INFERENCE_WORKER_FAILED: '+repr(exc))
        finally:self.complete.set();self.quit.set();self.pending.close()
    def process(self,snapshot,index):
        r=self.r
        row={'observation_index':index,'request_index':index,'warmup':index<self.warmup,
             'status':'STARTED','physical_dispatch_performed_by_prediction':False,
             'experimental_profile':r.profile_id}
        r.receipt['attempts'].append(row)
        try:
            r.guard();packet=snapshot['packet_ready_monotonic_ns'];now=self.clock_ns()
            r.core.validate_observation_age(snapshot,now/NS,now_ns=now)
            ctx=r.queue.request(packet,bootstrap=index==0)
            self.current_context=ctx;self.inflight_deadline_ns=packet+600_000_000
            live=r.context_bridge.prepare(runtime_context(ctx),snapshot,now_ns=now)
            row.update(rtc_context=live.runtime_context,packet_ready_monotonic_ns=packet,
                       internal_queue_context=runtime_context(ctx))
            prediction=r.runtime.infer_rtc(snapshot,r.task,r.journal,live.runtime_context)
            # Preserve ModelRuntime's returned dictionary exactly for its USB
            # result.json audit; never relabel stale/replayed output as live.
            row['prediction']=deepcopy(prediction)
            r.guard();require(not self.quit.is_set(),'prediction returned after cancellation')
            require(r.queue.phase in ('READY','ACTIVE'),'late result after queue stop/abort')
            require(self.clock_ns()<=self.inflight_deadline_ns,'model result arrived after 600ms deadline')
            raw=r.context_bridge.validate_prediction(live,prediction)
            began=self.clock_ns();candidate=r.queue.prepare_h50(ctx,raw)
            row['local_prepare_ms']=(self.clock_ns()-began)/1e6
            r.guard();ready=self.clock_ns()
            require(not self.quit.is_set(),'candidate completed after cancellation')
            # Read the final clock inside the same lock as publication. No
            # precomputed earlier timestamp is substituted after a long prepare.
            with r.queue._lock:
                ready=self.clock_ns();r.queue.commit(ctx,candidate,ready)
            row.update(status='NO_SEND_DIAGNOSTIC_PLAN_COMMITTED' if getattr(r,'no_send',True) else 'BF16_SMOOTHED_PLAN_COMMITTED',commit_ready_monotonic_ns=ready,
                packet_to_commit_ready_ms=(ready-packet)/1e6,deadline_met=ready<=packet+600_000_000,
                active_plan_id=candidate.plan.plan_id)
            initial=ctx.old_plan.state(ctx.cut_ns,'left')
            r.journal.write_json(r.directory/('experiment-smoothed-%06d.json'%index),{
                'schema':'isolated-smoothed-reference-v1','live_model_request_id':prediction['request_id'],
                'queue_context':runtime_context(ctx),'old_plan_id':ctx.old_plan.plan_id,
                'initial_q_v_a_rad_m':{key:initial[key].tolist() for key in ('q','v','a')},
                'raw_h50_actions':raw,'candidate_diagnostics':candidate.diagnostics,
                'reference_end_ns':candidate.plan.end_ns,'reference_scope':'DIAGNOSTIC_UNSENT_REFERENCE' if getattr(r,'no_send',True) else 'PHYSICAL_ACCEPTED_REFERENCE',
                'binding':r.binding})
        except BaseException as exc:
            # If persistence fails after publication, keep that fact visible;
            # abort the diagnostic chain and never label the window successful.
            committed=row.get('status') in ('NO_SEND_DIAGNOSTIC_PLAN_COMMITTED','BF16_SMOOTHED_PLAN_COMMITTED')
            row.update(status='FAILED_AFTER_DIAGNOSTIC_COMMIT' if committed else 'FAILED_NOT_COMMITTED',
                       error=repr(exc),deadline_met=row.get('deadline_met',False),
                       diagnostic_commit_performed_before_failure=committed)
            self.failure=repr(exc);r.queue.abort('PREDICTION_OR_PREPARE_FAILURE: '+repr(exc))
        finally:
            self.inflight_deadline_ns=None;self.current_context=None
            row['finished_monotonic_ns']=self.clock_ns()
            failure_event(r,index,row['status'],row.get('error'))
    def check_deadline(self,now_ns):
        with self.r.queue._lock:
            if (self.inflight_deadline_ns is not None and now_ns>self.inflight_deadline_ns and
                self.r.queue.phase in ('READY','ACTIVE') and self.r.queue.pending is self.current_context):
                self.failure='MODEL_OR_LOCAL_PREPARE_DEADLINE_EXCEEDED'
                self.r.queue.abort(self.failure)
    def close(self):
        self.quit.set();self.pending.close()
        for thread in (self.fetcher,self.worker):
            if thread.ident is not None:thread.join(self.close_join_s)
        return {'fetch_stopped':not self.fetcher.is_alive(),'inference_stopped':not self.worker.is_alive(),
                'failure':self.failure,'superseded_packet_ready_ns':list(self.pending.superseded)}

class Sender:
    """50Hz diagnostic consumer; outputs stop in a bounded RAM observer sink."""
    def __init__(self,resource,*,clock_ns=time.monotonic_ns):
        self.r,self.clock_ns=resource,clock_ns;self.quit=threading.Event();self.failed=threading.Event()
        self.failure=None;self.ticks=0;self.last_tick_ns=None;self.intervals_ns=[]
        duration=getattr(resource,"options",{}).get("duration_s",0)
        require(type(duration) in (int,float) and np.isfinite(duration) and 0<=duration<=1800,"invalid timing-buffer duration")
        self.timing_capacity=max(16384,int(np.ceil((duration+10)*50))+1)
        self.thread=threading.Thread(target=self._run,daemon=True,name='isolated-no-send-command-observer')
    def start(self):self.thread.start();return self
    def tick(self,now_ns,scheduled_ns):
        r=self.r;r.guard()
        require(type(now_ns) is int and now_ns>0,'actual monotonic sample time required')
        require(self.last_tick_ns is None or 0<=now_ns-self.last_tick_ns<=100_000_000,
                'observer clock reversed or gap exceeded 100ms')
        state=r.cache.control_snapshot()
        r.h['feedback_guard'](state,max_age_s=.2,require_enabled=False,require_can=False)
        r.h['gripper_guard'](state,max_age_s=.2,require_enabled=False,now_monotonic=now_ns/NS)
        r.h['validate_q'](state['q_deg'],r.bounds[:6])
        if self.last_tick_ns is not None:
            require(len(self.intervals_ns)<self.timing_capacity,'bounded observer timing buffer exhausted')
            self.intervals_ns.append(now_ns-self.last_tick_ns)
        with r.queue._lock:
            command=r.queue.sample_command(now_ns)
            sampled_phase,sampled_plan_id=r.queue.phase,r.queue.plan.plan_id
        self.last_tick_ns=now_ns;self.ticks+=1
        # Every tick reaches the final encoder, while only 10Hz compact rows
        # enter the existing asynchronous USB journal. No per-tick file write.
        if self.ticks==1 or self.ticks%5==0 or sampled_phase=='REFERENCE_STOPPED':
            scale=np.asarray([180/np.pi]*6+[1.]);diagnostic=(command.raw_reference_rad_m*scale).tolist()
            row={'kind':'NO_SEND_UNSENT_HOLD_AND_ACTUAL','monotonic_ns':now_ns,
                'reference_sample_monotonic_ns':now_ns,'scheduled_tick_monotonic_ns':scheduled_ns,
                'reference_q_deg_m':list(r.anchor),'reference_v_deg_m_s':[0.]*7,
                'actual_q_deg':deepcopy(state['q_deg']),'actual_gripper_m':state['grip_m'],
                'diagnostic_reference_q_deg_m':diagnostic,'diagnostic_reference_phase':sampled_phase,
                'diagnostic_reference_plan_id':sampled_plan_id,
                'output_conversion':r.core.prepare_output_command(list(r.anchor),r.bounds),
                'diagnostic_output_conversion':r.core.prepare_output_command(diagnostic,r.bounds),
                'isolated_sdk_joint_mdeg':list(command.joint_mdeg),'isolated_sdk_gripper_um':command.gripper_um,
                'physical_dispatch_performed':False,'scope':'NO_SEND_CURRENT_OBSERVATION_UNSENT_DIAGNOSTIC_REFERENCE'}
            require(r.events.enqueue(row) is not False,'NO_SEND event enqueue rejected')
        return command
    def _run(self):
        scheduled=self.clock_ns()
        try:
            while not self.quit.is_set():
                self.tick(self.clock_ns(),scheduled)
                finished=self.clock_ns();scheduled=scheduled+PERIOD_NS if finished<=scheduled+PERIOD_NS else finished+PERIOD_NS
                self.quit.wait(max(0.,(scheduled-self.clock_ns())/NS))
        except BaseException as exc:
            self.failure=repr(exc);self.failed.set();self.r.queue.abort('NO_SEND_OBSERVER_FAILED: '+repr(exc))
    def check_alive(self):require(not self.failed.is_set(),'NO_SEND observer failed: '+str(self.failure))
    def close(self):
        self.quit.set()
        if self.thread.ident is not None:self.thread.join(1)
        if self.intervals_ns:
            values=np.asarray(self.intervals_ns)/1e6
            self.r.receipt['diagnostic_observer_intervals_ms']={'count':len(values),
                'p50':float(np.percentile(values,50)),'p95':float(np.percentile(values,95)),'max':float(values.max()),
                'every_tick_encoded':True,'physical_send_performed':False,'event_sample_every_ticks':5}
        return not self.thread.is_alive()

def prepare_controlled_stop(resource,*,clock_ns=time.monotonic_ns,start_ns=None):
    """Prepare a future stop without rewriting elapsed reference or resetting q/v."""
    resource.guard();queue=resource.queue
    require(queue.phase in ('READY','ACTIVE'),'aborted/previously stopped queue cannot restart')
    began=clock_ns();start=began+PERIOD_NS if start_ns is None else start_ns
    require(type(start) is int and began<start<=began+1_000_000_000,'future covered stop within one second required')
    record={'start_ns':start,'preparation_started_ns':began,'preparation_lead_ns':start-began,
            'physical_stop_validated':False,'status':'PREPARING'}
    resource.receipt['normal_stop']=record
    try:
        candidate=queue.prepare_normal_stop(start,np.asarray([np.deg2rad(50)]*6+[.1]))
        resource.guard();ready=clock_ns();record.update(ready_ns=ready,prepare_ms=(ready-began)/1e6,
            margin_before_start_ms=(start-ready)/1e6,diagnostics=candidate.diagnostics)
        queue.commit_normal_stop(candidate,ready);record['status']='FUTURE_STOP_COMMITTED'
    except BaseException as exc:
        record.update(status='FAILED_NOT_COMMITTED',error=repr(exc));raise
    return candidate
