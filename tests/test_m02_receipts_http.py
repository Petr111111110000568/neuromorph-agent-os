"""M02 packaged receipts through real HTTP; no provider or model calls.

The two-owner auth double exercises storage scope independently of the current
single-owner cloud deployment. A separate real CloudAuth fixture checks its
allowlist and logout at the same HTTP endpoints without contacting Yandex.
"""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from workbench.cloud_auth import AuthError, CloudAuth, Config, TOKEN_URL
from workbench.server import make_server
from workbench.service import Service


ORIGIN = "https://research.example"
SEED = "m02-seed-36277624672"
RESUME = "m02-resume-36277674850"


class TwoOwnerFixtureAuth:
    config = SimpleNamespace(public_origin=ORIGIN)

    def session(self, cookie):
        if cookie not in ("fixture=owner-a", "fixture=owner-b"):
            raise AuthError("unauthorized")
        owner = cookie.split("=", 1)[1]
        return {"subject": owner, "csrf_token": "csrf-" + owner, "expires_at": 1234}

    def authorize_write(self, cookie, origin, token):
        session = self.session(cookie)
        if origin != ORIGIN or token != session["csrf_token"]:
            raise AuthError("csrf_rejected")
        return session


class M02ReceiptsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db_path = Path(self.temporary.name) / "studio.sqlite3"
        self.auth = TwoOwnerFixtureAuth()
        self.start()
        self.addCleanup(self.stop)

    def start(self):
        self.service = Service(db_path=self.db_path)
        self.server = make_server(self.service, port=0, cloud_auth=self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.service.close()

    def request(self, path="/api/studio/m02", data=None, *, owner="owner-a",
                cookie=None, csrf=None, origin=ORIGIN, host="research.example", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        request_headers = {"Host": host, "Cookie": cookie if cookie is not None else "fixture=" + owner}
        body = None
        if data is not None:
            body = json.dumps(data)
            request_headers.update({"Content-Type": "application/json", "Origin": origin,
                                    "X-CSRF-Token": "csrf-" + owner if csrf is None else csrf})
        request_headers.update(headers or {})
        try:
            connection.request("POST" if data is not None else "GET", path, body, request_headers)
            response = connection.getresponse()
            content = response.read()
            return response.status, json.loads(content), dict(response.getheaders())
        finally:
            connection.close()

    def snapshot(self, owner="owner-a"):
        status, value, _ = self.request(owner=owner)
        self.assertEqual(status, 200, value)
        return value

    def project(self, owner="owner-a", key="project-key"):
        status, value, _ = self.request("/api/studio/save",
            {"kind": "project", "title": "Finite M02 receipt review", "idempotency_key": key}, owner=owner)
        self.assertEqual(status, 200, value)
        return value["id"]

    def body(self, project_id, receipt_id=SEED, key="receipt-key"):
        return {"project_id": project_id, "receipt_id": receipt_id, "idempotency_key": key}

    def import_receipt(self, body, **options):
        return self.request("/api/studio/m02/import", body, **options)

    def assert_error(self, response, status, code):
        self.assertEqual(response[0], status, response[1])
        self.assertEqual(response[1]["error"]["code"], code)

    def test_private_catalog_and_import_require_auth_before_body_validation(self):
        with patch.object(self.service.m02_receipts, "snapshot") as snapshot, \
                patch.object(self.service.m02_receipts, "import_receipt") as write:
            self.assert_error(self.request(cookie=""), 401, "unauthorized")
            self.assert_error(self.import_receipt({"execute": "untrusted"}, cookie=""), 401, "unauthorized")
            snapshot.assert_not_called()
            write.assert_not_called()
        self.assertEqual(self.snapshot()["imports"], [])

    def test_import_requires_csrf_and_same_origin_without_mutation(self):
        body = self.body(self.project())
        self.assert_error(self.import_receipt(body, csrf=""), 403, "csrf_rejected")
        self.assert_error(self.import_receipt(body, csrf="csrf-owner-b"), 403, "csrf_rejected")
        self.assert_error(self.import_receipt(body, origin="https://attacker.example"), 403, "invalid_origin")
        self.assert_error(self.import_receipt(body, headers={"Sec-Fetch-Site": "cross-site"}), 403, "cross_site")
        self.assertEqual(self.snapshot()["imports"], [])

    def test_host_guard_and_no_store_apply_to_new_private_route(self):
        self.assert_error(self.request(host="attacker.example"), 403, "invalid_host")
        status, _, headers = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_catalog_is_two_pinned_historical_receipts_with_no_execution_capability(self):
        state = self.snapshot()
        self.assertEqual({entry["receipt_id"] for entry in state["catalog"]}, {SEED, RESUME})
        self.assertEqual(len(state["catalog"]), 2)
        self.assertEqual(state["imports"], [])
        for entry in state["catalog"]:
            self.assertRegex(entry["canonical_sha256"], r"^[a-f0-9]{64}$")
            self.assertTrue(entry["identity"])
            self.assertIn("github.com/Petr111111110000568/neuromorph-agent-os/actions/runs/", entry["source_url"])
            self.assertIsInstance(entry["receipt"], dict)
            self.assertEqual(entry["receipt_representation"], "bounded_projection")
            self.assertNotIn("restored_file_hashes", entry["receipt"])
            self.assertNotIn("transitions", entry["receipt"]["state"])
            self.assertNotIn("jobs", entry["receipt"]["state"])
            self.assertEqual(entry["summary"]["model_calls"], 0)
            self.assertEqual(entry["summary"]["additional_spend_usd"], 0)
            self.assertTrue(entry["limitations"])
        capabilities = state["capabilities"]
        self.assertTrue(capabilities["import_receipts"])
        self.assertTrue(capabilities["historical"])
        for key in ("model_calls", "network_calls", "execution", "live_verification", "scientific_validation"):
            self.assertIs(capabilities[key], False, key)

    def test_owner_b_cannot_import_into_owner_a_project_or_read_its_receipt(self):
        project_a = self.project()
        body = self.body(project_a)
        self.assert_error(self.import_receipt(body, owner="owner-b"), 404, "not_found")
        self.assertEqual(self.snapshot("owner-b")["imports"], [])
        status, imported, _ = self.import_receipt(body)
        self.assertIn(status, (200, 201), imported)
        self.assertEqual(imported["project_id"], project_a)
        self.assertEqual([record["id"] for record in self.snapshot()["imports"]], [imported["id"]])
        self.assertEqual(self.snapshot("owner-b")["imports"], [])
        project_b = self.project("owner-b")
        status, other, _ = self.import_receipt(self.body(project_b), owner="owner-b")
        self.assertIn(status, (200, 201), other)
        self.assertEqual(other["project_id"], project_b)
        self.assertEqual(len(self.snapshot()["imports"]), 1)
        self.assertEqual(len(self.snapshot("owner-b")["imports"]), 1)

    def test_studio_task_id_cannot_stand_in_for_a_project(self):
        project_id = self.project()
        status, task, _ = self.request("/api/studio/save", {"kind": "task", "title": "Review receipt",
            "project_id": project_id, "idempotency_key": "task-key"})
        self.assertEqual(status, 200)
        self.assert_error(self.import_receipt(self.body(task["id"])), 404, "not_found")
        self.assertEqual(self.snapshot()["imports"], [])

    def test_replay_survives_service_restart_and_changed_identity_conflicts(self):
        project_id = self.project()
        body = self.body(project_id)
        status, first, _ = self.import_receipt(body)
        self.assertIn(status, (200, 201), first)
        self.assertEqual(first["receipt_id"], SEED)
        self.assertRegex(first["canonical_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(self.import_receipt(body)[1], first)
        self.stop()
        self.start()
        self.assertEqual(self.import_receipt(body)[1], first)
        self.assertEqual(self.snapshot()["imports"], [first])
        self.assert_error(self.import_receipt(self.body(project_id, RESUME)), 409, "idempotency_conflict")
        other_project = self.project(key="second-project-key")
        self.assert_error(self.import_receipt(self.body(other_project)), 409, "idempotency_conflict")
        self.assertEqual(self.snapshot()["imports"], [first])

    def test_new_key_cannot_duplicate_an_existing_project_receipt_association(self):
        project_id = self.project()
        status, first, _ = self.import_receipt(self.body(project_id))
        self.assertEqual(status, 200, first)
        self.assert_error(self.import_receipt(self.body(project_id, key="another-request-key")),
                          409, "receipt_already_imported")
        self.assertEqual(self.snapshot()["imports"], [first])

    def test_two_concurrent_http_replays_return_one_durable_association(self):
        body = self.body(self.project())
        barrier = threading.Barrier(2)

        def write():
            barrier.wait(timeout=5)
            return self.import_receipt(body)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write) for _ in range(2)]
            replies = [future.result(timeout=10) for future in futures]
        for reply in replies:
            self.assertEqual(reply[0], 200, reply[1])
        self.assertEqual(replies[0][1], replies[1][1])
        self.assertEqual(self.snapshot()["imports"], [replies[0][1]])

    def test_missing_or_changed_packaged_receipt_is_503_before_any_import(self):
        body = self.body(self.project())
        library = self.service.m02_receipts
        files = ("config/m02_receipt_registry.json", "data/m02_receipts/seed.json",
                 "data/m02_receipts/resume.json")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for fault in ("missing-registry", "missing-receipt", "changed-receipt"):
                with self.subTest(fault=fault):
                    for relative in files:
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes((library.root / relative).read_bytes())
                    if fault == "missing-registry":
                        (root / files[0]).unlink()
                    elif fault == "missing-receipt":
                        (root / files[1]).unlink()
                    else:
                        path = root / files[1]
                        value = json.loads(path.read_text(encoding="utf-8"))
                        value["model_calls"] = 1
                        path.write_text(json.dumps(value), encoding="utf-8")
                    with patch.object(library, "root", root):
                        for response in (self.request(), self.import_receipt(body)):
                            self.assert_error(response, 503, "receipt_library_unavailable")
                            self.assertNotIn(str(root), json.dumps(response[1]))
                    self.assertEqual(self.snapshot()["imports"], [])

    def test_request_is_closed_and_cannot_supply_sources_hashes_owner_or_code(self):
        body = self.body(self.project())
        injected = {"url": "https://attacker.example/receipt.json", "root": "../",
                    "receipt": {"phase": "complete"}, "canonical_sha256": "0" * 64,
                    "owner": "owner-b", "handler": "untrusted-shell",
                    "sqlite_path": "../state.sqlite3", "execute": "print('not executable')"}
        for key, value in injected.items():
            with self.subTest(key=key):
                self.assert_error(self.import_receipt({**body, key: value}), 400, "invalid_request")
        for missing in body:
            with self.subTest(missing=missing):
                self.assert_error(self.import_receipt({k: v for k, v in body.items() if k != missing}),
                                  400, "invalid_request")
        self.assertEqual(self.snapshot()["imports"], [])
        self.assert_error(self.import_receipt(self.body(body["project_id"], "m02-not-packaged")),
                          404, "receipt_not_found")
        self.assertEqual(self.snapshot()["imports"], [])

    def test_import_and_catalog_do_not_dispatch_models_network_or_subprocesses(self):
        body = self.body(self.project())
        with ExitStack() as stack:
            calls = [stack.enter_context(patch(target, side_effect=AssertionError("receipt import is data only")))
                     for target in ("subprocess.Popen", "urllib.request.build_opener", "urllib.request.urlopen")]
            calls += [stack.enter_context(patch.object(self.service, method,
                       side_effect=AssertionError("receipt import cannot dispatch research")))
                      for method in ("run", "harnesses_run", "network_call", "brain_call", "council")]
            before = self.snapshot()
            status, imported, _ = self.import_receipt(body)
            self.assertIn(status, (200, 201), imported)
            after = self.snapshot()
            self.assertEqual(len(before["imports"]), 0)
            self.assertEqual(len(after["imports"]), 1)
            for call in calls:
                call.assert_not_called()

    def test_real_cloud_owner_allowlist_and_logout_protect_receipts(self):
        calls = []
        profile_id = ["2002"]

        def transport(method, url, headers, body):
            calls.append((method, url))
            if url == TOKEN_URL:
                return {"access_token": "fixture-token", "token_type": "bearer"}
            return {"id": profile_id[0], "client_id": "fixture-client"}

        self.stop()
        self.auth = CloudAuth(Config(ORIGIN, "fixture-client", "fixture-secret", "1001"), transport)
        self.start()

        def callback():
            flow = self.auth.begin()
            state = parse_qs(urlsplit(flow["authorize_url"]).query)["state"][0]
            return self.request("/auth/yandex/callback?" + urlencode({"code": "fixture-code", "state": state}),
                                cookie=flow["set_cookie"].split(";", 1)[0])

        self.assert_error(callback(), 403, "owner_required")
        self.assertEqual(len(calls), 2)
        self.assert_error(self.request(cookie=""), 401, "unauthorized")
        profile_id[0] = "1001"
        # Direct successful callback captures the HttpOnly session fixture, then
        # every read/write below passes through the real HTTP authentication.
        flow = self.auth.begin()
        state = parse_qs(urlsplit(flow["authorize_url"]).query)["state"][0]
        login = self.auth.callback({"code": "fixture-code", "state": state}, flow["set_cookie"].split(";", 1)[0])
        cookie = login["set_cookie"].split(";", 1)[0]
        self.assertEqual(self.request(cookie=cookie)[0], 200)
        status, project, _ = self.request("/api/studio/save", {"kind": "project", "title": "Owner scope",
            "idempotency_key": "cloud-project"}, cookie=cookie, csrf=login["csrf_token"])
        self.assertEqual(status, 200)
        body = self.body(project["id"])
        self.assert_error(self.import_receipt(body, cookie=cookie, csrf="invalid"), 403, "csrf_rejected")
        self.assertIn(self.import_receipt(body, cookie=cookie, csrf=login["csrf_token"])[0], (200, 201))
        self.assertEqual(self.request("/auth/logout", {}, cookie=cookie, csrf=login["csrf_token"])[0], 200)
        self.assert_error(self.request(cookie=cookie), 401, "unauthorized")
        self.assert_error(self.import_receipt(body, cookie=cookie, csrf=login["csrf_token"]), 401, "unauthorized")
        self.assertEqual(len(calls), 4, "receipt HTTP requests must not perform additional OAuth/provider requests")


if __name__ == "__main__":
    unittest.main()
