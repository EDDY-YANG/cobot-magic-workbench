"""Loopback-only inspection of an original UI archive. Never forwards a write."""
import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import shutil
import threading
from urllib.parse import unquote, urlsplit
import webbrowser

READ_PATHS = {
    'workbench': {'/api/state','/api/storage','/api/v2/speed','/api/semantic','/api/semantic/review','/api/presence'},
    'review': {'/api/catalog','/api/history','/api/metadata','/api/attachment','/api/video','/api/export.json','/api/export.csv'},
}
WORK_PREFIXES = ('/api/jobs/', '/api/image/', '/api/semantic/image/', '/api/semantic/video/', '/api/presence/image/', '/api/presence/video/')


def presentation(html, kind, ports, original=False):
    # Only route the links between the two applications to their inspection ports.
    for previous, port in ((8873, ports[0]), (8891, ports[1])):
        html = html.replace(f'href="http://127.0.0.1:{previous}/', f'href="http://127.0.0.1:{port}/')
    if original:
        return html
    if 'src="/operator-theme.js"' not in html:
        html = html.replace('</body>', '<link rel="stylesheet" href="/operator-theme.css"><script defer src="/operator-theme.js"></script></body>', 1)
    return html.replace('<body ', '<body data-ui-review="true" ', 1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def answer(self, status, body, media='application/json', head=False):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', media)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        if not head:
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass

    def reject_write(self):
        self.answer(403, {'error':'Review only / 只读检视：本次写入未转发至任务后台。'})
    do_POST = do_PUT = do_DELETE = do_PATCH = reject_write

    def do_HEAD(self):
        self.read(True)

    def do_GET(self):
        self.read(False)

    def read(self, head):
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
            return self.answer(403, {'error':'Loopback host required'}, head=head)
        route = urlsplit(self.path)
        path = unquote(route.path)
        if route.netloc or route.scheme or '..' in path.split('/') or '\\' in path or path.startswith('//'):
            return self.answer(400, {'error':'Invalid path'}, head=head)
        if path=='/__appearance/health':
            return self.answer(200, {'schema':'cobot-native-ui-inspection-v1', 'kind':self.server.kind, 'readonly':True},head=head)
        name = 'index.html' if path=='/' else path.lstrip('/')
        if name in self.server.members:
            file = self.server.root / self.server.kind / name
            data = file.read_bytes()
            if name=='index.html':
                data = presentation(data.decode(), self.server.kind, self.server.ports, route.query=='original=1').encode()
            mime = {'.mjs':'text/javascript','.js':'text/javascript','.urdf':'application/xml'}.get(file.suffix) or mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
            return self.answer(200,data,mime,head)
        allowed = path in READ_PATHS[self.server.kind] or (self.server.kind=='workbench' and path.startswith(WORK_PREFIXES))
        if not allowed:
            return self.answer(404, {'error':'Unknown read endpoint'}, head=head)
        conn=http.client.HTTPConnection('127.0.0.1',self.server.upstream,timeout=12)
        sent_headers=False
        try:
            headers={'Host':f'127.0.0.1:{self.server.upstream}'}
            if self.headers.get('Range'): headers['Range']=self.headers['Range']
            conn.request('GET',self.path,headers=headers)
            reply=conn.getresponse()
            # A read-only tab never needs the real write-session secrets.
            if reply.status==200 and path in ('/api/state','/api/catalog'):
                data=json.load(reply)
                key='csrf_token' if path=='/api/state' else 'csrf'
                if key in data: data[key]='inspection-only'
                return self.answer(200,data,head=head)
            self.send_response(reply.status)
            for key in ('Content-Type','Content-Length','Content-Range','Accept-Ranges','Content-Disposition'):
                if reply.getheader(key): self.send_header(key,reply.getheader(key))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            sent_headers=True
            if not head: shutil.copyfileobj(reply,self.wfile,65536)
        except (OSError, ValueError, http.client.HTTPException):
            if not sent_headers: self.answer(503,{'error':'Source unavailable / 原服务不可达，当前没有实时数据。'},head=head)
        finally:
            conn.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--port',type=int,default=8952)
    parser.add_argument('--main-upstream',type=int,default=8875)
    parser.add_argument('--review-upstream',type=int,default=8891)
    parser.add_argument('--open',action='store_true')
    args=parser.parse_args()
    root=args.root.resolve()
    manifest=json.loads((root/'assets.json').read_text(encoding='utf-8'))
    for kind,files in manifest.items():
        for name,expected in files.items():
            path=(root/kind/name).resolve()
            if not path.is_relative_to(root/kind) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                raise ValueError('Archive member mismatch: '+name)
    ports=(args.port,args.port+1)
    servers=[]
    try:
        for idx,(kind,upstream) in enumerate((('workbench',args.main_upstream),('review',args.review_upstream))):
            server=ThreadingHTTPServer(('127.0.0.1',ports[idx]),Handler)
            servers.append(server)
            server.kind,server.root,server.members=kind,root,manifest[kind]
            server.ports,server.upstream=ports,upstream
        threading.Thread(target=servers[1].serve_forever,daemon=True).start()
        url=f'http://127.0.0.1:{args.port}/#deploy'
        if args.open: webbrowser.open(url)
        print(url, flush=True)
        servers[0].serve_forever()
    finally:
        for server in servers: server.server_close()


if __name__=='__main__':
    main()
