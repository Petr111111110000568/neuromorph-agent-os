"""Separate member gateway. Administrator endpoints are never exposed here.

This is the project-specific contribution protocol, not an A2A implementation.
"""
import ipaddress
import json
import ssl
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from ..service import parse_json, ServiceError

MAX_BODY=128*1024

class Gateway(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    request_queue_size=32
    def __init__(self,*args,**kwargs):
        self._connection_slots=threading.BoundedSemaphore(32)
        self._tls_context=None
        super().__init__(*args,**kwargs)
    def process_request(self,request,client_address):
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:super().process_request(request,client_address)
        except Exception:
            self._connection_slots.release()
            self.shutdown_request(request)
            raise
    def process_request_thread(self,request,client_address):
        try:
            # The accept loop always handles raw sockets. A peer that never sends
            # ClientHello can occupy at most one bounded worker, not the listener.
            if self._tls_context is not None:
                request.settimeout(5)
                request=self._tls_context.wrap_socket(request,server_side=True,do_handshake_on_connect=False)
                request.do_handshake()
            super().process_request_thread(request,client_address)
        except (OSError,ssl.SSLError):
            self.shutdown_request(request)
        finally:self._connection_slots.release()

def make_gateway(exchange,host='127.0.0.1',port=8767,certfile=None,keyfile=None,public_base_url=None):
    if not isinstance(port,int) or isinstance(port,bool) or not 0<=port<=65535:raise ValueError('Invalid port')
    if not isinstance(host,str):raise ValueError('Invalid host')
    try:loopback=ipaddress.ip_address(host).is_loopback
    except ValueError:raise ValueError('Bind host must be an IP literal')
    # HTTPServer is IPv4. Do not silently accept an unsupported IPv6 deployment.
    if ':' in host:raise ValueError('This gateway binds IPv4 only')
    if bool(certfile)!=bool(keyfile):raise ValueError('Both certificate and key are required')
    if not loopback and (not certfile or not public_base_url):raise ValueError('Non-loopback binding requires TLS and public_base_url')
    public=None
    if public_base_url:
        public=urlsplit(public_base_url)
        if public.scheme!='https' or not public.hostname or public.username or public.password or public.path not in ('','/') or public.query or public.fragment:
            raise ValueError('public_base_url must be an HTTPS origin')
        _=public.port
    lock=threading.Lock();rates={}

    class Handler(BaseHTTPRequestHandler):
        server_version='MetaHarnessContribution/1'
        sys_version=''
        def setup(self):
            super().setup();self.connection.settimeout(10)
        def log_message(self,*args):pass
        def send_json(self,value,status=200):
            data=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status)
            for k,v in {'Content-Type':'application/json; charset=utf-8','Content-Length':str(len(data)),
              'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
              'Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'",'Connection':'close'}.items():self.send_header(k,v)
            self.end_headers();self.close_connection=True
            if self.command!='HEAD':self.wfile.write(data)
        def guard(self):
            actualport=self.server.server_address[1]
            allowed={f'127.0.0.1:{actualport}',f'localhost:{actualport}'} if loopback else set()
            if public:allowed.add(public.netloc.lower())
            hs=self.headers.get_all('Host',[])
            if len(hs)!=1 or hs[0].lower() not in allowed:raise PermissionError('Invalid Host')
            origins=self.headers.get_all('Origin',[])
            if origins or self.headers.get('Sec-Fetch-Site')=='cross-site':raise PermissionError('Browser-origin member calls are not supported')
            parsed=urlsplit(self.path)
            if parsed.scheme or parsed.netloc or '\\' in parsed.path or '%' in parsed.path or len(self.path)>2048:raise ValueError('Invalid request target')
            now=time.monotonic();peer=self.client_address[0]
            with lock:
                for k in list(rates):
                    if not rates[k] or rates[k][-1]<now-60:rates.pop(k,None)
                if peer not in rates and len(rates)>=1024:raise ServiceError('Gateway busy','rate_limit',429)
                q=rates.setdefault(peer,deque())
                while q and q[0]<now-60:q.popleft()
                if len(q)>=120:raise ServiceError('Request limit exceeded','rate_limit',429)
                q.append(now)
            return parsed.path,parse_qs(parsed.query,max_num_fields=8)
        def body(self):
            ls=self.headers.get_all('Content-Length',[])
            if self.headers.get('Transfer-Encoding') or len(ls)!=1 or not ls[0].isascii() or not ls[0].isdecimal():raise ValueError('Content-Length required')
            size=int(ls[0])
            if size>MAX_BODY:raise ServiceError('Body exceeds 128 KiB','body_limit',413)
            if self.headers.get_content_type()!='application/json':raise ServiceError('JSON required','content_type',415)
            raw=self.rfile.read(size)
            if len(raw)!=size:raise ValueError('Incomplete body')
            value=parse_json(raw.decode())
            if not isinstance(value,dict):raise ValueError('Object required')
            return value
        def token(self):
            values=self.headers.get_all('Authorization',[])
            if len(values)!=1 or not values[0].startswith('Bearer '):raise PermissionError('Member credential required')
            token=values[0][7:]
            if not token or len(token)>256 or any(c.isspace() for c in token):raise PermissionError('Invalid member credential')
            return token
        def operation(self,post=False):
            try:
                path,q=self.guard()
                if not post:
                    if path=='/.well-known/meta-harness.json':
                        result={'name':'Meta-Harness Research Contributions','protocol':'meta-harness-contribution/1',
                          'a2a_compliant':False,'registration':'single_use_invitation_and_explicit_terms',
                          'offers_path':'/v1/offers','join_path':'/v1/join','resources_path':'/v1/resources',
                          'reward':'nontransferable_access_credits_after_manual_review',
                          'data_policy':'public_computational_tasks; no personal genomes exchanged',
                          'operator_identity':'not_independently_verified','base_url':public_base_url or f'http://127.0.0.1:{self.server.server_address[1]}'}
                    elif path=='/v1/offers':result=exchange.list_offers(public=True)
                    elif path=='/v1/resources':result=exchange.list_resources(public=True)
                    elif path=='/v1/member':result=exchange.member(self.token())
                    elif path=='/v1/assignments':result=exchange.assignments(self.token())
                    elif path=='/v1/access':
                        ids=q.get('resource_id',[])
                        if len(ids)!=1:raise ValueError('One resource_id is required')
                        result=exchange.access(self.token(),ids[0])
                    else:raise KeyError('Endpoint not found')
                else:
                    if path=='/v1/join':result=exchange.join(self.body())
                    else:
                        methods={'/v1/claim':exchange.claim,'/v1/submit':exchange.submit,'/v1/redeem':exchange.redeem}
                        if path not in methods:raise KeyError('Endpoint not found')
                        token=self.token();exchange.member(token)
                        result=methods[path](token,self.body())
                self.send_json(result)
            except ServiceError as e:self.send_json({'error':{'code':e.code,'message':e.message}},e.status)
            except PermissionError:self.send_json({'error':{'code':'forbidden','message':'Access denied or invalid credential'}},403)
            except KeyError:self.send_json({'error':{'code':'not_found','message':'Resource or endpoint not found'}},404)
            except (ValueError,UnicodeError,RecursionError) as e:self.send_json({'error':{'code':'invalid_request','message':str(e)[:300]}},400)
            except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
            except Exception:self.send_json({'error':{'code':'internal_error','message':'Gateway could not process request'}},500)
        def do_GET(self):self.operation()
        def do_HEAD(self):self.operation()
        def do_POST(self):self.operation(True)
        def do_OPTIONS(self):self.send_json({'error':{'code':'method_not_allowed'}},405)
        do_PUT=do_OPTIONS
        do_DELETE=do_OPTIONS

    server=Gateway((host,port),Handler)
    if certfile:
        try:
            ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.minimum_version=ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(certfile,keyfile);server._tls_context=ctx
        except Exception:server.server_close();raise
    return server
