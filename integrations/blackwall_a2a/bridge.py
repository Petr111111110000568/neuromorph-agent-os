"""Blackwall: local, read-only subset of A2A 1.0 HTTP+JSON.

Agent Card + SendMessage + GetTask + ListTasks. This is not the complete A2A
protocol: no SSE, multi-turn, push, cancellation of running tasks or SDK conformance.
No LLM-generated commands are run. The adapter reads four fixed local URLs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.request import Request, urlopen
import uuid

AGENT_NAME = "Blackwall Read-only Agent"
BIND_HOST = "127.0.0.1"
PROTOCOL_VERSION = "1.0"
MAX_BODY_BYTES = 4096
MAX_RESPONSE_BYTES = 90000
SKILLS = {
    "status": "/api/system",
    "audit": "/api/audit/verify",
    "cognition": "/api/cognition/manifest",
}
CMD_RE = re.compile(r"^(status|audit|cognition|plan [0-4] [0-4])$")
ID_RE = re.compile(r"^[A-Za-z0-9:_-][A-Za-z0-9:_.-]{7,95}$")
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class ValidationError(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


class TaskStore:
    def __init__(self, location):
        self.location = Path(location)
        self.location.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.location.parent.chmod(0o700)
        self._initialize()

    def connection(self):
        conn = sqlite3.connect(self.location, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _initialize(self):
        conn = self.connection()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, message_id TEXT UNIQUE NOT NULL,
                    request_hash TEXT NOT NULL, context_id TEXT NOT NULL,
                    state TEXT NOT NULL, skill TEXT NOT NULL,
                    artifact_json TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created_at);
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL, task_id TEXT,
                    created_at TEXT NOT NULL, prev_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL);
            """)
            conn.execute("BEGIN IMMEDIATE")
            # Never claim to have completed work interrupted by a process crash.
            for row in conn.execute("SELECT id FROM tasks WHERE state='TASK_STATE_SUBMITTED'"):
                conn.execute("UPDATE tasks SET state='TASK_STATE_FAILED',updated_at=? WHERE id=?",
                             (timestamp(), row["id"]))
                self._audit(conn, "interrupted_on_restart", row["id"])
            conn.commit()
            self.location.chmod(0o600)
        finally:
            conn.close()

    @staticmethod
    def _audit(conn, event, task_id):
        last = conn.execute("SELECT event_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        previous = last["event_hash"] if last else "0" * 64
        moment = timestamp()
        entry_hash = digest("\n".join([previous, event, task_id or "", moment]))
        conn.execute("INSERT INTO audit(event,task_id,created_at,prev_hash,event_hash) "
                     "VALUES(?,?,?,?,?)", (event, task_id, moment, previous, entry_hash))

    def audit_verified(self):
        conn = self.connection()
        try:
            previous = "0" * 64
            count = 0
            for row in conn.execute("SELECT * FROM audit ORDER BY seq"):
                candidate = digest("\n".join([previous, row["event"],
                                                 row["task_id"] or "", row["created_at"]]))
                if candidate != row["event_hash"] or previous != row["prev_hash"]:
                    return {"valid": False, "verifiedEvents": count}
                previous, count = candidate, count + 1
            return {"valid": True, "verifiedEvents": count, "head": previous}
        finally:
            conn.close()

    def begin(self, message_id, request_hash, context_id, skill):
        conn = self.connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT id,request_hash FROM tasks WHERE message_id=?",
                                    (message_id,)).fetchone()
            if existing:
                conn.rollback()
                if existing["request_hash"] != request_hash:
                    raise ValidationError("messageId already used with different content", 409)
                return existing["id"], False
            task_id = str(uuid.uuid4())
            now = timestamp()
            conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)",
                         (task_id, message_id, request_hash, context_id,
                          "TASK_STATE_SUBMITTED", skill, None, now, now))
            self._audit(conn, "task_submitted", task_id)
            conn.commit()
            return task_id, True
        finally:
            conn.close()

    def finish(self, task_id, result, ok):
        conn = self.connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] != "TASK_STATE_SUBMITTED":
                conn.rollback()
                raise ValidationError("Task is not submitted", 409)
            state = "TASK_STATE_COMPLETED" if ok else "TASK_STATE_FAILED"
            artifact = {
                "artifactId": str(uuid.uuid4()),
                "name": "read-only-observation",
                "parts": [{"data": result, "mediaType": "application/json"}],
            }
            conn.execute("UPDATE tasks SET state=?,artifact_json=?,updated_at=? WHERE id=?",
                         (state, canonical(artifact), timestamp(), task_id))
            self._audit(conn, "task_finished_" + ("ok" if ok else "failed"), task_id)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def to_task(row):
        task = {
            "id": row["id"],
            "contextId": row["context_id"],
            "status": {"state": row["state"], "timestamp": row["updated_at"]},
        }
        if row["artifact_json"] is not None:
            task["artifacts"] = [json.loads(row["artifact_json"])]
        return task

    def get(self, task_id):
        conn = self.connection()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return self.to_task(row) if row else None
        finally:
            conn.close()

    def listing(self, limit=20, offset=0):
        conn = self.connection()
        try:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC,id DESC "
                                "LIMIT ? OFFSET ?",
                                (min(max(int(limit), 1), 50), max(0, int(offset)))).fetchall()
            return [self.to_task(row) for row in rows]
        finally:
            conn.close()

    def total_count(self):
        conn = self.connection()
        try:
            return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()


class LocalBackend:
    """Fixed localhost upstream; cannot fetch URLs, run code, or mutate anything."""

    def __init__(self, operator_token):
        if not operator_token or len(operator_token) < 24:
            raise ValueError("BLACKWALL_OPERATOR_TOKEN must be configured")
        self.operator_token = operator_token

    def read(self, command):
        if command in SKILLS:
            path = SKILLS[command]
        else:
            x, y = map(int, command.split()[1:])
            path = f"/api/cognition/plan?x={x}&y={y}"
        # Keep origin hard-coded. Backend redirects are not followed.
        request = Request("http://127.0.0.1:8766" + path,
                          headers={"X-Blackwall-Token": self.operator_token})
        from urllib.request import HTTPRedirectHandler, build_opener
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise RuntimeError("Backend redirect forbidden")
        opener = build_opener(NoRedirect())
        with opener.open(request, timeout=4) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Backend response too large")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, (dict, list)):
            raise ValueError("Unexpected backend result")
        return {"source": "local-blackwall-v1.5", "path": path,
                "observedAt": timestamp(), "payloadSHA256": digest(canonical(data)),
                "payload": data, "independentScientificValidation": False}


def parse_input(envelope):
    if not isinstance(envelope, dict) or set(envelope) - {"message", "configuration", "metadata"}:
        raise ValidationError("Unsupported A2A envelope")
    message = envelope.get("message")
    if not isinstance(message, dict) or message.get("role") != "ROLE_USER":
        raise ValidationError("Only ROLE_USER accepted")
    if "taskId" in message:
        raise ValidationError("Multi-turn messages are not supported", 501)
    mid = message.get("messageId")
    if not isinstance(mid, str) or not ID_RE.fullmatch(mid):
        raise ValidationError("Invalid messageId")
    cid = message.get("contextId")
    if cid is not None and (not isinstance(cid, str) or not ID_RE.fullmatch(cid)):
        raise ValidationError("Invalid contextId")
    parts = message.get("parts")
    if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
        raise ValidationError("Exactly one text part required")
    if set(parts[0]) != {"text"} or not isinstance(parts[0]["text"], str):
        raise ValidationError("Text-only messages supported")
    command = parts[0]["text"]
    if not CMD_RE.fullmatch(command):
        raise ValidationError("Unknown read-only capability")
    configuration = envelope.get("configuration", {})
    if configuration:
        if not isinstance(configuration, dict):
            raise ValidationError("Invalid configuration")
        modes = configuration.get("acceptedOutputModes")
        if modes is not None and (not isinstance(modes, list) or
                                 "application/json" not in modes):
            raise ValidationError("Only application/json output supported")
    return mid, cid or str(uuid.uuid4()), command


def agent_card(port):
    return {
        "name": AGENT_NAME,
        "description": "Operator-authorized local read-only evidence and simulation previews; "
                       "no external execution or physical actuation",
        "version": "0.1.0",
        "supportedInterfaces": [{"url": f"http://127.0.0.1:{port}",
                                 "protocolBinding": "HTTP+JSON", "protocolVersion": PROTOCOL_VERSION}],
        "capabilities": {"streaming": False, "pushNotifications": False,
                         "extendedAgentCard": False},
        "securitySchemes": {"bearerAuth": {"httpAuthSecurityScheme": {
            "scheme": "bearer", "bearerFormat": "opaque"}}},
        "securityRequirements": [{"bearerAuth": []}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [{"id": "local_readonly", "name": "Read-only local observations",
                    "description": "Commands: status, audit, cognition, plan X Y (X,Y 0..4)",
                    "tags": ["research", "audit", "simulation", "readonly"],
                    "examples": ["status", "audit", "plan 4 2"],
                    "inputModes": ["text/plain"],
                    "outputModes": ["application/json"]}],
    }


def make_server(store, backend, token, port=0):
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("A dedicated BLACKWALL_A2A_TOKEN >=32 chars is required")

    class Handler(BaseHTTPRequestHandler):
        server_version = "BlackwallA2ABridge/0.1"
        sys_version = ""

        def log_message(self, fmt, *args):
            # Avoid logging token, task content, query parameters or full URLs.
            return

        def send(self, status, data, a2a=True):
            output = canonical(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/a2a+json" if a2a else "application/json")
            self.send_header("Content-Length", str(len(output)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(output)

        def authorized(self):
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth, "Bearer " + token):
                self.send(401, {"error": {"code": 401, "message": "Unauthorized"}})
                return False
            version = self.headers.get("A2A-Version")
            if version is not None and version != PROTOCOL_VERSION:
                self.send(400, {"error": {"code": 400,
                    "message": "A2A protocol version not supported",
                    "supportedVersions": [PROTOCOL_VERSION]}})
                return False
            return True

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/.well-known/agent-card.json":
                return self.send(200, agent_card(self.server.server_address[1]))
            if path == "/health":
                return self.send(200, {"status": "ok", "scope": "loopback-only", 
                                       "a2aSubset": True}, a2a=False)
            if not self.authorized():
                return
            if path.startswith("/tasks/"):
                ident = path.split("/", 2)[2]
                if not UUID_RE.fullmatch(ident):
                    return self.send(400, {"error": {"code": 400, "message": "Invalid task ID"}})
                task = store.get(ident)
                return self.send(200, task) if task else self.send(404, {"error": {
                    "code": 404, "message": "Task not found"}})
            if path == "/tasks":
                from urllib.parse import parse_qs
                params = parse_qs(query, keep_blank_values=True)
                if set(params) - {"pageSize", "pageToken"}:
                    return self.send(400, {"error": {"code": 400,
                                                      "message": "Unsupported task filter"}})
                try:
                    size = int(params.get("pageSize", [20])[0])
                    if size not in range(1, 51):
                        raise ValueError()
                except ValueError:
                    return self.send(400, {"error": {"code": 400, "message": "Invalid pageSize"}})
                from base64 import urlsafe_b64decode, urlsafe_b64encode
                page_token = params.get("pageToken", [""])[0]
                offset = 0
                if page_token:
                    try:
                        raw = urlsafe_b64decode(page_token + "=" * (-len(page_token) % 4))
                        number, signature = raw.decode("ascii").split(":", 1)
                        if not number.isdecimal() or len(number) > 8:
                            raise ValueError("Invalid offset")
                        expected = hmac.new(token.encode(), number.encode(), hashlib.sha256).hexdigest()
                        if not hmac.compare_digest(signature, expected):
                            raise ValueError("Invalid pagination signature")
                        offset = int(number)
                    except (ValueError, UnicodeError):
                        return self.send(400, {"error": {"code": 400,
                                                          "message": "Invalid pageToken"}})
                total = store.total_count()
                tasks = store.listing(size, offset)
                next_token = ""
                if offset + len(tasks) < total:
                    number = str(offset + len(tasks))
                    signature = hmac.new(token.encode(), number.encode(), hashlib.sha256).hexdigest()
                    next_token = urlsafe_b64encode((number + ":" + signature).encode()).decode().rstrip("=")
                return self.send(200, {"tasks": tasks, "totalSize": total,
                                       "pageSize": size, "nextPageToken": next_token})
            return self.send(404, {"error": {"code": 404, "message": "Not found"}})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/message:send":
                return self.send(501, {"error": {"code": 501,
                                                  "message": "Operation not implemented"}})
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/a2a+json":
                return self.send(415, {"error": {"code": 415,
                                                  "message": "Use application/a2a+json"}})
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
            if size <= 0 or size > MAX_BODY_BYTES:
                return self.send(413, {"error": {"code": 413,
                                                  "message": "Invalid request size"}})
            try:
                envelope = json.loads(self.rfile.read(size))
                mid, cid, command = parse_input(envelope)
                task_id, fresh = store.begin(mid, digest(canonical(envelope)), cid, command)
            except ValidationError as e:
                return self.send(e.status, {"error": {"code": e.status, "message": str(e)}})
            except (ValueError, UnicodeError, json.JSONDecodeError):
                return self.send(400, {"error": {"code": 400, "message": "Malformed request"}})
            if fresh:
                try:
                    payload = backend.read(command)
                    store.finish(task_id, payload, True)
                except Exception as exc:
                    # Deliberately redact backend URL, token and any exception content.
                    store.finish(task_id, {"error": "Backend read failed",
                                                "errorClass": type(exc).__name__}, False)
            self.send(200, {"task": store.get(task_id)})

    return ThreadingHTTPServer((BIND_HOST, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--db", type=Path, default=Path.home() /
                        "blackwall" / "a2a-bridge" / "state" / "bridge.sqlite3")
    args = parser.parse_args(argv)
    if not args.serve:
        print("Use --serve to start a local-only A2A HTTP+JSON subset server")
        return 0
    inbound_token = os.environ.get("BLACKWALL_A2A_TOKEN", "")
    upstream_token = os.environ.get("BLACKWALL_OPERATOR_TOKEN", "")
    store = TaskStore(args.db)
    server = make_server(store, LocalBackend(upstream_token), inbound_token, args.port)
    print(f"Blackwall read-only A2A subset on http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())