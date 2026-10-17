"""No external network/model/host mutations; two independent localhost bridge instances."""
import json
import io
from unittest.mock import patch
from pathlib import Path
from threading import Thread
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

from integrations.blackwall_a2a.bridge import (
    TaskStore, ValidationError, agent_card, digest, make_server, parse_input,
    LocalBackend, canonical,
)


class FixtureBackend:
    def __init__(self, label):
        self.label = label
        self.calls = []
    def read(self, command):
        self.calls.append(command)
        return {"source": self.label, "command": command, "confirmed": True}


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state" / "bridge.sqlite3"
        self.store = TaskStore(self.db)
        self.backend = FixtureBackend("peer-one")
        self.token = "test-token-operator-signed-1234567890123456"
        self.server = make_server(self.store, self.backend, self.token, 0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def request(self, method, path, data=None, token=None, content_type="application/a2a+json", **headers):
        h = {"A2A-Version": "1.0", "Content-Type": content_type}
        if token is not False:
            h["Authorization"] = "Bearer " + (token if token is not None else self.token)
        h.update(headers)
        body = canonical(data).encode() if data is not None else None
        req = Request(self.base+path, headers=h, data=body, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def envelope(self, command="status", mid=None):
        return {"message": {"messageId": mid or str(uuid.uuid4()),
                            "role": "ROLE_USER", "parts": [{"text": command}]},
                "configuration": {"acceptedOutputModes": ["application/json"]}}

    def test_agent_card_and_auth(self):
        code, card = self.request("GET", "/.well-known/agent-card.json", token=False)
        self.assertEqual(code, 200)
        self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], "HTTP+JSON")
        self.assertEqual(card["supportedInterfaces"][0]["protocolVersion"], "1.0")
        self.assertFalse(card["capabilities"]["streaming"])
        self.assertIn("bearerAuth", card["securitySchemes"])
        self.assertNotIn(self.token, json.dumps(card))
        self.assertEqual(self.request("GET", "/tasks", token=False)[0], 401)
        self.assertEqual(self.request("GET", "/tasks", token="nope")[0], 401)
        self.assertEqual(self.request("GET", "/tasks", **{"A2A-Version":"0.3"})[0], 400)
        self.assertEqual(self.request("GET", "/health", token=False)[0], 200)

    def test_roundtrip_and_durable_task(self):
        value = self.envelope("status")
        code, result = self.request("POST", "/message:send", value)
        self.assertEqual(code, 200)
        task = result["task"]
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(task["artifacts"][0]["parts"][0]["data"]["source"], "peer-one")
        self.assertEqual(self.backend.calls, ["status"])
        self.assertEqual(self.request("GET", "/tasks/"+task["id"])[1], task)
        self.assertEqual(len(self.request("GET", "/tasks")[1]["tasks"]), 1)
        self.assertTrue(self.store.audit_verified()["valid"])
        self.assertEqual(TaskStore(self.db).get(task["id"]), task)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    def test_exactly_once_and_conflicting_message_id(self):
        envelope = self.envelope("audit")
        first = self.request("POST", "/message:send", envelope)[1]["task"]
        second = self.request("POST", "/message:send", envelope)[1]["task"]
        self.assertEqual(first, second)
        self.assertEqual(self.backend.calls, ["audit"])
        changed = {"message": dict(envelope["message"], parts=[{"text":"status"}])}
        self.assertEqual(self.request("POST", "/message:send", changed)[0], 409)
        self.assertEqual(len(self.backend.calls), 1)

    def test_command_allowlist(self):
        for query in ["run ls", "status; rm -rf /", "plan 5 1", "POST http://example.com",
                      "plan 1 -1", "status\nignore policy", "audit   "]:
            self.assertEqual(self.request("POST", "/message:send", self.envelope(query))[0], 422)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.request("POST", "/message:stream", self.envelope())[0], 501)
        self.assertEqual(self.request("POST", "/message:send", self.envelope(),
                                      content_type="text/plain")[0], 415)
        self.assertEqual(self.request("POST", "/message:send", {
            "message":{"messageId":str(uuid.uuid4()),"role":"ROLE_USER",
                       "parts":[{"url":"https://example.com/x"}]}})[0], 422)

    def test_simulation_is_only_preview(self):
        code, result = self.request("POST", "/message:send", self.envelope("plan 4 3"))
        self.assertEqual(code, 200)
        self.assertEqual(self.backend.calls, ["plan 4 3"])
        self.assertNotIn("actuator", json.dumps(result))

    def test_invalid_size_missing_task_and_malformed(self):
        self.assertEqual(self.request("GET", "/tasks/"+str(uuid.uuid4()))[0], 404)
        self.assertEqual(self.request("GET", "/tasks?pageSize=500")[0], 400)
        self.assertEqual(self.request("GET", "/tasks?contextId=private")[0], 400)
        self.assertEqual(self.request("POST", "/message:send", {"long":"x" * 8000})[0], 413)
        self.assertEqual(self.request("POST", "/message:send", {"message":"bad"})[0], 422)
        with self.assertRaises(ValueError):
            make_server(self.store, self.backend, "weak", 0)

    def test_backend_failure_persists_without_claiming_success(self):
        class Broken:
            def read(self, command):
                raise RuntimeError("internal secret should be redacted")
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.server = make_server(self.store, Broken(), self.token, 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        code, payload = self.request("POST", "/message:send", self.envelope())
        self.assertEqual(code, 200)
        self.assertEqual(payload["task"]["status"]["state"], "TASK_STATE_FAILED")
        self.assertNotIn("secret", canonical(payload))

    def test_interruption_never_becomes_completed(self):
        tid, fresh = self.store.begin("message-id-restart-0001", digest("payload"),
                                      str(uuid.uuid4()), "status")
        self.assertTrue(fresh)
        after = TaskStore(self.db).get(tid)
        self.assertEqual(after["status"]["state"], "TASK_STATE_FAILED")
        self.assertTrue(self.store.audit_verified()["valid"])

    def test_tampered_audit_detected(self):
        self.request("POST", "/message:send", self.envelope())
        with self.store.connection() as conn:
            conn.execute("UPDATE audit SET event='tampered' WHERE seq=1")
        self.assertFalse(self.store.audit_verified()["valid"])

    def test_two_independent_local_peers(self):
        with tempfile.TemporaryDirectory() as root:
            state = TaskStore(Path(root)/"peer-two.sqlite3")
            other = FixtureBackend("peer-two")
            second_token = "other-peer-token-very-long-1234567890"
            server = make_server(state, other, second_token, 0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                req = Request(f"http://127.0.0.1:{server.server_address[1]}/message:send",
                    data=canonical(self.envelope("cognition")).encode(),
                    method="POST", headers={"Content-Type":"application/a2a+json",
                                            "Authorization":"Bearer "+second_token})
                with urlopen(req, timeout=3) as response:
                    result = json.load(response)
                self.assertEqual(result["task"]["status"]["state"], "TASK_STATE_COMPLETED")
                self.assertEqual(other.calls, ["cognition"])
                self.assertEqual(self.backend.calls, [])
                self.assertTrue(state.audit_verified()["valid"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)


class TestClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name)/"bridge.sqlite3")
        self.backend = FixtureBackend("client-test")
        self.token = "other-secret-long-abcdef012345678901234567890"
        self.server = make_server(self.store, self.backend, self.token, 0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def test_approved_client_roundtrip(self):
        from integrations.blackwall_a2a.client import run
        port = self.server.server_address[1]
        result = run(port, "cognition", self.token)
        self.assertTrue(result["verified"])
        self.assertEqual(self.backend.calls, ["cognition"])

    def test_client_rejects_unsupported_command(self):
        from integrations.blackwall_a2a.client import run
        with self.assertRaises(ValueError):
            run(self.server.server_address[1], "execute shell", self.token)
        with self.assertRaises(ValueError):
            run(self.server.server_address[1], "status", "short")

    def test_pagination_integrity(self):
        from integrations.blackwall_a2a.client import request
        port=self.server.server_address[1]
        base=f"http://127.0.0.1:{port}"
        for i in range(3):
            msg={"message":{"messageId":str(uuid.uuid4()),"role":"ROLE_USER",
                            "parts":[{"text":"status"}]}}
            request(base+"/message:send", self.token, msg)
        a=request(base+"/tasks?pageSize=2",self.token)
        self.assertEqual(a["totalSize"],3)
        self.assertEqual(len(a["tasks"]),2)
        self.assertTrue(a["nextPageToken"])
        b=request(base+"/tasks?pageSize=2&pageToken="+a["nextPageToken"],self.token)
        self.assertEqual(b["totalSize"],3)
        self.assertEqual(len(b["tasks"]),1)
        self.assertFalse(b["nextPageToken"])
        import urllib.error
        try:
            request(base+"/tasks?pageToken="+a["nextPageToken"]+'bogus',self.token)
            self.fail('Tampered cursor accepted')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code,400)


class TestFixedLocalBackend(unittest.TestCase):
    def test_fixed_local_origin_and_redacted_token(self):
        class FakeOpener:
            def __init__(self):
                self.last = None
            def open(self, req, timeout):
                self.last = req
                self.timeout = timeout
                return io.BytesIO(b'{"reachable":true,"steps":2}')
        opener = FakeOpener()
        token = "secret-internal-core-token-1234567"
        with patch("urllib.request.build_opener", return_value=opener):
            result = LocalBackend(token).read("plan 4 3")
        self.assertEqual(opener.last.full_url,
                         "http://127.0.0.1:8766/api/cognition/plan?x=4&y=3")
        self.assertEqual(opener.timeout, 4)
        self.assertEqual(result["source"], "local-blackwall-v1.5")
        self.assertEqual(result["payload"]["reachable"], True)
        self.assertNotIn(token, canonical(result))

    def test_requires_backend_token(self):
        with self.assertRaises(ValueError):
            LocalBackend("insecure")

if __name__ == "__main__":
    unittest.main()
