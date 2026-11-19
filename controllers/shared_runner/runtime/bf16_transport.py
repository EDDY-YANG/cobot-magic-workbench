"""Bounded loopback HTTP with explicit content length; no model or SDK imports."""
import http.client,json,socket,threading,time

class TransportError(RuntimeError):pass
class BoundedHTTP:
    def __init__(self,port,timeout=3.):
        if port not in (18960,18961):raise ValueError('independent BF16 loopback port required')
        self.port,self.timeout=port,timeout
    def request(self,method,path,payload=None,headers=None,expected_statuses=(200,)):
        if path not in ('/health','/status','/infer_rtc','/lease/acquire','/lease/heartbeat','/lease/release'):
            raise ValueError('BF16 route not permitted')
        body=None if payload is None else json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
        if body is not None and len(body)>8*1024*1024:raise ValueError('request exceeds bound')
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=self.timeout)
        deadline=time.monotonic()+self.timeout;socks=[];expired=threading.Event()
        def expire():
            expired.set()
            for sock in list(socks):
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            conn.close()
        timer=threading.Timer(self.timeout,expire);timer.daemon=True;timer.start()
        try:
            h={'Accept':'application/json','Connection':'close',**(headers or {})}
            if body is not None:h.update({'Content-Type':'application/json','Content-Length':str(len(body))})
            conn.request(method,path,body=body,headers=h)
            if conn.sock is not None:socks.append(conn.sock)
            response=conn.getresponse()
            length=response.getheader('Content-Length','')
            if not length.isdecimal() or int(length)>4*1024*1024 or response.getheader('Transfer-Encoding'):
                raise TransportError('bounded non-chunked JSON response required')
            if response.getheader('Content-Type','').split(';')[0].strip()!='application/json':raise TransportError('non-JSON response')
            remaining=int(length);raw=bytearray()
            while remaining:
                left=deadline-time.monotonic()
                if left<=0 or expired.is_set():raise TransportError('HTTP whole-call deadline')
                for sock in socks:
                    if sock.fileno()>=0:sock.settimeout(left)
                chunk=response.read1(min(65536,remaining))
                if not chunk:raise TransportError('truncated HTTP response')
                raw.extend(chunk);remaining-=len(chunk)
            if time.monotonic()>deadline or expired.is_set():raise TransportError('HTTP whole-call deadline')
            def pairs(items):
                result={}
                for k,v in items:
                    if k in result:raise TransportError('duplicate response key')
                    result[k]=v
                return result
            def bad(_):raise TransportError('nonfinite response')
            value=json.loads(raw,object_pairs_hook=pairs,parse_constant=bad)
            if not isinstance(value,dict):raise TransportError('object response required')
            if response.status not in expected_statuses:raise TransportError('BF16 HTTP '+str(response.status)+': '+str(value.get('error','rejected'))[:500])
            return value
        finally:
            timer.cancel();timer.join(.2);conn.close()
