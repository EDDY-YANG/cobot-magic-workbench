"""Portable V2 NO_SEND core. Remote TimeQP is explicit; no actuator exists.

All state units: rad, rad, rad, rad, rad, rad, metres. Time: host monotonic ns.
prepare_request returns a real-model RTC condition built from an explicitly
DIAGNOSTIC_UNSENT_REFERENCE queue, not a claim of physical execution. The IO
runner must bind model identity, observation bytes, response ID and clock host.
"""
from pathlib import Path
from dataclasses import dataclass, replace
from functools import partial
import math
import sys
import threading
import time
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / 'vendor'
sys.path.insert(0, str(VENDOR))
from pinned_components import verify_all
SOURCE_MANIFEST = verify_all()
from client_time_queue import (ContractError, StopRequired, SEND_NS, NS, label_ns,
                               require, array)
from bootstrap_alignment import (NativeBootstrap, GuidedBootstrapQueue,
                                 RTC, prepared_seal)
if __package__:
    from .continuity_adapter import BoundTemporalAdapter
    from .position_contract import resolve_position_contract
    from .request_lifecycle import ReferenceCaptureDeferred
else:
    from continuity_adapter import BoundTemporalAdapter
    from position_contract import resolve_position_contract
    from request_lifecycle import ReferenceCaptureDeferred
from portable_backend import OFFICIAL, ShadowPlanner, BUILD_RECEIPTS


@dataclass(frozen=True)
class PreparedCandidate:
    """RAM-only handoff. Persist timings/diagnostics, not this object graph."""
    context: object
    candidate: object
    first_native: bool
    model_ready_ns: int
    prepare_finished_ns: int
    evidence_kind: str


class V2Core:
    """Single-run NO_SEND evaluator; no hidden retry, reset or fake actuator.

    Build/warm the solver before acquiring the initial observation. A caller
    provides one advancing feedback clock and actual timestamps. Construction
    initializes the controller once; first-result activation does not reset it.
    """
    def __init__(self, anchor_rad_m, bounds_rad_m, start_ns, *, clock_domain,
                 mode='native_first', trial_index=0, solver_dir,
                 tau=.03611058236938804, weight=200., gripper_lookahead_ms=0.,
                 now_fn=time.monotonic_ns, allow_solver_build=False,
                 timeqp_instance_id=None):
        require(mode in ('native_first', 'guided_hold35'), 'unknown bootstrap mode')
        require(gripper_lookahead_ms in (0., 90.), 'only direct / official 90ms comparison is exposed')
        self.anchor = array(anchor_rad_m, (7,))
        self.position_contract = resolve_position_contract(bounds_rad_m)
        self.bounds = self.position_contract.bounds_rad_m
        self.codec = self.position_contract.codec
        self._check_feedback(self.anchor)
        self.start_ns = int(start_ns)
        require(self.start_ns > 0, 'positive real monotonic start required')
        self.domain = tuple(clock_domain)
        require(len(self.domain) == 3 and all(isinstance(x, str) and x for x in self.domain), 'host/boot/clock identity required')
        self.mode, self.trial_index = mode, trial_index
        self.now_fn = now_fn
        self.lock = threading.RLock()
        self.status = 'RUNNING_NO_SEND'
        self.queue = None
        self.native = None
        self.pending = None
        self._preparing_context = None
        self._prepared_bundle = None
        self.prepared_native = None
        self.activation_due_ns = None
        self.last_tick_ns = None
        self.last_feedback_ns = None
        self.last_feedback = None
        self.last_feedback_revision = None
        self.last_sent_ns = None
        self.actual_sent_records = 0
        self.accepted_count = 0
        self.tick_count = 0
        self.observed_activation_ns = None
        self.gripper_lookahead_ms = gripper_lookahead_ms
        cfg = OFFICIAL.MPCConfig(nq=6, dt=.02, N=15, tau=float(tau),
            q_min=self.bounds[:6, 0], q_max=self.bounds[:6, 1], w_track0=10., w_ddy=float(weight))
        self.planner = ShadowPlanner(cfg, solver_dir, self.anchor[:6], allow_build=allow_solver_build)
        self.estimator = OFFICIAL.ReplayEstimator(6, tau)
        # No fabricated initial sent command enters this estimator.
        self.timeqp_instance_id = timeqp_instance_id
        self.optimizer_factory = None
        if timeqp_instance_id is not None:
            require(isinstance(timeqp_instance_id,str) and len(timeqp_instance_id)==32 and
                    all(x in '0123456789abcdef' for x in timeqp_instance_id),
                    'Explicit frozen TimeQP server instance required')
            if __package__:
                from .temporal_rpc import RemoteTimeParameterizationMPC
            else:
                from temporal_rpc import RemoteTimeParameterizationMPC
            self.optimizer_factory = partial(RemoteTimeParameterizationMPC,
                                              expected_instance_id=timeqp_instance_id)
        self.temporal = BoundTemporalAdapter(self.bounds,optimizer_factory=self.optimizer_factory)
        try:
            from .joint_boundary_repair import ReferenceRepairWatchdog
        except ImportError:
            from joint_boundary_repair import ReferenceRepairWatchdog
        self.reference_repair_watchdog = ReferenceRepairWatchdog()
        if mode == 'guided_hold35':
            self.queue = GuidedBootstrapQueue(self.anchor, self.bounds, self.start_ns,
                trial_index=trial_index, clock_domain=self.domain, mode='NO_SEND_SHADOW')

    def _check_feedback(self, q):
        require(np.all(q >= self.bounds[:, 0]) and np.all(q <= self.bounds[:, 1]),
                'real feedback out of position bounds; do not clip/reset')

    def prepare_request(self, observation_ns, now_ns):
        with self.lock:
            require(self.status == 'RUNNING_NO_SEND' and self.pending is None, 'no second pending request or restart')
            require(type(observation_ns) is int and type(now_ns) is int and
                    0 <= now_ns-observation_ns <= 200_000_000, 'stale/future observation')
            if self.mode == 'native_first' and self.native is None:
                self.native = NativeBootstrap(self.anchor, self.bounds, observation_ns,
                    trial_index=self.trial_index, clock_domain=self.domain, sender_epoch_ns=self.start_ns,
                    optimizer_factory=self.optimizer_factory)
                self.pending = self.native.ticket
            else:
                require(self.queue is not None, 'native queue awaits activation')
                if self.queue.clock.paused and self.queue.coverage_hold is None:
                    after = self.queue.resume_capture_after_ns
                    if after is None or observation_ns < after:
                        raise ReferenceCaptureDeferred('Capture crossed a reference pause', self.queue.clock.epoch)
                self.pending = self.queue.request(observation_ns, now_ns, clock_domain=self.domain)
            return {'token': self.pending.token, 'rtc': self.pending.wire(),
                'observation_ns': observation_ns, 'deadline_ns': self.pending.deadline_ns,
                'execution_cut_index': getattr(self.pending,'execution_cut_index',None),
                'model_freeze_steps': 12, 'known_context_steps': 35,
                'reference_semantics': 'DIAGNOSTIC_UNSENT_REFERENCE',
                'physical_execution_claimed': False}

    def prepare_candidate(self, token, response, ready_ns, *, now_ns=None,
                          evidence_kind='LIVE_NO_SEND_MODEL_RESPONSE'):
        """Background worker: validate context, then perform TimeQP without lock.

        Caller verifies actual model identity and response provenance. An
        explicit synthetic-clock override is accepted only for CPU fixtures.
        A cancelled/expired context may finish CPU preparation, but cannot be
        published by commit_candidate. The saved reference/state is not reset.
        """
        with self.lock:
            require(self.status == 'RUNNING_NO_SEND' and self.pending is not None, 'no outstanding request')
            require(token == self.pending.token, 'response token mismatch/replay')
            require(evidence_kind in ('LIVE_NO_SEND_MODEL_RESPONSE', 'SYNTHETIC_PROTOCOL_FIXTURE'), 'invalid response provenance')
            require(now_ns is None or evidence_kind=='SYNTHETIC_PROTOCOL_FIXTURE',
                    'Only explicit CPU fixtures may override the preparation clock')
            require(self._preparing_context is None and self._prepared_bundle is None,
                    'Only one candidate preparation may be in flight')
            context = self.pending
            require(type(ready_ns) is int, 'Actual model-ready nanoseconds required')
            require(context.observation_ns <= ready_ns <= context.deadline_ns, 'real model result missed800ms')
            first_native = self.native is not None and self.native.status == 'WAITING_FIRST_RESULT'
            self._preparing_context = context
        try:
            if first_native:
                candidate = self.native.prepare(response, evidence_kind=evidence_kind)
            else:
                candidate = self.temporal.prepare_response(context, response, evidence_kind=evidence_kind)
            finished_ns = int(self.now_fn()) if now_ns is None else int(now_ns)
            require(ready_ns <= finished_ns <= context.deadline_ns, 'temporal preparation missed800ms')
            if first_native:
                candidate=replace(candidate,not_before_ns=finished_ns,seal='')
                candidate=replace(candidate,seal=prepared_seal(candidate))
            bundle=PreparedCandidate(context,candidate,first_native,ready_ns,finished_ns,evidence_kind)
            with self.lock:
                require(self.pending is context, 'Pending request changed during preparation')
                self._prepared_bundle=bundle
            return bundle
        finally:
            with self.lock:
                if self._preparing_context is context:
                    self._preparing_context=None

    def commit_candidate(self, token, candidate_bundle, *, now_ns=None):
        """Control thread: no network/TimeQP; check current clock and publish."""
        with self.lock:
            require(self.status=='RUNNING_NO_SEND' and self.pending is not None,'no outstanding live request')
            require(isinstance(candidate_bundle,PreparedCandidate) and
                    self._prepared_bundle is candidate_bundle,'candidate was not prepared by this run or already consumed')
            bundle=candidate_bundle;context=bundle.context;candidate=bundle.candidate
            require(token==context.token and self.pending is context,'prepared candidate token/context mismatch')
            require(self._preparing_context is None,'preparation has not finished')
            require(now_ns is None or bundle.evidence_kind=='SYNTHETIC_PROTOCOL_FIXTURE',
                    'Only explicit CPU fixtures may override the commit clock')
            committed_ns=int(self.now_fn()) if now_ns is None else int(now_ns)
            require(context.observation_ns<=bundle.model_ready_ns<=bundle.prepare_finished_ns<=committed_ns<=context.deadline_ns,
                    'actual TimeQP completion or publication missed800ms')
            require(self.last_tick_ns is None or committed_ns>=self.last_tick_ns,'publication cannot precede sampled control tick')
            if bundle.first_native:
                require(self.native is not None and self.native.status=='WAITING_FIRST_RESULT' and
                        self.native.ticket is context,'native bootstrap no longer accepts this response')
                require(candidate.seal==prepared_seal(candidate),'native prepared candidate mutated')
                # Initialize reference phase once, on an existing20ms grid.
                activation=self.start_ns+max(0,(committed_ns-self.start_ns+SEND_NS-1)//SEND_NS)*SEND_NS
                require(activation<=context.deadline_ns,'next control tick misses800ms')
                self.prepared_native=candidate
                self.activation_due_ns=activation
            else:
                self.queue.commit(context,candidate,committed_ns)
            self.pending = None
            self._prepared_bundle = None
            self.accepted_count += 1
            return {'accepted': True, 'request_index': context.wire()['request_index'],
                'accepted_count': self.accepted_count, 'evidence_kind': bundle.evidence_kind,
                'model_ready_ns':bundle.model_ready_ns,'prepare_finished_ns':bundle.prepare_finished_ns,
                'commit_monotonic_ns':committed_ns,'timeqp_remote':self.timeqp_instance_id is not None,
                'activation_due_ns': self.activation_due_ns,
                'reference_semantics': 'DIAGNOSTIC_UNSENT_REFERENCE',
                'physical_dispatch_performed': False, 'temporal': candidate.diagnostics}

    def accept_response(self, token, response, ready_ns, *, now_ns=None,
                        evidence_kind='LIVE_NO_SEND_MODEL_RESPONSE'):
        """CPU-compatible synchronous wrapper; live runner uses two phases."""
        bundle=self.prepare_candidate(token,response,ready_ns,now_ns=now_ns,evidence_kind=evidence_kind)
        return self.commit_candidate(token,bundle,now_ns=now_ns)

    def record_actual_sent_command(self, sent_ns, command_rad_m, *, evidence):
        """Optional imported ACTUAL external history, never our diagnostic cmd.

        This function does not send anything. Runner must never call it with a
        virtual sink receipt; sequence/owner evidence is mandatory.
        """
        with self.lock:
            require(isinstance(evidence, dict) and evidence.get('physically_sent') is True
                    and evidence.get('source') and evidence.get('record_id'), 'actual sent-command evidence required')
            q = array(command_rad_m, (7,))
            self._check_feedback(q)
            require(type(sent_ns) is int and sent_ns >= self.start_ns and
                    (self.last_sent_ns is None or sent_ns > self.last_sent_ns), 'actual command clock must advance')
            self.estimator.push_command((sent_ns-self.start_ns)/NS, q[:6])
            self.planner.record_actual_command(q[:6])
            self.last_sent_ns = sent_ns
            self.actual_sent_records += 1

    def tick(self, now_ns, feedback_rad_m, feedback_ns, *, feedback_revision=None):
        """Evaluate fresh actual feedback; no HTTP-time substitution.

        For asynchronous CAN aggregates, feedback_revision is the fixed-order
        (q12,q34,q56,gripper) source timestamp tuple, or a verified SHA256 of it.
        The oldest component time remains the age/estimator anchor. Different
        revisions may legitimately have the same oldest component timestamp.
        """
        with self.lock:
            require(self.status == 'RUNNING_NO_SEND', 'core has stopped; explicit new run required')
            started = time.perf_counter_ns()
            try:
                require(type(now_ns) is int and type(feedback_ns) is int and
                        0 <= now_ns-feedback_ns <= 200_000_000, 'stale/future feedback')
                require(self.last_tick_ns is None or 0 < now_ns-self.last_tick_ns <= 100_000_000, 'control clock reversed or gap>100ms')
                require(self.last_feedback_ns is None or feedback_ns >= self.last_feedback_ns, 'feedback clock reversed')
                q = array(feedback_rad_m, (7,))
                self._check_feedback(q)
                if feedback_revision is None:
                    revision = ('aggregate_source_timestamp',feedback_ns)
                elif isinstance(feedback_revision,tuple):
                    require(len(feedback_revision)==4 and all(type(x) is int and
                        feedback_ns<=x<=now_ns for x in feedback_revision)
                        and min(feedback_revision)==feedback_ns,
                        'four source timestamps must agree with oldest feedback time')
                    revision = feedback_revision
                else:
                    require(isinstance(feedback_revision,str) and len(feedback_revision)==64 and
                        all(x in '0123456789abcdef' for x in feedback_revision),
                        'feedback revision must be four timestamps or their verified SHA256')
                    revision = feedback_revision
                if revision != self.last_feedback_revision:
                    # ReplayEstimator accepts a new real vector at the same
                    # conservative aggregate time; no time is fabricated.
                    self.estimator.push_observation((feedback_ns-self.start_ns)/NS, q[:6])
                    self.last_feedback_ns = feedback_ns
                    self.last_feedback = q.copy()
                    self.last_feedback_revision = revision
                else:
                    require(feedback_ns==self.last_feedback_ns and np.array_equal(q,self.last_feedback),
                            'same feedback revision has changed time or values')
                self.estimator.step_contact_decay()
                qhat = self.estimator.estimate_now((now_ns-self.start_ns)/NS)
                if self.native is not None and self.native.status == 'WAITING_FIRST_RESULT':
                    self.native.expire(now_ns)
                    if self.prepared_native is not None and now_ns >= self.activation_due_ns:
                        self.queue = self.native.activate(self.prepared_native, self.activation_due_ns, clock_domain=self.domain)
                        self.prepared_native = None
                        self.observed_activation_ns = now_ns
                if self.queue is None:
                    future = np.tile(self.anchor, (15, 1))
                    phase = None
                else:
                    self.queue.expire(now_ns)
                    sample = self.queue.sample_for_mpc(now_ns)
                    future = sample['reference_rad_m']
                    phase = sample['reference_phase_ns']
                repair_observation=self.reference_repair_watchdog.observe(now_ns,
                    self.queue.plan if self.queue is not None else None,phase)
                out, predicted, timing = self.planner.evaluate(qhat, future[:, :6], self.estimator.contact_flag)
                # Official heartbeat lookahead rounds90ms to5 control ticks.
                # Default0 follows the common-time-axis VLA gripper reference.
                grip_index = int(math.ceil(self.gripper_lookahead_ms/20.))
                grip = float(future[min(grip_index, len(future)-1), 6])
                encoded = self.codec.encode(np.r_[out.y_cmd, grip])
                if self.queue is not None:
                    self.queue.apply_mpc_alpha(out.alpha, now_ns)
                self.last_tick_ns = now_ns
                self.tick_count += 1
                return {'now_ns': now_ns, 'feedback_ns': feedback_ns,
                    'feedback_rad_m': q.tolist(), 'qhat_rad': qhat.tolist(),
                    'feedback_revision': revision,
                    'reference_rad_m': future[0].tolist(), 'reference_phase_ns': phase,
                    'reference_boundary_repair':repair_observation,
                    'diagnostic_command_rad_m': encoded.target_rad_m.tolist(),
                    'joint_mdeg': list(encoded.joint_mdeg), 'gripper_um': encoded.gripper_um,
                    'counterfactual_predicted_next_q_rad': predicted.tolist(),
                    'physical_dispatch_performed': False, 'command_is_unsent': True,
                    'reference_semantics': 'DIAGNOSTIC_UNSENT_REFERENCE',
                    'gripper_lookahead_ms': self.gripper_lookahead_ms,
                    'gripper_actual_lookahead_ms': grip_index*20.,
                    'estimator_actual_sent_history_count': len(self.estimator.cmd_hist),
                    'actual_sent_records': self.actual_sent_records,
                    'initial_command_history_basis': 'ASSUMED_HOLD_AT_MEASURED_ANCHOR_NOT_CONTROLLER_READBACK',
                    'scheduled_reference_activation_ns': self.activation_due_ns,
                    'observed_activation_tick_ns': self.observed_activation_ns,
                    'estimator_without_command_history': self.actual_sent_records == 0,
                    'controller_initializations': self.planner.reset_count,
                    'contact_flag': bool(self.estimator.contact_flag), 'alpha': float(out.alpha),
                    'mpc': timing, 'cycle_compute_ms': (time.perf_counter_ns()-started)/1e6}
            except Exception:
                self.status = 'FAULTED_NO_SEND'
                if self.queue is not None:
                    self.queue.abort('CORE_TICK_FAILED')
                raise

    def next_request_due_ns(self, now_ns):
        with self.lock:
            if self.status not in ('RUNNING_NO_SEND','RUNNING_PHYSICAL') or self.queue is None or self.pending is not None:
                return None
            return self.queue.next_capture_due_ns(now_ns)

    def drain_diagnostic_pause(self, request, result):
        """Discard only the validated NO_SEND response owned by the paused epoch."""
        with self.lock:
            if self.status != 'RUNNING_NO_SEND' or self.queue is None or self.queue.draining is None:
                return None
            ctx = self.queue.draining
            require(self.pending is ctx and ctx.token == request.get('token') and
                    self.queue.pending is None and self.queue.clock.paused and
                    ctx.clock_epoch != self.queue.clock.epoch, 'unowned diagnostic pause response')
            require(result.get('model_response_validated') is True and result.get('deadline_met') is True
                    and not result.get('cancelled_result') and not result.get('pipeline_error')
                    and not result.get('timeqp_preparation_error') and self._preparing_context is None
                    and bool(request.get('observation_sha256'))
                    and request['observation_sha256'] == result.get('observation_sha256'),
                    result.get('pipeline_error') or result.get('timeqp_preparation_error')
                    or 'invalid diagnostic response during pause')
            self.queue.acknowledge_response_drained(ctx,evidence_kind='LIVE_NO_SEND_MODEL_RESPONSE')
            self.pending = self._prepared_bundle = None
            return {'event':'DIAGNOSTIC_PAUSE_DRAINED_WITHOUT_COMMIT', 'request_index':ctx.index,
                    'request_epoch':ctx.clock_epoch, 'paused_epoch':self.queue.clock.epoch}

    @property
    def coverage_hold(self):
        with self.lock:
            return None if self.queue is None else self.queue.coverage_hold

    def stop(self, reason='USER_STOP'):
        """Stop CPU evaluation; IO caller must cancel/drain its model request."""
        with self.lock:
            if self.queue is not None:
                self.queue.abort(reason)
            if self.native is not None and self.native.status == 'WAITING_FIRST_RESULT':
                self.native.cancel()
            token = None if self.pending is None else self.pending.token
            self.status = 'STOPPED_NO_SEND'
            return {'status': self.status, 'pending_request_token': token,
                'reference_boundary_repair':self.reference_repair_watchdog.summary(),
                'model_response_drain_required': token is not None,
                'physical_stop_performed': False, 'physical_dispatch_count': 0}
