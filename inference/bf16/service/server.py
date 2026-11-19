"""Independent one-model BF16 RTC service, inert/default dry-run on import/CLI.

GPU inference runs outside the short state lock. Health/lease heartbeats remain
responsive. Shutdown invalidates leases/results first, drains actual backend
calls, and only THEN exits the two existing GPU-owner-lock context.
"""
from __future__ import annotations
import argparse
from collections import deque
from copy import deepcopy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sys
import threading
import time

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from service import contract as c
    from service.backend import read_config,verify_online_environment,online_backend
else:
    from . import contract as c
    from .backend import read_config,verify_online_environment,online_backend


class BF16Service:
    def __init__(self,backend,step,validators=None,*,clock=time.monotonic,service_nonce='',config_sha256=''):
        self.backend,self.step,self.validators=backend,c.step_value(step),validators or c.load_validators()
        self.clock=clock;self.lock=threading.RLock();self.idle=threading.Condition(self.lock)
        self.state='READY';self.closing=False;self.inflight=False;self.lease=None;self.epoch=0
        self.service_nonce,self.config_sha256=service_nonce,config_sha256
        self.synthetic=backend.synthetic_fixture is True
        self.attempts=deque(maxlen=256)
        policy=self.validators.json_safe(deepcopy(backend.metadata))
        rtc=self.validators.json_safe(deepcopy(backend.rtc))
        audit=self.validators.json_safe(deepcopy(backend.audit))
        identity={**deepcopy(backend.identity),'config_sha256':config_sha256,
                  'checkpoint_step':step,'independent_service_schema':c.SCHEMA}
        contract={'parameter_dtype':'mixed_bfloat16_float32','compute_dtype':'mixed_bfloat16_float32',
            'state_dtype':'float32','action_dtype':'float32','action_shape':[50,7],'latent_dim':32,'physical_dim':7,
            'known_prefix_capacity':35,'native_previous_valid_steps':0,'sampling_steps':10,
            'precision_verification_owner':'independent_BF16_forward_IR_audit'}
        semantics={'numerical_quality_accepted':False,'motion_accepted':False,'FP32_acceptance_reused':False,
                   'scope':'HISTORICAL_INPUT_MODEL_SERVICE_NOT_ROBOT_OR_TASK_ACCEPTANCE'}
        binding={'identity':identity,'semantics':semantics}
        binding['contract_fingerprint']=c.canonical_sha({'schema':c.SCHEMA,'identity':identity,'contract':contract,
            'policy':policy,'rtc':rtc,'precision_audit':audit,'semantics':semantics})
        self.fingerprint=binding['contract_fingerprint']
        self.base_health={'schema':c.SCHEMA,'local_only':True,'policy_precision':c.POLICY_PRECISION,
            'synthetic_fixture':self.synthetic,'ready_for_live':False,'quality_validation_passed':False,
            'policy':policy,'contract':contract,'rtc':rtc,'precision_audit':audit,'warmup':deepcopy(backend.warmup),
            'acceptance_binding':binding,'contract_fingerprint':self.fingerprint,
            'service_nonce':service_nonce,'config_sha256':config_sha256,'step':step,
            'readiness_scope':'MODEL_INFERENCE_ONLY_NOT_NUMERICAL_OR_ROBOT_ACCEPTANCE'}
        c.validate_health(self.health(),step,allow_synthetic=self.synthetic)
        if not self.synthetic and backend.warmup.get('completed') is not True:raise RuntimeError('Real historical warmup has not completed')

    def _expire(self):
        if self.lease is not None and self.clock()>=self.lease['expires']:
            self.lease=None;self.epoch+=1

    def _match(self,session,token):
        self._expire()
        c.require(self.lease is not None,'LEASE_EXPIRED','Lease expired or absent')
        c.require(isinstance(session,str) and isinstance(token,str) and self.lease['session_id']==session
            and secrets.compare_digest(self.lease['token'],token),'LEASE_MISMATCH','Lease identity mismatch',403)

    def status(self):
        with self.lock:
            self._expire()
            return {'schema':c.SCHEMA,'status':'SYNTHETIC_READY' if self.synthetic and self.state=='READY' else self.state,
                'selected_step':self.step,'step':self.step,'busy':self.inflight,'closing':self.closing,
                'lease':{'active':self.lease is not None},'synthetic_fixture':self.synthetic,
                'service_nonce':self.service_nonce,'config_sha256':self.config_sha256,
                'contract_fingerprint':self.fingerprint,'model_switch_supported':False,'robot_commands':0}

    def health(self):
        with self.lock:
            status=self.status();ready=self.state=='READY' and not self.closing and not self.synthetic
            return {**deepcopy(self.base_health),'ready':ready,'ready_for_inference':ready,
                'status':status['status'],'busy':self.inflight,'closing':self.closing,
                'manager':{'busy':self.inflight,'lease':status['lease']}}

    def acquire(self,session,step,trial_index=0,*,service_nonce=None):
        c.session_value(session);c.step_value(step)
        c.require(type(trial_index) is int and 0<=trial_index<=1_000_000,'INVALID_TRIAL','Invalid trial index',400)
        with self.lock:
            c.require(isinstance(service_nonce,str) and secrets.compare_digest(service_nonce,self.service_nonce),
                      'SERVICE_IDENTITY_MISMATCH','Expected service nonce differs; no lease allocated',403)
            self._expire()
            c.require(self.state=='READY' and not self.closing and step==self.step,'MODEL_NOT_READY','Requested BF16 candidate is not ready',503)
            c.require(self.lease is None and not self.inflight,'DEPLOYMENT_BUSY','A lease or inference is already active')
            self.epoch+=1;token=secrets.token_urlsafe(32)
            self.lease={'session_id':session,'token':token,'trial_index':trial_index,'last_request_index':-1,
                        'expires':self.clock()+c.LEASE_TTL,'epoch':self.epoch}
            return {'session_id':session,'step':self.step,'lease_token':token,'ttl_s':c.LEASE_TTL,
                'heartbeat_interval_s':.5,'trial_index':trial_index,'trial_seed':42+trial_index,
                'contract_fingerprint':self.fingerprint}

    def heartbeat(self,session,token):
        with self.lock:
            self._match(session,token)
            c.require(self.state=='READY' and not self.closing,'MODEL_NOT_READY','Leased BF16 backend unavailable',503)
            self.lease['expires']=self.clock()+c.LEASE_TTL
            return {'ok':True,'active':True,'session_id':session,'step':self.step,'ttl_s':c.LEASE_TTL}

    def release(self,session,token):
        with self.lock:
            self._match(session,token)
            c.require(not self.inflight,'INFERENCE_BUSY','Wait for active inference before release')
            self.lease=None;self.epoch+=1
            return {'ok':True,'released':True,'active':False,'session_id':session,'step':self.step}

    def infer(self,payload,session,token):
        c.require(isinstance(session,str) and isinstance(token,str),'LEASE_REQUIRED','Explicit deployment lease headers required',403)
        required={'state','front_jpeg_base64','right_jpeg_base64','prompt','rtc'}
        c.require(isinstance(payload,dict) and set(payload)==required,'INVALID_INPUT','Original observation fields plus explicit RTC required',400)
        try:request=self.validators.validate_rtc(payload['rtc'])
        except (ValueError,TypeError,OverflowError) as exc:raise c.APIError('INVALID_RTC','RTC input violates frozen contract',400) from exc
        with self.lock:
            self._match(session,token)
            c.require(self.state=='READY' and not self.closing,'MODEL_NOT_READY','BF16 backend unavailable',503)
            c.require(not self.inflight,'INFERENCE_BUSY','Only one GPU inference may be in flight')
            c.require(request['trial_index']==self.lease['trial_index'] and request['request_index']>self.lease['last_request_index'],
                      'RTC_SEQUENCE_MISMATCH','RTC trial/index repeats or reverses leased sequence')
            self.lease['last_request_index']=request['request_index'];epoch=self.epoch;self.inflight=True
            attempt={'trial_index':request['trial_index'],'request_index':request['request_index'],'status':'INFLIGHT'}
            self.attempts.append(attempt)
        try:
            try:observation=self.validators.decode_observation({key:payload[key] for key in required if key!='rtc'})
            except (ValueError,TypeError,OverflowError) as exc:raise c.APIError('INVALID_INPUT','Strict float32/two-JPEG observation validation failed',400) from exc
            observation['rtc']=deepcopy(payload['rtc'])
            started=time.perf_counter()
            try:
                result=self.backend.infer_rtc(observation)
                response=self.validators.encode_result(result,(time.perf_counter()-started)*1000)
                returned=self.validators.json_safe(result.get('rtc'))
                c.validate_response_rtc(returned,request)
                response.update(rtc=returned,contract_fingerprint=self.fingerprint,policy_precision=c.POLICY_PRECISION)
                c.canonical(response)
            except Exception as exc:
                with self.lock:
                    self.state='ERROR';self.lease=None;self.epoch+=1
                raise c.APIError('BF16_INFERENCE_FAILED','BF16 inference/output audit failed; no fallback or action returned',502) from exc
            with self.lock:
                self._match(session,token)
                c.require(not self.closing and self.state=='READY' and self.epoch==epoch,
                          'RESULT_REVOKED','Lease/service changed during inference; result rejected')
                attempt['status']='SUCCESS'
            return response
        finally:
            with self.idle:
                if attempt['status']=='INFLIGHT':attempt['status']='FAILED'
                self.inflight=False;self.idle.notify_all()

    def begin_close(self):
        with self.lock:
            self.closing=True;self.state='STOPPING';self.lease=None;self.epoch+=1

    def wait_idle(self,timeout=None):
        with self.idle:return self.idle.wait_for(lambda:not self.inflight,timeout=timeout)


def make_server(api,*,host=c.HOST,port=c.PORT):
    if host!=c.HOST or type(port) is not int or not 0<=port<=65535:raise ValueError('Loopback-only BF16 listener required')
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.0';server_version='TigerIndependentBF16/1';sys_version=''
        def log_message(self,*args):pass
        def setup(self):
            super().setup();self.connection.settimeout(c.REQUEST_TIMEOUT_SECONDS)
            self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        def reply(self,status,value):
            raw=c.canonical(value);self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.send_header('Connection','close');self.send_header('Cache-Control','no-store');self.end_headers()
            try:self.wfile.write(raw);self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
            if status>=400:
                # Same bounded half-close/drain principle as frozen fp32_http:
                # an unread rejected body must not reset away the HTTP error.
                try:
                    self.connection.shutdown(socket.SHUT_WR);deadline=time.monotonic()+.25;left=c.MAX_BODY_BYTES
                    while left and time.monotonic()<deadline:
                        self.connection.settimeout(max(.001,deadline-time.monotonic()))
                        data=self.rfile.read1(min(65536,left))
                        if not data:break
                        left-=len(data)
                except (OSError,TimeoutError):pass
        def body(self):
            lengths=self.headers.get_all('Content-Length',[])
            c.require(not self.headers.get_all('Transfer-Encoding'),'INVALID_INPUT','Transfer-Encoding unsupported',400)
            c.require(len(lengths)==1 and lengths[0].isascii() and lengths[0].isdecimal(),'INVALID_INPUT','One decimal Content-Length required',400)
            text=lengths[0].lstrip('0') or '0'
            c.require(len(text)<=len(str(c.MAX_BODY_BYTES)) and (len(text)<len(str(c.MAX_BODY_BYTES)) or text<=str(c.MAX_BODY_BYTES)),
                'BODY_TOO_LARGE','Request body too large',413)
            length=int(text);c.require(length>0,'INVALID_INPUT','Nonempty body required',400)
            c.require(len(self.headers.get_all('Content-Type',[]))==1 and self.headers.get_content_type()=='application/json',
                      'INVALID_INPUT','application/json required',400)
            chunks=[];remaining=length;deadline=time.monotonic()+c.REQUEST_TIMEOUT_SECONDS
            while remaining:
                left=deadline-time.monotonic();c.require(left>0,'REQUEST_TIMEOUT','Request body read deadline',408)
                self.connection.settimeout(left);block=self.rfile.read1(min(65536,remaining))
                c.require(bool(block),'INVALID_INPUT','Truncated request body',400);chunks.append(block);remaining-=len(block)
            value=c.strict_json(b''.join(chunks));c.require(isinstance(value,dict),'INVALID_INPUT','JSON object required',400)
            return value
        def do_GET(self):
            if self.path=='/status':self.reply(200,api.status())
            elif self.path=='/health':self.reply(200,api.health())
            else:self.reply(404,{'error':{'code':'UNKNOWN_ENDPOINT','message':'Endpoint not provided'}})
        def do_POST(self):
            if self.path not in ('/lease/acquire','/lease/heartbeat','/lease/release','/infer_rtc'):
                self.reply(404,{'error':{'code':'UNKNOWN_ENDPOINT','message':'Endpoint not provided'}});return
            try:
                body=self.body()
                if self.path=='/lease/acquire':
                    c.require(set(body) in ({'session_id','step'},{'session_id','step','trial_index'}),'INVALID_INPUT','Lease fields differ',400)
                    value=api.acquire(body['session_id'],body['step'],body.get('trial_index',0),
                        service_nonce=self.headers.get(c.SERVICE_NONCE_HEADER))
                elif self.path in ('/lease/heartbeat','/lease/release'):
                    c.require(set(body)=={'session_id','lease_token'},'INVALID_INPUT','Lease fields differ',400)
                    value=(api.heartbeat if self.path.endswith('heartbeat') else api.release)(body['session_id'],body['lease_token'])
                else:value=api.infer(body,self.headers.get(c.SESSION_HEADER),self.headers.get(c.LEASE_HEADER))
                self.reply(200,value)
            except c.APIError as exc:self.reply(exc.status,{'error':{'code':exc.code,'message':str(exc)}})
            except Exception:self.reply(502,{'error':{'code':'SERVICE_OPERATION_FAILED','message':'Independent BF16 operation failed; no fallback'}})
        def unsupported(self):self.reply(405,{'error':{'code':'METHOD_NOT_ALLOWED','message':'Method unsupported'}})
        do_PUT=do_DELETE=do_PATCH=do_OPTIONS=do_HEAD=do_TRACE=do_CONNECT=unsupported
    server=ThreadingHTTPServer((host,port),Handler);server.daemon_threads=True;server.timeout=.2
    return server


def write_new_json(path,value):
    path=Path(path);temporary=path.with_name('.'+path.name+'.tmp-'+str(os.getpid()))
    with temporary.open('x',encoding='utf8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def process_identity():
    proc=Path('/proc/self');raw=(proc/'stat').read_text();fields=raw[raw.rfind(')')+2:].split()
    return {'host':socket.gethostname(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'pid':os.getpid(),'start_ticks':int(fields[19]),'cmdline_sha256':c.file_sha(proc/'cmdline'),'identity_kind':'LinuxProcV1'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--run-dir',type=Path,required=True);parser.add_argument('--step',type=int,required=True)
    parser.add_argument('--execute-online',action='store_true');args=parser.parse_args()
    cfg,config_sha=read_config(args.config);c.step_value(args.step)
    if not args.execute_online:
        print(json.dumps({'status':'DRY_RUN_NO_MODEL_NO_LISTENER','step':args.step,'config_sha256':config_sha,'execute_online':False}));return
    verify_online_environment(cfg,args.run_dir,args.step,True)
    nonce=os.environ.get('TIGER_BF16_SERVICE_NONCE','')
    if re.fullmatch('[0-9a-f]{32}',nonce) is None:raise RuntimeError('Explicit lifecycle-owned service nonce required')
    run=args.run_dir.resolve();identity=process_identity();owner={'schema':c.SCHEMA,'identity':identity,'nonce':nonce,
        'entrypoint':str(Path(__file__).resolve()),'run_dir':str(run),'config_sha256':config_sha,'step':args.step,'status':'LOADING'}
    if (run/'owner.json').exists() or (run/'closed.json').exists():raise FileExistsError('Preserve prior service run receipts')
    write_new_json(run/'owner.json',owner);print(json.dumps({'event':'SERVICE_STARTED',**owner}),flush=True)
    stop=threading.Event();api=None;server=None;backend=None;failure=None;lifecycle={'locks_released':True,'worker_stopped':True}
    previous={}
    def stopping(signum,frame):
        stop.set()
        if api is not None:api.begin_close()
    for sig in (signal.SIGTERM,signal.SIGINT):previous[sig]=signal.signal(sig,stopping)
    try:
        with online_backend(cfg,run,args.step,execute_online=True,stop_event=stop,lifecycle=lifecycle) as backend:
            try:
                api=BF16Service(backend,args.step,service_nonce=nonce,config_sha256=config_sha)
                if stop.is_set():api.begin_close()
                else:
                    server=make_server(api);owner['status']='READY';write_new_json(run/'owner.json',owner)
                    while not stop.is_set():server.handle_request()
            finally:
                # This finally is INSIDE the GPU scope, including listener or
                # event-loop exceptions. Never release locks before draining.
                if api is not None:
                    api.begin_close()
                    while not api.wait_idle(.25):pass
    except BaseException as exc:failure=type(exc).__name__
    finally:
        if api is not None:
            api.begin_close()
            while not api.wait_idle(.25):pass
        if server is not None:server.server_close()
        stopped=lifecycle['worker_stopped']
        closed={**owner,'status':'CLOSED' if stopped and lifecycle['locks_released'] else 'FAILED_UNVERIFIED','locks_released':lifecycle['locks_released'],
            'inflight':False,'worker_stopped':stopped,'startup_or_service_error_type':failure,
            'numerical_quality_accepted':False,'robot_commands':0}
        write_new_json(run/'closed.json',closed);owner['status']=closed['status'];write_new_json(run/'owner.json',owner)
        for sig,handler in previous.items():signal.signal(sig,handler)
    if failure:raise SystemExit(2)


if __name__=='__main__':main()
