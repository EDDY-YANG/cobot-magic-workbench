"""Approved V2 reference-step warning only; no actuator or stop operations."""
import math

THRESHOLD_DEG = 10.0
SCHEMA = 'tiger-v2-reference-step-warning-v1'

def policy():
    return {'schema':SCHEMA,'threshold_deg':THRESHOLD_DEG,'action':'diagnostic_only',
        'automatic_stop_from_numeric_exceedance':False,'reference_hz':18,
        'scope':'J1_J6_REFERENCE_STEP_INFLATION_NOT_PHYSICAL_TRACKING',
        'position_and_MPC_feasibility_guards_unchanged':True,'live_warning_max_hz':1}

def assess(by_joint,first_extra):
    a=[float(x) for x in by_joint];b=[float(x) for x in first_extra]
    if len(a)!=6 or len(b)!=6 or not all(math.isfinite(x) and x>=0 for x in a+b):
        raise ValueError('Invalid reference-step diagnostic values')
    peaks=[max(x,y) for x,y in zip(a,b)]
    return {**policy(),'exceeded':max(peaks)>THRESHOLD_DEG,
        'exceeded_axes':[i+1 for i,x in enumerate(peaks) if x>THRESHOLD_DEG],
        'maximum_added_step_deg':max(peaks),'added_step_by_joint_deg':peaks}

def emit_if_due(channel,accepted,now_ns,last_ns):
    """One bounded nonblocking event per second; every packet retains metrics."""
    d=accepted.get('temporal',{}).get('soft_guided_splice_compatibility',{}).get('step_warning',{})
    if not d.get('exceeded') or channel is None or not callable(getattr(channel,'emit',None)):
        return last_ns
    if last_ns is not None and now_ns-last_ns<1_000_000_000:return last_ns
    axes='、'.join('J'+str(i) for i in d['exceeded_axes'])
    channel.emit({'schema':SCHEMA,'event':'V2_REFERENCE_STEP_WARNING','status':'V2_REFERENCE_STEP_WARNING',
        'run_id':channel.run_id,'request_index':accepted['request_index'],'runtime_ns':int(now_ns),
        'message':f'衔接参考步幅报警：{axes}最大额外步幅{d["maximum_added_step_deg"]:.2f}°，超过10°；仅报警，继续运行。',
        'diagnostic':d,'new_reference_committed':True,'motion_speed_measured':False})
    return int(now_ns)
