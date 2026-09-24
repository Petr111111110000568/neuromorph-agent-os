"""Campaign integration tests: persistence, budgets and evidence lineage."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from workbench.network.control import NetworkControl
from workbench.network.worker import _execute

ROOT = Path(__file__).resolve().parents[1]


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control = NetworkControl(ROOT, self.tmp.name)

    def tearDown(self):
        self.control.close()
        self.tmp.cleanup()

    def complete_jobs(self):
        queue = self.control.queue
        queue.register_worker("test-worker", ["discovery", "simulation"])
        while True:
            job = queue.claim("test-worker")
            if job is None:
                break
            result = _execute(job, ROOT, Path(self.tmp.name) / "worker")
            queue.finish(job["id"], "test-worker", job["lease_token"], **result)

    def test_real_bounded_cycle_survives_restart_and_budget(self):
        c = self.control.create_campaign({"question": "Эпигенетика: проверка вычислительных моделей", "max_iterations": 2, "max_jobs": 4})
        self.assertEqual(len(c["jobs"]), 2)
        self.complete_jobs()
        self.control.close()
        self.control = NetworkControl(ROOT, self.tmp.name)
        c = self.control.tick({"id": c["id"]})
        self.assertEqual(c["iteration"], 1)
        self.assertEqual(len(c["jobs"]), 4)
        self.complete_jobs()
        c = self.control.tick({"id": c["id"]})
        self.assertEqual(c["status"], "completed")
        self.assertEqual(c["iteration"], 2)
        self.assertEqual(self.control.tick({"id": c["id"]})["jobs"], c["jobs"])
        snapshot = json.dumps(self.control.export())
        self.assertNotIn('"lease_token"', snapshot)
        self.assertNotIn('"credential_value"', snapshot)
        self.assertEqual(len(self.control.queue.jobs()), 4)

    def test_insufficient_budget_stops_before_partial_iteration(self):
        c = self.control.create_campaign({"question": "genomics", "max_jobs": 1, "simulate": True})
        self.assertEqual(c["status"], "budget_exhausted")
        self.assertEqual(c["jobs"], [])

    def test_cancel_running_campaign_fences_late_results(self):
        c = self.control.create_campaign({"question": "genomics", "simulate": False})
        self.control.queue.register_worker("late", ["discovery"])
        job = self.control.queue.claim("late")
        self.assertEqual(self.control.cancel_campaign({"id": c["id"]})["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.control.queue.finish(job["id"], "late", job["lease_token"], result={"items": []})
        self.assertEqual(self.control.tick({"id": c["id"]})["status"], "cancelled")

    def test_discovery_outage_is_not_reported_as_research_success(self):
        c = self.control.create_campaign({"question": "genomics", "simulate": False, "online": True})
        q = self.control.queue
        q.register_worker("search", ["discovery"])
        job = q.claim("search")
        q.finish(job["id"], "search", job["lease_token"], result={"query": "genomics", "mode": "public_metadata", "items": [], "errors": [{"provider": "crossref", "code": "unavailable"}], "requests": 1})
        c = self.control.tick({"id": c["id"]})
        self.assertEqual(c["status"], "failed")

    def test_new_metadata_is_ingested_once_without_execution_privileges(self):
        job = self.control.discover({"query": "genomics", "offline": True})
        q = self.control.queue
        q.register_worker("search", ["discovery"])
        leased = q.claim("search")
        q.finish(job["id"], "search", leased["lease_token"], result={"items": [{"id": "doi:10.test/1", "title": "Novel source", "url": "https://example.org/paper", "adapter": {"available": True}, "provenance": {"doi": "10.test/1", "provider": "crossref"}}]})
        self.control.refresh_discoveries()
        self.control.refresh_discoveries()
        items = self.control.resources("Novel source")["items"]
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["adapter"]["available"])
        self.assertEqual(items[0]["provenance"]["doi"], "10.test/1")

    def test_input_budgets_and_unknown_fields_rejected_before_submission(self):
        for body in ({"question": "q", "max_jobs": True}, {"question": "q", "max_iterations": 99}, {"question": "q", "online": "yes"}, {"question": "q", "command": "curl"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.control.create_campaign(body)
        self.assertEqual(self.control.queue.jobs(), [])

    def test_cancel_recovers_job_after_partial_submission_rollback(self):
        submit = self.control.queue.submit
        def interrupt_second(kind, *args, **kwargs):
            if kind == "simulation":
                raise RuntimeError("Injected interruption between two DB commits")
            return submit(kind, *args, **kwargs)
        with patch.object(self.control.queue, "submit", side_effect=interrupt_second):
            with self.assertRaises(RuntimeError):
                self.control.create_campaign({"question": "genomics"})
        c = self.control.campaigns()["items"][0]
        self.assertEqual(c["jobs"], [])
        self.assertEqual(len(self.control.queue.jobs()), 1)
        self.control.close()
        self.control = NetworkControl(ROOT, self.tmp.name)
        cancelled = self.control.cancel_campaign({"id": c["id"]})
        self.assertEqual(len(cancelled["jobs"]), 1)
        self.assertEqual(self.control.queue.jobs()[0]["status"], "cancelled")

    def test_empty_worker_result_cannot_complete_campaign(self):
        c = self.control.create_campaign({"question": "genomics", "simulate": False, "max_iterations": 1})
        self.control.queue.register_worker("malformed", ["discovery"])
        j = self.control.queue.claim("malformed")
        self.control.queue.finish(j["id"], "malformed", j["lease_token"], result={})
        self.assertEqual(self.control.tick({"id": c["id"]})["status"], "failed")


if __name__ == "__main__":
    unittest.main()
