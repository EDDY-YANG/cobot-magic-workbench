"""RAM-only control channel for one owned V2 subprocess; no hardware or network IO."""
from copy import deepcopy
import math
import re
import time

SCHEMA = 'tiger-v2-manual-speed-v1'
LIMITS = {'min': 0.5, 'max': 4.0, 'default': 1.0}
STATUSES = {'RECEIVED', 'FROZEN', 'COMMITTED', 'REFERENCE_ACTIVE',
            'REJECTED', 'SUPERSEDED', 'CLOSED'}
_TOKEN = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
MAX_COMMANDS = 4096
ACK_TIMEOUT_NS = 2_000_000_000


def validate_request(value):
    if not isinstance(value, dict) or set(value) != {'run_id', 'command_id', 'target_scale'}:
        raise ValueError('调速请求仅允许 run_id、command_id、target_scale')
    for name in ('run_id', 'command_id'):
        if not isinstance(value[name], str) or not _TOKEN.fullmatch(value[name]):
            raise ValueError('无效的调速 ' + name)
    scale = value['target_scale']
    if type(scale) not in (int, float) or not math.isfinite(scale) or not LIMITS['min'] <= scale <= LIMITS['max']:
        raise ValueError('V2目标倍率必须为0.5至4.0的有限数字')
    return value


class ManualSpeedChannel:
    """Caller holds RealV2IO.lock. All latency calculations use the local clock.

    Runtime timestamps are retained as evidence only. They are never subtracted
    from this backend's monotonic clock. Unacknowledged sends are limited to one,
    so a stalled runtime cannot fill stdin or grow an unbounded command queue.
    """
    def __init__(self, clock_ns=time.monotonic_ns):
        self.clock_ns = clock_ns
        self.begin(False)

    def begin(self, deploy):
        self.deploy = deploy is True
        self.open = False
        self.run_id = None
        self.latest_id = None
        self.applied_scale = 1.0
        self.committed_scale = 1.0
        self.last_applied_seq = 0
        self.last_committed_seq = 0
        self.records = {}
        self.reason = '等待当前V2运动运行器调速握手' if self.deploy else '当前没有本后台拥有的V2运动任务'
        self.status = 'WAITING' if self.deploy else 'UNAVAILABLE'
        self.started_ns = self.clock_ns()
        self.handshake_latency_ms = None
        self.ack_timed_out = False

    def _expire_ack(self):
        record = self.records.get(self.latest_id)
        if (self.open and record is not None and record['ack_received_ns'] is None
                and self.clock_ns() - record['sent_ns'] > ACK_TIMEOUT_NS):
            self.ack_timed_out = True
            self.close('调速超过2秒未收到运行器确认；命令可能已执行、结果未确认；不会自动重发或恢复调速')

    def close(self, reason='V2运行器已结束'):
        self.open = False
        self.reason = reason
        self.status = 'CLOSED'

    def consume(self, value):
        """Return true for protocol events, including rejected/malformed events.

        They must never become the main run's final completion receipt.
        """
        event = value.get('event') if isinstance(value, dict) else None
        if not isinstance(event, str) or not event.startswith('V2_SPEED_'):
            return False
        if event not in ('V2_SPEED_READY', 'V2_SPEED_STATUS'):
            self.close('V2调速事件类型不匹配')
            return True
        if value.get('schema') != SCHEMA:
            self.close('V2调速协议版本不匹配')
            return True
        run_id = value.get('run_id')
        if not isinstance(run_id, str) or not _TOKEN.fullmatch(run_id):
            self.close('V2调速运行身份无效')
            return True
        if value['event'] == 'V2_SPEED_READY':
            if not self.deploy or self.status == 'CLOSED':
                return True
            if self.run_id is not None:
                if self.run_id != run_id:
                    self.close('同一子进程调速运行身份发生变化')
                return True
            limits = value.get('limits', {})
            if not isinstance(limits, dict) or any(type(limits.get(k)) not in (int, float)
                    or limits[k] != v for k, v in LIMITS.items()):
                self.close('V2调速范围合同不匹配')
                return True
            self.run_id, self.open, self.status = run_id, True, 'READY'
            self.reason = None
            self.handshake_latency_ms = (self.clock_ns() - self.started_ns) / 1e6
            return True
        self._expire_ack()
        if run_id != self.run_id:
            return True
        if not self.open:
            # A timeout is not evidence that a command was never executed.
            # Retain a late receipt as diagnostic evidence without reopening.
            record = self.records.get(value.get('command_id'))
            if (self.ack_timed_out and record is not None
                    and value.get('status') in STATUSES
                    and value.get('target_scale') == record['request']['target_scale']):
                record['late_runtime'] = deepcopy(value)
            return True
        status = value.get('status')
        if status not in STATUSES:
            self.close('V2调速状态无效')
            return True
        command_id = value.get('command_id')
        if status == 'CLOSED':
            self.close(value.get('reason') or 'V2运行器关闭调速通道')
            return True
        record = self.records.get(command_id)
        if record is None:
            return True
        if value.get('target_scale') != record['request']['target_scale']:
            self.close('V2调速回执倍率与请求不一致')
            return True
        now = self.clock_ns()
        if record['ack_received_ns'] is None:
            record['ack_received_ns'] = now
            if status == 'RECEIVED':
                record['ack_rtt_ms'] = (now - record['sent_ns']) / 1e6
        record['runtime'] = deepcopy(value)
        record['status'] = status
        applied = value.get('applied_target_scale')
        if status == 'COMMITTED' and record['seq'] >= self.last_committed_seq:
            self.committed_scale = record['request']['target_scale']
            self.last_committed_seq = record['seq']
        if status == 'REFERENCE_ACTIVE':
            if type(applied) not in (int, float) or not math.isfinite(applied) or not LIMITS['min'] <= applied <= LIMITS['max']:
                self.close('V2参考生效倍率无效')
                return True
            if record['seq'] >= self.last_applied_seq:
                self.applied_scale = applied
                self.last_applied_seq = record['seq']
        if command_id == self.latest_id:
            self.status = status
            self.reason = value.get('reason')
        return True

    def submit(self, data, write_line):
        """At most one send attempt per ID; duplicates return their first ack."""
        validate_request(data)
        self._expire_ack()
        if not self.open or not self.deploy:
            raise ValueError(self.reason or 'V2调速通道未就绪')
        if data['run_id'] != self.run_id:
            raise ValueError('调速运行ID已过期，不能跨轮提交')
        previous = self.records.get(data['command_id'])
        if previous:
            if previous['request'] != data:
                raise ValueError('相同调速命令ID不能更改倍率')
            return deepcopy(previous['http_ack'])
        if len(self.records) >= MAX_COMMANDS:
            raise ValueError('本轮调速命令数已达到上限')
        if any(r['ack_received_ns'] is None for r in self.records.values()):
            raise ValueError('上一条调速命令尚未收到运行器确认；不会自动重发')
        now = self.clock_ns()
        wire = dict(data, schema=SCHEMA, event='V2_SPEED_SET')
        # First HTTP response only means local pipe accepted the command.
        # Runtime receipt / reference application are separate facts.
        ack = {'schema': SCHEMA, 'run_id': self.run_id, 'command_id': data['command_id'],
               'target_scale': data['target_scale'], 'status': 'SENT',
               'accepted': True, 'runtime_acknowledged': False,
               'backend_sent_ns': now, 'automatic_retry': False}
        record = {'request': deepcopy(data), 'http_ack': ack, 'sent_ns': now,
                  'ack_received_ns': None, 'ack_rtt_ms': None, 'status': 'SENT', 'runtime': {},
                  'seq': len(self.records) + 1}
        self.records[data['command_id']] = record
        self.latest_id = data['command_id']
        try:
            write_line(wire)
        except Exception:
            record['http_ack'] = dict(ack, accepted=False, status='SEND_UNKNOWN')
            self.close('调速发送失败或结果未确认；禁止自动重发')
            raise
        self.status, self.reason = 'SENT', None
        return deepcopy(ack)

    def snapshot(self):
        self._expire_ack()
        record = self.records.get(self.latest_id, {})
        runtime = record.get('runtime', {})
        latency = deepcopy(runtime.get('latency')) if isinstance(runtime.get('latency'), dict) else {}
        latency.update(backend_ack_ms=record.get('ack_rtt_ms'),
                       ack_wait_ms=((self.clock_ns() - record['sent_ns']) / 1e6
                                    if record and record.get('ack_received_ns') is None else None),
                       handshake_ms=self.handshake_latency_ms,
                       clock_scope='backend RTT and runtime-local intervals; no cross-host clock subtraction')
        return {'schema': SCHEMA, 'available': self.open and self.deploy,
                'run_id': self.run_id, 'command_id': self.latest_id,
                'target_scale': record.get('request', {}).get('target_scale', 1.0),
                'applied_target_scale': self.applied_scale,
                'committed_target_scale': self.committed_scale,
                'has_committed_reference': self.last_committed_seq > 0,
                'has_applied_reference': self.last_applied_seq > 0,
                'local_scale_stats': deepcopy(runtime.get('local_scale_stats')),
                'pending': self.open and self.status in ('SENT', 'RECEIVED', 'FROZEN', 'COMMITTED'),
                'status': 'ACK_TIMEOUT' if self.ack_timed_out else self.status,
                'reason': self.reason, 'latency': latency,
                'limits': deepcopy(LIMITS),
                'runtime_received_ns': runtime.get('runtime_received_ns'),
                'plan_committed_ns': runtime.get('plan_committed_ns'),
                'reference_effective_ns': runtime.get('reference_effective_ns'),
                'ack_timeout': self.ack_timed_out,
                'delivery_result': 'UNKNOWN_MAY_HAVE_EXECUTED' if self.ack_timed_out else None,
                'late_runtime_status': record.get('late_runtime', {}).get('status'),
                'runtime_acknowledged': record.get('ack_received_ns') is not None}

