"""Local HTTP fixtures only; no SSH subprocess, model or hardware invocation."""
from pathlib import Path
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
import http.client,json,sys,threading,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from client import gateway as g

class Tests(unittest.TestCase):
    def setUp(self):
        self.requests=[];records=self.requests
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_GET(self):
                records.append(('GET',self.path,dict(self.headers)))
                value={'schema':g.HEALTH_SCHEMA,'protocol_version':1,'service':'agilex-control-platform','release_id':'CPU-TEST'} if self.path=='/platform/health' else {'content':'SERVER_OWNED_ASSET','synthetic_fixture':True}
                raw=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']));records.append(('POST',self.path,dict(self.headers),body))
                raw=b'{"synthetic_fixture":true,"status":"FAILED_ONCE"}'
                self.send_response(503);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.up=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        threading.Thread(target=self.up.serve_forever,daemon=True).start()
        up_port=self.up.server_port
        class Transport:
            config=SimpleNamespace(remote_port=up_port)
            ready=True
            def endpoint(self):
                if not self.ready:raise ConnectionError('CPU upstream absent')
                return up_port
            def snapshot(self):
                return {'schema':g.CLIENT_SCHEMA,'ssh_target':'CPU-fixture','remote_port':up_port,
                        'state':'CONNECTED' if self.ready else 'SSH_UNAVAILABLE','error':None,'automatic_motion':False}
        self.transport=Transport();self.server=g.make_server(self.transport,0)
        threading.Thread(target=self.server.serve_forever,daemon=True).start();self.port=self.server.server_port

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.up.shutdown();self.up.server_close()

    def request(self,path='/',method='GET',body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=2)
        try:
            c.request(method,path,body,headers or {});r=c.getresponse();return r.status,r.read()
        finally:c.close()

    def test_every_operational_asset_comes_from_upstream(self):
        code,raw=self.request('/platform-assets/platform.mjs')
        self.assertEqual(code,200);self.assertEqual(json.loads(raw)['content'],'SERVER_OWNED_ASSET')
        self.assertEqual(self.requests[0][2]['Host'],f'127.0.0.1:{self.up.server_port}')

    def test_post_rewrites_origin_keeps_token_and_never_retries(self):
        headers={'Origin':f'http://127.0.0.1:{self.port}','Content-Type':'application/json','X-Tiger-Token':'CPU-ONLY'}
        code,_=self.request('/api/jobs','POST',b'{"action":"CPU_ONLY","params":{}}',headers)
        self.assertEqual(code,503);self.assertEqual(len(self.requests),1)
        sent=self.requests[0];self.assertEqual(sent[2]['Origin'],f'http://127.0.0.1:{self.up.server_port}')
        self.assertEqual(sent[2]['X-Tiger-Token'],'CPU-ONLY')

    def test_cross_origin_and_oversize_post_are_not_forwarded(self):
        h={'Origin':'http://untrusted.invalid','Content-Type':'application/json','X-Tiger-Token':'CPU-ONLY'}
        self.assertEqual(self.request('/api/jobs','POST',b'{}',h)[0],403)
        h['Origin']=f'http://127.0.0.1:{self.port}'
        self.assertEqual(self.request('/api/jobs','POST',b'x'*16385,h)[0],400)
        self.assertEqual(self.requests,[])

    def test_disconnection_shows_no_cached_model_readiness(self):
        self.transport.ready=False
        code,raw=self.request('/')
        self.assertEqual(code,503);self.assertIn('尚未确认'.encode(),raw)
        code,raw=self.request('/api/state');self.assertEqual(code,503)
        self.assertEqual(json.loads(raw)['model_state'],'UNKNOWN');self.assertEqual(self.requests,[])

    def test_host_rebinding_and_local_endpoint_identity(self):
        self.assertEqual(self.request('/',headers={'Host':'untrusted.invalid'})[0],403)
        self.assertEqual(self.request('//untrusted.invalid/path')[0],403)
        code,raw=self.request('/__client/health');self.assertEqual(code,200)
        self.assertEqual(json.loads(raw)['schema'],g.CLIENT_SCHEMA)
        self.assertEqual(self.requests,[])

    def test_health_contract_and_existing_client_reuse(self):
        value=g.read_health(self.up.server_port,self.up.server_port)
        self.assertEqual(value['release_id'],'CPU-TEST')
        config=g.Config('CPU-fixture',remote_port=self.up.server_port,local_port=self.port)
        self.assertTrue(g.existing_client(config))
        with self.assertRaises(RuntimeError):g.existing_client(g.Config('another-profile',local_port=self.port))

    def test_ssh_command_is_argument_list_with_strict_host_identity(self):
        with patch.object(g.shutil,'which',return_value='/usr/bin/ssh'):
            args=g.Config('authorized-server').command(29999)
            self.assertIn('StrictHostKeyChecking=yes',args);self.assertIn('BatchMode=yes',args)
            self.assertNotIn('AddressFamily=inet6',args)
            self.assertIn('127.0.0.1:29999:127.0.0.1:8910',args)
            with self.assertRaises(ValueError):g.Config('-oProxyCommand=anything').command(29999)

if __name__=='__main__':unittest.main(verbosity=2)
