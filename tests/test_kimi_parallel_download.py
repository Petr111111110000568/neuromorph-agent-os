"""Independent bounded-concurrency regressions; inert byte streams only.

No real HTTP requests, model weights, production destination or model execution.
Threads are real so overlap, cooperative cancellation and durable shared state
are tested rather than simulated by a sequence of synchronous helper calls.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download_kimi_weights.py"
SPEC = importlib.util.spec_from_file_location("kimi_parallel_download_fixture", SCRIPT)
download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now
        self.lock = threading.Lock()

    def time(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.now += seconds

    def set(self, value):
        with self.lock:
            self.now = value


class ByteResponse(io.BytesIO):
    def __init__(self, data, name, start, end, total, observer):
        super().__init__(data)
        self.status = 206
        self.headers = {"Content-Range": f"bytes {start}-{end}/{total}",
                        "Content-Length": str(end - start + 1)}
        self.name, self.observer = name, observer
        self.entered = False

    def geturl(self):
        return "https://huggingface.co/fixture/" + self.name

    def read(self, size=-1):
        if not self.entered:
            self.entered = True
            with self.observer.lock:
                self.observer.active += 1
                self.observer.maximum_active = max(self.observer.maximum_active, self.observer.active)
                self.observer.readers.add(self.name)
                if self.observer.active >= self.observer.required_overlap:
                    self.observer.overlap.set()
        if self.observer.read_hook is not None:
            self.observer.read_hook(self.name, self.tell())
        if not self.observer.release.wait(5):
            raise AssertionError("fixture stream release deadline")
        return super().read(size)

    def close(self):
        if self.entered and not self.closed:
            with self.observer.lock:
                self.observer.active -= 1
        super().close()


class ByteTransport:
    """An independent opener per shard, with one synchronized observation log."""
    def __init__(self, bodies, clock, *, hold=False, overlap=2, actions=None, read_hook=None):
        self.bodies, self.clock = bodies, clock
        self.lock = threading.RLock()
        self.actions = {name: list(values) for name, values in (actions or {}).items()}
        self.requests = []
        self.active = self.maximum_active = self.factories = 0
        self.readers = set()
        self.required_overlap = overlap
        self.overlap, self.release = threading.Event(), threading.Event()
        self.read_hook = read_hook
        if not hold:
            self.release.set()

    def factory(self):
        observer = self
        with self.lock:
            self.factories += 1

        class Opener:
            def open(self, request, timeout):
                name = request.full_url.rsplit("/", 1)[-1]
                start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", request.get_header("Range")).groups())
                with observer.lock:
                    observer.requests.append({"name": name, "range": (start, end),
                                              "time": observer.clock.time(), "request": request})
                    actions = observer.actions.get(name, [])
                    action = actions.pop(0) if actions else None
                if isinstance(action, Exception):
                    raise action
                body = observer.bodies[name]
                if callable(action):
                    return action(name, start, end)
                return ByteResponse(body[start:end + 1], name, start, end, len(body), observer)

        return Opener()


class KimiParallelDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.stop = self.root / "STOP"
        self.clock = Clock()
        self.entries = []
        self.bodies = {}
        for number in range(1, 7):
            name = f"model-{number:05d}-of-000096.safetensors"
            body = (f"tiny-public-fixture-{number}-" * 2).encode("ascii")
            self.entries.append({"path": name, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
            self.bodies[name] = body
        self.names = [entry["path"] for entry in self.entries]
        self.state = download.State(self.root, self.names)

    def invoke(self, entries=None, *, workers=2, transport=None):
        transport = transport or ByteTransport(self.bodies, self.clock)
        with patch.object(download, "RANGE_BYTES", 8), patch.object(download, "READ_BYTES", 2):
            return download.download_files(self.entries if entries is None else entries,
                self.root, self.state, self.stop, workers=workers, opener_factory=transport.factory,
                clock=self.clock.time, sleep=self.clock.sleep)

    def restored(self):
        self.state = download.State(self.root, self.names)
        return self.state

    def assert_files_verified(self, entries):
        expected = {entry["path"] for entry in entries}
        self.assertEqual(set(self.state.value["verified"]), expected)
        for entry in entries:
            path = self.root / entry["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
            self.assertFalse(path.with_name(path.name + ".part").exists())
        durable = json.loads(self.state.path.read_text(encoding="utf-8"))
        self.assertEqual(set(durable["verified"]), expected)

    def test_real_streams_overlap_but_never_exceed_two_workers(self):
        transport = ByteTransport(self.bodies, self.clock, hold=True)
        with ThreadPoolExecutor(max_workers=1) as control:
            future = control.submit(self.invoke, transport=transport)
            try:
                self.assertTrue(transport.overlap.wait(5), "two distinct streams must actually overlap")
                # Keep both streams blocked long enough for erroneously unbounded
                # queued workers to enter; the test never relies only on a count
                # of submitted futures or configured worker slots.
                time.sleep(0.05)
                with transport.lock:
                    self.assertEqual(transport.active, 2)
                    self.assertEqual(transport.maximum_active, 2)
                    self.assertEqual(len(transport.readers), 2)
            finally:
                transport.release.set()
            future.result(timeout=15)
        self.assertEqual(transport.maximum_active, 2)
        self.assertEqual(transport.active, 0)
        self.assert_files_verified(self.entries)
        for observed in transport.requests:
            self.assertIn(download.REVISION, observed["request"].full_url)
            self.assertIsNone(observed["request"].get_header("Authorization"))
            self.assertIsNone(observed["request"].get_header("Cookie"))

    def test_parallel_transient_retries_and_completions_survive_without_lost_writes(self):
        transport = ByteTransport(self.bodies, self.clock,
            actions={name: [URLError("DO-NOT-PERSIST-SIGNED-URL")] for name in self.names})
        self.invoke(workers=4, transport=transport)
        self.assert_files_verified(self.entries)
        self.assertEqual(self.state.value["retries"], {name: 1 for name in self.names})
        self.restored()
        self.assertEqual(self.state.value["retries"], {name: 1 for name in self.names})
        self.assert_files_verified(self.entries)
        self.assertNotIn("DO-NOT-PERSIST", self.state.path.read_text(encoding="utf-8"))

    def test_global_429_gate_survives_restart_and_blocks_other_shard_until_deadline(self):
        first, other = self.entries[:2]
        transport = ByteTransport(self.bodies, self.clock, actions={first["path"]: [
            HTTPError("https://DO-NOT-PERSIST", 429, "PRIVATE-UPSTREAM-BODY", {"Retry-After": "3600"}, None)]})
        with self.assertRaisesRegex(download.DownloadError, "retry_later"):
            self.invoke([first], workers=1, transport=transport)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.state.value["global_retry_at"], 4600.0)
        for before_deadline in (1000.0, 4599.0):
            with self.subTest(now=before_deadline):
                self.clock.set(before_deadline)
                self.restored()
                blocked = ByteTransport(self.bodies, self.clock)
                # Raising instead of advancing time proves there is no request
                # admitted before the persistent global deadline. A short gate
                # may wait; a long gate may return retry_later immediately.
                def no_early_time_travel(_):
                    raise download.Stopped()
                with patch.object(self.clock, "sleep", no_early_time_travel):
                    with self.assertRaises(download.DownloadError):
                        self.invoke([other], workers=1, transport=blocked)
                self.assertEqual(blocked.requests, [])
                self.assertGreaterEqual(self.state.value["global_retry_at"], 4600.0)
        self.clock.set(4600.0)
        self.restored()
        allowed = ByteTransport(self.bodies, self.clock)
        self.invoke([other], workers=1, transport=allowed)
        self.assertTrue(allowed.requests)
        self.assertTrue(all(item["time"] >= 4600 for item in allowed.requests))
        self.assertEqual((self.root / other["path"]).read_bytes(), self.bodies[other["path"]])
        raw = self.state.path.read_text(encoding="utf-8")
        self.assertNotIn("DO-NOT-PERSIST", raw)
        self.assertNotIn("PRIVATE-UPSTREAM", raw)

    def test_exhausted_retry_count_cannot_be_reset_by_parallel_batch_restart(self):
        first = self.entries[0]
        exhausted = ByteTransport(self.bodies, self.clock,
            actions={first["path"]: [URLError("fixture outage") for _ in range(download.MAX_RETRIES + 2)]})
        with self.assertRaisesRegex(download.DownloadError, "file_retry_limit_reached"):
            self.invoke([first], workers=1, transport=exhausted)
        self.assertEqual(len(exhausted.requests), download.MAX_RETRIES + 1)
        self.assertEqual(self.state.value["retries"][first["path"]], download.MAX_RETRIES + 1)
        self.restored()
        forbidden = ByteTransport(self.bodies, self.clock)
        with self.assertRaisesRegex(download.DownloadError, "file_retry_limit_reached"):
            self.invoke([first], workers=2, transport=forbidden)
        self.assertEqual(forbidden.requests, [])
        self.assertEqual(self.state.value["retries"][first["path"]], download.MAX_RETRIES + 1)

    def test_terminal_failure_cooperatively_stops_peer_and_never_starts_queued_shard(self):
        first, peer, queued = self.entries[:3]
        peer_reading = threading.Event()

        def read_hook(name, offset):
            if name == first["path"]:
                if not peer_reading.wait(5):
                    raise AssertionError("peer never began its stream")
                raise download.DownloadError("fixture_terminal_failure")
            if name == peer["path"] and offset == 0:
                peer_reading.set()
                if not self.state.cancel.wait(5):
                    raise AssertionError("terminal failure did not signal cooperative cancellation")

        transport = ByteTransport(self.bodies, self.clock, read_hook=read_hook)
        with self.assertRaisesRegex(download.DownloadError, "fixture_terminal_failure"):
            self.invoke([first, peer, queued], workers=2, transport=transport)
        self.assertEqual(transport.active, 0, "all owned stream contexts must close before return")
        self.assertTrue(self.state.cancel.is_set())
        self.assertNotIn(queued["path"], {item["name"] for item in transport.requests})
        for entry in (first, peer, queued):
            self.assertFalse((self.root / entry["path"]).exists())
        partial = self.root / (peer["path"] + ".part")
        self.assertTrue(partial.exists())
        saved = partial.read_bytes()
        self.assertEqual(saved, self.bodies[peer["path"]][:len(saved)])
        self.assertLess(len(saved), peer["bytes"])
        self.assertEqual(self.state.value["verified"], [])

    def test_parallel_resume_preserves_prefix_and_requests_only_exact_tail(self):
        first, second = self.entries[:2]
        partial = self.root / (first["path"] + ".part")
        partial.write_bytes(self.bodies[first["path"]][:6])
        transport = ByteTransport(self.bodies, self.clock)
        self.invoke([first, second], transport=transport)
        requests = [item for item in transport.requests if item["name"] == first["path"]]
        self.assertEqual(requests[0]["range"], (6, 13))
        self.assertTrue(all(item["range"][0] >= 6 for item in requests))
        self.assert_files_verified([first, second])

    def test_corrupt_complete_partial_is_never_promoted_in_parallel_batch(self):
        first = self.entries[0]
        partial = self.root / (first["path"] + ".part")
        partial.write_bytes(b"X" + self.bodies[first["path"]][1:])
        transport = ByteTransport(self.bodies, self.clock)
        with self.assertRaisesRegex(download.DownloadError, "hash_mismatch"):
            self.invoke([first], workers=2, transport=transport)
        self.assertEqual(transport.requests, [])
        self.assertTrue(partial.exists())
        self.assertFalse((self.root / first["path"]).exists())
        self.assertEqual(self.state.value["verified"], [])

    def test_old_final_is_rehashed_even_when_state_already_claimed_verified(self):
        old, new = self.entries[:2]
        final = self.root / old["path"]
        final.write_bytes(b"X" + self.bodies[old["path"]][1:])
        self.state.value["verified"] = [old["path"]]
        self.state.update("fixture_previous_verified", force=True)
        self.restored()
        transport = ByteTransport(self.bodies, self.clock)
        with patch.object(download, "hash_file", wraps=download.hash_file) as hashing:
            with self.assertRaisesRegex(download.DownloadError, "existing_weight_hash_mismatch"):
                self.invoke([old, new], transport=transport)
        self.assertTrue(any(Path(call.args[0]) == final for call in hashing.call_args_list))
        self.assertFalse(any(item["name"] == old["path"] for item in transport.requests))
        self.assertEqual(final.read_bytes(), b"X" + self.bodies[old["path"]][1:])
        self.assertNotEqual(self.state.value.get("status"), "complete")

    def test_invalid_worker_count_or_duplicate_shard_is_rejected_before_open(self):
        transport = ByteTransport(self.bodies, self.clock)
        for workers in (0, 5, True, 1.5, "2"):
            with self.subTest(workers=workers), self.assertRaisesRegex(download.DownloadError, "invalid_workers"):
                self.invoke([self.entries[0]], workers=workers, transport=transport)
        with self.assertRaisesRegex(download.DownloadError, "duplicate_or_unknown_shard_writer"):
            self.invoke([self.entries[0], self.entries[0]], transport=transport)
        self.assertEqual(transport.requests, [])
        self.assertEqual(transport.factories, 0)
        self.assertEqual(self.state.value["verified"], [])

    def test_success_rehashes_old_finals_and_reports_only_current_process_verifications(self):
        old, new = self.entries[:2]
        final = self.root / old["path"]
        final.write_bytes(self.bodies[old["path"]])
        self.state.value["verified"] = [old["path"]]
        self.state.update("fixture_previous_verified", force=True)
        self.restored()
        self.assertEqual(self.state.value["verified_this_run"], [])
        transport = ByteTransport(self.bodies, self.clock)
        with patch.object(download, "hash_file", wraps=download.hash_file) as hashing:
            result = self.invoke([old, new], transport=transport)
        self.assertTrue(any(Path(call.args[0]) == final for call in hashing.call_args_list))
        self.assertFalse(any(item["name"] == old["path"] for item in transport.requests))
        self.assertEqual(result["processed_files"], 2)
        self.assertEqual(set(result["verified_files"]), {old["path"], new["path"]})
        self.assertEqual(set(self.state.value["verified_this_run"]), {old["path"], new["path"]})
        self.assertEqual(self.state.value["active_files"], {})
        self.assert_files_verified([old, new])


if __name__ == "__main__":
    unittest.main()
