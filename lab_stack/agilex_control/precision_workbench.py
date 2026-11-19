"""Unified manual precision/checkpoint jobs, using the existing WebApp ownership."""
from copy import deepcopy
import math,socket,threading,time
from .precision_services import RealPrecisionIO,PrecisionError,STEPS,require
from .v2_web_services import (RealV2IO,V2RunError,V2_WINDOW_S,V2_ACCEPTANCE_S,
                             V2_MIN_WINDOW_S,V2_MAX_WINDOW_S,validate_v2_duration,v2_window_contract_matches)

ACTIONS=('precision_load','precision_acceptance','precision_deploy','precision_release','v2_acceptance','v2_deploy')
V2_ACTIONS=('v2_acceptance','v2_deploy')
DEFAULT_TASK='右臂将红色方块放入绿色桶'

def validate_params(action,params):
    require(action in ACTIONS and isinstance(params,dict),'未知精度操作')
    expected=set() if action=='precision_release' else {'precision','step','task'}
    if action in ('precision_deploy','v2_deploy'):expected|={'leader_isolated','path_reviewed','duration'}
    if action=='v2_deploy' and 'operator_skip_no_motion_acceptance' in params:
        expected.add('operator_skip_no_motion_acceptance')
        require(params['operator_skip_no_motion_acceptance'] is True,'跳过NO_SEND必须是明确的人工直接运行请求')
    require(set(params)==expected,'精度操作参数必须完整且不能夹带其他字段')
    if not expected:return deepcopy(params)
    require(params['precision'] in ('FP32','BF16'),'精度只能为FP32或BF16')
    if action in V2_ACTIONS:require(params['precision']=='FP32','独立V2仅接入FP32模型')
    require(type(params['step']) is int and params['step'] in STEPS,'检查点必须为已登记候选')
    from .h50_prompt import validate_prompt
    validate_prompt(params['task'])
    require(not any(ord(c)<32 or ord(c)==127 for c in params['task']),'Prompt必须为单行文本')
    if action in ('precision_deploy','v2_deploy'):
        if action=='v2_deploy':
            validate_v2_duration(params['duration'])
        else:require(type(params['duration']) in (int,float) and math.isfinite(params['duration']) and 1<=params['duration']<=1800,'运行时长必须为1至1800秒')
        require(params['leader_isolated'] is True and params['path_reviewed'] is True,'请完成当前会话的主从隔离与起点/急停确认')
    return deepcopy(params)

def v2_feedback_gate(state,deploy):
    """Actual feedback checks; NO_SEND never needs automatic motor enable."""
    if state.get('stop_latch',{}).get('latched') is not False:return '软件STOP仍锁存；需显式解除后再运行V2'
    from .mode_state import controller_modes
    mode=controller_modes(state)['right']
    allowed=('CAN_MOVEJ',) if deploy else ('CAN_MOVEJ','STANDBY')
    if mode.get('fresh') is not True or mode.get('classification') not in allowed or mode.get('external_activity'):
        return '独立V2需要新鲜合法控制模式且没有其他控制源活动'
    arm=state.get('status',{}).get('arms',{}).get('right',{});grip=arm.get('gripper_feedback',{})
    from .hardware import require_feedback,HardwareBlocked
    try:require_feedback(arm,max_age_s=.2,require_enabled=deploy,require_can=deploy)
    except (HardwareBlocked,ValueError,TypeError) as exc:return '六关节反馈不满足V2条件：'+str(exc)
    flags=arm.get('motor_flags')
    if not isinstance(flags,list) or len(flags)!=6 or any(type(x) is not int or x & 0xBF for x in flags):return '六关节健康状态未核实'
    if deploy and arm.get('enabled')!=[True]*6:return '六关节使能未核实'
    age=grip.get('age_s')
    if grip.get('fresh') is not True or grip.get('healthy') is not True or (deploy and grip.get('enabled') is not True) or type(age) not in (int,float) or not math.isfinite(age) or not 0<=age<=.2:
        return '夹爪反馈、健康或使能未核实'
    for role in ('front','right'):
        age=state.get('hub',{}).get('cameras',{}).get(role,{}).get('age_s')
        if type(age) not in (int,float) or not math.isfinite(age) or not 0<=age<=.25:return '实际相机不新鲜：'+role
    return None

def v2_gate(state,component,action,params):
    profile=component.get('v2_profile',{})
    if profile.get('fresh') is not True or profile.get('ready') is not True or profile.get('error'):
        return profile.get('error') or '独立V2运行器尚未新鲜核实'
    if not v2_window_contract_matches(profile):return '独立V2运行器1至1800秒窗口合同不匹配'
    row=component.get('models',{}).get('FP32',{})
    if row.get('ready') is not True:return '独立V2需要先加载FP32模型'
    if not isinstance(row.get('fingerprint'),str) or not row['fingerprint']:return '当前FP32模型身份尚未核实'
    requested_manual=params is not None and params.get('operator_skip_no_motion_acceptance') is True
    supported_manual=profile.get('manual_run_without_nosend_supported') is True
    if requested_manual and not supported_manual:return '当前V2运行器不支持人工跳过NO_SEND'
    manual=supported_manual and (params is None or requested_manual)
    if action=='v2_deploy' and not manual:
        admission=profile.get('acceptance') or {}
        if (admission.get('validated') is not True or admission.get('step')!=row.get('step') or
                not row.get('fingerprint') or admission.get('model_fingerprint')!=row['fingerprint']):
            return '当前FP32实例尚无匹配的独立V2真实NO_SEND验收，请先验证'
    if params is not None:
        if params.get('precision')!='FP32' or row.get('step')!=params.get('step'):return '所选FP32检查点尚未实际加载；请先加载'
        if not any(x.get('step')==params['step'] and x.get('available') is True for x in component.get('candidates',[])):return '所选检查点文件尚未核实可用'
        if action=='v2_deploy' and not manual:
            if (admission.get('validated') is not True or admission.get('step')!=params['step'] or
                    admission.get('task')!=params['task'] or not row.get('fingerprint') or
                    admission.get('model_fingerprint')!=row['fingerprint']):
                return '此FP32实例/检查点/任务尚无匹配的独立V2真实NO_SEND验收，请先验证'
    return v2_feedback_gate(state,action=='v2_deploy')

def gate(state,component,action,params=None):
    if state.get('connected') is not True:return '请先连接 AgileX'
    if any(j.get('state') in ('queued','running','unknown') for j in state.get('jobs',[])):return '当前任务或保存尚未完成'
    if component.get('fresh') is not True or component.get('error'):return component.get('error') or '4090精度服务状态尚未核实'
    if state.get('usb',{}).get('verified') is not True or state.get('usb',{}).get('fresh') is not True:return '当前机器人USB身份未新鲜核实'
    hub=state.get('hub',{});record=hub.get('recording',{})
    if hub.get('api_reachable') is not True or record.get('active') is not False or record.get('saving') is not False:return '需要现有Hub在线且没有活动录制/保存'
    if hub.get('recovery',{}).get('deployment_active') is True:return '机器人已有部署owner'
    models=component.get('models',{})
    for row in models.values():
        if row.get('present') and (row.get('busy') is not False or row.get('lease_active') is not False):return '模型仍在加载/推理或有活动租约'
    if action in V2_ACTIONS:return v2_gate(state,component,action,params)
    if action=='precision_release':return None if any(x.get('present') for x in models.values()) else '没有需要释放的模型'
    if params is None:
        if action=='precision_load':return None
        if not any(x.get('ready') for x in models.values()):return '先加载所选模型'
        return None  # Draft-specific checks are repeated before actual submission.
    precision,step=params['precision'],params['step'];row=models.get(precision,{})
    if not any(x.get('step')==step and x.get('available') is True for x in component.get('candidates',[])):return '所选检查点文件尚未核实可用'
    if action=='precision_load':return None
    if row.get('ready') is not True or row.get('step')!=step:return '所选精度/检查点尚未实际加载；请先加载'
    if not component.get('robot_profile',{}).get('ready'):return '独立机器人运行器尚未核实'
    if action=='precision_acceptance':return None
    if state.get('stop_latch',{}).get('latched') is not False:return '软件STOP仍锁存；需显式解除后再部署'
    if precision=='FP32':
        from .web_backend import h50_action_gate
        reason=h50_action_gate(state,state.get('config',{}),'h50_deploy',{'step':step,'operator_skip_no_motion_acceptance':False})
        if reason:return reason
    else:
        admissions=component.get('robot_profile',{}).get('admissions',[])
        if not any(a.get('step')==step and a.get('task')==params['task'] and a.get('model_fingerprint')==row.get('fingerprint')
                   and a.get('validated') is True for a in admissions):return '此BF16实例/检查点/任务尚无匹配的真实NO_SEND验收，请先验证'
        from .mode_state import controller_modes
        mode=controller_modes(state)['right']
        if mode.get('fresh') is not True or mode.get('classification')!='CAN_MOVEJ' or mode.get('external_activity'):return '需要新鲜CAN/MOVEJ且没有其他控制源活动'
        arm=state.get('status',{}).get('arms',{}).get('right',{});grip=arm.get('gripper_feedback',{})
        from .hardware import require_feedback,HardwareBlocked
        try:require_feedback(arm,max_age_s=.2,require_enabled=True,require_can=True)
        except (HardwareBlocked,ValueError,TypeError) as exc:return '六关节反馈不满足部署条件：'+str(exc)
        flags=arm.get('motor_flags')
        if arm.get('enabled')!=[True]*6 or not isinstance(flags,list) or len(flags)!=6 or any(type(x) is not int or x & 0xBF for x in flags):return '六关节健康/使能不满足部署条件'
        age=grip.get('age_s')
        if grip.get('fresh') is not True or grip.get('healthy') is not True or grip.get('enabled') is not True or type(age) not in (int,float) or not math.isfinite(age) or not 0<=age<=.2:return '夹爪反馈、健康或使能未核实'
    for role in ('front','right'):
        age=hub.get('cameras',{}).get(role,{}).get('age_s')
        if type(age) not in (int,float) or not math.isfinite(age) or not 0<=age<=.25:return '实际相机不新鲜：'+role
    return None

class PrecisionWorkbench:
    def __init__(self,app,io=None,*,start_poll=True,v2_io=None):
        self.app=app;self.io=io or RealPrecisionIO();self.lock=threading.RLock();self.quit=threading.Event()
        self.v2=v2_io or RealV2IO(ssh=getattr(self.io,'_ssh',None));self.v2_updated=None
        self.value={'models':{},'candidates':[],'robot_profile':{},'error':'等待4090实际状态'};self.updated=None;self.robot_updated=0.;self.catalog_updated=0.
        self.v2_value={'ready':False,'error':'等待独立V2运行器状态','duration_s':V2_WINDOW_S,'acceptance':{'validated':False}}
        if start_poll:
            threading.Thread(target=self._poll,daemon=True,name='precision-model-metadata').start()
            threading.Thread(target=self._poll_v2,daemon=True,name='v2-runner-metadata').start()
    @classmethod
    def try_create(cls,app):
        if socket.gethostname()!='user-MS-7E06':return None
        return cls(app)
    def snapshot(self):
        with self.lock:value=deepcopy(self.value);at=self.updated;v2=deepcopy(self.v2_value);v2_at=self.v2_updated
        age=None if at is None else max(0.,time.monotonic()-at)
        v2_age=None if v2_at is None else max(0.,time.monotonic()-v2_at)
        v2.update(age_s=v2_age,fresh=v2_age is not None and v2_age<=25.,experimental=True)
        value.update(schema='tiger-manual-precision-workbench-v1',enabled=True,age_s=age,
            fresh=age is not None and age<=6.,default_duration_s=30,max_duration_s=1800,default_task=DEFAULT_TASK,
            routes={'FP32':'原FP32＋guided RTC','BF16':'独立BF16＋guided RTC＋平滑','V2_FP32':'FP32＋Realtime-VLA V2'},
            v2_profile=v2,v2_min_duration_s=V2_MIN_WINDOW_S,v2_max_duration_s=V2_MAX_WINDOW_S,
            v2_default_duration_s=V2_WINDOW_S,automatic_deployment=False)
        return value
    def refresh(self,force_robot=False):
        try:
            models=self.io.inspect_models();now=time.monotonic()
            with self.lock:catalog=deepcopy(self.value.get('candidates',[]));robot=deepcopy(self.value.get('robot_profile',{}))
            if now-self.catalog_updated>30 or not catalog:catalog=self.io.catalog();self.catalog_updated=now
            if force_robot or now-self.robot_updated>10:
                try:robot=self.io.robot_metadata()
                except Exception as exc:robot={'ready':False,'error':str(exc),'admissions':[]}
                self.robot_updated=now
            with self.lock:self.value={'models':models,'candidates':catalog,'robot_profile':robot,'error':None};self.updated=time.monotonic()
        except Exception as exc:
            with self.lock:self.value['error']=str(exc);self.updated=time.monotonic()
        return self.snapshot()
    def _poll(self):
        while not self.quit.is_set():self.refresh();self.quit.wait(2.)
    def refresh_v2(self):
        try:value=self.v2.metadata()
        except Exception as exc:value={'ready':False,'error':str(exc),'duration_s':V2_WINDOW_S,'acceptance':{'validated':False}}
        with self.lock:self.v2_value=value;self.v2_updated=time.monotonic()
        return self.snapshot()['v2_profile']
    def _poll_v2(self):
        while not self.quit.is_set():self.refresh_v2();self.quit.wait(10.)
    def capabilities(self,state):
        component=self.snapshot()
        return {a:{'available':(reason:=gate(state,component,a)) is None,'reason':reason} for a in ACTIONS}
    def validate(self,state,action,params):
        validate_params(action,params);reason=gate(state,self.snapshot(),action,params);require(reason is None,reason)
    def speed_snapshot(self):
        return self.v2.speed_snapshot()
    def speed_submit(self,data):
        return self.v2.speed_submit(data)
    def execute(self,job):
        action,params=job['action'],job['params'];validate_params(action,params)
        def progress(message,**metadata):
            with self.app.lock:job['progress']=message
            # Full model metadata remains in the bounded status cache; never per-tick logs.
            if 'runner' in metadata:
                with self.app.lock:job['runner_progress']=metadata['runner']
        try:
            if action=='precision_release':return self.io.release(progress)
            precision,step,task=params['precision'],params['step'],params['task']
            if action=='precision_load':return self.io.load(precision,step,progress)
            # Recheck live model identity immediately; CLI owns actual USB/lease/feedback gates.
            models=self.io.inspect_models();row=models[precision]
            require(row.get('ready') and row.get('step')==step and row.get('busy') is False and row.get('lease_active') is False,
                    '实际加载精度/检查点或租约已改变')
            if action in V2_ACTIONS:
                path_proof=self.io.prepare_model_path(precision,step,progress)
                with self.app.lock:job['model_path_preflight']=path_proof
                manual=params.get('operator_skip_no_motion_acceptance') is True
                if manual:
                    profile=self.refresh_v2()
                    require(profile.get('fresh') is True and profile.get('ready') is True and not profile.get('error')
                            and profile.get('manual_run_without_nosend_supported') is True,'当前V2人工直接运行支持状态已改变')
                try:return self.v2.run(step,task,deploy=action=='v2_deploy',progress=progress,
                    duration=params.get('duration',V2_ACCEPTANCE_S),operator_skip_no_motion_acceptance=manual)
                except V2RunError as exc:
                    with self.app.lock:
                        job['result']={'route':'V2_FP32','precision':'FP32','step':step,'task':task,
                            'status':'FAILED_RETAINED','duration_s':params.get('duration',V2_ACCEPTANCE_S),
                            'result':deepcopy(exc.receipt),'report':exc.receipt.get('report') or exc.receipt.get('run_dir'),
                            'physical_task_success':'NOT_VERIFIED' if action=='v2_deploy' else 'NOT_RUN'}
                    raise
                finally:self.refresh_v2()
            return self.io.run_robot(precision,step,task,deploy=action=='precision_deploy',progress=progress,duration=params.get('duration',30.))
        finally:self.refresh(force_robot=True)
    def cancel(self):
        # Closing our V2 pipe must happen even if the original IO cancel fails.
        try:return self.io.cancel()
        finally:self.v2.cancel()
    def close(self):self.quit.set();return self.cancel()
