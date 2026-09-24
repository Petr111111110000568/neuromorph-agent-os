"""Durability, concurrency and stale-owner regression tests for the real queue."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from workbench.network.queue import Queue, MAX_PAYLOAD_BYTES, MAX_RESULT_BYTES


def _process_claim(path, worker_id, ready, start, output):
    queue = Queue(path)
    try:
        queue.register_worker(worker_id, ["simulation"])
        ready.put(worker_id)
        if not start.wait(10):
            output.put({"error": "start deadline"})
            return
        output.put(queue.claim(worker_id))
    finally:
        queue.close()


class NetworkQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "queue.sqlite3"
        self.queue = Queue(self.path)

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def submit(self, **kwargs):
        return self.queue.submit("simulation", {"plugin_id": "quantum_circuit", "parameters": {}}, **kwargs)

    def register(self, worker="worker-a", capabilities=None):
        return self.queue.register_worker(worker, capabilities or ["simulation"])

    def assertRedacted(self, value, token=None):
        encoded = json.dumps(value)
        self.assertNotIn("lease_token", encoded)
        self.assertNotIn("lease_digest", encoded)
        self.assertNotIn("completion_digest", encoded)
        if token:
            self.assertNotIn(token, encoded)

    def test_two_connections_claim_once_concurrently(self):
        job = self.submit()
        self.register("first")
        self.register("second")
        other = Queue(self.path)
        barrier = threading.Barrier(2)

        def claim(q, name):
            barrier.wait(5)
            return q.claim(name)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(claim, self.queue, "first")
                b = pool.submit(claim, other, "second")
                claims = [a.result(10), b.result(10)]
            winners = [x for x in claims if x]
            self.assertEqual(len(winners), 1)
            self.assertEqual(winners[0]["id"], job["id"])
            self.assertEqual(other.get(job["id"])["attempts"], 1)
        finally:
            other.close()

    def test_independent_processes_claim_once(self):
        job = self.submit()
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        ready, output = context.Queue(), context.Queue()
        children = [context.Process(target=_process_claim, args=(str(self.path), name, ready, start, output))
                    for name in ("process-one", "process-two")]
        try:
            for child in children:
                child.start()
            self.assertEqual({ready.get(timeout=10), ready.get(timeout=10)}, {"process-one", "process-two"})
            start.set()
            claims = [output.get(timeout=10), output.get(timeout=10)]
            winners = [x for x in claims if x is not None]
            self.assertEqual(len(winners), 1)
            self.assertEqual(winners[0]["id"], job["id"])
            self.assertEqual(self.queue.get(job["id"])["attempts"], 1)
            for child in children:
                child.join(10)
                self.assertEqual(child.exitcode, 0)
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            ready.close()
            output.close()

    def test_restart_completion_idempotency_and_secret_redaction(self):
        self.register()
        job = self.submit(idempotency_key="campaign:one:simulation")
        claimed = self.queue.claim("worker-a")
        token = claimed["lease_token"]
        self.assertRedacted(self.queue.get(job["id"]), token)
        self.assertRedacted(self.queue.jobs(), token)
        self.assertRedacted(self.queue.workers(), token)
        stored = dict(self.queue._db.execute("SELECT * FROM network_jobs").fetchone())
        self.assertNotIn(token, json.dumps(stored))
        self.queue.close()
        self.queue = Queue(self.path)
        done = self.queue.finish(job["id"], "worker-a", token, {"probabilities": [0, 1]})
        self.assertEqual(done["status"], "completed")
        self.assertRedacted(done, token)
        self.queue.close()
        self.queue = Queue(self.path)
        repeated = self.queue.finish(job["id"], "worker-a", token, {"probabilities": [0, 1]})
        self.assertEqual(done, repeated)
        self.assertEqual(self.submit(idempotency_key="campaign:one:simulation")["id"], job["id"])
        with self.assertRaises(ValueError):
            self.queue.finish(job["id"], "worker-a", token, {"probabilities": [1, 0]})
        with self.assertRaises(ValueError):
            self.queue.submit("simulation", {"different": True}, idempotency_key="campaign:one:simulation")

    def test_idempotency_lookup_recovers_submission_after_restart(self):
        key = "campaign:interrupted:0:simulation"
        self.assertIsNone(self.queue.by_idempotency_key(key))
        self.assertEqual(self.queue.jobs(), [])
        self.register()
        job = self.submit(idempotency_key=key)
        claimed = self.queue.claim("worker-a")
        self.queue.close()
        self.queue = Queue(self.path)
        recovered = self.queue.by_idempotency_key(key)
        self.assertEqual(recovered["id"], job["id"])
        self.assertEqual(recovered["status"], "running")
        self.assertRedacted(recovered, claimed["lease_token"])
        self.queue.cancel(recovered["id"])
        self.assertEqual(self.queue.by_idempotency_key(key)["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.queue.finish(job["id"], "worker-a", claimed["lease_token"], {})
        for invalid in (None, "", "a b", "x" * 129, "bad\nkey", 3):
            with self.subTest(key=invalid), self.assertRaises(ValueError):
                self.queue.by_idempotency_key(invalid)

    def test_expiry_reclaim_fences_old_owner_and_exhausts_budget(self):
        self.register("old")
        self.register("new")
        job = self.submit(max_attempts=2)
        with patch("workbench.network.queue.time.time", return_value=1_800_000_000):
            old = self.queue.claim("old", lease_seconds=10)
        with patch("workbench.network.queue.time.time", return_value=1_800_000_011):
            with self.assertRaises(ValueError):
                self.queue.finish(job["id"], "old", old["lease_token"], {"stale": True})
            new = self.queue.claim("new", lease_seconds=10)
            self.assertEqual(new["attempts"], 2)
            self.assertNotEqual(new["lease_token"], old["lease_token"])
            with self.assertRaises(ValueError):
                self.queue.heartbeat(job["id"], "old", old["lease_token"])
        with patch("workbench.network.queue.time.time", return_value=1_800_000_022):
            self.assertIsNone(self.queue.claim("old"))
            ended = self.queue.get(job["id"])
            self.assertEqual(ended["status"], "failed")
            self.assertEqual(ended["attempts"], 2)
            self.assertIn("expired", ended["error"])
            with self.assertRaises(ValueError):
                self.queue.finish(job["id"], "new", new["lease_token"], {})

    def test_retry_error_idempotency_and_eventual_success(self):
        self.register()
        job = self.submit(max_attempts=2)
        first = self.queue.claim("worker-a")
        error = {"code": "temporary", "message": "provider unavailable"}
        retry = self.queue.finish(job["id"], "worker-a", first["lease_token"], error=error)
        self.assertEqual(retry["status"], "queued")
        self.assertEqual(self.queue.finish(job["id"], "worker-a", first["lease_token"], error=error), retry)
        second = self.queue.claim("worker-a")
        with self.assertRaises(ValueError):
            self.queue.finish(job["id"], "worker-a", first["lease_token"], error=error)
        done = self.queue.finish(job["id"], "worker-a", second["lease_token"], result={"ok": True})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["attempts"], 2)
        job2 = self.submit(max_attempts=1)
        claim2 = self.queue.claim("worker-a")
        failed = self.queue.finish(job2["id"], "worker-a", claim2["lease_token"], error="permanent")
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(self.queue.claim("worker-a"))

    def test_capability_routing_and_one_live_lease_per_worker(self):
        self.register("searcher", ["discovery"])
        self.register("simulator")
        simulation = self.submit()
        search = self.queue.submit("discovery", {"query": "epigenetic measurement"})
        self.assertEqual(self.queue.claim("searcher")["id"], search["id"])
        self.assertIsNone(self.queue.claim("searcher"))
        self.assertEqual(self.queue.claim("simulator")["id"], simulation["id"])
        self.submit()
        self.assertIsNone(self.queue.claim("simulator"))
        both = self.queue.submit("simulation", {}, capabilities=["simulation", "discovery"])
        self.register("combined", ["simulation", "discovery"])
        single = self.queue.claim("combined")
        self.queue.finish(single["id"], "combined", single["lease_token"], {})
        self.assertEqual(self.queue.claim("combined")["id"], both["id"])

    def test_heartbeat_extension_and_cancellation_are_fenced(self):
        self.register("owner")
        self.register("intruder")
        job = self.submit()
        with patch("workbench.network.queue.time.time", return_value=1_800_000_000):
            claimed = self.queue.claim("owner", 10)
        token = claimed["lease_token"]
        with patch("workbench.network.queue.time.time", return_value=1_800_000_005):
            with self.assertRaises(ValueError):
                self.queue.heartbeat(job["id"], "intruder", token)
            heartbeat = self.queue.heartbeat(job["id"], "owner", token, 20)
            self.assertRedacted(heartbeat, token)
        with patch("workbench.network.queue.time.time", return_value=1_800_000_015):
            self.assertEqual(self.queue.get(job["id"])["status"], "running")
            cancelled = self.queue.cancel(job["id"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(self.queue.cancel(job["id"]), cancelled)
            self.assertRedacted(cancelled, token)
            with self.assertRaises(ValueError):
                self.queue.finish(job["id"], "owner", token, {})
            self.assertIsNone(self.queue.claim("intruder"))

    def test_json_identity_and_resource_limits(self):
        invalid_payloads = [{"nan": float("nan")}, {"inf": float("inf")}, {3: "invalid key"},
                            {"tuple": (1, 2)}, {"huge": "a" * MAX_PAYLOAD_BYTES}]
        recursive = {}
        recursive["cycle"] = recursive
        invalid_payloads.append(recursive)
        for payload in invalid_payloads:
            with self.subTest(payload_type=type(payload)), self.assertRaises(ValueError):
                self.queue.submit("simulation", payload)
        for attempts in [0, 11, True, 1.2]:
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                self.submit(max_attempts=attempts)
        for worker_id in ["", "bad/worker", "x" * 129, "имя", None]:
            with self.subTest(worker=worker_id), self.assertRaises(ValueError):
                self.register(worker_id)
        for caps in [[], ["shell"], "simulation", [1], ["simulation"] * 3]:
            with self.subTest(caps=caps), self.assertRaises(ValueError):
                self.queue.register_worker("badcaps", caps)
        with self.assertRaises(ValueError):
            self.queue.submit("shell", {})
        with self.assertRaises(ValueError):
            self.queue.submit("simulation", {}, capabilities=["discovery"])
        with self.assertRaises(KeyError):
            self.queue.claim("unknown")
        self.register()
        job = self.submit()
        for seconds in [True, 0, -1, 3601, float("nan"), 10 ** 1000]:
            with self.subTest(seconds=str(seconds)[:30]), self.assertRaises(ValueError):
                self.queue.claim("worker-a", seconds)
        claim = self.queue.claim("worker-a")
        for result in [{"big": "x" * MAX_RESULT_BYTES}, {"bad": float("inf")}]:
            with self.assertRaises(ValueError):
                self.queue.finish(job["id"], "worker-a", claim["lease_token"], result)
        self.assertEqual(self.queue.get(job["id"])["status"], "running")
        with patch("workbench.network.queue.MAX_ACTIVE_JOBS", 1):
            with self.assertRaises(ValueError):
                self.submit()
        with patch("workbench.network.queue.MAX_WORKERS", 1):
            with self.assertRaises(ValueError):
                self.register("excess")


if __name__ == "__main__":
    unittest.main()
