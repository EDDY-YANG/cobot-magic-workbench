"""Bounded reference repair, separate from measured state and final CAN limits.

Prefer a shorter existing overlap knot for the offending arm axes only. If no
smooth position-legal candidate exists, project those reference axes
and let the unchanged constrained MPC handle the explicitly reported corners.
Neither option changes the accepted prefix or invents an initial state.
"""
from dataclasses import dataclass
from pathlib import Path
import json
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'vendor'))
from client_time_queue import ContractError, require, NS
try:
    from .gripper_candidate import _polyroots_batch, polynomial_derivative
except ImportError:
    from gripper_candidate import _polyroots_batch, polynomial_derivative

def readonly(value):
    a=np.asarray(value,dtype=float)
    require(np.isfinite(a).all(),'Nonfinite joint repair input')
    return np.frombuffer(a.tobytes(),dtype=float).reshape(a.shape)

def policy():
    return {'schema':'tiger-v2-joint-reference-repair-v1',
        'axes':['J1','J2','J3','J4','J5','J6'],
        'strategy':'SHORTEN_EXISTING_KNOWN_KNOT_THEN_REFERENCE_PROJECTION',
        'continuous_current_reference_repair_s':3.0,
        'timer':'ANY_ARM_AXIS_CONTINUOUS_AT_EXECUTED_REFERENCE_PHASE; axis change does not reset',
        'stop_action':'STOP_NEW_TARGETS; last received target may finish; NOT_HARDWARE_ESTOP',
        'future_preview_does_not_start_timer':True,'measured_feedback_is_not_this_timer':True,
        'position_limits_changed':False,'frozen_prefix_changed':False,'state_reset':False}

def position_extrema(curve,axes):
    """Exact polynomial stationary roots for selected axes, not time sampling."""
    c=curve.coefficients[:,:,list(axes)]
    dc=np.polynomial.polynomial.polyder(c,axis=1)
    roots=_polyroots_batch(dc.transpose(0,2,1).reshape(-1,5))
    low=np.full(len(axes),np.inf);high=np.full(len(axes),-np.inf)
    for interval in range(len(c)):
        for j in range(len(axes)):
            real=[float(np.clip(r.real,0,1)) for r in roots[interval*len(axes)+j]
                  if abs(r.imag)<=1e-8*(1+abs(r.real)) and -1e-10<=r.real<=1+1e-10]
            q=np.polynomial.polynomial.polyval([0.,1.,*real],c[interval,:,j])
            low[j]=min(low[j],float(np.min(q)));high[j]=max(high[j],float(np.max(q)))
    tolerance=128*np.finfo(float).eps*np.maximum(1.,np.max(np.sum(abs(c),axis=1),axis=0))
    return low,high,tolerance

@dataclass(frozen=True)
class JointBoundaryCurve:
    source: object
    bounds: np.ndarray
    axes: tuple
    arm_curve: object=None

    def __post_init__(self):
        b=readonly(self.bounds)
        require(b.shape==(7,2) and np.all(b[:,0]<b[:,1]),'Invalid repair position envelope')
        require(self.axes==tuple(sorted(set(self.axes))) and all(0<=a<6 for a in self.axes),'Invalid repaired axes')
        object.__setattr__(self,'bounds',b)
        object.__setattr__(self,'_source_position_tolerance',readonly(self.source.extrema()[3][:6]))
        # A boundary with outward inherited velocity cannot be made C1 by
        # clipping. Reject it explicitly instead of silently zeroing velocity.
        for a in self.axes:
            q=self.source.cut_anchor[a];v=self.source.cut_velocity[a]
            require(not ((q<=b[a,0] and v<0) or (q>=b[a,1] and v>0)),
                    f'J{a+1} cut is at boundary with outward inherited velocity; no state reset')

    @property
    def times(self):return self.source.times
    @property
    def base_values(self):return self.source.base_values
    @property
    def base(self):return self.source.base
    @property
    def cut_anchor(self):return self.source.cut_anchor
    @property
    def cut_velocity(self):return self.source.cut_velocity
    @property
    def bridge_end_ns(self):return self.source.bridge_end_ns
    @property
    def gripper_end_ns(self):return getattr(self.source,'gripper_end_ns',self.source.bridge_end_ns)

    def sample(self,at):
        q=np.array(self.source.sample(at),copy=True)
        if self.arm_curve is None:
            for a in self.axes:q[...,a]=np.clip(q[...,a],self.bounds[a,0],self.bounds[a,1])
        else:
            repaired=self.arm_curve.sample(at)
            for a in self.axes:q[...,a]=repaired[...,a]
        return q

    def _derivative(self,at,side,order):
        value=np.array(polynomial_derivative(self.source,int(at),order,side),copy=True)
        if self.arm_curve is not None:
            arm=polynomial_derivative(self.arm_curve,int(at),order,side)
            for a in self.axes:value[a]=arm[a]
        else:
            raw=self.source.sample(int(at));v=self.source.velocity(int(at),side)
            direction=1 if side=='right' else -1
            for a in self.axes:
                lower,upper=self.bounds[a]
                outside=raw[a]<lower or raw[a]>upper
                # One-sided derivative at a clipping crossing, never a finite
                # difference of physical feedback. Interior corners are explicit.
                if raw[a]==lower:outside=direction*v[a]<0
                if raw[a]==upper:outside=direction*v[a]>0
                if v[a]==0 and raw[a] in (lower,upper):
                    for derivative_order in (2,3):
                        derivative=polynomial_derivative(self.source,int(at),derivative_order,side)[a]
                        if derivative!=0:
                            sign=derivative*direction**derivative_order
                            outside=(raw[a]==lower and sign<0) or (raw[a]==upper and sign>0)
                            break
                if outside:value[a]=0.
        return value
    def velocity(self,at,side='right'):return self._derivative(at,side,1)
    def acceleration(self,at,side='right'):return self._derivative(at,side,2)
    def jerk(self,at,side='right'):return self._derivative(at,side,3)

    def extrema(self):
        low,high,peak,tol=(x.copy() for x in self.source.extrema())
        if self.arm_curve is None:
            # Projection is monotone and 1-Lipschitz. Position extrema are
            # exact; original speed peaks remain conservative upper bounds.
            for a in self.axes:
                low[a]=np.clip(low[a],*self.bounds[a]);high[a]=np.clip(high[a],*self.bounds[a])
        else:
            lo,hi,p,t=self.arm_curve.extrema()
            for a in self.axes:low[a],high[a],peak[a],tol[a]=lo[a],hi[a],p[a],t[a]
        return low,high,peak,tol

    def correction_motion_extrema(self):
        v,a=(x.copy() for x in self.source.correction_motion_extrema())
        if self.arm_curve is None:
            base_speed=np.max(abs(np.diff(self.base_values,axis=0)/(np.diff(self.times)/NS)[:,None]),axis=0)
            for axis in self.axes:v[axis]=max(v[axis],base_speed[axis])
            # Regular derivatives only. Projection contact impulses are NOT
            # claimed finite, bounded-jerk, or already smooth physical motion.
        else:
            av,aa=self.arm_curve.correction_motion_extrema()
            for axis in self.axes:v[axis],a[axis]=av[axis],aa[axis]
        return v,a

    def joint_repair_observation(self,at):
        raw=np.asarray(self.source.sample(int(at)))[:6]
        excess=np.maximum(self.bounds[:6,0]-raw,raw-self.bounds[:6,1])
        # Existing numerical arithmetic tolerance, not a new physical margin.
        mask=excess>self._source_position_tolerance
        return {'axes':[int(i+1) for i in np.flatnonzero(mask)],
                'raw_reference_rad':raw.tolist(),'excess_rad':np.maximum(0.,excess).tolist()}

    def integrity_payload(self):
        return (b'TIGER_JOINT_REFERENCE_REPAIR_V1\0'+self.source.integrity_payload()+self.bounds.tobytes()
                +bytes(self.axes)+(b'PROJECT' if self.arm_curve is None else self.arm_curve.integrity_payload()))

def _step_metrics(curve,raw_first_deg,step_budget_deg):
    probes=curve.times[curve.times<=curve.bridge_end_ns]
    bridge=np.rad2deg(np.diff(curve.sample(probes)[:,:6],axis=0))
    base=np.rad2deg(np.diff(curve.base.sample(probes)[:,:6],axis=0))
    added=float(np.max(np.maximum(0.,abs(bridge)-abs(base))))
    first=float(np.max(np.maximum(0.,abs(bridge[0])-abs(raw_first_deg))))
    return {'max_added_step_deg':added,'first_added_vs_raw_deg':first,
            'within_existing_budget':max(added,first)<=step_budget_deg,
            'step_warning_threshold_deg':step_budget_deg,'step_budget_enforced':False}

def repair_joint_reference(source,bounds,axes,curve_factory,*,raw_first_deg,step_budget_deg):
    """Longest legal shorter existing knot, followed by explicit projection."""
    axes=tuple(axes);b=np.asarray(bounds);search=[]
    for end in source.times[(source.times>source.times[0]) & (source.times<source.bridge_end_ns)][::-1]:
        arm=curve_factory(source.times,source.base_values,source.cut_anchor,source.cut_velocity,int(end))
        lo,hi,tol=position_extrema(arm,axes)
        ok=bool(np.all(np.maximum(b[list(axes),0]-lo,hi-b[list(axes),1])<=tol))
        row={'duration_s':(int(end)-int(source.times[0]))/NS,'position_ok':ok};search.append(row)
        if not ok:continue
        candidate=JointBoundaryCurve(source,b,axes,arm)
        row.update(_step_metrics(candidate,raw_first_deg,step_budget_deg))
        row['step_exceedance_action']='diagnostic_only'
        return candidate,{'strategy':'SHORTER_KNOWN_KNOT_QUINTIC','axes':[a+1 for a in axes],
            'selected_duration_s':row['duration_s'],'search':search,'projection_corners':False,
            'step_exceedance_action':'diagnostic_only',
            'qv_at_cut_preserved':True,'unaffected_axes_and_gripper_unchanged':True}
    candidate=JointBoundaryCurve(source,b,axes)
    metrics=_step_metrics(candidate,raw_first_deg,step_budget_deg)
    return candidate,{'strategy':'REFERENCE_POSITION_PROJECTION_BEFORE_MPC','axes':[a+1 for a in axes],
        'step_exceedance_action':'diagnostic_only',
        'search':search,'projection_corners':True,'velocity_peaks_are_upper_bounds':True,
        'regular_derivatives_exclude_projection_contact_impulses':True,
        'qv_at_cut_preserved':True,'unaffected_axes_and_gripper_unchanged':True,**metrics}

class ReferenceRepairTimeout(RuntimeError):pass

class ReferenceRepairWatchdog:
    """Constant RAM; observe only current reference phase, never future preview."""
    def __init__(self):
        self.since_ns=None;self.last_ns=None;self.max_ns=0;self.ticks=0;self.episodes=0;self.last_axes=[];self.triggered=False
    def observe(self,now_ns,plan,phase_ns):
        require(type(now_ns) is int and (self.last_ns is None or now_ns>self.last_ns),'Repair watchdog clock must advance')
        self.last_ns=now_ns;probe={'axes':[]}
        if plan is not None and phase_ns is not None:
            require(plan.start<=phase_ns<=plan.end,'Repair watchdog phase outside accepted queue')
            index=min(int(np.searchsorted([s.end for s in plan.pieces],phase_ns,side='right')),len(plan.pieces)-1)
            curve=plan.pieces[index].curve
            if hasattr(curve,'joint_repair_observation'):probe=curve.joint_repair_observation(int(phase_ns))
        if not probe['axes']:
            self.since_ns=None;self.last_axes=[];return None
        if self.since_ns is None:self.since_ns=now_ns;self.episodes+=1
        duration=now_ns-self.since_ns;self.max_ns=max(self.max_ns,duration);self.ticks+=1;self.last_axes=probe['axes']
        result={**probe,'continuous_ms':duration/1e6,'scope':'CURRENT_REFERENCE_NOT_FEEDBACK','limit_ms':3000.}
        if duration>=3*NS:
            self.triggered=True
            raise ReferenceRepairTimeout('内部参考连续需要位置修正达到3秒，停止新增目标；非硬件急停。涉及关节 '+str(probe['axes']))
        return result
    def summary(self):
        return {'policy':policy(),'episodes':self.episodes,'repaired_current_ticks':self.ticks,
                'max_continuous_ms':self.max_ns/1e6,'last_axes':self.last_axes,'triggered':self.triggered,
                'measured_feedback_modified':False,'storage':'CONSTANT_SIZE_RAM'}
