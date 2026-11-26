# 完整单行命令：Windows PC PowerShell；无需预定义变量，复制时去掉行首 #。
# 按需选择一条命令，不要把所有示例一起执行。
# # 本文件是命令分派模块，直接运行本文件不会执行 main(cfg, root)。
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' --help
# & 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe' -X utf8 -B 'C:\Users\Administrator\OneDrive\文档\ChatGPT\AgileX 双臂操作\2026-10-06-14-05-AGILEX-UNIFIED-CONTROL\2026-10-06-14-05-agilex.py' status
# 本轮仅补充命令注释；未运行测试、SSH、模型、相机或机械臂。
"""One explicit CLI for receive, capture, motion and policy operations.

Default move/deploy commands produce plans; only --apply can issue motion.
All remote commands select absolute interpreters; no pip/apt/conda modifications.
"""
import argparse
from collections import deque
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import signal
import errno
import socket
import subprocess
import sys
import threading
import time
from .storage import Session, Ownership, verify_usb, sha256, require_not_stopped, stop, clear_stop, write_json, stop_requested

ENTRY = "2026-10-06-14-05-agilex.py"
H50_COMMANDS = frozenset(('h50-load','h50-shadow','h50-acceptance','deploy-h50'))


def _open_h50_log(cfg,session):
    from .usb_storage import open_usb_directory
    verify_usb(cfg)
    parent = open_usb_directory(session.path,cfg)
    try:
        descriptor = os.open('h50-command.log',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        if os.fstat(descriptor).st_dev != os.fstat(parent).st_dev:
            os.close(descriptor)
            raise RuntimeError('H50 command log crossed selected USB filesystem')
        return os.fdopen(descriptor,'w',encoding='utf-8',buffering=1)
    finally:os.close(parent)


def _h50_command_logged(cfg,args,session,stop_event):
    from contextlib import redirect_stdout
    primary = None
    with _open_h50_log(cfg,session) as output:
        try:
            with redirect_stdout(output):return _h50_command(cfg,args,session,stop_event)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:output.flush();os.fsync(output.fileno());verify_usb(cfg)
            except Exception as exc:
                session.report['h50_command_log_error'] = repr(exc)
                if primary is None:raise


def _h50_finalize(cfg,session,step,run,*,acceptance=False):
    from .h50_acceptance import finalize_acceptance,finalize_run_artifacts
    finalized = (finalize_acceptance if acceptance else finalize_run_artifacts)(cfg,session,step,run)
    if (not isinstance(finalized,dict) or not isinstance(finalized.get('compact'),dict)
            or not isinstance(finalized.get('full'),dict)):
        raise RuntimeError('H50 finalizer returned an invalid audit receipt')
    session.report['h50_acceptance_finalization' if acceptance else 'h50_artifact_finalization'] = finalized['full']
    return finalized


def _h50_preserve_partial(cfg,session,step,*,acceptance):
    partial = session.report.get('h50_async_run')
    if isinstance(partial,dict):
        try:_h50_finalize(cfg,session,step,partial,acceptance=acceptance)
        except Exception as exc:session.report['h50_partial_artifact_error'] = repr(exc)


def _h50_command(cfg, args, session, stop_event):
    """New H50 path only; no legacy PolicyClient/three-stage delegation."""
    from .h50_approved_contract import require_approved_config
    from .h50_prompt import validate_prompt
    setting = require_approved_config(cfg)
    step = args.step
    task = validate_prompt(getattr(args, 'task', setting['task']))
    session.report['h50_task_prompt'] = {'task': task, 'default_task': setting['task'],
        'custom_prompt': task != setting['task'], 'scope': 'immutable_for_this_run'}
    prompt_kwargs = {'task': task} if task != setting['task'] else {}
    if args.command == 'h50-shadow':
        if type(args.count) is not int or not 1 <= args.count <= 5:
            raise ValueError('H50 shadow count must be within 1..5')
    if args.command == 'h50-load':
        from .h50_runtime import ModelRuntime
        runtime = ModelRuntime(cfg,step)
        primary = None
        try:
            health = runtime.ensure_ready()
            session.report['h50_model_health'] = health
            return {'schema':'tiger-h50-load-result-v1','status':'MODEL_READY','step':step,'health':health}
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:session.report['h50_load_cleanup'] = runtime.close()
            except Exception as exc:
                session.report['h50_load_cleanup_error'] = repr(exc)
                if primary is None:raise
    if args.command == 'deploy-h50':
        if not math.isfinite(args.duration) or not 1 <= args.duration <= 1800:
            raise ValueError('H50 duration must be within 1..1800 seconds')
        if not args.apply:
            return {'schema':'tiger-h50-deploy-plan-v1','status':'PLAN_ONLY_NO_HARDWARE','step':step,
                'duration_s':args.duration,'task':task,'start_pose':'confirmed_current','rtc_enabled':True,
                'return_to_zero':False,'contract':setting,'physical_task_success':'NOT_RUN'}
        if args.leader_isolated is not True or args.path_reviewed is not True:
            raise RuntimeError('H50 apply requires actual leader isolation and reviewed path including current start')
    # A durable reservation precedes model/recording/physical work. A failed
    # startup consumes that number; it cannot accidentally replay a prior RNG trial.
    from .h50_trials import allocate_trial
    allocation = allocate_trial(cfg,step,session.path.name,args.command)
    session.report['h50_trial_allocation'] = allocation
    session.save()
    trial_index = allocation['trial_index']
    if args.command in ('h50-shadow','h50-acceptance'):
        from .h50_deployment import run_h50_no_motion
        try:
            run = run_h50_no_motion(cfg,session,stop_event,step,
                count=100 if args.command=='h50-acceptance' else args.count,
                warmup=3 if args.command=='h50-acceptance' else 0,trial_index=trial_index,**prompt_kwargs)
        except BaseException:
            _h50_preserve_partial(cfg,session,step,acceptance=args.command=='h50-acceptance')
            raise
        session.report['h50_cli_run_receipt'] = run
        if args.command == 'h50-acceptance':
            finalized = _h50_finalize(cfg,session,step,run,acceptance=True)
            return {'schema':'tiger-h50-cli-result-v1','status':finalized['compact'].get('status','FAIL'),
                'step':step,'trial_index':trial_index,'trial_seed':allocation['trial_seed'],
                'run':run,'acceptance':finalized}
        artifacts = _h50_finalize(cfg,session,step,run)
        return {'schema':'tiger-h50-cli-result-v1',
            'status':run.get('status','FAILED') if artifacts['compact'].get('validated') is True else 'ARTIFACT_PRESERVATION_FAILED',
            'run_status':run.get('status','FAILED'),'step':step,'trial_index':trial_index,
            'trial_seed':allocation['trial_seed'],'run':run,'artifacts':artifacts}
    from .h50_deployment import run_h50_deployment
    try:
        run = run_h50_deployment(cfg,session,stop_event,step,args.duration,
            leader_isolated=True,path_reviewed=True,prepare_ready=False,
            current_start_confirmed=True,trial_index=trial_index,
            operator_skip_no_motion_acceptance=getattr(args,'operator_skip_no_motion_acceptance',False),**prompt_kwargs)
    except BaseException:
        _h50_preserve_partial(cfg,session,step,acceptance=False)
        raise
    session.report['h50_cli_run_receipt'] = run
    artifacts = _h50_finalize(cfg,session,step,run)
    return {'schema':'tiger-h50-cli-result-v1',
        'status':run.get('status','FAILED') if artifacts['compact'].get('validated') is True else 'ARTIFACT_PRESERVATION_FAILED',
        'run_status':run.get('status','FAILED'),'step':step,'trial_index':trial_index,
        'trial_seed':allocation['trial_seed'],'run':run,'artifacts':artifacts}


def _h50_exit_code(command, result):
    if command == 'h50-load':return 0 if result.get('status')=='MODEL_READY' else 1
    if command == 'h50-acceptance':
        compact = result.get('acceptance',{}).get('compact',{})
        return 0 if compact.get('status')=='PASS' and compact.get('validated') is True else 1
    good = ('NO_SEND_COMPLETED_REQUIRES_ROOT_AUDIT',) if command=='h50-shadow' else (
        'PLAN_ONLY_NO_HARDWARE','POLICY_WINDOW_COMPLETED_REQUIRES_OPERATOR_REVIEW')
    return 0 if result.get('status') in good else 1


def _compact_h50_json(command, report_path, result=None, error=None,*,full_receipt_saved=None):
    """One UTF-8 JSON object under 4 KiB; full objects remain on selected USB."""
    result = result or {}
    run = result.get('run',{})
    compact = {'schema':'tiger-h50-cli-summary-v1','command':command,'status':result.get('status','FAILED'),
        'step':result.get('step'),'trial_index':result.get('trial_index'),'trial_seed':result.get('trial_seed')}
    task = result.get('task', run.get('task'))
    if task is not None:compact['task'] = task
    for key in ('owned_recording_path','requested_measured','completed_measured','measured_deadline_passes',
                'measured_commit_ready_p50_ms','measured_commit_ready_p95_ms','measured_commit_ready_max_ms',
                'timing_basis','storage_semantic_version','storage_state',
                'module_CAN_api_calls','physical_task_success','root_acceptance_written'):
        value = run.get(key)
        if value is not None:compact[key] = value
    acceptance = result.get('acceptance',{}).get('compact')
    if isinstance(acceptance,dict):
        compact['acceptance'] = {k:acceptance[k] for k in ('status','step','validated','report','episode','manager_status','usb_finalflush_validated') if k in acceptance}
        errors = acceptance.get('errors')
        if isinstance(errors,list):compact['acceptance']['errors'] = [str(v)[:160] for v in errors[:3]]
    artifacts = result.get('artifacts',{}).get('compact')
    if isinstance(artifacts,dict):
        compact['artifacts'] = {k:artifacts[k] for k in ('status','validated','report','episode','scope','usb_finalflush_validated') if k in artifacts}
    # Data preservation and performance acceptance are separate results. Only
    # the independent artifact audit may tell the webpage that all streams are saved.
    preservation = acceptance if isinstance(acceptance,dict) else artifacts
    compact['usb_finalflush_validated'] = isinstance(preservation,dict) and preservation.get('usb_finalflush_validated') is True
    payload = {'report':None if report_path is None else str(report_path),'result':compact,
               'full_receipt_on_verified_USB':report_path is not None if full_receipt_saved is None else full_receipt_saved}
    if error is not None:
        payload['error'] = str(error)[:512]
        payload['automatic_reset_disable_return'] = False
    def encode():return json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':'))
    text = encode()
    if len(text.encode('utf-8'))>=4000:
        compact.pop('acceptance',None);compact.pop('artifacts',None);compact.pop('owned_recording_path',None)
        if 'error' in payload:payload['error'] = payload['error'][:128]
        text = encode()
    if len(text.encode('utf-8'))>=4000:
        payload.pop('report',None)
        payload['report_omitted_from_stdout'] = 'Path exceeds summary budget; operation.json remains on selected USB'
        text = encode()
    if len(text.encode('utf-8'))>=4000:
        payload['result'] = {'status':compact['status'],'command':command}
        payload['error'] = 'Summary fields exceed terminal budget; read USB operation.json'
        text = encode()
    if len(text.encode('utf-8'))>=4096:raise RuntimeError('H50 compact output exceeds 4 KiB')
    return text


def _shared_mode_snapshot(cfg, role):
    """Reuse the existing Hub; reject capture races before every mode send.

    A missing/unreachable Hub, missing recording flags or an active/saving
    recorder is a failure. There is no independent receiver fallback.
    """
    from .hub import rpc
    from .hardware import HardwareBlocked
    begin = time.monotonic()
    try:
        value = rpc(cfg, '/status', timeout=.5)
    except Exception as exc:
        raise HardwareBlocked('Existing shared Hub unavailable; no new receiver fallback') from exc
    elapsed = max(0., time.monotonic()-begin)
    recording = value.get('recording', {})
    if recording.get('active') is not False or recording.get('saving') is not False:
        raise HardwareBlocked('Controller mode change blocked: Hub recording active/saving/unknown')
    if value.get('task_state', {}).get('active') is True or value.get('recovery', {}).get('deployment_active') is True:
        raise HardwareBlocked('Controller mode change blocked: shared deployment/task is active')
    snapshot = value.get('arms', {}).get(role)
    if not isinstance(snapshot, dict):
        raise HardwareBlocked('Requested arm is unavailable in existing shared Hub; no new receiver fallback')
    snapshot = dict(snapshot)
    # Conservative transport age includes the whole local RPC. Raw timestamps
    # remain unchanged and use this same robot monotonic clock.
    for key in ('age_joint_s','age_status_s','age_motor_s','age_gripper_s'):
        age = snapshot.get(key)
        if type(age) in (int,float) and math.isfinite(age):
            snapshot[key] = age+elapsed
    snapshot['external_commands'] = {cid:{**frame,
        'last_age_s':frame['last_age_s']+elapsed if type(frame.get('last_age_s')) in (int,float) else frame.get('last_age_s')}
        for cid,frame in snapshot.get('external_commands',{}).items()}
    return snapshot


def _apply_shared_controller_action(cfg, args, session, stop_event):
    """Only the three new verbs use Hub snapshots and runtime capture guards."""
    from .hardware import PiperAdapter, sdk_profile_parameters
    from .modes import control_action, ModeOperationBlocked
    operation, role = args.mode_operation, args.arm
    if role != 'right':
        raise RuntimeError('New controller actions are authorized only for the right arm')
    high = operation == 'high-follow'
    required = (args.supported and args.teaching_ended and
                (args.roles_configured and args.poses_aligned and args.source_reviewed if high else args.leader_isolated))
    if not required:
        raise RuntimeError('Mode apply requires actual support and teaching-ended; high-follow requires roles-configured/poses-aligned/source-reviewed; standby/disable require leader-isolated')
    entry = cfg['can'][role]
    cancel = lambda: stop_event.is_set() or stop_requested(cfg)
    require_not_stopped(cfg)
    _shared_mode_snapshot(cfg, role)  # Reject before writer acquisition.
    with ExitStack() as stack:
        stack.enter_context(Ownership('writer-'+entry['interface']))
        parameters = {'interface':entry['interface'],'expected_serial':entry['expected_serial'],
                      'sdk_version':'0.6.2','leader_isolated':args.leader_isolated,
                      'max_feedback_age_s':.2,'native_speed_percent':cfg['native_speed_percent'],
                      'roles_configured':args.roles_configured,'poses_aligned':args.poses_aligned,
                      'source_reviewed':args.source_reviewed,
                      'gripper_bounds_m':cfg.get('gripper_bounds_m',(-.0013,.1))}
        parameters.update(sdk_profile_parameters(cfg,role))
        events=deque(maxlen=64)
        adapter=PiperAdapter(parameters, lambda:_shared_mode_snapshot(cfg,role), events.append)
        stack.callback(adapter.close)
        stack.callback(capture_writer_receipt,role,adapter,events,session)
        require_not_stopped(cfg)
        if cancel():
            raise RuntimeError('STOP/cancel before writer acquisition')
        _shared_mode_snapshot(cfg, role)
        adapter.connect_for_apply(**({'high_follow_only':True} if high else
                                    {'disable_only':True} if operation=='disable' else {'mode_only':True}))
        try:
            result=control_action(adapter,operation,supported=args.supported,teaching_ended=args.teaching_ended,
                roles_configured=args.roles_configured,poses_aligned=args.poses_aligned,
                source_reviewed=args.source_reviewed,cancel=cancel)
            session.report['mode_receipt']=result
            return result
        except ModeOperationBlocked as exc:
            session.report['mode_receipt']=exc.receipt
            raise


def _finish_deployment_cleanup(cfg, raw_rpc, session, recording_path, cleanup_errors, original_error):
    """Finalize only this deployment's recording; never mask its original fault."""
    def task_event(event, **receipt):
        if cfg.get("task_state", {}).get("enabled") is True:
            try:
                data = {"session_id": session.path.name, "event": event, "receipt": receipt}
                publisher = getattr(session, "_task_publisher", None)
                if publisher is not None:
                    publisher.publish(event, **receipt)
                    return
                raw_rpc(cfg, "/task/event", data, timeout=1)
            except Exception as exc:
                session.report.setdefault("task_cleanup_reporting_errors", []).append(repr(exc))
    if original_error is not None and session.report.get("motion_phase") == "preflight":
        task_event("return_skipped", reason="Deployment preflight failed; no automatic return")
        task_event("fault", area="preflight", reason=repr(original_error))
    if recording_path is not None:
        from .recording_lifecycle import preserving_rpc, stop_owned_recording
        try:
            def rpc(config, path, data=None, timeout=20):
                return preserving_rpc(raw_rpc, config, path, data, timeout)
            receipt = stop_owned_recording(cfg, rpc, recording_path, timeout=300)
            session.report["recording_cleanup"] = receipt
            session.report["recording_closed"] = receipt.get("last_recording_status")
            if receipt["healthy"]:
                task_event("recording_completed", saved=True, episode_path=recording_path)
            elif not receipt.get("completed") and not receipt.get("last_recording_status", {}).get("error"):
                task_event("recording_pending", reason=str(receipt.get("failure")), episode_path=recording_path,
                           motion_closed=not any("host CAN socket close" in e for e in cleanup_errors))
            else:
                task_event("recording_failed", reason=str(receipt.get("failure")), episode_path=recording_path)
            if not receipt["healthy"]:
                raise RuntimeError("Deployment recording finalization failed: " + str(receipt["failure"]))
        except Exception as cleanup_error:
            session.report["recording_cleanup_error"] = repr(cleanup_error)
            cleanup_errors.append("recording close: " + repr(cleanup_error))
    session.report["cleanup_errors"] = cleanup_errors
    publisher = getattr(session, "_task_publisher", None)
    if publisher is not None:
        session.report["task_event_delivery"] = publisher.close()
    if cleanup_errors and original_error is None:
        raise RuntimeError("Deployment cleanup incomplete: " + repr(cleanup_errors))


def parser(cfg=None):
    cfg = {} if cfg is None else cfg
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Receive-only dual arm/camera/ROS status")
    status.add_argument("--json", action="store_true", help="Print the full report/result JSON instead of the short summary")
    d = commands.add_parser("diagnose", help="Receive-only bounded link/gap logging")
    d.add_argument("--seconds", type=float, default=3)
    d.add_argument("--json", action="store_true", help="Print the full report/result JSON instead of the short summary")
    limits = commands.add_parser("limits", help="Six query-only CAN commands, no motion/enable")
    limits.add_argument("--arm", choices=("right", "left"), default="right")
    mode = commands.add_parser("mode", help="Inspect real mode or explicitly prepare/apply bounded takeover or supported reset")
    mode.add_argument("mode_operation", choices=("inspect", "takeover", "reset", "high-follow", "standby", "disable"))
    mode.add_argument("--arm", choices=("right", "left"), default="right")
    mode.add_argument("--seconds", type=float, default=1.0, help="Receive-only inspection window, 0.3..5 seconds")
    mode.add_argument("--json", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--leader-isolated", action="store_true")
    mode.add_argument("--supported", action="store_true", help="Arm has actual reliable support; reset immediately loses motor holding power")
    mode.add_argument("--teaching-ended", action="store_true", help="Physical teaching/trajectory playback has actually ended")
    mode.add_argument("--zero-pose-reviewed", action="store_true", help="Required for reset: manufacturer mechanical zero posture prerequisite was reviewed; does not write calibration")
    mode.add_argument("--roles-configured", action="store_true", help="Existing OEM physical leader/follower roles were reviewed; never writes roles")
    mode.add_argument("--poses-aligned", action="store_true", help="Actual leader/follower mechanical poses were confirmed aligned")
    mode.add_argument("--source-reviewed", action="store_true", help="Existing OEM target stream origin was reviewed; CAN IDs do not authenticate sender")
    grip = commands.add_parser("gripper", help="Shared-Hub metrology or explicit empty-gripper calibration; no implicit motion")
    grip.add_argument("gripper_operation", choices=("inspect", "measure", "disable", "zero", "cpv-plan"))
    grip.add_argument("--arm", choices=("right",), default="right")
    grip.add_argument("--seconds", type=float, default=2.)
    grip.add_argument("--label", choices=("closed", "mid", "open"), default="closed")
    grip.add_argument("--measured-gap-mm", type=float)
    grip.add_argument("--apply", action="store_true")
    grip.add_argument("--leader-isolated", action="store_true")
    grip.add_argument("--empty-gripper", action="store_true")
    grip.add_argument("--physically-closed", action="store_true")
    grip.add_argument("--coordinate-change-reviewed", action="store_true")
    manual_grip = commands.add_parser('gripper-move',help='Plan or explicitly move only the right gripper to an mm target')
    manual_grip.add_argument('--target-mm',type=float,required=True)
    manual_grip.add_argument('--speed-mm-s',type=float)
    manual_grip.add_argument('--apply',action='store_true')
    manual_grip.add_argument('--leader-isolated',action='store_true')
    manual_grip.add_argument('--path-reviewed',action='store_true')
    move = commands.add_parser("move", help="Plan or execute ready/joint-zero; never coordinate calibration")
    move.add_argument("target", choices=("ready", "zero", "target"))
    move.add_argument("--angles-deg", nargs=6, type=float, help="Explicit six-joint draft; degrees, not coordinate calibration")
    move.add_argument("--speed-deg-s", type=float, help="Pose reference speed; positive and no greater than configured pose cap")
    move.add_argument("--arm", choices=("right", "left", "both", "can-movej"), default="right")
    move.add_argument("--expected-arms", nargs="+", help="Frozen registered CAN/MOVEJ targets; zero-only")
    move.add_argument("--apply", action="store_true")
    move.add_argument("--close-gripper", action="store_true", help="Right ready/zero only: close empty gripper during reviewed joint pose; verify both")
    move.add_argument("--gripper-speed-mm-s", type=float, help="Positive preparation reference speed, at most 10 mm/s; actual physical speed needs verification")
    move.add_argument("--json", action="store_true", help="Print the full report/result JSON for a plan-only move")
    move.add_argument("--leader-isolated", action="store_true", help="Physical control-source isolation has actually been established")
    move.add_argument("--path-reviewed", action="store_true", help="Both actual sweep and final pose were reviewed clear; no collision certificate")
    h = commands.add_parser("hub-start", help="One shared 3-camera LeRobot/Rerun receive-only producer")
    h.add_argument("--takeover-existing", action="store_true", help="Gracefully stop ONLY the exact identified old read-only producer")
    commands.add_parser("hub-serve", help=argparse.SUPPRESS)
    commands.add_parser("hub-stop", help="Stop this new sensor process and finish its recording; no arm command")
    r = commands.add_parser("record-start", help="Start native 30Hz sample/real JPEG review recording")
    r.add_argument("--task", required=True)
    r.add_argument('--batch-id')
    r.add_argument('--trial-id')
    r.add_argument('--category')
    r.add_argument('--notes',default='')
    retry = commands.add_parser('record-retry',help='Explicitly retry/recover same frozen trial; bound episode replays only')
    retry.add_argument('--batch-id',required=True)
    retry.add_argument('--trial-id',required=True)
    rs = commands.add_parser("record-stop", help="Wait until native USB writer closes")
    rs.add_argument('--episode',help='Exact expected episode path; refuses changed owner')
    rl = commands.add_parser("record-list", help="Page native episode summaries")
    rl.add_argument('--page',type=int,default=1)
    rl.add_argument('--page-size',type=int,default=10)
    co = commands.add_parser('collection',help='USB collection batches and human review; no hardware')
    subs = co.add_subparsers(dest='collection_command',required=True)
    cc = subs.add_parser('create')
    cc.add_argument('--task',required=True)
    cc.add_argument('--title',required=True)
    cc.add_argument('--operator',required=True)
    cc.add_argument('--target-count',type=int,default=100)
    cl = subs.add_parser('list')
    cl.add_argument('--batch-id',required=True)
    cl.add_argument('--page',type=int,default=1)
    cl.add_argument('--page-size',type=int,default=10)
    cr = subs.add_parser('review')
    cr.add_argument('--batch-id',required=True)
    cr.add_argument('--episode-id',required=True)
    cr.add_argument('--reviewer',required=True)
    cr.add_argument('--verdict',choices=('accepted','rejected'),required=True)
    cr.add_argument('--note',required=True)
    ex = commands.add_parser("export-right", help="Reviewed native episode -> LeRobot0.5.2, front/right+7D state/action")
    ex.add_argument("--episode", required=True)
    ex.add_argument("--reviewed", action="store_true")
    teleop = commands.add_parser("teleop-status", help="Observe leader command presence without changing role")
    teleop.add_argument("--json", action="store_true", help="Print the full report/result JSON instead of the short summary")
    commands.add_parser("teleop-enter", help="Report unsupported role transition rather than fake success")
    commands.add_parser("teleop-exit", help="Report unsupported leader isolation rather than blind dual-zero")
    commands.add_parser("model-plan", help="Exact checkpoint/source/precision/staging plan")
    commands.add_parser("model-stage", help="Authenticate/restore original files into new USB cache; no inference")
    ms = commands.add_parser("model-start", help="Start hardware-free local model server after staging")
    ms.add_argument("--num-steps", type=int, choices=(5, 10), default=cfg.get("sampler_steps", 5))
    mf = commands.add_parser("model-serve", help=argparse.SUPPRESS)
    mf.add_argument("--num-steps", type=int, choices=(5, 10), default=cfg.get("sampler_steps", 5))
    commands.add_parser("model-status", help="Service identity/warmup parameters")
    commands.add_parser("model-stop", help="Stop ONLY this new model process; no arm command")
    sh = commands.add_parser("shadow", help="Real front/right inputs, model outputs only; no CAN writer")
    sh.add_argument("--samples", type=int, default=3)
    fp32 = commands.add_parser("shadow-fp32", help="4090 FP32/H50 with native USB capture; zero action dispatch")
    fp32.add_argument("--count", type=int, default=3)
    fp32.add_argument("--task", required=True)
    for verb in ('h50-load','h50-shadow','h50-acceptance','deploy-h50'):
        h50 = commands.add_parser(verb,help='Selected 4090 FP32 H50/RTC; full receipts on verified USB')
        h50.add_argument('--step',type=int,choices=(2000,5000,10000,15000,20000),
                         default=cfg.get('h50_deployment',{}).get('default_step',5000))
        if verb == 'deploy-h50':
            h50.add_argument('--task',default=cfg.get('h50_deployment',{}).get('task','右臂将红色方块放入绿色桶'))
            h50.add_argument('--duration',type=float,default=30.)
            h50.add_argument('--apply',action='store_true')
            h50.add_argument('--leader-isolated',action='store_true')
            h50.add_argument('--path-reviewed',action='store_true',help='Reviewed sweep includes the confirmed current start')
            h50.add_argument('--operator-skip-no-motion-acceptance',action='store_true',
                help='Explicit manual experiment without100-call performance acceptance; hardware/feedback/STOP/deadline guards remain')
        elif verb == 'h50-shadow':
            h50.add_argument('--count',type=int,choices=range(1,6),default=3)
            h50.add_argument('--task',default=cfg.get('h50_deployment',{}).get('task','右臂将红色方块放入绿色桶'))
    dep = commands.add_parser("deploy", help="Prepare10deg/s -> timed asyncRTC -> normal zero10deg/s")
    dep.add_argument("--duration", type=float, default=30)
    dep.add_argument("--apply", action="store_true")
    dep.add_argument("--leader-isolated", action="store_true")
    dep.add_argument("--path-reviewed", action="store_true")
    commands.add_parser("stop", help="Latch no-new-target software stop; not motor disable/E-stop")
    commands.add_parser("clear-stop", help="Clear this software latch only; does not start motion")
    return p


def clean_env(cfg, role):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("ROS", "AMENT", "COLCON", "CATKIN")) or key in ("PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH"):
            env.pop(key, None)
    env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
               LD_LIBRARY_PATH=cfg[role + "_library_path"])
    return env


def foreground_process(cfg, root, role, operation, *arguments):
    return subprocess.run([cfg[role + "_python"], "-I", "-B", str(root / ENTRY), operation, *arguments],
                          env=clean_env(cfg, role)).returncode


def _runtime_pid_path(name):
    directory = Path("/run/user") / str(os.getuid()) / "tiger-agilex-unified"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / (name + ".json")


def _registered_child(root, operation):
    """Read exact process identity; uncertain ownership never permits replacement."""
    source = _runtime_pid_path(operation)
    if not source.exists():
        return None, False
    saved = json.loads(source.read_text(encoding="utf-8"))
    command = saved.get("command") if isinstance(saved, dict) else None
    registered_root = (Path(saved['root']) if isinstance(saved,dict) and
                       isinstance(saved.get('root'),str) and saved['root'] else None)
    if (not isinstance(saved, dict) or type(saved.get("pid")) is not int or saved["pid"] <= 0
            or saved.get("operation") != operation or registered_root is None or not registered_root.is_absolute()
            or '..' in registered_root.parts
            or not isinstance(command, list) or len(command) < 5
            or not all(isinstance(item, str) for item in command)
            or command[3:5] != [str(registered_root / ENTRY), operation]
            or not isinstance(saved.get("proc_start_ticks"), str)):
        raise RuntimeError("Registered service identity is invalid; inspect it before launching or stopping")
    proc = Path("/proc") / str(saved["pid"])
    try:
        stat = (proc / "stat").read_text().rsplit(") ", 1)[1].split()
        if stat[0] in ("Z", "X"):
            return saved, False
        actual = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        checked_stat = (proc / "stat").read_text().rsplit(") ", 1)[1].split()
    except FileNotFoundError:
        return saved, False
    if (stat[19] != saved["proc_start_ticks"] or checked_stat[19] != stat[19]
            or actual != [item.encode() for item in command]):
        raise RuntimeError("Registered PID identity changed; refusing to replace or signal an unrelated process")
    if checked_stat[0] in ("Z", "X"):
        return saved, False
    if registered_root != Path(root):
        raise RuntimeError('Registered service on another USB is still running; stop its verified owner before switching')
    return saved, True


def child(cfg, root, role, operation, *arguments):
    """Serialize registration before ports bind; retain the first live service PID."""
    with Ownership("service-launch-" + operation):
        saved, alive = _registered_child(root, operation)
        if alive:
            raise RuntimeError(f"Registered {operation} PID {saved['pid']} is still running; no duplicate launch")
        if operation == "hub-serve":
            _wait_hub_exit(root, cfg["hub_port"])
        session = Session(cfg, operation + "-launch")
        command = [cfg[role + "_python"], "-I", "-B", str(root / ENTRY), operation, *arguments]
        with (session.path / "service.log").open("xb") as output:
            process = subprocess.Popen(command, env=clean_env(cfg, role), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        proc_start = Path(f"/proc/{process.pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
        receipt = {"pid": process.pid, "proc_start_ticks": proc_start, "operation": operation,
                   "command": command, "root": str(root), "log": str(session.path / "service.log")}
        source = _runtime_pid_path(operation)
        pending = source.with_suffix(".json.tmp")
        pending.write_text(json.dumps(receipt), encoding="utf-8")
        pending.replace(source)
        session.finish("PROCESS_LAUNCHED_READINESS_NOT_VERIFIED", **receipt)
        return receipt


def _bounded_hub_probe(cfg, root, rpc, expected_child, timeout_s):
    """Bound a read-only USB/process/status probe, including slow RPC headers.

    A timed-out probe is terminal; the caller never accumulates retry threads.
    The daemon thread has no device/SDK/start/stop/dispatch operation.
    """
    import queue
    outcome = queue.Queue(maxsize=1)

    def probe():
        try:
            verify_usb(cfg)
            saved, alive = _registered_child(root, 'hub-serve')
            if not alive:
                raise RuntimeError('Registered Hub child exited or is missing')
            if expected_child is not None and any(saved.get(k) != expected_child.get(k)
                                                   for k in ('pid', 'proc_start_ticks', 'root')):
                raise RuntimeError('Registered Hub identity changed during readiness')
            begin = time.monotonic()
            status = rpc(cfg, '/status', timeout=min(.5, timeout_s))
            checked, alive = _registered_child(root, 'hub-serve')
            if not alive or any(checked.get(k) != saved.get(k) for k in ('pid', 'proc_start_ticks', 'root')):
                raise RuntimeError('Registered Hub exited/replaced while reading status')
            verify_usb(cfg)
            outcome.put((True, (saved, status, time.monotonic() - begin)))
        except Exception as exc:
            outcome.put((False, exc))

    worker = threading.Thread(target=probe, name='bounded-hub-readiness-probe', daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise RuntimeError('Hub read-only probe exceeded its bounded deadline; no retry or duplicate launch')
    ok, result = outcome.get_nowait()
    if not ok:
        raise result
    return result


def _hub_readiness_errors(cfg, value, registered, transport_age_s):
    """Validate status provenance/profile; return only still-starting reasons."""
    from .capture_profile import contract
    from .hub import hub_storage_binding
    if not isinstance(value, dict) or value.get('schema') != 'tiger-unified-sensor-hub-v1':
        raise RuntimeError('Wrong Hub status schema')
    producer = value.get('producer')
    if (not isinstance(producer, dict) or producer.get('schema') != 'tiger-hub-producer-identity-v1'
            or type(producer.get('pid')) is not int or producer['pid'] != registered['pid']
            or not isinstance(producer.get('proc_start_ticks'), str)
            or producer['proc_start_ticks'] != registered['proc_start_ticks']):
        raise RuntimeError('Hub /status producer PID/start_ticks differ from the registered live child')
    expected_storage = hub_storage_binding(cfg)
    if expected_storage is None or value.get('storage_binding') != expected_storage:
        raise RuntimeError('Hub readiness requires the exact frozen USB storage binding')
    capture = contract(cfg)
    if value.get('recording_contract', {}).get('capture_contract') != capture:
        raise RuntimeError('Hub capture profile differs from the frozen requested profile')
    if (type(transport_age_s) not in (int, float) or not math.isfinite(transport_age_s)
            or transport_age_s < 0):
        raise RuntimeError('Invalid status transport age')
    pending, frames = [], {}
    for role in capture['required_camera_roles']:
        camera = value.get('cameras', {}).get(role)
        if not isinstance(camera, dict):
            pending.append('Missing camera: ' + role)
            continue
        if camera.get('serial') != cfg['cameras'][role]:
            raise RuntimeError('Camera physical serial mismatch: ' + role)
        if (role == 'front' and camera.get('recovery_state') in ('waiting_for_device', 'opening')
                and camera.get('thread_alive') is True and camera.get('alignment_publication_error') is None):
            pending.append('Front camera reconnect: ' + camera['recovery_state'])
            continue
        if camera.get('error') is not None or camera.get('alignment_publication_error') is not None:
            raise RuntimeError('Camera worker failed: ' + role + ': ' + repr(camera.get('error') or camera.get('alignment_publication_error')))
        profile = camera.get('profile')
        if profile is None:
            pending.append('Camera profile not available: ' + role)
        elif (not isinstance(profile, dict) or any(profile.get(k) != v for k, v in
                 {'width': 640, 'height': 480, 'backend': 'V4L2', 'fourcc': 'MJPG'}.items())
              or type(profile.get('fps')) not in (int, float) or not math.isfinite(profile['fps'])
              or abs(profile['fps'] - 30) > .01):
            raise RuntimeError('Actual camera profile mismatch: ' + role)
        count, age = camera.get('frames'), camera.get('age_s')
        if type(count) is not int or count <= 0:
            pending.append('No real camera frames: ' + role)
        else:
            frames[role] = count
        if (camera.get('fresh') is not True or camera.get('thread_alive') is not True
                or type(age) not in (int, float) or not math.isfinite(age)
                or age < 0 or age + transport_age_s > .5):
            pending.append('Camera is not fresh/alive: ' + role)
    # Follow the selected capture contract. Excluded arms remain visible, and
    # cannot silently block a right-only profile or masquerade as required data.
    for role in capture['required_arm_roles']:
        arm = value.get('arms', {}).get(role)
        failure = value.get('observer_start_errors', {}).get(role)
        if failure is not None:
            raise RuntimeError('Required CAN observer failed: ' + role + ': ' + repr(failure))
        if not isinstance(arm, dict):
            pending.append('Missing arm feedback: ' + role)
            continue
        if arm.get('observer_error') is not None:
            raise RuntimeError('Required CAN observer error: ' + role + ': ' + repr(arm['observer_error']))
        joints, grip = arm.get('q_deg'), arm.get('grip_m')
        valid_q = (isinstance(joints, list) and len(joints) == 6 and
                   all(type(v) in (int, float) and math.isfinite(v) for v in joints))
        valid_grip = type(grip) in (int, float) and math.isfinite(grip)
        ages = [arm.get(k) for k in ('age_joint_s', 'age_status_s', 'age_gripper_s')]
        fresh_ages = all(type(age) in (int, float) and math.isfinite(age) and
                         0 <= age and age + transport_age_s <= .2 for age in ages)
        if (arm.get('observer_running') is not True or arm.get('feedback_fresh') is not True
                or not valid_q or not valid_grip or not fresh_ages):
            pending.append('Actual joint/gripper/status feedback not fresh: ' + role)
    return pending, frames, capture


def _wait_hub_ready(cfg, root, rpc, *, expected_child=None, timeout_s=30., poll_s=.2,
                    stop_event=None, session=None):
    """No motion: require two valid snapshots with advancing camera counters.

    Failure preserves the registered child/log for inspection. It never kills,
    restarts, opens another receiver, or relaxes a motion/controller guard.
    """
    import urllib.error
    if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 60
            or type(poll_s) not in (int, float) or not math.isfinite(poll_s) or not .05 <= poll_s <= 1):
        raise ValueError('Hub readiness timeout/poll outside bounded ranges')
    begin = time.monotonic()
    deadline = begin + timeout_s
    progress = {'schema': 'tiger-hub-readiness-v1', 'status': 'WAITING', 'timeout_s': timeout_s,
                'polls': 0, 'child': expected_child, 'last_pending': [],
                'new_receiver_opened_by_readiness': False, 'motion_authorized': False}
    if session is not None:
        session.report['hub_readiness'] = progress
    initial_frames = None
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError('Hub readiness cancelled; child/log retained')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Hub readiness deadline expired: ' + '; '.join(progress['last_pending']))
            progress['polls'] += 1
            try:
                registered, value, age = _bounded_hub_probe(cfg, root, rpc, expected_child, min(1., remaining))
            except (OSError, urllib.error.URLError) as exc:
                progress['last_pending'] = ['Status transport not ready: ' + repr(exc)]
                initial_frames = None
            else:
                expected_child = registered
                progress['child'] = registered
                pending, frames, capture = _hub_readiness_errors(cfg, value, registered, age)
                progress['last_pending'] = pending
                progress['camera_frames_latest'] = frames
                if pending:
                    initial_frames = None
                elif initial_frames is None:
                    initial_frames = dict(frames)
                    progress['camera_frames_initial'] = initial_frames
                    progress['last_pending'] = ['Waiting for all required camera counters to advance']
                else:
                    if any(frames[role] < count for role, count in initial_frames.items()):
                        raise RuntimeError('Camera frame counter regressed in the same Hub producer')
                    if all(frames[role] > count for role, count in initial_frames.items()):
                        progress.update(status='READY', elapsed_s=time.monotonic()-begin,
                                        last_pending=[], capture_contract=capture)
                        return {'status': 'HUB_READY', 'sensors': value, 'readiness': progress}
                    progress['last_pending'] = ['Required camera counter did not advance']
            time.sleep(min(poll_s, max(0., deadline-time.monotonic())))
    except Exception as exc:
        progress.update(status='FAILED', elapsed_s=time.monotonic()-begin, error=repr(exc))
        log = expected_child.get('log') if isinstance(expected_child, dict) else None
        raise RuntimeError('Hub failed real readiness; child/log retained at %s: %s' % (log, exc)) from exc


def _wait_hub_exit(root, port, *, timeout_s=25.):
    """Wait for the registered owner and verify the socket can be rebound."""
    deadline = time.monotonic() + timeout_s
    while True:
        _, alive = _registered_child(root, "hub-serve")
        if not alive:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE:
                        raise
                else:
                    return
        if time.monotonic() >= deadline:
            raise RuntimeError("Hub stop pending: old process or listening port has not been released")
        time.sleep(.1)


def stop_child(root, operation, *, hub_port=None):
    with Ownership("service-launch-" + operation):
        saved, alive = _registered_child(root, operation)
        if hub_port is not None:
            if alive:
                try:
                    os.kill(saved["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
            _wait_hub_exit(root, hub_port)
            return {**(saved or {"operation": operation}), "status": "HUB_EXIT_AND_PORT_RELEASE_VERIFIED"}
        if saved is None:
            return {"status": "NO_REGISTERED_PROCESS", "operation": operation}
        if not alive:
            return {"status": "REGISTERED_PROCESS_ALREADY_EXITED", **saved}
        os.kill(saved["pid"], signal.SIGTERM)
        return {"status": "SIGTERM_REQUESTED_NOT_YET_CONFIRMED", **saved}


def old_camera_owner_handoff(cfg):
    """Only exact read-only producer, never wildcard kill or original ROS script."""
    source = Path(cfg["legacy_producer"])
    if sha256(source) != cfg["legacy_producer_sha256"]:
        raise RuntimeError("Legacy producer source changed; inspect owner handoff first")
    matches = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                parts = (proc / "cmdline").read_bytes().split(b"\0")
                if str(source).encode() in parts:
                    matches.append(int(proc.name))
            except (PermissionError, FileNotFoundError):
                pass
    if len(matches) > 1:
        raise RuntimeError("Multiple legacy camera producers; no automatic handoff")
    for pid in matches:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 12
        while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
            time.sleep(.1)
        if Path(f"/proc/{pid}").exists():
            raise RuntimeError("Old producer did not release; no force kill or duplicate camera open")
    return matches


def require_capture_owner_clear():
    from .sensors import camera_inventory
    inventory = camera_inventory()
    owners = [owner for info in inventory["cameras"].values() for owner in info["open_owners"]]
    if owners:
        raise RuntimeError("Camera device still held by another process: " + repr(owners))
    return inventory


def ensure_bounds(cfg, observer, session, leader_isolated=False):
    """Query exact controller responses; intersect with SDK documented envelope.

    No source pretends that a valid command boundary proves collision freedom.
    Neither aggregate SDK cached objects nor training ranges are used as limits.
    """
    from .hardware import PiperAdapter, DOCUMENTED_COMMAND_LIMITS_DEG, sdk_profile_parameters
    events = deque(maxlen=64)
    entry = cfg["can"]["left" if observer.interface == "can_left" else "right"]
    parameters = {"interface": entry["interface"], "expected_serial": entry["expected_serial"],
        "sdk_version": "0.6.2", "leader_isolated": leader_isolated, "max_feedback_age_s": .2,
        "native_speed_percent": cfg["native_speed_percent"], "gripper_bounds_m": cfg["gripper_bounds_m"],
        "gripper_effort_mNm": cfg["gripper_effort_mNm"], "command_limits_deg": DOCUMENTED_COMMAND_LIMITS_DEG}
    parameters.update(sdk_profile_parameters(cfg,'left' if observer.interface=='can_left' else 'right'))
    adapter = PiperAdapter(parameters, observer.control_snapshot, events.append)
    try:
        adapter.connect_for_apply(query_only=True)
        receipt = adapter.query_limits(observer.latest_frame)
        session.event("MOTOR_SPECIFIC_RAW_LIMIT_REPLIES", receipt=receipt, query_packets=adapter.packets_sent)
    finally:
        adapter.close()
    bounds = []
    for native, documented in zip(receipt["limits"], DOCUMENTED_COMMAND_LIMITS_DEG):
        low, high = max(native["minimum_deg"], documented[0]), min(native["maximum_deg"], documented[1])
        if low >= high:
            raise RuntimeError("Controller/documented command envelopes do not intersect")
        bounds.append([low, high])
    parameters["command_limits_deg"] = bounds
    session.event("EXPLICIT_COMMAND_BOUNDS", bounds_deg=bounds,
                  basis="intersection of fresh raw replies and installed SDK documentation; not collision limits")
    return PiperAdapter(parameters, observer.control_snapshot, events.append), events


def capture_writer_receipt(role, adapter, events, session):
    """Keep bounded evidence on success AND failure; no high-rate disk writes."""
    receipt = {"packets_sent": adapter.packets_sent, "last_hardware_events": list(events)}
    if hasattr(adapter, "last_mode_handoff"):
        receipt["last_mode_handoff"] = adapter.last_mode_handoff
    try:
        receipt["final_observed_feedback"] = adapter.snapshot()
    except Exception as exc:
        receipt["feedback_capture_error"] = repr(exc)
    session.report.setdefault("hardware_receipts", {})[role] = receipt
    session.report.setdefault("packets_sent_by_arm", {})[role] = adapter.packets_sent
    # Preserve the existing single-arm report keys for readers of older receipts.
    session.report.update(packets_sent=adapter.packets_sent, last_hardware_events=list(events))


def _apply_flags(args):
    if not args.leader_isolated or not args.path_reviewed:
        raise RuntimeError("--apply requires actual --leader-isolated and --path-reviewed conditions, not a software shortcut")


def render_console_summary(command, result, report_path):
    """Format existing evidence only; never access hardware or change the result.

    The full unmodified result remains in the USB operation.json. Missing fields
    stay unknown, and a completed read/plan is never described as motion success.
    """
    def mapping(value):
        return value if isinstance(value, dict) else {}

    def number(value, places=3):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return f"{value:.{places}f}" if places else str(value)
        return "未知"

    def boolean(value, yes="是", no="否"):
        return yes if value is True else no if value is False else "未知"

    def angles(value):
        return "[" + ", ".join(number(item) for item in value) + "]" if isinstance(value, (list, tuple)) else "未知"

    def arm_lines(role, state):
        state = mapping(state)
        label = {"right": "右执行臂", "left": "左执行臂"}.get(role, str(role))
        status = mapping(state.get("status"))
        lines = [f"{label}：J1–J6 {angles(state.get('q_deg'))} °；夹爪 {number(state.get('grip_m'), 4)} m",
                 "  反馈新鲜=" + boolean(state.get("feedback_fresh", state.get("fresh")))
                 + f"；控制器={status.get('arm_status_name', '未知')} / {status.get('ctrl_mode_name', '未知')}"
                 + f"；错误码={number(status.get('err_code'), 0)}"]
        enabled = state.get("enabled")
        enabled_text = "[" + ", ".join(boolean(item) for item in enabled) + "]" if isinstance(enabled, (list, tuple)) else "未知"
        faults = state.get("motor_fault_bits")
        lines.append("  电机使能 J1–J6=" + enabled_text + "；故障位=" + (str(faults) if isinstance(faults, list) else "未知"))
        duration = number(state.get("duration_s"))
        hz = mapping(state.get("per_id_observed_hz"))
        lines.append(f"  本次约 {duration} s 观察：关节反馈 0x2a5/0x2a6/0x2a7="
                     + "/".join(number(hz.get(key), 1) for key in ("0x2a5", "0x2a6", "0x2a7"))
                     + " Hz（消息频率，不是录制/执行频率）")
        counters = mapping(state.get("interface_counters"))
        delta = mapping(state.get("interface_counter_delta"))
        lines.append(f"  接收丢弃：本次增量 {number(delta.get('rx_dropped'), 0)}；接口累计 {number(counters.get('rx_dropped'), 0)}"
                     + f"；本次 rx_errors={number(delta.get('rx_errors'), 0)}；错误帧={number(state.get('error_frames'), 0)}"
                     + f"；本接收器溢出={number(state.get('socket_rx_overflow_packets'), 0)}")
        if state.get("interface_counters_reset") is True:
            lines.append("  注意：接口计数器本次出现重置，负增量不能当作零丢包。")
        for key in ("observer_error", "interface_counters_error"):
            if state.get(key):
                lines.append("  读取异常：" + str(state[key]))
        if state.get("required_missing_ids"):
            lines.append("  尚未收到必要反馈帧：" + ", ".join(state["required_missing_ids"]))
        external = state.get("external_commands")
        if isinstance(external, dict) and external:
            observed = [f"{cid}={number(info.get('count'), 0)}" for cid, info in external.items()
                        if isinstance(info, dict) and isinstance(info.get("count"), (int, float)) and info["count"] > 0]
            all_counts_known = all(isinstance(info, dict) and isinstance(info.get("count"), (int, float))
                                   for info in external.values())
            details = ", ".join(observed) if observed else "本窗口已记录计数均为零" if all_counts_known else "计数未完整提供"
            if observed and not all_counts_known:
                details += "（其余计数未完整提供）"
            lines.append("  总线观察到的外部控制帧：" + details
                         + "；不能据此确定发送者或证明主臂隔离。")
        else:
            lines.append("  外部控制帧：未知，不能证明主臂隔离。")
        return lines

    data = mapping(result)
    if command == "move":
        lines = ["位姿计划已生成；未执行运动。无 --apply 的 move 命令只读取反馈并计算参考轨迹。"]
        for role, outcome in data.items():
            outcome = mapping(outcome)
            lines.extend(arm_lines(role, outcome.get("actual")))
            plan = mapping(outcome.get("plan"))
            lines.extend(["  参考起点=" + angles(plan.get("start_deg")) + " °",
                          "  参考目标=" + angles(plan.get("target_deg")) + " °",
                          f"  参考速度上限={number(plan.get('max_speed_deg_s'), 1)} °/s；加速度上限={number(plan.get('max_accel_deg_s2'), 1)} °/s²",
                          f"  参考轨迹时长约 {number(plan.get('duration_s'))} s（不含模式交接、实际跟踪及到位等待）",
                          "  plan_only=" + boolean(outcome.get("plan_only"))
                          + "；本次实际查询控制器限位=" + boolean(outcome.get("actual_controller_limit_queries_sent")),
                          "  碰撞路径验收=" + boolean(plan.get("collision_checked"))
                          + "；断电保护验收=" + boolean(plan.get("power_loss_protection"))])
        lines.append("计划完成不等于实际到位；此计划未切换模式、使能电机或发送关节运动目标。")
    else:
        lines = ["状态读取已完成；这是只读诊断，不是运动/录制/模型部署结果。",
                 "主机=" + str(data.get("hostname", "未知"))
                 + "；本次诊断发送 CAN 指令=" + number(data.get("CAN_commands_sent_by_this_diagnostic"), 0)]
        arms = mapping(data.get("arms"))
        for role in ("right", "left"):
            lines.extend(arm_lines(role, arms.get(role)))
        inventory = mapping(data.get("camera_inventory"))
        cameras = mapping(inventory.get("cameras"))
        for role, label in (("front", "头部"), ("right", "右腕"), ("left", "左腕")):
            camera = mapping(cameras.get(role))
            owners = camera.get("open_owners")
            pids = sorted({str(owner.get("pid", "未知")) for owner in owners if isinstance(owner, dict)}) if isinstance(owners, list) else None
            owner_text = ",".join(pids) if pids else "本次未找到占用者" if pids is not None else "未知"
            lines.append(f"{label}相机：设备枚举=" + boolean(camera.get("enumerated"))
                         + "；实时帧=" + boolean(camera.get("live_frames_verified"), "本次已验证", "本次未验证") + "；占用 PID=" + owner_text)
        denied = inventory.get("inaccessible_process_fd_directories")
        if isinstance(denied, int) and denied > 0:
            lines.append(f"相机占用检查不完整：{denied} 个进程目录不可读；设备枚举/占用不证明实时图像正常。")
        else:
            lines.append("相机设备枚举/占用不证明实时图像正常；实时验证按本次记录显示。")
        shared = mapping(data.get("shared_producer"))
        lines.append("共享采集服务 API 可达=" + boolean(shared.get("api_reachable")))
        if shared.get("error"):
            lines.append("  共享服务信息：" + str(shared["error"]))
        ros = mapping(data.get("ros"))
        lines.append("ROS master 可达=" + boolean(ros.get("master_reachable"))
                     + "；评估=" + str(ros.get("assessment", "未知"))
                     + "；通道已证明 clean=" + boolean(ros.get("ROS_channel_clean_proven")))
        if ros.get("error"):
            lines.append("  ROS 信息：" + str(ros["error"]))
        capability = mapping(data.get("mode_switch_capability"))
        if capability:
            lines.append("遥操作自动进入已验证=" + boolean(capability.get("automatic_enter_verified"))
                         + "；自动退出已验证=" + boolean(capability.get("automatic_exit_verified"))
                         + "；主臂信号阻断已验证=" + boolean(capability.get("leader_signal_blocking_verified")))
            if capability.get("blocker"):
                lines.append("  遥操作切换卡点：" + str(capability["blocker"]))
        lines.append("短时观察不证明长期无丢包；没有序号时，不能精确推算丢失帧数。")
    lines.extend(["完整原始报告：" + str(report_path), "加 --json 可在终端查看完整 report/result JSON。"])
    return "\n".join(lines)


def main(cfg, root):
    args = parser(cfg).parse_args()
    # A stop does not depend on new source synchronization or available USB.
    # This writes only a RAM stop channel plus USB latch when mounted.
    if args.command == "stop":
        print(json.dumps(stop(cfg), ensure_ascii=False, indent=2), flush=True)
        return 0
    try:
        verify_usb(cfg)
        session = Session(cfg, args.command)
    except Exception as exc:
        if args.command in H50_COMMANDS:
            print(_compact_h50_json(args.command,None,{'status':'FAILED','step':getattr(args,'step',None)},repr(exc)),flush=True)
            return 1
        raise
    stop_event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    # SIGTERM stops long control loops without disabling motors. Services handle
    # their own bounded cleanup and do not inherit a no-op SIGTERM indefinitely.
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    try:
        command = args.command
        if command == 'gripper-move':
            from .manual_gripper import run as manual_gripper_run
            result=manual_gripper_run(cfg,args,session,cancel=stop_event.is_set)
        elif command == "gripper":
            from .gripper_calibration import run as gripper_operation
            try:
                result = gripper_operation(cfg, args, session,
                    cancel=lambda: stop_event.is_set() or (args.apply and stop_requested(cfg)))
            except Exception as exc:
                if hasattr(exc, 'gripper_calibration_receipt'):
                    session.report['gripper_calibration_verification'] = exc.gripper_calibration_receipt
                raise
        elif command == "mode":
            from .modes import inspect as inspect_mode, takeover, reset as supported_reset, plan as mode_plan, ModeOperationBlocked
            operation = args.mode_operation
            if operation == "inspect" and args.apply:
                raise ValueError("Mode inspect is strictly receive-only; --apply is not accepted")
            if not math.isfinite(args.seconds) or not .3 <= args.seconds <= 5:
                raise ValueError("Mode inspect --seconds must be within 0.3..5")
            if operation != "inspect" and not args.apply:
                result = mode_plan(operation)
            elif operation in ('high-follow', 'standby', 'disable'):
                result = _apply_shared_controller_action(cfg, args, session, stop_event)
            else:
                if operation != "inspect":
                    required = (args.leader_isolated and args.supported
                                and (args.teaching_ended if operation == "takeover" else args.zero_pose_reviewed))
                    if not required:
                        raise RuntimeError("Mode apply requires actual leader isolation and physical support; takeover requires teaching ended; reset requires manufacturer zero-pose review")
                    require_not_stopped(cfg)
                from .sensors import CanObserver
                from .hardware import PiperAdapter, sdk_profile_parameters
                role = args.arm
                entry = cfg["can"][role]
                with ExitStack() as stack:
                    if operation != "inspect":
                        stack.enter_context(Ownership("writer-"+entry["interface"]))
                    observer = CanObserver(**entry).start()
                    stack.callback(observer.stop)
                    if operation == "inspect":
                        result = inspect_mode(observer.control_snapshot, seconds=args.seconds,
                                              cancel=stop_event.is_set)
                        session.report["mode_receipt"] = result
                    else:
                        parameters = {"interface": entry["interface"], "expected_serial": entry["expected_serial"],
                                      "sdk_version": "0.6.2", "leader_isolated": args.leader_isolated,
                                      "max_feedback_age_s": .2, "native_speed_percent": cfg["native_speed_percent"]}
                        parameters.update(sdk_profile_parameters(cfg,role))
                        events = deque(maxlen=64)
                        adapter = PiperAdapter(parameters, observer.control_snapshot, events.append)
                        stack.callback(adapter.close)
                        stack.callback(capture_writer_receipt, role, adapter, events, session)
                        cancel = lambda: stop_event.is_set() or stop_requested(cfg)
                        # Warm up receive-only feedback without opening a writer.
                        deadline = time.monotonic()+.3
                        while time.monotonic() < deadline:
                            if cancel():
                                raise RuntimeError("STOP/cancel before writer acquisition")
                            time.sleep(min(.02, max(0, deadline-time.monotonic())))
                        require_not_stopped(cfg)
                        adapter.connect_for_apply(mode_only=operation == "takeover", reset_only=operation == "reset")
                        try:
                            result = takeover(adapter, supported=args.supported, cancel=cancel) if operation == "takeover" else supported_reset(adapter, supported=args.supported, cancel=cancel)
                            session.report["mode_receipt"] = result
                        except ModeOperationBlocked as exc:
                            session.report["mode_receipt"] = exc.receipt
                            raise
                session.report["mode_receipt"] = result
        elif command in ("status", "diagnose", "teleop-status"):
            from .sensors import diagnostics
            result = diagnostics(duration=args.seconds if command == "diagnose" else 1,
                                 shared_url=f"http://127.0.0.1:{cfg['hub_port']}/status")
            if command == "teleop-status":
                from .hardware import teleoperation_capability
                result["mode_switch_capability"] = teleoperation_capability()
        elif command == "stop":
            result = stop(cfg)
        elif command == "clear-stop":
            result = clear_stop(cfg)
        elif command in ("teleop-enter", "teleop-exit"):
            from .hardware import enter_teleoperation, exit_teleoperation, teleoperation_capability
            session.report["capability"] = teleoperation_capability()
            session.save()
            (enter_teleoperation if command == "teleop-enter" else exit_teleoperation)()
        elif command == "hub-start":
            from .hub import rpc
            registered, alive = _registered_child(root, 'hub-serve')
            if alive:
                result = _wait_hub_ready(cfg, root, rpc, expected_child=registered,
                                         stop_event=stop_event, session=session)
                result['status'] = 'EXISTING_UNIFIED_HUB_READY'
            else:
                moved = old_camera_owner_handoff(cfg) if args.takeover_existing else []
                inventory = require_capture_owner_clear()
                launched = child(cfg, root, "hardware", "hub-serve")
                ready = _wait_hub_ready(cfg, root, rpc, expected_child=launched,
                                        stop_event=stop_event, session=session)
                result = {"handoff_stopped_exact_pids": moved, "inventory": inventory,
                          **launched, **ready}
        elif command == "hub-serve":
            from .hub import SensorHub
            with Ownership("camera-owner"):
                require_capture_owner_clear()
                SensorHub(cfg).run()
            result = {"status": "SENSOR_HUB_CLOSED"}
        elif command == "hub-stop":
            result = stop_child(root, "hub-serve", hub_port=cfg["hub_port"])
        elif command in ("record-start", "record-stop",'record-retry'):
            from .hub import rpc
            if command == 'record-retry':
                from .collection import CollectionStore
                from .collection_commands import retry_payload
                result = rpc(cfg,'/record/start',retry_payload(CollectionStore(cfg),args.batch_id,args.trial_id))
            elif command == "record-start":
                payload = {'task':args.task}
                if any((args.batch_id,args.trial_id,args.category,args.notes)):
                    if not all((args.batch_id,args.trial_id,args.category)):
                        raise ValueError('Collection start needs batch, trial and category together')
                    payload.update(batch_id=args.batch_id,trial_id=args.trial_id,category=args.category,notes=args.notes)
                result = rpc(cfg, "/record/start", payload)
            else:
                from .recording_lifecycle import preserving_rpc, stop_owned_recording
                def capture_rpc(config, path, data=None, timeout=20):
                    return preserving_rpc(rpc, config, path, data, timeout)
                current = capture_rpc(cfg, "/status?compact=1", timeout=2)["recording"]
                if args.episode is not None and current.get('path') != args.episode:
                    raise RuntimeError('Recording owner/path changed; no stop sent')
                if current.get("path") is None:
                    if current.get("active"):
                        raise RuntimeError("Active recording has no verified episode path; no blind stop")
                    result = {**current, "record_stop_no_active_episode": True}
                else:
                    receipt = stop_owned_recording(cfg, capture_rpc, current["path"], timeout=300)
                    session.report["recording_cleanup"] = receipt
                    if not receipt["healthy"]:
                        raise RuntimeError("Requested recording finalization failed: " + str(receipt["failure"]))
                    result = {**receipt["last_recording_status"], "finalization": receipt}
        elif command == "record-list":
            from .collection_commands import list_native
            result = list_native(cfg,root,args.page,args.page_size)
        elif command == 'collection':
            from .collection import CollectionStore
            store = CollectionStore(cfg)
            if args.collection_command == 'create':
                result = store.create(args.task,args.title,args.operator,args.target_count)
            elif args.collection_command == 'list':
                result = store.list(args.batch_id,args.page,args.page_size)
            else:
                if not args.note.strip():
                    raise ValueError('Human review requires evidence note')
                result = store.review(args.batch_id,args.episode_id,args.reviewer,args.verdict,args.note)
        elif command == "export-right":
            from .dataset import export_right
            result = export_right(cfg, root, args.episode, reviewed=args.reviewed)
        elif command == "model-plan":
            from .policy import model_plan
            result = model_plan(root)
        elif command == "model-stage":
            from .policy import stage_model
            with Ownership("model-stage"):
                result = stage_model(root)
        elif command == "model-start":
            with socket.socket() as check:
                if check.connect_ex(("127.0.0.1", cfg["model_port"])) == 0:
                    raise RuntimeError("Model port is occupied. Use model-status; no duplicate GPU service")
            if not (root / "model-cache/stage-verified.json").is_file():
                raise RuntimeError("Run model-stage explicitly first; no silent large restore")
            result = child(cfg, root, "model", "model-serve", "--num-steps", str(args.num_steps))
        elif command == "model-serve":
            from .policy import serve_model
            with Ownership("gpu0-model"):
                probe = subprocess.check_output(["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"], text=True, timeout=5)
                if probe.strip():
                    raise RuntimeError("GPU already has compute owners; no automatic process kill: " + probe)
                # Model service closes normally on SIGTERM; no arm access.
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
                serve_model(root, num_steps=args.num_steps, port=cfg["model_port"])
            result = {"status": "MODEL_SERVICE_CLOSED"}
        elif command == "model-status":
            from .policy import PolicyClient
            client = PolicyClient(url=f"http://127.0.0.1:{cfg['model_port']}", execution_hz=cfg["action_time_hz"])
            try:
                result = client.health()
            finally:
                client.close()
        elif command == "model-stop":
            result = stop_child(root, "model-serve")
        elif command == "shadow":
            from .hub import rpc
            from .policy import PolicyClient, shadow
            client = PolicyClient(url=f"http://127.0.0.1:{cfg['model_port']}", execution_hz=cfg["action_time_hz"])
            try:
                result = shadow(client, lambda: rpc(cfg, "/observation"), samples=args.samples,
                    event_callback=lambda name, record: session.event(name, record=record))
            finally:
                client.close()
        elif command == "shadow-fp32":
            from .fp32_shadow_run import run as run_fp32_shadow
            result = run_fp32_shadow(cfg, session, stop_event, args.count, args.task)
        elif command in H50_COMMANDS:
            result = _h50_command_logged(cfg,args,session,stop_event)
        elif command in ("move", "limits"):
            from .sensors import CanObserver
            from .hardware import plan_pose, READY_DEG, DOCUMENTED_COMMAND_LIMITS_DEG
            if command == "move":
                if args.target == "target":
                    if args.arm != "right" or args.angles_deg is None or not all(math.isfinite(x) for x in args.angles_deg):
                        raise ValueError("Custom target requires right arm and six finite actual-coordinate angles")
                    pose_target = list(args.angles_deg)
                else:
                    if args.angles_deg is not None:
                        raise ValueError("--angles-deg is only valid with move target")
                    pose_target = cfg["ready_deg"] if args.target == "ready" else cfg["joint_zero_deg"]
                pose_speed = cfg["pose_speed_deg_s"] if args.speed_deg_s is None else args.speed_deg_s
                if not math.isfinite(pose_speed) or not 0 < pose_speed <= cfg["pose_speed_deg_s"]:
                    raise ValueError("Pose speed must be positive and no greater than the configured cap")
            pose_cfg = cfg
            if command == "move":
                if args.close_gripper and (args.target not in ("ready", "zero") or args.arm != "right"):
                    raise ValueError("--close-gripper only supports reviewed right ready/zero poses")
                if args.gripper_speed_mm_s is not None:
                    if (not args.close_gripper or not math.isfinite(args.gripper_speed_mm_s)
                            or not 0 < args.gripper_speed_mm_s <= 10):
                        raise ValueError("Gripper speed requires --close-gripper and a positive at-most-10 mm/s value")
                    pose_cfg = {**cfg, "ready_gripper_speed_m_s": args.gripper_speed_mm_s/1000}
            if command == "move" and args.arm in ("left", "both") and args.target == "ready":
                raise RuntimeError("Training ready median is right-arm only; no left-arm median has been established")
            if command == "move" and args.apply:
                _apply_flags(args)
                require_not_stopped(cfg)
            can_zero = command == "move" and args.arm == "can-movej"
            if can_zero:
                if args.target != "zero" or args.close_gripper or args.gripper_speed_mm_s is not None:
                    raise ValueError("All-CAN zero changes only six joint targets")
                if pose_target != [0]*6:
                    raise ValueError("All-CAN six-zero requires an actual six-zero preset")
                from .can_zero import require_selection
                roles = tuple(sorted(cfg["can"]))
                expected = args.expected_arms
                if not expected or any(r not in roles for r in expected) or len(set(expected)) != len(expected):
                    raise ValueError("Missing or invalid frozen CAN/MOVEJ arm list")
            else:
                if command == "move" and args.expected_arms:
                    raise ValueError("--expected-arms requires --arm can-movej")
                roles = ("right", "left") if args.arm == "both" else (args.arm,)
            outcomes = {}
            if can_zero:
                session.report["can_zero_outcomes"] = outcomes
            prepared_adapters = {}
            with ExitStack() as stack:
                # Acquire all requested locks before any movement; separate
                # control commands cannot compete inside this new library.
                if command == "limits" or args.apply:
                    for role in roles:
                        stack.enter_context(Ownership("writer-" + cfg["can"][role]["interface"]))
                observers = {}
                if can_zero:
                    # All writer leases are held before reading the current set.
                    failures = {}
                    for role in roles:
                        try:
                            obs = CanObserver(**cfg["can"][role]).start()
                            observers[role] = obs
                            stack.callback(obs.stop)
                        except (OSError, RuntimeError) as exc:
                            failures[role] = {"observer_error":str(exc)}
                    time.sleep(.3)
                    states = {**failures, **{r:o.control_snapshot() for r,o in observers.items()}}
                    chosen = require_selection(cfg, states, expected)
                    session.report["can_zero_selection"] = chosen
                    roles = tuple(chosen["targets"])
                for role in roles:
                    observer = observers.get(role) if can_zero else CanObserver(**cfg["can"][role]).start()
                    if not can_zero:
                        stack.callback(observer.stop)
                        time.sleep(.3)
                    if command == "move" and not args.apply:
                        state = observer.snapshot()
                        target = pose_target
                        outcomes[role] = {"actual": state, "plan": plan_pose(state["q_deg"], target,
                            DOCUMENTED_COMMAND_LIMITS_DEG, max_speed_deg_s=pose_speed).as_dict(),
                            "actual_controller_limit_queries_sent": False, "plan_only": True}
                        if args.close_gripper:
                            grip_start = state.get('grip_m')
                            grip_rate = pose_cfg.get('ready_gripper_speed_m_s')
                            grip_duration = (abs(grip_start-cfg['ready_gripper_m'])/grip_rate
                                if type(grip_start) in (int,float) and math.isfinite(grip_start)
                                and type(grip_rate) in (int,float) and math.isfinite(grip_rate) and grip_rate > 0 else None)
                            outcomes[role]["ready_gripper_plan"] = {
                                "target_m": cfg["ready_gripper_m"],
                                "tolerance_m": cfg["ready_gripper_tolerance_m"],
                                "stable_s": cfg["ready_gripper_stable_s"],
                                "reference_speed_m_s": grip_rate,
                                "estimated_reference_duration_s": grip_duration,
                                "post_reference_settle_budget_s": cfg["ready_gripper_timeout_s"],
                                "timeout_s": (grip_duration or 0)+cfg["ready_gripper_timeout_s"],
                                "physical_speed_verified": False,
                                "empty_gripper_required": True,
                                "coordination": "same writer, interleaved with joint trajectory",
                                "hardware_atomic_7d": False,
                                "gripper_health_verified": False}
                        continue
                    adapter, events = ensure_bounds(cfg, observer, session, leader_isolated=getattr(args, "leader_isolated", False))
                    stack.callback(adapter.close)
                    # ExitStack invokes this before closing writer/observer even
                    # if handoff, enable or the trajectory raises an exception.
                    stack.callback(capture_writer_receipt, role, adapter, events, session)
                    if command == "limits":
                        outcomes[role] = {"actual_command_bounds_deg": adapter.cfg["command_limits_deg"], "query_only": True}
                        continue
                    # Validate ALL requested arms before issuing any motion or
                    # enable. A blocked left target must not first move the right.
                    adapter.require_leader_isolation()
                    adapter.require_no_foreign_commands()
                    state = adapter.feedback(require_enabled=can_zero)
                    target = pose_target
                    plan_pose(state["q_deg"], target, adapter.cfg["command_limits_deg"],
                              max_speed_deg_s=pose_speed, max_accel_deg_s2=cfg["pose_accel_deg_s2"])
                    prepared_adapters[role] = (adapter, events)
                if can_zero and args.apply:
                    require_selection(cfg, {r:o.control_snapshot() for r,o in observers.items()}, expected)
                # Execution is deliberately sequential, not a bimanual
                # collision planner. Every arm replans from fresh feedback.
                for role, (adapter, events) in prepared_adapters.items():
                    if can_zero:
                        # Earlier arms can have our own recent TX; revalidate only the
                        # not-yet-executed target, without suppressing its foreign TX.
                        one_cfg = {**cfg, "can": {role: cfg["can"][role]}}
                        require_selection(one_cfg, {role: observers[role].control_snapshot()}, [role])
                    adapter.connect_for_apply()
                    if not all(adapter.feedback(require_enabled=False)["enabled"]):
                        if can_zero:
                            raise RuntimeError("Selected CAN/MOVEJ arm became disabled; no automatic enable")
                        adapter.handoff_can_movej()
                        adapter.enable()
                    print("实际执行请求：", role, args.target, f"；参考上限 {pose_speed}°/s。先确认静止和控制模式，随后才发送位置目标。", flush=True)
                    pose_options = dict(path_reviewed=args.path_reviewed, speed_deg_s=pose_speed,
                        accel_deg_s2=cfg["pose_accel_deg_s2"], transmit_hz=cfg["pose_send_hz"],
                        cancel=lambda: stop_event.is_set() or stop_requested(cfg),
                        endpoint_tolerance_deg=cfg["endpoint_tolerance_deg"], settle_timeout_s=cfg["endpoint_settle_timeout_s"])
                    if can_zero:
                        pose_options["require_existing_can"] = True
                    if args.close_gripper:
                        from .preparation import prepare_ready_pose
                        try:
                            outcomes[role] = prepare_ready_pose(pose_cfg, adapter, target_deg=pose_target, **pose_options)
                        except BaseException as exc:
                            if getattr(exc, "ready_pose_receipt", None) is not None:
                                session.report["ready_pose_and_gripper_result"] = exc.ready_pose_receipt
                            if getattr(exc, "preparation_receipt", None) is not None:
                                session.report["ready_gripper_result"] = exc.preparation_receipt
                            raise
                    else:
                        outcomes[role] = adapter.execute_pose(pose_target, **pose_options)
                    session.report["last_hardware_events"] = list(events)
                    session.report["packets_sent"] = adapter.packets_sent
            result = outcomes
        elif command == "deploy":
            if not args.apply:
                result = {"status": "PLAN_ONLY_NO_INFERENCE_NO_MOTION", "stages": [
                    {"ready_deg": cfg["ready_deg"], "speed_deg_s": cfg["pose_speed_deg_s"],
                     "ready_gripper_m": cfg.get("ready_gripper_m"),
                     "ready_gripper_tolerance_m": cfg.get("ready_gripper_tolerance_m"),
                     "empty_gripper_required": "ready_gripper_m" in cfg,
                     "order": "six joints, then gripper, then new real observation"},
                    {"duration_s": args.duration, "action_hz": cfg["action_time_hz"], "CAN_send_hz": cfg["policy_send_hz"], "async_RTC": True,
                     "speed_deg_s": cfg["policy_speed_deg_s"], "accel_deg_s2": cfg["policy_accel_deg_s2"], "horizon": cfg["horizon"]},
                    {"joint_zero_deg": cfg["joint_zero_deg"], "normal_return_speed_deg_s": cfg["pose_speed_deg_s"]}],
                    "zero_pose_is_not_calibration": True, "physical_validation": "NOT_RUN"}
            else:
                _apply_flags(args)
                require_not_stopped(cfg)
                from .sensors import CanObserver
                from .hub import rpc
                from .deployment import run_three_stage, TaskEventPublisher
                from .policy import PolicyClient
                # Register this attempt before model/recording/mode handoff can
                # fail. This is bookkeeping, not a ready-pose completion claim.
                session._task_publisher = TaskEventPublisher(cfg, session.path.name)
                session.report["motion_phase"] = "preflight"
                session._task_publisher.publish("deployment_started", scope="model/sensors/recording/controller checks")
                from .gripper_calibration import coordinate_review
                if coordinate_review(cfg).get('pending') is not False:
                    raise RuntimeError('Gripper zero coordinate/metrology review is pending; old policy cannot resume')
                # Model identity and real sensors precede any writer acquisition.
                client = PolicyClient(url=f"http://127.0.0.1:{cfg['model_port']}", execution_hz=cfg["action_time_hz"])
                try:
                    session.report["model_identity"] = client.health()
                finally:
                    client.close()
                rpc(cfg, "/observation")
                with Ownership("writer-can_right"):
                    # A zero transaction may have completed during model/RPC
                    # preflight. Recheck after acquiring the same writer lock.
                    if coordinate_review(cfg).get('pending') is not False:
                        raise RuntimeError('Gripper coordinate review changed during preflight; deployment blocked')
                    observer = CanObserver(**cfg["can"]["right"]).start()
                    adapter = None
                    own_recording = False
                    owned_recording_path = None
                    try:
                        time.sleep(.3)
                        adapter, events = ensure_bounds(cfg, observer, session, leader_isolated=True)
                        before = rpc(cfg, "/status")["recording"]
                        if before["active"]:
                            raise RuntimeError("Existing recording active; finish it explicitly rather than merge deployment episodes")
                        from .recording_lifecycle import OWNED_RECORDING_STOP_PROTOCOL
                        if before.get('owned_stop_protocol') != OWNED_RECORDING_STOP_PROTOCOL:
                            raise RuntimeError('Hub owned-stop protocol not verified; update/restart receive service before deployment')
                        record = rpc(cfg, "/record/start", {"task": "right-arm pi0.5 deployment review"})
                        own_recording = True
                        owned_recording_path = record["path"]
                        session.report['owned_recording_path'] = owned_recording_path
                        session.event("DEPLOYMENT_RECORDING_STARTED", recording=record)
                        adapter.connect_for_apply()
                        adapter.handoff_can_movej()
                        if not all(adapter.feedback(require_enabled=False)["enabled"]):
                            adapter.enable()
                        result = run_three_stage(cfg, adapter, session, stop_event, args.duration, args.path_reviewed)
                    finally:
                        original_error = sys.exc_info()[1]
                        cleanup_errors = []
                        if adapter:
                            session.report.update(packets_sent=adapter.packets_sent, last_hardware_events=list(events))
                            try:
                                adapter.close()
                            except Exception as cleanup_error:
                                cleanup_errors.append("host CAN socket close: " + repr(cleanup_error))
                        try:
                            observer.stop()
                        except Exception as cleanup_error:
                            cleanup_errors.append("observer close: " + repr(cleanup_error))
                        # Host CAN writer/observer have closed before USB waiting.
                        # Only our captured episode path may be finalized; the
                        # helper retains the original motion/inference exception.
                        _finish_deployment_cleanup(cfg, rpc, session,
                            owned_recording_path if own_recording else None, cleanup_errors, original_error)
        else:
            raise RuntimeError("Unsupported command")
        h50_exit = _h50_exit_code(command,result) if command in H50_COMMANDS else 0
        session.finish("H50_RESULT_FAILED_VALIDATION" if h50_exit else "COMMAND_COMPLETED_SCOPE_SPECIFIC", result=result)
        report_path = str(session.path / "operation.json")
        if command in H50_COMMANDS:
            summary_error = None
            if h50_exit:
                audited = result.get('acceptance',result.get('artifacts',{})).get('compact',{})
                reasons = audited.get('errors',[])
                summary_error = 'H50 validation failed: '+str(result.get('status','FAILED'))
                if isinstance(reasons,list) and reasons:summary_error += '; '+str(reasons[0])[:240]
            print(_compact_h50_json(command,report_path,result,summary_error),flush=True)
            return h50_exit
        concise = command in ("status", "diagnose", "teleop-status") or (command == "move" and not args.apply)
        if concise and not getattr(args, "json", False):
            print(render_console_summary(command, result, report_path), flush=True)
        else:
            response = {"report": report_path, "result": result}
            if command == "shadow-fp32":
                from .fp32_shadow_run import compact_receipt
                response['result'] = compact_receipt(result)
            elif command == "mode":
                from .mode_state import compact_mode_receipt
                compact = compact_mode_receipt(result)
                response.update(result=compact, mode_receipt=compact)
            elif command == "gripper":
                from .gripper_calibration import compact_receipt
                response['result'] = compact_receipt(result)
            print(json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False), flush=True)
        return 0
    except BaseException as exc:
        # Preserve error; no broad catch that changes failure into PASS, no reset
        # or movement in cleanup. A no-longer-mounted USB must not fall back.
        publisher = getattr(session, "_task_publisher", None)
        if publisher is not None and not publisher.closed:
            publisher.publish("return_skipped", reason="Deployment failed before normal return; no automatic recovery")
            publisher.publish("fault", area=session.report.get("motion_phase", "preflight"), reason=repr(exc))
            session.report["task_event_delivery"] = publisher.close()
        failure_receipt_saved = False
        try:
            session.finish("FAILED_OR_BLOCKED_NO_AUTOMATIC_RECOVERY", failure=repr(exc))
            failure_receipt_saved = True
        except Exception as save_error:
            print("Report save failed:", repr(save_error), file=sys.stderr)
        if args.command in H50_COMMANDS:
            partial = {'status':'FAILED','step':getattr(args,'step',None)}
            allocation = session.report.get('h50_trial_allocation',{})
            partial.update({key:allocation.get(key) for key in ('trial_index','trial_seed')})
            print(_compact_h50_json(args.command,session.path/'operation.json',partial,repr(exc),
                                   full_receipt_saved=failure_receipt_saved),flush=True)
            return 1
        if args.command in ("status", "diagnose", "teleop-status", "move") and not getattr(args, "json", False):
            print("任务失败或被阻止：" + repr(exc), flush=True)
            for role, receipt in session.report.get("hardware_receipts", {}).items():
                packets = receipt.get("packets_sent", {})
                feedback = receipt.get("final_observed_feedback", {})
                targets = sum(packets.get(key, 0) for key in ("0x155", "0x156", "0x157"))
                print(f"{role}：本写入者模式帧={packets.get('0x151', 0)}；关节位置帧={targets}；最后真实状态={feedback.get('status_hex', '未知')}。", flush=True)
                handoff = receipt.get("last_mode_handoff")
                if handoff:
                    print("模式交接证据已保存：请求与反馈时间、实际帧数和状态变化见报告 hardware_receipts。", flush=True)
            print("未自动复位、失能或回位。完整原始报告：" + str(session.path / "operation.json"), flush=True)
        else:
            mode_fields = {}
            if "mode_receipt" in session.report:
                from .mode_state import compact_mode_receipt
                mode_fields["mode_receipt"] = compact_mode_receipt(session.report["mode_receipt"])
            print(json.dumps({"error": repr(exc), "report": str(session.path / "operation.json"),
                              "automatic_reset_disable_return": False,
                              **mode_fields}, ensure_ascii=False), flush=True)
        return 1
