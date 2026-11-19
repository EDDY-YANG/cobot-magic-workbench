"""Finite-support analytic reference correction for soft-guided VLA chunks.

The official TimeQP sees projected model rows12..49, never an overwritten row12.
The inherited prefix remains unchanged. A quintic residual matches position
and velocity at cut and vanishes with its derivative at known label34. The
official piecewise-linear base retains its existing interior velocity knots;
this module does not claim that the entire path is globally C1.
"""
from dataclasses import dataclass,replace
from pathlib import Path
import hashlib
import math
import sys
import time
import uuid
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'vendor'))
from client_time_queue import (Curve,Slice,Plan,Candidate,ContractError,array,require,candidate_seal,
                              label_ns,NS,FREEZE,KNOWN,HORIZON,SEND_NS)
try:
    from .gripper_candidate import build_c2_gripper_candidate,plan_acceleration,GripperCandidateRejected,_polyroots_batch
    from .bridge_step_policy import THRESHOLD_DEG
except ImportError:
    from gripper_candidate import build_c2_gripper_candidate,plan_acceleration,GripperCandidateRejected,_polyroots_batch
    from bridge_step_policy import THRESHOLD_DEG


class GripperBridgeAdmissionError(ContractError):
    """Retain the rejected B search without returning an unaccepted curve."""
    def __init__(self,message,diagnostics):
        self.gripper_transition_diagnostics=diagnostics
        super().__init__(message)


def immutable(value,dtype,shape=None):
    a=np.asarray(value,dtype=dtype)
    if shape is not None:require(a.shape==shape,'Unexpected bridge array shape')
    require(np.isfinite(a).all(),'Nonfinite bridge array')
    return np.frombuffer(a.tobytes(),dtype=dtype).reshape(a.shape)


def unit_interval_roots(coefficients):
    """All numerically real roots in[0,1], retaining near-endpoint roots."""
    c=np.trim_zeros(np.asarray(coefficients,float),'b')
    if len(c)<=1:return []
    roots=np.polynomial.polynomial.polyroots(c)
    answer=[]
    for root in roots:
        if abs(root.imag)<=1e-8*(1.+abs(root.real)) and -1e-10<=root.real<=1.+1e-10:
            answer.append(float(min(1.,max(0.,root.real))))
    return answer


@dataclass(frozen=True)
class SoftGuidedBridgeCurve:
    """Piecewise degree<=5 polynomial, actual normalized segment coefficients.

    `base_values` are original TimeQP samples. There is deliberately no `.q`
    pretending polynomial coefficients are ordinary sampled positions.
    """
    times: np.ndarray
    base_values: np.ndarray
    cut_anchor: np.ndarray
    cut_velocity: np.ndarray
    bridge_end_ns: int
    coefficients: np.ndarray=None

    def __post_init__(self):
        times=immutable(self.times,np.int64)
        require(times.ndim==1 and len(times)>=3 and np.all(np.diff(times)>0),'Strict bridge knot times required')
        base=immutable(self.base_values,np.float64,(len(times),7))
        anchor=immutable(self.cut_anchor,np.float64,(7,))
        velocity=immutable(self.cut_velocity,np.float64,(7,))
        require(type(self.bridge_end_ns) is int and times[0]<self.bridge_end_ns<times[-1],
                'Bridge end must be an interior known-overlap knot')
        require(self.bridge_end_ns in times,'Bridge end must match the actual known34 label')
        duration=(self.bridge_end_ns-int(times[0]))/NS
        base_velocity=(base[1]-base[0])/((int(times[1])-int(times[0]))/NS)
        dq=anchor-base[0];dv=velocity-base_velocity
        residual=np.zeros((6,7),float)
        residual[0]=dq;residual[1]=duration*dv
        residual[3]=-10*dq-6*duration*dv
        residual[4]=15*dq+8*duration*dv
        residual[5]=-6*dq-3*duration*dv
        coefficients=np.zeros((len(times)-1,6,7),float)
        for index in range(len(times)-1):
            c=coefficients[index];c[0]=base[index];c[1]=base[index+1]-base[index]
            if times[index]>=self.bridge_end_ns:continue
            s0=(int(times[index])-int(times[0]))/(self.bridge_end_ns-int(times[0]))
            width=(int(times[index+1])-int(times[index]))/(self.bridge_end_ns-int(times[0]))
            for power in range(6):
                for local_power in range(power+1):
                    c[local_power]+=residual[power]*math.comb(power,local_power)*s0**(power-local_power)*width**local_power
        # Exact inherited boundary bits; algebraically equivalent to residual
        # evaluation, preventing cancellation roundoff from changing a seam.
        coefficients[0,0]=anchor
        coefficients[0,1]=velocity*((int(times[1])-int(times[0]))/NS)
        object.__setattr__(self,'times',times)
        object.__setattr__(self,'base_values',base)
        object.__setattr__(self,'cut_anchor',anchor)
        object.__setattr__(self,'cut_velocity',velocity)
        object.__setattr__(self,'coefficients',immutable(coefficients,np.float64))

    def _base_sample(self,at):
        t=np.asarray(at,dtype=np.int64);flat=t.reshape(-1)
        indices=np.minimum(np.searchsorted(self.times,flat,side='right')-1,len(self.times)-2)
        frac=(flat-self.times[indices])/(self.times[indices+1]-self.times[indices])
        values=self.base_values[indices]+frac[:,None]*(self.base_values[indices+1]-self.base_values[indices])
        exact=flat==self.times[indices+1]
        values[exact]=self.base_values[indices[exact]+1]
        return values.reshape((*t.shape,7))

    @property
    def base(self):
        """Independent read view for adapter step-budget diagnostics."""
        return Curve(self.times,self.base_values)

    def sample(self,at):
        t=np.asarray(at,dtype=np.int64);flat=t.reshape(-1)
        require(flat.size>0 and np.all(flat>=self.times[0]) and np.all(flat<=self.times[-1]),
                'Bridge sample outside finite accepted coverage')
        out=np.empty((len(flat),7),float)
        restored=flat>=self.bridge_end_ns
        if restored.any():out[restored]=self._base_sample(flat[restored])
        active=~restored
        if active.any():
            active_t=flat[active]
            indices=np.minimum(np.searchsorted(self.times,active_t,side='right')-1,len(self.times)-2)
            u=(active_t-self.times[indices])/(self.times[indices+1]-self.times[indices])
            values=np.zeros((len(active_t),7))
            for degree in reversed(range(6)):
                values=values*u[:,None]+self.coefficients[indices,degree]
            values[active_t==self.times[0]]=self.cut_anchor
            out[active]=values
        return out.reshape((*t.shape,7))

    def velocity(self,at,side='right'):
        require(type(at) is int and side in ('left','right') and self.times[0]<=at<=self.times[-1],
                'Bridge velocity requires a covered integer timestamp')
        index=int(np.clip(np.searchsorted(self.times,at,side=side)-1,0,len(self.times)-2))
        if at==self.times[0]:return self.cut_velocity.copy()
        if self.times[index]>=self.bridge_end_ns:
            return (self.base_values[index+1]-self.base_values[index])/((int(self.times[index+1])-int(self.times[index]))/NS)
        u=(at-int(self.times[index]))/(int(self.times[index+1])-int(self.times[index]))
        derivative=np.zeros(7)
        for degree in reversed(range(1,6)):
            derivative=derivative*u+degree*self.coefficients[index,degree]
        return derivative/((int(self.times[index+1])-int(self.times[index]))/NS)

    def integrity_payload(self):
        header=np.array([len(self.times),self.bridge_end_ns],dtype=np.int64).tobytes()
        return b'TIGER_SOFT_GUIDED_ANALYTIC_BRIDGE_V1\0'+header+b''.join(a.tobytes() for a in
            (self.times,self.base_values,self.cut_anchor,self.cut_velocity,self.coefficients))

    def extrema(self):
        """Evaluate endpoints and all derivative roots for every7D interval."""
        low=np.full(7,np.inf);high=np.full(7,-np.inf);vpeak=np.zeros(7)
        arithmetic_scale=np.maximum(1.,np.max(np.sum(abs(self.coefficients),axis=1),axis=0))
        bridge_count=int(np.searchsorted(self.times,self.bridge_end_ns))
        # After the bridge every interval is exactly the official linear base:
        # endpoints exhaust position extrema and the slope is constant.
        tail=self.base_values[bridge_count:]
        low=np.minimum(low,np.min(tail,axis=0));high=np.maximum(high,np.max(tail,axis=0))
        tail_dt=np.diff(self.times[bridge_count:])/NS
        if len(tail_dt):
            vpeak=np.maximum(vpeak,np.max(abs(np.diff(tail,axis=0)/tail_dt[:,None]),axis=0))
        # The second derivative of every active piece comes from the SAME
        # global residual. Its roots locate velocity extrema for all pieces.
        residual=self._global_residual()
        global_velocity_critical=[unit_interval_roots(np.polynomial.polynomial.polyder(residual[:,axis],2))
                                  for axis in range(7)]
        derivative_coefficients=np.polynomial.polynomial.polyder(self.coefficients[:bridge_count],axis=1)
        position_roots=_polyroots_batch(derivative_coefficients.transpose(0,2,1).reshape(-1,5))
        span=self.bridge_end_ns-int(self.times[0])
        for index,coeff in enumerate(self.coefficients[:bridge_count]):
            dt=(int(self.times[index+1])-int(self.times[index]))/NS
            s0=(int(self.times[index])-int(self.times[0]))/span
            width=(int(self.times[index+1])-int(self.times[index]))/span
            for axis in range(7):
                c=coeff[:,axis];dc=derivative_coefficients[index,:,axis]
                # Preserve EVERY position quartic root from the actual stored
                # local polynomial. No sampling or global approximation here.
                roots=[float(min(1.,max(0.,r.real))) for r in position_roots[index*7+axis]
                       if abs(r.imag)<=1e-8*(1.+abs(r.real)) and -1e-10<=r.real<=1.+1e-10]
                positions=np.polynomial.polynomial.polyval([0.,1.,*roots],c)
                low[axis]=min(low[axis],float(np.min(positions)))
                high[axis]=max(high[axis],float(np.max(positions)))
                critical=[min(1.,max(0.,(s-s0)/width)) for s in global_velocity_critical[axis]
                          if s0-1e-12<=s<=s0+width+1e-12]
                speeds=np.polynomial.polynomial.polyval([0.,1.,*critical],dc)/dt
                vpeak[axis]=max(vpeak[axis],float(np.max(abs(speeds))))
        return low,high,vpeak,128*np.finfo(float).eps*arithmetic_scale

    def _global_residual(self):
        duration=(self.bridge_end_ns-int(self.times[0]))/NS
        base_velocity=(self.base_values[1]-self.base_values[0])/((int(self.times[1])-int(self.times[0]))/NS)
        dq=self.cut_anchor-self.base_values[0];dv=self.cut_velocity-base_velocity
        residual=np.zeros((6,7),float)
        residual[0]=dq;residual[1]=duration*dv
        residual[3]=-10*dq-6*duration*dv
        residual[4]=15*dq+8*duration*dv
        residual[5]=-6*dq-3*duration*dv
        return residual

    def correction_motion_extrema(self):
        # One global quintic,7axes: do not solve the same derivative roots
        # independently in all22coordinate-transformed intervals.
        velocity=np.zeros(7);acceleration=np.zeros(7)
        residual=self._global_residual();duration=(self.bridge_end_ns-int(self.times[0]))/NS
        for axis in range(7):
            dv=np.polynomial.polynomial.polyder(residual[:,axis])
            da=np.polynomial.polynomial.polyder(dv)
            dj=np.polynomial.polynomial.polyder(da)
            v=np.polynomial.polynomial.polyval([0.,1.,*unit_interval_roots(da)],dv)/duration
            a=np.polynomial.polynomial.polyval([0.,1.,*unit_interval_roots(dj)],da)/duration**2
            velocity[axis]=float(np.max(abs(v)))
            acceleration[axis]=float(np.max(abs(a)))
        return velocity,acceleration


def build_soft_bridge(request,raw,bounds,optimizer,*,step_budget_deg=THRESHOLD_DEG):
    """Build an immutable sealed candidate; input projection is explicit.

    Position limits apply to model targets before TimeQP and its float32 output,
    as in the previous adapter. Arm outbounds first try a shorter existing
    known-overlap quintic, then explicit pre-MPC reference projection. Gripper B
    retains its prior policy. Every accepted curve is rechecked; old cut q/v,
    the frozen prefix and physical feedback are never reset to pass.
    """
    started=time.perf_counter_ns();raw=array(raw,(50,7));bounds=array(bounds,(7,2))
    require(np.all(bounds[:,0]<bounds[:,1]),'Invalid bridge command bounds')
    require(request.wire()['enabled'] is True and len(request.previous)==KNOWN,
            'Residual bridge is only for actual soft-guided known35 requests')
    anchor=request.cut_anchor
    require(np.all(anchor>=bounds[:,0]) and np.all(anchor<=bounds[:,1]),'Accepted cut reference is outside bounds')
    old_velocity=request.old_plan.velocity(request.cut_phase_ns,'left')
    execution_cut_index=getattr(request,'execution_cut_index',FREEZE)
    require(type(execution_cut_index) is int and FREEZE<=execution_cut_index<KNOWN-1,
            'Execution cut must be an interior known-overlap label')
    require(request.cut_phase_ns==request.observation_phase_ns+label_ns(execution_cut_index),
            'Execution cut label and request phase disagree')
    projected=np.clip(raw[FREEZE:],bounds[:,0],bounds[:,1])
    optimized=np.asarray(optimizer.optimize(projected.tolist()),float)
    require(optimized.shape==(38,7) and np.isfinite(optimized).all(),'Official TimeQP output must remain38x7')
    full_base=np.clip(optimized,bounds[:,0],bounds[:,1])
    base=full_base[execution_cut_index-FREEZE:]
    times=np.array([request.observation_phase_ns+label_ns(i) for i in range(execution_cut_index,HORIZON+1)],dtype=np.int64)
    end=request.observation_phase_ns+label_ns(KNOWN-1)
    require(end<=request.old_plan.end,'Known overlap coverage is missing; do not extend/pad the old plan')
    curve=SoftGuidedBridgeCurve(times,np.vstack((base,base[-1])),anchor,old_velocity,int(end))
    low,high,vpeak,tolerance=curve.extrema()
    violation=np.maximum(bounds[:,0]-low,high-bounds[:,1])
    legacy_extrema={'position_min_rad_m':low.tolist(),'position_max_rad_m':high.tolist(),
                    'position_numeric_excess_rad_m':np.maximum(0.,violation).tolist()}
    bad_joints=tuple(int(i) for i in np.flatnonzero(violation[:6]>tolerance[:6]))
    gripper_transition={'strategy':'KEEP_LEGACY_C1','triggered':False,
        'trigger':'legacy G analytic outbounds; independent arm repair precedes final admission',
        'legacy_extrema':legacy_extrema,'legacy_six_joint_bounds_pass':not bad_joints,
        'six_joint_bounds_pass':not bad_joints,
        'physical_motion_authorized':False}
    if violation[6]>tolerance[6]:
        old_acceleration=plan_acceleration(request.old_plan,request.cut_phase_ns,'left')
        gripper_transition.update(strategy='APPROVED_C2_GRIPPER_B',triggered=True,
            inherited_cut_acceleration_rad_m_s2=old_acceleration.tolist())
        try:
            curve=build_c2_gripper_candidate(curve,old_acceleration,known_end_ns=int(end),bounds=bounds[6])
        except GripperCandidateRejected as exc:
            gripper_transition.update(status='REJECTED_NO_FEASIBLE_B',candidate=exc.diagnostics)
            raise GripperBridgeAdmissionError(
                'Analytic gripper extremum cannot be repaired by approved B: '+str(exc),gripper_transition) from exc
        b=curve.diagnostics
        gripper_transition.update(status='ACCEPTED_REFERENCE_ONLY',candidate=b)
        # B keeps arm bytes unchanged. Joint repair below certifies any arm
        # violations independently before the complete candidate can be used.
        gm=b['gripper_extrema']
        low[6]=gm['position']['min'];high[6]=gm['position']['max'];vpeak[6]=gm['velocity']['abs_peak']
        tolerance[6]=b['selected']['position_tolerance_m']
        violation=np.maximum(bounds[:,0]-low,high-bounds[:,1])
    joint_repair={'triggered':False,'strategy':'KEEP_LEGACY','legacy_extrema':legacy_extrema}
    if bad_joints:
        try:
            from .joint_boundary_repair import repair_joint_reference,policy
        except ImportError:
            from joint_boundary_repair import repair_joint_reference,policy
        curve,repaired=repair_joint_reference(curve,bounds,bad_joints,SoftGuidedBridgeCurve,
            raw_first_deg=np.rad2deg(raw[execution_cut_index+1,:6]-raw[execution_cut_index,:6]),
            step_budget_deg=step_budget_deg)
        joint_repair.update(repaired,triggered=True,policy=policy())
        low,high,vpeak,tolerance=curve.extrema()
        violation=np.maximum(bounds[:,0]-low,high-bounds[:,1])
        gripper_transition['six_joint_bounds_pass']=bool(np.all(violation[:6]<=tolerance[:6]))
    require(np.all(violation<=tolerance),
            'Analytic bridge position extremum outside command bounds; packet rejected without clipping: '+str(violation.tolist()))
    require(np.array_equal(curve.sample(request.cut_phase_ns),anchor),'Bridge changed frozen cut position')
    require(np.array_equal(curve.velocity(request.cut_phase_ns),old_velocity),'Bridge changed inherited cut velocity')
    history_start=max(request.old_plan.start,request.observation_phase_ns-200_000_000)
    prefix=request.old_plan.prefix(history_start,request.cut_phase_ns)
    plan=Plan(prefix+(Slice(request.cut_phase_ns,int(times[-1]),curve),),uuid.uuid4().hex)
    base_curve=Curve(times,np.vstack((base,base[-1])))
    probes=np.linspace(request.cut_phase_ns,end,129).astype(np.int64)
    correction_peak=np.max(abs(curve.sample(probes)-base_curve.sample(probes)),axis=0)
    correction_velocity,correction_acceleration=curve.correction_motion_extrema()
    bridge_labels=np.array([request.observation_phase_ns+label_ns(i) for i in range(execution_cut_index,KNOWN)],dtype=np.int64)
    bridge_steps=np.diff(curve.sample(bridge_labels),axis=0)
    base_steps=np.diff(base_curve.sample(bridge_labels),axis=0)
    extra_steps=np.maximum(0.,abs(bridge_steps)-abs(base_steps))
    diagnostics={'schema':'tiger-soft-guided-c1-with-approved-gripper-b-reference-v1',
        'model_response_shape':[50,7],'known_context_points':KNOWN,'frozen_intervals':FREEZE,
        'official_optimizer_input_rows':38,'official_optimizer_model_indices':[12,49],
        'execution_cut_index':execution_cut_index,'model_freeze_steps':FREEZE,
        'optimized_rows_discarded_before_execution_cut':execution_cut_index-FREEZE,
        'raw12_overwritten_before_timeqp':False,'temporal_tail_layer':'OFFICIAL_BASE_BEFORE_ANALYTIC_BRIDGE',
        'bridge_start_phase_ns':request.cut_phase_ns,'bridge_end_phase_ns':int(end),
        'bridge_label_interval':[execution_cut_index,34],'bridge_duration_s':(end-request.cut_phase_ns)/NS,
        'gripper_transition':gripper_transition,
        'gripper_bridge_end_phase_ns':int(getattr(curve,'gripper_end_ns',end)),
        'gripper_bridge_duration_s':(getattr(curve,'gripper_end_ns',end)-request.cut_phase_ns)/NS,
        'boundary_model_delta_rad_m':(raw[execution_cut_index]-anchor).tolist(),
        'boundary_base_delta_rad_m':(base[0]-anchor).tolist(),
        'inherited_cut_velocity_rad_m_s':old_velocity.tolist(),
        'base_cut_velocity_rad_m_s':base_curve.velocity(request.cut_phase_ns).tolist(),
        'cut_reference_velocity_jump_rad_m_s':(curve.velocity(request.cut_phase_ns)-old_velocity).tolist(),
        'model_target_projection_max_rad_m':np.max(abs(projected-raw[FREEZE:]),axis=0).tolist(),
        'timeqp_output_projection_max_rad_m':np.max(abs(full_base-optimized),axis=0).tolist(),
        'bridge_output_clipped':joint_repair.get('projection_corners',False),
        'joint_reference_repair':joint_repair,
        'velocity_peaks_are_upper_bounds':joint_repair.get('velocity_peaks_are_upper_bounds',False),
        'correction_derivatives_exclude_projection_contact_impulses':joint_repair.get('projection_corners',False),
        'position_min_rad_m':low.tolist(),'position_max_rad_m':high.tolist(),
        'position_numeric_tolerance_rad_m':tolerance.tolist(),
        'position_numeric_excess_rad_m':np.maximum(0.,violation).tolist(),
        'velocity_abs_peak_rad_m_s':vpeak.tolist(),
        'all_phase_20ms_position_step_upper_bound_rad_m':(vpeak*SEND_NS/NS).tolist(),
        'correction_peak_129sample_estimate_rad_m':correction_peak.tolist(),
        'correction_velocity_abs_peak_rad_m_s':correction_velocity.tolist(),
        'correction_acceleration_abs_peak_rad_m_s2':correction_acceleration.tolist(),
        'correction_derivatives_exclude_inherited_base_velocity_knots':True,
        'bridge_labels_cut_to34_max_extra_step_rad_m':np.max(extra_steps,axis=0).tolist(),
        'q_continuous_at_cut':True,'velocity_continuous_at_cut':True,
        'base_internal_knots_piecewise_linear':True,'global_C1_path_claimed':False,
        'bridge_end_additional_velocity_jump':False,'new_absolute_reference_restored_after_label34':True,
        'terminal_hold_interval_labels':[49,50],'physical_state_reset_performed':False,
        'prepare_ms':(time.perf_counter_ns()-started)/1e6}
    candidate=Candidate(request.token,request.old_plan.plan_id,request.revision,request.clock_epoch,
        plan,raw,array(full_base,(38,7)),diagnostics,'')
    if execution_cut_index==FREEZE:
        diagnostics['bridge_labels12_to34_max_extra_step_rad_m']=np.max(extra_steps,axis=0).tolist()
    return replace(candidate,seal=candidate_seal(candidate))
