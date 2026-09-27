"""Finite gateway/ledger fixtures only: never launch an SDK or a model."""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from workbench import tandem
from workbench.autonomy import local_review
from workbench.autonomy.daemon import _job_lock


class TandemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.model_dir = self.root / "runtime" / "local-model"
        binary_dir = self.model_dir / "llama-b11146-vulkan"
        binary_dir.mkdir(parents=True)
        self.exe = binary_dir / "llama-cli.exe"
        self.dll = binary_dir / "fixture.dll"
        self.gguf = self.model_dir / (tandem.MODEL + ".gguf")
        self.exe.write_bytes(b"MZ-nonexecutable-fixture")
        self.dll.write_bytes(b"nonexecutable-fixture")
        self.gguf.write_bytes(b"GGUF\x03\x00\x00\x00tiny-fixture-not-a-model")
        self.pins = {"schema_version": 1, "runtime": "llama.cpp", "executable": self.pin(self.exe),
                     "model": self.pin(self.gguf), "dependencies": [self.pin(self.dll)]}
        self.manifest = self.model_dir / "manifest.json"
        self.manifest.write_text(json.dumps(self.pins), encoding="utf-8")
        self.data = self.root / "data"
        self.data.mkdir()
        self.calls = []
        self.tokens = []
        self.stage_calls = []

    def pin(self, path):
        return {"path": path.relative_to(self.root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def task_folder(self, task="fixture-1"):
        return self.data / "tandem" / hashlib.sha256(task.encode()).hexdigest()

    def model_runner(self, exe, model, prompt, **options):
        self.calls.append((exe, model, prompt, options))
        state = json.loads((self.task_folder() / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["reserved_calls"], len(self.calls))
        self.assertEqual(state["status"], "running")
        self.assertEqual(options["max_tokens"], 512)
        self.assertEqual(options["timeout"], 300)
        self.assertEqual(options["context_size"], 8192)
        return {"status": "unverified_proposal", "text": "Непроверенный ответ fixture " + str(len(self.calls)),
                "output_format": local_review.OUTPUT_FORMAT, "stdout_published": False}

    def request(self, base, token, *, body=None, method="POST", path="/v1/chat/completions", headers=None, raw=None):
        parsed = urlsplit(base)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        selected = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        selected.update(headers or {})
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        try:
            connection.request(method, path, payload, selected)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def body(self, prompt, **updates):
        value = {"model": tandem.MODEL, "messages": [{"role": "system", "content": "No tools fixture."},
                 {"role": "user", "content": prompt}], "max_tokens": 512}
        value.update(updates)
        return value

    def role_runner(self, stage, prompt, base, token, folder):
        self.stage_calls.append(stage)
        self.tokens.append(token)
        self.assertEqual(folder.name, stage)
        status, raw = self.request(base, token, method="GET", path="/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["data"][0]["id"], tandem.MODEL)
        status, raw = self.request(base, token, body=self.body(prompt))
        self.assertEqual(status, 200)
        return json.loads(raw)["choices"][0]["message"]["content"]

    def coordinator(self, runner=None, **updates):
        options = dict(role_runner=runner or self.role_runner, profile_ids={"openclaw": "fixture-openclaw-pin", "hermes": "fixture-hermes-pin"},
                       model_runner=self.model_runner)
        options.update(updates)
        return tandem.TandemCoordinator(self.root, self.data, **options)

    def run_cycle(self, coordinator=None, **updates):
        options = dict(task_id="fixture-1", question="Как проверить конечный алгоритм?", public_data_confirmed=True)
        options.update(updates)
        return (coordinator or self.coordinator()).run(**options)

    def test_three_real_gateway_requests_bound_to_harnesses_and_same_task_replay(self):
        coordinator = self.coordinator()
        receipt = self.run_cycle(coordinator)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(self.stage_calls, ["plan", "critique", "synthesis"])
        self.assertEqual(receipt["harnesses"], ["openclaw", "hermes", "openclaw"])
        self.assertEqual(receipt["reserved_calls"], 3)
        self.assertEqual(receipt["responses_received"], 3)
        self.assertEqual(receipt["accepted_stages"], list(tandem.STAGES))
        self.assertEqual(len(set(self.tokens)), 3)
        self.assertEqual(self.run_cycle(coordinator), receipt)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(receipt["independent_models"], 1)
        self.assertEqual(receipt["external_model_calls"], 0)
        self.assertFalse(receipt["scientific_validation"])
        self.assertFalse(receipt["tools_enabled"])
        self.assertFalse(receipt["code_execution_allowed"])
        for index, call in enumerate(self.calls):
            if index:
                self.assertIn("fixture " + str(index), call[2])
        for path in self.task_folder().rglob("*.json"):
            raw = path.read_text(encoding="utf-8")
            for token in self.tokens:
                self.assertNotIn(token, raw)
            self.assertNotIn(str(self.root), raw)

    def test_sse_returns_actual_answer_without_fabricated_usage(self):
        def runner(stage, prompt, base, token, folder):
            status, raw = self.request(base, token, body=self.body(prompt, stream=True, stream_options={"include_usage": True}))
            self.assertEqual(status, 200)
            self.assertTrue(raw.endswith(b"data: [DONE]\n\n"))
            text = ""
            for line in raw.decode().splitlines():
                if line.startswith("data: {"):
                    chunk = json.loads(line[6:])
                    self.assertNotIn("usage", chunk)
                    text += chunk["choices"][0]["delta"].get("content", "")
            return {"status": "completed", "text": text}
        self.assertEqual(self.run_cycle(self.coordinator(runner))["status"], "completed")
        self.assertEqual(len(self.calls), 3)

    def test_retries_cannot_make_a_second_model_call_per_stage(self):
        def runner(stage, prompt, base, token, folder):
            status, raw = self.request(base, token, body=self.body(prompt))
            self.assertEqual(status, 200)
            status2, _ = self.request(base, token, body=self.body(prompt))
            self.assertEqual(status2, 409)
            return json.loads(raw)["choices"][0]["message"]["content"]
        self.assertEqual(self.run_cycle(self.coordinator(runner))["status"], "completed")
        self.assertEqual(len(self.calls), 3)

    def test_auth_origin_host_paths_and_oversize_refused_before_reservation(self):
        statuses = []
        def runner(stage, prompt, base, token, folder):
            cases = [dict(token="wrong"), dict(headers={"Origin": "https://example.invalid"}),
                     dict(headers={"Host": "example.invalid"}), dict(path="/v1/chat/completions?x=1"),
                     dict(path="/v1/responses"), dict(headers={"Content-Length": str(tandem.MAX_BODY + 1)}, raw=b"{}")]
            for changes in cases:
                options = dict(token=token, body=self.body(prompt))
                options.update(changes)
                status, _ = self.request(base, **options)
                statuses.append(status)
            return "There was no actual answer"
        receipt = self.run_cycle(self.coordinator(runner))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["reserved_calls"], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(statuses, [401, 403, 403, 404, 404, 413])

    def test_no_tools_unknown_roles_binding_model_and_duplicate_json_fail_closed(self):
        statuses = []
        def runner(stage, prompt, base, token, folder):
            bodies = [self.body(prompt, tools=[{"type": "function", "function": {"name": "shell"}}]),
                self.body(prompt, tool_choice="auto"), self.body(prompt, parallel_tool_calls=True),
                self.body(prompt, model="remote-fallback"), self.body(prompt, max_tokens=513),
                self.body(prompt, n=2), self.body(prompt, stream=1), self.body(prompt, provider={"api_key": "secret"}),
                self.body("Unrelated user question"),
                self.body(prompt, messages=[{"role": "tool", "content": prompt}]),
                self.body(prompt, messages=[{"role": "user", "content": prompt, "tool_calls": []}])]
            for body in bodies:
                statuses.append(self.request(base, token, body=body)[0])
            raw = b'{"model":"wrong","model":"' + tandem.MODEL.encode() + b'","messages":[]}'
            statuses.append(self.request(base, token, raw=raw)[0])
            return "not a response"
        receipt = self.run_cycle(self.coordinator(runner))
        self.assertEqual(receipt["reserved_calls"], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(statuses, [400] * 12)

    def test_harness_stdout_echo_never_counts_as_model_response(self):
        receipt = self.run_cycle(self.coordinator(lambda stage, prompt, base, token, folder: prompt))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["responses_received"], 0)
        self.assertEqual(receipt["accepted_stages"], [])
        self.assertEqual(self.calls, [])

    def test_harness_final_mismatch_keeps_response_but_stops_next_stage(self):
        def runner(stage, prompt, base, token, folder):
            self.role_runner(stage, prompt, base, token, folder)
            return "An unrelated fabricated final"
        coordinator = self.coordinator(runner)
        receipt = self.run_cycle(coordinator)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["reserved_calls"], 1)
        self.assertEqual(receipt["responses_received"], 1)
        self.assertEqual(receipt["accepted_stages"], [])
        self.assertEqual(self.run_cycle(coordinator), receipt)
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_after_served_response_never_resends_on_new_coordinator(self):
        def interrupted(stage, prompt, base, token, folder):
            self.role_runner(stage, prompt, base, token, folder)
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_cycle(self.coordinator(interrupted))
        state = json.loads((self.task_folder() / "state.json").read_text())
        self.assertEqual(state["reserved_calls"], 1)
        self.assertEqual(state["accepted_stages"], [])
        receipt = self.run_cycle()
        self.assertEqual(receipt["status"], "interrupted")
        self.assertEqual(receipt["responses_received"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_pending_task_without_calls_is_not_restarted(self):
        def interrupted(*args):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_cycle(self.coordinator(interrupted))
        receipt = self.run_cycle()
        self.assertEqual(receipt["status"], "interrupted")
        self.assertEqual(receipt["reserved_calls"], 0)
        self.assertEqual(self.calls, [])

    def test_model_failure_and_secret_bearing_exception_are_not_published_or_retried(self):
        def failed(*args, **kwargs):
            raise RuntimeError("Authorization Bearer fake-sensitive-secret")
        coordinator = self.coordinator(model_runner=failed)
        receipt = self.run_cycle(coordinator)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["reserved_calls"], 1)
        self.assertEqual(receipt["responses_received"], 0)
        self.assertEqual(self.run_cycle(coordinator), receipt)
        for path in self.task_folder().rglob("*.json"):
            self.assertNotIn("fake-sensitive-secret", path.read_text(encoding="utf-8"))

    def test_changed_question_or_profile_cannot_reuse_task_identity(self):
        self.run_cycle()
        with self.assertRaises(ValueError):
            self.run_cycle(question="Другой вопрос")
        other = self.coordinator(profile_ids={"openclaw": "different", "hermes": "fixture-hermes-pin"})
        with self.assertRaises(ValueError):
            self.run_cycle(other)
        self.assertEqual(len(self.calls), 3)

    def test_tampered_receipt_or_response_is_not_reset(self):
        self.run_cycle()
        response = self.task_folder() / "plan-response.json"
        original = response.read_bytes()
        value = json.loads(original)
        value["text"] = "Tampered"
        response.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_cycle()
        response.write_bytes(original)
        receipt_path = self.task_folder() / "receipt.json"
        saved = json.loads(receipt_path.read_text(encoding="utf-8"))
        saved["reserved_calls"] = 0
        receipt_path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_cycle()
        self.assertEqual(len(self.calls), 3)

    def test_shared_offline_council_lock_prevents_tandem_creation(self):
        coordinator = self.coordinator()
        with _job_lock(self.data / "offline-council" / "model.lock"):
            with self.assertRaises(ValueError):
                self.run_cycle(coordinator)
        self.assertFalse(self.task_folder().exists())
        self.assertEqual(self.calls, [])

    def test_pins_verified_before_first_call_and_stamps_checked_before_later_calls(self):
        def runner(stage, prompt, base, token, folder):
            if stage == "critique":
                self.dll.write_bytes(b"changed-runtime-binary")
            return self.role_runner(stage, prompt, base, token, folder)
        receipt = self.run_cycle(self.coordinator(runner))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["reserved_calls"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_concurrent_duplicate_requests_admit_only_one(self):
        def runner(stage, prompt, base, token, folder):
            outcomes = []
            start = threading.Barrier(3)
            def request():
                start.wait(timeout=5)
                outcomes.append(self.request(base, token, body=self.body(prompt)))
            threads = [threading.Thread(target=request) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            self.assertEqual(sorted(status for status, raw in outcomes), [200, 409])
            raw = next(raw for status, raw in outcomes if status == 200)
            return json.loads(raw)["choices"][0]["message"]["content"]
        self.assertEqual(self.run_cycle(self.coordinator(runner))["status"], "completed")
        self.assertEqual(len(self.calls), 3)

    def test_public_confirmation_and_pathlike_identity_rejected(self):
        for options in ({"public_data_confirmed": False}, {"public_data_confirmed": 1}, {"task_id": "../other"}):
            with self.assertRaises(ValueError):
                self.run_cycle(**options)
        self.assertEqual(self.calls, [])

    def test_invalid_context_rejected_before_process_creation(self):
        for context in (0, True, "8192", 65536):
            result = local_review._run_local(self.exe, self.gguf, "fixture", max_tokens=1, timeout=1,
                                             stop_file=None, context_size=context)
            self.assertEqual(result, {"status": "failed"})

    def test_explicit_text_blocks_work_but_images_and_tool_blocks_do_not(self):
        def runner(stage, prompt, base, token, folder):
            for block in ({"type": "image_url", "image_url": "https://example.invalid/private"},
                          {"type": "tool_result", "text": prompt}):
                body = self.body(prompt, messages=[{"role": "user", "content": [block]}])
                self.assertEqual(self.request(base, token, body=body)[0], 400)
            body = self.body(prompt, messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}])
            status, raw = self.request(base, token, body=body)
            self.assertEqual(status, 200)
            return json.loads(raw)["choices"][0]["message"]["content"]
        self.assertEqual(self.run_cycle(self.coordinator(runner))["status"], "completed")
        self.assertEqual(len(self.calls), 3)

    def test_task_stop_after_last_answer_prevents_completed_status(self):
        def runner(stage, prompt, base, token, folder):
            answer = self.role_runner(stage, prompt, base, token, folder)
            if stage == "synthesis":
                (folder.parent / "STOP").write_bytes(b"")
            return answer
        receipt = self.run_cycle(self.coordinator(runner))
        self.assertEqual(receipt["status"], "interrupted")
        self.assertEqual(receipt["failure_code"], "operator_stop")
        self.assertEqual(receipt["failure_phase"], "synthesis")
        self.assertEqual(receipt["responses_received"], 3)
        self.assertEqual(receipt["accepted_stages"], ["plan", "critique"])
        self.assertEqual(self.run_cycle(), receipt)
        self.assertEqual(len(self.calls), 3)

    def test_stage_context_has_explicit_truncation_and_question_is_unchanged(self):
        question = "Как воспроизвести алгоритм? " * 40
        previous = [{"stage": stage, "output_sha256": "a" * 64, "text": "Ответ " * 2000}
                    for stage in ("plan", "critique")]
        prompt = tandem._stage_prompt(question, "synthesis", previous)
        self.assertLessEqual(len(prompt), tandem.MAX_STAGE_CHARS)
        self.assertIn(question, prompt)
        self.assertIn('"truncated":true', prompt)
        with self.assertRaises(ValueError):
            self.run_cycle(question="x" * 3001)
        self.assertFalse(self.task_folder().exists())

    def test_gateway_diagnostics_distinguish_refusals_without_recording_private_values(self):
        statuses, observed_tokens = [], []
        private_name = "privateCredentialFieldDoNotPublish"
        private_value = "privateValueDoNotPublish"
        def runner(stage, prompt, base, token, folder):
            observed_tokens.append(token)
            for body in (self.body(prompt, tools=None), self.body(prompt, tool_choice=None),
                         self.body(prompt, **{private_name: private_value}),
                         self.body(prompt, model=private_value)):
                statuses.append(self.request(base, token, body=body)[0])
            statuses.append(self.request(base, token, raw=b'{"model":')[0])
            return "No model answer was served"
        receipt = self.run_cycle(self.coordinator(runner))
        path = self.task_folder() / "plan" / "gateway-diagnostic.json"
        raw = path.read_text(encoding="utf-8")
        diagnostic = json.loads(raw)
        self.assertEqual(statuses, [400] * 5)
        self.assertEqual(receipt["reserved_calls"], 0)
        self.assertEqual(diagnostic["http_response_counts"], {"400": 5})
        self.assertEqual(diagnostic["validation_code_counts"], {"tools_not_empty_array": 1,
            "tool_choice_not_none": 1, "unsupported_fields": 1, "unsupported_model": 1, "invalid_request": 1})
        self.assertEqual(diagnostic["unknown_api_fields_omitted"], 1)
        self.assertEqual(diagnostic["response_attempts"], 5)
        self.assertFalse(diagnostic["values_recorded"])
        self.assertEqual(set(diagnostic["api_field_names"]), {"model", "messages", "max_tokens", "tools", "tool_choice"})
        for secret in [private_name, private_value, *observed_tokens, "Как проверить"]:
            self.assertNotIn(secret, raw)
        self.assertLessEqual(path.stat().st_size, 4096)

    def test_gateway_diagnostics_exist_without_sdk_request_and_saturate_unknown_key_count(self):
        observations = []
        def runner(stage, prompt, base, token, folder):
            path = folder / "gateway-diagnostic.json"
            observations.append(json.loads(path.read_text(encoding="utf-8")))
            body = self.body(prompt, **{"private-key-" + str(n): "hidden" for n in range(650)})
            for _ in range(2):
                observations.append(self.request(base, token, body=body)[0])
            return "No response"
        self.run_cycle(self.coordinator(runner))
        initial = observations[0]
        self.assertEqual(initial["response_attempts"], 0)
        self.assertEqual(initial["http_response_counts"], {})
        self.assertEqual(observations[1:], [400, 400])
        path = self.task_folder() / "plan" / "gateway-diagnostic.json"
        raw = path.read_text(encoding="utf-8")
        final = json.loads(raw)
        self.assertEqual(final["unknown_api_fields_omitted"], 1000)
        self.assertTrue(final["counters_saturated"])
        self.assertEqual(final["http_response_counts"], {"400": 2})
        self.assertEqual(final["api_field_names"], ["max_tokens", "messages", "model"])
        self.assertNotIn("private-key", raw)
        self.assertNotIn("hidden", raw)
        self.assertLessEqual(path.stat().st_size, 4096)


if __name__ == "__main__":
    unittest.main()
