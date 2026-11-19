"""CPU-only V2 reference-clock/queue adapter for unchanged H50 guided RTC.

Server role: original H50 -> boundary metadata -> pinned official temporal QP.
Client role: immutable pre-MPC reference, timestamp queries, future replacement.
No actuator, model, socket, SSH, camera, GPU or live-admission interface exists.
"""
from collections import deque
from dataclasses import dataclass
import hashlib,json,threading,time,uuid
import numpy as np
from pinned_components import official_temporal,original_rtc_validator

NS=1_000_000_000;HZ=18;SEND_NS=20_000_000;DEADLINE_NS=600_000_000
FREEZE=12;KNOWN=35;HORIZON=50;REPLAN=14;LOW_WATER_NS=(23*NS+9)//18
class ContractError(ValueError):pass
class StopRequired(ContractError):pass
def require(ok,message):
    if not ok:raise ContractError(message)
def stamp(value):
    require(type(value) is int and 0<=value<2**63,'integer monotonic nanoseconds required');return value
def label_ns(index):
    require(type(index) is int and index>=0,'nonnegative integer label index required');return (index*NS+9)//HZ
def completed_labels(elapsed_ns):return (stamp(elapsed_ns)*HZ+8)//NS
def array(value,shape):
    a=np.array(value,dtype=np.float64,copy=True)
    require(a.shape==shape and np.isfinite(a).all(),'finite array required: '+str(shape));a.flags.writeable=False;return a
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

class ReferenceClock:
    def __init__(self,start_ns):
        self.wall_anchor_ns=stamp(start_ns);self.phase_anchor_ns=0;self.paused=False;self.epoch=0;self.last_tick_ns=None
    def phase_at(self,wall_ns):
        stamp(wall_ns);require(wall_ns>=self.wall_anchor_ns,'observation predates current clock epoch')
        return self.phase_anchor_ns+(0 if self.paused else wall_ns-self.wall_anchor_ns)
    def pause(self,wall_ns):
        require(not self.paused,'already paused');self.phase_anchor_ns=self.phase_at(wall_ns)
        self.wall_anchor_ns=wall_ns;self.paused=True;self.epoch+=1
    def resume(self,wall_ns):
        require(self.paused and stamp(wall_ns)>=self.wall_anchor_ns,'invalid resume time')
        self.wall_anchor_ns=wall_ns;self.paused=False;self.epoch+=1
    def tick(self,wall_ns):
        phase=self.phase_at(wall_ns)
        require(self.last_tick_ns is None or wall_ns>self.last_tick_ns,'control clock must advance')
        if self.last_tick_ns is not None and wall_ns-self.last_tick_ns>100_000_000:raise StopRequired('control gap exceeded100ms')
        self.last_tick_ns=wall_ns;index=completed_labels(phase)
        fraction=(phase-label_ns(index))/(label_ns(index+1)-label_ns(index))
        return phase,index,float(fraction)
    def next_replan_after(self,wall_ns):
        require(not self.paused,'no inference requests while queue progress is paused')
        phase=self.phase_at(wall_ns);slot=phase*HZ//(REPLAN*NS)+1
        while label_ns(int(slot)*REPLAN)<=phase:slot+=1
        due_phase=label_ns(int(slot)*REPLAN)
        return self.wall_anchor_ns+due_phase-self.phase_anchor_ns

@dataclass(frozen=True)
class Curve:
    times: np.ndarray
    q: np.ndarray
    def __post_init__(self):
        times=np.array(self.times,dtype=np.int64,copy=True)
        require(times.ndim==1 and len(times)>=2 and np.all(np.diff(times)>0),'strictly increasing knot times')
        q=array(self.q,(len(times),7));times.flags.writeable=False
        object.__setattr__(self,'times',times);object.__setattr__(self,'q',q)
    def sample(self,at):
        t=np.asarray(at,dtype=np.int64);flat=t.reshape(-1)
        require(np.all(flat>=self.times[0]) and np.all(flat<=self.times[-1]),'curve coverage missing; no automatic padding')
        index=np.clip(np.searchsorted(self.times,flat,side='right')-1,0,len(self.times)-2)
        # Subtract integer timestamps before converting durations to float.
        f=(flat-self.times[index])/(self.times[index+1]-self.times[index])
        out=self.q[index]+f[:,None]*(self.q[index+1]-self.q[index])
        exact_right=flat==self.times[index+1];out[exact_right]=self.q[index[exact_right]+1]
        return out.reshape((*t.shape,7))
    def velocity(self,at,side='right'):
        stamp(at);require(self.times[0]<=at<=self.times[-1],'velocity coverage missing')
        i=int(np.clip(np.searchsorted(self.times,at,side=side)-1,0,len(self.times)-2))
        return (self.q[i+1]-self.q[i])/((int(self.times[i+1])-int(self.times[i]))/NS)

@dataclass(frozen=True)
class Slice:
    start: int
    end: int
    curve: Curve

@dataclass(frozen=True)
class Plan:
    pieces: tuple
    plan_id: str
    def __post_init__(self):
        require(bool(self.pieces),'empty plan')
        for s in self.pieces:require(s.start<s.end and s.curve.times[0]<=s.start<s.end<=s.curve.times[-1],'slice outside curve')
        for a,b in zip(self.pieces,self.pieces[1:]):
            require(a.end==b.start and np.array_equal(a.curve.sample(a.end),b.curve.sample(b.start)),'noncontinuous reference seam')
    @property
    def start(self):return self.pieces[0].start
    @property
    def end(self):return self.pieces[-1].end
    def sample(self,times):
        t=np.asarray(times,dtype=np.int64);flat=t.reshape(-1)
        require(flat.size>0 and np.all(flat>=self.start) and np.all(flat<=self.end),'accepted plan has insufficient coverage')
        indices=np.minimum(np.searchsorted([s.end for s in self.pieces],flat,side='right'),len(self.pieces)-1)
        out=np.empty((len(flat),7))
        for i in np.unique(indices):
            selected=indices==i;out[selected]=self.pieces[int(i)].curve.sample(flat[selected])
        return out.reshape((*t.shape,7))
    def prefix(self,start,end):
        require(self.start<=start<end<=self.end,'prefix interval unavailable')
        return tuple(Slice(max(s.start,start),min(s.end,end),s.curve) for s in self.pieces if s.end>start and s.start<end)
    def velocity(self,at,side='right'):
        i=min(int(np.searchsorted([s.end for s in self.pieces],at,side=side)),len(self.pieces)-1)
        return self.pieces[i].curve.velocity(at,side)

@dataclass(frozen=True)
class Request:
    token: str
    old_plan: Plan
    revision: int
    clock_epoch: int
    observation_ns: int
    observation_phase_ns: int
    cut_phase_ns: int
    deadline_ns: int
    cut_wall_ns: int
    trial: int
    index: int
    previous: tuple
    clock_domain: tuple
    def wire(self):
        return {'schema':'tiger-fp32-h50-rtc-v1','enabled':self.index!=0,'previous_actions':[list(r) for r in self.previous],
            'previous_valid_steps':0 if self.index==0 else KNOWN,'freeze_steps':FREEZE,'execution_label_hz':HZ,
            'native_training_hz':30,'beta':5.0,'trial_index':self.trial,'request_index':self.index}
    @property
    def cut_anchor(self):return self.old_plan.sample(self.cut_phase_ns)

@dataclass(frozen=True)
class Candidate:
    token: str
    old_plan_id: str
    revision: int
    clock_epoch: int
    plan: Plan
    raw_h50: np.ndarray
    temporal_tail: np.ndarray
    diagnostics: dict
    seal: str

def candidate_seal(candidate):
    h=hashlib.sha256()
    h.update(candidate.token.encode());h.update(candidate.old_plan_id.encode());h.update(str(candidate.revision).encode())
    for s in candidate.plan.pieces:
        h.update(np.asarray([s.start,s.end],np.int64).tobytes());h.update(s.curve.times.tobytes());h.update(s.curve.q.tobytes())
    h.update(candidate.raw_h50.tobytes());h.update(candidate.temporal_tail.tobytes());return h.hexdigest()

class ServerTemporalAdapter:
    """Pure server-role preparation; existing model request/response remains H50."""
    def __init__(self,bounds):
        self.bounds=array(bounds,(7,2));require(np.all(self.bounds[:,0]<self.bounds[:,1]),'invalid position bounds')
        self.optimizer=official_temporal()(dt_ref=1/HZ,dt_min=(1/HZ)*(.010/.018),dt_max=(1/HZ)*(.020/.018),
            lambda_acc=10.,lambda_time=1.,stride=50,optim_dims=list(range(7)),v_max=None,horizon=50,logging=False)
    def prepare(self,request,raw_h50,*,evidence_kind):
        require(evidence_kind in ('SYNTHETIC_CPU_FIXTURE','STORED_HISTORICAL_H50'),'this prototype cannot certify live model provenance')
        began=time.perf_counter_ns();raw=array(raw_h50,(HORIZON,7));anchor=request.cut_anchor
        require(np.all(anchor>=self.bounds[:,0]) and np.all(anchor<=self.bounds[:,1]),'accepted anchor outside bounds; do not clip state')
        # known35 is a guidance range, NOT35 immutable/cropped output points.
        # Keep the cut boundary at index12. Only model rows13..49 follow it.
        projected=np.clip(raw[FREEZE+1:],self.bounds[:,0],self.bounds[:,1])
        optimizer_input=np.vstack((anchor,projected))  #38points: accepted anchor +37future points.
        output=np.asarray(self.optimizer.optimize(optimizer_input.tolist()),float)
        require(output.shape==(HORIZON-FREEZE,7) and np.isfinite(output).all(),'official temporal output layout invalid')
        bounded=np.clip(output,self.bounds[:,0],self.bounds[:,1]);boundary_rounding=bounded[0]-anchor
        bounded[0]=anchor  # Preserve inherited boundary exactly across float32 conversion.
        times=np.array([request.observation_phase_ns+label_ns(i) for i in range(FREEZE,HORIZON+1)],np.int64)
        # Final interval49..50 is the existing declared H50 endpoint hold, not
        # an extension used to hide known35/queue coverage failure.
        curve=Curve(times,np.vstack((bounded,bounded[-1])))
        history_start=max(request.old_plan.start,request.observation_phase_ns-200_000_000)
        prefix=request.old_plan.prefix(history_start,request.cut_phase_ns)
        plan=Plan(prefix+(Slice(request.cut_phase_ns,int(times[-1]),curve),),uuid.uuid4().hex)
        diagnostics={'evidence_kind':evidence_kind,'live_model_validation':False,'model_response_shape':[50,7],
            'known_context_points':0 if request.index==0 else 35,'frozen_intervals':12,'cut_anchor_label':12,
            'mutable_model_indices':[13,49],'mutable_model_rows':37,'official_optimizer_input_rows':38,
            'boundary_model_delta_rad_m':(raw[12]-anchor).tolist(),
            'boundary_float32_rounding_rad_m':boundary_rounding.tolist(),
            'model_target_projection_max_rad_m':np.max(abs(projected-raw[13:]),axis=0).tolist(),
            'output_projection_max_rad_m':np.max(abs(bounded-output),axis=0).tolist(),
            'cut_reference_velocity_jump_rad_m_s':(curve.velocity(request.cut_phase_ns)-request.old_plan.velocity(request.cut_phase_ns,'left')).tolist(),
            'q_continuous_at_cut':True,'velocity_continuous_at_cut':False,
            'terminal_hold_interval_labels':[49,50],'reference_layer':'PRE_MPC_DESIRED_REFERENCE',
            'official_acados_executed':False,'prepare_ms':(time.perf_counter_ns()-began)/1e6}
        fields=dict(token=request.token,old_plan_id=request.old_plan.plan_id,revision=request.revision,clock_epoch=request.clock_epoch,
            plan=plan,raw_h50=raw,temporal_tail=array(bounded,(38,7)),diagnostics=diagnostics,seal='')
        first=Candidate(**fields);fields['seal']=candidate_seal(first);return Candidate(**fields)

class ClientTimeQueue:
    def __init__(self,anchor,bounds,start_ns,*,trial_index,clock_domain,mode='CPU_OFFLINE'):
        require(mode=='CPU_OFFLINE','no live execution mode is implemented')
        require(type(trial_index) is int and 0<=trial_index<=1_000_000,'valid trial index required')
        require(isinstance(clock_domain,tuple) and len(clock_domain)==3 and all(isinstance(v,str) and v for v in clock_domain),'host/boot/clock identity required')
        self.bounds=array(bounds,(7,2));require(np.all(self.bounds[:,0]<self.bounds[:,1]),'invalid position bounds')
        q=array(anchor,(7,));require(np.all(q>=self.bounds[:,0]) and np.all(q<=self.bounds[:,1]),'initial state is outside limits; no reset/clip')
        c=Curve(np.array([0,label_ns(HORIZON)]),np.vstack((q,q)))
        self.plan=Plan((Slice(0,label_ns(HORIZON),c),),uuid.uuid4().hex);self.clock=ReferenceClock(start_ns)
        self.domain=clock_domain;self.trial=trial_index;self.revision=0;self.index=-1;self.last_observation_ns=None
        self.pending=None;self.draining=None;self.status='RUNNING';self.lock=threading.RLock();self.events=deque(maxlen=256)
        self.validate_rtc=original_rtc_validator().validate_rtc
    def request(self,observation_ns,now_ns,*,clock_domain):
        with self.lock:
            require(self.status=='RUNNING' and not self.clock.paused,'no requests during pause/stop/fault')
            require(clock_domain==self.domain,'clock-domain mismatch')
            require(self.pending is None and self.draining is None,'one inference may be in flight; cancelled response must drain')
            require(0<=stamp(now_ns)-stamp(observation_ns)<=200_000_000,'stale or future observation')
            require(self.clock.last_tick_ns is None or now_ns>=self.clock.last_tick_ns,'request clock went backwards')
            require(self.last_observation_ns is None or observation_ns>self.last_observation_ns,'observation must advance')
            phase=self.clock.phase_at(observation_ns);cut=phase+label_ns(FREEZE)
            self.plan.sample(cut);index=self.index+1
            require(index==0 or self.revision>0,'no implicit bootstrap retry')
            previous=() if index==0 else tuple(tuple(float(x) for x in row) for row in self.plan.sample(
                np.array([phase+label_ns(i) for i in range(KNOWN)],np.int64)))
            context=Request(uuid.uuid4().hex,self.plan,self.revision,self.clock.epoch,observation_ns,phase,cut,
                observation_ns+DEADLINE_NS,observation_ns+label_ns(FREEZE),self.trial,index,previous,self.domain)
            self.validate_rtc(context.wire())
            self.index=index;self.last_observation_ns=observation_ns;self.pending=context
            self.events.append({'event':'REQUEST','index':index,'observation_ns':observation_ns,'reference_layer':'PRE_MPC_DESIRED_REFERENCE'})
            return context
    def commit(self,context,candidate,now_ns):
        with self.lock:
            require(self.status=='RUNNING' and not self.clock.paused,'queue is paused/stopped/faulted')
            require(self.pending is context and context.token==candidate.token,'response token/replay mismatch')
            require(self.revision==context.revision==candidate.revision and self.plan is context.old_plan and candidate.old_plan_id==self.plan.plan_id,'stale accepted plan revision')
            require(self.clock.epoch==context.clock_epoch==candidate.clock_epoch,'clock epoch changed')
            require(context.observation_ns<=stamp(now_ns)<=context.deadline_ns and now_ns<context.cut_wall_ns,'response missed600ms deadline/frozen cut')
            require(self.clock.last_tick_ns is None or now_ns>=self.clock.last_tick_ns,'commit cannot be backdated before a sampled tick')
            require(candidate.seal==candidate_seal(candidate),'candidate mutated after prepare')
            require(candidate.plan.end==context.observation_phase_ns+label_ns(HORIZON),'H50 end-time changed')
            expected=self.plan.prefix(candidate.plan.start,context.cut_phase_ns);actual=candidate.plan.pieces[:-1]
            require(len(expected)==len(actual) and all(a.start==b.start and a.end==b.end and a.curve is b.curve for a,b in zip(expected,actual)),'continuous frozen prefix replaced')
            require(np.array_equal(candidate.plan.sample(context.cut_phase_ns),self.plan.sample(context.cut_phase_ns)),'cut anchor changed')
            self.plan=candidate.plan;self.pending=None;self.revision+=1
            self.events.append({'event':'COMMIT','index':context.index,'revision':self.revision,'now_ns':now_ns})
    def sample_for_mpc(self,wall_ns,*,horizon=15):
        with self.lock:
            require(self.status in ('RUNNING','PAUSED'),'no new targets after stop/fault')
            require(type(horizon) is int and horizon==15,'current official MPC horizon must remain15')
            try:phase,index,fraction=self.clock.tick(wall_ns)
            except StopRequired:
                self.abort('CONTROL_CLOCK_GAP');raise
            if self.status=='RUNNING' and self.plan.end-phase<=LOW_WATER_NS:
                self.status='STOP_REQUIRED';self.draining=self.pending;self.pending=None;raise StopRequired('reference low water; physical braking is not implemented here')
            future_times=np.full(horizon,phase,np.int64) if self.clock.paused else phase+np.arange(horizon,dtype=np.int64)*SEND_NS
            future=self.plan.sample(future_times)
            return {'reference_rad_m':future.copy(),'reference_phase_ns':int(phase),'completed_reference_labels':int(index),
                'fractional_label_progress':fraction,'control_period_ns':SEND_NS,'reference_period_ratio':[NS,HZ],
                'reference_layer':'PRE_MPC_DESIRED_REFERENCE','physical_dispatch_performed':False,'clock_epoch':self.clock.epoch}
    def pause(self,wall_ns):
        with self.lock:
            require(self.status=='RUNNING','cannot pause current state');self.plan.sample(self.clock.phase_at(wall_ns))
            require(self.clock.last_tick_ns is None or wall_ns>=self.clock.last_tick_ns,'pause clock went backwards')
            self.clock.pause(wall_ns);self.draining=self.pending;self.pending=None;self.status='PAUSED'
            self.events.append({'event':'PAUSE_REFERENCE_PROGRESS','physical_stop_validated':False})
    def resume(self,wall_ns):
        with self.lock:
            require(self.status=='PAUSED','not paused');self.clock.resume(wall_ns);self.status='RUNNING'
            self.events.append({'event':'RESUME_WITHOUT_STATE_RESET','clock_epoch':self.clock.epoch})
    def apply_mpc_alpha(self,alpha,wall_ns):
        require(type(alpha) in (int,float) and alpha in (0,1),'only official pause/run alpha0/1 is supported')
        if alpha==0 and self.status=='RUNNING':self.pause(wall_ns)
        elif alpha==1 and self.status=='PAUSED':self.resume(wall_ns)
        else:require(self.status in ('RUNNING','PAUSED'),'cannot change progress after fault/stop')
    def abort(self,reason):
        with self.lock:self.status='FAULTED';self.draining=self.pending or self.draining;self.pending=None;self.events.append({'event':'FAULT','reason':str(reason)})
    def acknowledge_response_drained(self,context,*,evidence_kind):
        require(evidence_kind in ('SYNTHETIC_CPU_FIXTURE','STORED_HISTORICAL_H50'),'CPU replay drain evidence only')
        with self.lock:
            require(self.draining is context,'drain acknowledgement belongs to another request')
            self.draining=None;self.events.append({'event':'OFFLINE_RESPONSE_DRAINED','index':context.index,'live_io_verified':False})
    def expire(self,now_ns):
        with self.lock:
            if self.pending is not None and stamp(now_ns)>self.pending.deadline_ns:
                self.abort('MODEL_DEADLINE_EXPIRED');raise StopRequired('inference expired; no automatic bootstrap retry')
