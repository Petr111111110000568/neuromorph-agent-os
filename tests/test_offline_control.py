"""Offline UI controller tests: injected finite runner, no model or network.

The fixture manifest deliberately contains no executable configuration. Only the
controller's admission/persistence is under test; model integrity belongs to the
offline_council/local_review suites.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from workbench.autonomy.daemon import _job_lock
from workbench.offline_control import OfflineControl
from workbench.service import ServiceError


class OfflineControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.manifest = self.root / "runtime" / "local-model" / "manifest.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text("{}", encoding="utf-8")
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.controls = []
        self.addCleanup(self.finish_threads)
        self.control = self.make_control(self.fake_runner)
        self.request = {"question": "Which observation would falsify H?",
                        "request_id": "offline-request-1", "public_data_confirmed": True}

    def make_control(self, runner):
        control = OfflineControl(self.root, self.data_dir, runner=runner)
        self.controls.append(control)
        return control

    def finish_threads(self):
        self.release.set()
        for control in self.controls:
            thread = control.thread
            if thread and thread.ident is not None:
                thread.join(timeout=5)

    def fake_runner(self, question, **options):
        folder = options["output_dir"]
        # The controller must have persisted the request before it delegates.
        saved = json.loads((folder / "request.json").read_text(encoding="utf-8"))
        self.calls.append({"question": question, "saved": saved, "options": options})
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("Test runner was not released")
        status = "stopped" if options["stop_file"].exists() else "completed"
        receipt = {"schema_version": 1, "status": status, "external_model_calls": 0,
                   "scientific_validation": False, "answers": []}
        (folder / "council-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        return receipt

    def start_blocked(self):
        response = self.control.start(dict(self.request))
        self.assertTrue(self.entered.wait(timeout=5), "fake runner did not start")
        self.assertEqual(len(self.calls), 1)
        return response

    def join_finished(self):
        self.release.set()
        self.control.thread.join(timeout=5)
        self.assertFalse(self.control.thread.is_alive())

    def assert_error(self, operation, code, status):
        with self.assertRaises(ServiceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.status, status)

    def test_request_is_persisted_before_runner_and_paths_are_fixed(self):
        response = self.start_blocked()
        job = hashlib.sha256(self.request["request_id"].encode()).hexdigest()
        self.assertEqual(response["id"], job)
        self.assertFalse(response["duplicate_suppressed"])
        call = self.calls[0]
        self.assertEqual(call["saved"], self.request)
        options = call["options"]
        folder = self.data_dir / "offline-council" / job
        self.assertEqual(options["output_dir"], folder)
        self.assertEqual(options["state_file"], folder / "state.json")
        self.assertEqual(options["stop_file"], folder / "STOP")
        self.assertEqual(options["manifest"], self.manifest)
        self.assertEqual(options["project_id"], "local-ui")
        self.assertEqual(options["task_id"], job)
        self.assertLessEqual(options["max_tokens"], 512)
        self.assertLessEqual(options["timeout"], 300)

    def test_same_key_same_payload_during_run_is_suppressed(self):
        first = self.start_blocked()
        duplicate = self.control.start(dict(self.request))
        self.assertEqual(duplicate["id"], first["id"])
        self.assertTrue(duplicate["duplicate_suppressed"])
        self.assertEqual(len(self.calls), 1)

    def test_same_key_changed_question_is_conflict_without_rewrite(self):
        first = self.start_blocked()
        changed = {**self.request, "question": "An unrelated question"}
        self.assert_error(lambda: self.control.start(changed), "idempotency_conflict", 409)
        request_path = self.control.folder / first["id"] / "request.json"
        self.assertEqual(json.loads(request_path.read_text(encoding="utf-8")), self.request)
        self.assertEqual(len(self.calls), 1)

    def test_completed_request_stays_deduplicated_after_controller_restart(self):
        first = self.start_blocked()
        self.join_finished()
        forbidden_runner = Mock(side_effect=AssertionError("Must not rerun saved request"))
        restarted = self.make_control(forbidden_runner)
        duplicate = restarted.start(dict(self.request))
        self.assertEqual(duplicate["id"], first["id"])
        self.assertTrue(duplicate["duplicate_suppressed"])
        forbidden_runner.assert_not_called()
        self.assertIsNone(restarted.thread)
        snapshot = restarted.snapshot()
        self.assertFalse(snapshot["running"])
        self.assertEqual(len(snapshot["jobs"]), 1)
        self.assertEqual(snapshot["jobs"][0]["status"], "completed")

    def test_busy_rejection_does_not_reserve_a_second_request(self):
        self.start_blocked()
        other = {**self.request, "request_id": "offline-request-2"}
        self.assert_error(lambda: self.control.start(other), "offline_busy", 409)
        second_id = hashlib.sha256(other["request_id"].encode()).hexdigest()
        self.assertFalse((self.control.folder / second_id).exists())
        self.assertEqual(len(self.calls), 1)

    def test_missing_manifest_does_not_reserve_or_start(self):
        self.manifest.unlink()
        self.assert_error(lambda: self.control.start(self.request), "model_not_configured", 503)
        self.assertIsNone(self.control.thread)
        self.assertEqual(self.calls, [])
        self.assertEqual([p for p in self.control.folder.iterdir() if p.is_dir()], [])
        self.assertFalse(self.control.snapshot()["model_files_present"])

    def test_other_controller_cannot_reserve_while_model_is_busy(self):
        self.start_blocked()
        forbidden_runner = Mock(side_effect=AssertionError("Concurrent model use"))
        other_control = self.make_control(forbidden_runner)
        other = {**self.request, "request_id": "other-controller-request"}
        self.assert_error(lambda: other_control.start(other), "offline_busy", 409)
        second_id = hashlib.sha256(other["request_id"].encode()).hexdigest()
        self.assertFalse((other_control.folder / second_id).exists())
        forbidden_runner.assert_not_called()

    def test_admission_contention_is_retryable_without_reservation(self):
        with _job_lock(self.control.folder / "admission.lock"):
            self.assert_error(lambda: self.control.start(self.request), "offline_busy", 409)
        self.assertIsNone(self.control.thread)
        self.assertEqual(self.calls, [])
        self.assertEqual([p for p in self.control.folder.iterdir() if p.is_dir()], [])

    def test_returned_platform_receipt_is_persisted_without_inference(self):
        receipt = {"schema_version": 1, "status": "unsupported_platform",
                   "reserved_calls": 0, "external_model_calls": 0}
        runner = Mock(return_value=receipt)
        control = self.make_control(runner)
        response = control.start(self.request)
        control.thread.join(timeout=5)
        self.assertFalse(control.thread.is_alive())
        path = control.folder / response["id"] / "council-receipt.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), receipt)
        self.assertEqual(control.snapshot()["jobs"][0]["status"], "unsupported_platform")
        self.assertTrue(control.start(dict(self.request))["duplicate_suppressed"])
        self.assertEqual(runner.call_count, 1)

    def test_linked_data_directory_is_rejected_before_outside_write(self):
        target = self.root / "outside-data"
        target.mkdir()
        linked = self.root / "linked-data"
        try:
            linked.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links require unavailable platform privileges")
        with self.assertRaises(ValueError):
            OfflineControl(self.root, linked, runner=Mock())
        self.assertFalse((target / "offline-council").exists())

    def test_history_cap_rejects_new_key_without_deleting_history(self):
        for index in range(24):
            (self.control.folder / hashlib.sha256(str(index).encode()).hexdigest()).mkdir()
        before = {p.name for p in self.control.folder.iterdir() if p.is_dir()}
        self.assert_error(lambda: self.control.start(self.request), "offline_limit", 409)
        self.assertEqual({p.name for p in self.control.folder.iterdir() if p.is_dir()}, before)
        self.assertIsNone(self.control.thread)
        self.assertEqual(self.calls, [])

    def test_stop_marks_pending_only_and_is_idempotent(self):
        completed = self.control.folder / ("c" * 64)
        completed.mkdir()
        (completed / "request.json").write_text("{}", encoding="utf-8")
        (completed / "council-receipt.json").write_text('{"status":"completed"}', encoding="utf-8")
        started = self.start_blocked()
        pending = self.control.folder / started["id"]
        self.assertTrue(self.control.stop()["stop_requested"])
        self.assertTrue(self.control.stop()["stop_requested"])
        self.assertEqual((pending / "STOP").read_text(encoding="utf-8"), "stop")
        self.assertFalse((completed / "STOP").exists())
        self.join_finished()
        receipt = json.loads((pending / "council-receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(len(self.calls), 1)

    def test_invalid_payloads_never_create_a_job(self):
        invalid = [
            None, [], {}, {**self.request, "public_data_confirmed": False},
            {**self.request, "public_data_confirmed": 1},
            {**self.request, "executable": "untrusted.exe"},
            {**self.request, "request_id": "../elsewhere"},
            {**self.request, "request_id": "x/y"},
            {**self.request, "request_id": "x" * 81},
            {**self.request, "question": ""},
            {**self.request, "question": "x" * 2001},
            {**self.request, "question": "\x00"},
        ]
        for payload in invalid:
            with self.subTest(payload=repr(payload)), self.assertRaises(ServiceError):
                self.control.start(payload)
        self.assertIsNone(self.control.thread)
        self.assertEqual(self.calls, [])
        self.assertEqual([p for p in self.control.folder.iterdir() if p.is_dir()], [])

    def test_runner_failure_is_redacted_and_never_automatically_retried(self):
        secret = "DO_NOT_PUBLISH_TEST_EXCEPTION"
        runner = Mock(side_effect=RuntimeError(secret))
        control = self.make_control(runner)
        first = control.start(self.request)
        control.thread.join(timeout=5)
        self.assertFalse(control.thread.is_alive())
        receipt_path = control.folder / first["id"] / "council-receipt.json"
        raw = receipt_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        self.assertEqual(json.loads(raw)["status"], "failed")
        self.assertTrue(control.start(dict(self.request))["duplicate_suppressed"])
        self.assertEqual(runner.call_count, 1)

    def test_thread_start_failure_leaves_reservation_to_prevent_replay(self):
        with patch("workbench.offline_control.threading.Thread.start",
                   side_effect=RuntimeError("Thread unavailable")):
            with self.assertRaises(RuntimeError):
                self.control.start(self.request)
        restarted_runner = Mock()
        restarted = self.make_control(restarted_runner)
        duplicate = restarted.start(dict(self.request))
        self.assertTrue(duplicate["duplicate_suppressed"])
        restarted_runner.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_same_key_concurrent_calls_have_one_durable_job_and_one_runner(self):
        barrier = threading.Barrier(3)
        replies, errors = [], []

        def submit():
            try:
                barrier.wait(timeout=5)
                replies.append(self.control.start(dict(self.request)))
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=submit) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(replies), 2)
        self.assertEqual(len({reply["id"] for reply in replies}), 1)
        self.assertEqual(sum(bool(reply["duplicate_suppressed"]) for reply in replies), 1)
        self.assertTrue(self.entered.wait(timeout=5))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len([p for p in self.control.folder.iterdir() if p.is_dir()]), 1)


if __name__ == "__main__":
    unittest.main()

