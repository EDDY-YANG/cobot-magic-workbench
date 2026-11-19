"""BF16 bridge with original request timing and separate capacity35 reply semantics."""
from copy import deepcopy
import time
from runtime.frozen_legacy.live_bridge import require,finite_vector,canonical_sha,LiveRequest,WIRE_FIELDS,NS
from runtime.bf16_model import validate_bf16_response
from bf16_service_contract import contract
class BF16ContextBridge:
    def __init__(self,mode,session_id,approved_contract,approved_contract_sha256,guard):
        require(mode in ('NO_SEND','APPLY'),'explicit independent execution mode required')
        require(isinstance(session_id,str) and session_id,'unique current session identity required')
        require(callable(guard),'canonical resource guard required')
        require(isinstance(approved_contract_sha256,str) and len(approved_contract_sha256)==64 and
                all(c in '0123456789abcdef' for c in approved_contract_sha256.lower()),'exact approved timing SHA required')
        expected={'action_hz':18,'send_hz':50,'horizon':50,'rtc_freeze_steps':12,'rtc_known_steps':35,
                  'replan_steps':14,'reply_deadline_ms':600,'rtc_beta':5.0,'native_training_hz':30}
        require(all(approved_contract.get(k)==v for k,v in expected.items()),'live timing contract differs')
        for key in ('startup_semantic_version','execution_contract_version','no_send_semantic_version'):
            require(isinstance(approved_contract.get(key),str) and approved_contract[key],'startup/timing semantic identity missing')
        self._mode,self.session_id,self.contract,self.sha,self.guard=mode,session_id,deepcopy(approved_contract),approved_contract_sha256,guard
        self.next_index=0;self.next_response_index=0;self.seen_request_ids=set()

    @property
    def mode(self):return self._mode

    def prepare(self,queue_context,snapshot,*,now_ns=None):
        """Copy queue math into a live request; never relabel/mutate pending data."""
        self.guard()
        context=deepcopy(queue_context)
        now=time.monotonic_ns() if now_ns is None else now_ns
        packet=snapshot.get('packet_ready_monotonic_ns')
        require(type(now) is int and type(packet) is int and 0<=now-packet<=200_000_000,'live observation packet is stale/future')
        require(context.get('observation_monotonic_ns')==packet,'observation anchor differs from current snapshot')
        require(context.get('request_index')==self.next_index,'request duplicated/reversed/skipped')
        index=self.next_index
        enabled=index!=0
        require(context.get('inference_phase')==('NATIVE_BOOTSTRAP' if not enabled else 'RTC_CONTINUATION'),'native/RTC phase differs')
        expected={'schema':'tiger-fp32-h50-rtc-v1','enabled':enabled,'previous_valid_steps':35 if enabled else 0,
                  'freeze_steps':12,'execution_label_hz':18,'native_training_hz':30,'beta':5.0}
        require(all(type(context.get(k)) is type(v) and context[k]==v for k,v in expected.items()),'strict RTC fields differ')
        previous=context.get('previous_actions')
        require(isinstance(previous,list) and len(previous)==expected['previous_valid_steps'] and all(finite_vector(row,7) for row in previous),'finite actual accepted-reference prefix required')
        trial=context.get('trial_index')
        require(type(trial) is int and 0<=trial<=1_000_000,'invalid current trial index')
        require(context.get('deadline_monotonic_ns')==packet+600_000_000,'deadline differs')
        require(context.get('cut_monotonic_ns')==packet+(12*NS+9)//18,'protected cut differs')
        require(isinstance(context.get('context_id'),str) and context['context_id'] and type(context.get('generation')) is int,'queue context identity/generation missing')
        require(isinstance(context.get('old_plan_id'),str) and context['old_plan_id'],'accepted plan identity missing')
        wire={key:deepcopy(context[key]) for key in WIRE_FIELDS}
        scope='DIAGNOSTIC_UNSENT_REFERENCE' if self.mode=='NO_SEND' else 'PHYSICAL_ACCEPTED_REFERENCE'
        runtime={**deepcopy(wire),'observation_monotonic_ns':packet,'cut_monotonic_ns':context['cut_monotonic_ns'],
                 'deadline_monotonic_ns':context['deadline_monotonic_ns'],'old_plan_id':context['old_plan_id'],
                 'inference_phase':context['inference_phase'],'reference_scope':scope,
                 'startup_semantic_version':self.contract['startup_semantic_version'],
                 'execution_contract_version':self.contract['execution_contract_version'],'timing_contract_sha256':self.sha,
                 'no_send_semantic_version':self.contract['no_send_semantic_version'] if self.mode=='NO_SEND' else None,
                 'prefix_reference_kind':'NATIVE_BOOTSTRAP_NO_PREFIX' if not enabled else
                     ('DIAGNOSTIC_UNSENT_REFERENCE' if self.mode=='NO_SEND' else 'ACCEPTED_REFERENCE_PLAN_FUTURE_NOT_ALL_SENT')}
        internal={'session_id':self.session_id,'mode':self.mode,'queue_context_id':context['context_id'],
                  'queue_generation':context['generation'],'queue_context':context,'reference_scope':scope,
                  'previous_actions_sha256':canonical_sha(previous),'requires_new_live_model_inference':True,
                  'historical_actions_or_echo_are_not_accepted':True,
                  'original_queue_tags_are_preserved_math_library_metadata_not_live_inference_provenance':True,
                  'scope':'LIVE_EXPERIMENT_SOURCE_PREPARATION_NOT_DEPLOYMENT_ACCEPTANCE'}
        self.next_index+=1
        return LiveRequest(runtime,wire,internal)

    def validate_prediction(self,request,predicted):
        """Require the existing recorded ModelRuntime result, never bare actions."""
        self.guard();wire=request.wire;internal=request.internal
        require(internal['session_id']==self.session_id and internal['mode']==self.mode,'request belongs to another session/mode')
        require(wire['request_index']==self.next_response_index,'response duplicated/reversed/skipped')
        require(predicted.get('status')=='DEPLOYMENT_PREDICTION_BUFFERED','recorded current ModelRuntime result required')
        request_id=predicted.get('request_id')
        require(isinstance(request_id,str) and request_id and request_id not in self.seen_request_ids,'new live request identity required')
        require(predicted.get('rtc_context')==request.runtime_context,'recorded inference context differs')
        require(predicted.get('observation_anchor_monotonic_ns')==request.runtime_context['observation_monotonic_ns'],'response observation anchor differs')
        response=predicted.get('response',{})
        require(response.get('action_dtype')=='float32' and response.get('action_shape')==[50,7],'FP32 H50x7 response required')
        actions=response.get('actions')
        require(isinstance(actions,list) and len(actions)==50 and all(finite_vector(row,7) for row in actions),'finite original unprojected actions required')
        require(predicted.get('policy_precision')==contract.POLICY_PRECISION,'BF16 result identity missing')
        validate_bf16_response(response,wire)
        self.seen_request_ids.add(request_id);self.next_response_index+=1
        return deepcopy(actions)
