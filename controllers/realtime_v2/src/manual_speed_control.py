"""Bounded, per-run human speed requests; reference-policy acknowledgements only.

No SDK/model/socket imports. The sole control owner freezes a request per packet.
The stdin reader never mutates an accepted plan and EOF retains STOP behavior.
"""
from dataclasses import dataclass
from copy import deepcopy
from collections import Counter
import json
import math
import queue
import re
import threading
import time

SCHEMA = 'tiger-v2-manual-speed-v1'
LIMITS = {'min': .5, 'max': 4., 'default': 1.}
TOKEN = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
MAX_COMMANDS = 4096
MAX_LINE_BYTES = 2048


class SpeedEventOutbox:
    """Bounded event transport: its sole output worker may block, owners never do.

    Delivery means the sink returned, not UI acknowledgement. Failed/undelivered
    events are retained in a bounded receipt; neither queue overflow nor broken
    output is relabeled as completion. No file or stdout operation runs in put.
    """
    def __init__(self, sink, stop_event, *, capacity=256):
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            raise ValueError('Bounded event capacity required')
        self.sink=sink;self.stop_event=stop_event
        self.queue=queue.Queue(maxsize=capacity)
        self.lock=threading.Lock();self.closing=threading.Event()
        self.failed=False;self.accepting=True;self.enqueued=0;self.delivered=0;self.rejected=0
        self.inflight=None;self.errors=[];self.rejected_examples=[]
        self.thread=threading.Thread(target=self._run,name='v2-speed-event-output',daemon=True)
        self.thread.start()

    def _fail_locked(self, reason):
        self.failed=True;self.accepting=False;self.stop_event.set()
        reason=str(reason)[:300]
        if reason not in self.errors and len(self.errors)<8:self.errors.append(reason)

    def fail(self,reason):
        with self.lock:self._fail_locked(reason)

    def publish(self,value):
        # Small protocol records are frozen in RAM before handing them off.
        try:item=deepcopy(value)
        except BaseException as exc:
            with self.lock:
                self.rejected+=1;self._fail_locked('Event freeze failed: '+type(exc).__name__+': '+str(exc))
            return False
        with self.lock:
            if not self.accepting:
                self.rejected+=1
                if len(self.rejected_examples)<8:self.rejected_examples.append(item)
                return False
            try:self.queue.put_nowait(item)
            except queue.Full:
                self.rejected+=1
                if len(self.rejected_examples)<8:self.rejected_examples.append(item)
                self._fail_locked('Bounded speed event queue exhausted')
                return False
            self.enqueued+=1
        return True

    def _run(self):
        while True:
            with self.lock:
                if self.failed:return
            try:item=self.queue.get(timeout=.02)
            except queue.Empty:
                if self.closing.is_set():return
                continue
            with self.lock:self.inflight=item
            try:self.sink(item)
            except BaseException as exc:
                with self.lock:self._fail_locked('Event output failed: '+type(exc).__name__+': '+str(exc))
                return
            with self.lock:
                self.delivered+=1;self.inflight=None
            self.queue.task_done()

    def shutdown(self,timeout_s=.25):
        if type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or not 0<=timeout_s<=2:
            raise ValueError('Event shutdown timeout must be in0..2 seconds')
        with self.lock:self.accepting=False
        self.closing.set()
        self.thread.join(timeout_s)
        if self.thread.is_alive():self.fail('Event output did not drain before shutdown deadline')
        return self.receipt()

    def receipt(self):
        with self.lock:
            with self.queue.mutex:pending=deepcopy(list(self.queue.queue))
            return {'schema':'tiger-v2-speed-event-outbox-v1','capacity':self.queue.maxsize,
                'enqueued':self.enqueued,'delivered_to_sink':self.delivered,'rejected':self.rejected,
                'worker_alive':self.thread.is_alive(),'complete':not self.failed and not pending and
                    self.inflight is None and self.enqueued==self.delivered,
                'failure_reasons':list(self.errors),'undelivered_events':pending,
                'inflight_event':deepcopy(self.inflight),'rejected_examples':deepcopy(self.rejected_examples),
                'delivery_is_not_browser_ack':True,'storage':'BOUNDED_RAM_UNTIL_SESSION_FINALIZATION'}


def valid_scale(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not LIMITS['min'] <= value <= LIMITS['max']:
        raise ValueError('Target reference scale must be a finite number in0.5..4.0')
    return float(value)


def parse_line(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_LINE_BYTES:
        raise ValueError('Bounded UTF8 JSON line required')
    def pairs(items):
        out = {}
        for k,v in items:
            if k in out: raise ValueError('Duplicate JSON key')
            out[k] = v
        return out
    def bad(_): raise ValueError('Nonfinite JSON')
    return json.loads(raw.decode('utf8'), object_pairs_hook=pairs, parse_constant=bad)


@dataclass(frozen=True)
class FrozenSpeed:
    command_id: str | None
    target_scale: float
    runtime_received_ns: int | None
    frozen_ns: int
    request_index: int

    def as_dict(self):
        return dict(command_id=self.command_id,target_scale=self.target_scale,
                    runtime_received_ns=self.runtime_received_ns,
                    frozen_ns=self.frozen_ns,request_index=self.request_index)


class ManualSpeedControl:
    def __init__(self, run_id, *, emit, stop_event, clock_ns=time.monotonic_ns):
        if not isinstance(run_id,str) or not TOKEN.fullmatch(run_id):
            raise ValueError('Valid bound runtime session required')
        self.run_id,self.emit,self.stop_event,self.clock_ns=run_id,emit,stop_event,clock_ns
        self.lock=threading.RLock()
        self.available=False;self.closed=False;self.latest_id=None
        self.records={};self.rejections=0
        self.applied_target_scale=1.;self.committed_target_scale=1.
        self._to_activate=[]
        self.input_failure=None

    def start(self):
        with self.lock:
            if self.closed or self.stop_event.is_set():raise RuntimeError('Speed channel is stopped')
            self.available=True
            self.emit({'schema':SCHEMA,'event':'V2_SPEED_READY','run_id':self.run_id,
                       'limits':dict(LIMITS),'target_scale':1.,'applied_target_scale':1.,
                       'semantics':'TIMEQP_TARGET_PREFERENCE_NOT_GUARANTEED_PHYSICAL_MULTIPLIER'})

    def _event(self,record,status,**extra):
        record['status']=status
        record.update(extra)
        now=self.clock_ns()
        item={'schema':SCHEMA,'event':'V2_SPEED_STATUS','run_id':self.run_id,
              'command_id':record['command_id'],'target_scale':record['target_scale'],
              'status':status,'applied_target_scale':self.applied_target_scale,
              'committed_target_scale':self.committed_target_scale,
              'runtime_received_ns':record['runtime_received_ns'],'runtime_event_ns':now,
              'plan_committed_ns':record.get('plan_committed_ns'),
              'reference_effective_ns':record.get('reference_effective_ns'),
              'latency':{},'semantics':'REFERENCE_POLICY_ONLY_NOT_MEASURED_ROBOT_SPEED'}
        for name in ('reason','effective_reference_phase_ns','activation_due_ns','local_scale_stats','request_index',
                     'reference_sample_ns','writer_ack_completed_ns'):
            if name in record:item[name]=deepcopy(record[name])
        received=record['runtime_received_ns']
        if record.get('plan_committed_ns') is not None:
            item['latency']['plan_commit_ms']=(record['plan_committed_ns']-received)/1e6
        if record.get('reference_effective_ns') is not None:
            item['latency']['reference_switch_ms']=(record['reference_effective_ns']-received)/1e6
        if record.get('reference_sample_ns') is not None:
            item['latency']['reference_sample_ms']=(record['reference_sample_ns']-received)/1e6
        history=record.setdefault('history',[])
        # A command emits at most six distinct lifecycle transitions.
        if len(history)<8:history.append({'status':status,'runtime_ns':now})
        record['last_event']=deepcopy(item)
        self.emit(item)
        return item

    def receive(self,value):
        with self.lock:
            if not isinstance(value,dict) or set(value)!={'schema','event','run_id','command_id','target_scale'}:
                raise ValueError('Exact manual-speed protocol fields required')
            if value['schema']!=SCHEMA or value['event']!='V2_SPEED_SET':
                raise ValueError('Unsupported speed protocol')
            if value['run_id']!=self.run_id:raise ValueError('Stale/foreign speed run ID')
            cid=value['command_id']
            if not isinstance(cid,str) or not TOKEN.fullmatch(cid):raise ValueError('Invalid speed command ID')
            scale=valid_scale(value['target_scale'])
            if not self.available or self.closed or self.stop_event.is_set():
                raise ValueError('No running speed-capable session')
            old=self.records.get(cid)
            if old is not None:
                if old['target_scale']!=scale:raise ValueError('Command ID reused with different target')
                self.emit(deepcopy(old['last_event']))
                return deepcopy(old['last_event'])
            if len(self.records)>=MAX_COMMANDS:raise ValueError('This run reached its bounded speed-command capacity')
            previous=self.records.get(self.latest_id)
            if previous is not None and previous['status']=='RECEIVED':
                self._event(previous,'SUPERSEDED',reason='Newer requested target received before packet freeze')
            row={'command_id':cid,'target_scale':scale,'runtime_received_ns':self.clock_ns()}
            self.records[cid]=row;self.latest_id=cid
            return self._event(row,'RECEIVED')

    def reject(self,value,error):
        with self.lock:
            self.rejections+=1
            # Rejections do not modify the reference or cancel an earlier valid request.
            self.emit({'schema':SCHEMA,'event':'V2_SPEED_STATUS','run_id':self.run_id,
                'command_id':value.get('command_id') if isinstance(value,dict) else None,
                'target_scale':value.get('target_scale') if isinstance(value,dict) and
                    type(value.get('target_scale')) in (float,int) and math.isfinite(value['target_scale']) else None,
                'status':'REJECTED','reason':str(error)[:240],
                'applied_target_scale':self.applied_target_scale,
                'runtime_received_ns':self.clock_ns()})

    def freeze_for_request(self,request_index=0):
        with self.lock:
            if self.closed or self.stop_event.is_set():raise RuntimeError('STOP prevents new speed contexts')
            now=self.clock_ns()
            row=self.records.get(self.latest_id)
            if row is None:return FrozenSpeed(None,1.,None,now,request_index).as_dict()
            if row['status']=='RECEIVED':
                self._event(row,'FROZEN',frozen_ns=now,request_index=request_index)
            return FrozenSpeed(row['command_id'],row['target_scale'],row['runtime_received_ns'],
                               row.get('frozen_ns',now),request_index).as_dict()

    def mark_committed(self,snapshot,committed_ns,effective_phase_ns,activation_due_ns=None,local_scale_stats=None):
        with self.lock:
            cid=snapshot.get('command_id')
            if cid is None:return
            row=self.records.get(cid)
            if row is None or row['target_scale']!=snapshot['target_scale']:
                raise RuntimeError('Committed speed context lost its bound command')
            if row.get('plan_committed_ns') is not None:return
            if self.closed or self.stop_event.is_set():raise RuntimeError('Cannot acknowledge a commit after STOP')
            if row['status']!='FROZEN' or committed_ns<row['runtime_received_ns']:
                raise RuntimeError('Speed commit must follow frozen request')
            self.committed_target_scale=row['target_scale']
            row.update(plan_committed_ns=int(committed_ns),effective_reference_phase_ns=int(effective_phase_ns),
                       activation_due_ns=activation_due_ns,local_scale_stats=deepcopy(local_scale_stats))
            self._to_activate.append(cid)
            self._event(row,'COMMITTED')

    def advance_reference(self,now_ns,reference_phase_ns,*,reference_sample_ns=None):
        """Call after an actually successful writer acknowledgement; not after prepare."""
        with self.lock:
            if self.closed or self.stop_event.is_set() or type(reference_phase_ns) is not int:return
            if reference_sample_ns is None:reference_sample_ns=now_ns
            if type(now_ns) is not int or type(reference_sample_ns) is not int or not 0<=reference_sample_ns<=now_ns:
                raise ValueError('Actual writer ACK time must follow reference sample time')
            remaining=[]
            for cid in self._to_activate:
                row=self.records[cid]
                due=row.get('activation_due_ns')
                if (reference_phase_ns<row['effective_reference_phase_ns'] or
                    now_ns<row['plan_committed_ns'] or (due is not None and now_ns<due)):
                    remaining.append(cid);continue
                self.applied_target_scale=row['target_scale']
                self._event(row,'REFERENCE_ACTIVE',reference_effective_ns=int(now_ns),
                    writer_ack_completed_ns=int(now_ns),reference_sample_ns=int(reference_sample_ns))
            self._to_activate=remaining

    def close(self,reason='Window ended'):
        with self.lock:
            if self.closed:return
            self.closed=True;self.available=False
            self.emit({'schema':SCHEMA,'event':'V2_SPEED_STATUS','run_id':self.run_id,
                       'status':'CLOSED','reason':reason,'command_id':self.latest_id,
                       'applied_target_scale':self.applied_target_scale})
            self._to_activate=[]

    def input_failed(self,reason):
        # STOP is independent of diagnostic delivery. Production emit is only
        # outbox.publish, so a closed/broken stdout cannot trap this owner.
        self.stop_event.set()
        with self.lock:
            self.input_failure=str(reason)[:300]
            self.close('Owned stdin failed: '+self.input_failure)

    def receipt(self):
        with self.lock:
            return {'schema':SCHEMA,'run_id':self.run_id,'closed':self.closed,'limits':dict(LIMITS),
                    'command_count':len(self.records),'rejected_messages':self.rejections,
                    'max_commands':MAX_COMMANDS,'applied_target_scale':self.applied_target_scale,
                    'statuses':dict(Counter(r['status'] for r in self.records.values())),
                    'commands':deepcopy(list(self.records.values())),
                    'input_failure':self.input_failure,
                    'latency_clock':'runtime monotonic only','physical_speed_verified':False}


def stdin_control_loop(fd,channel,stop_event,read=None):
    """Consume bounded lines; EOF still requests the same existing STOP."""
    import os,select
    read=read or os.read
    buffer=b'';discard=False
    try:
        while not stop_event.is_set() and not channel.closed:
            if os.name != 'nt':
                ready,_,_=select.select([fd],[],[],.2)
                if not ready:continue
            raw=read(fd,1024)
            if not raw:
                stop_event.set();channel.close('Owned stdin EOF');return
            for byte in raw:
                if byte==10:
                    if not discard and buffer:
                        value=None
                        try:value=parse_line(buffer);channel.receive(value)
                        except (ValueError,UnicodeError) as exc:channel.reject(value,exc)
                    buffer=b'';discard=False
                elif not discard:
                    buffer+=bytes((byte,))
                    if len(buffer)>MAX_LINE_BYTES:
                        channel.reject(None,'Speed command exceeds bounded line length')
                        buffer=b'';discard=True
    except BaseException as exc:
        # Includes select/read failures: the daemon must not disappear while
        # leaving the motion owner unaware of its lost command/EOF channel.
        stop_event.set()
        try:channel.input_failed(type(exc).__name__+': '+str(exc))
        except BaseException:
            # Diagnostics can fail, but failure propagation is already set.
            stop_event.set()

