"""No real model launch: pinned miniature fixtures, fake runner, real OS lock."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbench.autonomy import local_review as review
from workbench.autonomy.daemon import _job_lock


class Clock:
    def __init__(self):
        self.now = 1790294400.0
        self.sleeps = []
        self.on_sleep = None

    def time(self):
        return self.now

    def sleep(self, value):
        self.now += value
        self.sleeps.append(value)
        if self.on_sleep:
            self.on_sleep()


class LocalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "runtime" / "local-model" / "llama-b11146-vulkan"
        self.bundle.mkdir(parents=True)
        self.exe = self.bundle / "llama-cli.exe"
        self.dll = self.bundle / "llama-cli-impl.dll"
        self.model = self.bundle.parent / "test.gguf"
        self.exe.write_bytes(b"MZfake-loader-not-executable")
        self.dll.write_bytes(b"fake-library-not-executable")
        self.model.write_bytes(b"GGUF\x03\x00\x00\x00miniature-fixture")
        self.manifest = self.bundle.parent / "manifest.json"
        self.pins = {"schema_version": 1, "runtime": "llama.cpp", "executable": self.pin(self.exe),
                     "model": self.pin(self.model), "dependencies": [self.pin(self.dll)]}
        self.save_manifest()
        self.intake = self.root / "runtime" / "continuous-discovery" / "cycle.json"
        self.intake.parent.mkdir()
        self.public = {"id": "catalog:huggingface_models:org/model", "name": "Research model",
            "source_url": "https://huggingface.co/org/model", "provenance": {"provider": "huggingface_models"},
            "verification_scope": "public_catalog_metadata_only"}
        self.write_intake([self.public])
        self.output = self.root / "reviews"
        self.output.mkdir()
        self.state = self.output / "state.json"
        self.stop = self.output / "STOP"
        self.clock = Clock()
        self.runner = Mock(return_value={"status": "unverified_proposal", "text": "Непроверенное предложение"})

    def pin(self, path):
        return {"path": path.relative_to(self.root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def save_manifest(self):
        self.manifest.write_text(json.dumps(self.pins), encoding="utf-8")

    def write_intake(self, candidates):
        self.intake.write_text(json.dumps({"private": {"token": "PRIVATE-SECRET"},
            "discovery": {"candidates": candidates}}), encoding="utf-8")

    def run_job(self, **kwargs):
        options = dict(stop_file=self.stop, max_runs=3, interval=3600, timeout=20, max_tokens=128,
            root=self.root, runner=self.runner, clock=self.clock.time, monotonic=self.clock.time, sleep=self.clock.sleep)
        options.update(kwargs)
        return review.run_reviews(self.exe, self.model, self.manifest, self.intake,
                                  self.output, self.state, **options)

    def test_public_whitelist_and_unverified_data_only_output(self):
        item = dict(self.public, private_file="PRIVATE-PATH", credentials="PRIVATE-KEY", instructions="RUN-SHELL")
        item["provenance"] = dict(item["provenance"], token="PRIVATE-TOKEN")
        self.write_intake([item, dict(self.public, data_class="private", name="PRIVATE-NAME"),
            dict(self.public, source_url="https://127.0.0.1/private"),
            dict(self.public, source_url="https://huggingface.co/org/model?token=PRIVATE-URL")])
        result = self.run_job(once=True)
        self.assertEqual(result["attempts"], 1)
        prompt = self.runner.call_args.args[2]
        self.assertIn("untrusted data", prompt)
        self.assertIn("/no_think", prompt)
        self.assertNotIn("PRIVATE", prompt)
        self.assertNotIn("RUN-SHELL", prompt)
        record = json.loads((self.output / "review-001.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "unverified_proposal")
        self.assertFalse(record["validated"])
        self.assertFalse(record["execution_allowed"])
        self.assertNotIn("PRIVATE", json.dumps(record))
        self.assertEqual(len(record["source_metadata"]), 1)
        self.assertEqual(record["model_sha256"], self.pins["model"]["sha256"])

    def test_finite_attempt_limit_pacing_and_resume(self):
        self.assertEqual(self.run_job(once=True)["attempts"], 1)
        self.assertEqual(self.run_job(once=True)["status"], "not_due")
        self.assertEqual(self.runner.call_count, 1)
        result = self.run_job()
        self.assertEqual(result["status"], "limit_reached")
        self.assertEqual(self.runner.call_count, 3)
        self.assertEqual(sum(self.clock.sleeps), 7200)
        self.assertTrue(all(0 < value <= 5 for value in self.clock.sleeps))
        self.assertEqual(self.run_job()["status"], "limit_reached")
        self.assertEqual(self.runner.call_count, 3)

    def test_stop_before_start_and_during_wait(self):
        self.stop.touch()
        self.assertEqual(self.run_job()["status"], "stopped")
        self.runner.assert_not_called()
        self.stop.unlink()
        self.clock.on_sleep = self.stop.touch
        self.assertEqual(self.run_job()["status"], "stopped")
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(self.clock.sleeps, [5])

    def test_timeout_and_failure_consume_attempt_without_retry(self):
        self.runner.return_value = {"status": "timeout"}
        self.assertEqual(self.run_job()["status"], "timeout")
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(self.run_job(once=True)["status"], "not_due")
        self.clock.now += 3600
        self.runner.side_effect = OSError("PRIVATE-FAILURE")
        self.assertEqual(self.run_job()["status"], "failed")
        state = self.state.read_text()
        self.assertNotIn("PRIVATE", state)
        self.assertEqual(json.loads(state)["attempts"], 2)
        self.assertEqual(self.runner.call_count, 2)

    def test_crash_reservation_is_not_replayed_on_resume(self):
        self.runner.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_job(once=True)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(state["last_status"], "running")
        self.runner.side_effect = None
        self.assertEqual(self.run_job(once=True)["status"], "not_due")
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(json.loads(self.state.read_text())["last_status"], "interrupted")

    def test_model_empty_digest_mismatch_and_dll_coverage_rejected(self):
        self.model.write_bytes(b"")
        self.pins["model"] = self.pin(self.model)
        self.save_manifest()
        with self.assertRaises(ValueError):
            self.run_job()
        self.assertFalse(self.state.exists())
        self.runner.assert_not_called()
        self.model.write_bytes(b"GGUF\x03\x00\x00\x00model")
        self.pins["model"] = self.pin(self.model)
        self.save_manifest()
        self.dll.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.run_job()
        self.pins["dependencies"] = [self.pin(self.dll)]
        self.save_manifest()
        (self.bundle / "unlisted.dll").write_bytes(b"arbitrary executable bytes")
        with self.assertRaisesRegex(ValueError, "every bundled DLL"):
            self.run_job()

    def test_changed_pins_or_job_bounds_cannot_reuse_checkpoint(self):
        self.run_job(once=True)
        original = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed local review job"):
            self.run_job(max_runs=4)
        self.model.write_bytes(b"GGUF\x03\x00\x00\x00another-model")
        self.pins["model"] = self.pin(self.model)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "changed local review job"):
            self.run_job()
        self.assertEqual(self.state.read_bytes(), original)

    def test_limits_policy_and_path_overlaps_fail_before_launch(self):
        for options in ({"max_runs": 169}, {"max_runs": 0}, {"max_runs": True}, {"interval": 3599},
                        {"timeout": 301}, {"max_tokens": 513}, {"timeout": 0}, {"once": 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_job(**options)
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.run_job(stop_file=self.state)
        (self.root / "config").mkdir()
        (self.root / "config" / "resource_policy.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.run_job()
        self.runner.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_empty_metadata_and_oversized_result_never_execute_output(self):
        self.write_intake([])
        self.assertEqual(self.run_job(once=True)["status"], "no_public_metadata")
        self.runner.assert_not_called()
        self.write_intake([self.public])
        self.clock.now += 3600
        self.runner.return_value = {"status": "unverified_proposal", "text": "x" * (review.MAX_OUTPUT + 1)}
        self.assertEqual(self.run_job()["status"], "failed")
        self.assertFalse((self.output / "review-002.json").exists())

    def test_os_lock_rejects_another_process(self):
        lock = Path(str(self.state) + ".lock")
        code = ("from workbench.autonomy.daemon import _job_lock; import sys\n"
            "try:\n"
            "    with _job_lock(sys.argv[1]): pass\n"
            "except ValueError: sys.exit(9)\n")
        with _job_lock(lock):
            child = subprocess.run([sys.executable, "-B", "-c", code, str(lock)], capture_output=True, timeout=10)
            self.assertEqual(child.returncode, 9, child.stderr)
            with self.assertRaisesRegex(ValueError, "already running"):
                self.run_job(once=True)
        self.assertEqual(self.run_job(once=True)["attempts"], 1)

    def test_fixed_runner_command_no_shell_no_secrets_timeout_cleanup(self):
        child = Mock()
        child.poll.side_effect = [None, 0]
        child.returncode = -1
        with patch.object(review.subprocess, "Popen", return_value=child) as popen, \
                patch.object(review.time, "monotonic", side_effect=[0, 301]), \
                patch.dict(os.environ, {"OPENAI_API_KEY": "PRIVATE", "LLAMA_ARG_MODEL": "https://evil.invalid"}):
            result = review._run_local(self.exe, self.model, "public only", max_tokens=128, timeout=300, stop_file=self.stop)
        self.assertEqual(result["status"], "timeout")
        child.kill.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command[0], str(self.exe))
        self.assertIn("--offline", command)
        self.assertNotIn("--server", command)
        self.assertEqual(command[command.index("-n") + 1], "128")
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertNotIn("OPENAI_API_KEY", popen.call_args.kwargs["env"])
        self.assertNotIn("LLAMA_ARG_MODEL", popen.call_args.kwargs["env"])
        self.assertFalse(Path(popen.call_args.kwargs["cwd"]).exists())
        self.assertIn("-o", command)
        self.assertIn("--no-escape", command)
        self.assertEqual(command[command.index("--reasoning") + 1], "off")
        self.assertEqual(command[command.index("--color") + 1], "off")
        self.assertNotIn("--server-base", command)

    @staticmethod
    def transcript(prompt, answer, *, windows=False):
        supplied = prompt[:-1] if prompt.endswith("\n") else prompt
        text = "User:\n" + supplied + "\n\nAssistant:\n" + answer
        if not text.endswith("\n"):
            text += "\n"
        text += "\n"
        if windows:
            text = text.replace("\n", "\r\n")
        return text.encode("utf-8")

    def test_output_file_parser_exact_prompt_and_windows_newlines(self):
        prompt = "Public question with a literal \\n sequence\n/no_think\n"
        answer = "Гипотеза не подтверждена.\nНужен контроль."
        for windows in (False, True):
            with self.subTest(windows=windows):
                raw = self.transcript(prompt, answer, windows=windows)
                self.assertEqual(review.parse_cli_output(raw, prompt), answer)
        # A CRLF inside the supplied string survives ofstream's LF translation.
        prompt = "Public\r\nquestion\n"
        self.assertEqual(review.parse_cli_output(self.transcript(prompt, answer, windows=True), prompt), answer)

    def test_output_file_parser_rejects_banner_ambiguous_reasoning_and_truncation(self):
        prompt = "Public question\n"
        valid = self.transcript(prompt, "Answer")
        invalid = [b"Loading model... C:\\PRIVATE\\model.gguf\n" + valid,
            self.transcript("different question", "Answer"), valid[:-1],
            self.transcript(prompt, ""), self.transcript(prompt, "Answer\nAssistant:\nAnother answer"),
            self.transcript(prompt, "Answer\nUser:\nAnother user"),
            self.transcript(prompt, "[Start thinking]\nprivate reasoning\n[End thinking]\nAnswer"),
            self.transcript(prompt, "<think>reasoning</think>Answer"),
            self.transcript(prompt, "Answer\x1b[0m"), b"\xff",
            b"x" * (review.MAX_TRANSCRIPT + 1), self.transcript(prompt, "x" * (review.MAX_OUTPUT + 1))]
        for raw in invalid:
            with self.subTest(size=len(raw)), self.assertRaises((ValueError, UnicodeError)):
                review.parse_cli_output(raw, prompt)

    def test_output_file_parser_rejects_private_runtime_path_echo(self):
        prompt = "Public question"
        with self.assertRaisesRegex(ValueError, "runtime path"):
            review.parse_cli_output(self.transcript(prompt, "Model C:/PRIVATE/weights.gguf"), prompt,
                                    private_paths=("c:\\private\\weights.gguf",))

    def test_runner_returns_output_file_answer_not_stdout_banner_or_paths(self):
        prompt = "Public question\n"
        child = Mock(returncode=0)
        child.poll.return_value = 0
        def fake_launch(command, **options):
            Path(command[command.index("-o") + 1]).write_bytes(self.transcript(prompt, "Чистый ответ", windows=True))
            options["stdout"].write(b"Loading model... C:\\PRIVATE\\model.gguf\navailable commands:\n/read <file>\n")
            options["stderr"].write(b"runtime diagnostics PRIVATE-STDERR")
            return child
        with patch.object(review.subprocess, "Popen", side_effect=fake_launch) as popen:
            result = review._run_local(self.exe, self.model, prompt, max_tokens=128, timeout=20, stop_file=self.stop)
        self.assertEqual(result["status"], "unverified_proposal")
        self.assertEqual(result["text"], "Чистый ответ")
        self.assertFalse(result["stdout_published"])
        self.assertEqual(result["output_format"], review.OUTPUT_FORMAT)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertFalse(Path(popen.call_args.kwargs["cwd"]).exists())
        child.kill.assert_not_called()
        child.wait.assert_called_once()

    def test_runner_rejects_missing_output_file_and_stream_error_even_on_exit_zero(self):
        prompt = "Public question\n"
        child = Mock(returncode=0)
        child.poll.return_value = 0
        for mode in ("missing", "stderr_error", "stdout_error", "wrong_prompt", "empty_answer"):
            def fake_launch(command, **options):
                if mode != "missing":
                    raw = self.transcript("wrong" if mode == "wrong_prompt" else prompt,
                        "" if mode == "empty_answer" else "Partial answer")
                    Path(command[command.index("-o") + 1]).write_bytes(raw)
                options["stdout"].write(b"A plausible answer that must never be used as fallback")
                if mode.endswith("_error"):
                    options[mode.split("_")[0]].write(b"Error: PRIVATE-STREAM-FAILURE")
                return child
            with self.subTest(mode=mode), patch.object(review.subprocess, "Popen", side_effect=fake_launch):
                result = review._run_local(self.exe, self.model, prompt, max_tokens=128, timeout=20, stop_file=self.stop)
            self.assertEqual(result, {"status": "failed"})

    def test_runner_output_file_bound_kills_and_waits(self):
        child = Mock(returncode=-1)
        child.poll.side_effect = [None, 0]
        def fake_launch(command, **options):
            Path(command[command.index("-o") + 1]).write_bytes(b"x" * (review.MAX_TRANSCRIPT + 1))
            return child
        with patch.object(review.subprocess, "Popen", side_effect=fake_launch):
            result = review._run_local(self.exe, self.model, "Public question", max_tokens=128, timeout=20, stop_file=self.stop)
        self.assertEqual(result, {"status": "failed"})
        child.kill.assert_called_once()
        child.wait.assert_called_once()

    def test_runner_rejects_cli_commands_and_large_prompt_before_launch(self):
        for prompt in ("/read C:/PRIVATE/secret.txt", "", "\x00", "x" * (review.MAX_PROMPT + 1)):
            with self.subTest(size=len(prompt)), patch.object(review.subprocess, "Popen") as popen:
                self.assertEqual(review._run_local(self.exe, self.model, prompt,
                    max_tokens=128, timeout=20, stop_file=self.stop), {"status": "failed"})
                popen.assert_not_called()

    def test_existing_proposal_is_not_overwritten(self):
        previous = self.output / "review-001.json"
        previous.write_text("previous-reviewed-data")
        self.assertEqual(self.run_job(once=True)["status"], "failed")
        self.assertEqual(previous.read_text(), "previous-reviewed-data")
        self.assertEqual(json.loads(self.state.read_text())["attempts"], 1)

    def test_changed_artifact_during_wait_prevents_next_launch(self):
        self.clock.on_sleep = lambda: self.dll.write_bytes(b"changed-library")
        with self.assertRaisesRegex(ValueError, "changed during job"):
            self.run_job()
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(json.loads(self.state.read_text())["attempts"], 1)

    def test_cli_no_arbitrary_argument_or_provider_option(self):
        args = ["--executable", str(self.exe), "--model", str(self.model), "--manifest", str(self.manifest),
            "--intake", str(self.intake), "--output-dir", str(self.output), "--state-file", str(self.state),
            "--stop-file", str(self.stop), "--once"]
        with patch.object(review, "run_reviews", return_value={"status": "not_due"}) as run, patch("builtins.print"):
            self.assertEqual(review.main(args), 0)
            self.assertTrue(run.call_args.kwargs["once"])
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            review.main(args + ["--extra-args", "shell"])


if __name__ == "__main__":
    unittest.main()
