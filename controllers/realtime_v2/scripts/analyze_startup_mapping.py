"""CONTROL-MAPPING comparison only, using one saved REAL GUIDED failure H50.

The full-H50/native_first mapping branch DOES NOT claim a real native inference.
Its RTC response metadata is an explicitly synthesized CPU protocol fixture;
the50x7 numerical output and actual initial state remain exactly unchanged.
No remote/model/hardware actions. First-order predicted state is simulated.
"""
from pathlib import Path
import argparse
from copy import deepcopy
import hashlib
import json
import math
import sys
import time
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src'),str(ROOT/'vendor')]
from src.evidence_reader import iter_evidence_records

def require(ok,message):
    if not ok:raise ValueError(message)

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def read(path):return json.loads(Path(path).read_text(encoding='utf8'))

def math_modules():
    from pinned_components import verify_all,command_limiter
    manifest=verify_all()
    from bootstrap_alignment import NativeBootstrap,GuidedBootstrapQueue,BoundTemporalAdapter,validate_response,RTC
    from client_time_queue import SEND_NS,NS,label_ns
    from portable_backend import OFFICIAL,BUILD_ALLOWED,BUILD_RECEIPTS
    return locals()

def replay_mapping(pair, mode, view, ready_duration_ns, solver_dir, *, cpu_budget_ns,
             tau,weight,gripper_lookahead_ms,math_modules):
    m=math_modules;period=m['SEND_NS'];ns=m['NS'];start=1_000_000_000
    anchor=pair['anchor'];bounds=pair['bounds'];row=pair['rows'][mode]
    rtc=row['rtc'];raw,binding=m['validate_response'](rtc,row['response'],evidence_kind='SYNTHETIC_PROTOCOL_FIXTURE')
    result={'mode':mode,'timing_view':view,'real_raw_h50_sha256':hashlib.sha256(raw.tobytes()).hexdigest(),
        'original_observation_ns':pair['original_observation_ns'],
        'original_request_started_ns':row['request_started_ns'],'original_validated_ns':row['validated_ns'],
        'original_observation_to_validated_ms':(row['validated_ns']-pair['original_observation_ns'])/1e6,
        'original_deadline_met':row['deadline_met'],
        'measured_request_to_validated_ms':(row['validated_ns']-row['request_started_ns'])/1e6,
        'hypothetical_result_available_ms':ready_duration_ns/1e6,'fixed_cpu_preparation_budget_ms':cpu_budget_ns/1e6,
        'timing_is_counterfactual_fresh_observation':True,'original_timestamps_modified':False,
        'physical_tracking_validated':False,'real_robot_commands':0,'model_calls':0,
        'response_binding':binding,'simulation_model':'six independent identical first-order joints; no gripper physical model',
        'tau_s':tau,'w_ddy':weight,'initial_actual_observation_rad_m':anchor.tolist(),
        'controller_initializations':0,'plant_initializations':1,'midrun_state_resets':0,
        'terminal_preview_hold':'single-packet diagnostic only; not rolling queue admission'}
    commit=start+((ready_duration_ns+period-1)//period)*period
    result['counterfactual_commit_s']=(commit-start)/ns
    if commit-start>600_000_000:
        return {**result,'status':'ADMISSION_FAILED_600MS_BUDGET','metrics':None},None
    domain=('OFFLINE_REAL_PAIR','SIMULATED_CLOCK','MONOTONIC_NS')
    prep_start=time.perf_counter_ns()
    if mode=='native_first':
        bootstrap=m['NativeBootstrap'](anchor,bounds,start,trial_index=rtc['trial_index'],clock_domain=domain,sender_epoch_ns=start)
        require(bootstrap.ticket.wire()==rtc,'Reconstructed native RTC differs from actual request')
        candidate=bootstrap.prepare(row['response'],evidence_kind='SYNTHETIC_PROTOCOL_FIXTURE')
        queue=bootstrap.activate(candidate,commit,clock_domain=domain)
        plan=queue.plan;epoch=commit
    else:
        queue=m['GuidedBootstrapQueue'](anchor,bounds,start,trial_index=rtc['trial_index'],clock_domain=domain)
        context=queue.request(start,start,clock_domain=domain)
        require(context.wire()==rtc,'Reconstructed guided RTC differs from actual request')
        candidate=m['BoundTemporalAdapter'](bounds).prepare_response(context,row['response'],evidence_kind='SYNTHETIC_PROTOCOL_FIXTURE')
        queue.commit(context,candidate,commit);plan=queue.plan;epoch=start
    result['temporal_prepare_ms']=(time.perf_counter_ns()-prep_start)/1e6
    result['temporal']=candidate.diagnostics
    cut=0 if mode=='native_first' else context.cut_phase_ns
    result['seam_reference_velocity_deg_s']=np.rad2deg(plan.velocity(cut)[:6]).tolist()
    result['model_target_rows_used']=50 if mode=='native_first' else 37
    result['native_response_generated']=False
    result['response_metadata_is_explicit_cpu_fixture']=True
    # This is a controlled path-mapping comparison at the SAME recorded real
    # commit time. A new local CPU preparation timing must not be substituted
    # for that archived availability. It is measured separately; the real
    # pipeline budget remains600ms, not an invented20ms TimeQP deadline.
    result['fixed_cpu_preparation_budget_ms']=None
    result['temporal_preparation_timing_is_offline_diagnostic_only']=True
    official=m['OFFICIAL']
    class SimulatedPlantPlanner(official.AcadosPlanner):
        """This state history belongs solely to the explicitly simulated plant."""
        def __init__(self,cfg):
            self.reset_count=0
            super().__init__(cfg,export_dir=str(Path(solver_dir).resolve()),
                             json_file=str(Path(solver_dir).resolve()/'ocp.json'))
            self.reset(anchor[:6])
        def reset(self,q):
            require(self.reset_count==0,'Simulation cannot reset controller midrun')
            self.reset_count+=1
            super().reset(q)
        def _rate_limit_fallback(self,*args,**kwargs):
            raise RuntimeError('Actual acados failed; no synthetic fallback')
    cfg=official.MPCConfig(nq=6,dt=.02,N=15,tau=tau,q_min=bounds[:6,0],q_max=bounds[:6,1],w_track0=10.,w_ddy=weight)
    permission=m['BUILD_ALLOWED'].set(False)
    try:
        planner=SimulatedPlantPlanner(cfg)
    finally:
        m['BUILD_ALLOWED'].reset(permission)
    codec=m['command_limiter'](bounds)
    end=epoch+plan.end;state=anchor[:6].copy();decay=np.exp(-.02/tau)
    records=[];failure=None;first_motion=None;activation_verified=False
    for index in range(int((end-start)//period)+1):
        at=start+index*period
        try:
            if at<commit:
                future=np.tile(anchor,(15,1))
            else:
                if not activation_verified:
                    # Activation changes reference availability only. No reset
                    # or replacement of plant/controller/history occurs here.
                    require(planner.reset_count==1,'Unexpected activation state reset')
                    activation_verified=True
                future=plan.sample(np.minimum(at-epoch+np.arange(15,dtype=np.int64)*period,plan.end))
            previous=planner.x_aug[6:12].copy();previous2=planner.x_aug[12:].copy()
            began=time.perf_counter_ns()
            output=planner.solve(state,future[:,:6],contact_mode=False)
            solve_ms=(time.perf_counter_ns()-began)/1e6
            require(planner.solver.status==0 and planner._h_bound_stages==tuple(range(15)),'Failed/incomplete acados solve')
            command=output.y_cmd
            excess=np.r_[bounds[:6,0]-command,command-bounds[:6,1],
                abs(command-previous)-cfg.dy_max,abs(command-2*previous+previous2)-cfg.ddy_max,
                abs(command-state)-tau*cfg.v_max]
            require(np.isfinite(command).all() and excess.max()<=1e-6,'Solved command fails independent constraint check')
            gi=math.ceil(gripper_lookahead_ms/20.)
            encoded=codec.encode(np.r_[command,future[gi,6]])
            command=encoded.target_rad_m[:6]
            # Only SIMULATED applied command history, no real IO/NO_SEND core.
            planner.x_aug[6:12]=command
            predicted=planner.solver.get(1,'x')[:6].copy()
            next_state=decay*state+(1-decay)*command
            if first_motion is None and np.max(abs(command-anchor[:6]))>np.deg2rad(.001):
                first_motion=(at-start)/ns
            records.append({'t_s':(at-start)/ns,'simulated_joint_rad':state.copy(),
                'reference_joint_rad':future[0,:6].copy(),'command_joint_rad':command.copy(),
                'predicted_next_joint_rad':predicted,'simulated_next_joint_rad':next_state.copy(),
                'gripper_reference_m':future[0,6],'gripper_command_m':encoded.target_rad_m[6],
                'solve_wall_ms':solve_ms})
            state=next_state
        except Exception as exc:
            failure={'tick':index,'error':type(exc).__name__+': '+str(exc)}
            break
    result.update(status='SIMULATED_FIRST_PACKET_COMPLETE' if failure is None else 'SIMULATION_FAILED',
        failure=failure,controller_initializations=planner.reset_count,
        first_simulated_command_change_s=first_motion,reference_end_s=(end-start)/ns,
        gripper_configured_lookahead_ms=gripper_lookahead_ms,gripper_sampled_lookahead_ms=math.ceil(gripper_lookahead_ms/20.)*20)
    if not records:
        result['metrics']=None
        return result,None
    arrays={key:np.asarray([r[key] for r in records]) for key in records[0]}
    steps=np.rad2deg(np.diff(np.vstack((anchor[:6],arrays['command_joint_rad'])),axis=0))
    errors=np.rad2deg(arrays['simulated_joint_rad']-arrays['reference_joint_rad'])
    wall=arrays['solve_wall_ms']
    result['metrics']={'command_step_peak_deg_per20ms':float(np.max(abs(steps))),
        'command_step_peak_deg_by_joint':np.max(abs(steps),axis=0).tolist(),
        'simulated_tracking_rms_deg':float(np.sqrt(np.mean(errors**2))),
        'simulated_tracking_rms_deg_by_joint':np.sqrt(np.mean(errors**2,axis=0)).tolist(),
        'simulated_tracking_max_deg':float(np.max(abs(errors))),
        'simulated_tracking_reference_differs_between_modes':True,
        'reference_end_s':(end-start)/ns,'actual_acados_calls':len(records),
        'solve_wall_ms':{'p50':float(np.median(wall)),'p95':float(np.percentile(wall,95)),'max':float(wall.max())},
        'gripper_command_reference_max_mm':float(np.max(abs(arrays['gripper_command_m']-arrays['gripper_reference_m']))*1000),
        'gripper_physical_tracking_error':None}
    return result,arrays

def load_mapping_fixture(run_dir,m):
    run=Path(run_dir).resolve(strict=True)
    op=read(run/'operation.json')
    require(op.get('schema') in ('tiger-independent-v2-physical40-v1','tiger-independent-v2-physical-window-v2'),
            'Expected preserved actual physical run')
    packets=iter_evidence_records(run/'physical-policy-packets.json')
    original=next(packets,None)
    packets.close()
    require(original is not None,'Real saved H50 required')
    require(original['rtc']['enabled'] is True and original['rtc']['request_index']==0,
            'This diagnostic requires the actual guided first packet')
    raw=np.asarray(original['response']['actions'],float)
    require(raw.shape==(50,7) and np.isfinite(raw).all(),'Finite actual H50x7 required')
    snapshot=original['captured_snapshot']
    anchor=np.asarray(snapshot['state_right_rad_m'],float)
    bounds=np.asarray(op['source_binding']['bounds_rad_m'],float)
    require(anchor.shape==(7,) and bounds.shape==(7,2),'State and bounds missing')
    require(np.all(anchor>=bounds[:,0]) and np.all(anchor<=bounds[:,1]),
            'This comparison requires an in-bounds original state; no state rewrite')
    native=deepcopy(original)
    native['rtc']=m['RTC'].bootstrap(original['rtc']['trial_index'],0)
    native['response']={'actions':deepcopy(original['response']['actions']),
                        'rtc':m['RTC'].validate_rtc(native['rtc'])}
    return {'anchor':anchor,'bounds':bounds,
        'rows':{'native_first':native,'guided_hold35':original},
        'original_observation_ns':original['observation_anchor_ns'],
        'source_status':op['status'],'source_h50_origin':'REAL_GUIDED_FIRST_RESPONSE',
        'source_sha256':{str(run/name):sha(run/name) for name in ('operation.json','physical-policy-packets.json')},
        'actual_original_packet':original,
        'native_response_generated':False,
        'fixture_note':'Only response RTC metadata is synthesized for native mapping; numeric H50 and actual initial state unchanged'}

def rolling_protocol_probe(pair,m,commit_offset):
    """Adversarial CPU fixture, not a real response to a rolling condition."""
    start=1_000_000_000;domain=('CPU_MAPPING_PROBE','NO_HARDWARE','MONOTONIC_NS')
    native=m['NativeBootstrap'](pair['anchor'],pair['bounds'],start,
        trial_index=pair['rows']['native_first']['rtc']['trial_index'],clock_domain=domain,sender_epoch_ns=start)
    candidate=native.prepare(pair['rows']['native_first']['response'],evidence_kind='SYNTHETIC_PROTOCOL_FIXTURE')
    activation=start+commit_offset
    queue=native.activate(candidate,activation,clock_domain=domain)
    observation=activation+m['label_ns'](14)
    request=queue.request(observation,observation,clock_domain=domain)
    raw=deepcopy(pair['actual_original_packet']['response']['actions'])
    response={'actions':raw,'rtc':m['RTC'].validate_rtc(request.wire())}
    rolling=m['BoundTemporalAdapter'](pair['bounds']).prepare_response(request,response,
        evidence_kind='SYNTHETIC_PROTOCOL_FIXTURE')
    before=queue.plan
    queue.commit(request,rolling,observation+200_000_000)
    probes=np.linspace(request.observation_phase_ns,request.cut_phase_ns,121).astype(np.int64)
    return {'scope':'SYNTHETIC_ROLLING_COUNTEREXAMPLE_NOT_NEW_MODEL_INFERENCE',
        'raw_numeric_values_reused_from_real_failed_first_packet':True,
        'actual_rolling_model_response_acquired':False,'request_index':request.index,
        'H50':50,'freeze_steps':request.wire()['freeze_steps'],
        'known_prefix_length':len(request.previous),'execution_label_hz':request.wire()['execution_label_hz'],
        'original_prefix_exactly_unchanged':bool(np.array_equal(before.sample(probes),queue.plan.sample(probes))),
        'cut_reference_velocity_jump_deg_s':np.rad2deg(rolling.diagnostics['cut_reference_velocity_jump_rad_m_s'][:6]).tolist(),
        'temporal_qp_alone_rejects_bad_soft_prefix':False,
        'rolling_readiness':False,
        'reason':'Switching first packet to native does not solve a later inconsistent soft-guided seam; all-packet boundary admission/repair still required'}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real-run-dir',required=True)
    parser.add_argument('--solver-dir',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    m=math_modules();pair=load_mapping_fixture(args.real_run_dir,m)
    available=pair['actual_original_packet']['physical_reference_acceptance']['commit_monotonic_ns']-pair['original_observation_ns']
    out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=False)
    results=[]
    for mode in ('guided_hold35','native_first'):
        result,arrays=replay_mapping(pair,mode,'SAME_SAVED_GUIDED_H50_CONTROL_MAPPING_ONLY',available,args.solver_dir,
            cpu_budget_ns=20_000_000,tau=.03611058236938804,weight=200.,gripper_lookahead_ms=0.,math_modules=m)
        result['timing_rule']='same original observation-to-commit availability rounded up to20ms; not new inference timing'
        result['control_mapping_scope']='both branches are CPU protocol fixtures with the SAME real guided numeric output'
        if arrays is not None:np.savez_compressed(out/(mode+'.npz'),**arrays)
        (out/(mode+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
        results.append(result)
    commit_offset=((available+m['SEND_NS']-1)//m['SEND_NS'])*m['SEND_NS']
    probe=rolling_protocol_probe(pair,m,commit_offset)
    summary={'status':'CPU_STARTUP_MAPPING_DIAGNOSTIC_ONLY_ROLLING_NOT_READY',
        'results':results,'rolling_protocol_probe':probe,'source_status_preserved':pair['source_status'],
        'source_sha256':pair['source_sha256'],'source_h50_origin':pair['source_h50_origin'],
        'native_model_inference_performed':False,'real_model_calls':0,'robot_commands':0,
        'actual_robot_tracking_improvement_claimed':False,'tau_recalibrated':False,
        'readiness':False,'source_manifest':m['manifest'],'script_sha256':sha(__file__),
        'fixture_note':pair['fixture_note']}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    print(json.dumps({'output':str(out),'status':summary['status'],
        'metrics':[{'mode':r['mode'],'status':r['status'],'seam_deg_s':r.get('seam_reference_velocity_deg_s'),
                    'metrics':r.get('metrics')} for r in results], 'rolling_probe':probe},ensure_ascii=False),flush=True)

if __name__=='__main__':main()

