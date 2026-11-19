"""Pure reference/queue machinery. No SDK, socket, sensor or storage on import."""
from __future__ import annotations

import bisect
from dataclasses import dataclass
import math
import threading
import time
import uuid
if __package__:
    from .h50_approved_contract import APPROVED_CONTRACT,APPROVED_CONTRACT_SHA256,require_approved_config
else:
    from h50_approved_contract import APPROVED_CONTRACT,APPROVED_CONTRACT_SHA256,require_approved_config

_APPROVED = require_approved_config(APPROVED_CONTRACT)
NS = 1_000_000_000
HZ,NATIVE_HZ,SEND_HZ = (_APPROVED[k] for k in ('action_hz','native_training_hz','send_hz'))
HORIZON,FREEZE,KNOWN,REPLAN_STEPS,RESERVE = (_APPROVED[k] for k in
    ('horizon','rtc_freeze_steps','rtc_known_steps','replan_steps','queue_reserve_steps'))
BETA = _APPROVED['rtc_beta']
DEADLINE_NS = _APPROVED['reply_deadline_ms']*1_000_000
DEADLINE_S,LOW_WATER_S = DEADLINE_NS/NS,RESERVE/HZ
SEND_PERIOD_S,REPLAN_PERIOD_S = 1/SEND_HZ,REPLAN_STEPS/HZ
EXECUTION_VERSION = _APPROVED['execution_contract_version']
# Null is explicit no software velocity cap; JSON never contains Infinity.
SPEED = [math.inf if value is None else value for value in
         [_APPROVED['reference_speed_deg_s']]*6+[_APPROVED['gripper_reference_speed_m_s']]]
ACCEL = [_APPROVED['reference_accel_deg_s2']]*6+[_APPROVED['gripper_reference_accel_m_s2']]


def require(ok, reason):
    if not ok: raise RuntimeError('H50 async: '+reason)


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def settings(cfg):
    value=require_approved_config(cfg)
    require(value==_APPROVED,'executor/core timing contract mismatch')
    return value


def label_offset_ns(index):
    """Round each exact label offset once, without accumulating float periods."""
    require(type(index) is int and index>=0,'nonnegative integer label index required')
    return (index*NS+HZ//2)//HZ


def observation_anchor_ns(obs,original_ns=None):
    anchor=round(obs*NS) if original_ns is None else original_ns
    require(type(anchor) is int and anchor>0 and abs(anchor/NS-obs)<1e-8,'observation clock binding invalid')
    return anchor


def replan_due_ns(origin_ns,slot):
    require(type(origin_ns) is int and origin_ns>0,'integer schedule origin required')
    return origin_ns+label_offset_ns(slot*REPLAN_STEPS)


def next_replan_slot(origin_ns,finished_ns,previous_slot):
    require(type(finished_ns) is int and finished_ns>=origin_ns,'schedule clock moved backwards')
    elapsed=finished_ns-origin_ns
    slot=max(previous_slot+1,elapsed*HZ//(REPLAN_STEPS*NS)+1)
    # An exact rational period can round down by <1 ns. Never catch up at an
    # already-due slot, including that boundary case.
    if replan_due_ns(origin_ns,slot)<=finished_ns:slot+=1
    return slot


def gripper_command_range(bounds):
    """User-approved closed interval in metres, intersected with existing bounds."""
    require(len(bounds)==7 and len(bounds[6])==2 and all(finite(v) for v in bounds[6]),
            'finite gripper command bounds required')
    lo=max(-.0013,bounds[6][0]);hi=min(.1,bounds[6][1])
    require(lo<hi,'configured gripper bounds do not intersect approved range')
    return lo,hi


def project_gripper_command(value,bounds):
    """Shared bilateral mapping for model preflight and final sampled output."""
    require(finite(value),'finite gripper command required')
    lo,hi=gripper_command_range(bounds)
    return min(hi,max(lo,value))


JOINT_COMMAND_POLICY = 'JOINT_BILATERAL_COMMAND_PROJECTION_V1'


def project_joint_command(value,limits):
    """User-approved projection in degrees; never accepts nonfinite inputs."""
    require(finite(value),'finite joint command required')
    require(isinstance(limits,(list,tuple)) and len(limits)==2 and
            all(finite(x) for x in limits) and limits[0]<limits[1],
            'finite ordered joint command bounds required')
    return min(limits[1],max(limits[0],value))


def physical_actions(raw, bounds):
    """Validate finite H50; map adopted seven-axis targets into actual bounds.

    The old accepted reference supplies the frozen prefix. Returned raw prefix
    values remain untouched evidence and never become mechanical commands.
    """
    require(len(bounds)==7 and all(len(pair)==2 and all(finite(v) for v in pair) and pair[0]<pair[1]
                                  for pair in bounds),'seven finite ordered physical bounds required')
    require(isinstance(raw,list) and len(raw)==HORIZON,'exact H50 required; no trim/pad')
    rows=[]
    for index,row in enumerate(raw):
        require(isinstance(row,list) and len(row)==7 and all(finite(x) for x in row),'bad H50 row')
        values=[math.degrees(x) for x in row[:6]]+[row[6]]
        require(all(finite(x) for x in values),'physical unit conversion overflow')
        if index>=FREEZE:
            values[:6]=[project_joint_command(x,pair) for x,pair in zip(values[:6],bounds[:6])]
            values[6]=project_gripper_command(values[6],bounds)
        rows.append(values)
    return rows


def joint_command_projection(raw,bounds):
    """Sparse per-row corrections; original radians remain in raw_h50."""
    converted=physical_actions(raw,bounds)
    proof=[]
    for i,row in enumerate(raw):
        if i<FREEZE:continue
        for j,pair in enumerate(bounds[:6]):
            before=math.degrees(row[j]);after=converted[i][j]
            if before!=after:
                proof.append({'action_index':i,'joint_index':j+1,'raw_model_target_rad':row[j],
                    'raw_model_target_deg':before,'execution_target_deg':after,
                    'delta_deg':after-before,'bounds_deg':list(pair),
                    'clamp_side':'lower' if before<pair[0] else 'upper',
                    'policy':JOINT_COMMAND_POLICY,'raw_model_output_preserved':True})
    return proof


def gripper_command_projection(raw,bounds):
    """Audit both sides of the adopted tail; raw model values are never mutated."""
    converted=physical_actions(raw,bounds);lo,hi=gripper_command_range(bounds)
    return [{'action_index':i,'raw_model_target_m':row[6],'execution_target_m':converted[i][6],
             'delta_m':converted[i][6]-row[6],'excess_m':abs(converted[i][6]-row[6]),
             'clamp_side':'lower' if row[6]<lo else 'upper',
             'policy':'GRIPPER_BILATERAL_COMMAND_PROJECTION_V2',
             'raw_model_output_preserved':True}
            for i,row in enumerate(raw) if i>=FREEZE and not lo<=row[6]<=hi]


def gripper_upper_projection(raw,bounds):
    """Compatibility helper for upper-only consumers; new audits use both sides."""
    return [item for item in gripper_command_projection(raw,bounds) if item['clamp_side']=='upper']


OUTPUT_COMMAND_POLICY = 'tiger-h50-final-seven-axis-bilateral-sdk-quantized-v3'


def prepare_output_command(reference, bounds):
    """Pure seven-axis preflight after interpolation; never opens a writer.

    Project finite joint targets into actual command bounds and gripper targets
    into the approved range. Preserve raw references and sparse corrections.
    Return exact SDK-grid values. Bounds themselves are never expanded.
    Validate all seven dimensions before the caller's first CAN packet.
    """
    require(isinstance(reference,(list,tuple)) and len(reference)==7 and
            all(finite(x) for x in reference),'output requires seven finite values')
    require(len(bounds)==7 and all(len(p)==2 and all(finite(x) for x in p) and p[0]<p[1]
                                  for p in bounds),'output requires seven finite ordered bounds')
    joint_mdeg=[];joint_projection=[]
    for index,(value,(lo,hi)) in enumerate(zip(reference[:6],bounds[:6])):
        limited=project_joint_command(value,(lo,hi))
        minimum=math.ceil(lo*1000);maximum=math.floor(hi*1000)
        require(minimum<=maximum,'joint command bounds contain no SDK integer')
        integer=min(maximum,max(minimum,round(limited*1000)))
        require(lo<=integer/1000<=hi,'quantized output joint J%d outside command bounds'%(index+1))
        joint_mdeg.append(integer)
        if limited!=value:
            joint_projection.append({'joint_index':index+1,'before_deg':value,'limited_deg':limited,
                'command_deg':integer/1000,'delta_deg':limited-value,'bounds_deg':[lo,hi],
                'clamp_side':'lower' if value<lo else 'upper','policy':JOINT_COMMAND_POLICY})
    lo,upper=gripper_command_range(bounds)
    limited=project_gripper_command(reference[6],bounds)
    integer=round(limited*1_000_000);aperture=integer/1_000_000
    require(lo<=aperture<=upper,'quantized output gripper outside command bounds')
    return {'policy':OUTPUT_COMMAND_POLICY,'target_deg_m':[x/1000 for x in joint_mdeg]+[aperture],
            'joint_command_mdeg':joint_mdeg,'joint_projection':joint_projection,'gripper_command_um':integer,
            'gripper_before_m':reference[6],'gripper_limited_m':limited,'gripper_after_m':aperture,
            'gripper_lower_cap_m':lo,'gripper_upper_cap_m':upper,
            'gripper_lower_clamped':reference[6]<lo,'gripper_upper_clamped':reference[6]>upper,
            'gripper_delta_m':aperture-reference[6]}


def discarded_prefix_bound_violations(converted,bounds):
    return [{'action_index':i,'dimension':('J%d'%(j+1)) if j<6 else 'gripper','value':value,
             'unit':'deg' if j<6 else 'm','bounds':list(limits),'adopted_for_dispatch':False}
            for i,row in enumerate(converted[:FREEZE]) for j,(value,limits) in enumerate(zip(row,bounds))
            if not limits[0]<=value<=limits[1]]


@dataclass(frozen=True)
class Node:
    t: float
    q: tuple
    v: tuple


class Plan:
    def __init__(self,nodes,plan_id=None):
        require(len(nodes)>=2,'at least two reference nodes required')
        require(all(finite(n.t) and len(n.q)==len(n.v)==7 and all(finite(v) for v in [*n.q,*n.v]) for n in nodes),
                'finite 7D reference nodes required')
        require(all(b.t>a.t for a,b in zip(nodes,nodes[1:])),'reference times must increase')
        require(all(max(abs(qb-(qa+.5*(va+vb)*(b.t-a.t))) for qa,qb,va,vb in zip(a.q,b.q,a.v,b.v))<1e-7
                    for a,b in zip(nodes,nodes[1:])),'reference q/v segment integration is discontinuous')
        self.nodes=tuple(nodes);self.times=tuple(n.t for n in nodes)
        self.id=plan_id or uuid.uuid4().hex
    @property
    def end(self):return self.times[-1]
    def sample(self,t):
        require(finite(t) and self.times[0]-1e-9 <= t <= self.end+1e-9,'reference time outside accepted coverage')
        if t>=self.end:return list(self.nodes[-1].q),list(self.nodes[-1].v)
        index=max(0,bisect.bisect_right(self.times,t)-1)
        a,b=self.nodes[index:index+2];dt=b.t-a.t;elapsed=max(0.,t-a.t)
        acceleration=[(vb-va)/dt for va,vb in zip(a.v,b.v)]
        return ([q+v*elapsed+.5*acc*elapsed**2 for q,v,acc in zip(a.q,a.v,acceleration)],
                [v+acc*elapsed for v,acc in zip(a.v,acceleration)])
    def serializable(self):
        return {'plan_id':self.id,'nodes':[{'t':n.t,'q_deg_m':list(n.q),'v_deg_m_s':list(n.v)} for n in self.nodes]}


def hold_plan(anchor,start,end):
    zero=(0.,)*7
    return Plan([Node(start,tuple(anchor),zero),Node(end,tuple(anchor),zero)])


def governor_step(q,v,desired,dt,bounds):
    require(finite(dt) and 0<dt<=SEND_PERIOD_S+1e-8,'bounded reference integration tick required')
    nq,nv=[],[]
    for current,vel,target,(lo,hi),speed,accel in zip(q,v,desired,bounds,SPEED,ACCEL):
        require(lo<=target<=hi,'raw target outside limits')
        error=target-current
        wanted=math.copysign(min(speed,math.sqrt(2*accel*abs(error))),error) if error else 0.
        low=max(-speed,vel-accel*dt,2*(lo-current)/dt-vel)
        high=min(speed,vel+accel*dt,2*(hi-current)/dt-vel)
        disc_hi=(accel*dt)**2+8*accel*(hi-current-.5*vel*dt)
        disc_lo=(accel*dt)**2+8*accel*(current-lo+.5*vel*dt)
        high=min(high,max(0.,(-accel*dt+math.sqrt(max(0.,disc_hi)))/2))
        low=max(low,min(0.,(accel*dt-math.sqrt(max(0.,disc_lo)))/2))
        require(low<=high+1e-10,'no feasible reference continuation')
        velocity=min(high,max(low,wanted))
        if vel*velocity<0:
            # Endpoint feasibility misses the stationary point inside a turn.
            # Restrict the endpoint velocity only when this proposed segment's
            # exact q(t_zero)=q0-v0**2/(2*a) would cross a bound.
            turning_position=current-vel*vel/(2*((velocity-vel)/dt))
            # The existing endpoint/interior tolerance is 1e-10. Use half of
            # that numerical budget when solving the interior constraint; the
            # remaining half absorbs floating-point evaluation of q(t_zero).
            # Exact-bound outward motion still fails as before. No bound,
            # final verification tolerance or acceleration limit is changed.
            if turning_position<lo:
                require(current>lo,'negative velocity at lower boundary has no feasible continuation')
                low=max(low,math.nextafter(vel+dt*vel*vel/(2*(current-lo+5e-11)),math.inf))
            if turning_position>hi:
                require(current<hi,'positive velocity at upper boundary has no feasible continuation')
                high=min(high,math.nextafter(vel-dt*vel*vel/(2*(hi-current+5e-11)),-math.inf))
            require(low<=high+1e-10,'no feasible reference continuation through interior extremum')
            velocity=min(high,max(low,wanted))
        position=current+.5*(vel+velocity)*dt
        # Floating-point cancellation can round an endpoint onto the bound
        # while retaining an outward velocity of a few ulps. Select a feasible
        # zero-velocity endpoint for this segment instead of manufacturing an
        # infeasible state for the next tick. This never expands a bound or
        # forgives an already outward-moving state supplied at the boundary.
        rounded_outward=(velocity>0 and position>=hi) or (velocity<0 and position<=lo)
        if rounded_outward and abs(vel)<=accel*dt+1e-9:
            stopped_position=current+.5*vel*dt
            if lo<=stopped_position<=hi:
                velocity=0.;position=stopped_position
        require(lo-1e-10<=position<=hi+1e-10,'reference crossed actual limits')
        if vel*velocity<0:
            actual_acceleration=(velocity-vel)/dt
            turn_time=-vel/actual_acceleration
            turn_position=current+vel*turn_time+.5*actual_acceleration*turn_time**2
            require(lo-1e-10<=turn_position<=hi+1e-10,'reference interior extremum crossed actual limits')
        require(abs(velocity)<=speed+1e-9 and abs(velocity-vel)<=accel*dt+1e-9,'reference derivative violation')
        nq.append(position);nv.append(velocity)
    return nq,nv


def tail_plan(old,raw,obs,cut,bounds,*,observation_ns=None):
    anchor=observation_anchor_ns(obs,observation_ns)
    require(finite(cut) and abs(cut-(anchor+label_offset_ns(FREEZE))/NS)<1e-8,
            'tail cut must preserve the frozen 12-label prefix')
    rows=physical_actions(raw,bounds)
    q,v=old.sample(cut)
    history_start=max(old.times[0],obs-.2)
    hq,hv=old.sample(history_start)
    nodes=[Node(history_start,tuple(hq),tuple(hv))]
    nodes.extend(n for n in old.nodes if history_start+1e-10<n.t<cut-1e-10)
    nodes.append(Node(cut,tuple(q),tuple(v)))
    when=cut;end=(anchor+label_offset_ns(HORIZON))/NS
    while end-when>1e-9:
        dt=min(SEND_PERIOD_S,end-when)
        index=min(float(HORIZON-1),max(0.,(when+dt-anchor/NS)*HZ));lo=int(index);hi=min(HORIZON-1,lo+1);fraction=index-lo
        desired=[a+(b-a)*fraction for a,b in zip(rows[lo],rows[hi])]
        q,v=governor_step(q,v,desired,dt,bounds)
        when+=dt;nodes.append(Node(when,tuple(q),tuple(v)))
    candidate=Plan(nodes)
    aq,av=candidate.sample(cut);oq,ov=old.sample(cut)
    require(max(abs(a-b) for a,b in zip(aq,oq))<1e-8 and max(abs(a-b) for a,b in zip(av,ov))<1e-8,
            'replacement cut is not q/v continuous')
    return candidate


def brake_plan(plan,now,bounds):
    q,v=plan.sample(now);nodes=[Node(now,tuple(q),tuple(v))];when=now
    while any(abs(x)>1e-10 for x in v):
        dt=min(SEND_PERIOD_S,min(abs(x)/a for x,a in zip(v,ACCEL) if abs(x)>1e-10))
        nv=[math.copysign(max(0.,abs(x)-a*dt),x) if abs(x)>1e-10 else 0. for x,a in zip(v,ACCEL)]
        nq=[x+.5*(a+b)*dt for x,a,b in zip(q,v,nv)]
        require(all(lo-1e-8<=x<=hi+1e-8 for x,(lo,hi) in zip(nq,bounds)),'approved braking crossed actual limits')
        when+=dt;q,v=nq,nv;nodes.append(Node(when,tuple(q),tuple(v)))
    if len(nodes)==1:nodes.append(Node(when+.001,tuple(q),(0.,)*7))
    return Plan(nodes)


class AsyncQueue:
    """One immutable accepted plan; replace only the future uncommitted tail."""
    def __init__(self,anchor,bounds,start,*,no_send=False,diagnostic=False,start_ns=None):
        require(type(no_send) is bool and type(diagnostic) is bool and not(no_send and diagnostic),
                'physical HOLD and diagnostic reference must be separate queues')
        self.lock=threading.RLock();self.bounds=bounds;self.no_send=no_send;self.anchor=list(anchor)
        self.diagnostic=diagnostic
        self.reference_scope='DIAGNOSTIC_UNSENT_REFERENCE' if diagnostic else 'PHYSICAL_ACCEPTED_REFERENCE'
        start_ns=observation_anchor_ns(start,start_ns)
        self.plan=hold_plan(anchor,start_ns/NS,(start_ns+label_offset_ns(HORIZON))/NS);self.phase='RUNNING';self.reason=None
        self.accepted=0;self.rejected=0;self.last_cut=None;self.last_requested_index=-1;self.last_inference_phase='INITIAL_HOLD'
    def context(self,obs,trial_index,request_index,*,observation_ns=None):
        with self.lock:
            require(self.phase=='RUNNING','not accepting new predictions')
            require(not self.no_send,'physical NO_SEND HOLD queue cannot provide an inference context')
            require(type(request_index) is int and request_index>=0 and request_index>self.last_requested_index,
                    'request index must advance exactly once; no silent bootstrap retry')
            bootstrap=self.accepted==0
            require(not bootstrap or (request_index==0 and self.last_requested_index==-1),
                    'native bootstrap was not accepted; new inference is forbidden')
            require(bootstrap or request_index>0,'RTC continuation requires the accepted native bootstrap')
            anchor_ns=observation_anchor_ns(obs,observation_ns)
            old=self.plan
            previous=[]
            for index in range(0 if bootstrap else KNOWN):
                q,_=old.sample((anchor_ns+label_offset_ns(index))/NS)
                previous.append([math.radians(x) for x in q[:6]]+[q[6]])
            cut_ns=anchor_ns+label_offset_ns(FREEZE)
            # The old plan must cover the protected cut even without an RTC prefix.
            old.sample(cut_ns/NS)
            self.last_requested_index=request_index
            return {'schema':'tiger-fp32-h50-rtc-v1','enabled':not bootstrap,'previous_actions':previous,
                    'previous_valid_steps':0 if bootstrap else KNOWN,'freeze_steps':FREEZE,'execution_label_hz':HZ,'beta':BETA,
                    'native_training_hz':NATIVE_HZ,'execution_contract_version':EXECUTION_VERSION,
                    'timing_contract_sha256':APPROVED_CONTRACT_SHA256,
                    'trial_index':trial_index,'request_index':request_index,'old_plan_id':old.id,
                    'observation_monotonic_ns':anchor_ns,'cut_monotonic_ns':cut_ns,
                    'deadline_monotonic_ns':anchor_ns+DEADLINE_NS,
                    'inference_phase':'NATIVE_BOOTSTRAP' if bootstrap else 'RTC_CONTINUATION',
                    'reference_scope':self.reference_scope,
                    'startup_semantic_version':_APPROVED['startup_semantic_version'],
                    'no_send_semantic_version':_APPROVED['no_send_semantic_version'] if self.diagnostic else None,
                    'prefix_reference_kind':'NATIVE_BOOTSTRAP_NO_PREFIX' if bootstrap else
                        ('DIAGNOSTIC_UNSENT_REFERENCE' if self.diagnostic else 'ACCEPTED_REFERENCE_PLAN_FUTURE_NOT_ALL_SENT')},old
    def commit(self,candidate,context,now,*,now_ns=None):
        with self.lock:
            require(context.get('timing_contract_sha256')==APPROVED_CONTRACT_SHA256 and
                    context.get('execution_contract_version')==EXECUTION_VERSION and
                    context.get('execution_label_hz')==HZ and
                    context.get('startup_semantic_version')==_APPROVED['startup_semantic_version'] and
                    context.get('reference_scope')==self.reference_scope,'mixed execution/startup timing contract')
            require(context.get('inference_phase')==('NATIVE_BOOTSTRAP' if self.accepted==0 else 'RTC_CONTINUATION') and
                    context.get('enabled') is (self.accepted>0),'bootstrap/RTC phase differs from accepted queue state')
            now_ns=observation_anchor_ns(now,now_ns)
            if (self.phase!='RUNNING' or now_ns>context['deadline_monotonic_ns'] or
                    self.plan.id!=context['old_plan_id']):
                self.rejected+=1;return False
            require(not self.no_send,'NO_SEND cannot install a moving prediction')
            require(now_ns<context['cut_monotonic_ns'],'cut is already in the past')
            self.plan=candidate;self.last_cut=context['cut_monotonic_ns'];self.accepted+=1
            self.last_inference_phase=context['inference_phase']
            return True
    def brake(self,reason,now):
        with self.lock:
            if self.phase!='RUNNING':return
            if self.no_send:
                self.phase='STOPPED';self.reason=reason;return
            self.plan=brake_plan(self.plan,now,self.bounds);self.phase='BRAKE';self.reason=reason
    def abort(self,reason):
        with self.lock:self.phase='ABORT';self.reason=reason
    def sample(self,now):
        with self.lock:
            require(self.phase!='ABORT','ABORT_NO_NEW_TARGETS: '+str(self.reason))
            if self.no_send:
                return list(self.anchor),[0.]*7,self.phase,self.plan.id
            if self.phase=='RUNNING' and not self.no_send and self.plan.end-now<=LOW_WATER_S:
                self.brake('LOW_WATER_%d_TICKS'%RESERVE,now)
            if self.phase=='BRAKE' and now>=self.plan.end:
                self.phase='STOPPED'
            if self.phase=='STOPPED':return list(self.plan.nodes[-1].q),[0.]*7,self.phase,self.plan.id
            q,v=self.plan.sample(now)
            return q,v,self.phase,self.plan.id


class LatestObservation:
    def __init__(self):
        self.condition=threading.Condition();self.value=None;self.closed=False;self.superseded=[]
    def put(self,value):
        with self.condition:
            if self.closed:return
            if self.value is not None:self.superseded.append(self.value['packet_ready_monotonic_ns'])
            self.value=value;self.condition.notify()
    def take(self):
        with self.condition:
            while self.value is None and not self.closed:self.condition.wait(.05)
            if self.value is None:return None
            value,self.value=self.value,None;return value
    def close(self):
        with self.condition:self.closed=True;self.condition.notify_all()


def validate_observation_age(snapshot,now,*,now_ns=None):
    now_ns=observation_anchor_ns(now,now_ns)
    state_max_ns=round(_APPROVED['feedback_max_age_s']*NS)
    camera_max_ns=round(_APPROVED['camera_max_age_s']*NS)
    ready=snapshot.get('packet_ready_monotonic_ns')
    require(type(ready) is int and 0<=now_ns-ready<=state_max_ns,'observation packet stale/future')
    require(snapshot.get('hardware_synchronized') is False,'unexpected synchronization claim')
    for role in ('front','right'):
        source=snapshot.get('frames',{}).get(role,{}).get('source_monotonic_ns')
        require(type(source) is int and 0<=now_ns-source<=camera_max_ns,'camera source exceeds 250 ms: '+role)
    source=snapshot.get('right_state',{}).get('source_monotonic_ns')
    require(type(source) is int and 0<=now_ns-source<=state_max_ns,'joint/gripper source exceeds 200 ms')
    return ready/NS


class TrackingGuard:
    def __init__(self):self.bad_since=None
    def check(self,actual,last_sent,now):
        error=0. if last_sent is None else max(abs(a-b) for a,b in zip(actual,last_sent))
        if error>5:
            if self.bad_since is None:self.bad_since=now
            require(now-self.bad_since<.1,'actual versus last host-submitted reference >5 deg for 100 ms')
        else:self.bad_since=None
        return error


def next_tick(scheduled,finished):
    target=scheduled+SEND_PERIOD_S
    return finished+SEND_PERIOD_S if finished>target else target
