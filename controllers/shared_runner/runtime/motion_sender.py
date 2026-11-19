"""Single independent CAN writer; same canonical feedback/tracking/foreign-owner checks."""
from copy import deepcopy
import time
import numpy as np
from runtime.execution import Sender,PERIOD_NS,NS
from runtime.legacy_support import require

class MotionSender(Sender):
    def __init__(self,resource,*,clock_ns=time.monotonic_ns):
        require(resource.no_send is False and resource.adapter is not None,'explicit owned APPLY writer required')
        super().__init__(resource,clock_ns=clock_ns)
        from agilex_control.h50_async import TrackingGuard
        self.tracking=TrackingGuard();self.last_sent=None
        self.thread.name='bf16-smoothing-single-can-writer'
    def tick(self,now_ns,scheduled_ns):
        r=self.r;r.guard()
        require(type(now_ns) is int and now_ns>0,'actual monotonic sample time required')
        require(self.last_tick_ns is None or 0<=now_ns-self.last_tick_ns<=100_000_000,'sender gap exceeded 100ms or reversed')
        state=r.cache.control_snapshot()
        r.h['feedback_guard'](state,max_age_s=.2,require_enabled=True,require_can=True)
        r.h['gripper_guard'](state,max_age_s=.2,require_enabled=True,now_monotonic=now_ns/NS)
        error=self.tracking.check(state['q_deg'],self.last_sent,now_ns/NS)
        r.adapter.require_no_foreign_commands()
        with r.queue._lock:
            command=r.queue.sample_command(now_ns)
            phase,plan=r.queue.phase,r.queue.plan.plan_id
            velocity=r.queue.plan.state(now_ns,'right')['v'] if phase!='REFERENCE_STOPPED' else np.zeros(7)
        scale=np.asarray([180/np.pi]*6+[1.])
        raw=(command.raw_reference_rad_m*scale).tolist()
        # The existing encoder is independently audited against the new integer limiter.
        output=r.core.prepare_output_command(raw,r.bounds)
        target=[v/1000. for v in command.joint_mdeg]+[command.gripper_um/1e6]
        r.h['validate_q'](target[:6],r.bounds[:6])
        require(all(abs(a-b)<1e-9 for a,b in zip(target,output['target_deg_m'])),'canonical and independent final encoders disagree')
        context={'plan_id':plan,'phase':phase,'rtc_enabled':r.context_bridge.next_response_index>1,
            'inference_phase':'RTC_CONTINUATION' if r.context_bridge.next_response_index>1 else 'NATIVE_BOOTSTRAP',
            'reference_scope':'PHYSICAL_ACCEPTED_REFERENCE','reference_sample_monotonic_ns':now_ns,
            'scheduled_tick_monotonic_ns':scheduled_ns,'final_target_rad_m':command.target_rad_m.tolist(),
            'joint_output_projection':deepcopy(output['joint_projection']),
            'gripper_output':{k:output[k] for k in ('policy','gripper_before_m','gripper_after_m','gripper_lower_clamped','gripper_upper_clamped','gripper_delta_m')}}
        with r.queue._lock:
            require(r.queue.phase!='ABORTED','queue abort blocks all new command groups')
            require(0<=self.clock_ns()-now_ns<=100_000_000,'sample expired before dispatch')
            with r.adapter.dispatch(context):
                r.guard();r.adapter.send_joint_target_deg(target[:6]);r.guard()
                r.adapter.send_gripper_m(target[6],require_enabled=True)
        self.last_sent=target[:6]
        if self.last_tick_ns is not None:
            require(len(self.intervals_ns)<self.timing_capacity,'timing RAM buffer exhausted')
            self.intervals_ns.append(now_ns-self.last_tick_ns)
        self.last_tick_ns=now_ns;self.ticks+=1
        if self.ticks==1 or self.ticks%5==0:
            require(r.events.enqueue({'kind':'ACCEPTED_REFERENCE_AND_ACTUAL','monotonic_ns':now_ns,
                'reference_sample_monotonic_ns':now_ns,'scheduled_tick_monotonic_ns':scheduled_ns,
                'reference_q_deg_m':raw,'reference_v_deg_m_s':(velocity*scale).tolist(),
                'actual_q_deg':deepcopy(state['q_deg']),'actual_gripper_m':state['grip_m'],
                'tracking_error_to_last_host_submission_deg':error,'physical_dispatch_performed':True,
                'phase':phase,'plan_id':plan,'output_conversion':output,
                'isolated_sdk_joint_mdeg':list(command.joint_mdeg),'isolated_sdk_gripper_um':command.gripper_um}) is not False,
                'reference evidence enqueue failed')
        return command
    def _run(self):
        scheduled=self.clock_ns()
        try:
            while not self.quit.is_set():
                self.tick(self.clock_ns(),scheduled)
                finished=self.clock_ns();scheduled=scheduled+PERIOD_NS if finished<=scheduled+PERIOD_NS else finished+PERIOD_NS
                self.quit.wait(max(0.,(scheduled-self.clock_ns())/NS))
        except BaseException as exc:
            self.failure=repr(exc);self.failed.set();self.r.queue.abort('APPLY_SENDER_FAILED: '+repr(exc))
    def close(self):
        value=super().close()
        timing=self.r.receipt.pop('diagnostic_observer_intervals_ms',None)
        if timing is not None:
            timing['physical_send_performed']=True;self.r.receipt['sender_intervals_ms']=timing
        return value
