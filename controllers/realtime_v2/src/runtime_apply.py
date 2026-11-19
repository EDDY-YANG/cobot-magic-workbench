"""Explicitly authorized physical V2 session. Default CLI never calls this."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import json,os,select,signal,sys,threading,time,uuid
import numpy as np
from .live_io import ExistingHub,PinnedLoadedModel,owned_session,public_receipt,require
from .feedback_cache import FeedbackCache
from .runtime_guard import check_stop,can_counters,verify_model,source_files,verify_files,sha
from .runtime import wait_owned_recording_ready,emit_unbuffered
from .lifecycle import drain_before_unlock
from .v2_physical_core import V2PhysicalCore
from .v2_live_writer import V2LiveWriter
from .physical_window import run_physical_window
from .window_contract import validate_window_seconds
from .manual_speed_control import ManualSpeedControl, SpeedEventOutbox, stdin_control_loop
from .speed_teaching import build_labels, saved_receipt, FILENAME as TEACHING_FILE


def stable_start(writer,guard,stop_event):
    start=time.monotonic();samples=[];last_revision=None
    while time.monotonic()-start<=3.:
        guard();require(not stop_event.is_set(),'Startup cancelled')
        feedback=writer.cache.read_latest();source=feedback['source_ns']/1e9
        revision=tuple(feedback['source_ns_by_component'][k] for k in ('q12','q34','q56','gripper'))
        values=[*np.rad2deg(feedback['q_rad_m'][:6]).tolist(),feedback['q_rad_m'][6]]
        if revision != last_revision:
            samples.append((source,values));last_revision=revision
        samples=[r for r in samples if source-r[0]<=.35]
        if len(samples)>1 and source-samples[0][0]>=.3:
            data=np.asarray([r[1] for r in samples])
            if np.all(np.ptp(data,axis=0)<=np.asarray([.5]*6+[.002])):
                return {'window_s':source-samples[0][0],'samples':len(samples),
                        'q_deg_m':values,'initial_velocity':'HOLD assumption, not measured motor velocity'}
        stop_event.wait(.02)
    raise RuntimeError('Actual startup position did not remain stable')


def execute_physical(binding,cfg,storage,args):
    require(args.authorize_motion and args.leader_isolated and args.onsite_confirmed and args.current_start_confirmed,
            'Current explicit physical execution authorization required')
    duration = validate_window_seconds(args.duration)
    root=Path(binding['runtime_root']);run_id=datetime.now().strftime('%Y-%m-%d-%H-%M-%S')+'-v2-apply-'+uuid.uuid4().hex[:8]
    run=root/'runs'/run_id;stop=threading.Event();monitor_quit=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,lambda *_:stop.set())
    speed_events=SpeedEventOutbox(emit_unbuffered,stop)
    speed_control=ManualSpeedControl(run_id,emit=speed_events.publish,stop_event=stop)
    if args.stop_on_stdin_eof:
        threading.Thread(target=stdin_control_loop,args=(sys.stdin.fileno(),speed_control,stop),
                         name='v2-speed-requests-and-eof',daemon=True).start()
    try:
        return _execute_owned(binding,cfg,storage,args,root,run_id,run,stop,monitor_quit,
                              duration,speed_control,speed_events)
    finally:
        # Also covers preparation failures before the journal/inner cleanup
        # exists. Stopping diagnostics cannot retain hardware ownership here.
        stop.set()
        try:speed_control.close('Physical session owner exited')
        except BaseException as exc:speed_events.fail('Speed close failed: '+repr(exc))
        speed_events.shutdown(.25)


def _execute_owned(binding,cfg,storage,args,root,run_id,run,stop,monitor_quit,
                   duration,speed_control,speed_events):
    from .long_window_evidence import SessionEvidence
    from agilex_control.h50_journal import H50USBJournal
    from agilex_control.time_contract import capture_clock_domain
    with owned_session(cfg,storage=storage):
        storage.require_not_stopped(cfg);check_stop(cfg,root/'STOP-APPLY')
        model=PinnedLoadedModel(binding['model']['precision'],binding['model']['step'])
        model_receipt=model.bind();verify_model(binding,model_receipt)
        hub=ExistingHub(cfg,full_status=True);state=hub.status()
        require(not state['recording']['active'] and not state['recording']['saving'],'Hub is occupied')
        before=can_counters();run.mkdir(parents=True,exist_ok=False)
        journal=H50USBJournal(cfg,run);episode=None;cache=None;writer=None;core=None;evidence=None
        result={'schema':'tiger-independent-v2-physical-window-v2','status':'STARTED','model':model_receipt,
            'source_binding':binding,'human_flags':{'leader_isolated':True,'onsite_confirmed':True,'current_start_confirmed':True},
            'window_s':duration,'requested_window_s':duration,'automatic_repeat':False,'runtime_root':str(root),'run_dir':str(run)}
        def save(name,data):journal.write_json(run/name,public_receipt(data))
        save('operation.json',result)
        errors=[];monitor_errors=[];last_usb=[time.monotonic()]
        def guard():
            require(not stop.is_set() and not monitor_errors,'STOP/background fault')
            # The monitor owns filesystem checks. Never stat/read USB from the
            # control/sender thread; a stalled monitor still fails closed here.
            require(time.monotonic()-last_usb[0]<.75,'USB monitor stalled')
            if cache is not None:cache.check_alive()
            if evidence is not None:evidence.check_alive()
        def monitor():
            while not monitor_quit.wait(.02):
                try:
                    check_stop(cfg,root/'STOP-APPLY')
                    if time.monotonic()-last_usb[0]>=.25:
                        storage.verify_usb(cfg);journal.verify();last_usb[0]=time.monotonic()
                except BaseException as exc:
                    monitor_errors.append(type(exc).__name__+': '+str(exc));stop.set();model.cancel();return
        monitor_thread=threading.Thread(target=monitor,name='v2-physical-usb-stop',daemon=True);monitor_thread.start()
        try:
            evidence=SessionEvidence(cfg,run)
            evidence.start()
            hub.begin_owned_recording('V2 APPLY '+str(duration)+'s '+args.prompt)
            result['recording_ready']=wait_owned_recording_ready(hub,stop,external_guard=guard)
            episode=H50USBJournal(cfg,hub.owned_path)
            cache=FeedbackCache(hub).start();cache.wait_ready(timeout_s=3.,stop_event=stop)
            writer=V2LiveWriter(cfg,cache,evidence.tx_sink,guard,canonical_root=binding['canonical_root'],
                canonical_sources=binding['canonical_sources'],confirmations=result['human_flags'])
            result['writer_open']=writer.open()
            require(np.array_equal(np.asarray(writer.bounds_rad_m),np.asarray(binding['bounds_rad_m'])),
                    'Fresh actual limits differ from precompiled controller; no motion')
            result['startup_stability']=stable_start(writer,guard,stop)
            model.acquire(run_id,args.trial_index)
            captured=hub.capture(args.prompt,episode);domain=capture_clock_domain()
            core_init_started_ns=time.monotonic_ns()
            core=V2PhysicalCore(captured.payload['state'],writer.bounds_rad_m,captured.anchor_ns,
                clock_domain=(domain['host_id'],domain['boot_id'],domain['clock_name']),mode='native_first',
                trial_index=args.trial_index,solver_dir=binding['solver_dir'],gripper_lookahead_ms=0.,
                timeqp_instance_id=binding['timeqp']['instance_id'],manual_speed=speed_control)
            core_init_finished_ns=time.monotonic_ns()
            result['controller_initialization']=core.initialization_receipt
            result['startup_observation']={
                'controller_anchor_observation_ns':captured.anchor_ns,
                'controller_init_started_ns':core_init_started_ns,
                'controller_init_finished_ns':core_init_finished_ns,
                'controller_init_ms':(core_init_finished_ns-core_init_started_ns)/1e6,
                'anchor_age_at_init_end_ms':(core_init_finished_ns-captured.anchor_ns)/1e6,
                'first_policy_input':'FRESH_BACKGROUND_CAPTURE_AFTER_CONTROLLER_INITIALIZATION',
                'controller_state_reset':False,'observation_timestamp_rewritten':False}
            speed_control.start()
            # captured initialized the controller from real measured state.
            # Solver construction can consume its freshness budget. Keep that
            # state/history, but acquire the first VLA input in the existing
            # background worker after construction, just like later inputs.
            result['execution']=run_physical_window(core,model,hub,episode,writer,cache,prompt=args.prompt,
                duration_s=duration,stop_event=stop,fast_guard=guard,initial_capture=None,save=save,evidence=evidence)
            result['status']='PHYSICAL_WINDOW_COMPLETED_AND_POSITION_SETTLED'
        except BaseException as exc:
            errors.append(type(exc).__name__+': '+str(exc));result['status']='FAILED_RETAINED'
            if writer is not None:result['fault_stop']=writer.fault_stop(str(exc))
            if core is not None:core.fault(str(exc))
        finally:
            try:speed_control.close('Physical policy window ended or faulted')
            except BaseException as exc:
                stop.set();errors.append('manual speed close: '+repr(exc))
            result['model_drain']=drain_before_unlock(model,emit=speed_events.publish)
            if writer is not None:
                try:result['writer_close']=writer.close()
                except BaseException as exc:errors.append('writer close: '+repr(exc))
            if cache is not None:
                while True:
                    try:result['feedback_cache']=cache.close(timeout_s=3.);break
                    except RuntimeError:speed_events.publish({'event':'WAIT_FEEDBACK_DRAIN_OWNERSHIP_HELD'})
            monitor_quit.set();monitor_thread.join(3.)
            try:
                if hub.owned_path is not None:result['recording']=hub.end_owned_recording()
                result['interface_before']=before;result['interface_after']=can_counters()
                require(result['interface_after']['can_right']['ifindex']==before['can_right']['ifindex'],
                        'Right CAN interface was replaced')
                require(result['interface_after']['can_left']==before['can_left'],'Unexpected left-arm TX/interface change')
                require(result.get('feedback_cache',{}).get('healthy',True),'Physical feedback cache failed')
                require(source_files(root)==binding['runtime_sources'],'Private source changed during motion')
                verify_files(binding['canonical_root'],binding['canonical_sources'])
                require(sha(binding['config_path'])==binding['config_sha256'],'Original configuration changed')
                result['protected_sources_unchanged']=True
            except BaseException as exc:errors.append('finalization: '+repr(exc))
            if evidence is not None:
                try:
                    result['evidence']=evidence.close()
                    require(result['evidence'].get('complete') is True,'Long-window evidence incomplete')
                except BaseException as exc:errors.append('evidence close: '+repr(exc))
            else:
                errors.append('Evidence session did not start')
            if 'execution' in result:
                result['execution']['evidence_finalization_pending']=False
                result['execution']['evidence_complete']=result.get('evidence',{}).get('complete',False)
                if errors or monitor_errors:
                    result['execution']['status']='PHYSICAL_FAILED_RETAINED'
                    result['execution']['error']=('; '.join(errors+monitor_errors))
            result['monitor_errors']=monitor_errors;result['errors']=errors
            if errors or monitor_errors:result['status']='FAILED_RETAINED'
            # The final result is queued only after model, writer, feedback,
            # recording and evidence cleanup. A stuck sink has a finite wait;
            # failure is saved rather than throwing out of device cleanup.
            speed_events.publish({'status':result['status'],'run_dir':str(run),'errors':list(errors)})
            output_receipt=speed_events.shutdown(.25)
            result['manual_speed_output']=output_receipt
            if not output_receipt['complete']:
                errors.append('manual speed output incomplete: '+'; '.join(output_receipt['failure_reasons']))
                result['status']='FAILED_RETAINED'
                if 'execution' in result:
                    result['execution']['status']='PHYSICAL_FAILED_RETAINED'
                    result['execution']['error']='; '.join(errors+monitor_errors)
            try:
                result['manual_speed']=speed_control.receipt()
                result['manual_speed']['event_output']=output_receipt
                save('manual-speed-summary.json',result['manual_speed'])
                # Once, after control/model/recording drain; never in the50Hz loop.
                try:
                    labels=build_labels(result['manual_speed'],operation=result,prompt=args.prompt,
                        clock_domain=capture_clock_domain(),raw_episode=hub.owned_path)
                    save(TEACHING_FILE,labels)
                    result['speed_teaching']=saved_receipt(labels,run/TEACHING_FILE)
                except Exception as exc:
                    errors.append('manual speed label export: '+type(exc).__name__+': '+str(exc))
                    result['speed_teaching']={'saved':False,'error':errors[-1],'training_ready':False}
                    result['status']='FAILED_RETAINED'
                if 'execution' in result:save('physical-summary.json',result['execution'])
                save('operation.json',result)
            finally:
                try:
                    if episode is not None:episode.close()
                finally:journal.close()
        if result['status']!='PHYSICAL_WINDOW_COMPLETED_AND_POSITION_SETTLED':raise RuntimeError('Physical run failed; evidence retained')
        return result
