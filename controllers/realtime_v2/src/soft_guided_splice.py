"""Necessary compatibility checks for soft-guided RTC rolling splices.

This is not a velocity/acceleration/jerk limiter or a physical safety proof.
It never shifts a suffix, clips feedback, changes a target, resets a state or
accepts an expired model response. The 5deg ceiling is inherited as a generous
incompatibility budget from the existing host tracking guard; it is not a Piper
rated limit. Valid smaller soft-guidance deviations need not equal the prefix.
"""
from copy import deepcopy
from dataclasses import replace
import numpy as np

from bootstrap_alignment import (BoundTemporalAdapter as OriginalBoundTemporalAdapter,
                                 validate_response)
from client_time_queue import ContractError, FREEZE, NS, label_ns

JOINT_SPLICE_BUDGET_DEG = 5.0
JOINT_COMMAND_GRID_DEG = .001


class SoftGuidedSpliceError(ContractError):
    def __init__(self, reason, diagnostics):
        self.splice_diagnostics = deepcopy(diagnostics)
        self.reason = reason
        maximum = diagnostics.get('raw12_max_abs_anchor_error_deg')
        extra = diagnostics.get('final_max_added_step_deg')
        super().__init__(f'{reason}: raw12_anchor_error_max_deg={maximum}, '
                         f'final_added_step_max_deg={extra}; old plan unchanged, no target offset')


class BoundTemporalAdapter(OriginalBoundTemporalAdapter):
    """Check model prefix agreement before solve and artificial step after it.

    Rejection returns no candidate. Caller decides between retaining the still
    valid old plan and a healthy controlled stop; it must not commit/relabel this
    response or silently retry it. A stopped/stale queue remains non-resumable.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_splice_diagnostics = None

    def _reject(self, reason, diagnostics):
        diagnostics.update(status='REJECTED_MODEL_REFERENCE_INCOMPATIBLE', reason=reason,
                           candidate_returned=False)
        self.last_splice_diagnostics = deepcopy(diagnostics)
        raise SoftGuidedSpliceError(reason, diagnostics)

    def prepare_response(self, request, response, *, evidence_kind):
        wire = request.wire()
        if wire.get('enabled') is not True:
            raise ContractError('Native no-prefix output must use NativeBootstrap and retain raw0..49')
        raw, _ = validate_response(wire, response, evidence_kind=evidence_kind)
        anchor = np.asarray(request.cut_anchor, dtype=np.float64)
        anchor_error = np.rad2deg(raw[FREEZE, :6] - anchor[:6])
        raw_step = np.rad2deg(raw[FREEZE+1, :6] - raw[FREEZE, :6])
        diagnostics = {
            'schema': 'tiger-soft-guided-splice-compatibility-v1',
            'status': 'CHECKING', 'request_index': wire['request_index'],
            'evidence_kind': evidence_kind, 'freeze_steps': wire['freeze_steps'],
            'previous_valid_steps': wire['previous_valid_steps'],
            'threshold_deg': JOINT_SPLICE_BUDGET_DEG,
            'threshold_source': 'Existing5deg host tracking budget reused only as a necessary compatibility ceiling',
            'not_a_physical_tracking_or_safety_admission': True,
            'raw12_anchor_error_deg': anchor_error.tolist(),
            'raw12_max_abs_anchor_error_deg': float(np.max(np.abs(anchor_error))),
            'raw12_to13_increment_deg': raw_step.tolist(),
            'gripper_raw12_anchor_error_mm': float((raw[FREEZE, 6]-anchor[6])*1000),
            'gripper_raw12_to13_increment_mm': float((raw[FREEZE+1, 6]-raw[FREEZE, 6])*1000),
            'gripper_new_threshold_applied': False,
            'targets_modified_by_guard': False, 'suffix_offset_applied': False,
            'reference_or_state_reset': False, 'old_plan_changed_by_guard': False,
            'candidate_returned': False,
        }
        if diagnostics['raw12_max_abs_anchor_error_deg'] > JOINT_SPLICE_BUDGET_DEG:
            self._reject('RAW12_DISAGREES_WITH_ACCEPTED_CUT_ANCHOR', diagnostics)

        # Original official temporal optimizer and all existing projections,
        # frozen-prefix construction and response validation remain unchanged.
        candidate = super().prepare_response(request, response, evidence_kind=evidence_kind)
        next_ns = request.observation_phase_ns + label_ns(FREEZE+1)
        interval_s = (next_ns-request.cut_phase_ns)/NS
        target = candidate.plan.sample(next_ns)
        joined_step = np.rad2deg(target[:6]-anchor[:6])
        added_step = np.maximum(0., np.abs(joined_step)-np.abs(raw_step))
        ratios = [float(abs(join)/abs(original)) if abs(original)>JOINT_COMMAND_GRID_DEG else None
                  for join, original in zip(joined_step, raw_step)]
        diagnostics.update(
            final_first_interval_s=float(interval_s),
            final_join_increment_deg=joined_step.tolist(),
            final_added_step_deg=added_step.tolist(),
            final_max_added_step_deg=float(np.max(added_step)),
            raw_increment_velocity_deg_s=(raw_step*18.).tolist(),
            final_join_velocity_deg_s=(joined_step/interval_s).tolist(),
            increment_amplification_ratio_diagnostic_only=ratios,
            ratio_denominator_floor_deg=JOINT_COMMAND_GRID_DEG,
            ratio_threshold_applied=False,
            gripper_final_join_increment_mm=float((target[6]-anchor[6])*1000),
        )
        if diagnostics['final_max_added_step_deg'] > JOINT_SPLICE_BUDGET_DEG:
            self._reject('TEMPORAL_SPLICE_ADDS_MORE_THAN5DEG_IN_ONE_REFERENCE_INTERVAL', diagnostics)
        diagnostics.update(status='COMPATIBLE_REFERENCE_NOT_PHYSICAL_ADMISSION', candidate_returned=True)
        self.last_splice_diagnostics = deepcopy(diagnostics)
        combined = dict(candidate.diagnostics)
        combined['soft_guided_splice_compatibility'] = deepcopy(diagnostics)
        return replace(candidate, diagnostics=combined)
