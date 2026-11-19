"""Async H50/RTC candidate. Import opens no device, socket or service.

Explicit apply requires the approved acceptance and current-start confirmations.
NO_SEND keeps a physical UNSENT HOLD and a separate diagnostic reference chain.
The diagnostic chain is never installed into the sender and creates no adapter.
"""
from __future__ import annotations
from collections import deque
from contextlib import ExitStack,contextmanager
from copy import deepcopy
import math
import hashlib
import os
from pathlib import Path
import sys
import threading
import time
import uuid
if __package__:
    from .h50_prompt import validate_prompt
    from . import h50_async as _reference_core
    from .h50_async import (AsyncQueue,LatestObservation,TrackingGuard,tail_plan,physical_actions,discarded_prefix_bound_violations,gripper_command_projection,joint_command_projection,
                           validate_observation_age,next_tick,require,finite,DEADLINE_NS,NS,
                           settings as core_settings,replan_due_ns,next_replan_slot,prepare_output_command,OUTPUT_COMMAND_POLICY)
else:
    from h50_prompt import validate_prompt
    import h50_async as _reference_core
    from h50_async import (AsyncQueue,LatestObservation,TrackingGuard,tail_plan,physical_actions,discarded_prefix_bound_violations,gripper_command_projection,joint_command_projection,
                          validate_observation_age,next_tick,require,finite,DEADLINE_NS,NS,
                          settings as core_settings,replan_due_ns,next_replan_slot,prepare_output_command,OUTPUT_COMMAND_POLICY)
STEPS=(2000,5000,10000,15000,20000)


def _hooks():
    from .hardware import PiperAdapter,DOCUMENTED_COMMAND_LIMITS_DEG,sdk_profile_parameters
    from .hardware import require_feedback,require_gripper_feedback,validate_target_deg
    from .storage import Ownership,verify_usb,require_not_stopped,stop_requested,write_json,open_usb_directory
    from .hub import rpc
    from .cli import _wait_hub_ready
    from .recording_lifecycle import preserving_rpc,stop_owned_recording,OWNED_RECORDING_STOP_PROTOCOL
    from .fp32_shadow_run import _recover_start_path,_episode_path,_tx_packets,_hub_no_send
    from .h50_journal import H50BufferedJournal
    from .tx_evidence import TxEvidenceWriter
    from .h50_runtime import ModelRuntime
    from .deployment import TaskEventPublisher
    return dict(adapter=PiperAdapter,documented=DOCUMENTED_COMMAND_LIMITS_DEG,sdk_profile=sdk_profile_parameters,
        feedback_guard=require_feedback,gripper_guard=require_gripper_feedback,validate_q=validate_target_deg,
        ownership=Ownership,verify_usb=verify_usb,require_not_stopped=require_not_stopped,stop_requested=stop_requested,
        write_json=write_json,open_dir=open_usb_directory,rpc=rpc,ready_hub=_wait_hub_ready,
        preserving_rpc=preserving_rpc,stop_owned=stop_owned_recording,stop_protocol=OWNED_RECORDING_STOP_PROTOCOL,
        recover_start=_recover_start_path,episode_path=_episode_path,tx_counters=_tx_packets,hub_no_send=_hub_no_send,
        journal=H50BufferedJournal,event_writer=TxEvidenceWriter,runtime=ModelRuntime,publisher=TaskEventPublisher,
        parent_life=ParentLife)


def _settings(cfg):
    return core_settings(cfg)


def _health(value,step):
    require(isinstance(value,dict) and value.get('ready') is True,'model not ready')
    policy=value.get('policy',{})
    require(type(policy.get('checkpoint_step')) is int and policy['checkpoint_step']==step and
        policy.get('precision')=='float32' and policy.get('model_compute_dtype')=='float32' and
        policy.get('horizon')==50 and policy.get('physical_action_dim')==7 and
        policy.get('units')==['rad']*6+['m'] and policy.get('action_basis')=='absolute' and
        policy.get('precision_audit_passed') is True,'wrong selected FP32/H50 policy')
    return deepcopy(policy)


class ParentLife:
    """Detect known parent death/reparent/PID reuse; not a PC network heartbeat."""
    def __init__(self):
        require(os.name=='posix','Linux parent process identity required')
        self.pid=os.getppid();self.ticks=self._read(self.pid)
    @staticmethod
    def _read(pid):
        tail=Path('/proc/%d/stat'%pid).read_text().rsplit(') ',1)[1].split()
        require(tail[0] not in ('Z','X'),'operator parent is dead/zombie')
        return tail[19]
    def check(self):
        require(os.getppid()==self.pid,'operator parent exited/reparented')
        require(self._read(self.pid)==self.ticks,'operator parent PID identity changed')
    def receipt(self):
        return {'pid':self.pid,'proc_start_ticks':self.ticks,'check':'parent alive + identity in existing cache loop',
            'limitation':'Does not prove PC connectivity while SSH/agent remains alive. EOF STOP is the primary channel-loss path.'}


class HubFeedbackCache:
    """Existing Hub HTTP -> RAM, never an additional CAN/camera reader."""
    def __init__(self,cfg,rpc,producer,binding,verify_usb,stop_requested,*,parent_check=None):
        self.cfg,self.rpc,self.producer,self.binding=cfg,rpc,deepcopy(producer),deepcopy(binding)
        self.verify_usb,self.stop_requested=verify_usb,stop_requested
        self.parent_check=parent_check
        self.lock,self.quit,self.failed_event=threading.Lock(),threading.Event(),threading.Event()
        self.failure=self.value=self.received=self.owned_path=self.binding_since=None
        self.thread=threading.Thread(target=self._run,name='h50-existing-hub-cache',daemon=True)
    def start(self):
        self.thread.start();deadline=time.monotonic()+2
        while self.value is None:
            self.check_alive(False);require(time.monotonic()<deadline,'no initial Hub cache feedback');self.quit.wait(.01)
        return self
    def _run(self):
        next_usb=0.
        while not self.quit.is_set():
            try:
                if self.parent_check is not None:self.parent_check()
                require(not self.stop_requested(self.cfg),'software STOP latch present')
                if time.monotonic()>=next_usb:self.verify_usb(self.cfg);next_usb=time.monotonic()+.25
                begin=time.monotonic();value=self.rpc(self.cfg,'/status',timeout=.1)
                require(time.monotonic()-begin<=.2,'Hub transport stale')
                require(value.get('producer')==self.producer,'Hub producer changed')
                require(value.get('storage_binding')==self.binding,'Hub selected USB changed')
                require(isinstance(value.get('arms',{}).get('right'),dict),'real right feedback missing')
                with self.lock:owned,since=self.owned_path,self.binding_since
                if owned is not None and begin<since:continue
                if owned is not None:
                    record=value.get('recording',{})
                    require(record.get('path')==owned and record.get('active') is True and
                        record.get('error') is None and type(record.get('overflows')) is int and
                        record['overflows']==0,'owned recording changed/stopped/failed')
                with self.lock:self.value,self.received=deepcopy(value),begin
            except Exception as exc:self.failure=repr(exc);self.failed_event.set();return
            self.quit.wait(.02)
    def bind_recording(self,path):
        with self.lock:self.owned_path,self.binding_since=path,time.monotonic()
    def check_alive(self,require_value=True):
        require(not self.failed_event.is_set(),'Hub cache failed: '+str(self.failure))
        if require_value:require(self.received is not None and time.monotonic()-self.received<=.2,'Hub cache stopped advancing')
    def control_snapshot(self):
        self.check_alive()
        with self.lock:value=deepcopy(self.value['arms']['right']);elapsed=time.monotonic()-self.received
        for key in ('age_joint_s','age_status_s','age_motor_s','age_gripper_s'):
            if finite(value.get(key)):value[key]+=elapsed
        for value0 in value.get('external_commands',{}).values():
            if finite(value0.get('last_age_s')):value0['last_age_s']+=elapsed
        return value
    def latest_frame(self,can_id):return self.control_snapshot().get('raw',{}).get(hex(can_id))
    def close(self):
        self.quit.set();self.thread.join(1)
        return {'stopped':not self.thread.is_alive(),'failure':self.failure}


class AsyncSender:
    """Only writer thread; no HTTP, model call or USB I/O in this loop."""
    def __init__(self,adapter,queue,cache,guard,hooks,events,*,no_send=False,diagnostic_queue=None):
        self.adapter,self.queue,self.cache,self.guard,self.h,self.events=adapter,queue,cache,guard,hooks,events
        require(diagnostic_queue is None or (no_send and adapter is None and diagnostic_queue is not queue and
                diagnostic_queue.diagnostic and not diagnostic_queue.no_send),'diagnostic reference cannot drive a hardware writer')
        self.diagnostic_queue=diagnostic_queue
        self.no_send=no_send;self.quit=threading.Event();self.failed_event=threading.Event()
        self.failure=None;self.ticks=0;self.last_sent=None;self.tracking=TrackingGuard()
        self.thread=threading.Thread(target=self._run,name='h50-no-send-observer' if no_send else 'h50-only-can-writer',daemon=True)
    def start(self):self.thread.start();return self
    def _run(self):
        scheduled=time.monotonic();last_tick=None
        try:
            while not self.quit.is_set():
                self.guard();now=time.monotonic()
                require(last_tick is None or now-last_tick<=.1,'sender gap exceeded 100 ms');last_tick=now
                state=self.cache.control_snapshot()
                self.h['feedback_guard'](state,max_age_s=.2,require_enabled=not self.no_send,require_can=not self.no_send)
                self.h['gripper_guard'](state,max_age_s=.2,require_enabled=not self.no_send,now_monotonic=now)
                q,v,phase,plan_id=self.queue.sample(now)
                output=prepare_output_command(q,self.queue.bounds)
                target=output['target_deg_m']
                if self.no_send:
                    require(q==self.queue.anchor and all(x==0 for x in v),'NO_SEND reference must remain UNSENT HOLD');error=None
                else:
                    self.h['validate_q'](state['q_deg'],self.adapter.cfg['command_limits_deg'])
                    error=self.tracking.check(state['q_deg'],self.last_sent,now);self.adapter.require_no_foreign_commands()
                    context={'plan_id':plan_id,'rtc_enabled':self.queue.last_inference_phase=='RTC_CONTINUATION',
                        'inference_phase':self.queue.last_inference_phase,'reference_scope':self.queue.reference_scope,
                        'scheduled_tick_monotonic_ns':round(scheduled*1e9),
                        'reference_sample_monotonic_ns':round(now*1e9),
                        'final_target_rad_m':[math.radians(x) for x in target[:6]]+[target[6]],
                        'joint_output_projection':deepcopy(output['joint_projection']),
                        'gripper_output':{k:output[k] for k in ('policy','gripper_before_m','gripper_after_m','gripper_lower_clamped','gripper_upper_clamped','gripper_delta_m')},'phase':phase}
                    with self.adapter.dispatch(context):
                        self.guard();self.adapter.send_joint_target_deg(target[:6]);self.guard()
                        self.adapter.send_gripper_m(target[6],require_enabled=True)
                    self.last_sent=list(target[:6])
                diagnostic={}
                if self.diagnostic_queue is not None:
                    if self.diagnostic_queue.phase=='ABORT':
                        diagnostic={'diagnostic_reference_phase':'ABORT','diagnostic_reference_plan_id':self.diagnostic_queue.plan.id}
                    else:
                        dq,dv,dp,di=self.diagnostic_queue.sample(now)
                        diagnostic={'diagnostic_reference_q_deg_m':dq,'diagnostic_reference_v_deg_m_s':dv,
                                    'diagnostic_reference_phase':dp,'diagnostic_reference_plan_id':di,
                                    'diagnostic_output_conversion':prepare_output_command(dq,self.diagnostic_queue.bounds)}
                    diagnostic.update(reference_scope='DIAGNOSTIC_UNSENT_REFERENCE',diagnostic_reference_dispatched=False)
                self.events.enqueue({'kind':'NO_SEND_UNSENT_HOLD_AND_ACTUAL' if self.no_send else 'ACCEPTED_REFERENCE_AND_ACTUAL',
                    'monotonic_ns':time.monotonic_ns(),'plan_id':plan_id,'phase':phase,'reference_q_deg_m':q,
                    'reference_v_deg_m_s':v,'actual_q_deg':state['q_deg'],'actual_gripper_m':state['grip_m'],
                    'tracking_error_to_last_host_submission_deg':error,'physical_dispatch_performed':not self.no_send,
                    'output_conversion':output,**diagnostic})
                self.ticks+=1;scheduled=next_tick(scheduled,time.monotonic());self.quit.wait(max(0.,scheduled-time.monotonic()))
        except BaseException as exc:self.failure=repr(exc);self.failed_event.set();self.queue.abort(self.failure)
    def check_alive(self):require(not self.failed_event.is_set(),'sender/observer failed: '+str(self.failure))
    def close(self):self.quit.set();self.thread.join(1);return not self.thread.is_alive()


class PredictionProcessor:
    def __init__(self,resource,queue,trial_index):
        self.r,self.queue,self.trial_index=resource,queue,trial_index
        self.next_request_index=0
    def process(self,snapshot,index,*,warmup=False,commit=True):
        r=self.r;attempt={'observation_index':index,'request_index':None,'warmup':warmup,'status':'STARTED','physical_dispatch_performed_by_prediction':False,
            'plan_preparation_scope':r.receipt['plan_preparation_scope'],
            'plan_preparation_source_sha256':deepcopy(r.receipt['plan_preparation_source_sha256']),
            'plan_preparation_status':'NOT_STARTED'}
        r.receipt['attempts'].append(attempt)
        try:
            r.guard();now_ns=time.monotonic_ns()
            obs=validate_observation_age(snapshot,now_ns/NS,now_ns=now_ns)
            request_index=self.next_request_index
            context,old=self.queue.context(obs,self.trial_index,request_index,
                                           observation_ns=snapshot['packet_ready_monotonic_ns'])
            attempt['rtc_context']=deepcopy(context);attempt['packet_ready_monotonic_ns']=snapshot['packet_ready_monotonic_ns']
            attempt['inference_phase']=context['inference_phase'];attempt['reference_scope']=context['reference_scope']
            attempt['request_index']=request_index;self.next_request_index+=1
            predicted=r.runtime.infer_rtc(snapshot,r.task,r.journal,context);attempt['prediction']=deepcopy(predicted)
            require(predicted.get('status')=='DEPLOYMENT_PREDICTION_BUFFERED','prediction was not accepted into bounded RAM journal')
            require(_health(predicted.get('health_before'),r.step)==r.receipt['model'] and
                    _health(predicted.get('health_after'),r.step)==r.receipt['model'],'model/checkpoint/norm identity changed')
            response=predicted.get('response',{})
            require(response.get('action_dtype')=='float32' and response.get('action_shape')==[50,7],'wrong FP32 H50 response')
            raw=response.get('actions');converted=physical_actions(raw,self.queue.bounds)
            projection=gripper_command_projection(raw,self.queue.bounds)
            attempt['gripper_command_projection']=deepcopy(projection)
            joint_projection=joint_command_projection(raw,self.queue.bounds)
            attempt['joint_command_projection']=deepcopy(joint_projection)
            prefix_diagnostics=discarded_prefix_bound_violations(converted,self.queue.bounds)
            attempt['discarded_prefix_bound_violations']=prefix_diagnostics
            rtc=response.get('rtc');require(isinstance(rtc,dict) and rtc.get('enabled') is context['enabled'] and
                rtc.get('previous_valid_steps')==context['previous_valid_steps'],'native bootstrap/RTC response phase mismatch')
            attempt['rtc_response']=deepcopy(rtc)
            attempt['plan_preparation_status']='STARTED'
            candidate=tail_plan(old,raw,obs,context['cut_monotonic_ns']/NS,self.queue.bounds,
                                           observation_ns=context['observation_monotonic_ns'])
            attempt['plan_preparation_status']='COMPLETE'
            prepared={'schema':'tiger-h50-prepared-reference-v2','request_id':predicted['request_id'],'rtc_context':context,
                'source_packet_ready_monotonic_ns':snapshot['packet_ready_monotonic_ns'],'raw_h50':raw,'gripper_command_projection':deepcopy(projection),
                'joint_command_projection':deepcopy(joint_projection),
                'plan_preparation_scope':attempt['plan_preparation_scope'],
                'plan_preparation_source_sha256':deepcopy(attempt['plan_preparation_source_sha256']),
                'old_plan_id':old.id,'candidate_plan_id':candidate.id,
                'inference_phase':context['inference_phase'],'reference_scope':context['reference_scope'],
                'discarded_prefix_bound_violations':prefix_diagnostics,'mechanical_bounds_checked_indices':[12,49],
                'old_reference':old.serializable(),'candidate_reference':candidate.serializable(),
                'scope':'VALIDATED_NOT_COMMITTED',
                'commit_decision_file':'h50-commit-%06d.json'%index}
            if r.no_send:
                require(self.queue is not r.queue and self.queue.diagnostic and r.queue.no_send,
                        'diagnostic queue must be separate from physical NO_SEND HOLD')
                prepared['physical_unsent_hold_reference']=r.queue.plan.serializable()
            r.journal.write_json(r.directory/('h50-prepared-%06d.json'%index),prepared)
            r.guard();ready=time.monotonic_ns()
            attempt['commit_ready_monotonic_ns']=ready
            attempt['packet_to_commit_ready_ms']=(ready-snapshot['packet_ready_monotonic_ns'])/1e6
            attempt['deadline_met']=ready<=context['deadline_monotonic_ns']
            attempt['commit_check_monotonic_ns']=ready
            if not attempt['deadline_met']:
                attempt['status']='LATE_PREDICTION_BUFFERED_NOT_COMMITTED'
                if commit:self.queue.brake('COMMIT_DEADLINE_EXCEEDED',time.monotonic())
            elif not commit:attempt['status']='VALIDATED_WITHOUT_COMMIT'
            elif self.queue.commit(candidate,context,(commit_ns:=time.monotonic_ns())/NS,now_ns=commit_ns):
                attempt['status']='NO_SEND_DIAGNOSTIC_PLAN_COMMITTED' if r.no_send else 'FUTURE_TAIL_COMMITTED'
            else:attempt['status']='RESULT_NOT_COMMITTED_QUEUE_CHANGED_OR_STOPPING'
            accepted=self.queue.plan is candidate
            attempt['plan_commit']={'schema':'tiger-h50-plan-commit-v1','request_index':request_index,'observation_index':index,
                'inference_phase':context['inference_phase'],'reference_scope':context['reference_scope'],
                'old_plan_id':old.id,'candidate_plan_id':candidate.id,'active_plan_id':self.queue.plan.id,
                'accepted':accepted,'status':attempt['status'],
                'decision_monotonic_ns':commit_ns if accepted else time.monotonic_ns(),
                'deadline_monotonic_ns':context['deadline_monotonic_ns'],'cut_monotonic_ns':context['cut_monotonic_ns'],
                'accepted_plan_count':self.queue.accepted,'rejected_plan_count':self.queue.rejected,
                'physical_dispatch_performed':False,'diagnostic_only':r.no_send}
            r.journal.write_json(r.directory/('h50-commit-%06d.json'%index),attempt['plan_commit'])
            if not accepted and commit and self.queue.phase=='RUNNING':
                self.queue.brake('PLAN_NOT_COMMITTED',time.monotonic())
        except Exception as exc:
            if attempt['plan_preparation_status']=='STARTED':attempt['plan_preparation_status']='FAILED'
            attempt['status']='FAILED_OUTPUT_UNAVAILABLE' if 'prediction' not in attempt else 'FAILED_VALIDATION_NOT_COMMITTED'
            attempt['error']=repr(exc)
            r.receipt['prediction_failure_detail']={'request_index':attempt.get('request_index'),'inference_phase':attempt.get('inference_phase'),'error':repr(exc)}
            if commit and self.queue.phase=='RUNNING':
                try:r.guard()
                except Exception as guard_error:self.queue.abort('GUARD_FAILURE: '+repr(guard_error))
                else:self.queue.brake('PREDICTION_FAILED',time.monotonic())
        finally:
            attempt['finished_monotonic_ns']=time.monotonic_ns()
            r.events.enqueue({'kind':'RTC_QUEUE_DECISION','request_index':index,'status':attempt['status'],
                'deadline_met':attempt.get('deadline_met'),'commit_ready_latency_ms':attempt.get('packet_to_commit_ready_ms'),
                'inference_phase':attempt.get('inference_phase'),'reference_scope':attempt.get('reference_scope'),
                'plan_commit':deepcopy(attempt.get('plan_commit')),
                'error':attempt.get('error'),'physical_dispatch_performed_by_prediction':False})
        return attempt


class AsyncPredictions:
    def __init__(self,resource,queue,trial_index):
        self.r,self.queue=resource,queue;self.processor=PredictionProcessor(resource,queue,trial_index)
        self.pending=LatestObservation();self.quit=threading.Event();self.inflight_deadline_ns=None;self.failure=None
        self.fetcher=threading.Thread(target=self._fetch,name='h50-latest-observation',daemon=True)
        self.worker=threading.Thread(target=self._infer,name='h50-single-inflight-rtc',daemon=True)
    def start(self):self.fetcher.start();self.worker.start();return self
    def _fetch(self):
        origin=time.monotonic_ns();slot=0
        try:
            while not self.quit.is_set() and self.queue.phase=='RUNNING':
                if self.quit.wait(max(0.,(replan_due_ns(origin,slot)-time.monotonic_ns())/NS)):break
                self.r.guard();self.pending.put(self.r.rpc(self.r.cfg,'/observation/fp32',timeout=.25))
                slot=next_replan_slot(origin,time.monotonic_ns(),slot)
        except Exception as exc:
            self.failure=repr(exc)
            if self.queue.phase=='RUNNING':self.queue.brake('OBSERVATION_FAILED',time.monotonic())
        finally:self.pending.close()
    def _infer(self):
        index=0
        while not self.quit.is_set():
            snapshot=self.pending.take()
            if snapshot is None:return
            self.inflight_deadline_ns=snapshot['packet_ready_monotonic_ns']+DEADLINE_NS
            self.processor.process(snapshot,index);self.inflight_deadline_ns=None;index+=1
            if self.queue.phase!='RUNNING':return
    def check_deadline(self,now_ns):
        require(type(now_ns) is int,'integer deadline check required')
        if self.inflight_deadline_ns is not None and now_ns>self.inflight_deadline_ns and self.queue.phase=='RUNNING':
            self.queue.brake('COMMIT_DEADLINE_EXCEEDED_INFLIGHT',now_ns/NS)
    def close(self):
        self.quit.set();self.pending.close();self.fetcher.join(1);self.worker.join(4)
        return {'fetch_stopped':not self.fetcher.is_alive(),'inference_stopped':not self.worker.is_alive(),
                'failure':self.failure,'superseded_packet_ready_ns':list(self.pending.superseded)}


def model_acceptance_for_run(runtime,operator_skip_no_motion_acceptance=False):
    """Explicit operator experiment waiver; never manufacture an acceptance PASS."""
    require(type(operator_skip_no_motion_acceptance) is bool,'manual experiment selection must be boolean')
    if operator_skip_no_motion_acceptance:
        return {'status':'NOT_VALIDATED_OPERATOR_MANUAL_EXPERIMENT','validated':False,
                'no_motion_performance_acceptance_skipped':True,
                'scope':'Operator explicitly chose this physical trial without the100-call no-motion acceptance; runtime hardware/feedback/STOP/deadline checks remain required.'}
    return runtime.require_acceptance()


class Resources:
    def __init__(self,cfg,session,stop_event,step,no_send,trial_index,operator_skip_no_motion_acceptance=False,task=None):
        require(type(operator_skip_no_motion_acceptance) is bool and not(no_send and operator_skip_no_motion_acceptance),
                'manual experiment waiver is only valid for an explicit APPLY run')
        self.operator_skip_no_motion_acceptance=operator_skip_no_motion_acceptance
        self.cfg,self.session,self.stop_event,self.step,self.no_send=deepcopy(cfg),session,stop_event,step,no_send
        self.setting=_settings(self.cfg);self.h=_hooks();self.trial_index=trial_index
        self.task=validate_prompt(self.setting['task'] if task is None else task)
        self.runtime=self.cache=self.events=self.adapter=self.query=self.sender=self.predictions=self.publisher=self.journal=None
        self.parent=None
        self.owned_path=self.directory=None;self.counter_before=None;self.hardware_events=deque(maxlen=64)
        self.receipt={'schema':'tiger-h50-async-run-v1','mode':'NO_SEND' if no_send else 'APPLY',
            'status':'STARTED','step':step,'trial_index':trial_index,'contract':deepcopy(self.setting),
            'task':self.task,'default_task':self.setting['task'],
            'custom_prompt':self.task!=self.setting['task'],'prompt_scope':'immutable_for_this_run',
            'operator_skip_no_motion_acceptance':operator_skip_no_motion_acceptance,
            'timing_basis':self.setting['commit_timing_basis'],
            'storage_semantic_version':self.setting['storage_semantic_version'],
            'storage_state':'NOT_STARTED','usb_finalflush_validated':False,
            'plan_preparation_scope':'DIAGNOSTIC_CHAIN_REAL_PLANS_NO_PHYSICAL_COMMIT' if no_send else 'REAL_TAIL_PLAN_FOR_APPLY',
            'startup_semantic_version':self.setting['startup_semantic_version'],
            'no_send_semantic_version':self.setting['no_send_semantic_version'],
            'inference_mode':'native32_bootstrap_then_rtc',
            'plan_preparation_source_sha256':{
                'executor':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'core':hashlib.sha256(Path(_reference_core.__file__).read_bytes()).hexdigest()},
            'attempts':[],'cleanup_errors':[],'module_CAN_api_calls':0 if no_send else None,
            'no_send_scope':'Separate diagnostic moving-plan chain; physical sender remains UNSENT HOLD. Real observations stay unchanged; not physical closed-loop tracking.' if no_send else None,
            'stop_semantics':'STOP/fault stops new targets; last received target may finish. Not hardware E-stop.',
            'actual_motor_velocity_acceleration_verified':False,'automatic_enable':False,
            'automatic_ready':False,'automatic_zero_return':False,'physical_camera_drop_count':None}
        session.report['h50_async_run']=self.receipt
    def rpc(self,cfg,path,data=None,timeout=2):
        return self.h['preserving_rpc'](self.h['rpc'],cfg,path,data,timeout=timeout)
    def guard(self):
        require(not self.stop_event.is_set(),'software STOP/cancel requested')
        if self.runtime is not None:self.runtime.check_alive()
        if self.cache is not None:self.cache.check_alive()
        if self.journal is not None:self.journal.check_alive()
        if self.events is not None:
            require(self.events.status().get('timing_evidence_incomplete') is False,'async evidence queue incomplete')
    def wait(self,seconds,*,guarded=True):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if guarded:self.guard()
            time.sleep(min(.05,max(0.,end-time.monotonic())))
    def start(self):
        cfg,h=self.cfg,self.h
        require(Path(h['verify_usb'](cfg))==Path(cfg['usb']),'USB verifier returned wrong root')
        require(Path(self.session.path).is_relative_to(Path(cfg['usb'])),'run session outside selected USB')
        h['require_not_stopped'](cfg);self.guard()
        self.parent=h['parent_life']();self.receipt['operator_parent_guard']=self.parent.receipt()
        self.publisher=h['publisher'](cfg,self.session.path.name);self.session._task_publisher=self.publisher
        self.publisher.publish('deployment_started',scope='NO_SEND RTC acceptance' if self.no_send else 'H50 async RTC preflight')
        self.runtime=h['runtime'](cfg,self.step,mode='deployment')
        self.receipt['model']=_health(self.runtime.ensure_ready(),self.step)
        if not self.no_send:self.receipt['prior_acceptance']=model_acceptance_for_run(self.runtime,self.operator_skip_no_motion_acceptance)
        self.receipt['lease']=self.runtime.acquire(self.session.path.name,trial_index=self.trial_index)
        status=h['ready_hub'](cfg,Path(cfg['remote_bundle']),self.rpc,stop_event=self.stop_event,session=self.session)['sensors']
        current=status['recording']
        require(current.get('active') is False and current.get('saving') is False and
                current.get('owned_stop_protocol')==h['stop_protocol'],'existing or unknown recording owner')
        self.counter_before=h['tx_counters'](cfg)
        self.receipt['interface_tx_before']=deepcopy(self.counter_before)
        self.receipt['hub_application_send_before']=h['hub_no_send'](status)
        self.cache=HubFeedbackCache(cfg,self.rpc,status['producer'],status['storage_binding'],h['verify_usb'],h['stop_requested'],
                                     parent_check=self.parent.check).start()
        record_task=self.task+' [h50-async:'+uuid.uuid4().hex+']'
        try:
            response=self.rpc(cfg,'/record/start',{'task':record_task},timeout=20)
            self.owned_path=h['episode_path'](cfg,response.get('path'),current.get('path'))
        except BaseException:
            try:self.owned_path=h['recover_start'](cfg,self.rpc,current.get('path'),record_task,h)
            except Exception as exc:self.receipt['recording_start_outcome_unknown']=repr(exc)
            raise
        self.receipt['owned_recording_path']=self.owned_path;self.cache.bind_recording(self.owned_path)
        self.directory=Path(self.owned_path)/'evidence';fd=h['open_dir'](self.directory,cfg,create=True);os.close(fd)
        self.events=h['event_writer'](cfg,self.directory/'h50-async-events-and-host-tx.jsonl',capacity=4096)
        self.receipt['events_start']=self.events.start();self.journal=h['journal'](cfg,self.owned_path)
        self.receipt['storage_state']='BOUNDED_RAM_ASYNC_USB_RECORDING'
        self.receipt['record_before_started_ns']=time.monotonic_ns();self.wait(2.)
        self.receipt['record_before_completed_ns']=time.monotonic_ns()
        self.bounds=[list(pair) for pair in h['documented']]+[list(cfg['gripper_bounds_m'])]
        self.receipt['bounds_source']='SDK_DOCUMENTED_ONLY_NO_QUERY_NO_SEND' if self.no_send else 'FRESH_CONTROLLER_AND_SDK_INTERSECTION'
        if not self.no_send:self._open_writer()
        self.receipt['output_command_policy']=OUTPUT_COMMAND_POLICY
        self.receipt['output_command_bounds_deg_m']=deepcopy(self.bounds)
        state=self.cache.control_snapshot()
        h['feedback_guard'](state,max_age_s=.2,require_enabled=not self.no_send,require_can=not self.no_send)
        h['gripper_guard'](state,max_age_s=.2,require_enabled=not self.no_send,now_monotonic=time.monotonic())
        self.anchor=[*state['q_deg'],state['grip_m']]
        require(all(finite(q) and lo<=q<=hi for q,(lo,hi) in zip(self.anchor,self.bounds)),'actual current anchor outside command bounds')
        self.receipt['initial_actual_q_deg_m']=list(self.anchor)
        origin=time.monotonic_ns()
        self.queue=AsyncQueue(self.anchor,self.bounds,origin/NS,no_send=self.no_send,start_ns=origin)
        self.reference_queue=AsyncQueue(self.anchor,self.bounds,origin/NS,diagnostic=True,start_ns=origin) if self.no_send else self.queue
        self.receipt['initial_physical_reference']=self.queue.plan.serializable()
        self.receipt['initial_planning_reference']=self.reference_queue.plan.serializable()
        self.sender=AsyncSender(self.adapter,self.queue,self.cache,self.guard,h,self.events,no_send=self.no_send,
            diagnostic_queue=self.reference_queue if self.no_send else None).start()
        self.publisher.publish('policy_started',rtc_enabled=True,no_send=self.no_send)
    def _open_writer(self):
        h,cfg=self.h,self.cfg;entry=cfg['can']['right']
        params=dict(interface=entry['interface'],expected_serial=entry['expected_serial'],sdk_version='0.6.2',
            leader_isolated=True,max_feedback_age_s=.2,native_speed_percent=self.setting['native_speed_percent'],
            gripper_bounds_m=cfg['gripper_bounds_m'],gripper_effort_mNm=cfg['gripper_effort_mNm'],
            command_limits_deg=deepcopy(h['documented']))
        params.update(h['sdk_profile'](cfg,'right'))
        self.query=h['adapter'](params,self.cache.control_snapshot,self.hardware_events.append)
        try:
            self.query.attach_tx_evidence(self.events);self.query.connect_for_apply(query_only=True)
            limits=self.query.query_limits(self.cache.latest_frame);self.receipt['raw_controller_limits']=limits
        finally:self.query.close();self.query=None
        require(len(limits['limits'])==6,'six actual controller limits required')
        bounds=[[max(n['minimum_deg'],d[0]),min(n['maximum_deg'],d[1])] for n,d in zip(limits['limits'],h['documented'])]
        require(all(lo<hi for lo,hi in bounds),'controller/documented bounds do not intersect')
        self.bounds=bounds+[list(cfg['gripper_bounds_m'])];params['command_limits_deg']=bounds
        self.adapter=h['adapter'](params,self.cache.control_snapshot,self.hardware_events.append)
        self.adapter.attach_tx_evidence(self.events);self.guard();self.adapter.connect_for_apply()
        # No handoff, enable, ready movement or zero write: current mode must already be valid.
        self.adapter.feedback(require_can=True);self.adapter.require_no_foreign_commands()
    def close(self,primary):
        h,cfg=self.h,self.cfg;errors=self.receipt['cleanup_errors']
        if self.predictions is not None:
            self.receipt['prediction_workers']=self.predictions.close()
            if not all(self.receipt['prediction_workers'][k] for k in ('fetch_stopped','inference_stopped')):errors.append('prediction worker not stopped')
        if self.sender is not None:
            self.receipt['sender_stopped']=self.sender.close();self.receipt['sender_ticks']=self.sender.ticks
            self.receipt['sender_failure']=self.sender.failure
            if self.sender.failure:errors.append('sender/observer failure: '+self.sender.failure)
        writer_closed=self.adapter is None
        for label,adapter in (('query',self.query),('writer',self.adapter)):
            if adapter is not None:
                try:adapter.close();writer_closed=True if label=='writer' else writer_closed
                except Exception as exc:errors.append(label+' host socket close: '+repr(exc))
        if self.sender is not None and not self.receipt.get('sender_stopped'):
            self.sender.thread.join(1);self.receipt['sender_stopped']=not self.sender.thread.is_alive()
            if not self.receipt['sender_stopped']:errors.append('sender did not terminate after host socket close')
        if self.adapter is not None:self.receipt['packets_sent']=self.adapter.packets_sent
        if self.owned_path is not None:
            self.receipt['post_record_started_ns']=time.monotonic_ns();self.wait(2.,guarded=False)
            self.receipt['post_record_completed_ns']=time.monotonic_ns()
        if self.cache is not None:
            self.cache.owned_path=None;self.receipt['cache_final']=self.cache.close()
            if not self.receipt['cache_final']['stopped']:errors.append('Hub cache worker not stopped')
            if self.cache.failure and primary is None:errors.append('Hub cache failure: '+self.cache.failure)
        if self.events is not None:
            self.receipt['events_final']=self.events.close(timeout_s=5)
            if self.receipt['events_final'].get('timing_evidence_incomplete') is not False:errors.append('USB async evidence incomplete')
        if self.owned_path is not None:
            try:
                if self.publisher:self.publisher.publish('recording_pending',motion_closed=writer_closed and self.receipt.get('sender_stopped',True))
                final=h['stop_owned'](cfg,self.rpc,self.owned_path,timeout=300);self.receipt['recording_finalization']=final
                require(final.get('completed') is True and final.get('healthy') is True,'owned native recording incomplete')
            except Exception as exc:
                errors.append('owned recording finalization: '+repr(exc))
        if self.journal is not None:
            safe_to_close=self.predictions is None or self.receipt['prediction_workers'].get('inference_stopped') is True
            self.receipt['storage_state']='USB_FINAL_FLUSH_PENDING'
            if safe_to_close:
                try:
                    final=self.journal.close();self.receipt['journal_final']=final
                    self.receipt['journal_closed']=True
                    require(isinstance(final,dict) and final.get('complete') is True and
                            final.get('status')=='USB_FLUSH_COMPLETE' and
                            type(final.get('accepted')) is int and final['accepted']==final.get('written') and
                            final.get('pending_records')==0 and final.get('pending_bytes')==0,
                            'buffered model/reference journal was not fully flushed to USB')
                    self.receipt['storage_state']='USB_JOURNAL_FLUSH_COMPLETE_REQUIRES_ARTIFACT_AUDIT'
                except Exception as exc:
                    errors.append('H50 journal final flush: '+repr(exc))
                    self.receipt['storage_state']='FAILED_USB_NOT_FULLY_SAVED'
                    self.receipt.setdefault('journal_closed',False)
            else:
                self.receipt['journal_closed']=False;self.receipt['storage_state']='FAILED_USB_NOT_FULLY_SAVED'
                errors.append('journal remains owned by an unfinished inference worker; not closed underneath it')
        if self.owned_path is not None and self.publisher:
            native=self.receipt.get('recording_finalization',{})
            journal=self.receipt.get('journal_final',{})
            if native.get('completed') is True and native.get('healthy') is True and journal.get('complete') is True and self.receipt.get('storage_state')=='USB_JOURNAL_FLUSH_COMPLETE_REQUIRES_ARTIFACT_AUDIT':
                self.publisher.publish('recording_pending',native_flushed=True,model_journal_flushed=True,
                    usb_finalflush_validated=False,requires_qc=True,owned_episode=self.owned_path)
            else:self.publisher.publish('recording_failed',reason='native recording or buffered journal final flush incomplete')
        if self.counter_before is not None:
            try:
                after=h['tx_counters'](cfg);self.receipt['interface_tx_after']=after
                delta={role:after[role]['tx_packets']-before['tx_packets'] for role,before in self.counter_before.items()}
                self.receipt['interface_tx_delta']=delta
                if self.no_send and any(v!=0 for v in delta.values()):errors.append('interface TX changed during NO_SEND; attribution unknown')
            except Exception as exc:errors.append('kernel TX counter read: '+repr(exc))
        if self.runtime is not None:
            try:self.receipt['lease_release']=self.runtime.close()
            except Exception as exc:errors.append('model lease release: '+repr(exc))
        if self.publisher:
            # The finalizer owns the final recording result after independent
            # readback and video/CSV export. Keep this existing metadata worker
            # and its sequence/heartbeat alive until that audit completes.
            self.receipt['task_event_delivery']=self.publisher.status()
            self.receipt['task_event_delivery']['close_deferred_until_artifact_audit']=True
            if self.receipt['task_event_delivery'].get('errors'):errors.append('task metadata delivery incomplete')
        self.receipt['last_hardware_events']=list(self.hardware_events)
        if primary is not None or errors:self.receipt['status']='FAILED_NO_AUTOMATIC_RECOVERY'
        if self.owned_path is not None:
            try:
                directory=Path(self.owned_path)/'evidence';fd=h['open_dir'](directory,cfg,create=True);os.close(fd)
                h['write_json'](directory/'h50-async-run.json',self.receipt,cfg)
            except Exception as exc:errors.append('final USB receipt: '+repr(exc));self.receipt['status']='FAILED_NO_AUTOMATIC_RECOVERY'
        if errors and primary is None:raise RuntimeError('H50 cleanup/evidence failure: '+repr(errors))


@contextmanager
def _resources(cfg,session,stop_event,step,trial_index,*,no_send,operator_skip_no_motion_acceptance=False,task=None):
    require(type(step) is int and step in STEPS,'unsupported checkpoint')
    require(type(trial_index) is int and trial_index>=0,'nonnegative trial index required')
    r=Resources(cfg,session,stop_event,step,no_send,trial_index,operator_skip_no_motion_acceptance,task=task);primary=None
    with ExitStack() as locks:
        for name in ('h50-deployment','fp32-shadow-run','writer-can_right'):locks.enter_context(r.h['ownership'](name))
        try:r.start();yield r
        except BaseException as exc:
            primary=exc;r.receipt.update(status='FAILED_NO_AUTOMATIC_RECOVERY',error=repr(exc))
            if r.publisher:r.publisher.publish('fault',area='H50 async',reason=repr(exc))
            raise
        finally:r.close(primary)


def _wait_reference_and_actual_stop(r,timeout=4):
    deadline=time.monotonic()+timeout;history=deque()
    while True:
        r.guard();r.sender.check_alive();now=time.monotonic()
        require(now<deadline,'approved reference brake/actual settle timed out')
        if r.queue.phase=='STOPPED':
            state=r.cache.control_snapshot();r.h['feedback_guard'](state,max_age_s=.2,require_enabled=True,require_can=True)
            r.h['gripper_guard'](state,max_age_s=.2,require_enabled=True,now_monotonic=now)
            history.append((now,[*state['q_deg'],state['grip_m']]))
            while history and now-history[0][0]>.35:history.popleft()
            if len(history)>1 and now-history[0][0]>=.3 and all(
                max(q[j] for _,q in history)-min(q[j] for _,q in history)<=([.5]*6+[.002])[j] for j in range(7)):
                return state
        time.sleep(.02)


def run_h50_deployment(cfg,session,stop_event,step,duration=30.,leader_isolated=False,
                       path_reviewed=False,prepare_ready=False,*,no_send=False,
                       current_start_confirmed=False,trial_index=0,operator_skip_no_motion_acceptance=False,task=None):
    require(prepare_ready is False,'legacy ready preparation is disabled by approved contract')
    require(no_send is False,'use explicit run_h50_no_motion entry for NO_SEND acceptance')
    require(leader_isolated is True and path_reviewed is True and current_start_confirmed is True,
            'explicit current-start, actual leader isolation and reviewed path confirmations required')
    require(finite(duration) and 1<=duration<=1800,'policy duration must be in 1..1800 seconds')
    with _resources(cfg,session,stop_event,step,trial_index,no_send=False,
                    operator_skip_no_motion_acceptance=operator_skip_no_motion_acceptance,task=task) as r:
        r.predictions=AsyncPredictions(r,r.queue,trial_index).start();deadline=time.monotonic()+duration
        r.receipt['policy_window_started_ns']=time.monotonic_ns()
        while time.monotonic()<deadline and r.queue.phase=='RUNNING':
            r.guard();r.sender.check_alive();r.predictions.check_deadline(time.monotonic_ns());time.sleep(.01)
        r.guard();r.sender.check_alive()
        if r.queue.phase=='RUNNING':r.queue.brake('POLICY_WINDOW_COMPLETED',time.monotonic())
        final=_wait_reference_and_actual_stop(r)
        r.receipt.update(status='POLICY_WINDOW_COMPLETED_REQUIRES_OPERATOR_REVIEW' if r.queue.reason=='POLICY_WINDOW_COMPLETED' else
                        'CONTROLLED_BRAKE_AFTER_QUEUE_OR_MODEL_FAILURE',stop_reason=r.queue.reason,
                        final_actual_q_deg=final['q_deg'],final_actual_gripper_m=final['grip_m'],
                        accepted_plan_count=r.queue.accepted,rejected_plan_count=r.queue.rejected,
                        physical_task_success='OPERATOR_REVIEW_REQUIRED')
        r.publisher.publish('deadline',duration_s=duration)
        r.publisher.publish('return_skipped',reason='approved contract ends at current position')
        if r.queue.reason!='POLICY_WINDOW_COMPLETED':
            raise RuntimeError('H50 deployment ended by approved controlled brake: '+str(r.queue.reason)+'; '+str(r.receipt.get('prediction_failure_detail',{}).get('error','')))
    return r.receipt


def _percentile(values,percent):
    if not values:return None
    ordered=sorted(values);index=(len(ordered)-1)*percent/100;low=int(index);high=min(len(ordered)-1,low+1)
    return ordered[low]+(ordered[high]-ordered[low])*(index-low)


def _wait_slot(target_ns,guard):
    while time.monotonic_ns()<target_ns:
        guard()
        time.sleep(max(0.,min(.02,(target_ns-time.monotonic_ns())/NS)))


def run_h50_no_motion(cfg,session,stop_event,step,count=3,warmup=0,*,trial_index=0,task=None):
    """Native bootstrap then diagnostic RTC chain; physical sender stays HOLD.

    Actual observations are neither synthesized nor advanced to match the
    diagnostic plan. This measures planning continuity/performance, not tracking.
    """
    require(type(count) is int and 1<=count<=1000 and type(warmup) is int and 0<=warmup<=100,'bounded count/warmup required')
    with _resources(cfg,session,stop_event,step,trial_index,no_send=True,task=task) as r:
        queue=r.reference_queue
        processor=PredictionProcessor(r,queue,trial_index);origin=time.monotonic_ns();slot=0
        r.receipt.update(requested_measured=count,requested_warmup=warmup)
        for index in range(count+warmup):
            r.guard();r.sender.check_alive()
            target=replan_due_ns(origin,slot)
            _wait_slot(target,r.guard)
            queue.sample(time.monotonic())
            if queue.phase!='RUNNING':break
            try:
                snapshot=r.rpc(r.cfg,'/observation/fp32',timeout=.25)
                attempt=processor.process(snapshot,index,warmup=index<warmup,commit=True)
                if attempt['status']!='NO_SEND_DIAGNOSTIC_PLAN_COMMITTED':
                    r.receipt['diagnostic_chain_terminated_at_observation']=index
                    r.receipt['diagnostic_chain_stop_reason']=attempt['status']
                    break
            except Exception as exc:
                r.receipt['attempts'].append({'request_index':None,'observation_index':index,'warmup':index<warmup,
                    'status':'FAILED_OBSERVATION_UNAVAILABLE','error':repr(exc),'physical_dispatch_performed':False})
                queue.abort('OBSERVATION_UNAVAILABLE_NO_AUTOMATIC_RETRY')
                r.receipt['diagnostic_chain_terminated_at_observation']=index
                r.receipt['diagnostic_chain_stop_reason']='FAILED_OBSERVATION_UNAVAILABLE'
                break
            slot=next_replan_slot(origin,time.monotonic_ns(),slot)
        measured=[a for a in r.receipt['attempts'] if not a['warmup']]
        values=[a['packet_to_commit_ready_ms'] for a in measured if 'packet_to_commit_ready_ms' in a]
        all_good=len(measured)==count and len(r.receipt['attempts'])==count+warmup and all(
            a.get('deadline_met') is True and a['status']=='NO_SEND_DIAGNOSTIC_PLAN_COMMITTED' for a in r.receipt['attempts'])
        r.receipt.update(status='NO_SEND_COMPLETED_REQUIRES_ROOT_AUDIT' if all_good else 'NO_SEND_COMPLETED_WITH_FAILURES',
            requested_measured=count,requested_warmup=warmup,completed_measured=len(measured),
            measured_deadline_passes=sum(a.get('deadline_met') is True for a in measured),
            measured_timed_predictions=len(values),measured_missing_latency_count=len(measured)-len(values),
            actual_model_requests=sum(a.get('request_index') is not None for a in r.receipt['attempts']),
            measured_commit_ready_latencies_ms=values,measured_commit_ready_p50_ms=_percentile(values,50),
            measured_commit_ready_p95_ms=_percentile(values,95),measured_commit_ready_max_ms=max(values) if values else None,
            model_outputs_never_dispatched=True,physical_unsent_reference_never_replaced_by_model=True,
            diagnostic_reference_plan_count=queue.accepted,
            diagnostic_final_reference=queue.plan.serializable(),
            physical_final_unsent_reference=r.queue.plan.serializable(),
            native_bootstrap_calls=sum(a.get('inference_phase')=='NATIVE_BOOTSTRAP' for a in r.receipt['attempts']),
            rtc_continuation_calls=sum(a.get('inference_phase')=='RTC_CONTINUATION' for a in r.receipt['attempts']),
            physical_motion_validation='NOT_PERFORMED',root_acceptance_written=False)
        r.publisher.publish('policy_completed',no_send=True,measured_count=len(measured))
        r.publisher.publish('return_skipped',reason='NO_SEND never created a hardware writer')
    return r.receipt
