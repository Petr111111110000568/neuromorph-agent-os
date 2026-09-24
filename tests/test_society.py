"""Integration gates for research missions: actual jobs, state, and boundaries."""
import copy
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from workbench.mcp_server import serve_stdio
from workbench.network.worker import _execute
from workbench.server import make_server
from workbench.service import Service, ServiceError

ROOT = Path(__file__).resolve().parents[1]


class SocietyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="society-test-")
        self.state = Path(self.tmp.name)
        self.service = Service(ROOT, self.state / "workbench.sqlite3")
        self.society = self.service.society
        self.queue = self.service.network.queue

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def mission(self, **values):
        return self.society.create_mission(dict(question="epigenetics model validation", **values))

    def execute(self, mission, change=None):
        for index in range(2):
            self.queue.register_worker(f"test-worker-{index}", ["discovery", "evidence", "simulation"])
        for index in range(3):
            owner = f"test-worker-{index % 2}"
            job = self.queue.claim(owner, lease_seconds=60)
            self.assertIsNotNone(job)
            result = _execute(job, ROOT, self.state / owner)
            if change:
                result = change(job, result)
            self.queue.finish(job["id"], owner, job["lease_token"], **result)
        return self.society.tick({"id": mission["id"]})

    def test_policy_routes_for_closed_and_nonpublic_data(self):
        cases = [("public", "clear_web", "allowed"), ("public", "authenticated", "requires_connection"),
                 ("public", "private", "requires_connection"), ("internal", "clear_web", "local_only"),
                 ("sensitive_genomic", "authenticated", "local_only"), ("public", "onion", "unsupported_transport")]
        for data_class, layer, expected in cases:
            with self.subTest(data_class=data_class, layer=layer):
                result = self.society.route({"question": "agent literature", "data_class": data_class, "network_layer": layer})
                self.assertEqual(result["decision"], expected)
                self.assertTrue(result["reasons"])
                self.assertTrue(all(a["integration_status"] == "documented_not_connected" for a in result["ranked_agents"]))
        for invalid in ({"data_class": "secret"}, {"network_layer": "ftp"}, {"execute": True}):
            with self.assertRaises(ValueError):
                self.society.route(dict(question="test", **invalid))

    def test_nonpublic_online_rejected_before_submission(self):
        for classification in ("internal", "sensitive_genomic"):
            with self.subTest(data_class=classification), self.assertRaises(ValueError):
                self.mission(online=True, data_class=classification)
        self.assertEqual(self.queue.jobs(), [])
        self.assertEqual(self.society.missions(), [])
        offline = self.mission(data_class="sensitive_genomic", online=False)
        self.assertTrue(all(j["payload"].get("offline", True) for j in offline["job_details"]))

    def test_bounded_inputs(self):
        for invalid in ({"max_sources": 0}, {"max_sources": 9}, {"max_sources": True},
                        {"online": "yes"}, {"extra": "field"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.mission(**invalid)
        self.assertEqual(self.queue.jobs(), [])

    def test_three_actual_offline_jobs_and_redacted_export(self):
        mission = self.mission(max_sources=3)
        self.assertEqual({j["kind"] for j in mission["job_details"]}, {"discovery", "evidence", "simulation"})
        with mock.patch("workbench.society.evidence._fetch", side_effect=AssertionError("offline network request")), \
             mock.patch("workbench.network.discovery._fetch_json", side_effect=AssertionError("offline network request")):
            done = self.execute(mission)
        self.assertEqual(done["status"], "completed", done.get("issues"))
        self.assertTrue(done["evidence_items"])
        self.assertTrue(all(not i["abstract_available"] for i in done["evidence_items"]))
        method = done["method_results"][0]
        self.assertEqual(method["status"], "completed")
        self.assertRegex(method["provenance"]["output_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len({j["worker_id"] for j in done["job_details"]}), 2)
        self.assertTrue(all(j["attempts"] == 1 for j in done["job_details"]))
        exported = json.dumps(self.society.export())
        for secret_field in ('"lease_token"', '"lease_digest"', '"completion_digest"', '"META_HUB_TOKEN"'):
            self.assertNotIn(secret_field, exported)
        count = len(self.queue.jobs())
        self.society.tick({"id": mission["id"]})
        self.assertEqual(len(self.queue.jobs()), count)

    def test_persistence_and_scoped_claim_assessment(self):
        done = self.execute(self.mission())
        source = done["evidence_items"][0]["id"]
        claim = self.society.add_claim({"mission_id": done["id"], "text": "Источник найден в локальном каталоге",
            "source_ids": [source], "scope": "Каталог, без проверки содержания статьи", "assessment": "supported_within_scope",
            "rationale": "Проверена запись каталога; научные выводы не оценивались"})
        self.assertFalse(claim["independently_validated"])
        self.assertEqual(claim["assessed_by"], "local_user_self_reported")
        self.service.close()
        self.service = Service(ROOT, self.state / "workbench.sqlite3")
        self.society = self.service.society
        self.queue = self.service.network.queue
        restored = self.society.missions()[0]
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["claims"][0]["id"], claim["id"])
        self.assertEqual(restored["method_results"][0]["provenance"], done["method_results"][0]["provenance"])

    def test_claims_reject_unknown_sources_and_unsupported_verdict(self):
        mission = self.mission()
        base = {"mission_id": mission["id"], "text": "Hypothesis", "scope": "unvalidated hypothesis"}
        unreviewed = self.society.add_claim(base)
        self.assertEqual(unreviewed["assessment"], "unreviewed")
        for invalid in ({"source_ids": ["fabricated-source"]}, {"assessment": "supported_within_scope"},
                        {"assessment": "clinically_validated"}, {"source_ids": ["x", "x"]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.society.add_claim(dict(base, **invalid))

    def test_simulation_forged_output_hash_cannot_complete_mission(self):
        def forge(job, outcome):
            if job["kind"] == "simulation":
                outcome["result"]["provenance"]["output_sha256"] = "0" * 64
            return outcome
        done = self.execute(self.mission(), forge)
        self.assertEqual(done["status"], "partial")
        self.assertEqual(done["method_results"], [])
        self.assertTrue(done["issues"])

    def test_evidence_mode_query_and_abstract_provenance_rejected(self):
        mission = self.mission()
        job = next(j for j in mission["job_details"] if j["kind"] == "evidence")
        original = dict(job, result=_execute(job, ROOT, self.state / "evidence-probe")["result"])
        self.assertTrue(original["result"]["items"])
        variants = []
        for key, value in (("query", "different query"), ("mode", "public_abstract_triage"), ("requests", 1)):
            other = copy.deepcopy(original)
            other["result"][key] = value
            variants.append(other)
        fake_abstract = copy.deepcopy(original)
        fake_abstract["result"]["items"][0].update(abstract_available=True, abstract_sha256="a" * 64)
        variants.append(fake_abstract)
        for other in variants:
            self.assertFalse(self.society._valid_result(other))

    def test_evidence_wrong_provider_rejected(self):
        mission = self.mission()
        job = next(j for j in mission["job_details"] if j["kind"] == "evidence")
        result = _execute(job, ROOT, self.state / "evidence-provider")["result"]
        result["items"][0]["provenance"]["provider"] = "pretend_private_genomes"
        self.assertFalse(self.society._valid_result(dict(job, result=result)))

    def test_discovery_bound_to_submitted_query_and_mode(self):
        mission = self.mission()
        job = next(j for j in mission["job_details"] if j["kind"] == "discovery")
        result = _execute(job, ROOT, self.state / "discovery-probe")["result"]
        for key, wrong in (("query", "unrelated query"), ("mode", "public_metadata"), ("requests", 2)):
            with self.subTest(key=key):
                other = copy.deepcopy(result)
                other[key] = wrong
                self.assertFalse(self.society._valid_result(dict(job, result=other)))

    def test_directory_rejects_nonpublic_online_before_submission(self):
        for classification in ("internal", "sensitive_genomic"):
            with self.subTest(classification=classification), self.assertRaises(ValueError):
                self.society.directory({"query": "research", "provider": "agentverse", "offline": False,
                                        "data_class": classification})
        self.assertEqual(self.queue.jobs(), [])
        for invalid in ({"provider": "unknown_directory"}, {"limit": 9}, {"offline": "false"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.society.directory(dict(query="research", **invalid))

    def test_actual_offline_directory_worker_has_no_remote_access(self):
        queued = self.society.directory({"query": "agent", "provider": "agentverse", "offline": True,
                                         "data_class": "internal", "limit": 3})
        self.queue.register_worker("directory-worker", ["directory"])
        job = self.queue.claim("directory-worker")
        self.assertEqual(job["id"], queued["id"])
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("unexpected network")):
            outcome = _execute(job, ROOT, self.state / "directory-worker")
        self.queue.finish(job["id"], "directory-worker", job["lease_token"], **outcome)
        result = self.society.directory_jobs()[0]
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["result_validation"], "envelope_checked_not_external_identity_verified")
        self.assertEqual(result["result"]["mode"], "offline_registry")
        self.assertEqual(result["result"]["requests"], 0)
        self.assertLessEqual(len(result["result"]["items"]), 3)
        self.assertTrue(all(i["status"] == "directory_listed_not_connected" for i in result["result"]["items"]))
        self.assertEqual(self.society.status()["directory_jobs"][0]["id"], job["id"])

    def test_forged_directory_url_and_provider_are_not_exposed_as_results(self):
        from datetime import datetime, timezone
        self.queue.register_worker("directory-worker", ["directory"])
        for field, value in (("url", "https://attacker.invalid/copied-agent"), ("url", "javascript:alert(1)"),
                             ("provider", "pretend_private_directory")):
            with self.subTest(field=field, value=value):
                queued = self.society.directory({"query": "research", "provider": "agentverse", "offline": False})
                job = self.queue.claim("directory-worker")
                item = {"id": "external-test", "name": "Self-described agent", "url": "https://agentverse.ai/agents/test",
                        "status": "directory_listed_not_connected", "provenance": {"provider": "agentverse",
                        "retrieved_at": datetime.now(timezone.utc).isoformat()}}
                if field == "provider":
                    item["provenance"][field] = value
                else:
                    item[field] = value
                fake = {"query": "research", "provider": "agentverse", "mode": "public_directory",
                        "requests": 1, "items": [item], "errors": []}
                self.queue.finish(job["id"], "directory-worker", job["lease_token"], result=fake)
                checked = next(j for j in self.society.directory_jobs() if j["id"] == queued["id"])
                self.assertEqual(checked["status"], "invalid_result")
                self.assertIsNone(checked["result"])
                self.assertEqual(checked["error"]["code"], "invalid_directory_envelope")

    def interrupted_submission(self):
        real = self.queue.submit
        calls = 0
        def partial(*args, **kwargs):
            nonlocal calls
            calls += 1
            job = real(*args, **kwargs)
            if calls == 1:
                raise RuntimeError("simulated crash after durable queue commit")
            return job
        with mock.patch.object(self.queue, "submit", side_effect=partial), self.assertRaises(RuntimeError):
            self.mission()
        self.assertEqual(len(self.queue.jobs()), 1)
        saved = self.society.missions()[0]
        self.assertEqual(saved["jobs"], [])
        return saved, self.queue.jobs()[0]["id"]

    def test_partial_submission_recovered_idempotently(self):
        saved, committed_id = self.interrupted_submission()
        restored = self.society.tick({"id": saved["id"]})
        self.assertEqual(len(restored["jobs"]), 3)
        self.assertIn(committed_id, restored["jobs"])
        for _ in range(2):
            self.society.tick({"id": saved["id"]})
        self.assertEqual(len(self.queue.jobs()), 3)
        self.assertEqual(self.execute(restored)["status"], "completed")

    def test_cancel_recovers_partially_committed_job(self):
        saved, committed_id = self.interrupted_submission()
        cancelled = self.society.cancel({"id": saved["id"]})
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["jobs"], [committed_id])
        self.assertEqual(self.queue.get(committed_id)["status"], "cancelled")
        self.society.tick({"id": saved["id"]})
        self.assertEqual(len(self.queue.jobs()), 1)

    def test_cancel_fences_live_worker_result(self):
        mission = self.mission()
        self.queue.register_worker("late-worker", ["discovery"])
        job = self.queue.claim("late-worker")
        self.society.cancel({"id": mission["id"]})
        with self.assertRaises(ValueError):
            self.queue.finish(job["id"], "late-worker", job["lease_token"], result={"stale": True})
        self.assertTrue(all(j["status"] == "cancelled" for j in self.queue.jobs()))


class SocietyInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="society-api-")
        self.service = Service(ROOT, Path(self.tmp.name) / "workbench.sqlite3")
        self.server = make_server(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.service.close()
        self.tmp.cleanup()

    def request(self, path, body=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        client.request("GET" if body is None else "POST", path,
            body=None if body is None else json.dumps(body),
            headers={} if body is None else {"Content-Type": "application/json"})
        response = client.getresponse()
        data = response.read()
        status = response.status
        headers = dict(response.getheaders())
        client.close()
        return status, headers, data

    def test_http_registry_route_mission_cancel_and_export(self):
        status, _, raw = self.request("/api/society")
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(raw)["counts"]["agents_documented"], 0)
        status, _, raw = self.request("/api/society/directory", {"query": "research", "provider": "agentverse", "offline": True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["kind"], "directory")
        status, _, raw = self.request("/api/society/route", {"question": "genomics", "network_layer": "onion"})
        self.assertEqual(json.loads(raw)["decision"], "unsupported_transport")
        status, _, raw = self.request("/api/society/mission", {"question": "epigenetics model", "online": False})
        self.assertEqual(status, 200)
        mission = json.loads(raw)
        self.assertEqual(len(mission["jobs"]), 3)
        status, _, raw = self.request("/api/society/mission/cancel", {"id": mission["id"]})
        self.assertEqual(json.loads(raw)["status"], "cancelled")
        status, _, raw = self.request("/api/society/mission", {"question": "private", "online": True, "data_class": "sensitive_genomic"})
        self.assertEqual(status, 400)
        status, headers, raw = self.request("/api/society/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertNotIn('"lease_token"', raw.decode())

    def test_real_stdio_mcp_exposes_and_executes_society_tools(self):
        frames = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "society-tests", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "route_research", "arguments": {"question": "genomics", "data_class": "internal"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "start_research_mission", "arguments": {"question": "epigenetics model"}}}]
        result = subprocess.run([sys.executable, "-m", "workbench", "--data-dir", str(Path(self.tmp.name) / "mcp"), "mcp"],
            input="\n".join(json.dumps(f) for f in frames) + "\n", cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        tools = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertTrue({"society_status", "route_research", "start_research_mission", "record_research_claim", "search_agent_directory"} <= tools)
        self.assertEqual(json.loads(replies[2]["result"]["content"][0]["text"])["decision"], "local_only")
        mission = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertEqual(len(mission["jobs"]), 3)
        self.assertFalse(mission["online"])


if __name__ == "__main__":
    unittest.main()
