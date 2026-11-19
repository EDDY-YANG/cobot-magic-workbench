"""Bounded web adapter for the independent V2 runner. Import has no IO.

This module owns only its SSH child. Models retain their existing lifecycle;
closing the child's stdin requests the runner's recorded stop/cleanup path.
"""
import base64
import json
import math
import os
import subprocess
import threading

from .precision_services import RealPrecisionIO, PrecisionError, STEPS, parsed_output, require
from .v2_manual_speed import ManualSpeedChannel

V2_ROOT = '/media/robot/RECORDING_USB/2026-10-26-13-55-V2-JOINT-BOUNDARY-REPAIR'
V2_PYTHON = V2_ROOT + '/environment/bin/python'
V2_WINDOW_S = 40
V2_ACCEPTANCE_S = 30
V2_MIN_WINDOW_S = 1
V2_MAX_WINDOW_S = 1800


def validate_v2_duration(duration):
    require(type(duration) in (int, float) and math.isfinite(duration)
            and V2_MIN_WINDOW_S <= duration <= V2_MAX_WINDOW_S,
            '独立V2运行时长必须为1至1800秒')
    return duration


def v2_window_contract_matches(profile):
    contract = profile.get('window_contract') or {}
    expected = {'min_duration_s': V2_MIN_WINDOW_S, 'max_duration_s': V2_MAX_WINDOW_S,
                'default_duration_s': V2_WINDOW_S, 'acceptance_duration_s': V2_ACCEPTANCE_S}
    return isinstance(contract, dict) and all(type(contract.get(key)) in (int, float)
                                            and contract[key] == value for key, value in expected.items())


class V2RunError(PrecisionError):
    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt = receipt


class RealV2IO:
    def __init__(self, ssh=None):
        self._ssh = ssh or RealPrecisionIO()._ssh
        self.lock = threading.RLock()
        self.child = None
        self.speed = ManualSpeedChannel()
        self.backend_cleanup_error = None

    def command(self, action, *, step=None, task=None, duration=None, operator_skip_no_motion_acceptance=False):
        require(action in ('inspect', 'acceptance', 'run', 'stop'), '未知独立V2操作')
        require(type(operator_skip_no_motion_acceptance) is bool and
                (not operator_skip_no_motion_acceptance or action == 'run'), '人工跳过NO_SEND仅用于明确运行请求')
        argv = [V2_PYTHON, '-I', '-B', V2_ROOT + '/scripts/web_bridge.py', action]
        if action in ('acceptance', 'run'):
            require(type(step) is int and step in STEPS, 'V2检查点必须为已登记候选')
            from .h50_prompt import validate_prompt
            validate_prompt(task)
            require(not any(ord(c) < 32 or ord(c) == 127 for c in task), 'Prompt必须为单行文本')
            if action == 'run':
                validate_v2_duration(duration)
            else:
                require(type(duration) in (int, float) and math.isfinite(duration) and duration == V2_ACCEPTANCE_S,
                        'V2无运动验收窗口必须为30秒')
            encoded = base64.b64encode(task.encode('utf-8')).decode('ascii')
            argv += ['--step', str(step), '--prompt-base64', encoded, '--duration', str(duration), '--execute-online']
            if action == 'run':
                argv += ['--authorize-motion', '--leader-isolated', '--onsite-confirmed', '--current-start-confirmed']
                if operator_skip_no_motion_acceptance:
                    argv += ['--operator-skip-no-motion-acceptance']
        elif action == 'stop':
            argv += ['--execute-online']
        return self._ssh(argv)

    def metadata(self):
        p = subprocess.run(self.command('inspect'), capture_output=True, text=True, timeout=12)
        require(p.returncode == 0, '独立V2状态尚未核实：' + (p.stderr or p.stdout)[-600:])
        result = parsed_output(p.stdout)
        require(type(result.get('ready')) is bool and v2_window_contract_matches(result),
                '独立V2状态接口或1至1800秒窗口合同不匹配')
        return result

    def run(self, step, task, *, deploy, progress, duration=V2_ACCEPTANCE_S, operator_skip_no_motion_acceptance=False):
        command = self.command('run' if deploy else 'acceptance', step=step, task=task,
                               duration=duration if deploy else V2_ACCEPTANCE_S,
                               operator_skip_no_motion_acceptance=operator_skip_no_motion_acceptance)
        with self.lock:
            if self.child is not None and self.child.poll() is not None:
                self.child = None
            require(self.child is None, '本网页已有独立V2任务')
            p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
            self.child = p
            self.speed.begin(deploy)
            self.backend_cleanup_error = None
            if deploy:
                try:
                    # One short atomic JSON line, no TextIO buffering. A full or
                    # stalled pipe must never block the shared web/STOP locks.
                    os.set_blocking(p.stdin.fileno(), False)
                except (AttributeError, OSError, ValueError):
                    self.speed.close('当前宿主不支持非阻塞调速管道；原运行与停止入口保持')
        output, last, receipt = '', None, None
        try:
            progress(f'独立V2：{duration:g}秒运动窗口执行中' if deploy else '独立V2：连续30秒真实NO_SEND，无运动目标发送')
            try:
                for line in p.stdout:
                    output = (output + line)[-65536:]
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        with self.lock:
                            speed_event = self.speed.consume(value)
                        if speed_event:
                            continue
                        last = value
                        progress('独立V2正在执行，等待最终回执与USB保存', runner=value)
            except Exception:
                with self.lock:
                    self.speed.close('V2输出读取失败；运行及停止结果未确认，调速已关闭')
                raise
            with self.lock:
                self.speed.close('V2输出已关闭；等待进程退出与最终回执，调速已关闭')
            code = p.wait()
            receipt = last or parsed_output(output)
            expected = 'V2_WEB_WINDOW_COMPLETED' if deploy else 'V2_WEB_NO_SEND_VALIDATED'
            if code != 0 or receipt.get('status') != expected:
                raise V2RunError('独立V2未完成：' + str(receipt.get('error') or receipt.get('status') or output[-1000:]), receipt)
            return {'route': 'V2_FP32', 'precision': 'FP32', 'step': step, 'task': task,
                    'status': expected, 'duration_s': duration if deploy else V2_ACCEPTANCE_S,
                    'operator_skip_no_motion_acceptance': operator_skip_no_motion_acceptance,
                    'report': receipt.get('report') or receipt.get('run_dir'), 'result': receipt,
                    'physical_task_success': 'REQUIRES_OPERATOR_REVIEW' if deploy else 'NOT_RUN'}
        finally:
            close_error = None
            try:
                if p.stdin and not p.stdin.closed:
                    p.stdin.close()
            except Exception as exc:
                close_error = str(exc)
                if isinstance(receipt, dict):
                    receipt.setdefault('backend_cleanup_errors', []).append(close_error)
            finally:
                with self.lock:
                    if self.child is p:
                        # A read/close failure is not proof that the owned child
                        # exited. Keep ownership so a later STOP can still try.
                        if p.poll() is not None:
                            self.child = None
                        self.backend_cleanup_error = close_error
                        self.speed.close('V2输入关闭异常：' + close_error if close_error else 'V2运行器已退出或输出读取失败；结果见最终回执')

    def speed_snapshot(self):
        with self.lock:
            value = self.speed.snapshot()
            value['backend_cleanup_error'] = self.backend_cleanup_error
            if self.child is None or self.child.poll() is not None:
                value.update(available=False, pending=False, reason='当前V2运行器没有活动子进程')
            return value

    def speed_submit(self, data):
        with self.lock:
            require(self.child is not None and self.child.poll() is None,
                    '当前没有本后台拥有的V2活动运行器')
            stream = self.child.stdin
            require(stream is not None and not stream.closed, 'V2停止已请求，调速通道关闭')
            def write_line(value):
                payload = (json.dumps(value, ensure_ascii=True, allow_nan=False) + '\n').encode('ascii')
                self._write_pipe_line(stream, payload)
            return self.speed.submit(data, write_line)

    @staticmethod
    def _write_pipe_line(stream, payload):
        # 512 is the POSIX minimum PIPE_BUF; the validated identifiers are short.
        require(len(payload) <= 512, '调速命令超出原子管道行长度')
        count = os.write(stream.fileno(), payload)
        require(count == len(payload), '调速命令只写入部分字节，结果未确认')

    def cancel(self):
        with self.lock:
            self.speed.close('用户已请求停止，调速通道关闭')
            requested = self.child is not None and self.child.poll() is None
            if requested and self.child.stdin and not self.child.stdin.closed:
                try:
                    self.child.stdin.close()
                except Exception as exc:
                    self.backend_cleanup_error = str(exc)
                    raise
        return {'cancel_requested': requested, 'route': 'V2_FP32', 'mechanism': 'owned_runner_stdin_eof',
                'hardware_estop': False, 'automatic_return_to_zero': False,
                'stop_confirmed': False}
