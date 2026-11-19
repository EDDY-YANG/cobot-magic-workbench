"""Independent BF16 service contract. Import is standard-library/CPU only."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys

SCHEMA='tiger-bf16-rtc-service-v1'
CONFIG_SCHEMA='tiger-bf16-rtc-service-config-v1'
POLICY_PRECISION='mixed_bf16_backbone_fp32_rtc'
HOST,PORT='127.0.0.1',18960
ALLOWED_STEPS=(2000,5000,10000,15000,20000)
LEASE_TTL=2.0
SESSION_HEADER='X-Tiger-Deployment-Session'
LEASE_HEADER='X-Tiger-Deployment-Lease'
SERVICE_NONCE_HEADER='X-Tiger-Service-Nonce'
MAX_BODY_BYTES=8*1024*1024
REQUEST_TIMEOUT_SECONDS=15
F32_MAX=3.4028234663852886e38
RTC_SCHEMA='tiger-fp32-h50-rtc-v1'
UNITS=['rad']*6+['m']
FROZEN_SHA={
    'fp32_http.py':'89aef2c1e4c168b30c84a5c77e70b0717299ef16ecae9d1f280887e6e7f8891f',
    'frozen_rtc_core.py':'5f2a4f959f3d72991d04bda0c6198861636bb6f7022dfa33dd26f4f10d8759e4'}


class APIError(RuntimeError):
    def __init__(self,code,message,status=409):
        super().__init__(message);self.code,self.status=code,status


def require(value,code,message,status=409):
    if not value:raise APIError(code,message,status)


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf8')
def canonical_sha(value):return hashlib.sha256(canonical(value)).hexdigest()
def file_sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def strict_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise APIError('INVALID_JSON','Duplicate JSON field',400)
            result[key]=value
        return result
    def constant(value):raise APIError('INVALID_JSON','Non-finite JSON constant',400)
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=constant)
    except (ValueError,UnicodeDecodeError,RecursionError) as exc:raise APIError('INVALID_JSON','Invalid UTF-8 JSON body',400) from exc


def step_value(step):
    require(type(step) is int and step in ALLOWED_STEPS,'INVALID_STEP','Explicit existing candidate step required',400)
    return step


def session_value(value):
    require(isinstance(value,str) and 0<len(value)<=200 and value.strip()==value and '\0' not in value,
            'INVALID_SESSION','Explicit nonempty session_id required',400)
    return value


def load_validators(folder=None):
    """Reuse exact frozen JPEG/float32 observation/output and RTC validators."""
    folder=Path(folder) if folder is not None else Path(__file__).resolve().parents[1]/'frozen'
    loaded={}
    for filename,expected in FROZEN_SHA.items():
        path=folder/filename
        if file_sha(path)!=expected:raise RuntimeError('Frozen CPU validation source SHA differs: '+filename)
        name='_bf16_service_cpu_'+path.stem+'_'+expected[:12]
        module=sys.modules.get(name)
        if module is None:
            spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
            sys.modules[name]=module;spec.loader.exec_module(module)
        elif Path(module.__file__).resolve()!=path.resolve():raise RuntimeError('Foreign CPU validator module owner')
        loaded[filename]=module
    native,rtc=loaded['fp32_http.py'],loaded['frozen_rtc_core.py']
    class Validators:
        decode_observation=staticmethod(native.decode_observation)
        encode_result=staticmethod(native.encode_result)
        validate_rtc=staticmethod(rtc.validate_rtc)
        json_safe=staticmethod(native._json_safe)
        source_sha256=dict(FROZEN_SHA)
    return Validators()


def validate_response_rtc(returned,request):
    expected={'schema':RTC_SCHEMA,'enabled':request['enabled'],'freeze_steps':12,
        # Frozen MixedPolicy reports its static known-prefix CAPACITY35 here,
        # including native enabled=False requests whose actual input is empty.
        'previous_valid_steps':35,'execution_label_hz':18,
        'native_training_hz':30,'beta':5.0,'trial_index':request['trial_index'],
        'request_index':request['request_index'],'trial_seed':request['trial_seed'],
        'previous_actions_sha256':request['previous_actions_sha256'],
        'unknown_error_weights_zero':True,'prefix_exactly_overwritten':False,
        'latent_extra_dimensions_forced_zero':False}
    require(isinstance(returned,dict) and all(type(returned.get(key)) is type(value) and returned[key]==value
        for key,value in expected.items()),'RTC_RESPONSE_MISMATCH','BF16 RTC response differs from validated request',502)


def validate_health(value,step,*,allow_synthetic=False):
    """Independent mixed-precision identity, never an FP32 acceptance alias."""
    step_value(step)
    require(isinstance(value,dict) and value.get('schema')==SCHEMA and value.get('local_only') is True,
            'INVALID_HEALTH','Not the independent BF16 service',503)
    synthetic=value.get('synthetic_fixture') is True
    require(allow_synthetic or not synthetic,'SYNTHETIC_BACKEND','Synthetic tests are not BF16 readiness',503)
    require(value.get('ready') is True or allow_synthetic and synthetic,
            'MODEL_NOT_READY','Independent BF16 backend is not ready',503)
    policy,wire,rtc,audit=(value.get(k,{}) for k in ('policy','contract','rtc','precision_audit'))
    require(value.get('policy_precision')==POLICY_PRECISION and policy.get('precision')==POLICY_PRECISION
        and policy.get('checkpoint_step')==step and policy.get('horizon')==50
        and policy.get('internal_action_dim')==32 and policy.get('physical_action_dim')==7
        and policy.get('action_basis')=='absolute' and policy.get('units')==UNITS,
        'PRECISION_CONTRACT_MISMATCH','Independent BF16 policy identity differs',503)
    require(wire.get('action_dtype')=='float32' and wire.get('action_shape')==[50,7]
        and wire.get('state_dtype')=='float32' and wire.get('latent_dim')==32,
        'ACTION_CONTRACT_MISMATCH','Original float32 physical action interface required',503)
    require(rtc.get('freeze_steps')==12 and rtc.get('previous_valid_steps')==35
        and rtc.get('execution_label_hz')==18 and rtc.get('native_training_hz')==30 and rtc.get('beta')==5.0,
        'RTC_CONTRACT_MISMATCH','RTC12/35/beta5/playback18 contract differs',503)
    require(audit.get('status')=='MIXED_FORWARD_IR_VERIFIED_NOT_QUALITY_ACCEPTANCE'
        and type(audit.get('bf16_dot_conv_nodes')) is int and audit['bf16_dot_conv_nodes']>0
        and type(audit.get('stablehlo_bf16_dot_conv_nodes')) is int and audit['stablehlo_bf16_dot_conv_nodes']>0
        and audit.get('fp16_nodes')==0 and audit.get('output_dtypes')==['float32']
        and policy.get('precision_audit_passed') is True and rtc.get('precision_audit_passed') is True,
        'MIXED_AUDIT_MISSING','Independent BF16 forward IR audit required',503)
    for key in ('normalization_sha256','tokenizer_sha256'):
        require(isinstance(policy.get(key),str) and re.fullmatch('[0-9a-f]{64}',policy[key]) is not None,
                'IDENTITY_MISSING','Normalization/tokenizer identity missing',503)
    require(policy.get('source_commit')=='215abfb217dbac7d5f1273282331b9b1866c0479',
            'IDENTITY_MISSING','Pinned upstream source commit differs',503)
    fingerprint=value.get('contract_fingerprint')
    require(isinstance(fingerprint,str) and len(fingerprint)==64 and
        value.get('acceptance_binding',{}).get('contract_fingerprint')==fingerprint,
        'FINGERPRINT_MISSING','Independent BF16 fingerprint is missing',503)
    return deepcopy(value)
