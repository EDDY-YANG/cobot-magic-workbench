"""Injected NO_SEND experiment bridge. Import constructs no SDK/socket/worker.

The root-owned resource provider keeps the canonical owner locks, Hub producers,
model lease and verified USB journal. This isolated stage rejects APPLY and any
writer adapter; its diagnostic reference never becomes a hardware target.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import threading
import time

MODES = ("NO_SEND",)
WIRE_FIELDS = ("schema", "enabled", "previous_actions", "previous_valid_steps", "freeze_steps",
               "execution_label_hz", "native_training_hz", "beta", "trial_index", "request_index")
NS = 1_000_000_000


class LiveBridgeRejected(RuntimeError):
    pass


def require(condition,message):
    if not condition:raise LiveBridgeRejected(message)


def finite_vector(value,n):
    return isinstance(value,(list,tuple)) and len(value)==n and all(type(x) in (int,float) and math.isfinite(x) for x in value)


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()


@dataclass(frozen=True)
class LiveRequest:
    _runtime_context: dict
    _wire: dict
    _internal: dict

    @property
    def runtime_context(self):return deepcopy(self._runtime_context)
    @property
    def wire(self):return deepcopy(self._wire)
    @property
    def internal(self):return deepcopy(self._internal)


class LiveContextBridge:
    def __init__(self,mode,session_id,approved_contract,approved_contract_sha256,guard):
        require(mode=='NO_SEND','this offline finishing branch forbids APPLY')
        require(isinstance(session_id,str) and session_id,'unique current session identity required')
        require(callable(guard),'canonical resource guard required')
        require(isinstance(approved_contract_sha256,str) and len(approved_contract_sha256)==64 and
                all(c in '0123456789abcdef' for c in approved_contract_sha256.lower()),'exact approved timing SHA required')
        expected={'action_hz':18,'send_hz':50,'horizon':50,'rtc_freeze_steps':12,'rtc_known_steps':35,
                  'replan_steps':14,'reply_deadline_ms':600,'rtc_beta':5.0,'native_training_hz':30}
        require(all(approved_contract.get(k)==v for k,v in expected.items()),'live timing contract differs')
        for key in ('startup_semantic_version','execution_contract_version','no_send_semantic_version'):
            require(isinstance(approved_contract.get(key),str) and approved_contract[key],'startup/timing semantic identity missing')
        self._mode,self.session_id,self.contract,self.sha,self.guard=mode,session_id,deepcopy(approved_contract),approved_contract_sha256,guard
        self.next_index=0;self.next_response_index=0;self.seen_request_ids=set()

    @property
    def mode(self):return self._mode

    def prepare(self,queue_context,snapshot,*,now_ns=None):
        """Copy queue math into a live request; never relabel/mutate pending data."""
        self.guard()
        context=deepcopy(queue_context)
        now=time.monotonic_ns() if now_ns is None else now_ns
        packet=snapshot.get('packet_ready_monotonic_ns')
        require(type(now) is int and type(packet) is int and 0<=now-packet<=200_000_000,'live observation packet is stale/future')
        require(context.get('observation_monotonic_ns')==packet,'observation anchor differs from current snapshot')
        require(context.get('request_index')==self.next_index,'request duplicated/reversed/skipped')
        index=self.next_index
        enabled=index!=0
        require(context.get('inference_phase')==('NATIVE_BOOTSTRAP' if not enabled else 'RTC_CONTINUATION'),'native/RTC phase differs')
        expected={'schema':'tiger-fp32-h50-rtc-v1','enabled':enabled,'previous_valid_steps':35 if enabled else 0,
                  'freeze_steps':12,'execution_label_hz':18,'native_training_hz':30,'beta':5.0}
        require(all(type(context.get(k)) is type(v) and context[k]==v for k,v in expected.items()),'strict RTC fields differ')
        previous=context.get('previous_actions')
        require(isinstance(previous,list) and len(previous)==expected['previous_valid_steps'] and all(finite_vector(row,7) for row in previous),'finite actual accepted-reference prefix required')
        trial=context.get('trial_index')
        require(type(trial) is int and 0<=trial<=1_000_000,'invalid current trial index')
        require(context.get('deadline_monotonic_ns')==packet+600_000_000,'deadline differs')
        require(context.get('cut_monotonic_ns')==packet+(12*NS+9)//18,'protected cut differs')
        require(isinstance(context.get('context_id'),str) and context['context_id'] and type(context.get('generation')) is int,'queue context identity/generation missing')
        require(isinstance(context.get('old_plan_id'),str) and context['old_plan_id'],'accepted plan identity missing')
        wire={key:deepcopy(context[key]) for key in WIRE_FIELDS}
        scope='DIAGNOSTIC_UNSENT_REFERENCE' if self.mode=='NO_SEND' else 'PHYSICAL_ACCEPTED_REFERENCE'
        runtime={**deepcopy(wire),'observation_monotonic_ns':packet,'cut_monotonic_ns':context['cut_monotonic_ns'],
                 'deadline_monotonic_ns':context['deadline_monotonic_ns'],'old_plan_id':context['old_plan_id'],
                 'inference_phase':context['inference_phase'],'reference_scope':scope,
                 'startup_semantic_version':self.contract['startup_semantic_version'],
                 'execution_contract_version':self.contract['execution_contract_version'],'timing_contract_sha256':self.sha,
                 'no_send_semantic_version':self.contract['no_send_semantic_version'] if self.mode=='NO_SEND' else None,
                 'prefix_reference_kind':'NATIVE_BOOTSTRAP_NO_PREFIX' if not enabled else
                     ('DIAGNOSTIC_UNSENT_REFERENCE' if self.mode=='NO_SEND' else 'ACCEPTED_REFERENCE_PLAN_FUTURE_NOT_ALL_SENT')}
        internal={'session_id':self.session_id,'mode':self.mode,'queue_context_id':context['context_id'],
                  'queue_generation':context['generation'],'queue_context':context,'reference_scope':scope,
                  'previous_actions_sha256':canonical_sha(previous),'requires_new_live_model_inference':True,
                  'historical_actions_or_echo_are_not_accepted':True,
                  'original_queue_tags_are_preserved_math_library_metadata_not_live_inference_provenance':True,
                  'scope':'LIVE_EXPERIMENT_SOURCE_PREPARATION_NOT_DEPLOYMENT_ACCEPTANCE'}
        self.next_index+=1
        return LiveRequest(runtime,wire,internal)

    def validate_prediction(self,request,predicted):
        """Require the existing recorded ModelRuntime result, never bare actions."""
        self.guard();wire=request.wire;internal=request.internal
        require(internal['session_id']==self.session_id and internal['mode']==self.mode,'request belongs to another session/mode')
        require(wire['request_index']==self.next_response_index,'response duplicated/reversed/skipped')
        require(predicted.get('status')=='DEPLOYMENT_PREDICTION_BUFFERED','recorded current ModelRuntime result required')
        request_id=predicted.get('request_id')
        require(isinstance(request_id,str) and request_id and request_id not in self.seen_request_ids,'new live request identity required')
        require(predicted.get('rtc_context')==request.runtime_context,'recorded inference context differs')
        require(predicted.get('observation_anchor_monotonic_ns')==request.runtime_context['observation_monotonic_ns'],'response observation anchor differs')
        response=predicted.get('response',{})
        require(response.get('action_dtype')=='float32' and response.get('action_shape')==[50,7],'FP32 H50x7 response required')
        actions=response.get('actions')
        require(isinstance(actions,list) and len(actions)==50 and all(finite_vector(row,7) for row in actions),'finite original unprojected actions required')
        rtc=response.get('rtc',{})
        for key in WIRE_FIELDS:
            if key=='previous_actions':continue
            require(type(rtc.get(key)) is type(wire[key]) and rtc[key]==wire[key],'RTC response echo differs: '+key)
        require(rtc.get('trial_seed')==42+wire['trial_index'] and rtc.get('previous_actions_sha256')==internal['previous_actions_sha256'],'RTC seed/prefix SHA differs')
        self.seen_request_ids.add(request_id);self.next_response_index+=1
        return deepcopy(actions)


class BorrowedWriterBridge:
    """One NO_SEND diagnostic tick; no SDK/writer construction or dispatch.

    The provider owns lifetime, owner locks, real identity/limits, USB recording,
    model lease and current per-session operator assertions. No mode flip exists.
    """
    def __init__(self,mode,session_id,cache,guard,hooks,bounds,output_prepare,events,*,adapter=None,
                 tracking_check=None,session_assertions=None,physical_anchor=None):
        require(mode=='NO_SEND','this offline finishing branch forbids APPLY/writer use')
        require(isinstance(session_id,str) and session_id,'current session required')
        require(callable(guard) and callable(output_prepare),'canonical guards/output preparation required')
        require(all(callable(hooks.get(key)) for key in ('feedback_guard','gripper_guard','validate_q')),'canonical feedback/gripper/position hooks required')
        require(len(bounds)==7 and all(len(pair)==2 and math.isfinite(pair[0]) and math.isfinite(pair[1]) and pair[0]<pair[1] for pair in bounds),'seven real current bounds required')
        require(adapter is None,'NO_SEND cannot hold a writer adapter')
        require(finite_vector(physical_anchor,7),'actual initial anchor required for separate UNSENT HOLD')
        require(all(lo<=x<=hi for x,(lo,hi) in zip(physical_anchor,bounds)),'actual anchor outside real bounds; no feedback clipping')
        self._mode,self.session_id,self.cache,self.guard,self.h,self.bounds=mode,session_id,cache,guard,hooks,tuple(tuple(p) for p in bounds)
        self.prepare,self.events,self.adapter=output_prepare,events,adapter
        self.tracking,self.session_assertions=tracking_check,session_assertions
        self.anchor=tuple(physical_anchor) if physical_anchor is not None else None
        self.phase='RUNNING';self.last_tick_ns=None;self.last_sent=None;self.groups=0;self.lock=threading.RLock();self.last_abort=None

    @property
    def mode(self):return self._mode

    def _event(self,value):
        self.events.enqueue(value)

    def hard_abort(self,queue,reason):
        with self.lock:
            self.phase='ABORT'
            self.last_abort={'kind':'EXPERIMENT_HARD_ABORT_NO_NEW_GROUPS','session_id':self.session_id,
                             'reason':str(reason),'last_controller_target_may_continue':True,'hardware_estop':False}
            try:queue.hard_abort(str(reason))
            except Exception as exc:self.last_abort['reference_abort_error']=repr(exc)
            try:self._event(deepcopy(self.last_abort))
            except Exception as exc:self.last_abort['event_delivery_error']=repr(exc)
            return deepcopy(self.last_abort)

    def _healthy_snapshot(self,now_ns):
        self.guard()
        state=self.cache.control_snapshot()
        self.h['feedback_guard'](state,max_age_s=.2,require_enabled=False,require_can=False)
        self.h['gripper_guard'](state,max_age_s=.2,require_enabled=False,now_monotonic=now_ns/NS)
        return state

    def tick(self,queue,now_ns,*,scheduled_ns=None):
        with self.lock:
            require(self.phase!='ABORT','ABORT blocks all new groups')
            try:
                require(type(now_ns) is int and now_ns>0,'actual integer monotonic sample time required')
                require(self.last_tick_ns is None or 0<=now_ns-self.last_tick_ns<=100_000_000,'sender reversed clock or gap exceeded 100ms')
                state=self._healthy_snapshot(now_ns);self.last_tick_ns=now_ns
                # The physical HOLD is independent of the diagnostic future.
                with queue.group_sample(now_ns/NS) as group:
                    diagnostic=deepcopy(group)
                physical=list(self.anchor);output=self.prepare(physical,self.bounds)
                result={'kind':'EXPERIMENT_NO_SEND_PHYSICAL_UNSENT_HOLD','session_id':self.session_id,
                    'reference_sample_monotonic_ns':now_ns,
                    'physical_reference_deg_m':physical,'physical_reference_v_deg_m_s':[0.]*7,
                    'diagnostic_reference':diagnostic,'actual_q_deg':deepcopy(state['q_deg']),
                    'actual_gripper_m':state['grip_m'],'output_conversion':output,
                    'physical_dispatch_performed':False,'mode':'NO_SEND','model_closed_loop_tracking_validated':False}
                if scheduled_ns is not None:
                    require(type(scheduled_ns) is int and scheduled_ns>0,'integer scheduled tick timestamp required')
                    result['scheduled_tick_monotonic_ns']=scheduled_ns
                self.groups+=1;self._event(result);return result
            except Exception as exc:
                self.hard_abort(queue,'tick failed: '+repr(exc));raise

    def request_controlled_stop(self,queue,now_ns,stop_builder,*,reason):
        with self.lock:
            require(self.phase!='ABORT','hard-aborted bridge cannot send a stop trajectory')
            try:
                self._healthy_snapshot(now_ns)
                result=queue.controlled_stop(now_ns,stop_builder)
                require(result.get('status')=='STOP_REFERENCE_CERTIFIED','reference stop unavailable')
                self.phase='CONTROLLED_STOP'
                self._event({'kind':'EXPERIMENT_CONTROLLED_STOP_PLANNED','session_id':self.session_id,
                             'reason':str(reason),'physical_dispatch_performed':False,
                             'actual_stop_requires_later_feedback_settle_validation':True})
                return result
            except Exception as exc:
                self.hard_abort(queue,'controlled stop unavailable/guard failed: '+repr(exc));raise
