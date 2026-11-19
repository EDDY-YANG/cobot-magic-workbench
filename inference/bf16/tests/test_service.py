"""PC synthetic protocol/state tests; no true backend, JAX, GPU or hardware."""
from contextlib import contextmanager
from copy import deepcopy
import base64
import io
import json
import http.client
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import numpy as np
from PIL import Image
from service import contract as c
from service.server import BF16Service,make_server

NONCE='a'*32
CONFIG_SHA='b'*64


def make_payload(*,enabled=False,index=0,trial=2):
    image=io.BytesIO();Image.new('RGB',(32,32),(10,20,30)).save(image,format='JPEG')
    jpeg=base64.b64encode(image.getvalue()).decode('ascii')
    return {'state':[0.]*7,'front_jpeg_base64':jpeg,'right_jpeg_base64':jpeg,'prompt':'SYNTHETIC PROTOCOL FIXTURE',
        'rtc':{'schema':'tiger-fp32-h50-rtc-v1','enabled':enabled,
            'previous_actions':[[0.]*7 for _ in range(35)] if enabled else [],
            'previous_valid_steps':35 if enabled else 0,'freeze_steps':12,'execution_label_hz':18,
            'native_training_hz':30,'beta':5.0,'trial_index':trial,'request_index':index}}


class SyntheticBackend:
    """Literal frozen MixedPolicy metadata shape, explicitly NOT BF16 readiness."""
    synthetic_fixture=True
    def __init__(self):
        self.metadata={'checkpoint_step':5000,'precision':'mixed_bf16_backbone_fp32_rtc',
            'horizon':50,'internal_action_dim':32,'physical_action_dim':7,'action_basis':'absolute',
            'units':['rad']*6+['m'],'state_dtype':'float32','action_dtype':'float32','sampling_steps':10,
            'precision_audit_passed':True,'quality_validation_passed':False,
            'source_commit':'215abfb217dbac7d5f1273282331b9b1866c0479',
            'normalization_sha256':'22704c461f2485b8f15ff3b0aa99f4491e04c97735d53d1ca2e70f45dda85126',
            'tokenizer_sha256':'8986bb4f423f07f8c7f70d0dbe3526fb2316056c17bae71b1ea975e77a168fc6'}
        self.rtc={'schema':'tiger-fp32-h50-rtc-v1','implementation_schema':'tiger-pi05-bf16-backbone-f32-rtc-offline-v1',
            'precision_audit_passed':True,'precision_audit_sha256':'c'*64,'freeze_steps':12,'previous_valid_steps':35,
            'execution_label_hz':18,'native_training_hz':30,'beta':5.0,
            'vjp_precision_note':'BF16 network differentiation; FP32 outer RTC math'}
        self.audit={'status':'MIXED_FORWARD_IR_VERIFIED_NOT_QUALITY_ACCEPTANCE','bf16_dot_conv_nodes':1,
            'stablehlo_bf16_dot_conv_nodes':1,'fp16_nodes':0,'output_dtypes':['float32'],
            'fixture':'SYNTHETIC_AUDIT_NOT_MODEL_EVIDENCE'}
        self.identity={'test_only':'SYNTHETIC_BACKEND_NOT_REAL_BF16'}
        self.warmup={'completed':True,'real_historical_inputs':False,'synthetic_fixture':True}
        self.calls=0;self.block=False;self.entered=threading.Event();self.release=threading.Event();self.returned=threading.Event()
        self.bad_dtype=False;self.raise_error=False;self.stopped=False
    def infer_rtc(self,observation):
        self.calls+=1;self.entered.set()
        if self.block and not self.release.wait(3):raise RuntimeError('SYNTHETIC fixture release missing')
        if self.raise_error:raise RuntimeError('SYNTHETIC PRIVATE INPUT MUST NEVER BE RETURNED')
        rtc=observation['rtc']
        # Independently copy actual frozen MixedPolicy return semantics: static
        # capacity35 even for native0, while prefix hash/enabled/RNG echo input.
        result={'actions':np.zeros((50,7),dtype=np.float64 if self.bad_dtype else np.float32),
            'timing':{'device_synchronized_ms':0.,'total_host_output_ms':0.},
            'rtc':{**self.rtc,'enabled':rtc['enabled'],
                'previous_actions_sha256':c.canonical_sha(rtc['previous_actions']),
                'trial_index':rtc['trial_index'],'request_index':rtc['request_index'],'trial_seed':42+rtc['trial_index'],
                'unknown_error_weights_zero':True,'prefix_exactly_overwritten':False,'latent_extra_dimensions_forced_zero':False}}
        self.returned.set();return result


def api_fixture(backend=None,clock=time.monotonic):
    backend=backend or SyntheticBackend()
    return BF16Service(backend,5000,clock=clock,service_nonce=NONCE,config_sha256=CONFIG_SHA)


@contextmanager
def server_fixture(api):
    http=make_server(api,port=0);thread=threading.Thread(target=http.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
    try:yield http.server_address[1]
    finally:
        api.begin_close();api.backend.release.set();api.wait_idle(3);http.shutdown();http.server_close();thread.join(2)


def call(port,path,body=None,headers=None,raw=None):
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
    encoded=raw if raw is not None else c.canonical(body) if body is not None else None
    fields={'Content-Type':'application/json',**(headers or {})}
    try:
        connection.request('GET' if body is None and raw is None else 'POST',path,body=encoded,headers=fields)
        response=connection.getresponse();return response.status,json.loads(response.read())
    finally:connection.close()


def lease(api,session='synthetic-session',trial=2):
    return api.acquire(session,5000,trial,service_nonce=NONCE)


class ServiceTests(unittest.TestCase):
    def test_import_and_synthetic_health_do_not_claim_model_ready(self):
        api=api_fixture();health=api.health()
        self.assertFalse(health['ready']);self.assertFalse(health['ready_for_live']);self.assertTrue(health['synthetic_fixture'])
        with self.assertRaises(c.APIError):c.validate_health(health,5000)
        c.validate_health(health,5000,allow_synthetic=True)
        for name in ('jax','torch','piper_sdk'):self.assertNotIn(name,sys.modules)

    def test_native_and_continuation_preserve_real_capacity35_response_semantics(self):
        api=api_fixture();key=lease(api)
        for enabled,index in ((False,0),(True,3)):
            body=make_payload(enabled=enabled,index=index);result=api.infer(body,key['session_id'],key['lease_token'])
            self.assertEqual(result['action_shape'],[50,7]);self.assertEqual(result['action_dtype'],'float32')
            self.assertEqual(result['rtc']['previous_valid_steps'],35)
            self.assertEqual(result['rtc']['enabled'],enabled)
            self.assertEqual(result['rtc']['previous_actions_sha256'],c.canonical_sha(body['rtc']['previous_actions']))
            self.assertEqual(result['rtc']['trial_seed'],44)
        self.assertEqual(api.backend.calls,2)

    def test_wrong_nonce_no_lease_then_repeated_index_and_trial_mismatch_rejected(self):
        api=api_fixture()
        with self.assertRaises(c.APIError) as error:api.acquire('synthetic',5000,2,service_nonce='0'*32)
        self.assertEqual(error.exception.code,'SERVICE_IDENTITY_MISMATCH');self.assertIsNone(api.lease)
        key=lease(api);api.infer(make_payload(),key['session_id'],key['lease_token'])
        for body in (make_payload(),make_payload(index=1,trial=3)):
            with self.assertRaises(c.APIError) as error:api.infer(body,key['session_id'],key['lease_token'])
            self.assertEqual(error.exception.code,'RTC_SEQUENCE_MISMATCH')
        self.assertEqual(api.backend.calls,1)

    def test_lease_cycles_reset_index_but_not_seed_or_fingerprint(self):
        api=api_fixture();first=lease(api);response=api.infer(make_payload(),first['session_id'],first['lease_token'])
        api.release(first['session_id'],first['lease_token']);second=lease(api,'second-synthetic')
        again=api.infer(make_payload(),second['session_id'],second['lease_token'])
        self.assertEqual(response['rtc']['trial_seed'],again['rtc']['trial_seed'])
        self.assertEqual(first['contract_fingerprint'],second['contract_fingerprint'])
        with self.assertRaises(c.APIError):api.heartbeat(first['session_id'],first['lease_token'])

    def test_health_and_heartbeat_work_during_blocked_model_singleflight(self):
        backend=SyntheticBackend();backend.block=True;api=api_fixture(backend);key=lease(api);errors=[]
        thread=threading.Thread(target=lambda:self._infer_errors(api,key,errors));thread.start();self.assertTrue(backend.entered.wait(1))
        try:
            self.assertTrue(api.status()['busy']);self.assertTrue(api.heartbeat(key['session_id'],key['lease_token'])['active'])
            with self.assertRaises(c.APIError) as error:api.release(key['session_id'],key['lease_token'])
            self.assertEqual(error.exception.code,'INFERENCE_BUSY')
            with self.assertRaises(c.APIError):api.infer(make_payload(index=1),key['session_id'],key['lease_token'])
            self.assertEqual(backend.calls,1)
        finally:backend.release.set();thread.join(2)
        self.assertEqual(errors,[]);self.assertFalse(api.inflight)

    @staticmethod
    def _infer_errors(api,key,errors):
        try:api.infer(make_payload(),key['session_id'],key['lease_token'])
        except BaseException as exc:errors.append(exc)

    def test_expiration_and_shutdown_revoke_results_and_drain_actual_backend(self):
        for action in ('expire','close'):
            with self.subTest(action=action):
                clock=[0.];backend=SyntheticBackend();backend.block=True;api=api_fixture(backend,clock=lambda:clock[0]);key=lease(api);errors=[]
                worker=threading.Thread(target=lambda:self._infer_errors(api,key,errors));worker.start();self.assertTrue(backend.entered.wait(1))
                if action=='expire':clock[0]=2.01
                else:api.begin_close()
                self.assertFalse(api.wait_idle(.01));self.assertTrue(api.inflight)
                backend.release.set();worker.join(2)
                self.assertTrue(api.wait_idle(.1));self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],c.APIError)
                self.assertEqual(api.attempts[-1]['status'],'FAILED')
                with self.assertRaises(c.APIError):api.heartbeat(key['session_id'],key['lease_token'])

    def test_bad_output_or_backend_exception_no_fallback_and_no_private_error(self):
        for fault in ('bad_dtype','raise_error'):
            backend=SyntheticBackend();setattr(backend,fault,True);api=api_fixture(backend);key=lease(api)
            with self.assertRaises(c.APIError) as error:api.infer(make_payload(),key['session_id'],key['lease_token'])
            self.assertEqual(error.exception.code,'BF16_INFERENCE_FAILED');self.assertNotIn('PRIVATE INPUT',str(error.exception))
            self.assertEqual(api.state,'ERROR');self.assertEqual(backend.calls,1);self.assertIsNone(api.lease)

    def test_strict_real_jpeg_float32_and_rtc_decoders_are_not_bypassed(self):
        cases=[]
        value=make_payload();value['state'][0]=True;cases.append(value)
        value=make_payload();value['state'][0]=1e100;cases.append(value)
        value=make_payload();value['front_jpeg_base64']='not-base64';cases.append(value)
        buffer=io.BytesIO();Image.new('RGB',(32,32)).save(buffer,format='PNG')
        value=make_payload();value['right_jpeg_base64']=base64.b64encode(buffer.getvalue()).decode();cases.append(value)
        value=make_payload(enabled=True);value['rtc']['previous_actions'].pop();cases.append(value)
        value=make_payload();value['rtc']['beta']=6.;cases.append(value)
        for body in cases:
            api=api_fixture();key=lease(api)
            with self.assertRaises(c.APIError):api.infer(body,key['session_id'],key['lease_token'])
            self.assertEqual(api.backend.calls,0)

    def test_loopback_http_original_framing_nonce_lease_native_and_rtc(self):
        api=api_fixture()
        with server_fixture(api) as port:
            status,health=call(port,'/health');self.assertEqual(status,200);self.assertEqual(health['service_nonce'],NONCE)
            status,failed=call(port,'/lease/acquire',{'session_id':'http-synthetic','step':5000,'trial_index':2})
            self.assertEqual(status,403);self.assertIsNone(api.lease)
            status,key=call(port,'/lease/acquire',{'session_id':'http-synthetic','step':5000,'trial_index':2},{c.SERVICE_NONCE_HEADER:NONCE})
            self.assertEqual(status,200)
            headers={c.SESSION_HEADER:key['session_id'],c.LEASE_HEADER:key['lease_token']}
            for enabled,index in ((False,0),(True,3)):
                status,result=call(port,'/infer_rtc',make_payload(enabled=enabled,index=index),headers)
                self.assertEqual(status,200,result);c.validate_response_rtc(result['rtc'],c.load_validators().validate_rtc(make_payload(enabled=enabled,index=index)['rtc']))
            status,result=call(port,'/lease/release',{'session_id':key['session_id'],'lease_token':key['lease_token']})
            self.assertTrue(result['released'])
            for path in ('/select','/acceptance/submit','/infer','/motion','/shutdown'):
                self.assertEqual(call(port,path,{})[0],404)
            self.assertEqual(call(port,'/lease/acquire',raw=b'{"session_id":"x","session_id":"y"}')[0],400)
            self.assertEqual(call(port,'/lease/acquire',raw=b'{"x":NaN}')[0],400)


if __name__=='__main__':unittest.main()
