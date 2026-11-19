"""Explicit right-gripper target control using the existing Hub and SDK tracker."""
from collections import deque
from contextlib import ExitStack
from pathlib import Path
import math
import time


def parameters(cfg, target_mm, speed_mm_s=None):
    bounds=cfg.get('gripper_bounds_m')
    if not isinstance(bounds,(list,tuple)) or len(bounds)!=2 or not all(type(x) in (int,float) and math.isfinite(x) for x in bounds) or bounds[0]>=bounds[1]:
        raise ValueError('Explicit gripper coordinate bounds are required')
    if type(target_mm) not in (int,float) or not math.isfinite(target_mm) or not bounds[0]*1000<=target_mm<=bounds[1]*1000:
        raise ValueError('G目标必须在 %.3f–%.3f mm 的配置范围内；不自动裁剪'%(bounds[0]*1000,bounds[1]*1000))
    if not math.isclose(target_mm*1000,round(target_mm*1000),rel_tol=0.,abs_tol=1e-7):
        raise ValueError('G目标精度须为0.001 mm或更粗')
    configured_speed=cfg.get('ready_gripper_speed_m_s',.01)
    speed=(configured_speed*1000 if type(configured_speed) in (int,float) else None) if speed_mm_s is None else speed_mm_s
    minimum=cfg.get('pose_send_hz',50)/1000
    if type(speed) not in (int,float) or not math.isfinite(speed) or not minimum<=speed<=10:
        raise ValueError('G手动参考速度必须在 %g–10 mm/s 范围内'%minimum)
    return {'target_mm':float(target_mm),'target_m':target_mm/1000,'reference_speed_mm_s':speed,
            'reference_speed_m_s':speed/1000,'bounds_mm':[x*1000 for x in bounds]}


def run(cfg,args,session,*,cancel=lambda:False):
    from .hardware import PiperAdapter,HardwareBlocked,require_gripper_feedback,sdk_profile_parameters
    from .storage import Ownership,verify_usb,require_not_stopped,stop_requested
    from .hub import rpc
    from .h50_deployment import HubFeedbackCache
    from .preparation import prepare_ready_gripper
    from .tx_evidence import TxEvidenceWriter
    request=parameters(cfg,args.target_mm,args.speed_mm_s)
    verify_usb(cfg)
    def cancelled():return cancel() or (args.apply and stop_requested(cfg))
    def read_status():
        status=rpc(cfg,'/status?compact=1',timeout=.5)
        if args.apply and (status.get('recording',{}).get('active') is not False or
                           status.get('recording',{}).get('saving') is not False or
                           status.get('recovery',{}).get('deployment_active') is not False):
            raise HardwareBlocked('Finish the current recording/deployment before manual G movement')
        return status
    status=read_status();state=status['arms']['right']
    require_gripper_feedback(state,max_age_s=.2,require_enabled=False)
    result={'schema':'tiger-manual-gripper-v1',**request,'initial_actual_mm':state['grip_m']*1000,
            'joint_targets_sent':False,'coordinate_zero_changed':False,'joint_enable_sent':False,
            'video_recording_started':False,'actual_physical_speed_verified':False,
            'status':'PLAN_ONLY_NO_MOTION','estimated_reference_s':abs(request['target_m']-state['grip_m'])/request['reference_speed_m_s']}
    session.report['manual_gripper']=result
    if not args.apply:return result
    if args.leader_isolated is not True or args.path_reviewed is not True:
        raise HardwareBlocked('Manual G movement requires leader isolation and reviewed gripper path')
    require_not_stopped(cfg)
    entry=cfg['can']['right'];events=deque(maxlen=64)
    try:
        with Ownership('writer-'+entry['interface']),ExitStack() as stack:
            status=read_status()
            cache=HubFeedbackCache(cfg,rpc,status['producer'],status['storage_binding'],verify_usb,stop_requested).start()
            stack.callback(cache.close)
            deadline=time.monotonic()+1.
            while cache.received is None:
                cache.check_alive(require_value=False)
                if cancelled() or time.monotonic()>=deadline:raise HardwareBlocked('Manual G shared feedback did not become ready')
                time.sleep(.01)
            writer=TxEvidenceWriter(cfg,session.path/'host-manual-gripper-tx-v1.jsonl')
            result['tx_start']=writer.start()
            stack.callback(lambda:result.update(tx_evidence=writer.close(timeout_s=5)))
            baseline=cache.control_snapshot()['q_deg'];last_record=-math.inf
            def snapshot():
                nonlocal last_record
                if cancelled():raise HardwareBlocked('Manual G cancelled; no new target')
                health=writer.status()
                if health.get('timing_evidence_incomplete') or health.get('writer_error'):raise HardwareBlocked('Manual G recording failed; no new target')
                current=cache.control_snapshot()
                if max(abs(a-b) for a,b in zip(current['q_deg'],baseline))>.5:
                    raise HardwareBlocked('Joint posture moved during G-only operation; no joint targets were requested')
                now=time.monotonic()
                if now-last_record>=.02:
                    writer.enqueue({'kind':'MANUAL_GRIPPER_ACTUAL','monotonic_ns':time.monotonic_ns(),
                        'q_deg':list(current['q_deg']),'gripper_m':current['grip_m'],
                        'gripper_source_timing':current.get('source_timing',{}).get('gripper')})
                    last_record=now
                return current
            adapter=PiperAdapter({**entry,'sdk_version':'0.6.2',**sdk_profile_parameters(cfg,'right'),
                'leader_isolated':True,'max_feedback_age_s':.2},snapshot,events.append)
            stack.callback(adapter.close)
            adapter.attach_tx_evidence(writer)
            # This existing connection mode permits only CAN0x159, never joint/enable/mode IDs.
            adapter.connect_for_apply(gripper_calibration_only=True)
            adapter.feedback(require_can=True)
            adapter.require_no_foreign_commands()
            local_cfg={**cfg,'ready_gripper_m':request['target_m'],'ready_gripper_speed_m_s':request['reference_speed_m_s']}
            result['status']='RUNNING'
            with adapter.dispatch({'operation':'manual_gripper','target_mm':request['target_mm'],
                                   'joint_targets_sent':False,'coordinate_zero_changed':False}):
                result['verification']=prepare_ready_gripper(local_cfg,adapter,cancel=cancelled)
            result['verification']['assumption']='operator-reviewed manual G target path; contact not independently verified'
            result['verification']['command_receipt_scope']='manual gripper only; no joint targets'
            result['packets_sent']=adapter.packets_sent
            result['final_actual_mm']=result['verification']['final_actual_m']*1000
            result['sdk_events']=list(events)
        evidence=result['tx_evidence']
        if evidence.get('finished') is not True or evidence.get('timing_evidence_incomplete') or evidence.get('writer_error'):
            raise HardwareBlocked('Manual G command/feedback evidence did not finish')
        result['status']='GRIPPER_TARGET_CONFIRMED_FROM_REAL_FEEDBACK'
        return {k:result[k] for k in ('schema','status','target_mm','initial_actual_mm','final_actual_mm',
            'reference_speed_mm_s','joint_targets_sent','joint_enable_sent','coordinate_zero_changed','video_recording_started')}
    except BaseException as exc:
        result.update(status='FAILED_NO_AUTOMATIC_RECOVERY',error=repr(exc),sdk_events=list(events))
        if hasattr(exc,'preparation_receipt'):
            result['verification']=exc.preparation_receipt
            result['verification']['assumption']='operator-reviewed manual G target path; contact not independently verified'
            result['verification']['command_receipt_scope']='manual gripper only; no joint targets'
        raise
