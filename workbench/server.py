"""Loopback-only HTTP server for the local research console."""
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from .service import ServiceError, parse_json

MAX_BODY = 128 * 1024


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(service, host="127.0.0.1", port=8765, cloud_auth=None):
    if host != "127.0.0.1" and not (cloud_auth is not None and host == '0.0.0.0'):
        raise ValueError("Only 127.0.0.1 binding is supported")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("Port must be in 0..65535")

    class Handler(BaseHTTPRequestHandler):
        server_version = "MetaHarness/0.10"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, fmt, *args):
            # URLs/questions may contain private project information.
            pass

        def response(self, data, status=200, content_type="application/json; charset=utf-8", download=None, headers=()):
            if isinstance(data, str):
                data = data.encode("utf-8")
            elif not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def guard(self):
            port = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if port == 80:
                allowed |= {"127.0.0.1", "localhost"}
            if cloud_auth is not None:
                allowed = {urlsplit(cloud_auth.config.public_origin).netloc}
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or hosts[0].lower() not in allowed:
                raise ServiceError("Invalid Host header", "invalid_host", 403)
            origins = self.headers.get_all("Origin", [])
            allowed_origins = {cloud_auth.config.public_origin} if cloud_auth else {"http://" + host for host in allowed}
            if len(origins) > 1 or origins and origins[0] not in allowed_origins:
                raise ServiceError("Cross-origin requests are not permitted", "invalid_origin", 403)
            if self.headers.get("Sec-Fetch-Site") == "cross-site" and not (cloud_auth and self.command == 'GET' and urlsplit(self.path).path == '/auth/yandex/callback'):
                raise ServiceError("Cross-site requests are not permitted", "cross_site", 403)
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                raise ServiceError("Absolute request targets are not supported", "invalid_path", 400)
            path = unquote(parsed.path)
            if "\\" in path or "\x00" in path or any(part == ".." for part in path.split("/")):
                raise ServiceError("Invalid path", "invalid_path", 400)
            return path, parse_qs(parsed.query, keep_blank_values=True, max_num_fields=32)

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise ServiceError("Transfer-Encoding is not supported", "invalid_length", 400)
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ServiceError("Content-Length is required", "invalid_length", 411)
            try:
                size = int(lengths[0])
            except ValueError as exc:
                raise ServiceError("Invalid Content-Length", "invalid_length") from exc
            if size < 0 or size > MAX_BODY:
                raise ServiceError("Request body exceeds 128 KiB", "body_limit", 413)
            if self.headers.get_content_type() != "application/json":
                raise ServiceError("Content-Type must be application/json", "invalid_content_type", 415)
            data = self.rfile.read(size)
            if len(data) != size:
                raise ServiceError("Incomplete request body", "incomplete_body")
            return parse_json(data.decode("utf-8"))

        def dispatch_get(self, path, query):
            if path == '/api/science/catalog':
                from .science import catalogue
                return self.response(catalogue())
            if path == '/api/offline':
                if cloud_auth is not None:
                    raise ServiceError('Offline runtime is local only','local_only',403)
                return self.response(service.offline.snapshot())
            if path in ('/api/studio', '/api/studio/export'):
                return self.response(service.studio.snapshot(self.owner), download='neuromorph-workspace.json' if path.endswith('/export') else None)
            if path == '/api/harnesses':
                return self.response(service.harnesses_status())
            if path == '/api/harnesses/runs':
                return self.response(service.harnesses_runs())
            brain_routes = {'/api/brain': 'status', '/api/brain/sessions': 'sessions',
                            '/api/brain/session': 'session', '/api/brain/export': 'export'}
            if path in brain_routes:
                return self.response(service.brain_call(brain_routes[path], {'id': query.get('id', [''])[0]}),
                                     download='meta-harness-brain.json' if path.endswith('/export') else None)
            federation_routes = {"/api/federation": "status", "/api/federation/offers": "offers",
              "/api/federation/resources": "resources", "/api/federation/candidates": "candidates",
              "/api/federation/reviews": "reviews", "/api/federation/export": "export"}
            if path in federation_routes:
                return self.response(service.federation_call(federation_routes[path]), download="meta-harness-federation.json" if path.endswith('/export') else None)
            society_routes = {"/api/society": "status", "/api/society/agents": "agents", "/api/society/export": "export"}
            if path in society_routes:
                return self.response(service.society_call(society_routes[path], {"q": query.get("q", [""])[0]}), download="meta-harness-society.json" if path.endswith("/export") else None)
            network_routes = {"/api/network": "status", "/api/resources": "resources", "/api/campaigns": "campaigns", "/api/accounts": "accounts", "/api/network/export": "export"}
            if path in network_routes:
                return self.response(service.network_call(network_routes[path], {"q": query.get("q", [""])[0]}), download="meta-harness-network.json" if path.endswith("/export") else None)
            routes = {"/api/status": service.status, "/api/plugins": service.plugins,
              "/api/runs": service.runs, "/api/advisors": service.advisors,
              "/api/council": service.councils, "/api/workflows": service.workflows,
              "/api/environment": service.environment, "/api/cloud-status": service.cloud_status,
              "/api/roadmap": service.roadmap,
              "/api/audit": service.audit}
            if path == "/api/sources":
                return self.response(service.sources(query.get("q", [""])[0]))
            if path in routes:
                return self.response(routes[path]())
            if path == "/api/export":
                return self.response(service.export(), download="meta-harness-snapshot.json")
            if path == "/api/report":
                return self.response(service.report(), content_type="text/markdown; charset=utf-8", download="meta-harness-report.md")
            if path.startswith("/api/"):
                raise ServiceError("Endpoint not found", "not_found", 404)
            web = (service.root / "web").resolve()
            target = (web / (("studio.html" if cloud_auth else "index.html") if path == "/" else path.lstrip("/"))).resolve()
            if not target.is_relative_to(web) or not target.is_file():
                raise ServiceError("File not found", "not_found", 404)
            if target.suffix.lower() not in {".html", ".css", ".js", ".svg", ".png", ".jpg", ".ico", ".woff", ".woff2"}:
                raise ServiceError("File type not served", "not_found", 404)
            mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return self.response(target.read_bytes(), content_type=mime + ("; charset=utf-8" if mime.startswith("text/") else ""))

        def do_GET(self):
            self.handle_operation(False)

        def do_HEAD(self):
            self.handle_operation(False)

        def do_POST(self):
            self.handle_operation(True)

        def do_OPTIONS(self):
            self.handle_operation(False, unsupported=True)

        def do_PUT(self):
            self.handle_operation(False, unsupported=True)

        def do_DELETE(self):
            self.handle_operation(False, unsupported=True)

        def handle_operation(self, post, unsupported=False):
            try:
                path, query = self.guard()
                self.owner = 'local'
                if cloud_auth is not None:
                    from .cloud_auth import AuthError
                    try:
                        if path == '/auth/login' and self.command == 'GET':
                            login = cloud_auth.begin()
                            return self.response('', 302, headers=[('Location', login['authorize_url']), ('Set-Cookie', login['set_cookie'])])
                        if path == '/auth/yandex/callback' and self.command == 'GET':
                            if any(len(v) != 1 for v in query.values()):
                                raise ServiceError('Invalid OAuth response', 'invalid_oauth', 400)
                            login = cloud_auth.callback({k:v[0] for k,v in query.items()}, self.headers.get('Cookie',''))
                            return self.response('', 302, headers=[('Location','/studio.html'),('Set-Cookie',login['set_cookie']),('Set-Cookie',login['clear_oauth_cookie'])])
                        if path.startswith('/api/') or path == '/auth/logout':
                            session = cloud_auth.authorize_write(self.headers.get('Cookie',''), self.headers.get('Origin',''), self.headers.get('X-CSRF-Token','')) if post else cloud_auth.session(self.headers.get('Cookie',''))
                            self.owner = session['subject']
                            if path == '/api/session':
                                return self.response({'authenticated':True, 'csrf_token':session['csrf_token'], 'expires_at':session['expires_at']})
                            if path == '/auth/logout' and post:
                                result = cloud_auth.logout(self.headers.get('Cookie',''),self.headers.get('Origin',''),self.headers.get('X-CSRF-Token',''))
                                return self.response({'logged_out':True}, headers=[('Set-Cookie',result['set_cookie'])])
                    except AuthError as exc:
                        return self.response({'error':{'code':exc.code,'message':'Authentication or request verification failed.'}}, getattr(exc,'status',401))
                elif path == '/api/session':
                    return self.response({'authenticated':False,'mode':'local','csrf_token':None})
                if unsupported:
                    raise ServiceError("Method not supported", "method_not_allowed", 405)
                if not post:
                    return self.dispatch_get(path, query)
                if path == '/api/studio/save':
                    return self.response(service.studio.save(self.body(), self.owner))
                if path == '/api/science/protocol':
                    from .science import protocol_task
                    return self.response(service.studio.save(protocol_task(self.body()), self.owner))
                if path in ('/api/offline/start','/api/offline/stop'):
                    if cloud_auth is not None:
                        raise ServiceError('Offline runtime is local only','local_only',403)
                    data=self.body()
                    if path.endswith('/start'):
                        return self.response(service.offline.start(data),202)
                    if data != {}:
                        raise ServiceError('Stop expects an empty object')
                    return self.response(service.offline.stop())
                if path == '/api/harnesses/run':
                    return self.response(service.harnesses_run(self.body()))
                routes = {"/api/sources": service.add_source, "/api/run": service.run,
                          "/api/council": service.council, "/api/workflow": service.workflow}
                brain_routes = {'/api/brain/' + name: name for name in ('start', 'tick', 'cancel')}
                if path in brain_routes:
                    return self.response(service.brain_call(brain_routes[path], self.body()))
                federation_routes = {"/api/federation/" + name: name for name in
                  ("discover", "card", "offer", "resource", "review", "proposal", "cancel", "revoke")}
                if path in federation_routes:
                    return self.response(service.federation_call(federation_routes[path], self.body()))
                society_routes = {"/api/society/directory": "directory", "/api/society/route": "route", "/api/society/mission": "mission",
                    "/api/society/mission/tick": "tick", "/api/society/mission/cancel": "cancel",
                    "/api/society/claim": "claim", "/api/society/snapshot": "snapshot"}
                if path in society_routes:
                    return self.response(service.society_call(society_routes[path], self.body()))
                network_routes = {"/api/discover": "discover", "/api/plan": "plan", "/api/campaign": "campaign",
                  "/api/campaign/tick": "tick", "/api/campaign/cancel": "cancel_campaign", "/api/job/cancel": "cancel_job",
                  "/api/accounts": "account_save", "/api/accounts/request": "account_request", "/api/local-resources": "inspect_local"}
                if path in network_routes:
                    return self.response(service.network_call(network_routes[path], self.body()))
                if path not in routes:
                    raise ServiceError("Endpoint not found", "not_found", 404)
                return self.response(routes[path](self.body()), 201 if path == "/api/sources" else 200)
            except ServiceError as exc:
                self.response({"error": {"message": exc.message, "code": exc.code}}, exc.status)
            except (ValueError, UnicodeError, RecursionError):
                self.response({"error": {"message": "Invalid request encoding or data", "code": "invalid_request"}}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.response({"error": {"message": "Internal server error", "code": "internal_error"}}, 500)

    return LocalServer((host, port), Handler)
