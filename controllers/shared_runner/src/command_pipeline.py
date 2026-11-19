"""Fixed-clock, cached command smoothing. No network, model, SDK or motion writer.

The candidate carries analytic joint q/v/a through startup and packet seams.
It is a software reference filter, not identified robot dynamics or feedback MPC.
"""
from dataclasses import dataclass
import math
import threading
import time
import uuid
import numpy as np
from .acceleration_filter import generate_filtered_tail

NS = 1_000_000_000
HZ, HORIZON, FREEZE, KNOWN = 18, 50, 12, 35
PERIOD_NS, DEADLINE_NS, HISTORY_NS = 20_000_000, 600_000_000, 200_000_000


def label_ns(index):
    return (int(index)*NS + HZ//2)//HZ


def array(value, shape):
    a = np.array(value, dtype=np.float64, copy=True)
    if a.shape != shape or not np.isfinite(a).all():
        raise ValueError('finite array with explicit physical shape required')
    return np.frombuffer(a.tobytes(),dtype=np.float64).reshape(a.shape)


def clock(value):
    if type(value) is not int or value <= 0:
        raise ValueError('positive integer monotonic nanosecond clock required')
    return value


@dataclass(frozen=True)
class LinearCurve:
    times_ns: tuple
    values: np.ndarray

    def __post_init__(self):
        ts = tuple(clock(x) for x in self.times_ns)
        if len(ts)<2 or any(a>=b for a,b in zip(ts,ts[1:])):
            raise ValueError('strictly increasing fixed timestamps required')
        object.__setattr__(self,'times_ns',ts)
        object.__setattr__(self,'values',array(self.values,(len(ts),7)))

    def sample(self, at):
        clock(at)
        if not self.times_ns[0] <= at <= self.times_ns[-1]:
            raise ValueError('reference exhausted; no implicit extrapolation')
        i = min(int(np.searchsorted(self.times_ns,at,side='right'))-1,len(self.times_ns)-2)
        f = (at-self.times_ns[i])/(self.times_ns[i+1]-self.times_ns[i])
        return self.values[i]*(1-f)+self.values[i+1]*f

    def state(self,at,side='left'):
        if side not in ('left','right'):raise ValueError('invalid state side')
        q=self.sample(at)
        i=max(0,min(int(np.searchsorted(self.times_ns,at,side=side))-1,len(self.times_ns)-2))
        v=(self.values[i+1]-self.values[i])/((self.times_ns[i+1]-self.times_ns[i])/NS)
        return {'q':q,'v':v,'a':np.zeros(7),'j':np.zeros(7)}


@dataclass(frozen=True)
class CubicCurve:
    """Immutable saved-reference adapter; units are rad6+m throughout."""
    origin_ns: int
    end_ns: int
    relative_times_s: tuple
    q0: np.ndarray
    v0: np.ndarray
    a0: np.ndarray
    jerk: np.ndarray
    terminal_q: np.ndarray

    def __post_init__(self):
        clock(self.origin_ns);clock(self.end_ns)
        ts=tuple(float(x) for x in self.relative_times_s)
        if len(ts)<2 or ts[0]!=0 or not all(math.isfinite(x) for x in ts) or any(a>=b for a,b in zip(ts,ts[1:])):
            raise ValueError('invalid cubic source clock')
        if abs(ts[-1]-(self.end_ns-self.origin_ns)/NS)>1e-8: raise ValueError('cubic endpoint clock mismatch')
        object.__setattr__(self,'relative_times_s',ts)
        for name in ('q0','v0','a0','jerk'):object.__setattr__(self,name,array(getattr(self,name),(len(ts)-1,7)))
        object.__setattr__(self,'terminal_q',array(self.terminal_q,(7,)))
        lengths=np.diff(ts)[:,None]
        ends=self.q0+lengths*(self.v0+lengths*(self.a0/2+lengths*self.jerk/6))
        expected=np.vstack([self.q0[1:],self.terminal_q])
        if not np.isfinite(ends).all() or np.max(abs(ends-expected))>1e-10:
            raise ValueError('saved cubic position discontinuity; no state repair')

    def sample(self,at):
        if not self.origin_ns<=clock(at)<=self.end_ns:raise ValueError('cubic reference exhausted')
        if at==self.end_ns:return self.terminal_q.copy()
        t=(at-self.origin_ns)/NS
        i=min(int(np.searchsorted(self.relative_times_s,t,side='right'))-1,len(self.relative_times_s)-2)
        h=t-self.relative_times_s[i]
        return self.q0[i]+h*(self.v0[i]+h*(self.a0[i]/2+h*self.jerk[i]/6))

    def state(self,at,side='left'):
        if side not in ('left','right'):raise ValueError('invalid state side')
        q=self.sample(at)
        t=self.relative_times_s[-1] if at==self.end_ns else (at-self.origin_ns)/NS
        i=max(0,min(int(np.searchsorted(self.relative_times_s,t,side=side))-1,len(self.relative_times_s)-2))
        h=t-self.relative_times_s[i]
        return {'q':q,'v':self.v0[i]+h*(self.a0[i]+.5*h*self.jerk[i]),
                'a':self.a0[i]+h*self.jerk[i],'j':self.jerk[i].copy()}

    def envelope(self):
        low=np.minimum(self.q0.min(axis=0),self.terminal_q)
        high=np.maximum(self.q0.max(axis=0),self.terminal_q)
        for i,(left,right) in enumerate(zip(self.relative_times_s,self.relative_times_s[1:])):
            h=right-left
            end=self.q0[i]+h*(self.v0[i]+h*(self.a0[i]/2+h*self.jerk[i]/6))
            low=np.minimum(low,end);high=np.maximum(high,end)
            for axis in range(7):
                roots=np.polynomial.polynomial.polyroots([self.v0[i,axis],self.a0[i,axis],self.jerk[i,axis]/2])
                for z in roots:
                    if abs(z.imag)<1e-12 and 0<z.real<h:
                        u=float(z.real);q=self.q0[i,axis]+u*(self.v0[i,axis]+u*(self.a0[i,axis]/2+u*self.jerk[i,axis]/6))
                        low[axis]=min(low[axis],q);high[axis]=max(high[axis],q)
        return low,high


@dataclass(frozen=True)
class NativeGovernorCurve:
    """Preserve the existing governor's absolute-time scalar arithmetic.

    This avoids numerical branch changes from re-evaluating the gripper under a
    different local-time polynomial order. No historical values are injected.
    """
    times_s: tuple
    q_deg_m: np.ndarray
    v_deg_m_s: np.ndarray
    envelope_curve: CubicCurve

    def __post_init__(self):
        object.__setattr__(self,'times_s',tuple(float(x) for x in self.times_s))
        object.__setattr__(self,'q_deg_m',array(self.q_deg_m,(len(self.times_s),7)))
        object.__setattr__(self,'v_deg_m_s',array(self.v_deg_m_s,(len(self.times_s),7)))

    def state(self,at,side='left'):
        clock(at)
        if not self.envelope_curve.origin_ns<=at<=self.envelope_curve.end_ns:raise ValueError('native reference exhausted')
        if side not in ('left','right'):raise ValueError('invalid side')
        t=at/NS;i=max(0,min(int(np.searchsorted(self.times_s,t,side=side))-1,len(self.times_s)-2))
        dt=self.times_s[i+1]-self.times_s[i];elapsed=max(0.,t-self.times_s[i])
        av=[(float(b)-float(a))/dt for a,b in zip(self.v_deg_m_s[i],self.v_deg_m_s[i+1])]
        q=[float(q)+float(v)*elapsed+.5*a*elapsed**2 for q,v,a in zip(self.q_deg_m[i],self.v_deg_m_s[i],av)]
        v=[float(v)+a*elapsed for v,a in zip(self.v_deg_m_s[i],av)]
        if at==self.envelope_curve.end_ns:q=self.q_deg_m[-1].tolist();v=self.v_deg_m_s[-1].tolist()
        scale=np.asarray([np.pi/180]*6+[1.])
        return {'q':np.asarray(q)*scale,'v':np.asarray(v)*scale,'a':np.asarray(av)*scale,'j':np.zeros(7)}

    def sample(self,at):return self.state(at,'right')['q']
    def envelope(self):return self.envelope_curve.envelope()


@dataclass(frozen=True)
class Span:
    start_ns: int
    end_ns: int
    curve: object

    def __post_init__(self):
        if type(self.curve) not in (LinearCurve,CubicCurve,SmoothedCurve,NativeGovernorCurve):
            raise ValueError('only supported immutable reference curves are accepted')


@dataclass(frozen=True)
class Plan:
    spans: tuple
    plan_id: str = ''

    def __post_init__(self):
        spans = tuple(self.spans)
        if not spans or any(clock(s.start_ns)>=clock(s.end_ns) for s in spans):
            raise ValueError('nonempty reference spans required')
        if any(a.end_ns!=b.start_ns for a,b in zip(spans,spans[1:])):
            raise ValueError('reference spans must share exact boundaries')
        for left,right in zip(spans,spans[1:]):
            if np.max(abs(left.curve.sample(left.end_ns)-right.curve.sample(right.start_ns)))>1e-10:
                raise ValueError('reference position discontinuity between spans')
        object.__setattr__(self,'spans',spans)
        object.__setattr__(self,'plan_id',self.plan_id or uuid.uuid4().hex)

    @property
    def start_ns(self): return self.spans[0].start_ns
    @property
    def end_ns(self): return self.spans[-1].end_ns

    def sample(self, at):
        clock(at)
        if not self.start_ns<=at<=self.end_ns:
            raise ValueError('reference exhausted; no implicit HOLD')
        index = min(int(np.searchsorted([s.end_ns for s in self.spans],at,side='right')),len(self.spans)-1)
        return np.array(self.spans[index].curve.sample(at),dtype=float,copy=True)

    def state(self,at,side='left'):
        clock(at)
        if side not in ('left','right') or not self.start_ns<=at<=self.end_ns:
            raise ValueError('state outside accepted reference or invalid side')
        index=min(int(np.searchsorted([s.end_ns for s in self.spans],at,side=side)),len(self.spans)-1)
        return self.spans[index].curve.state(at,side)

    def view(self, start, end):
        if not self.start_ns<=start<end<=self.end_ns:
            raise ValueError('slice must be covered by existing plan')
        # Keep original curve objects. Never resample the protected prefix.
        return tuple(Span(max(start,s.start_ns),min(end,s.end_ns),s.curve)
                     for s in self.spans if s.end_ns>start and s.start_ns<end)


@dataclass(frozen=True)
class AnchoredReference:
    source: Plan
    cut_ns: int
    anchor: np.ndarray
    bridge_end_ns: int

    def __post_init__(self):
        object.__setattr__(self,'anchor',array(self.anchor,(7,)))

    def sample(self, at):
        if at<self.cut_ns: raise ValueError('tail queried before cut')
        if at<=self.bridge_end_ns:
            f=(at-self.cut_ns)/(self.bridge_end_ns-self.cut_ns)
            return self.anchor*(1-f)+self.source.sample(self.bridge_end_ns)*f
        return self.source.sample(at)

    def state(self,at,side='left'):
        if at<=self.bridge_end_ns:
            q=self.sample(at);v=(self.source.sample(self.bridge_end_ns)-self.anchor)/((self.bridge_end_ns-self.cut_ns)/NS)
            return {'q':q,'v':v,'a':np.zeros(7),'j':np.zeros(7)}
        return self.source.state(at,side)


@dataclass(frozen=True)
class DirectReference:
    """Already q/v-continuous upstream governor; no 20ms catch-up bridge."""
    source: object
    cut_ns: int
    anchor: np.ndarray
    anchor_velocity: np.ndarray

    def __post_init__(self):
        from .gripper_reference import ScalarGripperCurve
        if type(self.source) not in (Plan,ScalarGripperCurve):raise ValueError('immutable gripper source required')
        object.__setattr__(self,'anchor',array(self.anchor,(7,)))
        object.__setattr__(self,'anchor_velocity',array(self.anchor_velocity,(7,)))
        current=self.gripper_state(self.cut_ns,side='right',anchor_exact=False)
        if abs(current['q']-self.anchor[6])>1e-10 or abs(current['v']-self.anchor_velocity[6])>1e-9:
            raise ValueError('gripper source must continue accepted q/v; no catch-up bridge')

    def gripper_state(self,at,side='left',*,anchor_exact=True):
        if at<self.cut_ns:raise ValueError('source queried before protected cut')
        value=self.source.state(at,side)
        if type(self.source) is Plan:value={key:float(x[6]) for key,x in value.items()}
        if at==self.cut_ns and anchor_exact:
            value['q']=self.anchor[6];value['v']=self.anchor_velocity[6]
        return value

@dataclass(frozen=True)
class SmoothedCurve:
    joints: object
    gripper_reference: object

    def __post_init__(self):
        from .acceleration_filter import AccelerationFilteredCurve
        if type(self.joints) is not AccelerationFilteredCurve or type(self.gripper_reference) is not DirectReference:
            raise ValueError('immutable supported joint and gripper curves required')

    def sample(self, at):
        return np.r_[self.joints.sample(at)[:6],self.gripper_reference.gripper_state(at,'right')['q']]

    def state(self,at,side='left'):
        result=self.joints.state(at,side);g=self.gripper_reference.gripper_state(at,side)
        for key in ('q','v','a','j'):result[key]=np.r_[result[key][:6],g[key]]
        return result


def validate_plan_bounds(plan,bounds):
    """Position-only check. No velocity/acceleration/jerk gates are introduced."""
    seen=set()
    def check(p):
        for span in p.spans:
            c=span.curve
            if id(c) in seen:continue
            seen.add(id(c))
            if type(c) is LinearCurve:
                low,high=c.values.min(axis=0),c.values.max(axis=0)
            elif type(c) in (CubicCurve,NativeGovernorCurve):
                low,high=c.envelope()
            elif type(c) is SmoothedCurve:
                low,high=c.joints.envelope()
                low=np.r_[low[:6],bounds[6,0]];high=np.r_[high[:6],bounds[6,1]]
                source=c.gripper_reference.source
                if type(source) is Plan:check(source)
                else:
                    gl,gh=source.envelope()
                    if gl<bounds[6,0]-1e-10 or gh>bounds[6,1]+1e-10:raise ValueError('gripper source exceeds position bounds')
            else:raise ValueError('mutable or unsupported source curve')
            if np.any(low<bounds[:,0]-1e-10) or np.any(high>bounds[:,1]+1e-10):
                raise ValueError('source position envelope exceeds existing bounds')
    check(plan)


@dataclass(frozen=True)
class Context:
    nonce: str
    generation: int
    old_plan: Plan
    observation_ns: int
    cut_ns: int
    previous_actions: tuple
    bootstrap: bool
    request_index: int
    trial_index: int

    def wire(self):
        return {'schema':'tiger-fp32-h50-rtc-v1','enabled':not self.bootstrap,
            'previous_actions':[[float(x) for x in q] for q in self.previous_actions],
            'previous_valid_steps':0 if self.bootstrap else KNOWN,
            'freeze_steps':FREEZE,'execution_label_hz':HZ,'native_training_hz':30,
            'beta':5.0,'trial_index':self.trial_index,'request_index':self.request_index}


@dataclass(frozen=True)
class Candidate:
    nonce: str
    generation: int
    plan: Plan
    anchored_reference: DirectReference
    diagnostics: dict


@dataclass(frozen=True)
class StopCandidate:
    nonce: str
    generation: int
    old_plan: Plan
    start_ns: int
    plan: Plan
    diagnostics: dict


class SmoothingQueue:
    """Single-owner prepare/commit contract; execution samples accepted cache only."""
    def __init__(self,initial_plan,bounds_rad_m,*,trial_index=0):
        from .command_limits import CommandLimiter
        self.command_limiter=CommandLimiter(bounds_rad_m)
        self.bounds=self.command_limiter.bounds_rad_m
        if np.any(self.bounds[:,0]>=self.bounds[:,1]): raise ValueError('invalid bounds')
        validate_plan_bounds(initial_plan,self.bounds)
        self.plan=initial_plan
        self.generation=0
        self.phase='READY'
        self.failure=None
        self.pending=None
        self._prepared=None
        self._prepared_stop=None
        self.request_index=-1
        self.last_observation_ns=0
        if type(trial_index) is not int or trial_index<0: raise ValueError('nonnegative trial identity required')
        self.trial_index=trial_index
        self._lock=threading.RLock()

    def request(self,observation_ns,*,bootstrap=False):
        clock(observation_ns)
        with self._lock:
            if self.phase=='ABORTED': raise RuntimeError('queue aborted')
            if self.phase in ('STOPPING_REFERENCE','REFERENCE_STOPPED'):raise RuntimeError('normal stop forbids new predictions')
            if self.pending is not None: raise RuntimeError('single request already in flight')
            if observation_ns<=self.last_observation_ns: raise ValueError('observation clock must advance')
            if self.request_index==-1 and not bootstrap:
                raise ValueError('first request must explicitly use native bootstrap')
            if bootstrap and (self.generation!=0 or self.request_index!=-1):
                raise ValueError('bootstrap cannot reset a continued execution')
            cut=observation_ns+label_ns(FREEZE)
            self.plan.sample(cut-PERIOD_NS)
            self.plan.sample(cut)
            previous=() if bootstrap else tuple(tuple(self.plan.sample(observation_ns+label_ns(i))) for i in range(KNOWN))
            self.request_index+=1
            ctx=Context(uuid.uuid4().hex,self.generation,self.plan,observation_ns,cut,
                        previous,bool(bootstrap),self.request_index,self.trial_index)
            self.pending=ctx
            self._prepared=None
            self.last_observation_ns=observation_ns
            return ctx

    def prepare_h50(self,ctx,actions_rad_m):
        raw=array(actions_rad_m,(HORIZON,7))
        projected=raw.copy()
        projected[FREEZE:]=np.minimum(np.maximum(projected[FREEZE:],self.bounds[:,0]),self.bounds[:,1])
        from .gripper_reference import generate_gripper_tail
        initial=ctx.old_plan.state(ctx.cut_ns,side='left')
        plan=generate_gripper_tail(raw,ctx.observation_ns,ctx.cut_ns,float(initial['q'][6]),float(initial['v'][6]),self.bounds[6])
        def target_at(at):
            index=min(49.,max(0.,(at-ctx.observation_ns)/NS*HZ))
            lo=int(index);hi=min(49,lo+1);f=index-lo
            return projected[lo,:6]+(projected[hi,:6]-projected[lo,:6])*f
        candidate=self.prepare_reference(ctx,plan,joint_target_at=target_at)
        candidate.diagnostics['command_projection_count']=int(np.count_nonzero(projected[FREEZE:]!=raw[FREEZE:]))
        candidate.diagnostics['raw_model_actions']=raw.tolist()
        candidate.diagnostics['input_kind']='RAW_H50_INTERFACE_NOT_A_HISTORICAL_DATA_CLAIM'
        candidate.diagnostics['projected_model_actions']=projected.tolist()
        candidate.diagnostics['upstream_reference']='JOINT_GOVERNOR_WITH_ACCEL_FILTER_OWN_Q_V_A_GRIPPER_ORIGINAL_GOVERNOR'
        return candidate

    def prepare_reference(self,ctx,source,*,joint_target_at=None):
        start=time.perf_counter_ns()
        end=ctx.observation_ns+label_ns(HORIZON)
        if source.end_ns!=end or source.start_ns>ctx.cut_ns:
            raise ValueError('H50 end and source coverage must preserve original clock')
        if ctx.generation!=self.generation or ctx.old_plan is not self.plan:
            raise ValueError('stale context')
        if self.pending is not ctx:raise ValueError('context not owned by this queue')
        if type(source) is Plan:validate_plan_bounds(source,self.bounds)
        else:
            lo,hi=source.envelope()
            if lo<self.bounds[6,0]-1e-10 or hi>self.bounds[6,1]+1e-10:raise ValueError('gripper envelope outside bounds')
            if joint_target_at is None:raise ValueError('six-joint target required with scalar gripper source')
        old_state=ctx.old_plan.state(ctx.cut_ns,side='left')
        anchor=old_state['q']
        previous=ctx.old_plan.sample(ctx.cut_ns-PERIOD_NS)
        times=tuple(range(ctx.cut_ns,end,PERIOD_NS))+(end,)
        target=DirectReference(source,ctx.cut_ns,anchor,old_state['v'])
        joint_target_at=joint_target_at or (lambda at:source.sample(at)[:6])
        reference=np.asarray([joint_target_at(t) for t in times])
        if not np.isfinite(reference).all() or np.any(reference<self.bounds[:6,0]-1e-12) or np.any(reference>self.bounds[:6,1]+1e-12):
            raise ValueError('reference exceeds unchanged position bounds')
        joints=generate_filtered_tail(ctx.cut_ns,end,anchor[:6],old_state['v'][:6],old_state['a'][:6],
                                      joint_target_at,self.bounds[:6])
        tail=SmoothedCurve(joints,target)
        keep_start=max(ctx.old_plan.start_ns,ctx.observation_ns-HISTORY_NS)
        spans=ctx.old_plan.view(keep_start,ctx.cut_ns)+(Span(ctx.cut_ns,end,tail),)
        plan=Plan(spans)
        diag=dict(joints.diagnostics)
        diag.update({'input_kind':'EXPLICIT_REFERENCE_NOT_RAW_H50','prepare_ms':(time.perf_counter_ns()-start)/1e6,
                     'cut_ns':ctx.cut_ns,'end_ns':end,'time_compression':False,'gripper_smoothing':False,
                     'join_guarantee':'JOINT_Q_V_A_CONTINUOUS_GRIPPER_Q_V_CONTINUOUS_NO_A_J_HARD_BOUND',
                     'anchor_previous_rad_m':previous.tolist(),'anchor_rad_m':anchor.tolist(),
                     'target_anchor_difference_rad_m':(np.r_[reference[0],target.gripper_state(ctx.cut_ns)['q']]-anchor).tolist(),
                     'anchor_velocity_rad_m_s':old_state['v'].tolist(),
                     'gripper_anchor_bridge_ns':0,
                     'terminal_joint_error_rad':(joints.terminal_q-reference[-1]).tolist()})
        candidate=Candidate(ctx.nonce,ctx.generation,plan,target,diag)
        with self._lock:
            if self.pending is not ctx or self.generation!=ctx.generation:raise ValueError('context changed during preparation')
            self._prepared=candidate
        return candidate

    def commit(self,ctx,candidate,ready_ns):
        clock(ready_ns)
        with self._lock:
            if self.phase=='ABORTED': raise RuntimeError('queue aborted')
            if self.pending is not ctx or self.generation!=ctx.generation or self.plan is not ctx.old_plan:
                raise ValueError('stale or foreign context; current plan unchanged')
            if candidate.nonce!=ctx.nonce or candidate.generation!=ctx.generation:
                raise ValueError('candidate belongs to another request')
            if candidate is not self._prepared:
                raise ValueError('candidate was not prepared by this queue')
            if ready_ns<ctx.observation_ns: raise ValueError('ready precedes observation')
            if ready_ns>ctx.observation_ns+DEADLINE_NS:
                self.abort('COMMIT_DEADLINE_EXCEEDED')
                raise RuntimeError('commit deadline exceeded; no replacement installed')
            if candidate.plan.end_ns!=ctx.observation_ns+label_ns(HORIZON): raise ValueError('changed execution horizon')
            expected=ctx.old_plan.view(candidate.plan.start_ns,ctx.cut_ns)
            prefix=candidate.plan.spans[:-1]
            if len(prefix)!=len(expected) or any(a.start_ns!=b.start_ns or a.end_ns!=b.end_ns or a.curve is not b.curve for a,b in zip(prefix,expected)):
                raise ValueError('protected prefix was replaced')
            if not np.array_equal(candidate.plan.sample(ctx.cut_ns),ctx.old_plan.sample(ctx.cut_ns)):
                raise ValueError('position discontinuity at protected cut')
            if np.max(abs(candidate.plan.state(ctx.cut_ns,'right')['v']-ctx.old_plan.state(ctx.cut_ns,'left')['v']))>1e-9:
                raise ValueError('velocity discontinuity at protected cut')
            if np.max(abs(candidate.plan.state(ctx.cut_ns,'right')['a'][:6]-ctx.old_plan.state(ctx.cut_ns,'left')['a'][:6]))>1e-8:
                raise ValueError('joint acceleration discontinuity at protected cut')
            self.plan=candidate.plan
            self.generation+=1
            self.pending=None
            self._prepared=None
            self.phase='ACTIVE'
            return self.plan

    def prepare_normal_stop(self,start_ns,deceleration_rad_m_s2):
        from .reference_stop import build_normal_stop
        clock(start_ns)
        with self._lock:
            if self.phase not in ('READY','ACTIVE'):raise RuntimeError('queue cannot prepare a normal stop')
            old=self.plan;generation=self.generation
            state=old.state(start_ns,'left')
        result=build_normal_stop(start_ns,state['q'],state['v'],self.bounds,deceleration_rad_m_s2,
                                 command_limiter=self.command_limiter)
        # A future stop may be prepared well before its start. Keep all currently
        # covered prefix, otherwise installing it could create a hole at ready_ns.
        keep=old.start_ns
        prefix=old.view(keep,start_ns) if keep<start_ns else ()
        stopped=Plan(prefix+(Span(start_ns,result.curve.end_ns,result.curve),))
        candidate=StopCandidate(uuid.uuid4().hex,generation,old,start_ns,stopped,result.diagnostics)
        with self._lock:
            if self.phase not in ('READY','ACTIVE') or self.generation!=generation or self.plan is not old:
                raise ValueError('accepted reference changed while stop was prepared')
            self._prepared_stop=candidate
        return candidate

    def commit_normal_stop(self,candidate,ready_ns):
        clock(ready_ns)
        with self._lock:
            if self.phase not in ('READY','ACTIVE'):raise RuntimeError('normal stop cannot override fault or previous stop')
            if candidate is not self._prepared_stop or candidate.generation!=self.generation or candidate.old_plan is not self.plan:
                raise ValueError('stale or foreign normal-stop candidate')
            if not candidate.plan.start_ns<=ready_ns<=candidate.start_ns:
                raise ValueError('stop commit must be covered and cannot rewrite elapsed reference')
            left=self.plan.state(candidate.start_ns,'left');right=candidate.plan.state(candidate.start_ns,'right')
            if not np.array_equal(left['q'],right['q']) or not np.array_equal(left['v'],right['v']):
                raise ValueError('stop must preserve actual accepted q/v exactly')
            self.plan=candidate.plan;self.generation+=1
            self.pending=None;self._prepared=None;self._prepared_stop=None
            self.phase='STOPPING_REFERENCE'
            return self.plan

    def abort(self,reason):
        with self._lock:
            self.phase='ABORTED'
            self.failure=str(reason)
            self.pending=None
            self._prepared=None
            self._prepared_stop=None

    def sample(self,at):
        """Continuous accepted reference, also used by the unchanged RTC contract.

        A downstream command consumer must use sample_command, which applies
        the final finite/range/SDK-integer boundary. This API is not feedback.
        """
        with self._lock:
            if self.phase=='ABORTED': raise RuntimeError('aborted: no further command samples')
            try:
                clock(at)
                terminal_hold=self.phase in ('STOPPING_REFERENCE','REFERENCE_STOPPED') and at>=self.plan.end_ns
                q=self.plan.sample(self.plan.end_ns if terminal_hold else at)
                allowance=self.command_limiter.reference_numerical_allowance_rad_m
                excess=np.maximum(np.maximum(self.bounds[:,0]-q,q-self.bounds[:,1]),0.)
                if q.shape!=(7,) or not np.isfinite(q).all() or np.any(excess>allowance):
                    raise ValueError('sample outside position contract')
                if terminal_hold:self.phase='REFERENCE_STOPPED'
                return q
            except ValueError:
                self.abort('REFERENCE_EXHAUSTED_OR_INVALID_CLOCK')
                raise

    def sample_command(self,at):
        """Final command-only boundary for NO_SEND/adapters; never opens a writer.

        Keep one generation locked across reference sampling and encoding.
        ABORT, coverage and gross-reference guards run before saturation.
        """
        with self._lock:
            raw=self.sample(at)
            return self.command_limiter.encode(raw)
