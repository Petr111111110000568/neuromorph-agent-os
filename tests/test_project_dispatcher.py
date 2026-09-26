"""M02 integration boundaries with real Queue/SQLite and fixed inert fixtures.

No model, network, generated code or shell execution. Process tests intentionally
crash AFTER durable commits; the only child entrypoints are this test module.
The business projection below is fixed, not selected after seeing a failure.
"""
import copy
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench.network.queue import Queue
from workbench.project_dispatcher import ProjectDispatcher, run_fixture_once


BASE_COMMIT = "2b731fbba92fc8427b7296f80579e9d6a8e02e9c"
POLICY = {"max_reserved_attempts": 8, "max_attempts_per_handoff": 2,
          "max_paid_calls": 0, "max_model_calls": 0}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def business_projection(dispatcher, queue):
    """Observable effects, not timestamps, lease maintenance or recovery caches.

    Includes task revision/current pointer, immutable intent contract/reservation,
    Queue identity/input/attempts/result, artifact bytes, publication event
    identity and review decisions. Excludes diagnostic counters, Queue status,
    leases, timestamps, cached control queue_job_id and outbox delivery flags.
    Tests assert lifecycle/delivery flags separately; this projection cannot make
    a no-op implementation pass the required first-effect assertions.
    """
    state = dispatcher.snapshot()
    return {
        "budget": (state["budget"]["max_reserved_attempts"], state["budget"]["reserved_attempts"],
                   state["budget"]["actual_attempts"]),
        "tasks": sorted((t["task_id"], t["project_id"], t["revision"], t["handoff_id"],
                         t["input_sha256"], t["current_result_sha256"]) for t in state["tasks"]),
        "intents": sorted((i["project_id"], i["task_id"], i["handoff_id"], i["revision"],
                           i["input_sha256"], i["queue_key"], i["reserved_attempts"]) for i in state["intents"]),
        "jobs": sorted((j["id"], j["kind"], canonical(j["payload"]), j["max_attempts"],
                        j["attempts"], canonical(j["result"])) for j in queue.jobs()),
        "artifacts": sorted((a["sha256"], canonical(a["content"])) for a in state["artifacts"]),
        "publications": sorted((e["event_id"], e["task_id"], e["handoff_id"], e["revision"],
                                e["result_sha256"]) for e in state["outbox"]),
        "reviews": sorted((r["task_id"], r["handoff_id"], r["revision"], r["result_sha256"],
                           r["decision"], r["note"]) for r in state["reviews"]),
    }


def _competing_tick(control_path, queue_path, identity, ready, start, output):
    queue = Queue(queue_path)
    dispatcher = None
    try:
        dispatcher = ProjectDispatcher(control_path, queue)
        ready.put(identity)
        if not start.wait(15):
            raise RuntimeError("process start deadline")
        output.put({"identity": identity, "result": dispatcher.tick()})
    except Exception as exc:
        output.put({"identity": identity, "error": type(exc).__name__ + ": " + str(exc)})
    finally:
        if dispatcher is not None:
            dispatcher.close()
        queue.close()


def _crash_at_commit(control_path, queue_path, stage, action):
    queue = Queue(queue_path)

    def crash(observed):
        if observed == stage:
            os._exit(71)

    dispatcher = ProjectDispatcher(control_path, queue, failpoint=crash)
    try:
        if action == "tick":
            dispatcher.tick()
        elif action == "worker":
            run_fixture_once(queue, worker_id="crash-fixture", failpoint=crash)
        else:
            os._exit(73)
    finally:
        dispatcher.close()
        queue.close()
    os._exit(72)  # required durable boundary was never reached


class ProjectDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.control_path = root / "control.sqlite3"
        self.queue_path = root / "queue.sqlite3"
        self.queue = Queue(self.queue_path)
        self.dispatcher = ProjectDispatcher(self.control_path, self.queue)
        self.addCleanup(self.close_current)

    def close_current(self):
        self.dispatcher.close()
        self.queue.close()

    def restart(self, *, failpoint=None):
        self.close_current()
        self.queue = Queue(self.queue_path)
        self.dispatcher = ProjectDispatcher(self.control_path, self.queue, failpoint=failpoint)

    @staticmethod
    def project_spec(project_id):
        return {"project_id": project_id, "mission": "Check reproducible scoped evidence",
                "allowed_artifacts": ["synthetic-evidence"],
                "closure_criteria": ["Reviewed finite result with provenance"],
                "resource_policy": dict(POLICY)}

    @staticmethod
    def task_spec(task_id, project_id, *, fixture_id="evidence_alpha", dependencies=None):
        return {"task_id": task_id, "project_id": project_id,
                "goal": "Which observation would falsify the same hypothesis?",
                "dependencies": list(dependencies or []), "revision": 1,
                "handoff_id": task_id + "-handoff-1", "base_commit": BASE_COMMIT,
                "handler": "m02.fixture.v1", "fixture_id": fixture_id,
                "limitations": ["Fixed synthetic artifact; no scientific validation"]}

    def seed(self, task_id="a", project_id="project-a", **options):
        self.dispatcher.create_project(self.project_spec(project_id))
        spec = self.task_spec(task_id, project_id, **options)
        self.dispatcher.create_task(spec)
        return spec

    @staticmethod
    def task(dispatcher, task_id):
        return next(t for t in dispatcher.snapshot()["tasks"] if t["task_id"] == task_id)

    def run_to_review(self, task_id, *, dispatcher=None, queue=None):
        dispatcher, queue = dispatcher or self.dispatcher, queue or self.queue
        for _ in range(12):
            task = self.task(dispatcher, task_id)
            if task["status"] == "awaiting_review":
                self.assertTrue(task["current_result_sha256"])
                return task
            dispatcher.tick()
            run_fixture_once(queue, worker_id="integration-fixture")
            dispatcher.tick()
        self.fail("finite fixture did not reach awaiting_review")

    def review(self, task_id, decision="accepted", *, note="Checked fixed evidence"):
        task = self.task(self.dispatcher, task_id)
        return self.dispatcher.review(task_id, expected_revision=task["revision"],
                                      result_sha256=task["current_result_sha256"], decision=decision, note=note)

    def two_process_ticks(self):
        context = multiprocessing.get_context("spawn")
        ready, output, start = context.Queue(), context.Queue(), context.Event()
        children = [context.Process(target=_competing_tick,
                    args=(str(self.control_path), str(self.queue_path), name, ready, start, output))
                    for name in ("first-process", "second-process")]
        try:
            for child in children:
                child.start()
            self.assertEqual({ready.get(timeout=20), ready.get(timeout=20)}, {"first-process", "second-process"})
            start.set()
            replies = [output.get(timeout=35), output.get(timeout=35)]
            for child in children:
                child.join(10)
                self.assertEqual(child.exitcode, 0)
            self.assertFalse(any("error" in reply for reply in replies), replies)
            return replies
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            ready.close()
            output.close()

    def crash(self, stage, action="tick"):
        context = multiprocessing.get_context("spawn")
        child = context.Process(target=_crash_at_commit,
                                args=(str(self.control_path), str(self.queue_path), stage, action))
        try:
            child.start()
            child.join(35)
            self.assertEqual(child.exitcode, 71, "child must crash at the requested committed boundary")
        finally:
            if child.is_alive():
                child.terminate()
                child.join(5)

    def test_same_question_in_two_projects_is_scoped_to_distinct_handoffs(self):
        self.seed("a", "project-a")
        self.seed("b", "project-b", fixture_id="evidence_beta")
        self.dispatcher.tick()
        self.dispatcher.tick()
        state = self.dispatcher.snapshot()
        self.assertEqual(len(self.queue.jobs()), 2)
        self.assertEqual(state["budget"]["reserved_attempts"], 4)
        self.assertEqual(len({row["queue_key"] for row in state["intents"]}), 2)
        self.assertEqual(len({row["queue_job_id"] for row in state["intents"]}), 2)
        a = self.run_to_review("a")
        with self.assertRaises((ValueError, KeyError)):
            self.dispatcher.review("b", expected_revision=1, result_sha256=a["current_result_sha256"],
                                   decision="accepted", note="Wrong project result")
        self.review("a")
        self.assertEqual(self.task(self.dispatcher, "a")["status"], "accepted")
        self.assertNotEqual(self.task(self.dispatcher, "b")["status"], "accepted")
        self.run_to_review("b")
        self.review("b")
        self.assertEqual(self.task(self.dispatcher, "b")["status"], "accepted")
        self.assertEqual(self.dispatcher.snapshot()["model_calls"], 0)

    def test_identical_task_is_idempotent_but_changed_same_handoff_is_conflict(self):
        spec = self.seed()
        self.dispatcher.tick()
        first = business_projection(self.dispatcher, self.queue)
        self.assertEqual(len(first["jobs"]), 1)  # first effect exists
        self.assertEqual(first["budget"][1], 2)
        self.dispatcher.create_task(copy.deepcopy(spec))
        self.assertEqual(business_projection(self.dispatcher, self.queue), first)
        for field, value in (("goal", "Mutated goal"), ("limitations", ["Changed contract"]),
                             ("fixture_id", "repair_alpha"), ("base_commit", "e" * 40)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.dispatcher.create_task({**spec, field: value})
            self.assertEqual(business_projection(self.dispatcher, self.queue), first)

    def test_two_processes_reserve_and_enqueue_same_handoff_once(self):
        self.seed()
        before = business_projection(self.dispatcher, self.queue)
        self.two_process_ticks()
        state = self.dispatcher.snapshot()
        self.assertEqual(len(state["intents"]), 1)
        self.assertEqual(len(self.queue.jobs()), 1)
        self.assertEqual(state["budget"]["reserved_attempts"], 2)
        self.assertNotEqual(business_projection(self.dispatcher, self.queue), before)
        self.assertEqual(self.queue.jobs()[0]["attempts"], 0)

    def test_two_processes_share_last_budget_reservation_across_projects(self):
        for task_id, project_id in (("a0", "project-a"), ("b0", "project-b"), ("a1", "project-a")):
            self.seed(task_id, project_id)
            self.run_to_review(task_id)
            self.review(task_id)
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 6)
        self.seed("a2", "project-a")
        self.seed("b1", "project-b")
        self.two_process_ticks()
        state = self.dispatcher.snapshot()
        self.assertEqual(state["budget"]["reserved_attempts"], 8)
        self.assertEqual(len(state["intents"]), 4)
        self.assertEqual(len(self.queue.jobs()), 4)
        self.assertEqual(sum(t["status"] == "dispatched" for t in state["tasks"]), 1)
        first = business_projection(self.dispatcher, self.queue)
        self.restart()
        self.dispatcher.tick()
        self.assertEqual(business_projection(self.dispatcher, self.queue), first)

    def test_process_crash_after_enqueue_recovers_same_job_without_reservation(self):
        self.seed()
        zero = business_projection(self.dispatcher, self.queue)
        self.crash("after_queue_submit")
        committed = business_projection(self.dispatcher, self.queue)
        self.assertNotEqual(committed, zero)
        self.assertEqual(len(committed["jobs"]), 1)
        self.assertEqual(committed["budget"][1], 2)
        job_id = self.queue.jobs()[0]["id"]
        self.restart()
        self.dispatcher.tick()
        self.assertEqual(business_projection(self.dispatcher, self.queue), committed)
        self.assertEqual(self.task(self.dispatcher, "a")["queue_job_id"], job_id)
        self.dispatcher.tick()
        self.assertEqual(business_projection(self.dispatcher, self.queue), committed)

    def test_process_crash_after_finish_promotes_saved_result_without_reexecution(self):
        self.seed()
        self.dispatcher.tick()
        before = business_projection(self.dispatcher, self.queue)
        self.crash("after_queue_finish", "worker")
        done = self.queue.jobs()[0]
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["attempts"], 1)
        self.assertIsNotNone(done["result"])
        self.assertNotEqual(business_projection(self.dispatcher, self.queue), before)
        self.restart()
        self.dispatcher.tick()
        promoted = self.dispatcher.snapshot()
        self.assertEqual(self.task(self.dispatcher, "a")["status"], "awaiting_review")
        self.assertEqual(len(promoted["outbox"]), 1)
        self.assertEqual(len(promoted["artifacts"]), 1)
        self.assertEqual(self.queue.jobs()[0]["attempts"], 1)
        first = business_projection(self.dispatcher, self.queue)
        self.dispatcher.tick()
        self.assertEqual(business_projection(self.dispatcher, self.queue), first)

    def test_stale_promotion_cannot_overwrite_already_promoted_revision_two(self):
        self.seed()
        self.dispatcher.tick()
        run_fixture_once(self.queue, worker_id="old-result")
        old_job = self.queue.jobs()[0]
        other_queue = Queue(self.queue_path)
        other = ProjectDispatcher(self.control_path, other_queue)
        self.addCleanup(other_queue.close)
        self.addCleanup(other.close)
        observed = {}

        def interleave(stage):
            if stage == "before_promotion" and not observed:
                observed["entered"] = True
                other.revise("a", expected_revision=1, handoff_id="a-handoff-r2", reason="New operator revision",
                             goal="A new hypothesis contract", fixture_id="repair_alpha")
                result = self.run_to_review("a", dispatcher=other, queue=other_queue)
                observed["new_hash"] = result["current_result_sha256"]

        self.restart(failpoint=interleave)
        self.dispatcher.tick()
        state = self.dispatcher.snapshot()
        self.assertTrue(observed.get("entered"))
        current = self.task(self.dispatcher, "a")
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["current_result_sha256"], observed["new_hash"])
        self.assertEqual(self.queue.get(old_job["id"])["result"], old_job["result"])
        self.assertEqual(state["budget"]["reserved_attempts"], 4)
        self.assertGreaterEqual(state["counts"]["stale_rejections"], 1)
        self.assertFalse(any(e["revision"] == 1 for e in state["outbox"]))
        self.assertEqual({e["result_sha256"] for e in state["outbox"]}, {observed["new_hash"]})

    def test_late_outbox_delivery_never_replaces_current_revision(self):
        self.seed()
        old = self.run_to_review("a")
        old_hash = old["current_result_sha256"]
        self.dispatcher.revise("a", expected_revision=1, handoff_id="new-handoff", reason="New evidence",
                               fixture_id="repair_alpha")
        new = self.run_to_review("a")
        new_hash = new["current_result_sha256"]
        self.assertNotEqual(old_hash, new_hash)
        self.assertEqual(len(self.dispatcher.snapshot()["outbox"]), 2)
        self.dispatcher.deliver_outbox()
        state = self.dispatcher.snapshot()
        states = {row["revision"]: row["status"] for row in state["outbox"]}
        self.assertEqual(states, {1: "superseded", 2: "delivered"})
        self.assertEqual(self.task(self.dispatcher, "a")["current_result_sha256"], new_hash)
        self.assertTrue({old_hash, new_hash}.issubset({a["sha256"] for a in state["artifacts"]}))
        first = business_projection(self.dispatcher, self.queue)
        self.dispatcher.deliver_outbox()
        self.assertEqual(business_projection(self.dispatcher, self.queue), first)
        self.assertEqual({row["revision"]: row["status"] for row in self.dispatcher.snapshot()["outbox"]}, states)

    def test_process_crash_after_promotion_recovers_one_publication_event(self):
        self.seed()
        self.dispatcher.tick()
        run_fixture_once(self.queue, worker_id="promotion-fixture")
        self.crash("after_control_promotion")
        state = self.dispatcher.snapshot()
        self.assertEqual(len(state["outbox"]), 1)
        self.assertEqual(state["outbox"][0]["status"], "pending")
        self.assertTrue(self.task(self.dispatcher, "a")["current_result_sha256"])
        event_id = state["outbox"][0]["event_id"]
        first = business_projection(self.dispatcher, self.queue)
        self.restart()
        self.dispatcher.tick()
        self.dispatcher.deliver_outbox()
        self.assertEqual(business_projection(self.dispatcher, self.queue), first)
        self.assertEqual(self.dispatcher.snapshot()["outbox"][0]["event_id"], event_id)
        self.assertEqual(self.dispatcher.snapshot()["outbox"][0]["status"], "delivered")
        self.assertEqual(self.dispatcher.snapshot()["counts"]["promotions"], 1)

    def test_outbox_delivery_rechecks_revision_after_concurrent_promotion(self):
        self.seed()
        old = self.run_to_review("a")
        old_hash = old["current_result_sha256"]
        other_queue = Queue(self.queue_path)
        other = ProjectDispatcher(self.control_path, other_queue)
        self.addCleanup(other_queue.close)
        self.addCleanup(other.close)
        observed = {}

        def interleave(stage):
            if stage == "before_outbox_delivery" and not observed:
                observed["entered"] = True
                other.revise("a", expected_revision=1, handoff_id="racing-outbox-r2", reason="New result before delivery",
                             fixture_id="repair_alpha")
                observed["new_hash"] = self.run_to_review("a", dispatcher=other, queue=other_queue)["current_result_sha256"]

        self.restart(failpoint=interleave)
        self.dispatcher.deliver_outbox()
        self.assertTrue(observed.get("entered"))
        self.assertNotEqual(observed["new_hash"], old_hash)
        self.assertEqual(self.task(self.dispatcher, "a")["current_result_sha256"], observed["new_hash"])
        old_event = next(e for e in self.dispatcher.snapshot()["outbox"] if e["revision"] == 1)
        self.assertEqual(old_event["status"], "superseded")

    def test_cancel_recovers_interrupted_enqueue_and_preserves_spent_reservation(self):
        self.seed()
        self.crash("after_queue_submit")
        job_id = self.queue.jobs()[0]["id"]
        self.restart()
        self.dispatcher.cancel("a", expected_revision=1, reason="Operator stopped this scope")
        self.dispatcher.tick()
        state = self.dispatcher.snapshot()
        self.assertEqual(self.task(self.dispatcher, "a")["status"], "cancelled")
        self.assertEqual(self.queue.get(job_id)["status"], "cancelled")
        self.assertEqual(state["budget"]["reserved_attempts"], 2)
        self.assertEqual(state["budget"]["actual_attempts"], 0)
        self.assertEqual(state["outbox"], [])
        self.assertIsNone(self.task(self.dispatcher, "a")["current_result_sha256"])
        first = business_projection(self.dispatcher, self.queue)
        self.dispatcher.tick()
        self.assertEqual(business_projection(self.dispatcher, self.queue), first)

    def test_review_accepts_one_project_and_rework_is_bounded_without_refund(self):
        self.seed("a", "project-a")
        self.seed("b", "project-b", fixture_id="evidence_beta")
        self.run_to_review("a")
        self.run_to_review("b")
        old_b = self.task(self.dispatcher, "b")
        self.review("a")
        self.review("b", "rejected", note="Missing a concrete negative control")
        revised = self.task(self.dispatcher, "b")
        self.assertEqual(self.task(self.dispatcher, "a")["status"], "accepted")
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["status"], "pending")
        self.assertEqual(revised["parent_handoff"], old_b["handoff_id"])
        self.assertNotEqual(revised["handoff_id"], old_b["handoff_id"])
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 4)
        after_first_review = business_projection(self.dispatcher, self.queue)
        self.dispatcher.review("b", expected_revision=1, result_sha256=old_b["current_result_sha256"],
                               decision="rejected", note="Missing a concrete negative control")
        self.assertEqual(business_projection(self.dispatcher, self.queue), after_first_review)
        self.run_to_review("b")
        self.review("b", "rejected", note="Still insufficient evidence")
        state = self.dispatcher.snapshot()
        self.assertEqual(self.task(self.dispatcher, "b")["status"], "exhausted")
        self.assertEqual(self.task(self.dispatcher, "b")["revision"], 2)
        self.assertEqual(state["budget"]["reserved_attempts"], 6)
        self.assertEqual(len(state["intents"]), 3)
        self.assertIn(old_b["current_result_sha256"], {a["sha256"] for a in state["artifacts"]})
        first = business_projection(self.dispatcher, self.queue)
        self.dispatcher.tick()
        self.assertEqual(business_projection(self.dispatcher, self.queue), first)

    def test_cancellation_after_queue_finish_prevents_control_publication(self):
        self.seed()
        self.dispatcher.tick()
        run_fixture_once(self.queue, worker_id="cancel-after-finish")
        completed = self.queue.jobs()[0]
        self.assertEqual(completed["status"], "completed")
        self.assertIsNotNone(completed["result"])
        self.dispatcher.cancel("a", expected_revision=1, reason="Cancel before accepting the computed result")
        self.dispatcher.tick()
        state = self.dispatcher.snapshot()
        self.assertEqual(self.task(self.dispatcher, "a")["status"], "cancelled")
        self.assertIsNone(self.task(self.dispatcher, "a")["current_result_sha256"])
        self.assertEqual(state["outbox"], [])
        self.assertEqual(state["budget"]["reserved_attempts"], 2)
        self.assertEqual(state["budget"]["actual_attempts"], 1)
        self.assertEqual(self.queue.get(completed["id"])["result"], completed["result"])
        first = business_projection(self.dispatcher, self.queue)
        self.dispatcher.tick()
        self.assertEqual(business_projection(self.dispatcher, self.queue), first)

    def test_result_cannot_raise_budget_or_create_untrusted_handler(self):
        self.seed()
        self.dispatcher.tick()
        finish = self.queue.finish

        def inject(job_id, worker_id, lease_token, result=None, error=None):
            changed = copy.deepcopy(result)
            changed["resource_policy"] = {**POLICY, "max_reserved_attempts": 999}
            changed["handler"] = "untrusted-shell"
            return finish(job_id, worker_id, lease_token, result=changed, error=error)

        with patch.object(self.queue, "finish", side_effect=inject):
            run_fixture_once(self.queue, worker_id="untrusted-artifact")
        self.dispatcher.tick()
        state = self.dispatcher.snapshot()
        self.assertEqual(state["budget"]["max_reserved_attempts"], 8)
        self.assertEqual(state["budget"]["reserved_attempts"], 2)
        self.assertEqual(state["budget"]["actual_attempts"], 1)
        self.assertEqual(state["outbox"], [])
        self.assertIsNone(self.task(self.dispatcher, "a")["current_result_sha256"])
        self.assertEqual(self.task(self.dispatcher, "a")["status"], "failed")

    def test_unavailable_channel_does_not_starve_other_project(self):
        self.seed("a", "project-a")
        self.seed("b", "project-b", fixture_id="evidence_beta")
        self.dispatcher.set_channel_available("project-a", False)
        self.dispatcher.tick()
        self.assertEqual(self.task(self.dispatcher, "a")["status"], "pending")
        self.assertEqual(self.task(self.dispatcher, "b")["status"], "dispatched")
        self.run_to_review("b")
        self.review("b")
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 2)
        self.dispatcher.set_channel_available("project-a", True)
        self.run_to_review("a")
        self.review("a")
        self.assertEqual(self.task(self.dispatcher, "a")["status"], "accepted")
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 4)

    def test_dependency_waits_for_review_not_just_completed_queue_result(self):
        self.seed("a", "project-a")
        self.seed("dependent", "project-a", dependencies=["a"])
        self.run_to_review("a")
        self.dispatcher.tick()
        self.assertEqual(len(self.queue.jobs()), 1)
        self.assertEqual(self.task(self.dispatcher, "dependent")["status"], "pending")
        self.review("a")
        self.dispatcher.tick()
        self.assertEqual(len(self.queue.jobs()), 2)
        self.assertEqual(self.task(self.dispatcher, "dependent")["status"], "dispatched")

    def test_dispatcher_snapshot_reports_attempts_without_exporting_queue_secrets(self):
        self.seed()
        self.dispatcher.tick()
        self.queue.register_worker("public-status-check", ["simulation", "evidence", "discovery", "directory"])
        claimed = self.queue.claim("public-status-check")
        self.assertIsNotNone(claimed)
        state = self.dispatcher.snapshot()
        self.assertEqual(state["budget"]["actual_attempts"], 1)
        self.assertEqual(state["budget"]["reserved_attempts"], 2)
        encoded = canonical(state)
        self.assertNotIn(claimed["lease_token"], encoded)
        for name in ("lease_token", "lease_digest", "completion_digest", "completion_fingerprint"):
            self.assertNotIn(name, encoded)
        self.assertEqual(state["model_calls"], 0)

    def test_project_must_explicitly_allow_fixed_artifact_class(self):
        for allowed in ([], ["unrelated-private-artifact"]):
            spec = {**self.project_spec("project-a"), "allowed_artifacts": allowed}
            with self.subTest(allowed=allowed), self.assertRaises(ValueError):
                self.dispatcher.create_project(spec)
        state = self.dispatcher.snapshot()
        self.assertEqual(state["projects"], [])
        self.assertEqual(state["intents"], [])
        self.assertEqual(state["budget"]["reserved_attempts"], 0)
        self.dispatcher.create_project(self.project_spec("project-a"))
        self.assertEqual(len(self.dispatcher.snapshot()["projects"]), 1)

    def test_whole_contract_utf8_budget_and_nonstring_fixture_reject_before_storage(self):
        oversized = {**self.project_spec("too-large"), "allowed_artifacts":
                     ["synthetic-evidence"] + [str(i) + "文" * 900 for i in range(7)]}
        with self.assertRaises(ValueError):
            self.dispatcher.create_project(oversized)
        self.assertEqual(self.dispatcher.snapshot()["projects"], [])
        self.dispatcher.create_project(self.project_spec("project-a"))
        spec = self.task_spec("a", "project-a")
        spec["limitations"] = [str(i) + "文" * 900 for i in range(8)]
        with self.assertRaises(ValueError):
            self.dispatcher.create_task(spec)
        for invalid in ({"instruction": "execute"}, ["evidence_alpha"], 1, None):
            with self.subTest(fixture=invalid), self.assertRaises(ValueError):
                self.dispatcher.create_task({**self.task_spec("a", "project-a"), "fixture_id": invalid})
        state = self.dispatcher.snapshot()
        self.assertEqual(state["tasks"], [])
        self.assertEqual(state["intents"], [])
        self.assertEqual(state["budget"]["reserved_attempts"], 0)

    def test_selection_event_survives_crash_and_waiting_does_not_grow_event_log(self):
        spec = self.seed()
        self.assertEqual(self.dispatcher.snapshot()["events"], [])
        self.crash("after_queue_submit")
        before = self.dispatcher.snapshot()["events"]
        reserved = [event for event in before if event["kind"] == "reserved"]
        self.assertEqual(len(reserved), 1)
        self.assertEqual(reserved[0]["task_id"], "a")
        self.assertEqual(reserved[0]["project_id"], "project-a")
        self.assertEqual(reserved[0]["handoff_id"], spec["handoff_id"])
        self.assertEqual(reserved[0]["revision"], 1)
        self.assertEqual(reserved[0]["details"]["reason"], "dependencies_accepted_channel_available_budget_reserved")
        self.assertTrue(reserved[0]["details"]["channel_available"])
        self.assertEqual(reserved[0]["details"]["dependencies"], [])
        self.assertEqual(reserved[0]["details"]["reserved_attempts"], 2)
        self.assertEqual(reserved[0]["details"]["total_reserved_attempts"], 2)
        self.restart()
        self.assertEqual(self.dispatcher.snapshot()["events"], before)
        self.dispatcher.tick()
        restored = self.dispatcher.snapshot()["events"]
        self.assertEqual(restored[:len(before)], before)
        bound = [event for event in restored if event["kind"] == "queue_bound"]
        self.assertEqual(len(bound), 1)
        self.assertTrue(bound[0]["details"]["recovered"])
        self.assertEqual(bound[0]["details"]["queue_job_id"], self.queue.jobs()[0]["id"])
        for _ in range(3):
            self.dispatcher.create_task(copy.deepcopy(spec))
            self.dispatcher.tick()
        self.assertEqual(self.dispatcher.snapshot()["events"], restored)
        self.assertLessEqual(len(restored), 256)

    def test_promotion_review_and_delivery_events_persist_without_duplicate_effects(self):
        self.seed()
        promoted = self.run_to_review("a")
        self.review("a")
        self.dispatcher.deliver_outbox()
        first = self.dispatcher.snapshot()["events"]
        kinds = [event["kind"] for event in first]
        for kind in ("reserved", "queue_bound", "promoted", "review", "outbox_delivered"):
            self.assertEqual(kinds.count(kind), 1)
        self.assertLess(kinds.index("reserved"), kinds.index("queue_bound"))
        self.assertLess(kinds.index("queue_bound"), kinds.index("promoted"))
        self.assertLess(kinds.index("promoted"), kinds.index("review"))
        self.assertLess(kinds.index("review"), kinds.index("outbox_delivered"))
        self.assertEqual(len({event["event_id"] for event in first}), len(first))
        self.assertEqual([event["sequence"] for event in first], sorted(event["sequence"] for event in first))
        reviewed = next(event for event in first if event["kind"] == "review")
        self.assertEqual(reviewed["details"]["result_sha256"], promoted["current_result_sha256"])
        self.assertEqual(reviewed["details"]["decision"], "accepted")
        self.restart()
        self.dispatcher.review("a", expected_revision=1, result_sha256=promoted["current_result_sha256"],
                               decision="accepted", note="Checked fixed evidence")
        self.dispatcher.deliver_outbox()
        self.dispatcher.tick()
        self.assertEqual(self.dispatcher.snapshot()["events"], first)


if __name__ == "__main__":
    unittest.main()
