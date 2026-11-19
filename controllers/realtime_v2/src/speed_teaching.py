"""Bounded human speed-label export. Pure CPU metadata; never controls a robot."""
from collections import Counter
import math

SCHEMA='tiger-v2-human-speed-labels-v1'
FILENAME='speed-teaching.json'
GEARS=(.5,.8,1.,1.5,2.,3.,4.)


def collection_contract():
    return {'schema':SCHEMA,'supported':True,'mode':'AUTOMATIC_WITH_V2_PHYSICAL_RECORDING',
        'filename':FILENAME,'max_commands':4096,'default_is_human_label':False,
        'learning_method':'NEURAL_NETWORK_PROVISIONAL_NOT_TRAINED',
        'controller_authority':'HUMAN_ONLY','automatic_speed_control_enabled':False}


def build_labels(speed,*,operation,prompt,clock_domain,raw_episode):
    """Preserve intent and execution separately; never label default as human.

    Receive time is an explicitly approximate human decision anchor. Future
    training must join causally available images/state in this same clock domain
    and review task outcomes. REFERENCE_ACTIVE is not physical speed proof.
    """
    commands=speed.get('commands',[])
    if not isinstance(commands,list) or len(commands)>4096:
        raise ValueError('Bounded manual-speed command list required')
    domain={k:clock_domain.get(k) for k in ('host_id','boot_id','clock_name')}
    if not all(isinstance(x,str) and x for x in domain.values()):
        raise ValueError('Actual robot host/boot/clock domain required')
    rows=[];seen=set()
    for cmd in commands:
        cid=cmd.get('command_id');scale=cmd.get('target_scale');received=cmd.get('runtime_received_ns')
        if not isinstance(cid,str) or not cid or cid in seen:
            raise ValueError('Unique actual operator command ID required')
        seen.add(cid)
        if type(scale) not in (float,int) or not math.isfinite(scale) or not .5<=scale<=4:
            raise ValueError('Finite operator scale in0.5..4 required')
        if type(received) is not int or received<=0:
            raise ValueError('Actual runtime receipt timestamp required')
        times={k:cmd.get(k) for k in ('frozen_ns','plan_committed_ns','reference_sample_ns',
                                     'reference_effective_ns','writer_ack_completed_ns')}
        for value in times.values():
            if value is not None and (type(value) is not int or value<received):
                raise ValueError('Speed lifecycle timestamp precedes receipt')
        committed=times['plan_committed_ns'];active=times['reference_effective_ns']
        if active is not None and (committed is None or active<committed):
            raise ValueError('Active reference requires earlier successful commit')
        row={'command_id':cid,'target_scale':float(scale),'preset_gear':float(scale) if scale in GEARS else None,
            'label_source':'EXPLICIT_HUMAN_WEB_COMMAND','runtime_received_ns':received,
            **times,'first_frozen_request_index':cmd.get('request_index'),'status':cmd.get('status'),
            'reference_activated':active is not None,'physical_speed_verified':False,
            'received_to_reference_active_ms':None if active is None else (active-received)/1e6,
            'decision_anchor_kind':'ROBOT_RECEIVE_TIME_PROXY_NOT_HUMAN_PERCEPTION_TIME',
            'observation_alignment':'PENDING_CAUSAL_FRAME_AND_STATE_JOIN',
            'outcome_review':'UNREVIEWED','training_ready':False}
        rows.append(row)
    rows.sort(key=lambda r:(r['runtime_received_ns'],r['command_id']))
    bound=operation.get('source_binding',{}).get('model',{})
    model={k:bound.get(k) for k in ('precision','step','instance','identity_sha256')}
    return {'schema':SCHEMA,'run_id':speed['run_id'],'run_dir':operation.get('run_dir'),
        'prompt':prompt,'model':model,'clock_domain':domain,
        'source_files':{'manual_events':'manual-speed-summary.json','trajectory_evidence':'evidence-manifest.json',
                        'operation':'operation.json','raw_episode':str(raw_episode) if raw_episode else None},
        'run_status':operation.get('status'),'trajectory_evidence_complete':operation.get('evidence',{}).get('complete') is True,
        'raw_recording_result':operation.get('recording'),
        'human_command_count':len(rows),'reference_activated_count':sum(r['reference_activated'] for r in rows),
        'counts_by_status':dict(Counter(r['status'] for r in rows)),
        'observed_target_scales':sorted(set(r['target_scale'] for r in rows)),
        'human_reaction_latency_ms':None,'camera_state_alignment_complete':False,
        'human_default_labels_synthesized':False,'physical_task_success':'UNREVIEWED',
        'training_ready':False,'labels':rows,
        'neural_network_plan':{'status':'PROVISIONAL_NOT_TRAINED','inputs':['causal_camera_frames',
            'J1_J6_feedback','gripper_feedback','previous_requested_scale','task_prompt'],
            'output':'bounded_target_scale_0.5_to_4.0','head':'small_regression_network',
            'continuous_input_scales_preserved':True,'split_by':'whole_recording_and_physical_position',
            'automatic_control_enabled':False}}


def saved_receipt(document,path):
    """Caller may invoke only after its verified USB journal save succeeds."""
    return {'schema':SCHEMA,'saved':True,'run_id':document['run_id'],'path':str(path),
        'human_command_count':document['human_command_count'],
        'reference_activated_count':document['reference_activated_count'],
        'trajectory_evidence_complete':document['trajectory_evidence_complete'],
        'training_ready':False,'neural_network_status':'PROVISIONAL_NOT_TRAINED'}
