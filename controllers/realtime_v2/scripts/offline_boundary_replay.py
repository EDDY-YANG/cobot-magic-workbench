"""Two saved real failures; restore accepted references without state resets.
No calls to a model or robot. Subsequent RTC is inspected, not relabelled as a
real future prediction after changing the accepted reference.
"""
from pathlib import Path
import sys,json,importlib.util,time
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/'v2-runtime' if (ROOT/'v2-runtime').exists() else ROOT
sys.path[:0]=[str(RUNTIME),str(RUNTIME/'src'),str(RUNTIME/'vendor')]
from src.temporal_rpc import config,load_original
from src.cached_timeqp import CachedTimeQP
from client_time_queue import Plan,Slice,ClientTimeQueue,label_ns,candidate_seal
from soft_guided_bridge import SoftGuidedBridgeCurve
from gripper_candidate import build_c2_gripper_candidate
from src.joint_boundary_repair import JointBoundaryCurve,ReferenceRepairWatchdog
from src.continuity_adapter import BoundTemporalAdapter

def original_optimizer(cfg,scale,old=False):
    opt=CachedTimeQP(load_original(),cfg,target_scale=scale)
    if old:
        # Reproduce the recorded pre-seven-gear multiplication bit pattern.
        opt.original.s_ref=(1/opt.original.dt_ref)*scale
    return opt

def optimized(packet,bounds,scale,old):
    raw=np.clip(np.asarray(packet['response']['actions'])[12:],bounds[:,0],bounds[:,1]).astype(np.float32)
    return np.clip(np.asarray(original_optimizer(config('rolling38'),scale,old).optimize(raw.tolist())),bounds[:,0],bounds[:,1])

def saved_curve(packet,bounds,old=False):
    d=packet['prepared_reference_diagnostics'];scale=d['manual_speed_snapshot']['target_scale']
    base=optimized(packet,bounds,scale,old);obs=d['bridge_start_phase_ns']-label_ns(15)
    times=np.array([obs+label_ns(i) for i in range(15,51)],np.int64)
    anchor=np.array(packet['rtc']['previous_actions'][15])
    curve=SoftGuidedBridgeCurve(times,np.vstack((base[3:],base[-1])),anchor,
        np.array(d['inherited_cut_velocity_rad_m_s']),d['bridge_end_phase_ns'])
    gripper=d.get('gripper_transition',{})
    if gripper.get('triggered'):
        curve=build_c2_gripper_candidate(curve,np.array(gripper['inherited_cut_acceleration_rad_m_s2']),
                                        known_end_ns=d['bridge_end_phase_ns'],bounds=bounds[6])
    lo,hi,_,_=curve.extrema()
    np.testing.assert_allclose(lo,d['position_min_rad_m'],atol=3e-12,rtol=0)
    np.testing.assert_allclose(hi,d['position_max_rad_m'],atol=3e-12,rtol=0)
    return curve

def restore_case(path):
    data=json.loads(Path(path).read_text(encoding='utf8'));bounds=np.array(data['bounds']);packets=data['packets']
    previous=packets[:-1];failed=packets[-1];old=data['name'].startswith('12-36')
    curves=[saved_curve(p,bounds,old) for p in previous]
    pieces=tuple(Slice(int(c.times[0]),int(curves[i+1].times[0]) if i+1<len(curves) else int(c.times[-1]),c)
                 for i,c in enumerate(curves))
    plan=Plan(pieces,'RESTORED_RECORDED_ACCEPTED_REFERENCE')
    epoch=previous[-1]['physical_reference_acceptance']['activation_due_ns']
    obs=failed['observation_anchor_ns'];phase=obs-epoch
    domain=('ARCHIVED_REAL_REFERENCE_CPU','NO_LIVE_DEVICE','MONOTONIC_RESTORED')
    queue=ClientTimeQueue(plan.sample(phase),bounds,epoch,trial_index=failed['rtc']['trial_index'],clock_domain=domain)
    queue.plan=plan;queue.index=failed['rtc']['request_index']-1;queue.revision=failed['rtc']['request_index']
    context=queue.request(obs,failed['request_started_ns'],clock_domain=domain)
    # Bit-exact model prefix condition is a requirement, not repaired to pass.
    np.testing.assert_array_equal(context.previous,failed['rtc']['previous_actions'])
    scale=previous[-1]['prepared_reference_diagnostics']['manual_speed_snapshot']['target_scale']
    return data,bounds,failed,queue,context,scale,old

def compare(path):
    data,bounds,failed,queue,context,scale,old=restore_case(path)
    base=optimized(failed,bounds,scale,old)
    times=np.array([context.observation_phase_ns+label_ns(i) for i in range(15,51)],np.int64)
    legacy=SoftGuidedBridgeCurve(times,np.vstack((base[3:],base[-1])),context.cut_anchor,
        context.old_plan.velocity(context.cut_phase_ns,'left'),context.observation_phase_ns+label_ns(34))
    lo,hi,_,tol=legacy.extrema();excess=np.maximum(bounds[:,0]-lo,hi-bounds[:,1])
    axes=tuple(int(i) for i in np.flatnonzero(excess[:6]>tol[:6]))
    projection=JointBoundaryCurve(legacy,bounds,axes)
    plo,phi,_,ptol=projection.extrema()
    assert np.all(np.maximum(bounds[:6,0]-plo[:6],phi[:6]-bounds[:6,1])<=ptol[:6])
    adapter=BoundTemporalAdapter(bounds,optimizer_factory=lambda **cfg:original_optimizer(cfg,scale,old))
    began=time.perf_counter_ns();candidate=adapter.prepare_response(context,failed['response'],evidence_kind='SAVED_REAL_MODEL_RESPONSE');elapsed=(time.perf_counter_ns()-began)/1e6
    curve=candidate.plan.pieces[-1].curve;lo,hi,_,tol=curve.extrema()
    assert np.all(np.maximum(bounds[:,0]-lo,hi-bounds[:,1])<=tol)
    np.testing.assert_array_equal(curve.sample(context.cut_phase_ns),context.cut_anchor)
    np.testing.assert_array_equal(curve.velocity(context.cut_phase_ns),context.old_plan.velocity(context.cut_phase_ns,'left'))
    probes=np.unique(np.r_[times,np.linspace(times[0],times[-1],1001).astype(np.int64)])
    untouched=[i for i in range(7) if i not in axes]
    np.testing.assert_array_equal(curve.sample(probes)[:,untouched],legacy.sample(probes)[:,untouched])
    # Commit at the recorded publication instant. This is replayed timing,
    # not a claim that today's CPU measurements happened on that past clock.
    queue.commit(context,candidate,failed['publication_rejected_ns'])
    next_obs=context.observation_ns+label_ns(14)
    following=queue.request(next_obs,next_obs,clock_domain=context.clock_domain)
    expected=queue.plan.sample(np.array([following.observation_phase_ns+label_ns(i) for i in range(35)],np.int64))
    np.testing.assert_array_equal(following.previous,expected)
    watchdog=ReferenceRepairWatchdog();active_ticks=0
    for at in range(int(times[0]),int(times[-1]),20_000_000):
        active_ticks+=watchdog.observe(at+1_000_000_000,candidate.plan,at) is not None
    result={'name':data['name'],'source':data['source'],'request_index':failed['rtc']['request_index'],
        'target_scale':scale,'old_joint_violation_deg':np.rad2deg(np.maximum(0,excess[:6])).tolist(),
        'new_joint_violation_deg':np.rad2deg(np.maximum(0,np.maximum(bounds[:6,0]-lo[:6],hi[:6]-bounds[:6,1]))).tolist(),
        'old_prefix_bit_exact':True,'cut_position_velocity_bit_exact':True,'untouched_axes_gripper_bit_exact':True,
        'next_RTC_reads_repaired_plan_bit_exact':True,'new_real_future_model_response_claimed':False,
        'prepare_ms_pc':elapsed,'repair':candidate.diagnostics['joint_reference_repair'],
        'added_step_deg':candidate.diagnostics['soft_guided_splice_compatibility']['final_max_added_step_deg'],
        'projection_A_position_legal':True,'projection_A_has_reference_velocity_corners':True,
        'watchdog_single_packet_replay':watchdog.summary(),'candidate_seal_valid':candidate.seal==candidate_seal(candidate),
        'model_calls':0,'hardware_calls':0}
    return result,(data,context,candidate)

if __name__=='__main__':
    result=[compare(ROOT/'inputs'/name)[0] for name in ('12-36-boundary.json','13-32-boundary.json')]
    (ROOT/'receipts/two-real-failure-replay.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps([{k:r[k] for k in ('name','old_joint_violation_deg','new_joint_violation_deg','prepare_ms_pc','added_step_deg')} |
                     {'selected':r['repair']['strategy'],'duration_s':r['repair'].get('selected_duration_s'),'RTC_condition_preserved':r['old_prefix_bit_exact']}
                    for r in result],ensure_ascii=False))
