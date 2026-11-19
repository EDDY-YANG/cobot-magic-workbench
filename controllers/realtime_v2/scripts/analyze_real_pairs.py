"""Analyze saved real FP32 native/guided first packets; never call a model.

Requires the completed compare-first run, its actual observation and both
responses. State evolution below is an explicitly simulated first-order plant,
not stationary robot feedback relabelled as tracking. The live NO_SEND core's
actual command history is never touched by this offline plant simulation.

Two timing views use hypothetical fresh copies of the SAME real observation:
common measured request duration vs each measured request duration. Original
observation age/lease sequencing timestamps remain separately preserved.
"""
from pathlib import Path
import argparse
import hashlib
import json
import math
import sys
import time
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODES = ('native_first', 'guided_hold35')

def require(ok, message):
    if not ok:
        raise ValueError(message)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
        ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def read_json(path):
    def nonfinite(value):
        raise ValueError('Nonfinite JSON: '+value)
    return json.loads(Path(path).read_text(encoding='utf8'),parse_constant=nonfinite)

def load_real_pair(run_dir, bounds_path):
    """Fail closed before importing/acquiring any solver or generating output."""
    run_dir=Path(run_dir).resolve(strict=True)
    paths={name:run_dir/name for name in ('operation.json','first-packet-observation.json',
        'native_first-response.json','guided_hold35-response.json')}
    data={name:read_json(path) for name,path in paths.items()}
    op=data['operation.json'];snapshot=data['first-packet-observation.json']
    require(op.get('schema')=='tiger-v2-real-io-nosend-v1' and op.get('mode')=='compare-first',
            'Only an actual isolated V2 compare-first receipt is admitted')
    require(op.get('status')=='REAL_IO_NO_SEND_COMPLETED_NOT_MOTION_ADMISSION',
            'Pair collection did not complete; failure must be retained, not relabelled')
    require(op.get('physical_motion_started') is False and op.get('APPLY_supported') is False,
            'Expected NO_SEND-only real acquisition')
    require(op.get('model',{}).get('precision')=='fp32', 'Current task requires actual loaded FP32')
    require(snapshot.get('schema')=='tiger-fp32-shadow-observation-v1'
            and snapshot.get('hardware_synchronized') is False, 'Expected actual independently sampled observation')
    require(snapshot.get('state_units')==['rad']*6+['m'],'Original rad6/metre7 unit declaration missing')
    anchor=np.asarray(snapshot.get('state_right_rad_m'),float)
    require(anchor.shape==(7,) and np.isfinite(anchor).all(),'Real measured rad6/metre7 initial state missing')
    bounds=np.asarray(read_json(bounds_path)['bounds_rad_m'],float)
    require(bounds.shape==(7,2) and np.isfinite(bounds).all() and np.all(bounds[:,0]<bounds[:,1]),'Invalid bound contract')
    require(np.all(anchor>=bounds[:,0]) and np.all(anchor<=bounds[:,1]),'Actual initial state outside bounds; no clipping/reset')
    ready=snapshot['packet_ready_monotonic_ns']
    require(type(ready) is int and ready>0,'Original observation monotonic anchor missing')
    snapshot_sha=digest(snapshot)
    rows={mode:data[mode+'-response.json'] for mode in MODES}
    pair_rows=op.get('comparison',{}).get('pairs',[])
    require(len(pair_rows)==2 and op['comparison'].get('same_input_and_seed') is True,'Completed pair identity missing')
    for index,mode in enumerate(MODES):
        row=rows[mode];record=pair_rows[index]
        require(record.get('mode')==mode and record.get('snapshot_sha256')==snapshot_sha,
                'Pair record differs from saved observation')
        require(digest(record['response'])==digest(row),'Standalone response differs from final operation receipt')
        require(row.get('observation_mode')=='FROZEN_REAL_FIRST_PACKET_COMPARISON'
                and row.get('evidence_scope')=='REPLAYED_REAL_OBSERVATION_NOT_LIVE_ADMISSIBLE',
                'Frozen real-input provenance missing')
        require(row.get('observation_sha256')==snapshot_sha and row.get('observation_anchor_ns')==ready,
                'Response bound to another observation/time')
        require(row.get('cancelled_result') is False and row.get('robot_commands')==0,
                'Cancelled/dispatched response cannot be used as this pair')
        require(row.get('physical_execution_admissible') is False,'Unexpected motion-admission claim')
        rtc=row['rtc'];returned=row['response']['rtc']
        require(rtc.get('enabled')==(mode=='guided_hold35') and rtc.get('request_index')==0,
                'Wrong first-packet condition')
        require(returned.get('trial_seed')==42+rtc['trial_index']==record['trial_seed'], 'Seed mismatch')
        require(row['request_started_ns']<=row['sent_ns']<=row['received_ns']<=row['validated_ns'],
                'Original request timing order invalid')
        require(row['validated_ns']>=ready,'Response predates observation')
        raw=np.asarray(row['response']['actions'],float)
        require(raw.shape==(50,7) and np.isfinite(raw).all(),'Exact real H50x7 output required')
        if mode=='guided_hold35':
            require(np.array_equal(np.asarray(rtc['previous_actions']),np.tile(anchor,(35,1))),
                    'Guided first packet is not conditioned on the exact measured HOLD35')
    require(rows[MODES[0]]['model_identity_sha256']==rows[MODES[1]]['model_identity_sha256'],
            'Different loaded model identity')
    require(rows[MODES[0]]['model_identity_sha256']==op['model']['identity_sha256'],
            'Response model identity differs from run admission')
    require(rows[MODES[0]]['rtc']['trial_index']==rows[MODES[1]]['rtc']['trial_index'],'Different RNG trial')
    require(pair_rows[0]['observation_payload_sha256']==pair_rows[1]['observation_payload_sha256'],
            'Original observation payloads differ')
    return {'anchor':anchor,'bounds':bounds,'rows':rows,'operation':op,'snapshot':snapshot,
        'source_sha256':{str(path):sha(path) for path in (*paths.values(),Path(bounds_path))},
        'original_observation_ns':ready,'snapshot_sha256':snapshot_sha,
        'model_identity_sha256':rows[MODES[0]]['model_identity_sha256']}

def load_math():
    sys.path[:0]=[str(ROOT/'src'),str(ROOT/'vendor')]
    from pinned_components import verify_all, command_limiter
    manifest=verify_all()
    from bootstrap_alignment import NativeBootstrap,GuidedBootstrapQueue,BoundTemporalAdapter,validate_response
    from client_time_queue import SEND_NS,NS
    from portable_backend import OFFICIAL,BUILD_ALLOWED,BUILD_RECEIPTS
    return locals()

def simulate(pair, mode, view, ready_duration_ns, solver_dir, *, cpu_budget_ns,
             tau,weight,gripper_lookahead_ms,math_modules):
    m=math_modules;period=m['SEND_NS'];ns=m['NS'];start=1_000_000_000
    anchor=pair['anchor'];bounds=pair['bounds'];row=pair['rows'][mode]
    rtc=row['rtc'];raw,binding=m['validate_response'](rtc,row['response'],evidence_kind='SAVED_REAL_MODEL_RESPONSE')
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
    commit=start+((ready_duration_ns+cpu_budget_ns+period-1)//period)*period
    result['counterfactual_commit_s']=(commit-start)/ns
    if commit-start>600_000_000:
        return {**result,'status':'ADMISSION_FAILED_600MS_BUDGET','metrics':None},None
    domain=('OFFLINE_REAL_PAIR','SIMULATED_CLOCK','MONOTONIC_NS')
    prep_start=time.perf_counter_ns()
    if mode=='native_first':
        bootstrap=m['NativeBootstrap'](anchor,bounds,start,trial_index=rtc['trial_index'],clock_domain=domain,sender_epoch_ns=start)
        require(bootstrap.ticket.wire()==rtc,'Reconstructed native RTC differs from actual request')
        candidate=bootstrap.prepare(row['response'],evidence_kind='SAVED_REAL_MODEL_RESPONSE')
        queue=bootstrap.activate(candidate,commit,clock_domain=domain)
        plan=queue.plan;epoch=commit
    else:
        queue=m['GuidedBootstrapQueue'](anchor,bounds,start,trial_index=rtc['trial_index'],clock_domain=domain)
        context=queue.request(start,start,clock_domain=domain)
        require(context.wire()==rtc,'Reconstructed guided RTC differs from actual request')
        candidate=m['BoundTemporalAdapter'](bounds).prepare_response(context,row['response'],evidence_kind='SAVED_REAL_MODEL_RESPONSE')
        queue.commit(context,candidate,commit);plan=queue.plan;epoch=start
    result['temporal_prepare_ms']=(time.perf_counter_ns()-prep_start)/1e6
    result['temporal']=candidate.diagnostics
    if result['temporal_prepare_ms']>cpu_budget_ns/1e6:
        return {**result,'status':'ADMISSION_FAILED_FIXED_CPU_PREPARATION_BUDGET','metrics':None},None
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

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',required=True,help='Actual completed compare-first directory')
    parser.add_argument('--bounds-json',required=True,help='Verified contract or binding containing bounds_rad_m')
    parser.add_argument('--solver-dir',required=True,help='Private solver already built by build_solver.py')
    parser.add_argument('--output',required=True,help='New timestamped output directory; never overwrite')
    parser.add_argument('--tau',type=float,default=.03611058236938804)
    parser.add_argument('--weight',type=float,default=200.)
    parser.add_argument('--cpu-budget-ms',type=float,default=20.)
    parser.add_argument('--gripper-lookahead-ms',type=float,choices=(0.,90.),default=0.)
    args=parser.parse_args()
    require(args.tau>0 and args.weight>0 and 0<args.cpu_budget_ms<=100,'Invalid explicit replay parameters')
    pair=load_real_pair(args.run_dir,args.bounds_json)
    modules=load_math()
    durations={mode:row['validated_ns']-row['request_started_ns'] for mode,row in pair['rows'].items()}
    out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=False)
    results=[]
    for view in ('controlled_common_request_duration','individual_measured_request_duration'):
        for mode in MODES:
            duration=max(durations.values()) if view=='controlled_common_request_duration' else durations[mode]
            result,arrays=simulate(pair,mode,view,duration,args.solver_dir,
                cpu_budget_ns=round(args.cpu_budget_ms*1e6),tau=args.tau,weight=args.weight,
                gripper_lookahead_ms=args.gripper_lookahead_ms,math_modules=modules)
            stem=view+'-'+mode
            if arrays is not None:
                np.savez_compressed(out/(stem+'.npz'),**arrays)
            (out/(stem+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
            results.append(result)
    summary={'schema':'tiger-v2-real-pair-first-order-offline-analysis-v1',
        'status':'CPU_ANALYSIS_FINISHED_NOT_REAL_MOTION_VALIDATION','results':results,
        'source_sha256':pair['source_sha256'],'snapshot_sha256':pair['snapshot_sha256'],
        'model_identity_sha256':pair['model_identity_sha256'],'official_source_manifest':modules['manifest'],
        'solver_build_receipts':modules['BUILD_RECEIPTS'],'script_sha256':sha(__file__),
        'new_model_calls':0,'hardware_calls':0,'physical_tracking_validated':False,
        'limits':['two frozen same-input leases do not establish live deadline success',
                  'all joint state evolution is simulated using an uncalibrated first-order model',
                  'native/guided reference geometry and durations differ; tracking errors use respective reference',
                  'single-packet terminal preview hold is diagnostic only',
                  'gripper has reference/command curves only; no simulated or actual gripper tracking claim']}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    print(json.dumps({'output':str(out),'statuses':[r['status'] for r in results]},ensure_ascii=False))
    if any(r['status']!='SIMULATED_FIRST_PACKET_COMPLETE' for r in results):
        raise SystemExit(2)

if __name__=='__main__':
    main()
