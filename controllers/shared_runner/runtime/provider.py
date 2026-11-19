"""Real existing-producer NO_SEND provider; nothing starts on import.

Source/lifecycle semantics are inherited from the reviewed canonical deployment
and the previous isolated NO_SEND provider. The supplied real cfg is never rebased.
"""
from copy import deepcopy
from contextlib import contextmanager,ExitStack
import os,time
from pathlib import Path
from runtime.legacy_support import require,loaded_runtime_factory,stable_anchor,current_loaded_model,OWNER_NAMES
from runtime.frozen_legacy.live_bridge import LiveContextBridge
from runtime.execution import make_queue,Sender

@contextmanager
def locked_existing_session(storage,cfg,*,expected_step=None,discover=current_loaded_model):
    """Only use the real selected config produced by the original controller.

    PC base configs can have usb_selection=None. They are not runtime configs;
    this entry does not choose a device, rebase paths or write configuration.
    """
    with ExitStack() as locks:
        for name in OWNER_NAMES:locks.enter_context(storage.Ownership(name))
        storage.verify_usb(cfg)
        require(isinstance(cfg.get('usb_selection'),dict) and cfg['usb_selection'].get('major_minor'),
                'actual selected USB configuration missing; use the original controller selection first')
        step,status=discover(cfg)
        require(expected_step is None or expected_step==step,'requested model differs; no checkpoint switch allowed')
        yield deepcopy(cfg),step,status

def resource_class(canonical_module,approved_sha,*,test_dependencies=None,runtime_factory=None,health_descriptor=None,bridge_type=None,apply=False):
    deps={} if test_dependencies is None else dict(test_dependencies)
    class NoSendResources(canonical_module.Resources):
        def __init__(self,cfg,session,stop_event,step,trial_index,options,binding):
            super().__init__(cfg,session,stop_event,step,not apply,trial_index,False,task=options.get('task'))
            if apply:
                require(test_dependencies is None,'real APPLY cannot inject test providers')
                require(binding.get('apply_gate_verified') is True and binding.get('mode')=='APPLY','independent live gate required')
                require(all(options.get(k) is True for k in ('leader_isolated','onsite_confirmed','current_start_confirmed')),'actual current-start authorizations required')
            self.options,self.binding=options,binding;self.profile_id=options['profile_id']
            self.core=canonical_module._reference_core
            self.receipt.update(schema='isolated-smoothed-live-nosend-v1',experimental_profile=self.profile_id,
                profile_binding=binding,no_writer_constructed=True,old_controller_acceptance_not_reused=True,
                manager_acceptance_not_modified=True,planner_transport='LOCAL_CPU_NO_PLANNER_SERVICE',
                final_command_entry='SmoothingQueue.sample_command',
                no_send_scope='Real current observations plus moving diagnostic future; no physical targets sent; not physical closed-loop tracking.')
            if apply:self.receipt.update(schema='isolated-bf16-smoothed-apply-v1',no_writer_constructed=False,
                no_send_scope=None,operator_assertions={k:options[k] for k in ('leader_isolated','onsite_confirmed','current_start_confirmed')})
        def _open_writer(self):
            if not apply:raise RuntimeError('NO_SEND ONLY: SDK writer/query construction forbidden')
            return canonical_module.Resources._open_writer(self)
        def wait(self,seconds,*,guarded=True):
            if 'wait' in deps:return deps['wait'](self,seconds,guarded=guarded)
            return super().wait(seconds,guarded=guarded)
        def start(self):
            cfg,h=self.cfg,self.h
            require(isinstance(cfg.get('usb_selection'),dict) and cfg['usb_selection'].get('major_minor'),
                    'actual frozen USB selection missing; use original control selection, do not fabricate or rebase config')
            require(Path(h['verify_usb'](cfg))==Path(cfg['usb']),'selected USB identity differs')
            require(Path(self.session.path).is_relative_to(Path(cfg['usb'])),'real session must be on current USB')
            h['require_not_stopped'](cfg);self.guard()
            self.parent=h['parent_life']();self.receipt['operator_parent_guard']=self.parent.receipt()
            self.publisher=h['publisher'](cfg,self.session.path.name);self.session._task_publisher=self.publisher
            self.publisher.publish('deployment_started',scope='ISOLATED_BF16_SMOOTHING_APPLY' if apply else 'ISOLATED_SMOOTHING_NO_SEND_ONLY')
            self.runtime=(runtime_factory or deps.get('loaded_runtime_factory',loaded_runtime_factory))(cfg,self.step)
            health=self.runtime.bind_loaded()
            self.receipt.update(model=(health_descriptor or canonical_module._health)(health,self.step),model_health=health,
                                model_fingerprint=self.runtime._fingerprint)
            self.receipt['lease']=self.runtime.acquire(self.session.path.name,trial_index=self.trial_index)
            # _wait_hub_ready only probes the registered existing producer and
            # verifies advancing real frame counters; it cannot launch a Hub.
            ready=h['ready_hub'](cfg,Path(cfg['remote_bundle']),self.rpc,
                                 stop_event=self.stop_event,session=self.session)
            status=ready['sensors'];current=status['recording']
            require(current.get('active') is False and current.get('saving') is False and
                    current.get('owned_stop_protocol')==h['stop_protocol'],'another recording owner is active/unknown')
            self.receipt['hub_readiness_existing_producer_only']=True
            self.counter_before=h['tx_counters'](cfg);self.receipt['interface_tx_before']=deepcopy(self.counter_before)
            self.receipt['hub_application_send_before']=h['hub_no_send'](status)
            cache_factory=deps.get('cache_factory',canonical_module.HubFeedbackCache)
            self.cache=cache_factory(cfg,self.rpc,status['producer'],status['storage_binding'],
                h['verify_usb'],h['stop_requested'],parent_check=self.parent.check).start()
            record_task=self.task+' [isolated-bf16-'+('apply:' if apply else 'nosend:')+self.session.path.name+']'
            try:
                response=self.rpc(cfg,'/record/start',{'task':record_task},timeout=20)
                self.owned_path=h['episode_path'](cfg,response.get('path'),current.get('path'))
            except BaseException:
                try:self.owned_path=h['recover_start'](cfg,self.rpc,current.get('path'),record_task,h)
                except Exception as exc:self.receipt['recording_start_outcome_unknown']=repr(exc)
                raise
            self.receipt['owned_recording_path']=self.owned_path;self.cache.bind_recording(self.owned_path)
            self.directory=Path(self.owned_path)/'evidence';fd=h['open_dir'](self.directory,cfg,create=True);os.close(fd)
            self.events=h['event_writer'](cfg,self.directory/'h50-async-events-and-host-tx.jsonl',capacity=4096)
            self.receipt['events_start']=self.events.start();self.journal=h['journal'](cfg,self.owned_path)
            self.receipt['storage_state']='BOUNDED_RAM_ASYNC_USB_RECORDING'
            self.receipt['record_before_started_ns']=time.monotonic_ns();self.wait(2.)
            self.receipt['record_before_completed_ns']=time.monotonic_ns()
            self.bounds=[list(p) for p in h['documented']]+[list(cfg['gripper_bounds_m'])]
            self.receipt['bounds_source']='SDK_DOCUMENTED_ONLY_NO_CONTROLLER_QUERY_NO_SEND'
            if apply:
                self._open_writer()
                self.receipt['bounds_source']='FRESH_CONTROLLER_AND_SDK_INTERSECTION'
            self.anchor,stability=deps.get('stable_anchor',stable_anchor)(self)
            require(all(lo<=value<=hi for value,(lo,hi) in zip(self.anchor,self.bounds)),
                    'real initial anchor outside command bounds; no feedback clipping')
            self.receipt.update(initial_actual_q_deg_m=list(self.anchor),startup_stability=stability,
                output_command_policy=canonical_module.OUTPUT_COMMAND_POLICY,
                output_command_bounds_deg_m=deepcopy(self.bounds))
            origin=deps.get('clock_ns',time.monotonic_ns)()
            self.queue=make_queue(self.anchor,self.bounds,origin,self.trial_index)
            self.reference_queue=self.queue
            self.receipt['initial_planning_reference']={'schema':'fixed-clock-linear-hold-v1',
                'start_ns':self.queue.plan.start_ns,'end_ns':self.queue.plan.end_ns,
                'q_rad_m':self.queue.plan.sample(origin).tolist(),
                'reference_initial_v_a':'software HOLD zeros, not measured motor velocity or acceleration'}
            self.receipt['initial_physical_reference']={'schema':'unsent-physical-hold-v1',
                'q_deg_m':list(self.anchor),'v_deg_m_s':[0.]*7,'physical_dispatch_performed':False}
            self.context_bridge=(bridge_type or LiveContextBridge)('APPLY' if apply else 'NO_SEND',self.session.path.name,self.setting,approved_sha,self.guard)
            if apply:
                from runtime.motion_sender import MotionSender
                require(self.adapter is not None and self.query is None,'single owned writer required')
                self.sender=MotionSender(self).start()
            else:
                require(self.adapter is None and self.query is None,'NO_SEND must never create a writer/query')
                self.sender=deps.get('sender_factory',Sender)(self).start()
            self.publisher.publish('policy_started',rtc_enabled=True,no_send=not apply,scope=self.profile_id)
        def close(self,primary):
            """One attempt per closer; errors survive later cleanup and lease close.

            Same owned-resource order as canonical Resources.close, with each
            stage isolated so an exception cannot skip remaining cleanup. In
            particular a failed ModelRuntime.close is never retried and replaced
            by its subsequent 'no lease acquired' response.
            """
            h,cfg=self.h,self.cfg;errors=self.receipt['cleanup_errors']
            def attempt(label,fn):
                try:return fn()
                except BaseException as exc:errors.append(label+': '+repr(exc));return None
            if self.predictions is not None:
                closed=attempt('prediction workers',self.predictions.close)
                self.receipt['prediction_workers']=closed or {'fetch_stopped':False,'inference_stopped':False}
                if not all(self.receipt['prediction_workers'].get(k) is True for k in ('fetch_stopped','inference_stopped')):
                    errors.append('prediction workers exceeded normal drain budget; ownership retained until actual exit')
                    self.receipt['status']='FAILED_NO_AUTOMATIC_RECOVERY'
                    self.receipt['inflight_drain']={'normal_budget_exceeded':True,'owner_locks_retained':True,
                        'journal_events_recording_lease_retained':True,'drained':False}
                    # Existing HTTP stages have their own bounded timeouts, but
                    # can cumulatively exceed one join budget. Never close an
                    # object still owned by the inference worker or return from
                    # the outer ownership context while it can write a result.
                    while not all(self.receipt['prediction_workers'].get(k) is True for k in ('fetch_stopped','inference_stopped')):
                        time.sleep(.05)
                        try:closed=self.predictions.close()
                        except BaseException as exc:
                            self.receipt['inflight_drain']['last_close_error']=repr(exc)
                            continue
                        self.receipt['prediction_workers']=closed
                    self.receipt['inflight_drain']['drained']=True
            if self.sender is not None:
                self.receipt['sender_stopped']=attempt('sender close',self.sender.close)
                self.receipt.update(sender_ticks=self.sender.ticks,sender_failure=self.sender.failure)
                if self.receipt['sender_stopped'] is not True or self.sender.failure:errors.append('NO_SEND observer did not finish healthy')
            if not apply and (self.adapter is not None or self.query is not None):errors.append('unexpected hardware writer/query in NO_SEND resource')
            if apply:
                for name,adapter in (('query',self.query),('writer',self.adapter)):
                    if adapter is not None:attempt(name+' host socket close',adapter.close)
                if self.adapter is not None:self.receipt['packets_sent']=self.adapter.packets_sent
            if self.owned_path is not None:
                self.receipt['post_record_started_ns']=time.monotonic_ns();attempt('post recording',lambda:self.wait(2.,guarded=False))
                self.receipt['post_record_completed_ns']=time.monotonic_ns()
            if self.cache is not None:
                self.cache.owned_path=None;self.receipt['cache_final']=attempt('cache close',self.cache.close)
                if not self.receipt['cache_final'] or self.receipt['cache_final'].get('stopped') is not True:errors.append('Hub cache worker not closed')
                if self.cache.failure:errors.append('Hub cache failure: '+str(self.cache.failure))
            if self.events is not None:
                self.receipt['events_final']=attempt('events close',lambda:self.events.close(timeout_s=5))
                if not self.receipt['events_final'] or self.receipt['events_final'].get('timing_evidence_incomplete') is not False:
                    errors.append('USB asynchronous evidence incomplete')
            if self.owned_path is not None:
                final=attempt('owned recording finalization',lambda:h['stop_owned'](cfg,self.rpc,self.owned_path,timeout=300))
                self.receipt['recording_finalization']=final or {}
                if not final or final.get('completed') is not True or final.get('healthy') is not True:errors.append('owned native recording incomplete')
            if self.journal is not None:
                safe=self.predictions is None or self.receipt['prediction_workers'].get('inference_stopped') is True
                self.receipt['storage_state']='USB_FINAL_FLUSH_PENDING'
                if safe:
                    final=attempt('journal final flush',self.journal.close);self.receipt['journal_final']=final
                    self.receipt['journal_closed']=final is not None
                    good=isinstance(final,dict) and final.get('complete') is True and final.get('status')=='USB_FLUSH_COMPLETE' and (
                        type(final.get('accepted')) is int and final['accepted']==final.get('written') and
                        final.get('pending_records')==0 and final.get('pending_bytes')==0)
                    if good:self.receipt['storage_state']='USB_JOURNAL_FLUSH_COMPLETE_REQUIRES_ARTIFACT_AUDIT'
                    else:errors.append('model/reference journal did not completely flush')
                else:self.receipt['journal_closed']=False;errors.append('unfinished inference still owns journal')
            if self.counter_before is not None:
                after=attempt('kernel TX counters',lambda:h['tx_counters'](cfg))
                if after is not None:
                    self.receipt['interface_tx_after']=after
                    delta={role:after[role]['tx_packets']-before['tx_packets'] for role,before in self.counter_before.items()}
                    self.receipt['interface_tx_delta']=delta
                    if not apply and any(value!=0 for value in delta.values()):errors.append('interface TX changed during NO_SEND; attribution unknown')
                    if apply and delta.get('left',0)!=0:errors.append('unexpected left-arm interface TX during right-only experiment')
            if self.runtime is not None:
                release=attempt('model lease release',self.runtime.close)
                if release is not None:self.receipt['lease_release']=release
                else:self.receipt['lease_release_unconfirmed']=True
            if self.publisher is not None:
                self.receipt['task_event_delivery']=attempt('publisher status',self.publisher.status)
                if self.receipt['task_event_delivery'] and self.receipt['task_event_delivery'].get('errors'):errors.append('task metadata delivery incomplete')
            self.receipt['last_hardware_events']=list(self.hardware_events)
            if primary is not None or errors:self.receipt['status']='FAILED_NO_AUTOMATIC_RECOVERY'
            if self.owned_path is not None:
                attempt('final USB receipt',lambda:h['write_json'](Path(self.owned_path)/'evidence/h50-async-run.json',self.receipt,cfg))
            if errors and primary is None:raise RuntimeError('NO_SEND cleanup/evidence failure: '+repr(errors))
    return NoSendResources
