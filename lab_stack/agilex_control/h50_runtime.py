"""Recorded FP32/H50 model access and an exclusive, live model lease.

No hardware writer exists here. A prediction is not an execution receipt.
The existing SSH forward authenticates the 4090 peer; control stays loopback.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid

from .remote_fp32 import (FP32ShadowClient, ShadowError, validate_health as _validate_health,
                         validate_snapshot, validate_recording, validate_result)
from .time_contract import capture_clock_domain
from .h50_approved_contract import require_approved_config, APPROVED_CONTRACT, APPROVED_CONTRACT_SHA256, acceptance_semantics

STEPS = (2000, 5000, 10000, 15000, 20000)
SEMANTICS=acceptance_semantics()
PLAN_PREPARATION_SCOPE='DIAGNOSTIC_CHAIN_REAL_PLANS_NO_PHYSICAL_COMMIT'


def robot_preparation_source():
    """Bind the no-motion preparation audit to the local executor/core sources."""
    root=Path(__file__).resolve().parent
    return {'h50_deployment_sha256':hashlib.sha256((root/'h50_deployment.py').read_bytes()).hexdigest(),
            'h50_async_sha256':hashlib.sha256((root/'h50_async.py').read_bytes()).hexdigest(),
            'approved_contract_sha256':APPROVED_CONTRACT_SHA256,
            'recording_sha256':hashlib.sha256((root/'recording.py').read_bytes()).hexdigest(),
            'hub_sha256':hashlib.sha256((root/'hub.py').read_bytes()).hexdigest()}
RTC_PARAMETERS={'native_training_hz':APPROVED_CONTRACT['native_training_hz'],
    'execution_label_hz':APPROVED_CONTRACT['action_hz'],
    'freeze_steps':APPROVED_CONTRACT['rtc_freeze_steps'], 'beta':APPROVED_CONTRACT['rtc_beta']}
COMMIT_NS=APPROVED_CONTRACT['reply_deadline_ms']*1_000_000


def _require(value, message):
    if not value:
        raise RuntimeError('H50 runtime: ' + message)


def validate_health(health, step):
    _validate_health(health, expected_step=step)
    rtc=health.get('rtc', {})
    expected={'schema':'tiger-fp32-h50-rtc-v1','enabled':True,'precision_audit_passed':True,
              **RTC_PARAMETERS,'previous_valid_steps':APPROVED_CONTRACT['rtc_known_steps']}
    _require(all(type(rtc.get(k)) is type(v) and rtc[k]==v for k,v in expected.items()), 'native H50/FP32 RTC contract or audit missing')
    for key in ('implementation_sha256','precision_audit_sha256'):
        _require(isinstance(rtc.get(key),str) and re.fullmatch('[0-9a-f]{64}',rtc[key]), 'RTC identity missing: '+key)
    binding=health.get('acceptance_binding')
    _require(isinstance(binding,dict) and isinstance(binding.get('identity'),dict) and
             re.fullmatch('[0-9a-f]{64}',str(binding.get('contract_fingerprint',''))), 'acceptance binding missing')
    identity=binding['identity']
    _require(binding.get('semantics')==SEMANTICS,'deferred USB timing/storage semantics missing')
    for key in ('normalization_sha256','tokenizer_sha256','source_commit'):
        _require(identity.get(key)==health['policy'][key],'acceptance/model identity differs: '+key)
    for key in ('rtc_implementation_sha256','rtc_precision_audit_sha256','fp32_loader_sha256','worker_entry_sha256','supervisor_sha256'):
        _require(re.fullmatch('[0-9a-f]{64}',str(identity.get(key,''))),'acceptance code identity missing: '+key)
    _require(identity['rtc_implementation_sha256']==rtc['implementation_sha256'] and
             identity['rtc_precision_audit_sha256']==rtc['precision_audit_sha256'],'RTC audit binding differs')
    return _fingerprint({'contract':health['contract'],'policy':health['policy'],'rtc':rtc,'acceptance_binding':binding})


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate_acceptance(value, health, step):
    validate_health(health,step)
    _require(isinstance(value,dict) and value.get('schema')=='tiger-fp32-h50-rtc-acceptance-v1' and
             value.get('status')=='PASS' and value.get('step')==step, 'selected checkpoint has no passing RTC acceptance')
    _require(value.get('plan_preparation_scope')==PLAN_PREPARATION_SCOPE,
             'HOLD-only acceptance does not cover real tail-plan preparation')
    _require(value.get('robot_preparation_source')==robot_preparation_source(),
             'acceptance is stale for the local executor/core/approved contract')
    binding=health['acceptance_binding']
    _require(value.get('identity')==binding['identity'] and value.get('contract_fingerprint')==binding['contract_fingerprint'],
             'acceptance is stale for the current model/code/precision')
    _require(value.get('semantics')==SEMANTICS,'acceptance uses a different persistence/clock contract')
    expected={'warmup_calls':APPROVED_CONTRACT['acceptance_warmup_calls'],
              'measured_calls':APPROVED_CONTRACT['acceptance_measured_calls'],
              'p95_budget_ms':APPROVED_CONTRACT['acceptance_p95_ms'],
              'max_budget_ms':APPROVED_CONTRACT['acceptance_max_ms'],
              'whole_gpu_budget_mib':APPROVED_CONTRACT['whole_gpu_budget_mib']}
    _require(all(value.get('requirements',{}).get(k)==v for k,v in expected.items()), 'acceptance requirements changed')
    r=value.get('results',{})
    _require(r.get('timing_basis')==SEMANTICS['timing_basis'] and r.get('usb_finalflush_validated') is True,
             'RAM control latency and completed USB persistence must both be verified')
    _require(r.get('mode')=='native32_bootstrap_then_rtc' and
             type(r.get('native_bootstrap_calls')) is int and type(r.get('rtc_continuation_calls')) is int and
             r.get('native_bootstrap_calls')==1 and r.get('rtc_continuation_calls')==102 and
             r.get('bootstrap_accepted_before_deadline') is True and r.get('diagnostic_reference_chain_validated') is True and r.get('warmup_calls')==3 and r.get('measured_calls')==100 and
             r.get('vjp_enabled_calls')==100, 'RTC sample counts not verified')
    for key in ('all_actions_float32_50x7','zero_weight_same_rng_noise_equivalent','robot_chain_audits_passed'):
        _require(r.get(key) is True,'missing acceptance evidence: '+key)
    for number,limit in ((r.get('latency_ms',{}).get('p95'),expected['p95_budget_ms']),
                        (r.get('latency_ms',{}).get('max'),expected['max_budget_ms']),
                        (r.get('whole_gpu_peak_mib'),expected['whole_gpu_budget_mib'])):
        _require(type(number) in (int,float) and math.isfinite(number) and 0<number<=limit,'approved latency/GPU budget not met')
    return deepcopy(value)


class _H50Client(FP32ShadowClient):
    decision = 'PREDICTED_NOT_DISPATCHED_BY_CLIENT'
    success_status = 'DEPLOYMENT_PREDICTION_BUFFERED'
    result_schema = 'tiger-h50-deployment-prediction-v1'

    def __init__(self, step, credentials, mode):
        super().__init__('http://127.0.0.1:18883', timeout_s=3.)
        self.step, self.credentials = step, credentials
        if mode == 'shadow':
            self.decision = 'SHADOW_NOT_DISPATCHED'
            self.success_status = 'SHADOW_INFERENCE_RECORDED'
            self.result_schema = 'tiger-fp32-shadow-result-v1'

    def _validate_health(self, health):
        return validate_health(health, self.step)

    def _request_headers(self, method, path):
        if path in ('/infer','/infer_rtc','/acceptance/submit'):
            session, token = self.credentials()
            _require(session is not None and token is not None, 'inference requires the active lease')
            return {'X-Tiger-Deployment-Session': session,
                    'X-Tiger-Deployment-Lease': token}
        return {}


class ModelRuntime:
    """One selected checkpoint, one lease, no model inference retries.

    The lease token is kept in RAM and deliberately absent from receipts.
    Sender check_alive reads only RAM. Slow HTTP runs in a separate heartbeat.
    """
    def __init__(self, cfg, step, mode='deployment'):
        settings = require_approved_config(cfg)
        _require(settings.get('enabled') is True and settings.get('model_port') == 18883,
                 'explicit 4090 deployment endpoint required')
        _require(type(step) is int and step in STEPS, 'unsupported checkpoint')
        _require(mode in ('deployment', 'shadow'), 'unsupported prediction scope')
        self.cfg, self.step, self.mode = deepcopy(cfg), step, mode
        self.failed_event, self._closing = threading.Event(), threading.Event()
        self.failure = None
        self._session_id = self._lease_token = self._fingerprint = None
        self._acceptance_binding = None
        self._worker = None
        self.trial_index = None
        self.request_index = 0
        self._infer_lock = threading.Lock()
        self.client = _H50Client(step, self._credentials, mode)
        self.api = FP32ShadowClient('http://127.0.0.1:18883', timeout_s=.8)
        self._domain = capture_clock_domain()
        _require(self._domain.get('identity_verified') is True, 'local source clock identity missing')

    def _credentials(self):
        return self._session_id, self._lease_token

    def _call(self, method, path, payload=None, *, statuses=(200,)):
        return self.api._request(method, path, payload, expected_statuses=statuses)

    def ensure_ready(self, timeout_s=180.):
        _require(self._session_id is None, 'cannot switch a leased runtime')
        _require(type(timeout_s) in (int, float) and 0 < timeout_s <= 300, 'invalid loading deadline')
        self._call('POST', '/select', {'step': self.step}, statuses=(200, 202))
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            status = self._call('GET', '/status')
            _require(status.get('status') != 'FAILED', 'model selection failed: ' + str(status.get('error')))
            if status.get('status') == 'READY' and status.get('selected_step') == self.step:
                health = self._call('GET', '/health')
                self._fingerprint = validate_health(health, self.step)
                self._acceptance_binding = deepcopy(health['acceptance_binding'])
                return health
            self._closing.wait(.25)
            _require(not self._closing.is_set(), 'loading cancelled')
        raise RuntimeError('H50 runtime: selected model did not become ready before deadline')

    def acquire(self, session_id, trial_index=0):
        _require(self._session_id is None and self._fingerprint is not None, 'ready runtime without an existing lease required')
        _require(isinstance(session_id, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', session_id),
                 'explicit deployment session identity required')
        _require(type(trial_index) is int and 0<=trial_index<=1000000,'invalid trial index')
        _require(self._acceptance_binding is not None,'ready model contract binding required')
        response = self._call('POST', '/lease/acquire', {'session_id': session_id, 'step': self.step,'trial_index':trial_index})
        token = response.get('lease_token')
        _require(isinstance(token, str) and 16 <= len(token) <= 256 and '\n' not in token, 'invalid lease credential')
        _require(response.get('step') == self.step and response.get('session_id') == session_id,
                 'lease identity mismatch')
        _require(response.get('ttl_s') == 2 and response.get('heartbeat_interval_s') == .5,
                 'lease timing contract mismatch')
        _require(response.get('trial_index')==trial_index and response.get('trial_seed')==42+trial_index and
                 response.get('contract_fingerprint')==self._acceptance_binding['contract_fingerprint'],
                 'lease trial RNG or model contract mismatch')
        self._session_id, self._lease_token = session_id, token
        self.trial_index,self.request_index=trial_index,0
        self._worker = threading.Thread(target=self._heartbeat, name='h50-model-lease', daemon=True)
        self._worker.start()
        return {'session_id': session_id, 'step': self.step, 'ttl_s': 2,
                'heartbeat_interval_s': .5, 'trial_index':trial_index,'trial_seed':42+trial_index,'credential_saved': False}

    def _heartbeat(self):
        while not self._closing.wait(.5):
            try:
                response = self._call('POST', '/lease/heartbeat',
                    {'session_id': self._session_id, 'lease_token': self._lease_token})
                _require(response.get('step', self.step) == self.step, 'lease checkpoint changed')
            except Exception as exc:
                # HTTP errors do not echo request headers or the lease token.
                self.failure = type(exc).__name__ + ': model lease heartbeat failed'
                self.failed_event.set()
                return

    def check_alive(self):
        _require(self._session_id is not None and not self._closing.is_set(), 'runtime has no live lease')
        _require(not self.failed_event.is_set(), self.failure or 'model lease is unavailable')

    def check_health(self):
        self.check_alive()
        return self.client.check_health()

    def run_once(self, snapshot, prompt, journal, **kwargs):
        self.check_alive()
        result = self.client.run_once(snapshot, prompt, journal, **kwargs)
        self.check_alive()
        _require(result.get('policy_fingerprint') == self._fingerprint, 'model identity changed after lease acquisition')
        return result

    def infer(self, snapshot, prompt, journal):
        self.check_alive()
        from .hub import rpc
        result = self.client.run_once(snapshot, prompt, journal, local_clock_domain=self._domain,
            recording_guard=lambda: rpc(self.cfg, '/recording/fp32-binding', timeout=1))
        self.check_alive()
        _require(result.get('policy_fingerprint') == self._fingerprint, 'model identity changed after lease acquisition')
        return result

    def require_acceptance(self):
        health=self._call('GET','/health')
        value=self._call('GET','/acceptance?step='+str(self.step))
        return validate_acceptance(value,health,self.step)

    def submit_acceptance(self, candidate):
        """Publish measured evidence under a new lease, without dispatching actions.

        The manager recomputes PASS/FAIL using its actual call ledger and NVML
        evidence. A rejected or failing report is retained as such by the caller.
        """
        self.check_alive()
        _require(isinstance(candidate,dict) and candidate.get('step')==self.step,
                 'acceptance candidate checkpoint differs')
        health=self._call('GET','/health')
        _require(validate_health(health,self.step)==self._fingerprint,
                 'model changed before acceptance publication')
        binding=health['acceptance_binding']
        _require(candidate.get('contract_fingerprint')==binding['contract_fingerprint'] and
                 candidate.get('identity')==binding['identity'], 'acceptance source identity differs')
        # POST acknowledges the saved file; it does not return the full receipt.
        # Preserve this non-secret evidence in RAM even if readback is ambiguous.
        self.acceptance_publication_evidence={'acknowledgement':None,'readback':None,'validated':False}
        value=self.client._request('POST','/acceptance/submit',deepcopy(candidate))
        self.acceptance_publication_evidence['acknowledgement']=deepcopy(value)
        self.check_alive()
        _require(isinstance(value,dict) and type(value.get('step')) is int and value['step']==self.step and
                 value.get('status') in ('PASS','FAIL') and
                 value.get('contract_fingerprint')==binding['contract_fingerprint'] and
                 re.fullmatch('[0-9a-f]{64}',str(value.get('sha256',''))) and
                 isinstance(value.get('results'),dict) and isinstance(value.get('failure_reasons'),list) and
                 all(isinstance(reason,str) for reason in value['failure_reasons']) and
                 all(isinstance(value.get(key),str) and value[key].startswith('/') and
                     '\x00' not in value[key] and '\n' not in value[key] for key in ('receipt','history')) and
                 value['receipt']!=value['history'] and
                 (value['status']!='PASS' or not value['failure_reasons']),
                 'manager returned invalid acceptance acknowledgement; publication unconfirmed')
        saved=self._call('GET','/acceptance?step='+str(self.step))
        self.acceptance_publication_evidence['readback']=deepcopy(saved)
        self.check_alive()
        _require(isinstance(saved,dict) and saved.get('schema')=='tiger-fp32-h50-rtc-acceptance-v1' and
                 type(saved.get('step')) is int and saved['step']==self.step and saved.get('status')==value['status'] and
                 saved.get('identity')==binding['identity'] and saved.get('contract_fingerprint')==binding['contract_fingerprint'] and
                 saved.get('results')==value['results'] and saved.get('failure_reasons')==value['failure_reasons'],
                 'acceptance readback is missing, stale, failed, or differs from acknowledgement; publication unconfirmed')
        # The manager stores and returns this exact compact JSON serialization,
        # preserving insertion order. Its ACK SHA binds the full saved content.
        raw=json.dumps(saved,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
        _require(hashlib.sha256(raw).hexdigest()==value['sha256'],'acceptance readback SHA differs from saved acknowledgement')
        for key in ('schema','step','semantics','source_run_session_id','source_trial_index','plan_preparation_scope',
                    'robot_preparation_source','metrics','evidence','requirements'):
            _require(key in candidate and key in saved and _fingerprint(saved[key])==_fingerprint(candidate[key]),
                     'acceptance readback source differs: '+key)
        _require(isinstance(candidate.get('source_run_session_id'),str) and bool(candidate['source_run_session_id']) and
                 type(candidate.get('source_trial_index')) is int and candidate['source_trial_index']>=0 and
                 isinstance(saved.get('submitted_results'),dict) and
                 _fingerprint(saved['submitted_results'])==_fingerprint(candidate.get('results')),
                 'acceptance readback submitted trial/results differ')
        if value['status']=='PASS':validate_acceptance(saved,health,self.step)
        self.acceptance_publication_evidence['validated']=True
        return deepcopy(saved)

    def infer_rtc(self, snapshot, prompt, journal, rtc_context):
        """Persist exact real inputs, prior reference and all late results.

        The approved 600ms commit deadline is separate from transport completion.
        The independent <=3s HTTP watchdog cannot block the local sender.
        """
        self.check_alive()
        _require(self._infer_lock.acquire(blocking=False),'one inference in flight is allowed')
        from .hub import rpc
        request_id=uuid.uuid4().hex
        directory=None
        index=self.request_index
        self.request_index+=1  # Every attempted observation has a distinct RNG index.
        result={'schema':'tiger-h50-rtc-recorded-result-v1','request_id':request_id,
                'status':'FAILED','decision':self.client.decision,'module_dispatched_commands':0,
                'checkpoint_step':self.step,'trial_index':self.trial_index,'trial_seed':42+self.trial_index,
                'request_index':index,'scope':'Model client never dispatches commands; queue commit and actual TX are separate evidence.'}
        start=time.monotonic_ns()
        anchor=start
        try:
            snapshot=deepcopy(snapshot);context=deepcopy(rtc_context)
            expected={'schema':'tiger-fp32-h50-rtc-v1',**RTC_PARAMETERS}
            for key,value in expected.items():
                _require(context.get(key)==value,'RTC context contract differs: '+key)
            expected_phase='NATIVE_BOOTSTRAP' if index==0 else 'RTC_CONTINUATION'
            _require(context.get('inference_phase')==expected_phase and
                     context.get('enabled') is (index!=0), 'native bootstrap must be first and cannot be retried')
            _require(context.get('reference_scope') in ('PHYSICAL_ACCEPTED_REFERENCE','DIAGNOSTIC_UNSENT_REFERENCE'),
                     'reference scope is missing or ambiguous')
            _require(context.get('startup_semantic_version')==APPROVED_CONTRACT['startup_semantic_version'] and
                     context.get('execution_contract_version')==APPROVED_CONTRACT['execution_contract_version'] and
                     context.get('timing_contract_sha256')==APPROVED_CONTRACT_SHA256, 'startup timing contract is stale')
            previous=context.get('previous_actions')
            enabled=context.get('enabled',True)
            valid=context.get('previous_valid_steps')
            _require(type(enabled) is bool and type(valid) is int and valid==(35 if enabled else 0),'RTC known-prefix scope invalid')
            _require(isinstance(previous,list) and len(previous)==valid,'known prefix length invalid')
            _require(all(isinstance(row,list) and len(row)==7 and all(type(x) in (int,float) and math.isfinite(x) for x in row) for row in previous),
                     'RTC previous reference must be finite absolute rad/m')
            if 'trial_index' in context:_require(context['trial_index']==self.trial_index,'RTC trial mismatch')
            if 'request_index' in context:_require(context['request_index']==index,'RTC request sequence mismatch')
            rtc={**expected,'enabled':enabled,'previous_actions':previous,'previous_valid_steps':valid,
                 'trial_index':self.trial_index,'request_index':index}
            anchor=snapshot.get('packet_ready_monotonic_ns')
            _require(type(anchor) is int and anchor>0,'observation anchor missing')
            deadline=anchor+COMMIT_NS
            _require(context.get('deadline_monotonic_ns',deadline)==deadline,'commit deadline differs from approved contract')
            validate_recording(snapshot,journal)
            validate_recording(rpc(self.cfg,'/recording/fp32-binding',timeout=1),journal)
            directory=journal.begin(request_id,{'request_id':request_id,'snapshot':snapshot,'prompt':prompt,
                'rtc':rtc,'rtc_context':context,'checkpoint_step':self.step,'decision':self.client.decision,
                'module_dispatched_commands':0})
            result.update(phase='INPUT_ACCEPTED_IN_RAM',rtc=rtc,rtc_context=context,observation_anchor_monotonic_ns=anchor,
                          deadline_monotonic_ns=deadline,journal_directory=str(directory))
            before=self.client._request('GET','/health')
            fingerprint=validate_health(before,self.step)
            _require(fingerprint==self._fingerprint,'policy/RTC identity changed since lease acquisition')
            frozen,payload,age=validate_snapshot(snapshot,prompt,journal,self._domain)
            _require(age['front_age_s']<=.25 and age['right_age_s']<=.25,'camera source exceeds approved 250ms freshness')
            payload['rtc']=rtc
            sent=time.monotonic_ns()
            result.update(phase='INFER_STARTED',health_before=before,policy_fingerprint=fingerprint,
                          snapshot_sha256=_fingerprint(frozen),request_payload_sha256=_fingerprint(payload),
                          client_sent_monotonic_ns=sent,observation_timing=age)
            response=self.client._request('POST','/infer_rtc',payload,request_id=request_id)
            received=time.monotonic_ns()
            result.update(phase='RESPONSE_RECEIVED',response=response,client_received_monotonic_ns=received,
                          client_round_trip_ms=(received-sent)/1e6)
            validate_result(response)
            _require(response.get('contract_fingerprint')==self._acceptance_binding['contract_fingerprint'],
                     'response contract differs from the leased playback contract')
            returned=response.get('rtc',{})
            _require(all(type(returned.get(k)) is type(v) and returned.get(k)==v for k,v in RTC_PARAMETERS.items()),
                     'RTC response training/playback/schedule identity differs')
            _require(returned.get('schema')==expected['schema'] and returned.get('enabled') is enabled and
                     type(returned.get('previous_valid_steps')) is int and returned['previous_valid_steps']==valid and
                     returned.get('trial_index')==self.trial_index and returned.get('request_index')==rtc['request_index'],
                     'RTC response identity/scope mismatch')
            _require(returned.get('trial_seed')==42+self.trial_index and
                     returned.get('previous_actions_sha256')==_fingerprint(previous) and
                     returned.get('latent_dimensions')==32 and returned.get('guidance_error_physical_dimensions')==7 and
                     returned.get('unknown_error_weights_zero') is True and returned.get('latent_extra_dimensions_forced_zero') is False,
                     'RTC seed/prefix/native32 evidence mismatch')
            after=self.client._request('GET','/health');result['health_after']=after
            _require(validate_health(after,self.step)==fingerprint,'policy/RTC identity changed during inference')
            validate_recording(rpc(self.cfg,'/recording/fp32-binding',timeout=1),journal)
            self.check_alive()
            result.update(status=self.client.success_status,phase='COMPLETE',
                          deadline_met_before_result_buffer=time.monotonic_ns()<=deadline)
        except Exception as exc:
            result['error']={'type':type(exc).__name__,'message':str(exc)}
            raise
        finally:
            result['client_finished_monotonic_ns']=time.monotonic_ns()
            try:
                if directory is not None:journal.finish(directory,result)
                buffered=time.monotonic_ns()
                safe_anchor=anchor if type(anchor) is int and anchor>0 else start
                result['result_ram_accepted_monotonic_ns']=buffered
                result['end_to_end_result_ram_ms']=(buffered-safe_anchor)/1e6
                result['deadline_met']=buffered<=result.get('deadline_monotonic_ns',0)
                if directory is not None:
                    journal.write_json(directory/'ram-ready-timing.json',{'request_id':request_id,
                        'result_ram_accepted_monotonic_ns':buffered,'end_to_end_result_ram_ms':result['end_to_end_result_ram_ms'],
                        'deadline_met':result['deadline_met'],'timing_basis':'packet_ready_to_validated_ram_commit_ready',
                        'usb_persistence_complete':False})
                result['ready_for_commit_monotonic_ns']=time.monotonic_ns()
                result['end_to_end_ready_ms']=(result['ready_for_commit_monotonic_ns']-safe_anchor)/1e6
                result['deadline_met']=result['ready_for_commit_monotonic_ns']<=result.get('deadline_monotonic_ns',0)
            finally:self._infer_lock.release()
        return result

    def close(self):
        self._closing.set()
        if self._worker is not None:
            self._worker.join(timeout=1.5)
            _require(not self._worker.is_alive(), 'lease heartbeat did not close')
        if self._session_id is None:
            return {'released': False, 'reason': 'no lease acquired', 'credential_saved': False}
        session = self._session_id
        try:
            response=self._call('POST', '/lease/release', {'session_id': session, 'lease_token': self._lease_token})
            return {'released': True, 'session_id': session, 'step': self.step, 'credential_saved': False,
                    'gpu_telemetry':deepcopy(response.get('gpu_telemetry'))}
        finally:
            self._session_id = self._lease_token = None


def shadow_trial(cfg, session, stop_event, step, count, task):
    """Real observation and RTC pipeline, explicitly no hardware writer."""
    from .h50_deployment import run_h50_no_motion
    return run_h50_no_motion(cfg,session,stop_event,step,count=count,warmup=0,trial_index=0)
