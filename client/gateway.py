"""Portable SSH thin client. All operational pages/API/code stay on the server.

Python standard library + installed OpenSSH. No robot SDK, model packages,
credentials, web mirror or automatic remote application command is included.
"""
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse,html,http.client,json,os,re,shutil,socket,subprocess,threading,time,webbrowser

VERSION='0.1.0-alpha.1'
CLIENT_SCHEMA='agilex-thin-client-health-v1'
HEALTH_SCHEMA='agilex-platform-health-v1'
MAX_REQUEST=16384

@dataclass(frozen=True)
class Config:
    ssh_target:str
    ssh_config:str|None=None
    remote_port:int=8910
    local_port:int=8875
    ssh_executable:str='ssh'

    @classmethod
    def read(cls,path):
        value=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        allowed={'schema','ssh_target','ssh_config','remote_port','local_port','ssh_executable'}
        if not isinstance(value,dict) or set(value)-allowed or value.get('schema')!='agilex-client-config-v1':
            raise ValueError('Expected agilex-client-config-v1; unknown settings are refused')
        obj=cls(**{k:v for k,v in value.items() if k!='schema'})
        obj.validate();return obj

    def validate(self):
        if not isinstance(self.ssh_target,str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:%\[\]-]{0,240}',self.ssh_target):
            raise ValueError('ssh_target must be a configured SSH alias or user@host, never shell text')
        for port in (self.remote_port,self.local_port):
            if type(port) is not int or not 1024<=port<=65535:raise ValueError('Ports must be integers in1024..65535')
        if self.ssh_config is not None and not Path(self.ssh_config).expanduser().is_file():
            raise ValueError('SSH config file does not exist')
        if not isinstance(self.ssh_executable,str) or not shutil.which(self.ssh_executable):
            raise ValueError('Install OpenSSH or set ssh_executable to its actual path')

    def command(self,local_forward_port):
        self.validate()
        args=[self.ssh_executable]
        if self.ssh_config:args+=['-F',str(Path(self.ssh_config).expanduser())]
        # No AddressFamily override: an IPv6 server may still use IPv4 local forwarding.
        return args+['-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','ExitOnForwardFailure=yes','-o','ConnectTimeout=6',
            '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=3','-N',
            '-L',f'127.0.0.1:{local_forward_port}:127.0.0.1:{self.remote_port}',self.ssh_target]


def read_health(port,server_port=8910):
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
    try:
        connection.request('GET','/platform/health',headers={'Host':f'127.0.0.1:{server_port}','Connection':'close'})
        response=connection.getresponse();raw=response.read(65537)
        if response.status!=200 or len(raw)>65536:raise ValueError('Server health unavailable or oversized')
        value=json.loads(raw)
        if (value.get('schema')!=HEALTH_SCHEMA or value.get('protocol_version')!=1 or
                value.get('service')!='agilex-control-platform'):
            raise ValueError('Unexpected server identity/protocol; refusing application proxy')
        return value
    finally:connection.close()


class Tunnel:
    def __init__(self,config,*,start=True):
        config.validate();self.config=config;self.lock=threading.RLock();self.quit=threading.Event()
        self.child=None;self.port=None;self.state='SSH_CONNECTING';self.error=None;self.last_check=None
        self.health={};self.stderr=deque(maxlen=8)
        self.worker=threading.Thread(target=self._run,daemon=True,name='agilex-ssh-transport')
        if start:self.worker.start()

    def _set(self,state,error=None,health=None):
        with self.lock:
            self.state=state;self.error=error;self.last_check=time.monotonic()
            if health is not None:self.health=health

    def _drain_error(self,stream):
        try:
            for line in iter(stream.readline,''):
                with self.lock:self.stderr.append(line.strip()[-500:])
        finally:stream.close()

    def _run(self):
        backoff=1
        while not self.quit.is_set():
            process=None
            try:
                self._set('SSH_CONNECTING')
                with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
                kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
                process=subprocess.Popen(self.config.command(port),stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,encoding='utf8',errors='replace',**kwargs)
                with self.lock:self.child=process;self.port=port;self.stderr.clear()
                threading.Thread(target=self._drain_error,args=(process.stderr,),daemon=True).start()
                while not self.quit.is_set() and process.poll() is None:
                    try:
                        health=read_health(port,self.config.remote_port)
                        self._set('CONNECTED',health=health);backoff=1
                    except ValueError as exc:self._set('PROTOCOL_MISMATCH',str(exc))
                    except (OSError,http.client.HTTPException) as exc:self._set('SERVER_UNAVAILABLE',str(exc))
                    self.quit.wait(2)
                if not self.quit.is_set():
                    with self.lock:reason='; '.join(self.stderr) or f'SSH exited {process.returncode}'
                    self._set('SSH_UNAVAILABLE',reason)
            except Exception as exc:self._set('SSH_UNAVAILABLE',str(exc))
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=3)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
                with self.lock:self.child=None;self.port=None
            if self.quit.wait(backoff):break
            backoff=min(backoff*2,10)

    def snapshot(self):
        with self.lock:
            age=None if self.last_check is None else max(0,time.monotonic()-self.last_check)
            state=self.state
            if state=='CONNECTED' and (age is None or age>5):state='SERVER_UNAVAILABLE'
            return {'schema':CLIENT_SCHEMA,'client_version':VERSION,'state':state,
                'ssh_target':self.config.ssh_target,'error':self.error,'last_check_age_s':age,
                'server_release_id':self.health.get('release_id'),
                'server_instance_id':self.health.get('server_instance_id'),
                'website_source':'4090_SERVER','automatic_motion':False,
                'remote_port':self.config.remote_port,'local_port':self.config.local_port}

    def endpoint(self):
        with self.lock:
            if self.snapshot()['state']!='CONNECTED' or self.port is None:
                raise ConnectionError(self.error or 'SSH/server is not ready')
            return self.port

    def close(self):
        self.quit.set()
        with self.lock:p=self.child
        if p is not None and p.poll() is None:p.terminate()
        if self.worker.is_alive():self.worker.join(timeout=5)


def make_server(transport,port):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup();self.connection.settimeout(15)
        def log_message(self,*args):pass
        def reply(self,status,value,mime='application/json; charset=utf-8'):
            raw=json.dumps(value,ensure_ascii=False).encode() if isinstance(value,dict) else value
            try:
                self.send_response(status);self.send_header('Content-Type',mime)
                self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff');self.end_headers()
                if self.command!='HEAD':self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        def valid_host(self):return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'
        def valid_path(self):
            # BaseHTTPRequestHandler normalizes leading // before do_GET.
            raw=self.requestline.split(' ')[1] if len(self.requestline.split(' '))==3 else ''
            return raw.startswith('/') and not raw.startswith('//') and '\\' not in raw
        def relay(self,method,body=None):
            upstream=None;headers_sent=False
            try:
                target=transport.endpoint()
                upstream=http.client.HTTPConnection('127.0.0.1',target,timeout=12)
                remote=f'127.0.0.1:{transport.config.remote_port}'
                headers={'Host':remote,'Connection':'close','X-AgileX-Client-Version':VERSION}
                for name in ('Range','Accept','Content-Type','X-Tiger-Token','Idempotency-Key'):
                    if self.headers.get(name):headers[name]=self.headers[name]
                if method=='POST':headers['Origin']='http://'+remote
                upstream.request(method,self.path,body=body,headers=headers)
                response=upstream.getresponse()
                self.send_response(response.status)
                for name in ('Content-Type','Content-Length','Content-Range','Accept-Ranges','Cache-Control',
                             'Content-Security-Policy','X-Content-Type-Options','X-AgileX-Release'):
                    if response.getheader(name):self.send_header(name,response.getheader(name))
                # No redirection follow, body rewrite, model launch, or POST retry.
                self.send_header('X-AgileX-Website-Source','server');self.send_header('Connection','close')
                self.end_headers();headers_sent=True;self.close_connection=True
                if method!='HEAD':
                    while True:
                        block=response.read(65536)
                        if not block:break
                        self.wfile.write(block)
            except (OSError,http.client.HTTPException,ConnectionError) as exc:
                if not headers_sent:
                    self.reply(503,{'error_code':'UPSTREAM_UNAVAILABLE','message':str(exc),
                        'operation_result':'UNKNOWN_NOT_RETRIED' if method=='POST' else 'UNAVAILABLE',
                        'automatic_retry':False,'model_state':'UNKNOWN'})
                self.close_connection=True
            finally:
                if upstream:upstream.close()
        def do_GET(self):
            if not self.valid_host() or not self.valid_path():return self.reply(403,{'error':'Host/path rejected'})
            if self.path=='/__client/health':return self.reply(200,transport.snapshot())
            if self.path in ('/','/__client') and transport.snapshot()['state']!='CONNECTED':
                state=transport.snapshot()
                body=('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="4"><title>AgileX · 连接状态</title>
<body style="font:16px system-ui;max-width:760px;margin:8vh auto;padding:24px">
<h1>客户端已启动，正在连接服务器</h1><p>模型和设备状态尚未确认。此页面不会启动、重试或恢复机器人动作。</p><pre style="white-space:pre-wrap">'''+
                    html.escape(state['state']+'\n'+(state.get('error') or '等待SSH与服务器协议检查'))+
                    '</pre><p>请核对SSH别名、主机密钥授权和网络。连接恢复后自动显示服务器工作台。</p></body></html>').encode()
                return self.reply(503,body,'text/html; charset=utf-8')
            if self.path.startswith('/__client/'):return self.reply(404,{'error':'Unknown client endpoint'})
            self.relay('GET')
        def do_HEAD(self):
            if not self.valid_host() or not self.valid_path():return self.reply(403,{'error':'Host/path rejected'})
            self.relay('HEAD')
        def do_POST(self):
            expected=f'http://127.0.0.1:{self.server.server_port}'
            if (not self.valid_host() or not self.valid_path() or self.headers.get('Origin')!=expected or
                    not self.headers.get('X-Tiger-Token') or not self.path.startswith('/api/')):
                return self.reply(403,{'error':'Local origin and existing server token required'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=MAX_REQUEST or self.headers.get('Content-Type','').split(';',1)[0]!='application/json':
                    raise ValueError('Bounded JSON body required')
                body=self.rfile.read(size)
                if len(body)!=size:raise ValueError('Incomplete request')
            except (ValueError,OSError) as exc:return self.reply(400,{'error':str(exc)})
            self.relay('POST',body)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.daemon_threads=True
    return server


def existing_client(config):
    connection=http.client.HTTPConnection('127.0.0.1',config.local_port,timeout=1)
    try:
        connection.request('GET','/__client/health',headers={'Host':f'127.0.0.1:{config.local_port}'})
        response=connection.getresponse();raw=response.read(65537)
        if len(raw)>65536:raise RuntimeError('Local port belongs to an unknown service')
        value=json.loads(raw)
        if (response.status!=200 or value.get('schema')!=CLIENT_SCHEMA or
                value.get('ssh_target')!=config.ssh_target or value.get('remote_port')!=config.remote_port):
            raise RuntimeError('Local port is already used by another service/profile; it will not be replaced')
        return True
    except (ConnectionRefusedError,ConnectionResetError):return False
    finally:connection.close()

def main(argv=None):
    parser=argparse.ArgumentParser(description='AgileX thin SSH client; server owns all pages and execution')
    parser.add_argument('--config',type=Path,required=True);parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args(argv);config=Config.read(args.config)
    url=f'http://127.0.0.1:{config.local_port}/'
    if existing_client(config):
        if not args.no_browser:webbrowser.open(url)
        print(json.dumps({'status':'EXISTING_CLIENT_REUSED','url':url,'model_started':False}));return
    transport=Tunnel(config,start=False)
    try:server=make_server(transport,config.local_port)
    except BaseException:transport.close();raise
    transport.worker.start()
    print(json.dumps({'client_version':VERSION,'url':url,'server_owned_website':True,'model_started':False}),flush=True)
    if not args.no_browser:webbrowser.open(url)
    try:server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:pass
    finally:server.server_close();transport.close()

if __name__=='__main__':main()
