"""Independent BF16 runtime. Exact canonical recording pipeline with mixed identity validation.
Generated source provenance is in bindings/bf16-adapter-source.json.
No model selection, manager publication, SDK construction or device call on import.
"""
from copy import deepcopy
import math,threading,time,uuid
from pathlib import Path
from agilex_control.h50_runtime import ModelRuntime as OriginalRuntime
from agilex_control.h50_runtime import _require,_fingerprint,RTC_PARAMETERS,APPROVED_CONTRACT,APPROVED_CONTRACT_SHA256,COMMIT_NS
from agilex_control.remote_fp32 import validate_recording,validate_snapshot,validate_result
from bf16_service_contract import contract
from runtime.bf16_transport import BoundedHTTP

def bf16_fingerprint(health,step):
    contract.validate_health(health,step)
    return _fingerprint({k:health[k] for k in ('contract','policy','rtc','acceptance_binding')})

def health_descriptor(health,step):
    contract.validate_health(health,step)
    return deepcopy(health['policy'])

def validate_bf16_response(response,wire):
    validate_result(response)
    _require(response.get('policy_precision')==contract.POLICY_PRECISION,'BF16 response precision differs')
    validated=contract.load_validators(Path(contract.__file__).parent/'frozen').validate_rtc(wire)
    contract.validate_response_rtc(response.get('rtc'),validated)
    # The frozen BF16 response omits latent dimension fields; health/IR binds 32D/7D.

def current_loaded_model(cfg):
    api=BoundedHTTP(18961,timeout=.8)
    status=api.request('GET','/status')
    _require(status.get('status')=='READY' and status.get('busy') is False and status.get('lease',{}).get('active') is False,
             'independent BF16 service unavailable or leased')
    step=status.get('selected_step')
    contract.validate_health(api.request('GET','/health'),step)
    return step,status

class BF16Client:
    decision='PREDICTED_NOT_DISPATCHED_BY_CLIENT'
    success_status='DEPLOYMENT_PREDICTION_BUFFERED'
    def __init__(self,owner):self.owner=owner;self.transport=BoundedHTTP(18961)
    def _request(self,method,path,payload=None,request_id=None,expected_statuses=(200,)):
        headers={}
        if path=='/infer_rtc':
            session,token=self.owner._credentials()
            _require(session is not None and token is not None,'active independent BF16 lease required')
            headers={'X-Tiger-Deployment-Session':session,'X-Tiger-Deployment-Lease':token}
            if request_id:headers['X-Request-ID']=request_id
        return self.transport.request(method,path,payload,headers=headers,expected_statuses=expected_statuses)

class BF16Runtime(OriginalRuntime):
    def __init__(self,cfg,step):
        super().__init__(cfg,step,mode='deployment')
        self.client=BF16Client(self);self.api=BoundedHTTP(18961,timeout=.8);self.service_nonce=None
    def _call(self,method,path,payload=None,*,statuses=(200,)):
        headers={'X-Tiger-Service-Nonce':self.service_nonce} if path=='/lease/acquire' else {}
        return self.api.request(method,path,payload,headers=headers,expected_statuses=statuses)
    def ensure_ready(self,*args,**kwargs):raise RuntimeError('independent runtime cannot select or reload a model')
    def bind_loaded(self):
        step,status=current_loaded_model(self.cfg)
        _require(step==self.step,'current BF16 checkpoint differs')
        health=self._call('GET','/health')
        self._fingerprint=bf16_fingerprint(health,self.step)
        self._acceptance_binding=deepcopy(health['acceptance_binding'])
        self.service_nonce=health.get('service_nonce')
        _require(isinstance(self.service_nonce,str) and len(self.service_nonce)>16,'service nonce missing')
        return health
    def require_acceptance(self):raise RuntimeError('FP32 acceptance cannot authorize BF16')
    def submit_acceptance(self,*args):raise RuntimeError('independent runtime cannot modify FP32 acceptance')
    def infer_rtc(self, snapshot, prompt, journal, rtc_context):
        """Persist exact real inputs, prior reference and all late results.

        The approved 600ms commit deadline is separate from transport completion.
        The independent <=3s HTTP watchdog cannot block the local sender.
        """
        self.check_alive()
        _require(self._infer_lock.acquire(blocking=False),'one inference in flight is allowed')
        from agilex_control.hub import rpc
        request_id=uuid.uuid4().hex
        directory=None
        index=self.request_index
        self.request_index+=1  # Every attempted observation has a distinct RNG index.
        result={'schema':'tiger-h50-rtc-recorded-result-v1','request_id':request_id,
                'status':'FAILED','decision':self.client.decision,'module_dispatched_commands':0,
                'policy_precision':contract.POLICY_PRECISION,'checkpoint_step':self.step,'trial_index':self.trial_index,'trial_seed':42+self.trial_index,
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
            fingerprint=bf16_fingerprint(before,self.step)
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
            validate_bf16_response(response,rtc)
            after=self.client._request('GET','/health');result['health_after']=after
            _require(bf16_fingerprint(after,self.step)==fingerprint,'policy/RTC identity changed during inference')
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
