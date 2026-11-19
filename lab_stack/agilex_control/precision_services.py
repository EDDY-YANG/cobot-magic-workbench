"""Pinned model lifecycle and isolated robot runner for the existing 4090 web host.

Import is inert. No SDK, camera, inference backend or package installation lives
here. Model changes use their original, identity-checking control programs.
"""
from contextlib import contextmanager
from copy import deepcopy
import errno,hashlib,importlib.util,json,os,queue,shlex,socket,subprocess,sys,threading,time,urllib.error,urllib.request,uuid
from pathlib import Path

BASE=Path('/opt/cobot-workbench/lab-packages')
FP=BASE/'2026-10-19-18-12-H50-Model-Supervisor'
FP_PY='/home/robot/openpi-workspace/pi05-215abfb217db/env/bin/python'
BF=BASE/'2026-10-21-19-38-BF16-Smoothing-Live/isolated-service'
BF_PY=str(BF.parent/'environment/bin/python')
ROBOT_ROOT='/media/robot/RECORDING_USB/2026-10-06-14-05-AGILEX-UNIFIED-CONTROL'
ROBOT_RUNNER='/media/robot/RECORDING_USB/2026-10-26-13-55-Joint-Repair-Shared-Runner'
ROBOT_PY='/media/robot/RECORDING_USB/2026-10-21-20-04-BF16-Smoothing-Environment/environment/bin/python'
ROBOT_ORIGINAL_PY='/home/agliex/miniconda3/envs/tiger-lerobot-piper-20261005/bin/python'
STEPS=(5000,10000,15000,20000,2000)
PINS={'model_supervisor.py':'5b735103daa14a2a065db3ea7b361b844301ed7926ddd704cab55352bd9b7cb8',
 'launch_supervisor.py':'17c10a33491f765e0ccd5a04fc976ae8f026290df0873611db54975baf8e4882',
 'supervisor_control.py':'50d8cb5214216bb38e8df6b39abc501605b2e1c6bb5902b2f4548124255396c9'}

class PrecisionError(RuntimeError):pass
def require(ok,message):
    if not ok:raise PrecisionError(message)
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def parsed_output(text):
    for line in reversed(text.splitlines()):
        try:value=json.loads(line)
        except (ValueError,TypeError):continue
        if isinstance(value,dict):return value
    try:
        value=json.loads(text)
        if isinstance(value,dict):return value
    except ValueError:pass
    raise PrecisionError('进程未返回可核对的JSON回执；保留原始结果，不自动重试')
def http(port,path,method='GET',body=None,timeout=1.2,absent_ok=False):
    require(port in (18882,18960),'只允许固定模型回环端口')
    data=None if body is None else json.dumps(body,allow_nan=False).encode()
    req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=data,method=method,
        headers={'Content-Type':'application/json','Connection':'close'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as reply:
            raw=reply.read(4*1024*1024+1)
    except urllib.error.HTTPError as exc:
        raw=exc.read(65536)
        raise PrecisionError('模型接口HTTP '+str(exc.code)+': '+raw.decode(errors='replace')[:300]) from exc
    except urllib.error.URLError as exc:
        if absent_ok and isinstance(exc.reason,OSError) and exc.reason.errno==errno.ECONNREFUSED:return None
        raise PrecisionError('模型接口不可确认；不当作模型已退出') from exc
    require(len(raw)<=4*1024*1024,'模型元数据超过上限')
    value=json.loads(raw);require(isinstance(value,dict),'模型接口需要JSON对象');return value
def proc_matches(identity):
    if not isinstance(identity,dict) or type(identity.get('pid')) is not int:return False
    p=Path('/proc')/str(identity['pid'])
    try:
        fields=(p/'stat').read_text().rsplit(') ',1)[1].split()
        if fields[0] in ('Z','X'):return False
        return (str(identity.get('start_ticks'))==fields[19] and
          identity.get('boot_id')==Path('/proc/sys/kernel/random/boot_id').read_text().strip() and
          identity.get('cmdline_sha256')==sha(p/'cmdline'))
    except FileNotFoundError:return False

def exited_identity(identity,stat_text=None,boot=None):
    """A matching zombie has exited but /proc may outlive its CUDA allocation."""
    if not isinstance(identity,dict) or type(identity.get('pid')) is not int:return False
    try:
        if stat_text is None:stat_text=(Path('/proc')/str(identity['pid'])/'stat').read_text()
        if boot is None:boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        fields=stat_text.rsplit(') ',1)[1].split()
        return fields[0] in ('Z','X') and fields[19]==str(identity.get('start_ticks')) and boot==identity.get('boot_id')
    except FileNotFoundError:return True

class RealPrecisionIO:
    def __init__(self):
        self.cancelled=threading.Event();self.lock=threading.RLock();self.child=None;self.forward_children=[]
        self._fp_module=None;self._bf_module=None
    def check_environment(self):
        require(os.name=='posix' and socket.gethostname()=='user-MS-7E06','统一模型入口只在已核验4090后台运行')
        m=json.loads(subprocess.check_output(['findmnt','-J','-T',str(BASE),'-o','TARGET,UUID'],text=True,timeout=5))['filesystems']
        require(m==[{'target':'/data/4090-1tb','uuid':'e76043b6-4ab6-42a5-8b06-4963ee92a567'}],'4090模型SSD身份改变')
        for name,h in PINS.items():require(sha(FP/'scripts'/name)==h,'原FP32管理源码改变，保留当前owner：'+name)
    def modules(self):
        with self.lock:return self._modules_locked()
    def _modules_locked(self):
        self.check_environment()
        if self._fp_module is None:
            spec=importlib.util.spec_from_file_location('_precision_original_supervisor',FP/'scripts/model_supervisor.py')
            self._fp_module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self._fp_module;spec.loader.exec_module(self._fp_module)
        if self._bf_module is None:
            p=BF/'service/contract.py'
            require(sha(p)=='085d8afff9dc51a39d20d7eeaec412c0f28f26b6956254eba1e03d5a19cfa1a7','独立BF16合同源码改变')
            spec=importlib.util.spec_from_file_location('_precision_independent_bf_contract',p)
            self._bf_module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self._bf_module;spec.loader.exec_module(self._bf_module)
        return self._fp_module,self._bf_module
    def catalog(self):return self.modules()[0].ProductionBackend().catalog()
    def _bf_launch(self,status):
        candidates=[]
        for path in sorted((BF/'runs').glob('*/controller-launch.json'),reverse=True)[:64]:
            v=json.loads(path.read_text())
            if status is not None and v.get('nonce')!=status.get('service_nonce'):continue
            if proc_matches(v.get('identity')):
                require(v.get('run_dir')==str(path.parent) and v.get('entrypoint')==str(BF/'service/server.py'),'BF16运行身份与目录不同')
                candidates.append(v)
        require(len(candidates)<=1,'发现多个BF16服务owner，不做自动切换')
        if status is not None:require(len(candidates)==1,'BF16端口没有匹配的生命周期回执')
        return candidates[0] if candidates else None
    def inspect_models(self):
        fp,bf=self.modules();result={}
        status=http(18882,'/status',absent_ok=True)
        path=FP/'receipts/manager-instance.json';identity=json.loads(path.read_text()) if path.exists() else None
        fp_alive=proc_matches(identity)
        if status is not None:
            require(fp_alive and status.get('schema')=='tiger-fp32-model-supervisor-v1','FP32端口owner未核实')
        elif fp_alive:status={'status':'STARTING','busy':True,'lease':{'active':False}}
        result['FP32']={'present':status is not None,'ready':False,'precision':'FP32','step':None,
            'status':'STOPPED' if status is None else status.get('status'),'identity':identity if fp_alive else None,
            'busy':False if status is None else status.get('busy'),'lease_active':False if status is None else status.get('lease',{}).get('active'),
            'acceptance_passed':False}
        if status is not None and status.get('status')=='READY':
            step=status.get('selected_step');health=fp.health_validated(http(18882,'/health'),step)
            acceptance=http(18882,'/acceptance?step='+str(step))
            result['FP32'].update(ready=True,step=step,health=health,fingerprint=health['contract_fingerprint'],
                acceptance_passed=acceptance.get('status')=='PASS',acceptance=acceptance)
        status=http(18960,'/status',absent_ok=True);launch=self._bf_launch(status)
        result['BF16']={'present':launch is not None,'ready':False,'precision':'BF16','step':None,
            'status':status.get('status') if status else 'LOADING' if launch else 'STOPPED',
            'busy':status.get('busy') if status else bool(launch),
            'lease_active':status.get('lease',{}).get('active') if status else False,
            'run_dir':launch['run_dir'] if launch else None,'identity':launch.get('identity') if launch else None,
            'acceptance_passed':False}
        if status and status.get('status')=='READY':
            step=status.get('selected_step');health=bf.validate_health(http(18960,'/health'),step)
            require(health.get('service_nonce')==launch['nonce'],'BF16服务实例在读取期间改变')
            result['BF16'].update(ready=True,step=step,health=health,
                fingerprint=digest({k:health[k] for k in ('contract','policy','rtc','acceptance_binding')}),
                service_fingerprint=health['contract_fingerprint'],nonce=launch['nonce'])
        for row in result.values():
            # Manager busy may cover inference only. Loading survives a cancelled
            # web job and must still block another select operation.
            row['transitioning']=bool(row['present'] and str(row.get('status','')).upper()
                in ('STARTING','LOADING','SELECTING','STOPPING','UNLOADING','SWITCHING'))
            if row['transitioning']:row['busy']=True
            row['readiness_scope']='MODEL_SERVICE_ONLY_NOT_ROBOT_TRANSPORT'
        require(not(result['FP32']['present'] and result['BF16']['present']),'两条模型路线同时存在，先检查owner；不自动停止任何一方')
        return result
    def _ssh(self,argv):
        return ['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=6',
                '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=3','tiger-agilex',shlex.join(argv)]
    def robot_command(self,action,step=None,task=None,confirm=False,duration=None):
        argv=[ROBOT_PY,'-I','-B',ROBOT_RUNNER+'/runtime/web_entry.py',action]
        if step is not None:argv+=['--step',str(step)]
        if task is not None:argv+=['--task',task]
        if duration is not None:argv+=['--duration',str(duration)]
        if confirm:argv+=['--leader-isolated','--onsite-confirmed','--current-start-confirmed']
        return self._ssh(argv)
    def robot_metadata(self):
        p=subprocess.run(self.robot_command('inspect'),capture_output=True,text=True,timeout=12)
        require(p.returncode==0,'独立机器人运行器尚未接线：'+(p.stderr or p.stdout)[-600:])
        return parsed_output(p.stdout)
    @contextmanager
    def switch_guard(self):
        p=subprocess.Popen(self.robot_command('guard'),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
        ready=queue.Queue()
        threading.Thread(target=lambda:ready.put(p.stdout.readline()),daemon=True).start()
        try:
            line=ready.get(timeout=12);receipt=json.loads(line)
            require(receipt.get('status')=='ROBOT_MODEL_SWITCH_LOCKS_HELD','机器人加载预检未通过：'+str(receipt.get('error') or receipt.get('status') or '没有锁回执'))
            yield lambda:require(p.poll() is None and not self.cancelled.is_set(),'模型交接已取消或机器人锁owner退出')
        finally:
            if p.stdin and not p.stdin.closed:p.stdin.close()
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:raise PrecisionError('交接锁释放未确认，不自动重试模型切换')
    def local(self,argv,timeout=45):
        p=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
        require(p.returncode==0,'模型生命周期操作失败：'+(p.stderr or p.stdout)[-1200:])
        return parsed_output(p.stdout)
    def launch_detached(self,argv):
        """Keep model children outside the web service's systemd control group.

        Original model controllers still create/validate/stop their exact PID.
        The transient unit only owns their launch and survives a page restart.
        """
        self.check_environment()
        root=BASE/'2026-10-21-22-05-Web-Precision-Control';root.mkdir(exist_ok=True)
        nonce=uuid.uuid4().hex[:16];unit='tiger-precision-model-'+nonce
        log=root/(time.strftime('%Y-%m-%d-%H-%M-%S')+'-'+nonce+'-launch.log')
        with log.open('x',encoding='utf8') as handle:handle.write('')
        cmd=['systemd-run','--user','--quiet','--unit='+unit,'--property=Type=oneshot',
            '--property=RemainAfterExit=yes','--property=TimeoutStartSec=60',
            '--property=StandardOutput=append:'+str(log),'--property=StandardError=append:'+str(log),'--',*argv]
        subprocess.run(cmd,check=True,capture_output=True,text=True,timeout=8)
        end=time.monotonic()+65
        while time.monotonic()<end:
            p=subprocess.run(['systemctl','--user','show',unit,'-p','ActiveState','-p','SubState','-p','ExecMainStatus'],
                capture_output=True,text=True,check=True,timeout=5)
            state=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
            if state.get('ActiveState')=='active' and state.get('SubState')=='exited':
                require(state.get('ExecMainStatus')=='0','模型启动器退出码异常')
                require(log.stat().st_size<=1024*1024,'启动回执超过上限')
                return parsed_output(log.read_text())
            require(state.get('ActiveState') not in ('failed','inactive'),'模型启动器失败：'+log.read_text()[-2000:])
            time.sleep(.2)
        raise PrecisionError('独立启动单元未完成；保留实际进程及回执 '+str(log))
    def _wait(self,predicate,guard,progress,seconds=300):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            guard();models=self.inspect_models();value=predicate(models)
            if value:return models
            for row in models.values():require(row['status']!='FAILED','模型加载失败，保留原失败回执')
            progress('等待模型实际就绪',models=models);self.cancelled.wait(.5)
        raise PrecisionError('模型操作超过等待预算；实际进程状态保留，不自动重启')
    def stop_model(self,precision,models,guard,progress):
        row=models[precision]
        if not row['present']:return
        require(row['busy'] is False and row['lease_active'] is False,'模型仍在加载/推理或被租用，不能交接')
        guard();progress('正在释放 '+precision+' 模型显存')
        if precision=='BF16':
            out=self.local([BF_PY,'-I','-B',str(BF/'service/control.py'),'stop','--config',str(BF/'service_config.json'),
                '--run-dir',row['run_dir'],'--execute-online'],timeout=40)
            require(out.get('status')=='BF16_SERVICE_CLOSED_CONFIRMED','BF16停止或GPU释放未确认')
        else:
            self.local([FP_PY,'-I','-B',str(FP/'scripts/supervisor_control.py'),'stop'])
        self._wait(lambda m:not m[precision]['present'],guard,progress,seconds=40)
        self.wait_gpu_release([r for r in (row.get('identity'),row.get('health',{}).get('manager',{}).get('worker')) if isinstance(r,dict) and r.get('pid')],guard,progress)
    def wait_gpu_release(self,identities,guard,progress):
        # CUDA process teardown can lag /proc disappearance. Never remove or
        # disguise an owner from the original launcher's inventory.
        path=FP/'scripts/gpu_inventory.py'
        require(sha(path)=='b4a46e34ef9db999b41cd50209ef14398f16ecc5e5149271b84a32313152a6d9','GPU inventory source changed')
        spec=importlib.util.spec_from_file_location('_precision_gpu_release_inventory',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        end=time.monotonic()+25.
        while True:
            guard();value=module.inventory()
            if value.get('new_model_allowed') is True:return
            pending=value.get('unknown_compute_owners',[])
            # Only wait for the exact exited owned PID. Other owners remain
            # blocked and untouched, including an inaccessible/reused identity.
            require(pending and all(str((r.get('query_row') or [''])[0]).strip() in {str(i['pid']) for i in identities} for r in pending)
                and all(exited_identity(i) for i in identities),'GPU还有其他/未退出owner，不自动接管：'+json.dumps(pending,ensure_ascii=False)[:1000])
            require(time.monotonic()<end,'旧模型已退出但驱动仍未完成显存释放；保留状态')
            progress('旧模型已退出，等待GPU驱动实际释放显存');self.cancelled.wait(.5)
    def ensure_forward(self,precision,guard):
        port,destination=(18883,18882) if precision=='FP32' else (18961,18960)
        probe=self.local(self.robot_command('ports'),timeout=12)
        if port not in probe.get('listening_ports',[]):
            guard()
            command=['systemd-run','--user','--quiet','--collect','--unit=tiger-precision-forward-'+str(port)+'-'+uuid.uuid4().hex[:8],
                '--property=Type=exec','--','ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ExitOnForwardFailure=yes',
                '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=3','-NT','-R',f'127.0.0.1:{port}:127.0.0.1:{destination}','tiger-agilex']
            p=subprocess.run(command,capture_output=True,text=True,timeout=12)
            require(p.returncode==0,'独立回环转发建立失败：'+p.stderr[-500:])
            # Verification below observes the real SSH listener and model health.
            for _ in range(20):
                guard()
                if port in self.local(self.robot_command('ports'),timeout=12).get('listening_ports',[]):break
                time.sleep(.1)
        seen=self.local(self.robot_command('endpoints'),timeout=12)
        local=http(destination,'/health')
        remote=seen.get(precision)
        require(isinstance(remote,dict) and remote.get('contract_fingerprint')==local.get('contract_fingerprint') and
            remote.get('policy',{}).get('checkpoint_step')==local.get('policy',{}).get('checkpoint_step'),
            '机器人模型通路未就绪或身份不同：AgileX 127.0.0.1:'+str(port)+' → 4090 127.0.0.1:'+str(destination)+'；保留未知转发，不重载模型')
        if precision=='FP32':
            require(remote.get('manager',{}).get('worker')==local.get('manager',{}).get('worker'),
                    '机器人转发的FP32 worker与4090当前实例不同；不覆盖未知端口')
        else:
            require(remote.get('service_nonce')==local.get('service_nonce'),
                    '机器人转发的BF16服务实例不同；不覆盖未知端口')
        return {'status':'ROBOT_MODEL_PATH_VERIFIED','precision':precision,'robot_port':port,
                'server_port':destination,'step':local.get('policy',{}).get('checkpoint_step'),
                'contract_fingerprint':local.get('contract_fingerprint'),'model_reload_performed':False}
    def prepare_model_path(self,precision,step,progress):
        """Reconcile the robot path before a new run, even after cancelled loading.

        Does not load/select a model or retry inference. A mismatched occupied
        port is rejected; only an absent reverse-forward listener is created.
        """
        self.cancelled.clear();self.check_environment()
        with self.switch_guard() as guard:
            before=self.inspect_models()[precision]
            require(before.get('ready') and before.get('step')==step and
                    before.get('busy') is False and before.get('lease_active') is False,
                    '当前模型仍在加载/使用或检查点已改变；不自动切换')
            progress('模型已加载，正在核对AgileX到4090的模型通路')
            proof=self.ensure_forward(precision,guard)
            guard();after=self.inspect_models()[precision]
            require(after.get('ready') and after.get('step')==step and
                    after.get('fingerprint')==before.get('fingerprint') and
                    after.get('identity')==before.get('identity') and
                    after.get('busy') is False and after.get('lease_active') is False,
                    '模型通路检查期间实例或租约改变；本次运行未开始')
            if precision=='FP32':
                require(after.get('health',{}).get('manager',{}).get('worker')==before.get('health',{}).get('manager',{}).get('worker'),
                        '模型通路检查期间FP32 worker改变；本次运行未开始')
            progress('模型通路已核对，开始所选验收或运行')
            return proof
    def load(self,precision,step,progress):
        self.cancelled.clear();self.check_environment()
        require(any(x['step']==step and x.get('available') for x in self.catalog()),'所选检查点权重/元数据未通过核验')
        with self.switch_guard() as guard:
            models=self.inspect_models()
            for row in models.values():require(not row['present'] or row['busy'] is False and row['lease_active'] is False,'当前模型仍被占用')
            opposite='BF16' if precision=='FP32' else 'FP32'
            self.stop_model(opposite,models,guard,progress);models=self.inspect_models();row=models[precision]
            if row['ready'] and row['step']==step:
                self.ensure_forward(precision,guard);return {'status':'MODEL_READY_REUSED','precision':precision,'step':step,'model':row,'robot_motion_started':False}
            if precision=='BF16':
                self.stop_model('BF16',models,guard,progress);guard();progress('正在加载 BF16 step'+str(step))
                self.launch_detached([BF_PY,'-I','-B',str(BF/'service/control.py'),'start','--config',str(BF/'service_config.json'),
                    '--step',str(step),'--execute-online'])
            elif row['present']:
                guard();http(18882,'/select','POST',{'step':step},timeout=3)
            else:
                guard();progress('正在加载原 FP32 step'+str(step))
                self.launch_detached([FP_PY,'-I','-B',str(FP/'scripts/launch_supervisor.py'),'--step',str(step)])
            models=self._wait(lambda m:m[precision]['ready'] and m[precision]['step']==step,guard,progress)
            self.ensure_forward(precision,guard)
            return {'status':'MODEL_READY','precision':precision,'step':step,'model':models[precision],'robot_motion_started':False}
    def release(self,progress):
        self.cancelled.clear()
        with self.switch_guard() as guard:
            models=self.inspect_models()
            for precision in ('FP32','BF16'):self.stop_model(precision,models,guard,progress)
        return {'status':'MODEL_SERVICES_RELEASED','robot_motion_started':False}
    def run_robot(self,precision,step,task,*,deploy,progress,duration=30.):
        self.prepare_model_path(precision,step,progress)
        if precision=='BF16':command=self.robot_command('deploy' if deploy else 'acceptance',step,task,confirm=deploy,duration=duration if deploy else None)
        else:
            command=self.robot_command('fp32-deploy' if deploy else 'fp32-acceptance',step,task,confirm=deploy,duration=duration if deploy else None)
        with self.lock:
            require(self.child is None,'本网页已有运行中的模型任务')
            p=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
            self.child=p
        output='';last=None
        try:
            progress(f'正在运行 {duration:g} 秒部署' if deploy else '真实NO_SEND验收中：3次预热＋100次；不会发送运动目标')
            for line in p.stdout:
                output=(output+line)[-65536:]
                try:value=json.loads(line)
                except ValueError:continue
                if isinstance(value,dict):last=value;progress('执行中，等待真实结束及USB保存审计',runner=value)
            code=p.wait();receipt=last or parsed_output(output)
            require(code==0,'机器人运行未成功完成：'+str(receipt.get('error') or output[-1500:]))
            if precision=='BF16':
                expected='BF16_SMOOTHED_WINDOW_COMPLETED_AND_SAVED' if deploy else 'NO_SEND_PROFILE_VALIDATED'
                require(receipt.get('status')==expected,'BF16真实回执没有通过：'+str(receipt.get('status')))
            return {'precision':precision,'step':step,'status':'DEPLOYMENT_COMPLETED' if deploy else 'NO_SEND_COMPLETED',
                'task':task if deploy or precision=='BF16' else '右臂将红色方块放入绿色桶','duration_s':duration if deploy else None,'report':receipt.get('report'),'result':receipt,'physical_task_success':'REQUIRES_OPERATOR_REVIEW' if deploy else 'NOT_RUN'}
        finally:
            if p.stdin and not p.stdin.closed:p.stdin.close()
            with self.lock:self.child=None
    def cancel(self):
        self.cancelled.set()
        with self.lock:
            if self.child is not None and self.child.poll() is None and self.child.stdin and not self.child.stdin.closed:
                self.child.stdin.close()
        return {'cancel_requested':True,'hardware_estop':False,'automatic_return_to_zero':False}
