"""DAgger preparation for the two OEM shared CAN branches. No CAN transmitter."""
import math
import time

from agilex_control.web_backend import ValidationError, WebApp

ROUTES = {'precision_deploy', 'v2_deploy'}
ACTIVE = {'queued', 'running', 'unknown'}


def numbers(values, count):
    return (isinstance(values, (list, tuple)) and len(values) == count
            and all(type(x) in (int, float) and math.isfinite(x) for x in values))


def arm_readback(arm, online):
    result = dict(q=None, g=None, status='无实时反馈')
    if not online or not arm.get('feedback_fresh') or not arm.get('identity', {}).get('identity_verified'):
        return result
    target = arm.get('action_targets', {})
    def fresh(values, count):
        return numbers(values, count) and all(0 <= x <= .2 for x in values)
    ages = target.get('pair_ages_s')
    if (fresh(ages, 3) and max(ages)-min(ages) <= .05
            and fresh([arm.get('age_joint_s')], 1)
            and numbers(target.get('q_deg'), 6) and numbers(arm.get('q_deg'), 6)):
        result['q'] = max(abs(a-b) for a,b in zip(target['q_deg'], arm['q_deg']))
    if (fresh([arm.get('age_gripper_s'), target.get('grip_age_s')], 2)
            and numbers([target.get('grip_m'), arm.get('grip_m')], 2)):
        result['g'] = 1000 * abs(target['grip_m'] - arm['grip_m'])
    result['status'] = '目标或反馈过期'
    if result['q'] is not None and result['g'] is not None:
        result['status'] = '总线目标接近' if result['q'] <= 2 and result['g'] <= 2 else '角度或开度有偏差'
    return result


class DaggerWorkbench(WebApp):
    def __init__(self, *args, **kwargs):
        self.intervention = None
        super().__init__(*args, **kwargs)

    def snapshot(self):
        with self.lock:
            state = super().snapshot()
            state['dagger'] = self.dagger_state(state)
            return state

    def _owned_runner(self, job):
        if not job or self.precision is None:
            return None
        return self.precision.v2 if job['action'] == 'v2_deploy' else self.precision.io

    def _running(self):
        active = [j for j in self.jobs if j['state'] in ACTIVE]
        if len(active) == 1 and active[0]['action'] in ROUTES and active[0]['state'] == 'running':
            return active[0]
        return None

    def dagger_state(self, state):
        job = self._running()
        runner = self._owned_runner(job)
        ready = False
        if runner:
            with runner.lock:
                p = runner.child
                ready = bool(p and p.poll() is None and p.stdin and not p.stdin.closed)
        session = self.intervention
        ended = session and session.get('ended') is not None
        in_progress = bool(session and not ended)
        online = state.get('connected') is True and state.get('cache_residence_s', 999) <= .5
        status = state.get('hub', {})
        arms = {side: arm_readback(status.get('arms', {}).get(side, {}), online) for side in ('left', 'right')}
        view = dict(arms=arms, phase='待介入', policy='正在执行' if job else '无可接管任务',
                    record='未录制人工片段', elapsed_s=None, can_enter=ready and not in_progress,
                    can_finish=False, binding=job['id'] if job else None,
                    note='在模型部署页启动一轮任务后，可以从此处介入。')
        if not online:
            view.update(phase='机器人离线', note='AgileX 实时状态不可用。等待机器人连接恢复。')
        elif ready:
            view['note']='点击介入后，会终止本轮模型新目标并等待机器人执行器收尾。'
        capture = status.get('recording', {})
        if in_progress and online and capture.get('dagger_job_id') == session['job_id']:
            roles = capture.get('capture_contract', {}).get('required_arm_roles', ())
            path = capture.get('path')
            if (set(roles) == {'left', 'right'} and isinstance(path, str) and path
                    and capture.get('owned_stop_protocol') == 'tiger-owned-recording-stop-v1'
                    and capture.get('active') is True and not session.get('episode')):
                session['episode'] = path
        if in_progress and session.get('save_job'):
            saving = next((j for j in self.jobs if j['id'] == session['save_job']), {})
            if saving.get('state') == 'succeeded':
                payload = saving.get('result') or {}
                final = (payload.get('result') or payload).get('finalization') or {}
                if (final.get('healthy') is True and final.get('completed') is True
                        and final.get('owned_path') == session.get('episode')):
                    session['saved'] = True
                    session['ended'] = time.monotonic()
                    in_progress, ended = False, True
        if ended and not job:
            view.update(phase='本次介入已结束', elapsed_s=round(session['ended']-session['started'],2),
                        note='本轮没有取得双主臂接管凭据，未生成可保存的人工纠正片段。')
        if in_progress:
            source_job = next((j for j in self.jobs if j['id'] == session['job_id']), None)
            phase = '等待模型退出'
            reason = '已向本轮运行器请求停止。等待执行器退出与原有录制收尾。'
            if not online or not source_job or source_job['state'] == 'unknown':
                phase, reason = '交接状态待核实', '机器人或执行回执不可用，不能确认控制权已交出。'
            elif source_job['state'] not in ACTIVE:
                phase = '等待现场交接'
                reason = ('原厂主从共线，无法独立读取两条主臂姿态或阻断主臂直发。'
                          '目前无法确认双臂自动接管。')
                roles = status.get('recording', {}).get('capture_contract', {}).get('required_arm_roles', [])
                if set(roles) != {'left','right'}:
                    reason += ' 当前录制范围未覆盖双臂。'
            view.update(phase=phase, policy='已请求停止' if phase != '等待现场交接' else '本轮运行器已退出',
                        note=reason, can_enter=False, can_finish=True, binding=session['job_id'],
                        elapsed_s=round(time.monotonic()-session['started'],2))
        if session and session.get('saved') and not job:
            view.update(phase='人工片段已保存', record='已写入 U 盘',
                        note='已核验本轮双臂片段的保存回执。'+session['episode'])
        elif in_progress and session.get('save_job'):
            finish_job = next((j for j in self.jobs if j['id'] == session['save_job']), {})
            failed = finish_job.get('state') in {'blocked','failed','unknown','succeeded'} or not finish_job
            view.update(phase='保存结果待核实' if failed else '正在保存', record='核对原生录制回执',
                        note=finish_job.get('error') or '等待本轮 U 盘片段完成收尾并通过完整性检查。',
                        can_finish=False)
        elif in_progress and session.get('episode'):
            matched = online and capture.get('path') == session['episode'] and capture.get('dagger_job_id') == session['job_id']
            view.update(phase='纠正采集' if matched else '录制归属待核实',
                        record='正在录制' if matched and capture.get('active') is True else '待核实',
                        note='本轮双臂片段：'+session['episode'], can_finish=bool(matched and capture.get('active')))
        return view

    def submit(self, action, params):
        with self.lock:
            session = self.intervention
            ongoing = bool(session and session.get('ended') is None)
            if action not in ('dagger_intervene', 'dagger_finish'):
                if ongoing and action not in ('status','record_list','collection_list'):
                    raise ValidationError('DAgger 正在交接，请先结束本轮介入')
                return super().submit(action, params)
            if not isinstance(params, dict) or set(params) != {'binding'} or not isinstance(params['binding'], str):
                raise ValidationError('需要本轮执行任务标识')
            if action == 'dagger_finish':
                if not session or params['binding'] != session['job_id']:
                    raise ValidationError('本次介入已改变')
                if not ongoing:
                    return self.snapshot()['dagger']
                view = self.snapshot()['dagger']
                if session.get('save_job'):
                    return view
                if session.get('episode'):
                    if not view['can_finish']:
                        raise ValidationError('录制身份已改变，不能停止其他片段')
                    saved = super().submit('record_stop', {'episode': session['episode']})
                    session['save_job'] = saved['id']
                else:
                    session['ended'] = time.monotonic()
                return self.snapshot()['dagger']
            if ongoing:
                if session['job_id'] == params['binding']:
                    return self.snapshot()['dagger']
                raise ValidationError('另一次介入进行中')
            job = self._running()
            if not job or job['id'] != params['binding']:
                raise ValidationError('模型任务已更换或已结束')
            runner = self._owned_runner(job)
            if runner is None:
                raise ValidationError('模型运行器不可用')
            with runner.lock:
                child = runner.child
                if child is None or child.poll() is not None or not child.stdin or child.stdin.closed:
                    raise ValidationError('该任务没有可暂停的运行器')
                self.intervention = dict(started=time.monotonic(), job_id=job['id'], ended=None)
                # The original runners consume EOF as cancellation. They own robot
                # cleanup, queue exit and stop_owned_recording for the model episode.
                if job['action'] == 'v2_deploy':
                    runner.speed.close('DAgger 介入已请求')
                child.stdin.close()
            self._event('DAgger 已向本轮机器人运行器请求停止新模型目标')
            return self.snapshot()['dagger']
