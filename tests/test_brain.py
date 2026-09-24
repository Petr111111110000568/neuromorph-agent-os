"""Brain coordinator state, provenance and trusted-worker integration gates."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from workbench.brain import Brain, PLUGINS, digest
from workbench.network.worker import _execute
from workbench.service import Service

ROOT = Path(__file__).resolve().parents[1]


class BrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="brain-test-")
        self.state = Path(self.tmp.name)
        self.service = Service(ROOT, self.state / "workbench.sqlite3")
        self.brain = self.service.brain
        self.queue = self.service.network.queue
        self.queue.register_worker("brain-test-worker", ["simulation"])

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def start(self, **values):
        return self.brain.start(dict(question="KAN cortical morphogenesis comparison", **values))

    def envelope(self, job, computation=None):
        parameters = copy.deepcopy(job["payload"]["parameters"])
        computation = computation or {"summary": "Controlled test envelope, not a measured experiment",
            "metrics": [{"label": "signed difference", "value": -0.25}], "model": {"synthetic": True},
            "validation": {"biological_validation": False}, "series": [], "table": []}
        return {"id": "test-run-" + job["id"], "status": "completed", "plugin_id": job["payload"]["plugin_id"],
                "parameters": parameters, "result": computation, "provenance": {"builtin_hashes": self.service._pins(),
                    "input_sha256": digest(parameters), "output_sha256": digest(computation)}}

    def finish_all(self, change=None, real=False):
        while True:
            job = self.queue.claim("brain-test-worker")
            if not job:
                break
            outcome = _execute(job, ROOT, self.state / "real-worker") if real else {"result": self.envelope(job)}
            if change:
                outcome = change(job, outcome)
            self.queue.finish(job["id"], "brain-test-worker", job["lease_token"], **outcome)

    def test_selected_jobs_typed_events_and_no_internal_question_in_queue(self):
        session = self.brain.start({
            "question": "internal private note cortical KAN", "data_class": "internal", "seed": 11})
        self.assertEqual(session["status"], "awaiting_workers")
        self.assertEqual(len(session["jobs"]), 3)
        self.assertEqual(session["budget"]["max_worker_claims"], 6)
        for job in self.queue.jobs():
            self.assertEqual(set(job["payload"]), {"plugin_id", "parameters"})
            self.assertNotIn("private note", json.dumps(job))
            self.assertEqual(job["payload"]["parameters"]["seed"], 11)
        self.assertEqual([event["region"] for event in session["events"]],
            ["thalamus", "association_cortex", "hippocampus", "prefrontal", "basal_ganglia"])
        for event in session["events"]:
            self.assertEqual(event["provenance"]["message_sha256"], digest({"input": event["input"], "output": event["output"]}))

    def test_inputs_are_rejected_before_session_or_queue_write(self):
        invalid = [{"plugins": []}, {"plugins": ["bad"]}, {"plugins": ["kan_benchmark"] * 2},
            {"plugins": [None]}, {"plugins": "kan_benchmark"}, {"seed": True}, {"seed": -1},
            {"seed": 2147483648}, {"data_class": []}, {"data_class": "sensitive_genomic"}, {"online": True}]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.start(**values)
        self.assertEqual(self.brain.sessions()["items"], [])
        self.assertEqual(self.queue.jobs(), [])

    def test_repeated_ticks_recover_same_jobs_and_events(self):
        session = self.start(plugins=["kan_benchmark"])
        for _ in range(3):
            refreshed = self.brain.tick({"id": session["id"]})
            self.assertEqual(refreshed["jobs"], session["jobs"])
            self.assertEqual(refreshed["events"], session["events"])
        self.assertEqual(len(self.queue.jobs()), 1)
        self.finish_all()
        done = self.brain.tick({"id": session["id"]})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.brain.tick({"id": session["id"]}), done)
        self.assertEqual(done["branches"][0]["result"]["metrics"][0]["value"], -0.25)
        self.assertEqual(done["workspace"]["scientific_conclusion"], "not_established")

    def test_actual_builtins_complete_through_trusted_worker_handler(self):
        session = self.start()
        self.finish_all(real=True)
        done = self.brain.tick({"id": session["id"]})
        self.assertEqual(done["status"], "completed", done["issues"])
        self.assertEqual({b["plugin_id"] for b in done["branches"]}, set(PLUGINS))
        self.assertTrue(all(all(branch["verification"]["checks"].values()) for branch in done["branches"]))
        self.assertEqual(len(done["critique"]), 3)
        self.assertTrue(all(c["comparisons"] for c in done["critique"]))
        self.assertFalse(any(b["verification"]["worker_attestation"] for b in done["branches"]))

    def test_forged_hash_or_full_default_parameter_mismatch_is_not_accepted(self):
        session = self.start(plugins=["kan_benchmark", "cortical_sequence"])
        def forge(job, outcome):
            run = outcome["result"]
            if job["payload"]["plugin_id"] == "kan_benchmark":
                run["provenance"]["output_sha256"] = "0" * 64
            else:
                run["parameters"]["unexpected_default"] = 9
                run["provenance"]["input_sha256"] = digest(run["parameters"])
            return outcome
        self.finish_all(forge)
        done = self.brain.tick({"id": session["id"]})
        self.assertEqual(done["status"], "failed")
        self.assertTrue(all(branch["result"] is None for branch in done["branches"]))
        self.assertEqual(len(done["issues"]), 2)

    def test_pin_manifest_is_compared_to_planned_snapshot(self):
        session = self.start(plugins=["kan_benchmark"])
        def forge(job, outcome):
            outcome["result"]["provenance"]["builtin_hashes"] = {"invented.py": "f" * 64}
            return outcome
        self.finish_all(forge)
        done = self.brain.tick({"id": session["id"]})
        self.assertEqual(done["status"], "failed")
        self.assertFalse(done["branches"][0]["verification"]["checks"]["planned_pins_match"])

    def test_partial_results_preserve_success_and_huge_metric_is_rejected(self):
        session = self.start(plugins=["kan_benchmark", "cortical_sequence"])
        def forge(job, outcome):
            if job["payload"]["plugin_id"] == "kan_benchmark":
                run = outcome["result"]
                run["result"]["metrics"][0]["value"] = 1 << 2000
                run["provenance"]["output_sha256"] = digest(run["result"])
            return outcome
        self.finish_all(forge)
        done = self.brain.tick({"id": session["id"]})
        self.assertEqual(done["status"], "partial")
        self.assertEqual([b["status"] for b in done["branches"]], ["rejected", "accepted"])
        self.assertFalse(done["branches"][0]["verification"]["checks"]["result_structure"])

    def test_critic_retains_mixed_sign_seed_results(self):
        session = self.start(plugins=["structural_plasticity"])
        def comparisons(job, outcome):
            run = outcome["result"]
            run["result"]["paired_comparisons"] = [
                {"seed": 42, "guided_minus_fixed_new_mse": -0.2},
                {"seed": 43, "guided_minus_fixed_new_mse": 0.3}]
            run["provenance"]["output_sha256"] = digest(run["result"])
            return outcome
        self.finish_all(comparisons)
        done = self.brain.tick({"id": session["id"]})
        critique = done["critique"][0]
        self.assertTrue(critique["direction_disagrees_between_seeds"])
        self.assertEqual(critique["seeds_better_than_named_control"], 1)
        self.assertEqual(critique["seeds_worse_than_named_control"], 1)
        self.assertEqual(len(done["branches"][0]["result"]["paired_comparisons"]), 2)

    def test_finished_sessions_survive_restart_and_feed_episodic_memory(self):
        session = self.start(plugins=["kan_benchmark"])
        self.finish_all()
        self.brain.tick({"id": session["id"]})
        self.service.close()
        self.service = Service(ROOT, self.state / "workbench.sqlite3")
        self.brain, self.queue = self.service.brain, self.service.network.queue
        second = self.start(plugins=["kan_benchmark"])
        self.assertEqual(second["memory"][0]["id"], session["id"])
        self.assertEqual(second["memory"][0]["kind"], "brain_episode")
        self.assertFalse(second["memory"][0]["independent_replication"])
        self.assertEqual(self.service.store.list("run"), [])

    def test_public_memory_excludes_prior_internal_episode(self):
        first = self.start(plugins=["kan_benchmark"], data_class="internal")
        self.finish_all()
        self.brain.tick({"id": first["id"]})
        public = self.start(plugins=["kan_benchmark"], data_class="public")
        self.assertEqual(public["memory"], [])
        internal = self.start(plugins=["kan_benchmark"], data_class="internal")
        self.assertEqual(internal["memory"][0]["id"], first["id"])

    def test_submission_crash_restart_recovers_without_duplicate(self):
        submit = self.queue.submit
        def crash_after_commit(*args, **kwargs):
            submit(*args, **kwargs)
            raise RuntimeError("coordinator crashed after queue commit")
        with mock.patch.object(self.queue, "submit", side_effect=crash_after_commit):
            with self.assertRaises(RuntimeError):
                self.start(plugins=["kan_benchmark", "cortical_sequence"])
        pending = self.brain.sessions()["items"][0]
        self.assertEqual(self.brain.get(pending["id"])["jobs"], {})
        self.assertEqual(len(self.queue.jobs()), 1)
        self.service.close()
        self.service = Service(ROOT, self.state / "workbench.sqlite3")
        self.brain, self.queue = self.service.brain, self.service.network.queue
        resumed = self.brain.tick({"id": pending["id"]})
        self.assertEqual(len(resumed["jobs"]), 2)
        self.assertEqual(len(self.queue.jobs()), 2)
        self.assertEqual(len({j["id"] for j in self.queue.jobs()}), 2)

    def test_cancel_recovers_commit_before_state_save_and_fences_result(self):
        submit = self.queue.submit
        def crash_after_commit(*args, **kwargs):
            submit(*args, **kwargs)
            raise RuntimeError("simulated crash")
        with mock.patch.object(self.queue, "submit", side_effect=crash_after_commit):
            with self.assertRaises(RuntimeError):
                self.start(plugins=["kan_benchmark"])
        ident = self.brain.sessions()["items"][0]["id"]
        job = self.queue.claim("brain-test-worker")
        cancelled = self.brain.cancel({"id": ident})
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(list(cancelled["jobs"].values()), [job["id"]])
        with self.assertRaises(ValueError):
            self.queue.finish(job["id"], "brain-test-worker", job["lease_token"], result=self.envelope(job))
        self.assertEqual(self.brain.tick({"id": ident})["status"], "cancelled")
        self.assertEqual(len(self.queue.jobs()), 1)

    def test_two_coordinators_share_idempotent_state(self):
        session = self.start(plugins=["kan_benchmark"])
        other = Brain(self.service)
        try:
            refreshed = other.tick({"id": session["id"]})
            self.assertEqual(refreshed["jobs"], session["jobs"])
            self.assertEqual(len(self.queue.jobs()), 1)
        finally:
            other.close()

    def test_cancel_intent_survives_crash_after_first_queue_cancel(self):
        session = self.start(plugins=["kan_benchmark", "cortical_sequence"])
        cancel = self.queue.cancel
        def crash_after_commit(ident):
            cancel(ident)
            raise RuntimeError("cancel worker commit then crash")
        with mock.patch.object(self.queue, "cancel", side_effect=crash_after_commit):
            with self.assertRaises(RuntimeError):
                self.brain.cancel({"id": session["id"]})
        self.assertEqual(self.brain.get(session["id"])["status"], "cancelling")
        resumed = self.brain.tick_all()["items"][0]
        self.assertEqual(resumed["status"], "cancelled")
        self.assertTrue(all(job["status"] == "cancelled" for job in self.queue.jobs()))
        self.assertEqual(len(self.queue.jobs()), 2)
        self.assertEqual(len([e for e in resumed["events"] if e["type"] == "cancellation_requested"]), 1)
        self.assertEqual(len([e for e in resumed["events"] if e["type"] == "actions_cancelled"]), 1)

    def test_export_has_no_lease_secrets_and_documents_internal_content(self):
        self.start(plugins=["kan_benchmark"])
        leased = self.queue.claim("brain-test-worker")
        exported = self.brain.export()
        encoded = json.dumps(exported)
        self.assertNotIn(leased["lease_token"], encoded)
        self.assertNotIn('"lease_digest"', encoded)
        self.assertIn("internal", exported["sharing_note"])
        self.assertEqual(self.brain.status()["counts"]["llm_agents_configured"], 0)


if __name__ == "__main__":
    unittest.main()
