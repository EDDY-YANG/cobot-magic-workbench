"""Physical V2 adapter: same MPC, actual-write acknowledgements, constant-reference settle.

This file owns no SDK/socket/model. Its caller must prove writer ownership,
acknowledge actual successful joint sends, and separately drain model workers.
The inherited pure queue/bootstrap mathematics are reused without resetting
states; outgoing lifecycle evidence always identifies the PHYSICAL route.
"""
from dataclasses import replace
from copy import deepcopy
import math
import time
import threading
import uuid
import numpy as np
try:
    from .request_lifecycle import ReferenceCaptureDeferred, ReferencePauseInvalidated
except ImportError:
    from request_lifecycle import ReferenceCaptureDeferred, ReferencePauseInvalidated
try:
    from .v2_core import (V2Core,PreparedCandidate,NativeBootstrap,require,array,
                          prepared_seal,SEND_NS,NS,OFFICIAL,BoundTemporalAdapter,GuidedBootstrapQueue)
except ImportError:
    from v2_core import (V2Core,PreparedCandidate,NativeBootstrap,require,array,
                         prepared_seal,SEND_NS,NS,OFFICIAL,BoundTemporalAdapter,GuidedBootstrapQueue)

from functools import partial
try:
    from .joint_boundary_repair import ReferenceRepairWatchdog
except ImportError:
    from joint_boundary_repair import ReferenceRepairWatchdog
try:
    from .v2_physical_planner import PhysicalPlanner
    from .position_contract import resolve_position_contract
except ImportError:
    from v2_physical_planner import PhysicalPlanner
    from position_contract import resolve_position_contract

class V2PhysicalCore(V2Core):
    def __init__(self, anchor_rad_m, bounds_rad_m, start_ns, *, clock_domain,
                 mode='native_first', trial_index=0, solver_dir,
                 tau=.03611058236938804, weight=200., gripper_lookahead_ms=0.,
                 now_fn=time.monotonic_ns, allow_solver_build=False,
                 timeqp_instance_id=None,protocol_fixture=False,manual_speed=None):
        require(mode in ('native_first', 'guided_hold35'), 'unknown bootstrap mode')
        require(gripper_lookahead_ms in (0., 90.), 'only direct / official 90ms comparison is exposed')
        self.measured_initial_state = array(anchor_rad_m, (7,))
        self.position_contract = resolve_position_contract(bounds_rad_m)
        self.bounds = self.position_contract.bounds_rad_m
        self.codec = self.position_contract.codec
        self._check_feedback(self.measured_initial_state)
        initial_reference=self.codec.encode(self.measured_initial_state)
        self.anchor=array(initial_reference.target_rad_m,(7,))
        self.initialization_receipt={
            'measured_state_rad_m':self.measured_initial_state.tolist(),
            'initial_reference_rad_m':self.anchor.tolist(),
            'reference_minus_state_rad_m':(self.anchor-self.measured_initial_state).tolist(),
            'reference_only_position_projection':True,
            'position_contract':self.position_contract.receipt(),
            'state_clipped':False,'physical_state_reset':False,
            'mpc_initial_state':'EXACT_MEASURED_SIX_JOINT_STATE',
            'initial_command_history':'ASSUMED_HOLD_AT_MEASURED_STATE_NOT_CONTROLLER_READBACK'}
        self.start_ns = int(start_ns)
        require(self.start_ns > 0, 'positive real monotonic start required')
        self.domain = tuple(clock_domain)
        require(len(self.domain) == 3 and all(isinstance(x, str) and x for x in self.domain), 'host/boot/clock identity required')
        self.mode, self.trial_index = mode, trial_index
        self.now_fn = now_fn
        self.lock = threading.RLock()
        self.status = 'RUNNING_PHYSICAL'
        self.queue = None
        self.native = None
        self.pending = None
        self.manual_speed = manual_speed
        self.reference_repair_watchdog = ReferenceRepairWatchdog()
        require(manual_speed is None or (callable(getattr(manual_speed,'freeze_for_request',None)) and
                callable(getattr(manual_speed,'mark_committed',None))), 'Manual speed controller interface required')
        self._pending_manual_speed = None
        self._prepared_manual_stats = None
        self._preparing_context = None
        self._prepared_bundle = None
        self._pause_invalidated_context = None
        self._pause_started_ns = None
        self._last_requested_mpc_alpha = 1.
        self._recovery_schedule_active = False
        self._request_not_before_ns = 0
        self._pause_discard_count = 0
        self._capture_deferred_count = 0
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
        self.planner = PhysicalPlanner(cfg, solver_dir, self.measured_initial_state[:6], allow_build=allow_solver_build)
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
        elif manual_speed is not None:
            if __package__:
                from .manual_timeqp import local_target_optimizer
            else:
                from manual_timeqp import local_target_optimizer
            self.optimizer_factory = local_target_optimizer
        self.temporal = BoundTemporalAdapter(self.bounds,optimizer_factory=self.optimizer_factory)
        if mode == 'guided_hold35':
            self.queue = GuidedBootstrapQueue(self.anchor, self.bounds, self.start_ns,
                trial_index=trial_index, clock_domain=self.domain, mode='NO_SEND_SHADOW')
        require(type(protocol_fixture) is bool,'Explicit boolean fixture mode required')
        self.protocol_fixture=protocol_fixture
        self.execution_scope='CPU_PHYSICAL_PROTOCOL_FIXTURE' if protocol_fixture else 'REAL_PHYSICAL_SESSION'
        self.session_id=uuid.uuid4().hex
        self.command_sequence=0
        self._joint_pending=None
        self._gripper_pending=None
        self._last_joint_ack=None
        self._last_gripper_command=float(self.measured_initial_state[6])
        self.gripper_ack_count=0
        self.settle_reference=None
        self.settle_started_ns=None
        self.fault_reason=None

    def _check_feedback(self,q):
        require(np.asarray(q).shape==(7,) and np.isfinite(q).all(),
                'Finite actual feedback required; command u bounds do not constrain measured state')


    def _physical_diagnostics(self,value,evidence_kind):
        # Imported arithmetic adapters accept archived real responses. The
        # outer caller binds the current live response; report that scope.
        result=deepcopy(value)
        if 'response_binding' in result:
            result['response_binding']['evidence_kind']=evidence_kind
            result['response_binding']['new_model_inference_performed']=evidence_kind=='LIVE_PHYSICAL_MODEL_RESPONSE'
        if 'external_measured_anchor_rows' in result:
            result['external_initial_reference_anchor_rows']=result.pop('external_measured_anchor_rows')
            result['external_anchor_semantics']='PROJECTED_POSITION_REFERENCE_NOT_MEASURED_STATE'
        result['initialization']=deepcopy(self.initialization_receipt)
        result['execution_scope']=self.execution_scope
        result['execution_route']='PHYSICAL'
        result['evidence_kind']=evidence_kind
        result['live_model_validation']=evidence_kind=='LIVE_PHYSICAL_MODEL_RESPONSE'
        result['reference_semantics']='PHYSICAL_ACCEPTED_PRE_MPC_REFERENCE'
        return result

    def prepare_request(self, observation_ns, now_ns):
        with self.lock:
            require(self.status == 'RUNNING_PHYSICAL' and self.pending is None, 'no second pending request or restart')
            require(self._pause_invalidated_context is None,'previous paused response still draining')
            if self.queue is not None and self.queue.coverage_hold is None and (
                    (self.queue.clock.paused and (self.queue.resume_capture_after_ns is None
                        or observation_ns < self.queue.resume_capture_after_ns)) or
                    (self._recovery_schedule_active and observation_ns < self.queue.clock.wall_anchor_ns)):
                raise ReferenceCaptureDeferred('Capture crossed an owned reference pause; acquire fresh observation',
                                               self.queue.clock.epoch)
            require(type(observation_ns) is int and type(now_ns) is int,'integer observation/request clocks required')
            age_ns=now_ns-observation_ns
            require(0 <= age_ns <= 200_000_000,
                    f'stale/future observation: age_ms={age_ns/1e6:.3f}; allowed=0..200ms; '
                    f'observation_ns={observation_ns}; request_ns={now_ns}')
            if self.mode == 'native_first' and self.native is None:
                self.native = NativeBootstrap(self.anchor, self.bounds, observation_ns,
                    trial_index=self.trial_index, clock_domain=self.domain, sender_epoch_ns=self.start_ns,
                    optimizer_factory=self.optimizer_factory)
                self.pending = self.native.ticket
            else:
                require(self.queue is not None, 'native queue awaits activation')
                self.pending = self.queue.request(observation_ns, now_ns, clock_domain=self.domain)
            self._request_not_before_ns=0
            if self.manual_speed is not None:
                # Freeze only after the real request/queue guards pass. This
                # metadata never changes H50/RTC wire fields or the CAN clock.
                snapshot=deepcopy(self.manual_speed.freeze_for_request(self.pending.wire()['request_index']))
                require(isinstance(snapshot,dict) and 'target_scale' in snapshot,
                        'Manual speed frozen snapshot is missing its target')
                self.temporal.optimizer.set_target_scale(snapshot['target_scale'])
                if self.native is not None and self.native.status=='WAITING_FIRST_RESULT':
                    self.native.optimizer_factory=partial(self.optimizer_factory,
                                                          target_scale=snapshot['target_scale'])
                self._pending_manual_speed=snapshot
            result={'token': self.pending.token, 'rtc': self.pending.wire(),
                'execution_route':'PHYSICAL','execution_scope':self.execution_scope,
                'observation_ns': observation_ns, 'deadline_ns': self.pending.deadline_ns,
                'execution_cut_index': getattr(self.pending,'execution_cut_index',None),
                'model_freeze_steps': 12, 'known_context_steps': 35,
                'reference_semantics': 'PHYSICAL_ACCEPTED_PRE_MPC_REFERENCE',
                'physical_execution_claimed': False}
            if self.manual_speed is not None:
                result['manual_speed_snapshot']=deepcopy(self._pending_manual_speed)
            return result

    def prepare_candidate(self, token, response, ready_ns, *, now_ns=None,
                          evidence_kind='LIVE_PHYSICAL_MODEL_RESPONSE'):
        """Background worker: validate context, then perform TimeQP without lock.

        Caller verifies actual model identity and response provenance. An
        explicit synthetic-clock override is accepted only for CPU fixtures.
        A cancelled/expired context may finish CPU preparation, but cannot be
        published by commit_candidate. The saved reference/state is not reset.
        """
        with self.lock:
            self._raise_if_pause_invalidated(token)
            require(self.status == 'RUNNING_PHYSICAL' and self.pending is not None, 'no outstanding request')
            require(token == self.pending.token, 'response token mismatch/replay')
            require(evidence_kind in ('LIVE_PHYSICAL_MODEL_RESPONSE', 'SYNTHETIC_PROTOCOL_FIXTURE'), 'invalid response provenance')
            require(now_ns is None or evidence_kind=='SYNTHETIC_PROTOCOL_FIXTURE',
                    'Only explicit CPU fixtures may override the preparation clock')
            require(self._preparing_context is None and self._prepared_bundle is None,
                    'Only one candidate preparation may be in flight')
            context = self.pending
            require(type(ready_ns) is int, 'Actual model-ready nanoseconds required')
            require(context.observation_ns <= ready_ns <= context.deadline_ns, 'real model result missed800ms')
            first_native = self.native is not None and self.native.status == 'WAITING_FIRST_RESULT'
            speed_snapshot=deepcopy(self._pending_manual_speed)
            self._preparing_context = context
        try:
            if first_native:
                candidate = self.native.prepare(response, evidence_kind=('SAVED_REAL_MODEL_RESPONSE' if evidence_kind=='LIVE_PHYSICAL_MODEL_RESPONSE' else evidence_kind))
            else:
                candidate = self.temporal.prepare_response(context, response, evidence_kind=('SAVED_REAL_MODEL_RESPONSE' if evidence_kind=='LIVE_PHYSICAL_MODEL_RESPONSE' else evidence_kind))
            candidate=replace(candidate,diagnostics=self._physical_diagnostics(candidate.diagnostics,evidence_kind))
            speed_stats=None
            if self.manual_speed is not None:
                qp=candidate.diagnostics.get('timeqp_rpc',{})
                require(speed_snapshot is not None and qp.get('target_scale')==speed_snapshot['target_scale'],
                        'Prepared TimeQP target differs from frozen manual request')
                speed_stats=deepcopy(qp.get('local_scale_stats'))
                require(isinstance(speed_stats,dict), 'Actual TimeQP reference-rate statistics required')
                diagnostics=dict(candidate.diagnostics)
                diagnostics['manual_speed_snapshot']=deepcopy(speed_snapshot)
                candidate=replace(candidate,diagnostics=diagnostics)
            finished_ns = int(self.now_fn()) if now_ns is None else int(now_ns)
            if first_native:
                candidate=replace(candidate,not_before_ns=finished_ns,seal='')
                candidate=replace(candidate,seal=prepared_seal(candidate))
            bundle=PreparedCandidate(context,candidate,first_native,ready_ns,finished_ns,evidence_kind)
            with self.lock:
                self._raise_if_pause_invalidated(token)
                require(ready_ns <= finished_ns <= context.deadline_ns, 'temporal preparation missed800ms')
                require(self.pending is context, 'Pending request changed during preparation')
                self._prepared_bundle=bundle
                self._prepared_manual_stats=speed_stats
            return bundle
        finally:
            with self.lock:
                if self._preparing_context is context:
                    self._preparing_context=None

    def commit_candidate(self, token, candidate_bundle, *, now_ns=None):
        """Control thread: no network/TimeQP; check current clock and publish."""
        with self.lock:
            self._raise_if_pause_invalidated(token)
            require(self.status=='RUNNING_PHYSICAL' and self.pending is not None,'no outstanding physical request')
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
            if self.manual_speed is not None:
                require(candidate.diagnostics.get('manual_speed_snapshot')==self._pending_manual_speed and
                        candidate.diagnostics.get('timeqp_rpc',{}).get('local_scale_stats')==self._prepared_manual_stats,
                        'Prepared manual speed evidence changed before commit')
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
            if self.manual_speed is not None:
                snapshot=deepcopy(self._pending_manual_speed)
                stats=deepcopy(self._prepared_manual_stats)
                self._pending_manual_speed=None;self._prepared_manual_stats=None
                # COMMITTED is emitted only after queue.commit or a successful
                # native activation schedule. Actual reference entry is later
                # acknowledged by the runtime after its real writer succeeds.
                self.manual_speed.mark_committed(snapshot,committed_ns,
                    0 if bundle.first_native else context.cut_phase_ns,
                    self.activation_due_ns if bundle.first_native else None,stats)
            return {'accepted': True, 'request_index': context.wire()['request_index'],
                'execution_route':'PHYSICAL','execution_scope':self.execution_scope,
                'accepted_count': self.accepted_count, 'evidence_kind': bundle.evidence_kind,
                'model_ready_ns':bundle.model_ready_ns,'prepare_finished_ns':bundle.prepare_finished_ns,
                'commit_monotonic_ns':committed_ns,'timeqp_remote':self.timeqp_instance_id is not None,
                'activation_due_ns': self.activation_due_ns,
                'reference_semantics': 'PHYSICAL_ACCEPTED_PRE_MPC_REFERENCE',
                'physical_dispatch_performed': False, 'temporal': candidate.diagnostics}

    def accept_response(self, token, response, ready_ns, *, now_ns=None,
                        evidence_kind='LIVE_PHYSICAL_MODEL_RESPONSE'):
        """CPU-compatible synchronous wrapper; live runner uses two phases."""
        bundle=self.prepare_candidate(token,response,ready_ns,now_ns=now_ns,evidence_kind=evidence_kind)
        return self.commit_candidate(token,bundle,now_ns=now_ns)

    def next_request_due_ns(self,now_ns):
        with self.lock:
            if (self.status!='RUNNING_PHYSICAL' or self.pending is not None or
                    self._pause_invalidated_context is not None or self._preparing_context is not None):
                return None
            due = super().next_request_due_ns(now_ns)
            return None if due is None else max(due,self._request_not_before_ns)

    def pause_discard_info(self,token):
        """Only the exact locally invalidated request qualifies, never arbitrary failures."""
        with self.lock:
            context=self._pause_invalidated_context
            if self.status!='RUNNING_PHYSICAL' or context is None or token!=context.token:
                return None
            require(self.queue is not None and self.queue.draining is context and
                    self.queue.pending is None and self.queue.clock.epoch!=context.clock_epoch,
                    'paused response lifecycle is inconsistent')
            return {'reason':'OWNED_MPC_REFERENCE_PAUSE','request_index':context.index,
                'request_clock_epoch':context.clock_epoch,'current_clock_epoch':self.queue.clock.epoch,
                'paused_at_ns':self._pause_started_ns,'state_reset':False}

    def _raise_if_pause_invalidated(self,token):
        info=self.pause_discard_info(token)
        if info is not None:
            raise ReferencePauseInvalidated(info)

    def _apply_mpc_progress(self,alpha,now_ns):
        """Keep the reference paused until an in-flight old-clock result drains.

        MPC ticks and real feedback continue; operator STOP never enters here.
        """
        self._last_requested_mpc_alpha=alpha
        was_paused=self.queue.clock.paused
        effective=0 if self._pause_invalidated_context is not None else alpha
        self.queue.apply_mpc_alpha(effective,now_ns)
        if not was_paused and self.queue.clock.paused:
            if self.pending is not None:
                require(self.queue.draining is self.pending,'core/queue pause context differs')
                self._pause_invalidated_context=self.pending
                self._pause_started_ns=now_ns
                self.pending=None
                self._prepared_bundle=None
                self._pending_manual_speed=None
                self._prepared_manual_stats=None
            self._recovery_schedule_active=True
        elif was_paused and not self.queue.clock.paused:
            self._recovery_schedule_active=True
        return 0 if self.queue.clock.paused else effective

    def consume_pause_result(self,request,result,now_ns):
        """Drain a validated stale response once, then request a new observation.

        Model/transport/identity errors remain fatal. No stale plan is committed,
        no timestamp is changed, and the estimator/controller history survives.
        """
        with self.lock:
            require(self.status=='RUNNING_PHYSICAL','cannot recover STOP/fault/settle')
            if result.get('reference_capture_deferred') is not None:
                require(not request.get('token') and self.pending is None and
                        self._pause_invalidated_context is None and self._preparing_context is None,
                        'deferred capture unexpectedly owns a model request')
                self._capture_deferred_count+=1
                self._request_not_before_ns=now_ns+SEND_NS
                return dict(result['reference_capture_deferred'],event='REFERENCE_CAPTURE_DEFERRED')
            info=self.pause_discard_info(request.get('token'))
            if info is None:
                require(result.get('reference_pause_discard') is None,'unowned or replayed pause discard')
                return None
            require(result.get('model_response_validated') is True and
                    not result.get('pipeline_error') and result.get('cancelled_result') is not True and
                    (not result.get('timeqp_preparation_error') or result.get('reference_pause_discard') is not None) and
                    isinstance(request.get('observation_sha256'),str) and
                    bool(request['observation_sha256']) and
                    result.get('observation_sha256')==request['observation_sha256'],
                    'paused response still requires model identity and observation validation')
            require(self._preparing_context is None,'paused CPU preparation has not drained')
            context=self._pause_invalidated_context
            self.queue.acknowledge_response_drained(context,evidence_kind=(
                'SYNTHETIC_CPU_FIXTURE' if self.protocol_fixture else 'LIVE_PHYSICAL_MODEL_RESPONSE'))
            self._pause_invalidated_context=None
            self._pause_discard_count+=1
            self._recovery_schedule_active=True
            if self.queue.clock.paused and self._last_requested_mpc_alpha==1:
                self.queue.arm_resume_capture(now_ns)
            return dict(info,event='PAUSED_RESPONSE_DRAINED_WITHOUT_COMMIT',
                drained_ns=now_ns,paused_clock_epoch=self.queue.clock.epoch,
                resumed_clock_epoch=None,coverage_hold=self.queue.coverage_hold,
                fresh_observation_required=True,physical_dispatch_performed=False)

    def record_actual_sent_command(self,*args,**kwargs):
        raise RuntimeError('Physical runtime must bind a successful writer receipt via ack_joints')

    def _check_ack_evidence(self,receipt):
        require(isinstance(receipt,dict) and receipt.get('physically_sent') is True and
                receipt.get('record_id') and receipt.get('source')=='CANONICAL_PIPER_ADAPTER_SOCKETCAN_SUCCESS',
                'Actual canonical SocketCAN write receipt required')
        fixture=receipt.get('evidence_kind')=='CPU_ACK_FIXTURE'
        require(fixture==self.protocol_fixture,'CPU ack fixture cannot enter real physical runtime')

    def ack_joints(self,receipt):
        """Writer calls immediately after all six joints were sent, before grip.

        Successful host SocketCAN writes are not proof of motor receipt. An
        eventual gripper failure must NOT discard these real joint commands.
        """
        with self.lock:
            self._check_ack_evidence(receipt)
            pending=self._joint_pending
            require(pending is not None and receipt.get('command_id')==pending['command_id'],
                    'Joint ack does not belong to the outstanding command')
            require(receipt.get('joint_send_completed') is True,'All six joint writes must complete')
            require(receipt.get('joint_mdeg')==pending['joint_mdeg'],'Joint ack encoding differs from issued command')
            sent=array(receipt.get('joint_command_rad'),(6,))
            require(np.max(abs(sent-pending['command_rad_m'][:6]))<=1e-12,
                    'Joint ack target differs from the encoded command')
            at=receipt.get('sent_ns')
            require(type(at) is int and pending['issued_ns']<=at<=int(self.now_fn()),
                    'Joint ack time must be the actual completed send time')
            # The gripper target may still be the previous actual value here.
            # Six-joint estimator/history advances only from the successful ack.
            V2Core.record_actual_sent_command(self,at,np.r_[sent,self._last_gripper_command],evidence=receipt)
            self._last_joint_ack={'command_id':pending['command_id'],'record_id':receipt['record_id'],
                                  'sent_ns':at}
            self._joint_pending=None
            return {'event':'PHYSICAL_JOINT_SEND_ACKNOWLEDGED','execution_route':'PHYSICAL',
                'execution_scope':self.execution_scope,'command_id':pending['command_id'],
                'sent_ns':at,'actual_sent_records':self.actual_sent_records,
                'motor_receipt_verified':False,'gripper_acknowledged':False,
                'fixture_only':self.protocol_fixture}

    def ack_gripper(self,receipt):
        with self.lock:
            self._check_ack_evidence(receipt)
            pending=self._gripper_pending
            require(pending is not None and self._joint_pending is None and self._last_joint_ack is not None
                    and receipt.get('command_id')==pending['command_id']==self._last_joint_ack['command_id'],
                    'Gripper ack requires the matching successful six-joint send')
            require(receipt.get('gripper_send_completed') is True and
                    receipt.get('gripper_um')==pending['gripper_um'],'Gripper send/encoding differs')
            sent=array(receipt.get('command_rad_m'),(7,))
            require(np.max(abs(sent-pending['command_rad_m']))<=1e-12,'Full physical command differs')
            at=receipt.get('gripper_sent_ns')
            require(type(at) is int and self._last_joint_ack['sent_ns']<=at<=int(self.now_fn()),
                    'Gripper ack time must be the actual completed send time')
            self._last_gripper_command=float(sent[6]);self.gripper_ack_count+=1
            self._gripper_pending=None
            return {'event':'PHYSICAL_GRIPPER_SEND_ACKNOWLEDGED','execution_route':'PHYSICAL',
                'execution_scope':self.execution_scope,'command_id':pending['command_id'],
                'gripper_sent_ns':at,'gripper_ack_count':self.gripper_ack_count,
                'motor_receipt_verified':False,'fixture_only':self.protocol_fixture}

    def _invalidate_model_context(self,reason):
        context=self.pending or self._pause_invalidated_context
        token=None if context is None else context.token
        self.pending=None
        self._pause_invalidated_context=None
        self._pending_manual_speed=None
        self._prepared_manual_stats=None
        self._prepared_bundle=None
        self.prepared_native=None
        if self.queue is not None:
            self.queue.abort(reason)
        if self.native is not None and self.native.status=='WAITING_FIRST_RESULT':
            self.native.cancel()
        return token

    def begin_settle(self,now_ns,reference_rad_m=None):
        """Freeze accepted r7 at this instant; continue same MPC on fresh state.

        Caller cancels/drains new model work. This is normal end-of-window
        convergence, not a certified emergency stop or automatic disable.
        """
        with self.lock:
            require(self.status=='RUNNING_PHYSICAL','Settle may begin once from running state')
            require(type(now_ns) is int and now_ns>=self.start_ns and
                    (self.last_tick_ns is None or now_ns>=self.last_tick_ns),'Settle clock reversed')
            if self.queue is None:
                reference=self.anchor.copy();phase=None
            else:
                phase=self.queue.clock.phase_at(now_ns)
                reference=self.queue.plan.sample(phase)
            if reference_rad_m is not None:
                supplied=array(reference_rad_m,(7,))
                require(np.array_equal(supplied,reference),'Settle must freeze the accepted current r7, not a new goal')
            self.settle_reference=array(reference,(7,))
            self.settle_phase_ns=phase;self.settle_started_ns=now_ns
            reason = 'REFERENCE_COVERAGE_HOLD' if self.coverage_hold is not None else 'NORMAL_WINDOW_END_FREEZE_REFERENCE'
            token=self._invalidate_model_context(reason)
            self.status='SETTLING_PHYSICAL'
            return {'event':'PHYSICAL_SETTLE_STARTED','execution_route':'PHYSICAL',
                'execution_scope':self.execution_scope,'now_ns':now_ns,
                'reference_rad_m':self.settle_reference.tolist(),'pending_model_token':token,
                'cancel_and_drain_required':token is not None,'controller_initializations':self.planner.reset_count,
                'gripper_target_held_m':float(self.settle_reference[6]),'automatic_gripper_release':False,
                'physical_stop_verified':False,'disable_performed':False}

    def fault(self,reason):
        """Reject further targets; preserve history and pending actual-write ack."""
        with self.lock:
            token=self._invalidate_model_context(str(reason))
            self.status='FAULTED_PHYSICAL';self.fault_reason=str(reason)
            return {'event':'PHYSICAL_CORE_FAULT','execution_route':'PHYSICAL',
                'execution_scope':self.execution_scope,'reason':str(reason),'pending_model_token':token,
                'new_commands_rejected':True,'disable_performed':False,'emergency_stop_performed':False,
                'joint_ack_pending':self._joint_pending is not None,'gripper_ack_pending':self._gripper_pending is not None}

    def close(self,reason='SESSION_CLOSE'):
        with self.lock:
            token=self._invalidate_model_context(str(reason))
            self.status='CLOSED_PHYSICAL'
            return {'event':'PHYSICAL_CORE_CLOSED','execution_route':'PHYSICAL',
                'execution_scope':self.execution_scope,'pending_model_token':token,
                'physical_stop_verified':False,'disable_performed':False,
                'initialization':deepcopy(self.initialization_receipt),
                'reference_boundary_repair':self.reference_repair_watchdog.summary(),
                'reference_pause_recovery':{'discarded_responses':self._pause_discard_count,
                    'deferred_captures':self._capture_deferred_count,'state_resets':0},
                'joint_send_ack_count':self.actual_sent_records,'gripper_send_ack_count':self.gripper_ack_count,
                'joint_ack_pending':self._joint_pending is not None,'gripper_ack_pending':self._gripper_pending is not None}

    def stop(self,reason='STOP_REQUEST'):
        return self.fault(reason)

    def tick(self, now_ns, feedback_rad_m, feedback_ns, *, feedback_revision=None):
        """Evaluate fresh actual feedback; no HTTP-time substitution.

        For asynchronous CAN aggregates, feedback_revision is the fixed-order
        (q12,q34,q56,gripper) source timestamp tuple, or a verified SHA256 of it.
        The oldest component time remains the age/estimator anchor. Different
        revisions may legitimately have the same oldest component timestamp.
        """
        lock_requested_ns = time.monotonic_ns()
        with self.lock:
            lock_acquired_ns = time.monotonic_ns()
            require(self.status in ('RUNNING_PHYSICAL','SETTLING_PHYSICAL'), 'core has stopped; explicit new run required')
            require(self._joint_pending is None and self._gripper_pending is None,'Previous physical command awaits writer result; no speculative history')
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
                if self.status=='RUNNING_PHYSICAL' and self.native is not None and self.native.status == 'WAITING_FIRST_RESULT':
                    self.native.expire(now_ns)
                    if self.prepared_native is not None and now_ns >= self.activation_due_ns:
                        self.queue = self.native.activate(self.prepared_native, self.activation_due_ns, clock_domain=self.domain)
                        self.prepared_native = None
                        self.observed_activation_ns = now_ns
                if self.status=='SETTLING_PHYSICAL':
                    future=np.tile(self.settle_reference,(15,1))
                    phase=self.settle_phase_ns
                elif self.queue is None:
                    future = np.tile(self.anchor, (15, 1))
                    phase = None
                else:
                    self.queue.expire(now_ns)
                    sample = self.queue.sample_for_mpc(now_ns)
                    future = sample['reference_rad_m']
                    phase = sample['reference_phase_ns']
                repair_observation=self.reference_repair_watchdog.observe(now_ns,
                    self.queue.plan if self.status=='RUNNING_PHYSICAL' and self.queue is not None else None,
                    phase if self.status=='RUNNING_PHYSICAL' else None)
                out, predicted, timing = self.planner.evaluate(qhat, future[:, :6], self.estimator.contact_flag)
                # Official heartbeat lookahead rounds90ms to5 control ticks.
                # Default0 follows the common-time-axis VLA gripper reference.
                grip_index = int(math.ceil(self.gripper_lookahead_ms/20.))
                grip = float(future[min(grip_index, len(future)-1), 6])
                encoded = self.codec.encode(np.r_[out.y_cmd, grip])
                reference_progress_alpha=out.alpha
                if self.status=='RUNNING_PHYSICAL' and self.queue is not None:
                    reference_progress_alpha=self._apply_mpc_progress(out.alpha, now_ns)
                self.last_tick_ns = now_ns
                self.tick_count += 1
                self.command_sequence+=1
                command_id=self.session_id+':'+str(self.command_sequence)
                command={'command_id':command_id,'issued_ns':now_ns,'joint_mdeg':list(encoded.joint_mdeg),
                    'command_rad_m':encoded.target_rad_m.copy(),'gripper_um':int(encoded.gripper_um)}
                self._joint_pending=command
                self._gripper_pending=command
                return {'command_id':command_id,'execution_route':'PHYSICAL',
                    'command_stage':'READY_FOR_PHYSICAL_SEND_NOT_YET_ACKNOWLEDGED',
                    'execution_scope':self.execution_scope,'status':self.status,'now_ns': now_ns, 'feedback_ns': feedback_ns,
                    'feedback_rad_m': q.tolist(), 'qhat_rad': qhat.tolist(),
                    'feedback_revision': revision,
                    'reference_rad_m': future[0].tolist(), 'reference_phase_ns': phase,
                    'reference_boundary_repair':repair_observation,
                    'feedback_reference_error_deg':np.rad2deg(q[:6]-future[0,:6]).tolist(),
                    'command_step_from_last_actual_deg':np.rad2deg(encoded.target_rad_m[:6]-self.planner.x_aug[6:12]).tolist(),
                    'settle_elapsed_s':None if self.settle_started_ns is None else (now_ns-self.settle_started_ns)/NS,
                    'command_rad_m': encoded.target_rad_m.tolist(),
                    'joint_mdeg': list(encoded.joint_mdeg), 'gripper_um': encoded.gripper_um,
                    'model_predicted_next_q_rad': predicted.tolist(),
                    'physical_dispatch_performed': False, 'command_is_unsent': True,
                    'reference_semantics': 'PHYSICAL_SETTLE_FROZEN_REFERENCE' if self.status=='SETTLING_PHYSICAL' else 'PHYSICAL_ACCEPTED_PRE_MPC_REFERENCE',
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
                    'reference_progress_alpha':float(reference_progress_alpha),
                    'reference_paused_for_response_drain':self._pause_invalidated_context is not None,
                    'waiting_for_recovery_observation':self.queue is not None and self.queue.resume_capture_after_ns is not None,
                    'mpc': timing, 'cycle_compute_ms': (time.perf_counter_ns()-started)/1e6,
                    'control_lock_requested_ns':lock_requested_ns,
                    'control_lock_acquired_ns':lock_acquired_ns}
            except Exception:
                self.fault('CORE_TICK_FAILED')
                raise
