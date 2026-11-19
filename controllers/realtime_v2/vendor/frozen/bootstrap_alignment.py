"""Offline bootstrap protocol adapters; frozen rolling queue stays unchanged.

Native no-prefix output and guided output are different contracts. Neither is
relabeled as the author's hard-prefilled model. No model or device is called.
"""
from pathlib import Path
from dataclasses import dataclass,replace
import sys,json,hashlib,math,time,uuid
import numpy as np
ROOT=Path(__file__).resolve().parents[1];PARENT=ROOT.parent
QUEUE=PARENT/'2026-10-22-12-17-客户端时间与队列适配'
sys.dont_write_bytecode=True
sys.path.insert(0,str(QUEUE/'src'))
from client_time_queue import (Request,ClientTimeQueue,ServerTemporalAdapter,Curve,Slice,Plan,
    array,label_ns,KNOWN,HORIZON,FREEZE,NS,SEND_NS,DEADLINE_NS,ContractError,require)
from pinned_components import original_rtc_validator,official_temporal
RTC=original_rtc_validator()
LEGACY_BF16='tiger-pi05-bf16-backbone-f32-rtc-offline-v1'

@dataclass(frozen=True)
class ObservationBinding:
    """Trusted caller-side identity of the archived request, not image retiming."""
    observation_ns: int
    snapshot_sha256: str
    request_payload_sha256: str
    request_id: str
    policy_fingerprint_sha256: str
    @classmethod
    def from_record(cls,prediction):
        for key in ('snapshot_sha256','request_payload_sha256'):
            value=prediction.get(key)
            require(isinstance(value,str) and len(value)==64,'missing request hash: '+key)
        require(isinstance(prediction.get('request_id'),str) and prediction['request_id'],'missing request identity')
        return cls(int(prediction['observation_anchor_monotonic_ns']),prediction['snapshot_sha256'],
            prediction['request_payload_sha256'],prediction['request_id'],RTC.canonical_sha(prediction['policy_fingerprint']))
    def verify(self,prediction,wire):
        require(ObservationBinding.from_record(prediction)==self,'observation/model/request binding mismatch')
        recorded=prediction.get('rtc_context',{})
        recorded_wire={k:recorded.get(k) for k in RTC.bootstrap()}
        require(recorded_wire==wire,'saved model request RTC condition differs from pending request')
        require(prediction.get('status')=='DEPLOYMENT_PREDICTION_BUFFERED'
            and prediction.get('decision')=='PREDICTED_NOT_DISPATCHED_BY_CLIENT'
            and type(prediction.get('module_dispatched_commands')) is int
            and prediction['module_dispatched_commands']==0
            and prediction.get('deadline_met') is True,'saved inference is not a valid buffered result')

@dataclass(frozen=True)
class GuidedInitialRequest(Request):
    def wire(self):
        wire=super().wire()
        wire.update(enabled=True,previous_actions=[list(row) for row in self.previous],previous_valid_steps=KNOWN)
        return wire

class GuidedBootstrapQueue(ClientTimeQueue):
    """Only request0 changes: real accepted HOLD35 instead of an empty prefix."""
    def request(self,observation_ns,now_ns,*,clock_domain):
        original=super().request(observation_ns,now_ns,clock_domain=clock_domain)
        if original.index:
            return original
        previous=tuple(tuple(float(v) for v in row) for row in original.old_plan.sample(
            np.array([original.observation_phase_ns+label_ns(i) for i in range(KNOWN)],np.int64)))
        fields=dict(original.__dict__);fields['previous']=previous
        guided=GuidedInitialRequest(**fields)
        self.validate_rtc(guided.wire())
        self.pending=guided
        self.events.append({'event':'GUIDED_INITIAL_ACCEPTED_HOLD35','request_index':0,
            'model_prefill_kind':'existing_soft_guidance_not_author_hard_overwrite'})
        return guided

def validate_response(wire,response,*,evidence_kind):
    """Bind output to actual RTC condition; no acceptance by shape alone."""
    require(evidence_kind in ('SAVED_REAL_MODEL_RESPONSE','SYNTHETIC_PROTOCOL_FIXTURE'),
            'this adapter has no live inference admission')
    request=RTC.validate_rtc(wire)
    require(isinstance(response,dict) and isinstance(response.get('rtc'),dict),'RTC response metadata required')
    meta=response['rtc']
    for key in ('schema','enabled','trial_index','request_index','freeze_steps','execution_label_hz','native_training_hz','beta'):
        require(type(meta.get(key)) is type(request[key]) or key=='beta',f'response type mismatch: {key}')
        require(meta.get(key)==request[key],f'response condition mismatch: {key}')
    require(meta.get('previous_actions_sha256')==request['previous_actions_sha256'],'response prefix SHA mismatch')
    require(meta.get('trial_seed')==42+wire['trial_index'],'response seed mismatch')
    declared=meta.get('previous_valid_steps')
    active=KNOWN if request['enabled'] else 0
    legacy_capacity=(not request['enabled'] and declared==KNOWN and meta.get('implementation_schema')==LEGACY_BF16)
    require(declared==active or legacy_capacity,'response active-prefix length mismatch')
    raw=array(response.get('actions'),(50,7))
    return raw,{'evidence_kind':evidence_kind,'active_prefix_steps':active,
        'legacy_reported_prefix_capacity_only':legacy_capacity,
        'raw_declared_previous_valid_steps':declared,
        'prefix_sha256':request['previous_actions_sha256'],
        'raw_h50_sha256':hashlib.sha256(raw.tobytes()).hexdigest(),
        'model_input_condition_changed':False,'new_model_inference_performed':False}

class BoundTemporalAdapter(ServerTemporalAdapter):
    """Existing rolling temporal preparation plus real condition binding."""
    def prepare_response(self,request,response,*,evidence_kind):
        raw,binding=validate_response(request.wire(),response,evidence_kind=evidence_kind)
        candidate=super().prepare(request,raw,evidence_kind=(
            'STORED_HISTORICAL_H50' if evidence_kind=='SAVED_REAL_MODEL_RESPONSE' else 'SYNTHETIC_CPU_FIXTURE'))
        diag=dict(candidate.diagnostics)
        diag.update(response_binding=binding,known_context_points=len(request.previous))
        return replace(candidate,diagnostics=diag)

    def prepare_record(self,request,prediction,*,expected_binding):
        expected_binding.verify(prediction,request.wire())
        require(expected_binding.observation_ns==request.observation_ns,'observation clock changed')
        return self.prepare_response(request,prediction['response'],evidence_kind='SAVED_REAL_MODEL_RESPONSE')

@dataclass(frozen=True)
class NativeTicket:
    token: str
    anchor: np.ndarray
    bounds: np.ndarray
    observation_ns: int
    deadline_ns: int
    trial_index: int
    clock_domain: tuple
    def wire(self):return RTC.bootstrap(self.trial_index,0)

@dataclass(frozen=True)
class NativePrepared:
    token: str
    relative_plan: Plan
    raw_h50: np.ndarray
    diagnostics: dict
    not_before_ns: int
    seal: str

def prepared_seal(p):
    h=hashlib.sha256(p.token.encode());h.update(p.raw_h50.tobytes())
    h.update(str(p.not_before_ns).encode())
    for s in p.relative_plan.pieces:
        h.update(np.asarray([s.start,s.end],np.int64).tobytes())
        h.update(s.curve.times.tobytes());h.update(s.curve.q.tobytes())
    return h.hexdigest()

class NativeBootstrap:
    """Create the reference clock once at first usable response, never reset it.

    The MPC/estimator/plant run throughout the initial hold. This class never
    initializes or changes their states. It only initializes a reference queue.
    """
    def __init__(self,anchor,bounds,observation_ns,*,trial_index,clock_domain,sender_epoch_ns=None):
        q=array(anchor,(7,));b=array(bounds,(7,2))
        require(np.all(b[:,0]<b[:,1]) and np.all(q>=b[:,0]) and np.all(q<=b[:,1]),'invalid measured anchor/bounds')
        require(type(observation_ns) is int and observation_ns>0,'observation timestamp required')
        require(isinstance(clock_domain,tuple) and len(clock_domain)==3,'explicit clock domain required')
        self.ticket=NativeTicket(uuid.uuid4().hex,q,b,observation_ns,observation_ns+DEADLINE_NS,trial_index,clock_domain)
        RTC.validate_rtc(self.ticket.wire())
        self.status='WAITING_FIRST_RESULT';self.queue=None;self.activation_ns=None
        self.sender_epoch_ns=observation_ns if sender_epoch_ns is None else sender_epoch_ns
        require(type(self.sender_epoch_ns) is int and self.sender_epoch_ns>0,'sender clock origin required')

    def prepare(self,response,*,evidence_kind):
        require(self.status=='WAITING_FIRST_RESULT','no bootstrap re-entry')
        began=time.perf_counter_ns();t=self.ticket
        raw,binding=validate_response(t.wire(),response,evidence_kind=evidence_kind)
        targets=np.clip(raw,t.bounds[:,0],t.bounds[:,1])
        # Native H50 has 50 NEW targets and no model-generated prefill row.
        # Measured anchor is external geometry, not a fabricated model output.
        waypoints=np.vstack((t.anchor,targets))
        optimizer=official_temporal()(dt_ref=1/18,dt_min=(1/18)*(.010/.018),dt_max=(1/18)*(.020/.018),
            lambda_acc=10.,lambda_time=1.,stride=51,optim_dims=list(range(7)),v_max=None,horizon=51,logging=False)
        # Capture the exact original temporal implementation's source time map.
        original_solve=optimizer.solve;source_times=[]
        def traced_solve(*args):
            result=original_solve(*args);source_times.append(result[1].copy());return result
        optimizer.solve=traced_solve
        output=np.asarray(optimizer.optimize(waypoints.tolist()),float)
        require(output.shape==(51,7) and np.isfinite(output).all(),'bootstrap temporal layout failed')
        output=np.clip(output,t.bounds[:,0],t.bounds[:,1]);output[0]=t.anchor
        times=np.array([label_ns(i) for i in range(51)],np.int64)
        curve=Curve(times,output);plan=Plan((Slice(0,int(times[-1]),curve),),uuid.uuid4().hex)
        source_phase=np.interp(np.arange(51)/18,source_times[0],np.arange(51))
        diagnostics={'response_binding':binding,'model_response_shape':[50,7],
            'model_prefill_rows':0,'external_measured_anchor_rows':1,
            'temporal_input_rows':51,'source_target_indices':[0,49],'cropped_native_targets':0,
            'reference_interval_count':50,'reference_duration_s':label_ns(50)/NS,
            'source_knot_phase_at_reference_end':float(source_phase[-1]),
            'source_path_all_targets_used_as_input':True,'source_path_entire_end_executed':bool(source_phase[-1]>=50-1e-9),
            'target_projection_max_rad_m':np.max(abs(targets-raw),axis=0).tolist(),
            'first_target_offset_after_activation_ns':label_ns(1),
            'physical_state_reset_performed':False,'prepare_ms':(time.perf_counter_ns()-began)/1e6}
        p=NativePrepared(t.token,plan,raw,diagnostics,t.observation_ns,'')
        return replace(p,seal=prepared_seal(p))

    def prepare_record(self,prediction,*,expected_binding):
        expected_binding.verify(prediction,self.ticket.wire())
        require(expected_binding.observation_ns==self.ticket.observation_ns,'observation clock changed')
        p=self.prepare(prediction['response'],evidence_kind='SAVED_REAL_MODEL_RESPONSE')
        ready=int(prediction['ready_for_commit_monotonic_ns'])
        require(self.ticket.observation_ns<=ready<=self.ticket.deadline_ns,'stored result was not ready in time')
        p=replace(p,not_before_ns=ready,seal='')
        return replace(p,seal=prepared_seal(p))

    def activate(self,prepared,now_ns,*,clock_domain):
        t=self.ticket
        require(self.status=='WAITING_FIRST_RESULT','bootstrap already used/cancelled')
        if now_ns>t.deadline_ns:
            self.status='EXPIRED'
            raise ContractError('first response exceeded unchanged600ms deadline')
        require(clock_domain==t.clock_domain,'activation clock domain mismatch')
        require(now_ns>=self.sender_epoch_ns and (now_ns-self.sender_epoch_ns)%SEND_NS==0,
            'initialize phase0 on an existing sender tick, not between ticks')
        require(prepared.token==t.token and prepared.seal==prepared_seal(prepared),'bootstrap response mutated or belongs to another request')
        require(prepared.not_before_ns<=now_ns<=t.deadline_ns,'activation before result availability')
        require(prepared.relative_plan.start==0 and prepared.relative_plan.end==label_ns(HORIZON),'H50 span changed')
        require(np.array_equal(prepared.relative_plan.sample(0),t.anchor),'bootstrap anchor changed')
        # One constructor: phase0 has never run before this activation.
        q=ClientTimeQueue(t.anchor,t.bounds,now_ns,trial_index=t.trial_index,clock_domain=t.clock_domain)
        q.plan=prepared.relative_plan;q.revision=1;q.index=0;q.last_observation_ns=t.observation_ns
        q.events.append({'event':'NATIVE_BOOTSTRAP_PLAN_ACCEPTED_AT_CLOCK_INITIALIZATION',
            'model_observation_ns':t.observation_ns,'reference_epoch_ns':now_ns,'initial_request_index':0})
        self.queue=q;self.activation_ns=now_ns;self.status='RUNNING'
        return q

    def cancel(self):
        require(self.status=='WAITING_FIRST_RESULT','cancel cannot restart a running queue')
        self.status='CANCELLED'

    def expire(self,now_ns):
        if self.status=='WAITING_FIRST_RESULT' and now_ns>self.ticket.deadline_ns:
            self.status='EXPIRED'
            raise ContractError('bootstrap expired; no automatic retry/reset')
