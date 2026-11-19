"""Portable Windows entry: offline replay and an explicitly configured SSH client."""
from pathlib import Path
import argparse,functools,http.client,json,os,secrets,sys,threading,urllib.parse,webbrowser
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from client.gateway import Config,Tunnel,make_server,existing_client

VERSION='0.1.2'
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8926)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args();origin=f'http://127.0.0.1:{args.port}';nonce=secrets.token_urlsafe(32)
    state_path=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'EmergeWorkbench/client.local.json'
    active=[]
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*a,**kw):super().__init__(*a,directory=str(ROOT/'site'),**kw)
        def log_message(self,*a):pass
        def setup(self):super().setup();self.connection.settimeout(10)
        def reply(self,status,data):
            raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def valid_host(self):return self.headers.get('Host')==f'127.0.0.1:{args.port}'
        def list_directory(self,path):self.send_error(403,'Directory listing disabled')
        def do_GET(self):
            if not self.valid_host():return self.reply(403,{'error':'Unexpected Host'})
            path=urllib.parse.urlsplit(self.path).path
            if path=='/sdk/health':return self.reply(200,{'schema':'cobot-portable-sdk-v1','version':VERSION,'robot_commands':0})
            if path=='/sdk/config':
                saved={}
                if state_path.is_file():
                    try:saved=json.loads(state_path.read_text(encoding='utf8'))
                    except (ValueError,OSError):pass
                return self.reply(200,{'csrf':nonce,'config':saved,'active':active[0].snapshot() if active else None})
            if path.startswith('/sdk/'):return self.reply(404,{'error':'Unknown SDK route'})
            target=(ROOT/'site'/urllib.parse.unquote(path).lstrip('/')).resolve()
            try:target.relative_to((ROOT/'site').resolve())
            except ValueError:return self.reply(403,{'error':'Path rejected'})
            super().do_GET()
        def do_POST(self):
            if not self.valid_host() or self.headers.get('Origin')!=origin or not secrets.compare_digest(self.headers.get('X-SDK-Token',''),nonce):return self.reply(403,{'error':'Local origin/token required'})
            if self.path!='/sdk/connect':return self.reply(404,{'error':'Unknown SDK route'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=4096 or self.headers.get('Content-Type')!='application/json':raise ValueError('Bounded JSON required')
                body=json.loads(self.rfile.read(size))
                if set(body)!={'ssh_target','ssh_config','remote_port','local_port'}:raise ValueError('Unexpected configuration fields')
                ssh=ROOT/'runtime/openssh/ssh.exe'
                cfg=Config(body['ssh_target'],body['ssh_config'] or None,int(body['remote_port']),int(body['local_port']),str(ssh) if ssh.is_file() else 'ssh')
                cfg.validate()
                if cfg.local_port==args.port:raise ValueError('Choose a different client port')
                if active:raise ValueError('An SSH client is already running. Close this SDK process before changing its connection.')
                transport=Tunnel(cfg,start=False)
                # Bind first: an unbound Windows loopback port can time out before
                # the OS reports ECONNREFUSED. Only probe an occupied port.
                try:server=make_server(transport,cfg.local_port)
                except OSError:
                    if existing_client(cfg):return self.reply(200,{'url':f'http://127.0.0.1:{cfg.local_port}/','reused':True})
                    raise ValueError('Client port unavailable; choose a free port')
                transport.worker.start();threading.Thread(target=server.serve_forever,daemon=True).start();active.append(transport)
                state_path.parent.mkdir(parents=True,exist_ok=True)
                state_path.write_text(json.dumps({'ssh_target':cfg.ssh_target,'ssh_config':cfg.ssh_config or '','remote_port':cfg.remote_port,'local_port':cfg.local_port},indent=2),encoding='utf8')
                self.reply(200,{'url':f'http://127.0.0.1:{cfg.local_port}/','robot_commands':0})
            except (ValueError,OSError,RuntimeError,TypeError,KeyError) as e:self.reply(400,{'error':str(e)})
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:
        c=http.client.HTTPConnection('127.0.0.1',args.port,timeout=2)
        try:
            c.request('GET','/sdk/health');r=c.getresponse();info=json.loads(r.read(4096))
            if r.status!=200 or info.get('schema')!='cobot-portable-sdk-v1' or info.get('version')!=VERSION:raise RuntimeError('Port belongs to another application; use --port with a free port')
        finally:c.close()
        if not args.no_browser:webbrowser.open(origin+'/start.html')
        return
    server.daemon_threads=True
    if not args.no_browser:webbrowser.open(origin+'/start.html')
    try:server.serve_forever()
    finally:
        for transport in active:transport.close()
        server.server_close()

if __name__=='__main__':main()
