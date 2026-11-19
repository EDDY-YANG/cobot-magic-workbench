"""Small actual-target adapter; original Piper guards remain authoritative.

Import/construct opens nothing. Caller owns shared locks, model lease, verified
USB/evidence sink, feedback cache and explicit onsite approvals. This wrapper
never changes mode, enables, clears STOP, calibrates, zeros or disables motors.
It is not an alternative controller: only the caller's V2 command is encoded.
"""
from collections import deque
from copy import deepcopy
import hashlib
import importlib
import math
from pathlib import Path
import sys
import threading
import time

from .v2_tracking_guard import (V2TrackingGuard, V2TrackingError,
                                tracking_guard_class, make_tracking_guard)


class V2WriterError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise V2WriterError(message)


def canonical_dependencies(root, expected):
    """Verify the already-selected canonical source before lazy SDK import."""
    root=Path(root).resolve(strict=True)
    modules={}
    for name in ('hardware','h50_async'):
        rel='agilex_control/'+name+'.py'
        path=root/rel
        require(rel in expected and hashlib.sha256(path.read_bytes()).hexdigest()==expected[rel],
                'Canonical writer source changed: '+rel)
        module=importlib.import_module('agilex_control.'+name)
        require(Path(module.__file__).resolve()==path.resolve(),'Foreign canonical module: '+name)
        modules[name]=module
    h,c=modules['hardware'],modules['h50_async']
    return dict(adapter=h.PiperAdapter,documented=h.DOCUMENTED_COMMAND_LIMITS_DEG,
        profile=h.sdk_profile_parameters,feedback_guard=h.require_feedback,
        gripper_guard=h.require_gripper_feedback,validate_q=h.validate_target_deg,
        TrackingGuard=tracking_guard_class(),settings=c.settings,encode=c.prepare_output_command)


class V2LiveWriter:
    """One explicitly opened right-arm writer, with latched send failures.

    send() invokes ack_joint immediately after all joint packets succeed, before
    the gripper call. That callback must update only actual joint-command history;
    gripper_send_completed remains false until its own successful host send.
    Partial packet errors are retained by the canonical TX sink, never retried.
    Host submission evidence is not proof of motor receipt or physical motion.
    """
    def __init__(self,cfg,feedback_cache,tx_events,fast_guard,*,canonical_root,
                 canonical_sources,confirmations,test_dependencies=None,tracking_action=None):
        require(callable(fast_guard),'Explicit current owner/STOP guard required')
        require(callable(getattr(feedback_cache,'read_latest',None)),'Existing feedback RAM cache required')
        require(callable(getattr(tx_events,'enqueue',None)) and callable(getattr(tx_events,'status',None)),
                'Canonical bounded TX evidence sink required')
        require(all(confirmations.get(key) is True for key in
                    ('leader_isolated','onsite_confirmed','current_start_confirmed')),
                'Current onsite/leader/current-start confirmations required')
        self.cfg=deepcopy(cfg);self.cache=feedback_cache;self.tx_events=tx_events;self.guard=fast_guard
        self.d=(canonical_dependencies(canonical_root,canonical_sources)
                if test_dependencies is None else dict(test_dependencies))
        self.test_fixture=test_dependencies is not None
        self.adapter=None;self.query=None;self.bounds=None;self.anchor=None
        self.tracking=(self.d['TrackingGuard']() if tracking_action is None else make_tracking_guard(tracking_action))
        self.last_joint_target_deg=None
        self.last_gripper_target_m=None;self.last_send_ns=None;self.groups=0
        self.fault=None;self.closed=False;self.events=deque(maxlen=128);self.lock=threading.Lock()
        self.receipt={'schema':'isolated-v2-actual-writer-v1','synthetic_fixture':self.test_fixture,
            'tracking_policy':(self.tracking.policy() if isinstance(self.tracking,V2TrackingGuard) else None),
            'opened':False,'normal_stop_method':'caller freezes common reference and continues same MPC',
            'fault_stop_method':'stop new targets; last received target may finish; not hardware E-stop',
            'automatic_enable':False,'automatic_mode_handoff':False,'automatic_zero':False,
            'automatic_disable':False,'partial_group_failures':[],'cleanup_errors':[]}

    def current_snapshot(self):
        """Original raw feedback plus elapsed cache age, never altered values."""
        cached=self.cache.read_latest()
        # read_latest() already returns an owned deep copy. Reuse that copy;
        # age updates below cannot mutate the sampler's published snapshot.
        state=cached['feedback']
        received=cached.get('received_monotonic_ns');now=time.monotonic_ns()
        require(type(received) is int and 0<=now-received<=200_000_000,'Feedback cache is stale/future')
        elapsed=(now-received)/1e9
        for key in ('age_joint_s','age_status_s','age_motor_s','age_gripper_s'):
            value=state.get(key)
            if type(value) in (int,float) and math.isfinite(value):
                state[key]=value+elapsed
        for item in state.get('external_commands',{}).values():
            value=item.get('last_age_s')
            if type(value) in (int,float) and math.isfinite(value):
                item['last_age_s']=value+elapsed
        return state

    def latest_frame(self,can_id):
        self.guard()
        return self.current_snapshot().get('raw',{}).get(hex(can_id))

    def _check(self):
        self.guard()
        require(self.fault is None and not self.closed,'V2 writer faulted/closed: '+str(self.fault))
        require(self.tx_events.status().get('timing_evidence_incomplete') is False,'TX evidence incomplete')
        state=self.current_snapshot()
        self.d['feedback_guard'](state,max_age_s=.2,require_enabled=True,require_can=True)
        self.d['gripper_guard'](state,max_age_s=.2,require_enabled=True,now_monotonic=time.monotonic())
        # Command bounds constrain targets, not the unmodified measurement.
        # Healthy raw feedback may be slightly outside that envelope (e.g.
        # J2=-0.016deg around its zero). Controller/status/tracking guards stay.
        return state

    def open(self):
        with self.lock:
            require(self.adapter is None and self.query is None and not self.closed,'Writer already opened/closed')
            self._check()
            cfg=self.cfg;entry=cfg['can']['right']
            settings=self.d['settings'](cfg)
            params=dict(interface=entry['interface'],expected_serial=entry['expected_serial'],sdk_version='0.6.2',
                leader_isolated=True,max_feedback_age_s=.2,native_speed_percent=settings['native_speed_percent'],
                gripper_bounds_m=cfg['gripper_bounds_m'],gripper_effort_mNm=cfg['gripper_effort_mNm'],
                command_limits_deg=deepcopy(self.d['documented']))
            params.update(self.d['profile'](cfg,'right'))
            try:
                self.query=self.d['adapter'](params,self.current_snapshot,self.events.append)
                self.query.attach_tx_evidence(self.tx_events)
                try:
                    self.query.connect_for_apply(query_only=True)
                    self.query.require_no_foreign_commands()
                    limits=self.query.query_limits(self.latest_frame)
                    self.receipt['raw_controller_limits']=deepcopy(limits)
                    self.receipt['query_packets_sent']=deepcopy(self.query.packets_sent)
                finally:
                    primary=sys.exception()
                    try:
                        self.query.close();self.query=None
                    except BaseException as cleanup:
                        self.receipt['cleanup_errors'].append('query: '+repr(cleanup))
                        if primary is None:raise
                        primary.add_note('Query close also failed: '+repr(cleanup))
                rows=limits.get('limits')
                require(isinstance(rows,list) and len(rows)==6,'Six real controller limit replies required')
                bounds=[]
                for index,(row,documented) in enumerate(zip(rows,self.d['documented']),1):
                    require(row.get('joint')==index,'Controller limit reply joint/order mismatch')
                    lo,hi=row.get('minimum_deg'),row.get('maximum_deg')
                    require(all(type(v) in (int,float) and math.isfinite(v) for v in (lo,hi)) and lo<hi,
                            'Invalid controller limits')
                    bounds.append([max(lo,documented[0]),min(hi,documented[1])])
                require(all(lo<hi for lo,hi in bounds),'Controller/documented limits do not intersect')
                self.bounds=bounds+[list(cfg['gripper_bounds_m'])]
                params['command_limits_deg']=bounds
                self.adapter=self.d['adapter'](params,self.current_snapshot,self.events.append)
                self.adapter.attach_tx_evidence(self.tx_events)
                self._check();self.adapter.connect_for_apply()
                self.adapter.feedback(require_can=True);self.adapter.require_no_foreign_commands()
                state=self._check()
                self.anchor=[*state['q_deg'],state['grip_m']]
                require(self.bounds[6][0]<=self.anchor[6]<=self.bounds[6][1],'Measured gripper outside current bounds')
                self.last_gripper_target_m=self.anchor[6]
                self.receipt.update(opened=True,bounds_deg_m=deepcopy(self.bounds),
                    current_measured_anchor_deg_m=list(self.anchor),sdk_profile=deepcopy(params),
                    initial_gripper_history_basis='CURRENT_MEASUREMENT_NO_INITIAL_TARGET_SENT')
                return deepcopy(self.receipt)
            except BaseException as exc:
                self.fault=type(exc).__name__+': '+str(exc)
                self.receipt.update(open_error=self.fault,opened=False)
                if self.adapter is not None:
                    try:self.adapter.close()
                    except BaseException as cleanup:
                        self.receipt['cleanup_errors'].append('open adapter close: '+repr(cleanup))
                        exc.add_note('Writer close also failed: '+repr(cleanup))
                raise

    @property
    def bounds_rad_m(self):
        require(self.bounds is not None,'Controller limits have not been queried')
        return [[math.radians(lo),math.radians(hi)] for lo,hi in self.bounds[:6]]+[list(self.bounds[6])]

    def send(self,command_rad_m,tick_context,*,ack_joint=None):
        require(callable(ack_joint),'Actual joint submission acknowledgement callback required')
        require(self.lock.acquire(blocking=False),'Concurrent V2 writer call refused')
        record=None
        attempt={'command_id':tick_context.get('command_id'),
                 'entry_ns':time.monotonic_ns(),'previous_joint_send_ns':self.last_send_ns,
                 'stage':'writer_precheck'}
        self.receipt['last_send_attempt']=attempt
        try:
            require(self.adapter is not None,'Writer must be explicitly opened')
            state=self._check();now=time.monotonic_ns()
            attempt.update(precheck_completed_ns=now,stage='send_gap_guard',
                           send_gap_ns=None if self.last_send_ns is None else now-self.last_send_ns)
            require(self.last_send_ns is None or 0<now-self.last_send_ns<=100_000_000,
                    'Writer tick gap exceeds100ms; gap_ms='+str(None if self.last_send_ns is None else (now-self.last_send_ns)/1e6)
                    +'; precheck_ms='+str((now-attempt['entry_ns'])/1e6))
            attempt['stage']='target_encoding_and_guards'
            require(isinstance(command_rad_m,(list,tuple)) and len(command_rad_m)==7 and
                    all(type(v) in (int,float) and math.isfinite(v) for v in command_rad_m),
                    'Finite original seven-dimensional target required')
            sample_ns=tick_context.get('reference_sample_monotonic_ns',tick_context.get('now_ns'))
            require(type(sample_ns) is int and 0<=now-sample_ns<=100_000_000,'V2 command sample is stale/future')
            command_id=tick_context.get('command_id')
            require(isinstance(command_id,str) and command_id,'Current V2 command identity required')
            raw=[math.degrees(v) for v in command_rad_m[:6]]+[command_rad_m[6]]
            self.d['validate_q'](raw[:6],self.bounds[:6])
            require(self.bounds[6][0]<=raw[6]<=self.bounds[6][1],'Gripper command exceeds current bounds')
            output=self.d['encode'](raw,self.bounds);target=output['target_deg_m']
            encoded=[math.radians(v) for v in target[:6]]+[target[6]]
            error=self.tracking.check(state['q_deg'],self.last_joint_target_deg,now/1e9)
            self.adapter.require_no_foreign_commands()
            context={**deepcopy(tick_context),'reference_scope':'PHYSICAL_ACCEPTED_REFERENCE',
                'final_target_rad_m':encoded,'output_conversion':output,'controller':'OFFICIAL_V2_ACADOS'}
            before=deepcopy(self.adapter.packets_sent)
            record={'record_id':'v2-host-group-'+str(self.groups+1),'source':'CANONICAL_PIPER_ADAPTER_SOCKETCAN_SUCCESS',
                'command_id':command_id,'joint_mdeg':list(output['joint_command_mdeg']),
                'gripper_um':output['gripper_command_um'],
                'joint_send_completed':False,'gripper_send_completed':False,'physically_sent':False,
                'requested_command_rad_m':encoded,'motor_receipt_verified':False,'packet_counts_before':before}
            with self.adapter.dispatch(context) as dispatch_id:
                record['dispatch_id']=dispatch_id
                attempt.update(dispatch_entered_ns=time.monotonic_ns(),stage='joint_precheck')
                self._check()
                attempt.update(joint_send_started_ns=time.monotonic_ns(),stage='joint_send')
                self.adapter.send_joint_target_deg(target[:6])
                sent_ns=time.monotonic_ns()
                attempt.update(joint_send_completed_ns=sent_ns,stage='joint_ack')
                self.last_joint_target_deg=list(target[:6]);self.last_send_ns=sent_ns
                record.update(joint_send_completed=True,physically_sent=True,sent_ns=sent_ns,
                    joint_command_rad=list(encoded[:6]),
                    packet_counts_after_joint=deepcopy(self.adapter.packets_sent),
                    # The seventh value is not this group's unsent gripper request.
                    command_rad_m=list(encoded[:6])+[self.last_gripper_target_m],
                    gripper_field_basis='PREVIOUS_SUCCESSFUL_TARGET_OR_INITIAL_MEASUREMENT')
                ack_joint(deepcopy(record))
                attempt.update(joint_ack_completed_ns=time.monotonic_ns(),stage='gripper_precheck')
                self._check()
                attempt.update(gripper_send_started_ns=time.monotonic_ns(),stage='gripper_send')
                self.adapter.send_gripper_m(target[6],require_enabled=True)
                self.last_gripper_target_m=target[6]
                record.update(gripper_send_completed=True,gripper_sent_ns=time.monotonic_ns(),command_rad_m=encoded)
                attempt['gripper_send_completed_ns']=record['gripper_sent_ns']
            self.groups+=1
            record.update(packet_counts_after=deepcopy(self.adapter.packets_sent),
                tracking_error_to_last_host_submission_deg=error,group_completed=True)
            if isinstance(self.tracking,V2TrackingGuard):
                record['tracking_check']=deepcopy(self.tracking.last_check)
            require(not self.adapter.tx_evidence_status.get('timing_evidence_incomplete'),'TX evidence incomplete after dispatch')
            attempt.update(completed_ns=time.monotonic_ns(),stage='completed')
            return record
        except BaseException as exc:
            attempt.update(failed_ns=time.monotonic_ns(),error=type(exc).__name__+': '+str(exc))
            self.fault=type(exc).__name__+': '+str(exc)
            if isinstance(exc,V2TrackingError) and 'tracking_fault' not in self.receipt:
                self.receipt['tracking_fault']={**deepcopy(exc.receipt),
                    'last_host_submission_monotonic_ns':self.last_send_ns,
                    'rejected_next_command_id':tick_context.get('command_id'),
                    'completed_groups_before_fault':self.groups}
            if record is not None:
                record.update(group_completed=False,error=self.fault,
                    packet_counts_after=deepcopy(self.adapter.packets_sent))
                self.receipt['partial_group_failures'].append(record)
            self.receipt['fault']=self.fault
            raise
        finally:
            self.lock.release()

    def fault_stop(self,reason):
        """Stop accepting targets; no hold/disable/zero packet is emitted."""
        with self.lock:
            self.fault=self.fault or str(reason)
            return {'new_targets_stopped':True,'robot_command_sent_by_stop':False,
                    'last_target_may_finish':True,'hardware_estop':False,'reason':self.fault}

    def close(self):
        """Close host sockets only; no DisableArm or gripper command."""
        with self.lock:
            errors=[]
            for name in ('query','adapter'):
                resource=getattr(self,name)
                if resource is not None:
                    self.receipt[name+'_packets_sent']=deepcopy(resource.packets_sent)
                    try:resource.close()
                    except BaseException as exc:errors.append(name+': '+repr(exc))
            self.closed=True
            self.receipt['cleanup_errors'].extend(errors)
            if callable(getattr(self.tracking,'diagnostic_receipt',None)):
                self.receipt['tracking_diagnostics']=self.tracking.diagnostic_receipt()
            self.receipt.update(closed=not errors,close_errors=errors,completed_groups=self.groups,
                last_joint_target_deg=deepcopy(self.last_joint_target_deg),
                last_gripper_target_m=self.last_gripper_target_m,last_hardware_events=list(self.events))
            require(not errors,'Host socket close unconfirmed: '+repr(errors))
            return deepcopy(self.receipt)
