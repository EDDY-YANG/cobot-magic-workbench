"""Soft-guided RTC adapter: explicit finite reference bridge, original MPC.

The old mismatch remains diagnostic evidence. Analytic position checks remain
admission requirements. Step inflation above the approved 10deg threshold is
warning-only; physical tracking has a separate approved diagnostic policy.
No physical claim here.
"""
from dataclasses import replace
from copy import deepcopy
import numpy as np
from bootstrap_alignment import BoundTemporalAdapter as OriginalAdapter,validate_response
from client_time_queue import ContractError,FREEZE,KNOWN,label_ns
try:
    from .soft_guided_bridge import build_soft_bridge
    from .bridge_step_policy import THRESHOLD_DEG,assess
except ImportError:
    from soft_guided_bridge import build_soft_bridge
    from bridge_step_policy import THRESHOLD_DEG,assess

LEGACY_ANCHOR_DIAGNOSTIC_DEG=5.

class BridgeAdmissionError(ContractError):
    def __init__(self,message,diagnostics):
        self.splice_diagnostics=deepcopy(diagnostics)
        super().__init__(message)

class BoundTemporalAdapter(OriginalAdapter):
    def prepare_response(self,request,response,*,evidence_kind):
        wire=request.wire()
        if wire.get('enabled') is not True:
            raise ContractError('No-prefix first response must retain all H50 through NativeBootstrap')
        raw,binding=validate_response(wire,response,evidence_kind=evidence_kind)
        cut_index=getattr(request,'execution_cut_index',FREEZE)
        delta=np.rad2deg(raw[cut_index,:6]-request.cut_anchor[:6])
        d={'schema':'tiger-soft-guided-bridge-admission-v1','evidence_kind':evidence_kind,
           'request_index':wire['request_index'],'freeze_steps':wire['freeze_steps'],
           'previous_valid_steps':wire['previous_valid_steps'],
           'execution_cut_index':cut_index,
           'raw_execution_cut_anchor_error_deg':delta.tolist(),
           'raw_execution_cut_max_abs_anchor_error_deg':float(np.max(abs(delta))),
           'raw_guard_would_reject':bool(np.max(abs(delta))>LEGACY_ANCHOR_DIAGNOSTIC_DEG),
           'raw_guard_enforced':False,'raw_guard_diagnostic_threshold_deg':LEGACY_ANCHOR_DIAGNOSTIC_DEG,
           'added_step_budget_deg':THRESHOLD_DEG,'added_step_action':'diagnostic_only','physical_tracking_guard_changed':False,
           'physical_tracking_validated':False,'old_plan_changed_by_guard':False,
           'permanent_suffix_offset_applied':False,'reference_or_state_reset':False,
           'bridge_applied':False,'candidate_returned':False}
        if cut_index==FREEZE:
            d.update(raw12_anchor_error_deg=delta.tolist(),raw12_max_abs_anchor_error_deg=float(np.max(abs(delta))))
        try:
            candidate=build_soft_bridge(request,raw,self.bounds,self.optimizer,
                                        step_budget_deg=THRESHOLD_DEG)
            curve=candidate.plan.pieces[-1].curve
            times=np.array([request.observation_phase_ns+label_ns(i) for i in range(cut_index,KNOWN)],np.int64)
            bridge_step=np.rad2deg(np.diff(curve.sample(times)[:,:6],axis=0))
            base_step=np.rad2deg(np.diff(curve.base.sample(times)[:,:6],axis=0))
            extra=np.maximum(0.,abs(bridge_step)-abs(base_step))
            raw_first=np.rad2deg(raw[cut_index+1,:6]-raw[cut_index,:6])
            first_extra=np.maximum(0.,abs(bridge_step[0])-abs(raw_first))
            d.update(bridge_applied=True,overlap_intervals_checked=len(extra),
                final_max_added_step_deg=float(np.max(extra)),
                max_added_step_deg_by_joint=np.max(extra,axis=0).tolist(),
                first_extra_vs_raw_execution_interval_deg=first_extra.tolist(),
                first_bridged_step_deg=bridge_step[0].tolist(),
                first_base_step_deg=base_step[0].tolist())
            if cut_index==FREEZE:
                d['first_extra_vs_raw12_to13_deg']=first_extra.tolist()
            d['step_warning']=assess(np.max(extra,axis=0),first_extra)
        except Exception as exc:
            if hasattr(exc,'gripper_transition_diagnostics'):
                d['gripper_transition']=deepcopy(exc.gripper_transition_diagnostics)
            d.update(status='REJECTED_REFERENCE_BRIDGE',reason=type(exc).__name__+': '+str(exc))
            self.last_splice_diagnostics=deepcopy(d)
            raise BridgeAdmissionError(d['reason'],d) from exc
        d.update(status='COMPATIBLE_BRIDGE_NOT_PHYSICAL_ADMISSION',candidate_returned=True)
        diagnostics=dict(candidate.diagnostics)
        diagnostics.update(response_binding=binding,evidence_kind=evidence_kind,
            soft_guided_splice_compatibility=d,
            live_model_validation=evidence_kind=='LIVE_NO_SEND_MODEL_RESPONSE')
        if getattr(self.optimizer,'last_receipt',None) is not None:
            diagnostics['timeqp_rpc']=dict(self.optimizer.last_receipt)
        self.last_splice_diagnostics=deepcopy(d)
        return replace(candidate,diagnostics=diagnostics)
