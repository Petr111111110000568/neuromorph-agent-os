"""Cloud CPU contract tests, including real pinned subprocesses and crash cuts.

These tests are intended for GitHub Actions/Colab. They do not call a model,
network endpoint, external scientific package or arbitrary program.
"""
import copy
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from workbench import m02_cpu as cpu
from workbench.network.queue import Queue
from workbench.project_dispatcher import POLICY, ProjectDispatcher


ROOT = Path(__file__).resolve().parents[1]
PARAMETERS = {"steps": 60, "replicates": 3, "recovery": .6, "coupling": .25,
              "load": .2, "perturbation": .5, "uncertainty": .15, "seed": 42}


def project(project_id="alpha"):
    return {"project_id": project_id, "mission": "Finite coupled CPU engineering experiment",
            "allowed_artifacts": ["synthetic-evidence"],
            "closure_criteria": ["Explicit review and reproducible finite result"],
            "resource_policy": dict(POLICY)}


def task(task_id="cpu-a", project_id="alpha", **overrides):
    return {"task_id": task_id, "project_id": project_id, "goal": "Check a predefined model invariant",
            "dependencies": [], "revision": 1, "handoff_id": task_id + "-r1",
            "base_commit": cpu.ACCEPTED_BASE_COMMIT, "handler": cpu.HANDLER,
            "parameters": {**PARAMETERS, **overrides}, "limitations": ["Synthetic CPU result; not scientific validation"]}


def business(dispatcher, queue):
    """Fixed B: identities, payload/results, pointers, budget, publication/review.

    Timestamps, lease maintenance, delivery flags and diagnostic counters are
    excluded before running the experiment. First effects are asserted apart.
    """
    state = dispatcher.snapshot()
    return {
        "budget": state["budget"],
        "tasks": [(t["task_id"], t["revision"], t["handoff_id"], t["input_sha256"], t["current_result_sha256"]) for t in state["tasks"]],
        "intents": [(i["handoff_id"], i["input_sha256"], i["queue_key"], i["reserved_attempts"]) for i in state["intents"]],
        "jobs": [(j["id"], j["kind"], j["payload"], j["attempts"], j["max_attempts"], j["result"]) for j in queue.jobs()],
        "cpu_results": state["cpu_results"], "artifacts": state["artifacts"],
        "publications": [(p["event_id"], p["handoff_id"], p["revision"], p["result_sha256"]) for p in state["outbox"]],
        "reviews": state["reviews"],
    }


def _crash(control, queued, stage):
    def cut(point):
        if point == stage:
            os._exit(71)
    queue = Queue(queued)
    dispatcher = ProjectDispatcher(control, queue, cpu_root=ROOT, failpoint=cut)
    if stage in ("after_queue_submit", "after_control_promotion"):
        dispatcher.tick()
    else:
        cpu.run_cpu_once(dispatcher, root=ROOT, worker_id="crash-cpu", failpoint=cut)
    os._exit(72)


def _competing_tick(control, queued, identity, ready, start, output):
    queue = Queue(queued)
    dispatcher = None
    try:
        dispatcher = ProjectDispatcher(control, queue, cpu_root=ROOT)
        ready.put(identity)
        if not start.wait(15):
            raise RuntimeError("concurrent dispatch start deadline")
        output.put({"identity": identity, "result": dispatcher.tick()})
    except Exception as exc:
        output.put({"identity": identity, "error": type(exc).__name__})
    finally:
        if dispatcher is not None:
            dispatcher.close()
        queue.close()


class M02CPUTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.control = Path(self.temp.name) / "control.sqlite3"
        self.queued = Path(self.temp.name) / "queue.sqlite3"
        self.queue = Queue(self.queued)
        self.dispatcher = ProjectDispatcher(self.control, self.queue, cpu_root=ROOT)
        self.addCleanup(self.close_current)

    def close_current(self):
        self.dispatcher.close()
        self.queue.close()

    def restart(self):
        self.close_current()
        self.queue = Queue(self.queued)
        self.dispatcher = ProjectDispatcher(self.control, self.queue, cpu_root=ROOT)

    def seed(self, task_id="cpu-a", project_id="alpha", **parameters):
        self.dispatcher.create_project(project(project_id))
        spec = task(task_id, project_id, **parameters)
        self.dispatcher.create_task(spec)
        return spec

    def current(self, task_id="cpu-a"):
        return next(t for t in self.dispatcher.snapshot()["tasks"] if t["task_id"] == task_id)

    def compute(self):
        self.dispatcher.tick()
        result = cpu.run_cpu_once(self.dispatcher, root=ROOT)
        self.assertEqual(result["status"], "completed")
        self.dispatcher.tick()
        return self.dispatcher.snapshot()["artifacts"][-1]["content"]

    def review(self, task_id="cpu-a", decision="accepted"):
        current = self.current(task_id)
        return self.dispatcher.review(task_id, expected_revision=current["revision"],
            result_sha256=current["current_result_sha256"], decision=decision, note="Checked predefined CPU invariant")

    def crash(self, stage):
        ctx = multiprocessing.get_context("spawn")
        child = ctx.Process(target=_crash, args=(str(self.control), str(self.queued), stage))
        try:
            child.start()
            child.join(35)
            self.assertEqual(child.exitcode, 71, "required durable boundary must be reached")
        finally:
            if child.is_alive():
                child.terminate()
                child.join(5)

    def test_all_eight_parameters_are_closed_bounded_finite_non_boolean(self):
        cases = [(key, True) for key in PARAMETERS]
        cases += [(key, float("nan")) for key in PARAMETERS]
        cases += [(key, float("inf")) for key in PARAMETERS]
        cases += [(key, "1") for key in PARAMETERS]
        cases += [(key, 10 ** 400) for key in PARAMETERS]
        cases += [(key, low - 1) for key, (low, _, _) in cpu.BOUNDS.items()]
        cases += [(key, high + 1) for key, (_, high, _) in cpu.BOUNDS.items()]
        cases += [("steps", 60.0), ("replicates", 3.0), ("seed", 42.0)]
        self.dispatcher.create_project(project())
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.dispatcher.create_task(task(**{key: value}))
        for key in PARAMETERS:
            spec = task()
            del spec["parameters"][key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                self.dispatcher.create_task(spec)
        spec = task()
        spec["parameters"]["python"] = "print(1)"
        with self.assertRaises(ValueError):
            self.dispatcher.create_task(spec)
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 0)
        self.assertEqual(self.queue.jobs(), [])

    def test_unknown_handler_base_and_extra_task_data_refuse_before_reserve(self):
        self.dispatcher.create_project(project())
        for change in ({"handler": "simulation"}, {"base_commit": "0" * 40},
                       {"plugin_id": "coupled_dynamics"}, {"url": "https://example.invalid"},
                       {"input_sha256": "0" * 64}, {"parameters": {}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.dispatcher.create_task({**task(), **change})
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 0)
        self.assertEqual(self.queue.jobs(), [])

    def test_input_hash_and_closed_envelope(self):
        payload = cpu.make_input(task())
        self.assertEqual(cpu.validate_input(payload), payload)
        for change in ({"input_sha256": "0" * 64}, {"parameters": {**PARAMETERS, "coupling": 1}},
                       {"revision": True}, {"schema_version": True}, {"parent_handoff": "invented"},
                       {"project": project()}, {"plugin_id": "coupled_dynamics"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                cpu.validate_input({**payload, **change})
        for key in payload:
            bad = dict(payload)
            del bad[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                cpu.validate_input(bad)

    def test_changed_builtin_and_self_replaced_registry_refuse_before_reserve(self):
        root = Path(self.temp.name) / "root"
        for name in (*cpu.PINS, "data/builtin_pins.json"):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        self.dispatcher.cpu_root = root
        self.seed()
        target = root / "plugin_worker.py"
        target.write_bytes(target.read_bytes() + b"\n# changed\n")
        with self.assertRaises(ValueError):
            self.dispatcher.tick()
        registry = {"files": dict(cpu.PINS)}
        import hashlib
        registry["files"]["plugin_worker.py"] = hashlib.sha256(target.read_bytes()).hexdigest()
        (root / "data/builtin_pins.json").write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.dispatcher.tick()
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 0)
        self.assertEqual(self.queue.jobs(), [])

    def test_control_hash_mismatch_refuses_before_reserve(self):
        self.seed()
        self.dispatcher._db.execute("UPDATE m02_tasks SET input_sha256=?", ("0" * 64,))
        with self.assertRaises(ValueError):
            self.dispatcher.tick()
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 0)
        self.assertEqual(self.queue.jobs(), [])

    def test_generic_worker_cannot_claim_cpu_job(self):
        self.seed()
        self.dispatcher.tick()
        self.queue.register_worker("generic", ["simulation", "discovery", "evidence", "directory"])
        self.assertIsNone(self.queue.claim("generic"))
        job = self.queue.jobs()[0]
        self.assertEqual((job["kind"], job["capabilities"], job["attempts"]), (cpu.KIND, [cpu.KIND], 0))

    def test_self_signed_input_without_control_authority_never_executes(self):
        payload = cpu.make_input(task())
        self.queue.submit(cpu.KIND, payload, max_attempts=2)
        with patch("workbench.service.Service.run", side_effect=AssertionError("must not run")):
            result = cpu.run_cpu_once(self.dispatcher, root=ROOT)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(self.dispatcher.snapshot()["cpu_results"], [])
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 0)

    def test_real_zero_perturbation_and_first_effect_then_idempotent_replay(self):
        spec = self.seed(perturbation=0)
        self.assertEqual(self.dispatcher.snapshot()["artifacts"], [])
        result = self.compute()
        scientific = result["scientific_payload"]
        self.assertEqual(scientific["series"][0]["points"], scientific["series"][1]["points"])
        self.assertEqual(scientific["series"][2]["points"], scientific["series"][3]["points"])
        self.assertEqual([m["value"] for m in scientific["metrics"][:2]], [0, 0])
        self.assertEqual(result["scientific_sha256"], cpu.digest(scientific))
        state = self.dispatcher.snapshot()
        self.assertEqual((len(state["artifacts"]), len(state["outbox"]), len(state["cpu_results"])), (1, 1, 1))
        self.assertEqual((state["budget"]["reserved_attempts"], state["budget"]["actual_attempts"]), (2, 1))
        self.assertEqual(scientific["model"]["calibrated"], False)
        self.review()
        before = business(self.dispatcher, self.queue)
        self.dispatcher.create_task(spec)
        self.dispatcher.tick()
        self.review()
        self.dispatcher.deliver_outbox()
        self.dispatcher.deliver_outbox()
        self.assertEqual(business(self.dispatcher, self.queue), before)
        with self.assertRaises(ValueError):
            self.dispatcher.create_task(task(perturbation=1))

    def test_real_coupling_preserves_means_but_changes_local_component(self):
        self.seed("coupling-zero", coupling=0)
        zero = self.compute()["scientific_payload"]
        self.review("coupling-zero")
        self.seed("coupling-one", coupling=1)
        self.compute()
        row = self.dispatcher.cpu_saved_result(self.current("coupling-one")["queue_job_id"])
        one = row["scientific_payload"]
        for series in (0, 1, 4, 5):
            error = max(abs(a["y"] - b["y"]) for a, b in zip(zero["series"][series]["points"], one["series"][series]["points"]))
            self.assertLessEqual(error, 1e-12)
        local_change = max(abs(a["y"] - b["y"]) for a, b in zip(zero["series"][3]["points"], one["series"][3]["points"]))
        self.assertGreater(local_change, .001)
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 4)

    def test_scientific_schema_rejects_nonfinite_grid_and_metadata_changes(self):
        self.seed()
        result = self.compute()
        base = result["scientific_payload"]
        mutations = [lambda v: v.update(extra="bad"), lambda v: v["model"].update(calibrated=True),
                     lambda v: v["series"][0]["points"][1].update(x=.051),
                     lambda v: v["series"][0]["points"][1].update(y=float("nan")),
                     lambda v: v["series"][0]["points"][1].update(y=True),
                     lambda v: v["series"].pop(), lambda v: v["table"][0].update(p05=float("inf")),
                     lambda v: v["metrics"][2].update(value=True), lambda v: v["parameters"].update(seed=43),
                     lambda v: v.update(limitations=[])]
        for mutate in mutations:
            bad = copy.deepcopy(base)
            mutate(bad)
            with self.assertRaises(ValueError):
                cpu.scientific_payload(bad, PARAMETERS)

    def test_changed_result_hash_or_valid_shape_forgery_is_not_promoted(self):
        self.seed()
        self.dispatcher.tick()
        cpu.run_cpu_once(self.dispatcher, root=ROOT)
        job = self.queue.jobs()[0]
        forged = copy.deepcopy(job["result"])
        forged["scientific_payload"]["series"][0]["points"][1]["y"] += .1
        forged["scientific_sha256"] = cpu.digest(forged["scientific_payload"])
        self.queue._db.execute("UPDATE network_jobs SET result=? WHERE id=?", (cpu.canonical(forged), job["id"]))
        self.dispatcher.tick()
        self.assertEqual(self.current()["status"], "failed")
        self.assertEqual(self.dispatcher.snapshot()["artifacts"], [])
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 2)

    def test_crash_after_enqueue_recovers_same_job_and_reservation(self):
        self.seed()
        self.crash("after_queue_submit")
        initial = self.queue.jobs()[0]
        self.restart()
        self.dispatcher.tick()
        self.assertEqual(self.queue.jobs()[0]["id"], initial["id"])
        self.assertEqual(self.current()["queue_job_id"], initial["id"])
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 2)

    def test_worker_between_enqueue_commit_and_control_binding_recovers_identity(self):
        self.seed()
        results = []
        def claim_during_binding_gap(stage):
            if stage == "after_queue_submit":
                results.append(cpu.run_cpu_once(self.dispatcher, root=ROOT))
        self.dispatcher.failpoint = claim_during_binding_gap
        self.dispatcher.tick()
        self.assertEqual([r["status"] for r in results], ["completed"])
        self.dispatcher.tick()
        self.assertEqual(self.current()["status"], "awaiting_review")
        jobs = self.queue.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["attempts"], 1)
        self.assertEqual(self.current()["queue_job_id"], jobs[0]["id"])
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 2)

    def test_crash_after_compute_without_durable_result_uses_second_attempt(self):
        self.seed()
        self.dispatcher.tick()
        self.crash("after_cpu_compute")
        self.assertEqual(self.dispatcher.snapshot()["cpu_results"], [])
        self.queue._db.execute("UPDATE network_jobs SET lease_until=0 WHERE status='running'")
        self.restart()
        cpu.run_cpu_once(self.dispatcher, root=ROOT)
        job = self.queue.jobs()[0]
        self.assertEqual(job["attempts"], 2)
        self.assertEqual(job["result"]["execution_attempt"], 2)
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 2)

    def test_crash_after_saved_result_reuses_science_without_recomputation(self):
        self.seed()
        self.dispatcher.tick()
        self.crash("after_cpu_result_saved")
        saved = self.dispatcher.snapshot()["cpu_results"][0]["content"]
        self.queue._db.execute("UPDATE network_jobs SET lease_until=0 WHERE status='running'")
        self.restart()
        with patch("workbench.service.Service.run", side_effect=AssertionError("must reuse durable result")):
            outcome = cpu.run_cpu_once(self.dispatcher, root=ROOT)
        self.assertEqual(outcome["status"], "completed")
        job = self.queue.jobs()[0]
        self.assertEqual(job["result"], saved)
        self.assertEqual((job["attempts"], saved["execution_attempt"]), (2, 1))
        self.dispatcher.tick()
        self.assertEqual(self.current()["status"], "awaiting_review")
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 2)

    def test_crash_after_finish_and_after_promotion_do_not_duplicate_effects(self):
        self.seed()
        self.dispatcher.tick()
        self.crash("after_queue_finish")
        self.assertEqual(self.queue.jobs()[0]["status"], "completed")
        self.restart()
        self.crash("after_control_promotion")
        self.restart()
        before = business(self.dispatcher, self.queue)
        self.dispatcher.tick()
        with patch("workbench.service.Service.run", side_effect=AssertionError("must not repeat")):
            self.assertEqual(cpu.run_cpu_once(self.dispatcher, root=ROOT)["status"], "waiting")
        self.assertEqual(business(self.dispatcher, self.queue), before)
        self.assertEqual(len(self.dispatcher.snapshot()["outbox"]), 1)

    def test_finish_ack_loss_reads_durable_queue_without_another_claim(self):
        self.seed()
        self.dispatcher.tick()
        finish = self.queue.finish
        def lost_ack(*args, **kwargs):
            finish(*args, **kwargs)
            raise OSError("lost local ACK")
        with patch.object(self.queue, "finish", side_effect=lost_ack):
            result = cpu.run_cpu_once(self.dispatcher, root=ROOT)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.queue.jobs()[0]["attempts"], 1)
        self.dispatcher.tick()
        self.assertEqual(len(self.dispatcher.snapshot()["artifacts"]), 1)

    def test_stale_r1_cannot_promote_after_revision_changed_during_cas(self):
        self.seed()
        self.dispatcher.tick()
        cpu.run_cpu_once(self.dispatcher, root=ROOT)
        fired = []
        def change_revision(stage):
            if stage == "before_promotion" and not fired:
                fired.append(stage)
                self.dispatcher.revise("cpu-a", expected_revision=1, handoff_id="cpu-a-r2", reason="Parameter review",
                                       parameters={**PARAMETERS, "coupling": 1})
        self.dispatcher.failpoint = change_revision
        self.dispatcher.tick()
        self.assertEqual(self.current()["revision"], 2)
        self.assertIsNone(self.current()["current_result_sha256"])
        self.assertEqual(self.dispatcher.snapshot()["artifacts"], [])
        self.dispatcher.tick()
        cpu.run_cpu_once(self.dispatcher, root=ROOT)
        self.dispatcher.tick()
        current = self.current()
        self.assertEqual(current["status"], "awaiting_review")
        result = self.dispatcher.cpu_saved_result(current["queue_job_id"])
        self.assertEqual(result["input"]["parent_handoff"], "cpu-a-r1")
        self.assertEqual((result["input"]["revision"], result["input"]["parameters"]["coupling"]), (2, 1))
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 4)

    def test_rejected_cpu_review_preserves_handler_and_parameters_in_r2(self):
        self.seed()
        self.compute()
        self.review(decision="rejected")
        revised = self.current()
        self.assertEqual((revised["handler"], revised["parameters"]), (cpu.HANDLER, PARAMETERS))
        self.assertNotIn("fixture_id", revised)
        self.assertEqual(revised["parent_handoff"], "cpu-a-r1")
        self.compute()
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 4)

    def test_cancel_after_finish_prevents_publication_and_retains_spent_budget(self):
        self.seed()
        self.dispatcher.tick()
        cpu.run_cpu_once(self.dispatcher, root=ROOT)
        self.dispatcher.cancel("cpu-a", expected_revision=1, reason="Operator cancellation")
        self.dispatcher.tick()
        self.assertEqual(self.current()["status"], "cancelled")
        self.assertIsNone(self.current()["current_result_sha256"])
        state = self.dispatcher.snapshot()
        self.assertEqual((len(state["artifacts"]), len(state["outbox"]), len(state["cpu_results"])), (0, 0, 1))
        self.assertEqual((state["budget"]["reserved_attempts"], state["budget"]["actual_attempts"]), (2, 1))

    def test_cpu_and_fixture_share_the_same_monotonic_global_budget(self):
        self.seed()
        self.dispatcher.tick()
        self.dispatcher.cancel("cpu-a", expected_revision=1, reason="Release project slot, keep reserve")
        for index in range(1, 4):
            self.seed("cpu-" + str(index))
            self.dispatcher.tick()
            self.dispatcher.cancel("cpu-" + str(index), expected_revision=1, reason="Keep spent reservation")
        fixture = task("fixture")
        fixture.pop("parameters")
        fixture.update(handler="m02.fixture.v1", fixture_id="evidence_alpha")
        self.dispatcher.create_task(fixture)
        result = self.dispatcher.tick()
        self.assertIn("budget_exhausted", result["reason"])
        self.assertEqual((len(self.queue.jobs()), self.dispatcher.snapshot()["budget"]["reserved_attempts"]), (4, 8))

    def test_two_cpu_dispatch_processes_cannot_spend_last_reservation_twice(self):
        for index in range(3):
            task_id = "spent-" + str(index)
            self.seed(task_id)
            self.dispatcher.tick()
            self.dispatcher.cancel(task_id, expected_revision=1, reason="Retain reservation while freeing slot")
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 6)
        self.seed("candidate-a", "alpha")
        self.seed("candidate-b", "beta")
        ctx = multiprocessing.get_context("spawn")
        ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        children = [ctx.Process(target=_competing_tick,
                    args=(str(self.control), str(self.queued), name, ready, start, output))
                    for name in ("first", "second")]
        try:
            for child in children:
                child.start()
            self.assertEqual({ready.get(timeout=20), ready.get(timeout=20)}, {"first", "second"})
            start.set()
            replies = [output.get(timeout=35), output.get(timeout=35)]
            for child in children:
                child.join(10)
                self.assertEqual(child.exitcode, 0)
            self.assertFalse(any("error" in r for r in replies), replies)
            self.assertEqual(sum(r["result"]["status"] == "dispatched" for r in replies), 1)
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            ready.close()
            output.close()
        state = self.dispatcher.snapshot()
        self.assertEqual(state["budget"]["reserved_attempts"], 8)
        candidates = [t for t in state["tasks"] if t["task_id"].startswith("candidate-")]
        self.assertEqual(sorted(t["status"] for t in candidates), ["dispatched", "pending"])
        self.assertEqual(len(self.queue.jobs()), 4)
        self.restart()
        self.dispatcher.tick()
        self.assertEqual(self.dispatcher.snapshot()["budget"]["reserved_attempts"], 8)
        self.assertEqual(len(self.queue.jobs()), 4)


if __name__ == "__main__":
    unittest.main()
