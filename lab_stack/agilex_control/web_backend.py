"""PC loopback operator UI. Run the timestamped web entry, not this module.

GET only reads RAM/cache. A five-second probe reads USB metadata over pinned
SSH before connection. Explicit connect opens the persistent monitoring link;
remote control still goes through the existing CLI and its CAN ownership locks.
No shell strings or arbitrary code can be submitted by the browser.
"""
import base64
from collections import deque
import copy
import datetime as dt
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
import math
import mimetypes
from pathlib import Path
import queue
import re
import secrets
import shlex
import subprocess
import threading
import time
from urllib.parse import unquote, urlparse
import uuid
from . import pc
from .mode_state import controller_modes
from .h50_approved_contract import APPROVED_CONTRACT, require_approved_config, acceptance_semantics

WEB_ENTRY = "2026-10-06-15-57-web.py"
FP32_SHADOW_TASK = "右臂将红色方块放入绿色桶"
H50_STEPS = (5000, 10000, 15000, 20000, 2000)
H50_FROZEN = copy.deepcopy(APPROVED_CONTRACT)
LOCAL_ACTIONS = {"connect", "disconnect", "sync"}
UNAVAILABLE = {"teleop_enter": "主从角色恢复及控制源切换尚未验证；不能假报进入成功",
               "teleop_exit": "软件隔离主臂尚未验证；不能假报退出成功",
               "calibrate": "原厂设零流程尚未集成验收；当前向导只读",
               "deploy_a100": "新统一库的 A100 推理适配尚未接通；本版只提供本机模型通路"}


def stamp():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()


def fp32_shadow_health_view(health=None, error=None):
    """Server metadata + declared PC route, never an invented server/motor permit."""
    value = {'schema': 'tiger-web-fp32-shadow-v1', 'route': '4090_fp32_h50_shadow_only',
             'scope': 'PC route semantics only; server health retained separately; no physical motion authorization',
             'shadow_only': True, 'motion_authorized': False, 'module_dispatched_commands': 0,
             'api_reachable': isinstance(health, dict) and error is None,
             'ready': False, 'health_fresh': isinstance(health, dict) and error is None,
             'health_age_s': 0., 'health_stale_after_s': 3., 'health': copy.deepcopy(health)}
    try:
        if error is not None:
            raise ValueError(str(error))
        from .remote_fp32 import validate_health
        fingerprint = validate_health(health)
        value.update(ready=True, metadata_fingerprint=fingerprint,
                     contract=copy.deepcopy(health['contract']), policy=copy.deepcopy(health['policy']))
    except (ValueError, TypeError, KeyError, RuntimeError, ImportError) as exc:
        value['error'] = str(exc)
    return value


def fp32_shadow_gate(value, cfg):
    """Idle receive-only route. Never consult legacy model ready or clear STOP."""
    if (cfg or {}).get('h50_deployment', {}).get('enabled') is True:
        return '历史固定2k快捷入口已迁移；请选择上方检查点影子入口'
    settings = (cfg or {}).get('fp32_shadow', {})
    if not isinstance(settings, dict) or settings.get('enabled') is not True:
        return '4090 FP32影子路由尚未显式启用'
    if type(settings.get('health_port', 18883)) is not int or settings.get('health_port', 18883) != 18883:
        return '4090影子路由只接受已冻结的loopback端口18883'
    reason = shared_capture_idle_gate(value, cfg)
    if reason:
        return reason
    shadow = value.get('fp32_shadow', {})
    health_age = shadow.get('health_age_s')
    if (shadow.get('ready') is not True or shadow.get('api_reachable') is not True
            or shadow.get('health_fresh') is not True or type(health_age) not in (int, float)
            or not math.isfinite(health_age) or not 0 <= health_age <= 3.):
        return shadow.get('error') or '4090 FP32/H50独立服务尚未新鲜就绪'
    checked = fp32_shadow_health_view(shadow.get('health'))
    if checked.get('ready') is not True or checked.get('metadata_fingerprint') != shadow.get('metadata_fingerprint'):
        return '4090 FP32/H50服务元数据指纹未通过复核'
    return None


def shared_capture_idle_gate(value, cfg):
    """Use the existing real producer; never open another capture stream."""
    usb, hub = value.get('usb', {}), value.get('hub', {})
    if usb.get('verified') is not True or usb.get('fresh') is not True:
        return '当前USB身份或回执状态未新鲜核实'
    if hub.get('api_reachable') is not True or hub.get('schema') != 'tiger-unified-sensor-hub-v1':
        return '共享Hub尚未实际在线；影子联调复用其真实采集'
    age = value.get('state_refresh', {}).get('hub', {}).get('age_s')
    if type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age <= .5:
        return 'Hub实时状态已过期或缺少刷新依据'
    scope = capture_scope_view(value, cfg)
    if scope.get('matched') is not True or scope.get('reason'):
        return scope.get('reason') or '采集范围/硬件身份合同尚未匹配'
    for role in ('front', 'right'):
        camera = hub.get('cameras', {}).get(role, {})
        if camera.get('fresh') is not True or camera.get('thread_alive') is not True or camera.get('error'):
            return '必需真实相机尚未新鲜就绪：' + role
    arm = hub.get('arms', {}).get('right', {})
    q, grip = arm.get('q_deg'), arm.get('grip_m')
    if (arm.get('fresh') is not True or arm.get('observer_running') is not True or arm.get('observer_error')
            or not isinstance(q, list) or len(q) != 6
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in q)
            or type(grip) not in (int, float) or not math.isfinite(grip)):
        return '右臂真实关节/夹爪反馈尚未新鲜就绪'
    recording = hub.get('recording', {})
    if recording.get('active') is not False or recording.get('saving') is not False:
        return '已有录制活动或收尾，等待其完整回执'
    if (hub.get('task_state', {}).get('active') is not False
            or hub.get('recovery', {}).get('deployment_active') is not False):
        return '现有任务/执行会话活动或未知；影子路由不能接管'
    return None


def h50_rtc_view(health, step):
    """RTC capabilities come from audited service evidence, never UI config."""
    view = {'ready': False}
    try:
        rtc = health.get('rtc') if isinstance(health, dict) else None
        expected = {'schema': 'tiger-fp32-h50-rtc-v1', 'enabled': True,
                    'precision_audit_passed': True, 'freeze_steps': APPROVED_CONTRACT['rtc_freeze_steps'],
                    'previous_valid_steps': APPROVED_CONTRACT['rtc_known_steps'],
                    'native_training_hz': APPROVED_CONTRACT['native_training_hz'],
                    'execution_label_hz': APPROVED_CONTRACT['action_hz'], 'beta': APPROVED_CONTRACT['rtc_beta']}
        if not isinstance(rtc, dict):
            raise ValueError('服务未提供新H50 RTC能力与精度审计')
        for key, expected_value in expected.items():
            actual = rtc.get(key)
            if isinstance(expected_value, (bool, str)):
                matched = type(actual) is type(expected_value) and actual == expected_value
            else:
                matched = type(actual) in (int, float) and actual == expected_value
            if not matched:
                raise ValueError('服务RTC合同不符：' + key)
        for key in ('precision_audit_sha256', 'implementation_sha256'):
            if not isinstance(rtc.get(key), str) or not re.fullmatch('[0-9a-f]{64}', rtc[key]):
                raise ValueError('RTC审计/实现SHA-256证据缺失：' + key)
        from .h50_runtime import validate_health
        fingerprint = validate_health(health, step)
        public = {key: copy.deepcopy(rtc[key]) for key in (*expected, 'precision_audit_sha256', 'implementation_sha256')}
        rtc_fingerprint = hashlib.sha256(json.dumps(public, sort_keys=True, separators=(',', ':'),
                                                   allow_nan=False).encode('utf-8')).hexdigest()
        view.update(public, ready=True, metadata_fingerprint=fingerprint, rtc_fingerprint=rtc_fingerprint)
    except (ValueError, TypeError, KeyError, RuntimeError, ImportError) as exc:
        view['error'] = str(exc)
    return view


def h50_config_gate(cfg):
    try:
        require_approved_config(cfg)
    except ValueError as exc:
        return '当前H50配置与已批准异步/RTC参数不符：' + str(exc)
    return None


def h50_acceptance_gate(record, health, step):
    """Check real per-model RTC admission evidence, including its source binding."""
    try:
        if not isinstance(record, dict) or record.get('schema') != 'tiger-fp32-h50-rtc-acceptance-v1':
            raise ValueError('尚无新RTC无运动性能验收记录')
        if record.get('status') != 'PASS':
            raise ValueError('新RTC验收状态为 ' + str(record.get('status', 'UNKNOWN')))
        if type(record.get('step')) is not int or record['step'] != step:
            raise ValueError('验收checkpoint与本次所选step不一致')
        binding = health.get('acceptance_binding') if isinstance(health, dict) else None
        if not isinstance(binding, dict) or not isinstance(binding.get('identity'), dict):
            raise ValueError('当前服务未提供验收源身份绑定')
        fingerprint, identity = binding.get('contract_fingerprint'), binding['identity']
        if not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
            raise ValueError('当前验收合同指纹无效')
        hashes = ('normalization_sha256', 'tokenizer_sha256', 'rtc_implementation_sha256',
                  'rtc_precision_audit_sha256', 'fp32_loader_sha256', 'worker_entry_sha256', 'supervisor_sha256')
        for key in hashes:
            if not isinstance(identity.get(key), str) or not re.fullmatch('[0-9a-f]{64}', identity[key]):
                raise ValueError('验收身份SHA-256缺失：' + key)
        if (not isinstance(identity.get('source_commit'), str)
                or not re.fullmatch('[0-9a-f]{40}', identity['source_commit'])
                or identity['source_commit'] != health.get('policy', {}).get('source_commit')
                or identity['normalization_sha256'] != health.get('policy', {}).get('normalization_sha256')
                or identity['tokenizer_sha256'] != health.get('policy', {}).get('tokenizer_sha256')
                or identity['rtc_implementation_sha256'] != health.get('rtc', {}).get('implementation_sha256')
                or identity['rtc_precision_audit_sha256'] != health.get('rtc', {}).get('precision_audit_sha256')):
            raise ValueError('验收绑定与当前模型/RTC健康元数据不一致')
        if record.get('contract_fingerprint') != fingerprint or record.get('identity') != identity:
            raise ValueError('验收源代码/模型合同指纹已过期；需对应当前源重新验收')
        semantics = acceptance_semantics()
        if binding.get('semantics') != semantics or record.get('semantics') != semantics:
            raise ValueError('验收播放频率/截止/存储合同已改变；旧时序记录已过期，需本合同重新实测')
        requirements = record.get('requirements', {})
        expected_requirements = {'warmup_calls': APPROVED_CONTRACT['acceptance_warmup_calls'],
                                 'measured_calls': APPROVED_CONTRACT['acceptance_measured_calls'],
                                 'p95_budget_ms': APPROVED_CONTRACT['acceptance_p95_ms'],
                                 'max_budget_ms': APPROVED_CONTRACT['acceptance_max_ms'],
                                 'whole_gpu_budget_mib': APPROVED_CONTRACT['whole_gpu_budget_mib']}
        for key, expected in expected_requirements.items():
            if type(requirements.get(key)) not in (int, float) or requirements[key] != expected:
                raise ValueError('验收门槛与批准值不符：' + key)
        results = record.get('results', {})
        if results.get('mode') != 'native32_bootstrap_then_rtc':
            raise ValueError('验收不是首包native、后续RTC的原生FP32路径')
        if results.get('timing_basis') != APPROVED_CONTRACT['commit_timing_basis'] or results.get('usb_finalflush_validated') is not True:
            raise ValueError('新控制ready指标或本次核验USB最终flush/读回证据缺失')
        if (type(results.get('native_bootstrap_calls')) is not int or results['native_bootstrap_calls'] != 1
                or type(results.get('rtc_continuation_calls')) is not int or results['rtc_continuation_calls'] != 102
                or results.get('bootstrap_accepted_before_deadline') is not True
                or results.get('diagnostic_reference_chain_validated') is not True):
            raise ValueError('首包native按时采用或后续102次RTC诊断链证据缺失')
        for key, expected in {'warmup_calls': APPROVED_CONTRACT['acceptance_warmup_calls'],
                              'measured_calls': APPROVED_CONTRACT['acceptance_measured_calls'],
                              'vjp_enabled_calls': APPROVED_CONTRACT['acceptance_measured_calls']}.items():
            if type(results.get(key)) is not int or results[key] != expected:
                raise ValueError('RTC实测调用数不完整：' + key)
        for key in ('all_actions_float32_50x7', 'zero_weight_same_rng_noise_equivalent', 'robot_chain_audits_passed'):
            if results.get(key) is not True:
                raise ValueError('RTC验收证据尚未通过：' + key)
        latency = results.get('latency_ms', {})
        p95, peak, gpu = latency.get('p95'), latency.get('max'), results.get('whole_gpu_peak_mib')
        if (any(type(item) not in (int, float) or not math.isfinite(item) or item <= 0 for item in (p95, peak, gpu))
                or p95 > APPROVED_CONTRACT['acceptance_p95_ms'] or peak > APPROVED_CONTRACT['acceptance_max_ms']
                or p95 > peak or gpu > APPROVED_CONTRACT['whole_gpu_budget_mib']):
            raise ValueError('RTC实测时延或整卡显存超过批准预算/缺失')
        from .h50_runtime import validate_acceptance
        validate_acceptance(record, health, step)
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, ImportError) as exc:
        return str(exc)
    return None


def h50_health_view(health=None, status=None, catalog=None, error=None, acceptance=None, acceptance_error=None):
    """Public manager metadata. Never expose lease credentials or infer permission."""
    view = {'schema': 'tiger-web-h50-deployment-v1', 'api_reachable': False,
            'manager_valid': False, 'ready': False, 'health_fresh': False,
            'health_age_s': 0., 'health_stale_after_s': 3.,
            'selected_step': None, 'desired_step': None, 'candidates': [],
            'rtc': {'ready': False}, 'acceptance': copy.deepcopy(acceptance),
            'acceptance_error': acceptance_error,
            'acceptance_passed': False, 'acceptance_reason': '尚未核对所选模型的RTC验收证据',
            'coordinate_compatibility': 'independent_physical_verification_pending'}
    try:
        if error is not None:
            raise ValueError(str(error))
        if not isinstance(health, dict) or not isinstance(status, dict) or not isinstance(catalog, dict):
            raise ValueError('4090 manager health/status/catalog不完整')
        phase, lease = status.get('status'), status.get('lease')
        if (not {'schema', 'status', 'selected_step', 'desired_step', 'selection_id',
                 'busy', 'lease', 'worker', 'error'} <= set(status)
                or not isinstance(status.get('schema'), str) or not status['schema']
                or phase not in ('IDLE', 'SELECTING', 'READY', 'FAILED')
                or type(status.get('busy')) is not bool or not isinstance(lease, dict)
                or not {'active', 'session_id', 'step', 'expires_in_s'} <= set(lease)
                or type(lease.get('active')) is not bool):
            raise ValueError('4090 manager状态/占用信息缺失或无效')
        for field in ('selected_step', 'desired_step'):
            if status.get(field) is not None and (type(status[field]) is not int or status[field] not in H50_STEPS):
                raise ValueError('4090 manager的step不在已归档候选中')
        if lease['active'] and (not isinstance(lease.get('session_id'), str) or not lease['session_id']
                or type(lease.get('step')) is not int or lease['step'] not in H50_STEPS
                or type(lease.get('expires_in_s')) not in (int, float)
                or not math.isfinite(lease['expires_in_s']) or lease['expires_in_s'] < 0):
            raise ValueError('4090活动租约元数据不完整')
        steps, candidates = catalog.get('steps'), catalog.get('candidates')
        if (not isinstance(steps, list) or len(steps) != len(H50_STEPS)
                or any(type(step) is not int for step in steps) or set(steps) != set(H50_STEPS)
                or not isinstance(candidates, list) or len(candidates) != len(H50_STEPS)):
            raise ValueError('4090候选目录不完整')
        seen = set()
        for candidate in candidates:
            if (not isinstance(candidate, dict) or type(candidate.get('step')) is not int
                    or candidate['step'] not in H50_STEPS or candidate['step'] in seen
                    or type(candidate.get('available')) is not bool):
                raise ValueError('4090候选条目无效')
            seen.add(candidate['step'])
            view['candidates'].append({key: copy.deepcopy(candidate.get(key))
                                       for key in ('step', 'available', 'error')})
        if catalog.get('selected_step') != status.get('selected_step'):
            raise ValueError('4090目录与状态的已加载step不一致；等待刷新')
        view.update(api_reachable=True, manager_valid=True, health_fresh=True,
                    manager_status=phase, busy=status['busy'], selected_step=status.get('selected_step'),
                    desired_step=status.get('desired_step'), selection_id=status.get('selection_id'),
                    lease={key: copy.deepcopy(lease.get(key))
                           for key in ('active', 'session_id', 'step', 'expires_in_s')},
                    manager_error=copy.deepcopy(status.get('error')),
                    health={key: copy.deepcopy(health.get(key))
                            for key in ('ready', 'local_only', 'contract', 'policy', 'acceptance_binding')})
        if health.get('ready') is True:
            from .remote_fp32 import validate_health
            step = health.get('policy', {}).get('checkpoint_step')
            if type(step) is not int or step not in H50_STEPS or step != view['selected_step']:
                raise ValueError('4090健康元数据与已加载step不一致')
            fingerprint = validate_health(health, expected_step=step)
            view.update(ready=True, metadata_fingerprint=fingerprint,
                        contract=copy.deepcopy(health['contract']), policy=copy.deepcopy(health['policy']))
            view['health']['rtc'] = copy.deepcopy(health.get('rtc'))
            view['rtc'] = h50_rtc_view(health, step)
            reason = acceptance_error or h50_acceptance_gate(acceptance, health, step)
            view.update(acceptance_passed=reason is None and view['rtc']['ready'],
                        acceptance_reason=reason or view['rtc'].get('error'))
        elif phase == 'READY':
            raise ValueError('manager报告READY但模型health尚未通过')
    except (ValueError, TypeError, KeyError, RuntimeError, ImportError) as exc:
        view.update(ready=False, manager_valid=False, error=str(exc))
    return view


def h50_action_gate(value, cfg, action, params=None):
    """Parameter-aware H50 route; the CLI rechecks leases, feedback and bounds."""
    settings = (cfg or {}).get('h50_deployment', {})
    if not isinstance(settings, dict) or settings.get('enabled') is not True:
        return '4090 H50部署路由尚未显式启用'
    if type(settings.get('model_port')) is not int or settings['model_port'] != 18883:
        return '4090 H50部署只接受已冻结的loopback端口18883'
    reason = h50_config_gate(cfg)
    if reason:
        return reason
    usb = value.get('usb', {})
    if usb.get('verified') is not True or usb.get('fresh') is not True:
        return '当前USB身份或回执状态未新鲜核实'
    model = value.get('h50_deployment', {})
    age = model.get('health_age_s')
    if (model.get('api_reachable') is not True or model.get('manager_valid') is not True
            or model.get('health_fresh') is not True or type(age) not in (int, float)
            or not math.isfinite(age) or not 0 <= age <= 3.):
        return model.get('error') or '4090 H50 manager状态未新鲜核实'
    if model.get('ready') is True:
        try:
            from .remote_fp32 import validate_health
            step = model.get('selected_step')
            if type(step) is not int or step not in H50_STEPS:
                raise ValueError('实际已加载step无效')
            if validate_health(model.get('health'), expected_step=step) != model.get('metadata_fingerprint'):
                raise ValueError('health元数据指纹已改变')
        except (ValueError, TypeError, KeyError, RuntimeError, ImportError) as exc:
            return '4090已加载模型元数据复核失败：' + str(exc)
    elif model.get('manager_status') == 'READY':
        return 'manager与模型ready状态不一致'
    if model.get('busy') is not False or model.get('lease', {}).get('active') is not False:
        return '4090正在加载/推理或已有执行租约；等待当前会话结束'
    if model.get('manager_status') == 'SELECTING':
        return '4090正在切换候选；等待加载回执'
    if action != 'h50_load' and (model.get('manager_status') == 'FAILED' or model.get('manager_error')):
        return '4090上次选择失败；请显式加载并核对完整错误回执'
    if params is not None:
        step = params.get('step', settings.get('default_step', 5000))
        candidate = next((item for item in model.get('candidates', []) if item.get('step') == step), {})
        if candidate.get('available') is not True:
            return candidate.get('error') or '所选checkpoint尚未核实可用'
    elif not any(item.get('available') is True for item in model.get('candidates', [])):
        return '没有已核实可用的4090 checkpoint'
    if action == 'h50_load':
        return None
    checked_rtc = h50_rtc_view(model.get('health'), model.get('selected_step'))
    if (model.get('ready') is not True or model.get('rtc', {}).get('ready') is not True
            or checked_rtc.get('ready') is not True
            or checked_rtc.get('rtc_fingerprint') != model['rtc'].get('rtc_fingerprint')):
        return checked_rtc.get('error') or '新H50 RTC精度审计与能力尚未真实通过；先加载所选RTC模型'
    reason = shared_capture_idle_gate(value, cfg)
    if reason:
        return reason
    for role in ('front', 'right'):
        age = value.get('hub', {}).get('cameras', {}).get(role, {}).get('age_s')
        if type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age <= APPROVED_CONTRACT['camera_max_age_s']:
            return 'RTC真实相机超过批准的250ms新鲜度或时间未知：' + role
    if action in ('h50_shadow', 'h50_acceptance'):
        return None
    requested_step = params.get('step', settings.get('default_step', 5000)) if params is not None else model.get('selected_step')
    if requested_step != model.get('selected_step'):
        return '请先加载所选checkpoint并核对其专属RTC验收，再开始正式部署'
    skip_acceptance = (params or {}).get('operator_skip_no_motion_acceptance', False)
    if type(skip_acceptance) is not bool:
        return '人工实验选项必须为布尔值'
    if not skip_acceptance:
        reason = model.get('acceptance_error') or h50_acceptance_gate(model.get('acceptance'), model.get('health'), requested_step)
        if reason:
            return reason + '；正式运动阻止'
    if value.get('stop_latch', {}).get('latched') is not False:
        return '停止锁存生效或未知；不自动清STOP'
    mode = controller_modes(value)['right']
    if mode.get('fresh') is not True or mode.get('classification') != 'CAN_MOVEJ':
        return '新H50部署需要新鲜确认的CAN/MOVEJ模式'
    if mode.get('external_activity'):
        return '存在其他总线控制活动；核对物理控制源隔离后再部署'
    arm = value.get('status', {}).get('arms', {}).get('right', {})
    from .hardware import require_feedback, HardwareBlocked
    try:
        require_feedback(arm, max_age_s=.2, require_enabled=True, require_can=True)
        flags, enabled = arm.get('motor_flags'), arm.get('enabled')
        if (not isinstance(flags, list) or len(flags) != 6
                or any(type(flag) is not int or flag & 0xBF for flag in flags)
                or not isinstance(enabled, list) or len(enabled) != 6
                or any(item is not True for item in enabled)):
            raise HardwareBlocked('六电机真实健康/使能状态不完整')
        # Public summaries use reported ages, never compare robot monotonic
        # timestamps against the PC clock. The executor checks raw 0x2A8 again.
        grip = arm.get('gripper_feedback', {})
        grip_age = grip.get('age_s')
        if (grip.get('fresh') is not True or grip.get('healthy') is not True
                or grip.get('enabled') is not True or type(grip_age) not in (int, float)
                or not math.isfinite(grip_age) or not 0 <= grip_age <= .2):
            raise HardwareBlocked('夹爪新鲜健康且已使能的真实反馈缺失')
    except (HardwareBlocked, ValueError, TypeError) as exc:
        return 'H50真实执行反馈/故障前置条件未通过：' + str(exc)
    return None


class ValidationError(ValueError):
    pass


def number(params, key, default, lo, hi):
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValidationError(f"{key} 必须在 {lo}–{hi} 之间且为有限数")
    return value


def build_command(action, params, cfg):
    """Browser action -> fixed argv. Pure validation, never opens hardware."""
    if not isinstance(params, dict):
        raise ValidationError("params 必须是对象")
    if action in UNAVAILABLE:
        raise ValidationError(UNAVAILABLE[action])
    simple = {"status": ["status", "--json"], "model_plan": ["model-plan"],
              "model_stage": ["model-stage"], "model_status": ["model-status"],
              "model_stop": ["model-stop"], "hub_stop": ["hub-stop"],
              "clear_stop": ["clear-stop"], "stop": ["stop"]}
    allowed = {
        "task_review_success": {"session_id", "note"}, "task_review_failure": {"session_id", "note"},
        **{key: set() for key in simple}, "diagnose": {"seconds"}, "limits": {"arm"},
        "mode_inspect": {"arm", "seconds"},
        "mode_takeover": {"arm", "leader_isolated", "supported", "teaching_ended", "zero_pose_reviewed"},
        "mode_reset": {"arm", "leader_isolated", "supported", "zero_pose_reviewed"},
        "mode_high_follow": {"arm", "supported", "roles_configured", "poses_aligned", "source_reviewed", "teaching_ended"},
        "mode_standby": {"arm", "leader_isolated", "supported", "teaching_ended"},
        "mode_disable": {"arm", "leader_isolated", "supported", "teaching_ended"},
        "gripper_inspect": {"arm", "seconds"},
        "gripper_measure": {"arm", "seconds", "label", "measured_gap_mm"},
        "gripper_disable": {"arm", "leader_isolated", "empty_gripper"},
        "gripper_zero": {"arm", "leader_isolated", "empty_gripper", "physically_closed", "measured_gap_mm", "coordinate_change_reviewed"},
        'move_gripper_plan': {'target_mm'},
        'move_gripper': {'target_mm','leader_isolated','path_reviewed'},
        "hub_start": {"takeover_existing"}, "record_start": {"task","batch_id","trial_id","category","notes"},
        "record_stop":{'episode'},'record_list':{'page','page_size'},
        'collection_create':{'task','title','operator','target_count'},
        'collection_list':{'batch_id','page','page_size'},
        'collection_review':{'batch_id','episode_id','reviewer','verdict','note'},
        'record_retry':{'batch_id','trial_id'},
        "model_start": {"num_steps"}, "shadow": {"samples"}, "shadow_fp32": {"count", "task"},
        'h50_load': {'step'}, 'h50_shadow': {'step', 'count', 'task'}, 'h50_acceptance': {'step'},
        'h50_deploy': {'step', 'duration', 'task', 'leader_isolated', 'path_reviewed', 'operator_skip_no_motion_acceptance'},
        "deploy_plan": {"duration"}, "deploy": {"duration", "leader_isolated", "path_reviewed"},
        "move_plan": {"pose", "arm", "target_deg", "speed_deg_s", "close_gripper", "gripper_speed_mm_s"},
        **{key: {"pose", "arm", "target_deg", "speed_deg_s", "leader_isolated", "path_reviewed", "close_gripper", "gripper_speed_mm_s"}
           for key in ("move_ready", "move_zero", "move_target")},
    }
    if action not in allowed:
        raise ValidationError("未知操作：" + str(action))
    extra = set(params) - allowed[action]
    if extra:
        raise ValidationError("未支持的参数：" + ", ".join(sorted(extra)))
    if action in simple:
        return simple[action]
    if action.startswith("gripper_"):
        if params.get("arm", "right") != "right":
            raise ValidationError("本轮夹爪核验仅支持右执行臂")
        verb = action.removeprefix("gripper_")
        argv = ["gripper", verb, "--arm", "right"]
        if verb in ("inspect", "measure"):
            argv += ["--seconds", str(number(params, "seconds", 2, .5, 5))]
            if verb == "measure":
                label = params.get("label", "closed")
                if label not in ("closed", "mid", "open"):
                    raise ValidationError("测量标签仅支持 closed/mid/open")
                argv += ["--label", label, "--measured-gap-mm", str(number(params, "measured_gap_mm", None, 0, 120))]
            return argv
        flags = ("leader_isolated", "empty_gripper")
        if verb == "zero":
            flags += ("physically_closed", "coordinate_change_reviewed")
            argv += ["--measured-gap-mm", str(number(params, "measured_gap_mm", None, 0, .2))]
        if any(params.get(flag) is not True for flag in flags):
            raise ValidationError("夹爪操作需控制源隔离和空载；设零还需真实闭合、卡尺测量和训练坐标变更复核")
        return argv + ["--apply"] + ["--"+flag.replace("_", "-") for flag in flags]
    if action.startswith("task_review_"):
        sid, note = params.get("session_id"), params.get("note")
        if not isinstance(sid, str) or not 1 <= len(sid) <= 160 or any(ord(c) < 32 for c in sid):
            raise ValidationError("人工复核需要当前任务ID")
        if not isinstance(note, str) or not note.strip() or len(note) > 256 or any(ord(c) < 32 for c in note):
            raise ValidationError("人工复核需要1–256字现场或录像观察说明")
        return ["task-review", "--session-id", sid, "--verdict", action.removeprefix("task_review_"), "--note", note.strip()]
    if action.startswith("mode_"):
        if params.get("arm", "right") != "right":
            raise ValidationError("本版模式页只支持右执行臂")
        verb = {"mode_inspect": "inspect", "mode_takeover": "takeover", "mode_reset": "reset",
                "mode_high_follow": "high-follow", "mode_standby": "standby", "mode_disable": "disable"}[action]
        argv = ["mode", verb, "--arm", "right"]
        if action == "mode_inspect":
            return argv + ["--seconds", str(number(params, "seconds", 1, .3, 5)), "--json"]
        if action == "mode_high_follow":
            flags = ("supported", "roles_configured", "poses_aligned", "source_reviewed", "teaching_ended")
        else:
            flags = ("leader_isolated", "supported")
            flags += ("zero_pose_reviewed",) if action == "mode_reset" else ("teaching_ended",)
        if any(params.get(flag) is not True for flag in flags):
            raise ValidationError("请完成本次模式操作的现场声明；恢复遥操需正确主从角色、姿态对应和来源核对，待机/失能需控制源隔离和可靠承托")
        return argv + ["--apply"] + ["--" + flag.replace("_", "-") for flag in flags]
    if action == "diagnose":
        return ["diagnose", "--seconds", str(number(params, "seconds", 3, 1, 15)), "--json"]
    if action == "limits":
        arm = params.get("arm", "right")
        if arm not in ("left", "right"):
            raise ValidationError("arm 必须是 left 或 right")
        return ["limits", "--arm", arm]
    if action == "hub_start":
        takeover = params.get("takeover_existing", False)
        if type(takeover) is not bool:
            raise ValidationError("相机接管必须显式布尔值")
        return ["hub-start"] + (["--takeover-existing"] if takeover else [])
    if action == "record_start":
        task = params.get("task")
        if not isinstance(task, str) or not task.strip() or len(task) > 256 or any(ord(c) < 32 for c in task):
            raise ValidationError("任务描述需要 1–256 个可读字符")
        argv = ["record-start", "--task", task.strip()]
        if set(params)-{'task'}:
            from .collection import CATEGORIES, _identifier, _BATCH, _TRIAL, _text
            _identifier(params.get('batch_id'),_BATCH,'batch ID')
            _identifier(params.get('trial_id'),_TRIAL,'trial ID')
            _text(task,'task',240)
            if params.get('category') not in CATEGORIES:
                raise ValidationError('类别必须为 A/B/C/D/random/recovery/test')
            notes = _text(params.get('notes',''),'notes',512,empty=True)
            argv += ['--batch-id',params['batch_id'],'--trial-id',params['trial_id'],
                     '--category',params['category'],'--notes',notes]
        return argv
    if action == 'record_retry':
        from .collection import _identifier, _BATCH, _TRIAL
        _identifier(params.get('batch_id'),_BATCH,'batch ID')
        _identifier(params.get('trial_id'),_TRIAL,'trial ID')
        return ['record-retry','--batch-id',params['batch_id'],'--trial-id',params['trial_id']]
    if action == 'record_stop':
        episode = params.get('episode')
        if episode is None and not params:
            return ['record-stop']
        if not isinstance(episode,str) or not episode or len(episode)>4096 or any(ord(c)<32 for c in episode):
            raise ValidationError('停止录制需要当前真实 episode 路径')
        return ['record-stop','--episode',episode]
    if action in ('collection_create','collection_list','collection_review','record_list'):
        from .collection import _identifier, _BATCH, _EPISODE, _text
        if action == 'collection_create':
            target = params.get('target_count',100)
            if type(target) is not int or not 1<=target<=10000:
                raise ValidationError('人工合格目标需为1–10000整数')
            return ['collection','create','--task',_text(params.get('task'),'task',240),
                    '--title',_text(params.get('title'),'title',120),'--operator',
                    _text(params.get('operator'),'operator',80),'--target-count',str(target)]
        if action == 'collection_review':
            _identifier(params.get('batch_id'),_BATCH,'batch ID')
            _identifier(params.get('episode_id'),_EPISODE,'episode ID')
            if params.get('verdict') not in ('accepted','rejected'):
                raise ValidationError('复核结果必须为 accepted/rejected')
            return ['collection','review','--batch-id',params['batch_id'],'--episode-id',params['episode_id'],
                    '--reviewer',_text(params.get('reviewer'),'reviewer',80),'--verdict',params['verdict'],
                    '--note',_text(params.get('note'),'review note',512)]
        page,size = params.get('page',1),params.get('page_size',10)
        if type(page) is not int or page<1 or type(size) is not int or not 1<=size<=10:
            raise ValidationError('分页需正整数，每页1–10条')
        argv = ['record-list']
        if action == 'collection_list':
            _identifier(params.get('batch_id'),_BATCH,'batch ID')
            argv = ['collection','list','--batch-id',params['batch_id']]
        return argv+['--page',str(page),'--page-size',str(size)]
    if action == "model_start":
        n = params.get("num_steps", cfg.get("sampler_steps", 5))
        if type(n) is not int or n not in (5, 10):
            raise ValidationError("采样步数仅支持 5 或 10")
        return ["model-start", "--num-steps", str(n)]
    if action == "shadow":
        n = number(params, "samples", 3, 1, 10)
        if int(n) != n:
            raise ValidationError("samples 必须为整数")
        return ["shadow", "--samples", str(int(n))]
    if action in ('h50_load', 'h50_shadow', 'h50_acceptance', 'h50_deploy'):
        settings = cfg.get('h50_deployment', {})
        step = params.get('step', settings.get('default_step', 5000))
        if type(step) is not int or step not in H50_STEPS:
            raise ValidationError('step必须是已归档的5000/10000/15000/20000/2000整数')
        if action in ('h50_load', 'h50_acceptance'):
            return ['h50-load' if action == 'h50_load' else 'h50-acceptance', '--step', str(step)]
        if action == 'h50_shadow':
            count = params.get('count', 3)
            task = params.get('task', settings.get('task', cfg.get('fp32_shadow', {}).get('task', FP32_SHADOW_TASK)))
            if type(count) is not int or not 1 <= count <= 5:
                raise ValidationError('count必须为1至5的整数')
            if not isinstance(task, str) or not task.strip() or len(task) > 200 or '\x00' in task:
                raise ValidationError('task必须为1至200字符的真实任务提示词')
            return ['h50-shadow', '--step', str(step), '--count', str(count), '--task', task]
        duration = number(params, 'duration', 30, 1, 1800)
        if any(params.get(flag) is not True for flag in ('leader_isolated', 'path_reviewed')):
            raise ValidationError('H50正式部署会真实运动，必须明确确认主臂信号隔离、当前起始姿态、空爪及任务扫掠范围')
        skip_acceptance = params.get('operator_skip_no_motion_acceptance', False)
        if type(skip_acceptance) is not bool:
            raise ValidationError('人工实验选项必须为布尔值')
        task = params.get('task', settings.get('task', FP32_SHADOW_TASK))
        from .h50_prompt import validate_prompt
        try:
            validate_prompt(task)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return ['deploy-h50', '--step', str(step), '--duration', str(duration), '--apply',
                '--leader-isolated', '--path-reviewed'] + (['--task', task] if 'task' in params else []) + (['--operator-skip-no-motion-acceptance'] if skip_acceptance else [])
    if action == 'shadow_fp32':
        count = params.get('count', 3)
        settings = cfg.get('fp32_shadow', {})
        default_task = settings.get('task', FP32_SHADOW_TASK) if isinstance(settings, dict) else FP32_SHADOW_TASK
        task = params.get('task', default_task)
        if type(count) is not int or not 1 <= count <= 5:
            raise ValidationError('count必须为1至5的整数')
        if not isinstance(task, str) or not task.strip() or len(task) > 200 or '\x00' in task:
            raise ValidationError('task必须为1至200字符的真实任务提示词')
        return ['shadow-fp32', '--count', str(count), '--task', task]
    if action in ('move_gripper_plan','move_gripper'):
        from .manual_gripper import parameters as gripper_parameters
        try:request=gripper_parameters(cfg,params.get('target_mm'))
        except ValueError as exc:raise ValidationError(str(exc)) from exc
        argv=['gripper-move','--target-mm',str(request['target_mm'])]
        if action=='move_gripper':
            if params.get('leader_isolated') is not True or params.get('path_reviewed') is not True:
                raise ValidationError('G开合需要确认主臂信号隔离及夹爪开合路径')
            argv+=['--apply','--leader-isolated','--path-reviewed']
        return argv
    if action.startswith("move_"):
        pose = params.get("pose", action.removeprefix("move_"))
        if action != "move_plan" and pose != action.removeprefix("move_"):
            raise ValidationError("操作与目标类型不一致")
        if pose not in ("ready", "zero", "target") or params.get("arm", "right") != "right":
            raise ValidationError("本版运动页只支持右执行臂")
        speed = number(params, "speed_deg_s", cfg["pose_speed_deg_s"], 1, cfg["pose_speed_deg_s"])
        argv = ["move", pose, "--arm", "right", "--speed-deg-s", str(speed)]
        close = params.get("close_gripper", pose == "ready")
        if type(close) is not bool or (close and pose not in ("ready", "zero")):
            raise ValidationError("闭合空爪选项必须为布尔值，仅支持准备位或六关节数值零位")
        if close:
            default_speed = cfg.get("ready_gripper_speed_m_s", .01)*1000
            grip_speed = number(params, "gripper_speed_mm_s", default_speed, 0, 10)
            if grip_speed <= 0:
                raise ValidationError("夹爪线性参考速度必须大于0且不超过10 mm/s")
            minimum_speed = cfg.get('pose_send_hz',50)/1000
            if grip_speed < minimum_speed:
                raise ValidationError(f"当前发送频率的SDK微米步分辨率要求至少{minimum_speed:g} mm/s")
            argv += ["--close-gripper", "--gripper-speed-mm-s", str(grip_speed)]
        elif "gripper_speed_mm_s" in params:
            raise ValidationError("未选择闭合空爪时不接受夹爪速度")
        if pose == "target":
            q = params.get("target_deg")
            if not isinstance(q, list) or len(q) != 6 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in q):
                raise ValidationError("目标需要六个有限关节角（度）")
            # This only rejects obvious invalid drafts. The existing CLI still
            # queries actual firmware limits and validates before movement.
            from .hardware import DOCUMENTED_COMMAND_LIMITS_DEG
            if any(not lo <= x <= hi for x, (lo, hi) in zip(q, DOCUMENTED_COMMAND_LIMITS_DEG)):
                raise ValidationError("目标超出当前 SDK 命令范围；未发送，不修改固件范围")
            argv += ["--angles-deg", *map(str, q)]
        elif "target_deg" in params:
            raise ValidationError("预设目标不接受自定义角度")
        if action == "move_plan":
            return argv + ["--json"]
    else:
        argv = ["deploy", "--duration", str(number(params, "duration", 30, 1, 300))]
        if action == "deploy_plan":
            return argv
    if params.get("leader_isolated") is not True or params.get("path_reviewed") is not True:
        raise ValidationError("实机操作需要明确确认控制源已隔离、运动路径已检查；勾选不会建立隔离")
    return argv + ["--apply", "--leader-isolated", "--path-reviewed"]


def parse_receipt(output):
    """Take the last actual JSON receipt, leaving diagnostic text separate."""
    decoder, candidates = json.JSONDecoder(), []
    for match in re.finditer(r'(?m)^\{',output):
        i = match.start()
        if output[i] == "{":
            try:
                value, end = decoder.raw_decode(output[i:])
                if isinstance(value, dict) and any(k in value for k in ("result", "error", "status", "report")):
                    candidates.append((i + end, value))
            except ValueError:
                pass
    return max(candidates, default=(0, {}), key=lambda pair: pair[0])[1]


def aged_snapshot(snapshot, elapsed):
    """Age a copy of real cached measurements; never promote invalid feedback."""
    value = copy.deepcopy(snapshot)
    elapsed = max(0.0, elapsed)

    def age_fields(node):
        if not isinstance(node, dict):
            return
        for key, item in list(node.items()):
            if isinstance(item, dict):
                age_fields(item)
            elif key == "pair_ages_s" and isinstance(item, list):
                # OEM target pairs age while cached just like joint feedback.
                # Preserve invalid ages so cache residence cannot validate them.
                node[key] = [age + elapsed if type(age) in (int, float)
                             and math.isfinite(age) and age >= 0 else age for age in item]
            elif ((key == "age_s" or key.endswith("_age_s") or
                   (key.startswith("age_") and key.endswith("_s"))) and
                  isinstance(item, (int, float)) and not isinstance(item, bool)):
                node[key] = item + elapsed

    for container in (value.get("status", {}), value.get("hub", {}), value):
        for arm in container.get("arms", {}).values():
            age_fields(arm)
            ages = [arm.get(k) for k in ("age_joint_s", "age_gripper_s", "age_status_s") if k in arm]
            threshold = arm.get("stale_after_s", .2)
            valid = bool(ages) and all(isinstance(x, (int, float)) and 0 <= x <= threshold for x in ages)
            arm["fresh"] = arm.get("fresh") is True and valid
            if "feedback_fresh" in arm:
                arm["feedback_fresh"] = arm.get("feedback_fresh") is True and valid
        for camera in container.get("cameras", {}).values():
            age_fields(camera)
            age = camera.get("age_s")
            camera["fresh"] = camera.get("fresh") is True and isinstance(age, (int, float)) and 0 <= age <= camera.get("stale_after_s", .5)
        stage = container.get("stage_observer")
        if isinstance(stage, dict):
            age_fields(stage)
            age = stage.get("age_s")
            stage["fresh"] = (stage.get("fresh") is True and isinstance(age, (int, float))
                              and not isinstance(age, bool) and math.isfinite(age)
                              and 0 <= age <= stage.get("freshness_limit_s", 1.))
            if not stage["fresh"]:
                stage.update(phase="unknown", label="无法确认阶段", task_result="unverified")
        recovery = container.get("recovery")
        if isinstance(recovery, dict):
            age_fields(recovery)
            age = recovery.get("age_s")
            recovery["fresh"] = (recovery.get("fresh") is True and type(age) in (int, float)
                and math.isfinite(age) and 0 <= age <= recovery.get("freshness_limit_s", 1.))
        for key in ("task_state", "perception"):
            observed = container.get(key)
            if isinstance(observed, dict):
                age_fields(observed)
                age = observed.get("age_s")
                observed["fresh"] = (observed.get("fresh") is True and type(age) in (int, float)
                    and math.isfinite(age) and 0 <= age <= observed.get("freshness_limit_s", 1.))
                if not observed["fresh"]:
                    observed["live_display_valid"] = False
    model = value.get("model", {})
    if isinstance(model, dict) and "health_age_s" in model:
        age = model.get("health_age_s")
        age = age + elapsed if type(age) in (int, float) and math.isfinite(age) and age >= 0 else None
        model["health_age_s"] = age
        threshold = model.get("health_stale_after_s", 3.)
        model["health_fresh"] = (model.get("health_fresh") is True and age is not None
            and type(threshold) in (int, float) and math.isfinite(threshold)
            and 0 <= age <= threshold)
        if not model["health_fresh"]:
            model.update(ready=False, api_reachable=False)
    for key in ('fp32_shadow', 'h50_deployment'):
        shadow = value.get(key)
        if not isinstance(shadow, dict):
            continue
        age = shadow.get('health_age_s')
        age = age + elapsed if type(age) in (int, float) and math.isfinite(age) and age >= 0 else None
        shadow['health_age_s'] = age
        shadow['health_fresh'] = shadow.get('health_fresh') is True and age is not None and 0 <= age <= 3.
        if not shadow['health_fresh']:
            shadow.update(ready=False, api_reachable=False)
    for key in ("usb", "gripper_coordinate_review"):
        component = value.get(key)
        if not isinstance(component, dict) or "age_s" not in component:
            continue  # Preserve the older agent contract during version checks.
        original_age = component.get("age_s")
        original_valid = type(original_age) in (int, float) and math.isfinite(original_age) and original_age >= 0
        age_fields(component)
        age, threshold = component.get("age_s"), component.get("stale_after_s", 15.)
        component["fresh"] = (original_valid and component.get("fresh") is True and type(age) in (int, float)
            and math.isfinite(age) and type(threshold) in (int, float)
            and math.isfinite(threshold) and 0 <= age <= threshold)
        if not component["fresh"]:
            if key == "usb":
                component["verified"] = False
            else:
                component["pending"] = True
    # Diagnostic component ages follow the same transport/cache interval. They
    # never grant readiness or replace source freshness/identity checks.
    age_fields(value.get("state_refresh"))
    process = model.get("process") if isinstance(model, dict) else None
    if isinstance(process, dict):
        age_fields(process)
        age = process.get("age_s")
        process["fresh"] = (process.get("fresh") is True and type(age) in (int, float)
                            and math.isfinite(age) and 0 <= age <= process.get("stale_after_s", 3.))
    return value


def feedback_delivery(value, connected):
    """Expose two observed ages without inventing a common sampling instant."""
    def measured_age(arm):
        ages = [arm.get(key) for key in ("age_joint_s", "age_status_s", "age_gripper_s")]
        return max(ages) if all(type(x) in (int, float) and math.isfinite(x) and x >= 0
                               for x in ages) else None

    def fresh(arm, age):
        threshold = arm.get("stale_after_s", .2)
        return bool(connected and arm.get("fresh") is True and age is not None
                    and type(threshold) in (int, float) and math.isfinite(threshold)
                    and 0 <= age <= threshold)

    direct = value.get("status", {}).get("arms", {})
    shared = value.get("hub", {}).get("arms", {})
    arms = {}
    for role in ("right", "left"):
        current, cached = direct.get(role, {}), shared.get(role, {})
        current = current if isinstance(current, dict) else {}
        cached = cached if isinstance(cached, dict) else {}
        direct_age, hub_age = measured_age(current), measured_age(cached)
        direct_fresh, hub_fresh = fresh(current, direct_age), fresh(cached, hub_age)
        arms[role] = {"direct_age_s": direct_age, "hub_age_s": hub_age,
            "direct_fresh": direct_fresh, "hub_fresh": hub_fresh,
            "freshness_disagreement": bool(connected and direct_age is not None
                and hub_age is not None and direct_fresh != hub_fresh)}
    return {"schema": "tiger-feedback-delivery-v1", "arms": arms,
        "pc_cache_age_s": value.get("cache_residence_s"),
        "components": copy.deepcopy(value.get("state_refresh", {})),
        "age_basis": "conservative source age plus delivery/cache time; not a hardware acquisition clock",
        "affects_execution_gates": False}


def capture_scope_view(value, cfg=None):
    """Compare the published service contract to explicit local configuration."""
    published = value.get('hub', {}).get('recording_contract', {}).get('capture_contract')
    result = {'configured': None, 'published': published, 'matched': False, 'reason': None}
    if cfg is None or 'capture_profile' not in cfg:
        return result  # Existing legacy callers still require all channels.
    from .capture_profile import contract
    try:
        result['configured'] = contract(cfg)
        result['matched'] = published == result['configured']
        if not result['matched']:
            result['reason'] = '共享服务的采集范围与本机配置不一致；请同步并重载后核验'
        else:
            hub = value.get('hub', {})
            if hub.get('schema') != 'tiger-unified-sensor-hub-v1':
                result['reason'] = '共享服务身份未核实；不能用其他服务状态开始录制'
            for role, serial in result['configured']['camera_serials'].items():
                if hub.get('cameras', {}).get(role, {}).get('serial') != serial:
                    result['reason'] = '必需相机串号尚未匹配：' + role
            for role, serial in result['configured']['arm_adapter_serials'].items():
                identity = hub.get('arms', {}).get(role, {}).get('identity')
                if (not isinstance(identity, dict) or identity.get('identity_verified') is not True
                        or identity.get('serial') != serial):
                    result['reason'] = '必需 CAN 适配器身份尚未匹配：' + role
            result['matched'] = result['reason'] is None
    except (ValueError, KeyError, TypeError):
        result['reason'] = '采集范围配置无效；不能省略必需反馈'
    return result


def mode_preflight(value, action_capabilities):
    """Describe cached mode prerequisites; no temporal proof or hardware access.

    Availability mirrors the API gates, not permission to move. The runtime
    still verifies raw OEM targets, writer ownership and distinct stationary
    feedback before sending any controller request.
    """
    right = value.get("status", {}).get("arms", {}).get("right", {})
    hub = value.get("hub", {})
    recording = hub.get("recording", {})
    target = right.get("action_targets", {})
    ages = target.get("pair_ages_s")
    numeric_ages = (isinstance(ages, list) and len(ages) == 3 and
                    all(type(age) in (int, float) and math.isfinite(age) and age >= 0 for age in ages))
    actual, requested = right.get("q_deg"), target.get("q_deg")
    numeric_vectors = all(isinstance(q, list) and len(q) == 6 and
                          all(type(angle) in (int, float) and math.isfinite(angle) for angle in q)
                          for q in (actual, requested))
    return {"schema": "tiger-mode-preflight-v1", "arm": "right",
            "source": "cached_shared_feedback", "commands_sent": 0,
            "automatic_switch_allowed": False, "physical_source_authenticated": False,
            "stationary_window_verified": False,
            "runtime_checks_required": ["raw_target_identity_and_bounds", "exclusive_writer",
                                        "distinct_fresh_stationary_feedback", "STOP_and_recording_recheck"],
            "shared_task_active": hub.get("task_state", {}).get("active"),
            "shared_deployment_active": hub.get("recovery", {}).get("deployment_active"),
            "recording_active": recording.get("active"), "recording_saving": recording.get("saving"),
            "oem_targets": {"max_pair_age_s": max(ages) if numeric_ages else None,
                            "receipt_skew_s": max(ages)-min(ages) if numeric_ages else None,
                            "max_joint_error_deg": max(abs(a-b) for a, b in zip(actual, requested))
                                                   if numeric_vectors else None},
            "actions": {action: dict(action_capabilities.get(action, {"available": False,
                                "reason": "尚未取得本次模式操作能力"}))
                        for action in ("mode_takeover", "mode_reset", "mode_high_follow",
                                       "mode_standby", "mode_disable")}}


def capabilities(value, connected, cfg=None):
    """Explain real prerequisites. Runtime CLI still rechecks before any action."""
    actions = ("connect", "disconnect", "sync", "status", "diagnose", "limits", "hub_start", "hub_stop",
               "record_start", "record_stop", "record_list", "move_plan", "move_ready", "move_zero",
               "move_target", "model_plan", "model_stage", "model_start", "model_status", "model_stop",
               "shadow", "shadow_fp32", "h50_load", "h50_shadow", "h50_acceptance", "h50_deploy", "deploy_plan", "deploy", "clear_stop")
    actions += ("mode_inspect", "mode_takeover", "mode_reset", "mode_high_follow", "mode_standby", "mode_disable")
    actions += ("gripper_inspect", "gripper_measure", "gripper_disable", "gripper_zero")
    actions += ("task_review_success", "task_review_failure")
    actions += ('collection_create','collection_list','collection_review','record_retry')
    result = {key: {"available": False, "reason": reason} for key, reason in UNAVAILABLE.items()}
    usb = value.get("usb", {}).get("verified") is True
    hub, model = value.get("hub", {}), value.get("model", {})
    right = value.get("status", {}).get("arms", {}).get("right", {})
    actual_mode = controller_modes(value)["right"]
    cameras = hub.get("cameras", {})
    busy = any(job.get("state") in ("queued", "running") for job in value.get("jobs", []))
    def fresh_cameras(roles):
        return all(cameras.get(role, {}).get("fresh") is True for role in roles)
    for action in actions:
        reason = None
        if action == "connect":
            reason = "已连接 AgileX" if connected else None
        elif action == "sync":
            reason = "请先断开页面通道，再同步代码" if connected else None
        elif not connected:
            reason = "请先连接 AgileX"
        elif action != "disconnect" and not usb:
            reason = "U 盘挂载身份未核实，不能保存真实回执"
        elif action == 'shadow_fp32':
            reason = fp32_shadow_gate(value, cfg)
        elif action in ('h50_load', 'h50_shadow', 'h50_acceptance', 'h50_deploy'):
            # This PC button is the explicitly requested manual-experiment route.
            # Actual submissions still need the typed flag and现场 confirmations.
            gate_params = ({'step':value.get('h50_deployment',{}).get('selected_step'),
                            'operator_skip_no_motion_acceptance':True} if action=='h50_deploy' else None)
            reason = h50_action_gate(value, cfg, action, gate_params)
        elif action.startswith("gripper_"):
            if hub.get("api_reachable") is not True:
                reason = "共享采集服务不可达；夹爪核验复用既有接收器"
            elif action in ("gripper_disable", "gripper_zero"):
                grip = right.get("gripper_feedback", {})
                age = grip.get("age_s")
                if value.get("stop_latch", {}).get("latched") is not False:
                    reason = "停止锁存未解除；不自动清STOP"
                elif hub.get("recording", {}).get("active") is not False:
                    reason = "录制活动或未知；先保留当前坐标合同"
                elif (grip.get("fresh") is not True or type(age) not in (int, float)
                      or not math.isfinite(age) or not 0 <= age <= .2):
                    reason = "夹爪独立反馈不新鲜；先只读核验"
                elif grip.get("healthy") is not True:
                    reason = "夹爪驱动报告故障；不通过设零清错"
                elif action == "gripper_zero" and grip.get("enabled") is not False:
                    reason = "夹爪仍使能；设零前需单独失能并实际闭合"
                elif actual_mode.get("fresh") is not True or actual_mode.get("arm_status") != 0:
                    reason = "六关节控制器状态未核实或仍有异常/示教"
                elif actual_mode.get("external_activity"):
                    reason = "近5秒有控制报文；等待安静窗口并复核控制源隔离"
        elif action.startswith("task_review_"):
            if hub.get("api_reachable") is not True or not hub.get("task_state", {}).get("session_id"):
                reason = "没有可复核的真实任务ID或共享服务不可达"
        elif action in ("mode_takeover", "mode_reset", "mode_high_follow", "mode_standby", "mode_disable"):
            latch = value.get("stop_latch", {}).get("latched")
            if latch is not False:
                reason = "停止已锁存；准备新会话后显式解除" if latch is True else "停止锁存状态尚未核实"
            elif action in ("mode_high_follow", "mode_standby", "mode_disable") and (
                    hub.get("api_reachable") is not True or hub.get("recording", {}).get("active") is not False
                    or hub.get("recording", {}).get("saving") is not False):
                reason = "模式操作需共享采集服务在线，且无活动录制或收尾"
            elif action in ("mode_high_follow", "mode_standby", "mode_disable") and model.get("busy") is True:
                reason = "模型仍在推理；先完成当前模型任务再切换模式"
            elif action in ("mode_high_follow", "mode_standby", "mode_disable") and (
                    hub.get("task_state", {}).get("active") is True
                    or hub.get("recovery", {}).get("deployment_active") is True):
                reason = "共享任务或部署会话仍活动；先完成并核对当前任务回执再切换模式"
            elif actual_mode["fresh"] is not True:
                reason = "缺少新鲜完整的真实 0x2A1 状态；先读取模式诊断"
            elif action != "mode_high_follow" and actual_mode["external_activity"]:
                reason = "近 5 秒存在总线控制报文；等待安静窗口并核对控制来源，不自动争抢控制"
            elif actual_mode["classification"] == "FAULT":
                reason = "真实控制器有异常；先检查诊断，本版不自动清故障"
            elif actual_mode.get("arm_status") != 0:
                reason = "机械臂仍报告示教记录／执行状态；先完成原厂结束流程，再重新诊断"
            elif action == "mode_takeover" and actual_mode["classification"] not in ("STANDBY", "CAN_MOVEJ"):
                reason = actual_mode["reason"]
            else:
                from .hardware import require_feedback, HardwareBlocked
                try:
                    require_feedback(right, max_age_s=.2, require_enabled=action == "mode_high_follow")
                    flags = right.get("motor_flags")
                    if not isinstance(flags, list) or len(flags) != 6 or any(type(flag) is not int for flag in flags):
                        raise HardwareBlocked("六电机原始状态未知")
                except (HardwareBlocked, ValueError, TypeError) as exc:
                    reason = "模式前置状态未就绪：" + str(exc)
                if not reason and action == "mode_high_follow":
                    target = right.get("action_targets", {})
                    ages = target.get("pair_ages_s")
                    q, measured = target.get("q_deg"), right.get("q_deg")
                    if (not isinstance(ages, list) or len(ages) != 3
                            or any(type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age <= .2 for age in ages)
                            or not isinstance(q, list) or len(q) != 6
                            or any(type(angle) not in (int, float) or not math.isfinite(angle) for angle in q)):
                        reason = "未收到新鲜完整的主臂目标；先核对原厂主从连接与目标来源"
                    elif max(ages)-min(ages) > .05:
                        reason = "主臂三组目标接收时间差超过50ms；先核对同组目标与来源，不能混用新旧目标恢复跟随"
                    elif not isinstance(measured, list) or len(measured) != 6 or max(abs(a-b) for a,b in zip(q, measured)) > 2:
                        reason = "当前主臂目标与执行臂反馈差超过2°，不能直接恢复高跟随追赶"
        elif action in ("move_ready", "move_zero", "move_target", "deploy"):
            latch = value.get("stop_latch", {}).get("latched")
            if action == "deploy" and value.get('gripper_coordinate_review', {}).get('pending') is True:
                reason = "夹爪设零后的物理行程和训练坐标尚未复核；策略执行被阻止"
            elif latch is not False:
                reason = "停止已锁存；准备新会话后显式解除" if latch is True else "停止锁存状态尚未核实"
            elif right.get("fresh") is not True:
                reason = "右臂实际反馈不新鲜；尚未确认可执行"
            elif actual_mode["fresh"] is not True:
                reason = "右臂完整控制状态不新鲜；先读取模式诊断"
            elif actual_mode["classification"] != "CAN_MOVEJ":
                reason = "实际控制模式为 " + actual_mode["classification"] + "；请先完成 CAN/MOVEJ 接管并确认持续回传"
            elif actual_mode["external_activity"]:
                reason = "近 5 秒存在总线控制报文；等待安静窗口并核对控制来源，不能同时下发运动目标"
            elif (right.get("status") or {}).get("err_code") != 0:
                reason = "右臂控制器状态未知或有故障；请先检查诊断"
            elif (right.get("status") or {}).get("arm_status") != 0:
                reason = "右臂不是健康 NORMAL 状态；请先检查诊断"
            elif (not isinstance(right.get("motor_flags"), list) or len(right["motor_flags"]) != 6 or
                  any(type(flag) is not int or flag & 0xBF for flag in right["motor_flags"])):
                reason = "六电机状态未知或有驱动故障；请先检查诊断"
            else:
                # Match the existing pose planner's start-vector check. These
                # are documented command bounds, not a new 0x473 query, and an
                # outside measured start does not imply a controller fault.
                from .hardware import DOCUMENTED_COMMAND_LIMITS_DEG, validate_target_deg, HardwareBlocked
                try:
                    validate_target_deg(right.get("q_deg", right.get("joints_deg")),
                                        DOCUMENTED_COMMAND_LIMITS_DEG)
                except (HardwareBlocked, TypeError, ValueError) as exc:
                    detail = str(exc).replace(" target ", " 实测起点 ")
                    if "outside" in detail:
                        reason = "现有轨迹规划的起点不在文档命令范围；这不是控制器已报超限故障。" + detail
                    else:
                        reason = "现有轨迹规划的实测起点尚未通过核验；请核对完整六关节反馈。" + detail
        if not reason and action == "deploy":
            gripper = right.get("gripper_feedback", {})
            age = gripper.get("age_s")
            if (gripper.get("fresh") is True and type(age) in (int, float)
                    and math.isfinite(age) and 0 <= age <= right.get("stale_after_s", .2)
                    and gripper.get("fault_flags")):
                faults = "、".join(gripper.get("fault_labels") or gripper["fault_flags"])
                reason = "夹爪驱动报告异常 " + str(gripper.get("status_hex", "")) + "：" + faults + "；先排查夹爪，不能重复闭合或开始部署"
        if not reason and action in ("record_start",'record_retry', "shadow", "deploy"):
            # Manual capture/deploy use the same frozen recording contract.
            # Registered but excluded devices are never fabricated as fresh.
            needs_recording = action in ("record_start",'record_retry', "deploy")
            roles = ("front", "right", "left") if needs_recording else ("front", "right")
            arm_roles = ("right", "left")
            scope = capture_scope_view(value, cfg) if needs_recording else {}
            profile_error = scope.get('reason')
            if scope.get('configured'):
                roles = scope['configured']['required_camera_roles']
                arm_roles = scope['configured']['required_arm_roles']
            if hub.get("api_reachable") is not True:
                reason = "请先启动共享监控并等实际相机出图"
            elif profile_error:
                reason = profile_error
            elif not fresh_cameras(roles):
                missing = ", ".join(role for role in roles if cameras.get(role, {}).get("fresh") is not True)
                reason = "相机缺少新鲜图像：" + missing
            elif needs_recording and hub.get("recording", {}).get("active") is True:
                # Recorder.active remains true until its writer finishes. Do
                # not infer a separate flush state from a nonzero queue alone.
                reason = "当前录制会话仍活动（可能正在采集或保存）；请结束并保存后再启动新录制或部署"
            elif needs_recording and any(hub.get("arms", {}).get(role, {}).get("fresh") is not True
                                        for role in arm_roles):
                missing = ", ".join(role for role in arm_roles
                                    if hub.get("arms", {}).get(role, {}).get("fresh") is not True)
                reason = "本次录制所需实际反馈不新鲜：" + missing
        if not reason and action in ("shadow", "deploy"):
            if right.get("fresh") is not True:
                reason = "右臂实际反馈不新鲜"
            elif model.get("api_reachable") is not True or model.get("ready") is not True:
                reason = "请先加载模型，并等待模型服务就绪"
        if not reason and action == "record_stop" and hub.get("recording", {}).get("active") is not True:
            reason = "当前没有共享录制会话"
        if not reason and action == "model_start" and value.get("model_stage", {}).get("marker_present") is not True:
            reason = "请先校验／暂存原模型；加载时还会核对实际文件"
        if not reason and action == "model_start" and model.get("ready") is True:
            reason = "模型服务已就绪；更改采样步数需先停止服务"
        if not reason and action == "model_start" and isinstance(model.get("process"), dict):
            process = model["process"]
            if process.get("fresh") is not True or process.get("identity_verified") is not True:
                reason = "模型进程身份或状态尚未核实；请等待刷新，不能重复启动"
            elif process.get("alive") is True:
                reason = "模型进程正在运行，等待服务就绪；不能重复加载，可查看服务日志"
        if not reason and busy and not action.startswith("task_review_"):
            reason = "当前任务执行中，请等待回执；停止新目标仍可独立使用"
        result[action] = {"available": reason is None, "reason": reason}
    grip = right.get('gripper_feedback', {})
    age, position = grip.get('age_s'), grip.get('aperture_m')
    reason = None
    if not connected or not usb:
        reason = '连接或真实USB尚未核实'
    elif busy:
        reason = '当前任务执行中，等待回执'
    elif (grip.get('fresh') is not True or type(age) not in (int,float)
          or not math.isfinite(age) or not 0 <= age <= .2):
        reason = '夹爪独立反馈不新鲜'
    elif grip.get('healthy') is not True:
        reason = '夹爪驱动报告故障 '+str(grip.get('status_hex','未知'))+'，不能执行闭合准备'
    elif (type(position) not in (int,float) or not math.isfinite(position)
          or not cfg_gripper_bounds(cfg, position)):
        reason = '夹爪实际起点不在原命令范围；不裁剪或猜零点'
    result['gripper_close'] = {'available':reason is None,'reason':reason,
                               'reference_speed_min_mm_s':(cfg or {}).get('pose_send_hz',50)/1000,
                               'reference_speed_max_mm_s':10,'physical_speed_verified':False}
    plan_reason=reason
    if plan_reason is None and hub.get('api_reachable') is not True:
        plan_reason='请先开启共享采集，G使用现有Hub反馈'
    result['move_gripper_plan']={'available':plan_reason is None,'reason':plan_reason}
    move_reason=plan_reason
    if move_reason is None:
        if value.get('stop_latch',{}).get('latched') is not False:
            move_reason='停止锁存尚未解除或未核对'
        elif actual_mode.get('fresh') is not True or actual_mode.get('classification')!='CAN_MOVEJ':
            move_reason='请先完成电脑CAN/MOVEJ接管并取得新鲜反馈'
        elif actual_mode.get('external_activity'):
            move_reason='存在其他控制报文，等待安静窗口后再执行G开合'
        elif right.get('enabled')!=[True]*6:
            move_reason='六关节尚未确认使能；G操作不代替六关节使能'
        elif right.get('fresh') is not True or (right.get('status') or {}).get('err_code')!=0 or (right.get('status') or {}).get('arm_status')!=0:
            move_reason='右臂反馈未核对或控制器存在故障'
        elif not isinstance(right.get('motor_flags'),list) or len(right['motor_flags'])!=6 or any(type(x) is not int or x&0xBF for x in right['motor_flags']):
            move_reason='六关节驱动状态未知或存在故障'
        elif hub.get('recording',{}).get('active') is not False or hub.get('recording',{}).get('saving') is not False:
            move_reason='请先结束当前录制并等待保存，再执行独立G开合'
        elif hub.get('recovery',{}).get('deployment_active') is True or value.get('h50_deployment',{}).get('lease',{}).get('active') is True:
            move_reason='当前部署会话占用控制通路'
    result['move_gripper']={'available':move_reason is None,'reason':move_reason}
    return result


def cfg_gripper_bounds(cfg, position):
    # Production callers provide their loaded config; the default preserves the
    # existing contract for offline callers. This never sends or clips targets.
    bounds = (cfg or {}).get('gripper_bounds_m', (-.0013,.1))
    return (isinstance(bounds,(tuple,list)) and len(bounds)==2
            and all(type(x) in (int,float) and math.isfinite(x) for x in bounds)
            and bounds[0] < bounds[1] and bounds[0] <= position <= bounds[1])


def bridge_ssh_argv(cfg):
    """Viewer forwarding shares the pinned stdio SSH process and PC loopback."""
    forwards = cfg.get("ssh_forwardings", [])
    if not isinstance(forwards, list):
        raise ValidationError("ssh_forwardings 必须是配置列表")
    options, local_ports = [], set()
    for forward in forwards:
        if (not isinstance(forward, dict) or
                set(forward) != {"local_host", "local_port", "remote_host", "remote_port"}):
            raise ValidationError("SSH 转发必须明确 local_host/local_port/remote_host/remote_port")
        if forward["local_host"] != "127.0.0.1":
            raise ValidationError("SSH 转发只允许绑定 PC 127.0.0.1")
        for key in ("local_port", "remote_port"):
            port = forward[key]
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValidationError("SSH 转发端口必须是 1–65535 的整数")
        if not isinstance(forward["remote_host"], str):
            raise ValidationError("SSH 转发 remote_host 必须是明确 IPv4 地址")
        try:
            remote_host = str(ipaddress.IPv4Address(forward["remote_host"]))
        except (ipaddress.AddressValueError, TypeError):
            raise ValidationError("SSH 转发 remote_host 必须是明确 IPv4 地址") from None
        if forward["local_port"] in local_ports:
            raise ValidationError("SSH 转发 PC 端口不能重复")
        local_ports.add(forward["local_port"])
        options.extend(["-L", f"127.0.0.1:{forward['local_port']}:{remote_host}:{forward['remote_port']}"])
    base = pc.ssh_base(cfg)
    if not base or base[-1] != cfg["ssh_alias"]:
        raise ValidationError("SSH 基础参数的目标别名位置不正确")
    if options:
        options = ["-o", "ExitOnForwardFailure=yes", *options]
    return [*base[:-1], *options, base[-1]]


class RemoteBridge:
    """Single pinned SSH stdio process; viewer ports bind only PC loopback."""
    def __init__(self, cfg, root):
        self.cfg, self.root = cfg, root
        self.process = None
        self.lock, self.write_lock = threading.Lock(), threading.Lock()
        self.pending = {}
        self.errors = deque(maxlen=20)

    def connect(self):
        if self.process and self.process.poll() is None:
            return self.request("state")
        ssh_argv = bridge_ssh_argv(self.cfg)
        if self.cfg.get('usb_selection'):
            # An idle reinserted disk can receive a new device number. Refresh
            # storage-only session metadata after exact code/non-storage checks.
            pc.prepare_session_config(self.cfg, self.root)
        pc.check_sources(self.cfg, self.root)
        # The selected USB contract travels with this session. The PC's base
        # configuration stays unchanged; the verified USB copy is rebased by sync.
        cmd = pc.remote_web_command(self.cfg)
        self.process = subprocess.Popen([*ssh_argv, cmd], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        process = self.process
        threading.Thread(target=self._reader, args=(process,), daemon=True, name="ssh-receipts").start()
        threading.Thread(target=self._errors, args=(process,), daemon=True, name="ssh-errors").start()
        return self.request("state", timeout=30)

    def discover_storage(self):
        # Self-contained metadata probe: works before a source bundle exists and
        # never constructs the receive-only agent, camera or CAN observers.
        return pc.discover_storage(self.cfg)

    def _errors(self, process):
        for line in process.stderr:
            self.errors.append(line.strip()[:1000])

    def _reader(self, process):
        try:
            for line in process.stdout:
                value = json.loads(line)
                with self.lock:
                    pending = self.pending.get(value.get("id"))
                if pending and pending[0] is process:
                    pending[1].put(value)
        except Exception as exc:
            self.errors.append(repr(exc))
        finally:
            with self.lock:
                for owner, response in self.pending.values():
                    if owner is process:
                        response.put({"ok": False, "error": "SSH 通道已结束；远端运动结果不能假定成功"})

    def request(self, op, timeout=8, **data):
        process = self.process
        if not process or process.poll() is not None:
            raise RuntimeError("SSH 未连接：" + "; ".join(self.errors))
        rid, response = uuid.uuid4().hex, queue.Queue()
        with self.lock:
            self.pending[rid] = (process, response)
        try:
            with self.write_lock:
                process.stdin.write(json.dumps({"id": rid, "op": op, **data}, allow_nan=False) + "\n")
                process.stdin.flush()
            value = response.get(timeout=timeout)
            if not value.get("ok"):
                raise RuntimeError(value.get("error", "远端请求失败"))
            return value.get("result")
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def close(self):
        if self.process and self.process.poll() is None:
            self.request("close")
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise RuntimeError("远端退出未确认，保留进程身份供检查")


def operator_stop_public_view(job):
    """Derived UI classification only; immutable USB failure receipts stay unchanged."""
    value=copy.deepcopy(job)
    mark=value.get('operator_stop',{})
    error=str(value.get('error') or '')
    software_stop=any(token in error for token in ('software STOP latch present','software STOP/cancel requested'))
    other_failure=any(token in error.lower() for token in ('hardwareblocked','recording_failed','usb not','stale','timed out'))
    if value.get('state')=='failed' and mark.get('confirmed') is True and software_stop and not other_failure:
        value.update(raw_terminal_state='failed',state='cancelled',outcome_reason='OPERATOR_REQUESTED_STOP',
            status_message='用户手动停止；原始错误保留，保存结果以独立回执为准')
    return value


class WebApp:
    STATE_POLL_PERIOD_S = .05
    STORAGE_POLL_PERIOD_S = 5.

    def __init__(self, cfg, root, transport=None, semantic_workspace=None, presence_workspace=None):
        self.cfg, self.root = copy.deepcopy(cfg), Path(root)
        self.base_cfg = copy.deepcopy(cfg)
        self.transport = transport if transport is not None else RemoteBridge(self.cfg, self.root)
        self.token = secrets.token_urlsafe(32)
        self.lock, self.operation = threading.RLock(), threading.Lock()
        self.jobs, self.events = deque(maxlen=32), deque(maxlen=128)
        self.connected, self.cached, self.cache_at = False, {}, time.monotonic()
        self.semantic_workspace = semantic_workspace
        self.presence_workspace = presence_workspace
        self.closed = threading.Event()
        from .precision_workbench import PrecisionWorkbench
        self.precision = PrecisionWorkbench.try_create(self) if transport is None else None
        self.storage_scan_lock = threading.Lock()
        self.storage_scan_at = None
        self.storage = {"schema": "tiger-web-usb-storage-v1", "status": "not_scanned",
            "candidates": [], "selected": None, "checked_at": None, "error": None,
            "reason": "等待只读扫描 AgileX 上实际挂载的 USB；不会使用 PC 或 4090 的盘",
            "runtime_ready": False, "scanning": False}
        threading.Thread(target=self._poll, daemon=True, name="web-state-cache").start()
        # Injected/offline transports stay inert unless their test explicitly
        # requests a scan. State GETs only read RAM and never start SSH work.
        if transport is None:
            threading.Thread(target=self._storage_poll, daemon=True, name="usb-metadata-cache").start()

    def snapshot(self):
        with self.lock:
            residence = max(0, time.monotonic() - self.cache_at)
            value = aged_snapshot(self.cached, residence)
            value["cache_residence_s"] = residence
            value.update(connected=self.connected, csrf_token=self.token, config=self.cfg,
                         source="AgileX SSH / 实际 CAN 反馈" if self.connected else "PC / 未连接",
                         jobs=[operator_stop_public_view(j) for j in self.jobs], events=list(self.events))
            value["capabilities"] = capabilities(value, self.connected, self.cfg)
            if self.precision is not None:
                value["precision_workbench"] = self.precision.snapshot()
                value["capabilities"].update(self.precision.capabilities(value))
            value["mode_preflight"] = mode_preflight(value, value["capabilities"])
            value['capture_scope'] = capture_scope_view(value, self.cfg)
            value["controller_modes"] = controller_modes(value)
            value["feedback_delivery"] = feedback_delivery(value, self.connected)
            value["feedback_delivery"].update(pc_poll_period_s=self.STATE_POLL_PERIOD_S,
                pc_rpc_duration_s=getattr(self, "state_rpc_duration_s", None))
            value['semantic_workspace_available'] = getattr(self, 'semantic_workspace', None) is not None
            value["usb_storage"] = self.storage_snapshot()
            return value

    def _storage_locked(self, allow_connect=False):
        busy = any(j.get("state") in ("queued", "running", "unknown")
            and not (allow_connect and j.get("action") in ("connect", "sync")) for j in self.jobs)
        # A Hub can outlive the page's CLI job. Do not confuse an ended recording
        # with a stopped producer, or retarget a still-live writer's contract.
        hub = self.cached.get("hub", {})
        return bool(self.connected or busy or hub.get("api_reachable") or hub.get("owner_present"))

    def storage_snapshot(self):
        with self.lock:
            value = copy.deepcopy(self.storage)
            value["selection_locked"] = self._storage_locked()
            value["scan_age_s"] = (None if self.storage_scan_at is None else
                                    max(0., time.monotonic() - self.storage_scan_at))
            value["poll_period_s"] = self.STORAGE_POLL_PERIOD_S
            value["record_root"] = str(self.cfg.get("remote_bundle", "")) + "/runs" if value["selected"] else None
            if value["selected"] and self.connected and self.cached.get("usb", {}).get("verified") is not True:
                value.update(status="lost", runtime_ready=False,
                    reason="当前选定 USB 未通过远端身份守卫；保留原目标并拒绝写入，不自动切盘")
            return value

    def _apply_storage(self, candidate):
        from .usb_storage import config_for_storage
        self.cfg = config_for_storage(self.base_cfg, candidate)
        if hasattr(self.transport, "cfg"):
            self.transport.cfg = self.cfg
        self.storage.update(selected=copy.deepcopy(candidate), status="selected",
            runtime_ready=False, reason="已选定此 USB；连接前仍核对源码，未初始化新盘不会开始录制")

    def _scan_storage(self, allow_connect=False):
        if not self.storage_scan_lock.acquire(blocking=False):
            return self.storage_snapshot()
        try:
            with self.lock:
                if self.storage_scan_at is not None and time.monotonic() - self.storage_scan_at < self.STORAGE_POLL_PERIOD_S:
                    return self.storage_snapshot()
                self.storage["scanning"] = True
            inventory = self.transport.discover_storage()
            from .usb_storage import choose_storage
            candidates = []
            for source in inventory.get("candidates", []):
                row = copy.deepcopy(source)
                row.update(name=row.get("display_name") or row.get("label") or row.get("uuid"),
                    capacity_bytes=row.get("size_bytes"), free_bytes=row.get("available_bytes"),
                    reason="；".join(row.get("reasons", [])))
                candidates.append(row)
            with self.lock:
                self.storage_scan_at = time.monotonic()
                self.storage.update(candidates=candidates, checked_at=inventory.get("checked_at"), error=None,
                                    inventory_schema=inventory.get("schema"))
                selected = self.storage.get("selected")
                matching = [r for r in candidates if selected and all(r.get(k) == selected.get(k)
                            for k in ("uuid", "mountpoint", "source", "major_minor", "physical_usb_path"))
                            and r.get("eligible") is True]
                if selected and len(matching) == 1:
                    self.storage["selected"] = matching[0]
                    if self.storage["status"] != "lost":
                        self.storage["status"] = "selected"
                elif selected and self._storage_locked(allow_connect=allow_connect):
                    self.storage.update(status="lost", runtime_ready=False,
                        reason="运行会话选定的 USB 已失去挂载身份；禁止切换到其他盘")
                elif not self._storage_locked(allow_connect=allow_connect):
                    self.storage.update(selected=None, runtime_ready=False)
                    try:
                        chosen = choose_storage(inventory)
                    except (ValueError, RuntimeError) as exc:
                        eligible = [r for r in candidates if r.get("eligible") is True]
                        self.storage.update(status="select_required" if len(eligible) > 1 else "no_usb",
                            reason=("发现多颗可用 USB，请明确选择一颗；不会自动切盘" if len(eligible) > 1 else
                                    "AgileX 尚无已挂载、身份明确且可写的 USB；不会写入内部硬盘"),
                            selection_detail=str(exc))
                    else:
                        chosen = next((r for r in candidates if r.get("uuid") == chosen.get("uuid")
                            and r.get("mountpoint") == chosen.get("mountpoint")), chosen)
                        self._apply_storage(chosen)
                return self.storage_snapshot()
        except Exception as exc:
            with self.lock:
                self.storage_scan_at = time.monotonic()
                self.storage.update(status="error", error=str(exc), runtime_ready=False,
                    reason="USB 扫描失败；没有自动挂载、格式化或写入任何盘")
            return self.storage_snapshot()
        finally:
            with self.lock:
                self.storage["scanning"] = False
            self.storage_scan_lock.release()

    def _storage_poll(self):
        while not self.closed.is_set():
            self._scan_storage()
            self.closed.wait(self.STORAGE_POLL_PERIOD_S)

    def scan_storage(self):
        if not hasattr(self.transport, "discover_storage"):
            raise ValidationError("当前通路不支持 USB 元数据扫描")
        threading.Thread(target=self._scan_storage, daemon=True, name="usb-explicit-scan").start()
        return self.storage_snapshot()

    def select_storage(self, params):
        if set(params) != {"uuid", "mountpoint"} or any(not isinstance(params[k], str) or not params[k] for k in params):
            raise ValidationError("选盘仅接受实际扫描得到的 uuid 与 mountpoint")
        if not self.operation.acquire(blocking=False):
            raise ValidationError("任务尚未完成，不能更改录制目标")
        try:
            with self.lock:
                if self._storage_locked():
                    raise ValidationError("请先结束并确认所有任务、采集服务已停止，再断开连接后选盘")
                if self.storage_scan_at is None or time.monotonic() - self.storage_scan_at > 15.:
                    raise ValidationError("USB 扫描已过期，请先刷新后选盘")
                from .usb_storage import choose_storage
                try:
                    chosen = choose_storage({"schema": self.storage.get("inventory_schema"),
                                            "candidates": self.storage["candidates"]},
                        selected_uuid=params["uuid"], selected_mountpoint=params["mountpoint"])
                except (ValueError, RuntimeError) as exc:
                    raise ValidationError(str(exc)) from exc
                self._apply_storage(chosen)
                return self.storage_snapshot()
        finally:
            self.operation.release()

    def _sync_storage_sources(self, require_absent=False):
        # A child process captures this operation's output without changing the
        # web server's global stdout. The browser never supplies executable code.
        script = ("import json,sys;from pathlib import Path;v=json.load(sys.stdin);"
            "r=Path(v['root']);sys.path.insert(0,str(r));from agilex_control import pc;"
            "raise SystemExit(pc.sync(v['cfg'],r,require_absent=v['require_absent']))")
        process = subprocess.run([__import__("sys").executable, "-X", "utf8", "-B", "-c", script],
            input=json.dumps({"root": str(self.root), "cfg": self.cfg, "require_absent": require_absent}, allow_nan=False),
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        if process.returncode:
            raise RuntimeError(process.stderr[-3000:] or process.stdout[-3000:])
        return parse_receipt(process.stdout)

    def _event(self, message, level="info"):
        with self.lock:
            self.events.append({"id": uuid.uuid4().hex, "at": stamp(), "message": message, "level": level})

    def _poll(self):
        delay = self.STATE_POLL_PERIOD_S
        while not self.closed.wait(delay):
            if not self.connected:
                delay = self.STATE_POLL_PERIOD_S
                continue
            requested_at = time.monotonic()
            try:
                value = self.transport.request("state", timeout=5)
                with self.lock:
                    self.cached = value
                    self.cache_at = requested_at  # Includes RPC elapsed, conservatively.
                    self.state_rpc_duration_s = max(0., time.monotonic() - requested_at)
                    remote_jobs = {j["id"]: j for j in value.get("jobs", [])}
                    for job in self.jobs:
                        if job.get("remote_id") in remote_jobs:
                            update = dict(remote_jobs[job["remote_id"]])
                            update.pop("id", None)
                            job.update(update)
            except Exception as exc:
                with self.lock:
                    self.connected = False
                    for job in self.jobs:
                        if job["state"] in ("running", "queued"):
                            job.update(state="unknown", error="通道失联，远端结果未确认：" + str(exc))
                self._event("SSH 状态通路中断：" + str(exc), "error")
            # Single request at a time. Slow calls consume their period; do not
            # add a fixed sleep after every response or replay missed requests.
            delay = max(0., self.STATE_POLL_PERIOD_S - (time.monotonic() - requested_at))

    def _v2_speed_gate(self):
        # Called with app.lock held. RAM-only: never creates another SSH/CAN owner.
        if self.precision is None:
            return '当前后台未接入V2调速服务'
        if not self.connected or time.monotonic() - self.cache_at > .5:
            return '控制状态未连接或已过期，暂停接受调速'
        if self.cached.get('stop_latch', {}).get('latched') is not False:
            return 'STOP已锁存或状态未核实，不能调速'
        active = [j for j in self.jobs if j.get('state') in ('queued', 'running', 'unknown')]
        if len(active) != 1 or active[0].get('action') != 'v2_deploy' or active[0].get('state') != 'running':
            return '仅当前网页正在运行的V2真机任务可以调速'
        if active[0].get('operator_stop'):
            return '本轮已经请求停止，不能调速'
        return None

    def v2_speed_snapshot(self):
        from .v2_manual_speed import ManualSpeedChannel
        with self.lock:
            value = self.precision.speed_snapshot() if self.precision else ManualSpeedChannel().snapshot()
            reason = self._v2_speed_gate()
            if reason:
                value.update(available=False, reason=reason)
            return value

    def v2_speed_submit(self, data):
        from .v2_manual_speed import validate_request
        try:
            validate_request(data)
            with self.lock:
                reason = self._v2_speed_gate()
                if reason:
                    raise ValidationError(reason)
                return self.precision.speed_submit(data)
        except (RuntimeError, ValueError) as exc:
            raise ValidationError(str(exc)) from exc

    def submit(self, action, params):
        if not isinstance(params, dict):
            raise ValidationError("params 必须是对象")
        from .precision_workbench import ACTIONS as PRECISION_ACTIONS, validate_params
        if action in PRECISION_ACTIONS:
            if self.precision is None:raise ValidationError("当前后台未接入4090精度服务")
            try:validate_params(action,params)
            except RuntimeError as exc:raise ValidationError(str(exc)) from exc
            argv = None
        elif action in LOCAL_ACTIONS:
            if params:
                raise ValidationError("连接／同步不接受其他参数")
            argv = None
        else:
            argv = build_command(action, params, self.cfg)
            if not self.connected:
                raise ValidationError("请先连接 AgileX")
        if not self.operation.acquire(blocking=False):
            raise ValidationError("当前已有任务，停止按钮仍可独立使用")
        with self.lock:
            if any(j["state"] in ("queued", "running") for j in self.jobs) and not action.startswith("task_review_"):
                self.operation.release()
                raise ValidationError("请等待当前任务完成，不并发提交控制任务")
            snapshot = self.snapshot()
            gate = snapshot["capabilities"].get(action, {})
            if gate.get('available') is True and action in ('h50_load', 'h50_shadow', 'h50_acceptance', 'h50_deploy'):
                reason = h50_action_gate(snapshot, self.cfg, action, params)
                gate = {'available': reason is None, 'reason': reason}
            if gate.get("available") is True and action in PRECISION_ACTIONS:
                try:self.precision.validate(snapshot,action,params)
                except RuntimeError as exc:gate={"available":False,"reason":str(exc)}
            if gate.get("available") is not True:
                self.operation.release()
                raise ValidationError(gate.get("reason") or "功能尚未就绪")
            job = {"id": uuid.uuid4().hex[:12], "action": action, "state": "queued", "created_at": stamp(),
                   "params": copy.deepcopy(params), "argv": argv, "result": None, "error": None}
            self.jobs.append(job)
        threading.Thread(target=self._run, args=(job,), daemon=True, name="web-job").start()
        return copy.deepcopy(job)

    def _run(self, job):
        try:
            with self.lock:
                job["state"] = "running"
            action = job["action"]
            from .precision_workbench import ACTIONS as PRECISION_ACTIONS
            if action in PRECISION_ACTIONS:
                result = self.precision.execute(job)
            elif action == "connect":
                if hasattr(self.transport, "discover_storage"):
                    storage = self._scan_storage(allow_connect=True)
                    if storage["status"] != "selected" or storage["selected"] is None:
                        raise ValidationError(storage.get("reason") or "请先选择已挂载的 USB")
                    # Initial code-only preparation happens only on an explicit
                    # connect. Merely inserting/scanning a disk writes nothing.
                    # Existing bundles retain the explicit sync requirement.
                    if storage["selected"].get("runtime_bundle_exists") is False:
                        self._sync_storage_sources(require_absent=True)
                requested_at = time.monotonic()
                value = self.transport.connect()
                with self.lock:
                    self.cached, self.connected, self.cache_at = value, True, requested_at
                    self.storage["runtime_ready"] = True
                result = {"connected": True, "identity": value.get("identity"), "CAN_motion_commands_sent": 0}
            elif action == "disconnect":
                self.transport.close()
                self.connected = False
                result = {"disconnected": True, "note": "断开监控不等于切换遥操或断电"}
            elif action == "sync":
                if self.connected:
                    raise ValidationError("先断开网页服务，再显式同步；不能覆盖运行中的版本")
                if hasattr(self.transport, "discover_storage"):
                    storage = self._scan_storage(allow_connect=True)
                    if storage["status"] != "selected" or storage["selected"] is None:
                        raise ValidationError(storage.get("reason") or "请先选择已挂载的 USB")
                result = self._sync_storage_sources()
                with self.lock:
                    self.storage["runtime_ready"] = False  # Actual connect rechecks the delivered bytes.
            else:
                remote = self.transport.request("submit", action=action, params=job["params"])
                with self.lock:
                    job["remote_id"] = remote["id"]
                    job["state"] = remote["state"]
                self._event("已提交：" + action + "；实际结果等待远端回执")
                return
            with self.lock:
                job.update(state="succeeded", result=result, finished_at=stamp())
        except Exception as exc:
            with self.lock:
                job.update(state="blocked" if isinstance(exc, ValidationError) else "failed", error=str(exc), finished_at=stamp())
            self._event(str(exc), "error")
        finally:
            self.operation.release()

    def stop(self):
        # Independent channel: neither operation mutex nor edited source hashes
        # should prevent latching a stop in an already running remote version.
        with self.lock:
            affected=[j for j in self.jobs if j.get('state') in ('queued','running')]
            for j in affected:j['operator_stop']={'requested_at':stamp(),'confirmed':False,'source':'web_stop_endpoint'}
        cancel_error = None
        try:
            if self.precision is not None:self.precision.cancel()
        except Exception as exc:
            cancel_error = str(exc)
            with self.lock:
                for j in affected:j['operator_stop']['local_cancel_error'] = cancel_error
        finally:
            # Independent STOP must still be attempted after local pipe/IO
            # cancellation fails. Neither failure is hidden by a success claim.
            if not self.connected:
                detail = '；本地取消异常：' + cancel_error if cancel_error else ''
                raise ValidationError("SSH 未连接，无法确认停止送达；已尝试取消本网页运行器，请使用机器人本机停止入口" + detail)
            try:
                result = self.transport.request("stop", timeout=5)
            except Exception as exc:
                if cancel_error:
                    raise RuntimeError('独立STOP请求失败：' + str(exc) + '；本地取消异常：' + cancel_error) from exc
                raise
        if cancel_error and isinstance(result, dict):
            result = dict(result, local_cancel_error=cancel_error)
            self._event('独立STOP回执已收到；本地运行器取消异常保留：' + cancel_error, 'error')
        with self.lock:
            for j in affected:j['operator_stop'].update(confirmed=True,confirmed_at=stamp())
        self._event("停止请求回执已收到；最后已接受目标仍可能完成")
        return result

    def image(self, role):
        if role not in ("front", "right", "left") or not self.connected:
            raise ValidationError("没有已连接的相机通路")
        return base64.b64decode(self.transport.request("image", timeout=3, role=role)["jpeg_b64"], validate=True)

    def close(self):
        self.closed.set()
        if self.precision is not None:self.precision.close()
        if getattr(self, 'semantic_workspace', None) is not None:
            self.semantic_workspace.close()
        if self.connected:
            self.transport.close()


def make_server(app, port=8766):
    web_root = (app.root / "web").resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, value, mime="application/json"):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def video_reply(self, path):
            from .semantic_presentation import byte_range
            size = path.stat().st_size
            try:
                first, last, partial = byte_range(self.headers.get('Range'), size)
            except ValueError:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self.send_response(206 if partial else 200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(last-first+1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if partial:
                self.send_header('Content-Range', f'bytes {first}-{last}/{size}')
            self.end_headers()
            try:
                with path.open('rb') as stream:
                    stream.seek(first)
                    remaining = last-first+1
                    while remaining:
                        block = stream.read(min(262144, remaining))
                        if not block:
                            break
                        self.wfile.write(block)
                        remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {"error": "Host 不匹配"})
            route = unquote(urlparse(self.path).path)
            try:
                if route == "/api/state":
                    return self.reply(200, app.snapshot())
                if route == '/api/v2/speed':
                    return self.reply(200, app.v2_speed_snapshot())
                if route == "/api/storage":
                    return self.reply(200, app.storage_snapshot())
                if route=='/api/presence' or route.startswith('/api/presence/video/') or route.startswith('/api/presence/image/'):
                    presentation=getattr(app,'presence_workspace',None)
                    if presentation is None:return self.reply(404,{'error':'本次未配置人员暂停离线验证'})
                    if route=='/api/presence':return self.reply(200,presentation.snapshot())
                    parts=route.split('/')
                    if len(parts)!=5:raise ValidationError('Invalid reviewed presence video')
                    if parts[3]=='image':
                        data,mime=presentation.image(parts[4])
                        return self.reply(200,data,mime)
                    return self.video_reply(presentation.video(parts[4]))
                if route == '/api/semantic':
                    if app.semantic_workspace is None:
                        return self.reply(200, {'schema': 'tiger-semantic-workspace-v1', 'enabled': False,
                            'error': '本次网页尚未配置语义模型与已审阅录像', 'commands_sent': 0})
                    return self.reply(200, app.semantic_workspace.snapshot())
                if route == '/api/semantic/review' or route.startswith('/api/semantic/video/'):
                    presentation = getattr(app.semantic_workspace, 'presentation', None)
                    if presentation is None:
                        return self.reply(404, {'error': '本次尚未配置已验证MP4与完整离线识别记录'})
                    if route == '/api/semantic/review':
                        return self.reply(200, presentation.snapshot())
                    parts = route.split('/')
                    if len(parts) not in (5,6):
                        raise ValidationError('Invalid reviewed video request')
                    return self.video_reply(presentation.video(parts[4],parts[5] if len(parts)==6 else None))
                if route.startswith('/api/semantic/image/'):
                    if app.semantic_workspace is None:
                        raise ValidationError('语义录像尚未配置')
                    parts = route.split('/')
                    if len(parts) != 7 or not parts[5].isdigit():
                        raise ValidationError('无效的录像图像请求')
                    return self.reply(200, app.semantic_workspace.image(parts[4], int(parts[5]), parts[6]), 'image/jpeg')
                if route.startswith("/api/jobs/"):
                    job = next((j for j in app.snapshot()["jobs"] if j["id"] == route.rsplit("/", 1)[-1]), None)
                    return self.reply(200 if job else 404, {"job": job})
                if route.startswith("/api/image/"):
                    return self.reply(200, app.image(route.rsplit("/", 1)[-1]), "image/jpeg")
                if route.startswith("/api/"):
                    return self.reply(404, {"error": "未知 API"})
                path = (web_root / ("index.html" if route == "/" else route.lstrip("/"))).resolve()
                if not path.is_relative_to(web_root) or not path.is_file():
                    return self.reply(404, {"error": "文件不存在"})
                mime = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".urdf": "application/xml"}.get(path.suffix, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                return self.reply(200, path.read_bytes(), mime)
            except Exception as exc:
                return self.reply(503, {"error": str(exc)})

        def do_POST(self):
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.valid_host() or self.headers.get("Origin") != expected_origin or
                    not secrets.compare_digest(self.headers.get("X-Tiger-Token", ""), app.token)):
                return self.reply(403, {"error": "同源请求与会话令牌校验失败"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384 or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise ValidationError("请求必须为大小不超过 16 KiB 的 JSON")
                data = json.loads(self.rfile.read(size), parse_constant=lambda x: (_ for _ in ()).throw(ValidationError("非有限 JSON 数字")))
                if not isinstance(data, dict):
                    raise ValidationError("请求需要对象")
                if self.path == "/api/stop":
                    if data:
                        raise ValidationError("停止不接受其他参数")
                    return self.reply(200, app.stop())
                if self.path == "/api/jobs":
                    if set(data) != {"action", "params"} or not isinstance(data["action"], str):
                        raise ValidationError("请求仅包含 action 和 params")
                    return self.reply(202, {"job": app.submit(data["action"], data["params"])})
                if self.path == '/api/v2/speed':
                    return self.reply(202, app.v2_speed_submit(data))
                if self.path == "/api/storage/scan":
                    if data:
                        raise ValidationError("USB 扫描不接受路径或其他参数")
                    return self.reply(202, app.scan_storage())
                if self.path == "/api/storage/select":
                    return self.reply(200, app.select_storage(data))
                if self.path in ('/api/semantic/replay', '/api/semantic/cancel'):
                    if app.semantic_workspace is None:
                        raise ValidationError('语义录像尚未配置')
                    if self.path.endswith('/cancel'):
                        if data:
                            raise ValidationError('取消不接受其他参数')
                        return self.reply(200, app.semantic_workspace.cancel())
                    return self.reply(202, app.semantic_workspace.start(data))
                return self.reply(404, {"error": "未知 API"})
            except (ValidationError, ValueError) as exc:
                return self.reply(409, {"error": str(exc)})
            except Exception as exc:
                return self.reply(503, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
