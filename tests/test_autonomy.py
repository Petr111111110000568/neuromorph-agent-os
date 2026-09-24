"""Autonomy integration tests use fake inference and public-directory transports."""
import copy
import io
import json
from email.message import Message
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from workbench.autonomy import engine, providers
from workbench.autonomy.__main__ import main


def config():
    return {"schema_version": 1,
            "project": {"name": "Meta-Harness", "repository_url": "https://github.com/example/project",
                        "public_context": "Public stdlib software research platform with synthetic benchmarks."},
            "discovery": {"query": "research", "providers": list(engine.discovery.PROVIDERS), "limit_per_provider": 2},
            "development": {"goals": ["Document reproducible candidate review", "Propose a benchmark control"], "max_tasks": 2},
            "provider": "auto", "allow_model_calls": False,
            "providers": {"openai": {"model": "test-openai", "max_output_tokens": 3000},
                          "anthropic": {"model": "test-anthropic", "max_output_tokens": 3000}}}


def proposal():
    return {"title": "Add a reproducibility checklist", "summary": "New documentation candidate, not independently tested.",
            "tasks": [{"title": "Explain review", "objective": "Document source and test provenance",
                       "evidence_ids": ["project-context"], "acceptance_criteria": ["Maintainer verifies each claimed control"],
                       "file_paths": ["docs/contributions/review-checklist.md"]}],
            "files": [{"path": "docs/contributions/review-checklist.md", "content": "# Review\n\nCheck source, assumptions and tests.\n"}]}


class Response(io.BytesIO):
    def __init__(self, payload, provider="openai", status=200, content_type="application/json", url=None, encoding=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        super().__init__(raw)
        self.status, self.url = status, url or providers.ENDPOINTS[provider]
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def geturl(self):
        return self.url


def model_response(provider="openai", candidate=None):
    text = json.dumps(candidate or proposal())
    if provider == "openai":
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}


class ProposalTests(unittest.TestCase):
    def test_new_data_only_candidate_and_evidence_are_valid(self):
        with tempfile.TemporaryDirectory() as td:
            result = engine.validate_proposal(proposal(), {"project-context"}, root=td)
            self.assertEqual(result["files"][0]["path"], "docs/contributions/review-checklist.md")
            self.assertFalse((Path(td) / "docs").exists())

    def test_paths_reject_traversal_absolute_protected_and_unsupported(self):
        values = ["/tmp/x.py", "../docs/contributions/x.md", "docs/contributions/../x.md",
                  "docs/contributions//x.md", "docs/contributions/.env", "docs/contributions/AGENTS.md",
                  "docs/contributions/config/x.json", "docs/contributions/.github/x.md",
                  "tests/test_core.py", "workbench/experiments/x.sh", "docs/contributions/a\\b.md",
                  "docs/contributions/naïve.md", "docs/contributions/x.md/", "docs/contributions/x:stream.md",
                  "workbench/experiments/__init__.py", "tests/proposals/__pycache__/x.py",
                  "docs/contributions/Claude.md", "docs/contributions/GEMINI.md", "docs/contributions/CODEX.md"]
        with tempfile.TemporaryDirectory() as td:
            for path in values:
                value = proposal()
                value["files"][0]["path"] = path
                value["tasks"][0]["file_paths"] = [path]
                with self.subTest(path=path), self.assertRaises(ValueError):
                    engine.validate_proposal(value, {"project-context"}, root=td)

    def test_symlinks_and_existing_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "docs").mkdir()
            (root / "elsewhere").mkdir()
            (root / "docs/contributions").symlink_to(root / "elsewhere", target_is_directory=True)
            with self.assertRaises(ValueError):
                engine.validate_proposal(proposal(), {"project-context"}, root=root)
            (root / "docs/contributions").unlink()
            (root / "docs/contributions").mkdir()
            (root / "docs/contributions/review-checklist.md").write_text("Existing")
            with self.assertRaises(ValueError):
                engine.validate_proposal(proposal(), {"project-context"}, root=root)

    def test_unknown_evidence_and_unassigned_files_are_rejected(self):
        first, second = proposal(), proposal()
        first["tasks"][0]["evidence_ids"] = ["fabricated-citation"]
        second["files"].append({"path": "docs/contributions/unused.md", "content": "unused"})
        for value in [first, second]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine.validate_proposal(value, {"project-context"})

    def test_utf8_limits_duplicates_unknown_fields_and_secrets(self):
        for change in [lambda x: x["files"][0].update(content="я" * 6001),
                       lambda x: x["files"].append(copy.deepcopy(x["files"][0])),
                       lambda x: x.update(shell="echo execute"),
                       lambda x: x["files"][0].update(content="sk-proj-" + "a" * 50),
                       lambda x: x["files"][0].update(content="-----BEGIN PRIVATE KEY-----")]:
            value = proposal()
            change(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine.validate_proposal(value, {"project-context"})

    def test_malformed_json_candidate_and_credential_echo_rejected(self):
        value = proposal()
        value["files"][0].update(path="workbench/experiments/example.json", content='{"x": NaN}')
        value["tasks"][0]["file_paths"] = [value["files"][0]["path"]]
        with self.assertRaises(ValueError):
            engine.validate_proposal(value, {"project-context"})
        value = proposal()
        value["files"][0]["content"] = "private-test-token"
        with self.assertRaises(ValueError):
            engine.validate_proposal(value, {"project-context"}, known_tokens=("private-test-token",))


class ProviderTests(unittest.TestCase):
    def test_openai_endpoint_request_and_no_storage_or_tools(self):
        with mock.patch.object(providers, "build_opener") as opener:
            opener.return_value.open.return_value = Response(model_response())
            result = providers.call_model("openai", config()["providers"]["openai"], "Return JSON", {"public": True}, "test-token")
        self.assertEqual(result["status"], "response_received")
        self.assertEqual(result["proposal"], proposal())
        request = opener.return_value.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, providers.ENDPOINTS["openai"])
        self.assertEqual(request.get_method(), "POST")
        self.assertFalse(body["store"])
        self.assertNotIn("tools", body)
        self.assertNotIn("test-token", request.data.decode())
        self.assertEqual(body["max_output_tokens"], 3000)
        opener.return_value.open.assert_called_once()
        self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 45)

    def test_anthropic_endpoint_contract(self):
        with mock.patch.object(providers, "build_opener") as opener:
            opener.return_value.open.return_value = Response(model_response("anthropic"), provider="anthropic")
            result = providers.call_model("anthropic", config()["providers"]["anthropic"], "Return JSON", {}, "test-token")
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, providers.ENDPOINTS["anthropic"])
        self.assertEqual(json.loads(request.data)["max_tokens"], 3000)
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(result["status"], "response_received")

    def test_access_denied_rate_limited_and_transport_never_echo_errors(self):
        cases = [(HTTPError("https://example.org", 403, "private-test-token", {}, None), "access_denied"),
                 (HTTPError("https://example.org", 429, "private-test-token", {}, None), "rate_limited"),
                 (URLError("private-test-token"), "transport_unavailable")]
        for error, status in cases:
            with self.subTest(status=status), mock.patch.object(providers, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                result = providers.call_model("openai", config()["providers"]["openai"], "JSON", {}, "private-test-token")
                self.assertEqual(result["status"], status)
                self.assertEqual(result["requests"], 1)
                self.assertNotIn("private-test-token", json.dumps(result))
                opener.return_value.open.assert_called_once()

    def test_redirect_type_compression_size_incomplete_invalid_json(self):
        cases = [Response(model_response(), status=302), Response(model_response(), url="https://other.example/"),
                 Response(model_response(), content_type="text/html"), Response(model_response(), encoding="gzip"),
                 Response(b"x" * (providers.MAX_RESPONSE_BYTES + 1)),
                 Response({"status": "incomplete", "output": []}),
                 Response({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "```json\n{}\n```"}]}]})]
        for response in cases:
            with self.subTest(response=response), mock.patch.object(providers, "build_opener") as opener:
                opener.return_value.open.return_value = response
                result = providers.call_model("openai", config()["providers"]["openai"], "JSON", {}, "test-token")
                self.assertIn(result["status"], {"invalid_response", "incomplete_response"})
                self.assertNotIn("proposal", result)

    def test_json_duplicate_deep_nonfinite_and_unicode_rejected(self):
        for value in [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'{"x":"\\ud800"}',
                      ("[" * 26 + "1" + "]" * 26).encode()]:
            with self.subTest(value=value), self.assertRaises((ValueError, UnicodeError)):
                providers.json_load(value)


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "data/agents_test.json").write_text(json.dumps([{
            "id": "candidate-1", "name": "Research metadata", "source_url": "https://example.org/research",
            "capabilities": ["research"]}]))
        self.output = self.root / "output"

    def test_actual_offline_discovery_stable_fingerprint_and_no_credentials(self):
        with mock.patch.object(providers, "call_model") as inference, mock.patch.object(engine.discovery.directory, "_fetch") as fetch:
            first = engine.run_cycle(config(), self.output, environment={}, root=self.root)
            second = engine.run_cycle(config(), self.output, environment={}, root=self.root)
        inference.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(first["cycle_id"], second["cycle_id"])
        self.assertEqual(first["status"], "blocked_provider_missing")
        self.assertEqual(first["discovery"]["requests"], 0)
        self.assertEqual(first["counts"]["discovered"], 1)
        self.assertEqual(first["discovery"]["candidates"][0]["source_url"], "https://example.org/research")
        self.assertEqual(first["counts"]["enrolled_external_agents"], 0)
        self.assertEqual(len(first["discovery"]["provider_reports"]), 4)
        self.assertEqual(json.loads((self.output / "proposal.json").read_text())["status"], "none")

    def test_long_resource_identifiers_remain_distinct_and_namespaced(self):
        first = engine._candidate({"id": "a" * 250 + "X", "url": "https://example.org/a"})
        second = engine._candidate({"id": "a" * 250 + "Y", "url": "https://example.org/b"})
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["id"], "catalog:" + "a" * 250 + "X")
        self.assertNotEqual(engine._candidate({"id": "project-context"})["id"], "project-context")

    def test_provider_gates_never_call_model(self):
        cases = [(False, "openai", {"OPENAI_API_KEY": "test-token"}, "blocked_offline"),
                 (True, "openai", {"OPENAI_API_KEY": "test-token"}, "blocked_model_calls_not_allowed"),
                 (True, "openai", {}, "blocked_provider_missing"),
                 (True, "none", {"OPENAI_API_KEY": "test-token"}, "disabled")]
        offline = engine.discovery.discover("research", root=self.root)
        for online, provider, env, status in cases:
            with self.subTest(status=status), mock.patch.object(engine.discovery, "discover", return_value=offline), mock.patch.object(providers, "call_model") as call:
                result = engine.run_cycle(config(), self.output, online=online, provider=provider, environment=env, root=self.root)
                self.assertEqual(result["status"], status)
                call.assert_not_called()

    def test_model_proposal_only_in_json_and_curated_input(self):
        (self.root / "PRIVATE.txt").write_text("never-read-private-repository-content")
        offline = engine.discovery.discover("research", root=self.root)
        with mock.patch.object(engine.discovery, "discover", return_value=offline), mock.patch.object(providers, "call_model") as call:
            call.return_value = {"status": "response_received", "requests": 1, "response_received": True, "proposal": proposal()}
            result = engine.run_cycle(config(), self.output, online=True, environment={"OPENAI_API_KEY": "test-token", "AUTONOMY_ALLOW_MODEL_CALLS": "true", "UNRELATED_SECRET": "private"}, root=self.root)
        self.assertEqual(result["status"], "proposal_validated")
        self.assertEqual(result["counts"]["candidate_files"], 1)
        self.assertEqual(result["counts"]["model_response_received"], 1)
        self.assertEqual(result["counts"]["agent_endpoints_contacted"], 0)
        self.assertFalse((self.root / proposal()["files"][0]["path"]).exists())
        self.assertNotIn("never-read", json.dumps(call.call_args.args[3]))
        self.assertNotIn("UNRELATED_SECRET", json.dumps(call.call_args.args[3]))
        self.assertNotIn("test-token", (self.output / "cycle.json").read_text())
        self.assertEqual(json.loads((self.output / "proposal.json").read_text())["status"], "validated")

    def test_invalid_model_files_atomic_rejection(self):
        invalid = proposal()
        invalid["files"].append({"path": ".github/workflows/pwn.yml", "content": "bad"})
        offline = engine.discovery.discover("research", root=self.root)
        with mock.patch.object(engine.discovery, "discover", return_value=offline), mock.patch.object(providers, "call_model") as call:
            call.return_value = {"status": "response_received", "requests": 1, "response_received": True, "proposal": invalid}
            result = engine.run_cycle(config(), self.output, online=True, environment={"ANTHROPIC_API_KEY": "test-token", "AUTONOMY_ALLOW_MODEL_CALLS": "true"}, root=self.root)
        self.assertEqual(result["status"], "proposal_rejected")
        self.assertEqual(json.loads((self.output / "proposal.json").read_text())["files"], [])
        self.assertEqual(result["inference"]["provider"], "anthropic")

    def test_online_directories_exact_limits_no_endpoint_calls(self):
        response = {"items": [], "provider_reports": [{"provider": p, "item_count": 0, "errors": []}
                    for p in engine.discovery.PROVIDERS], "requests": 4}
        with mock.patch.object(engine.discovery, "discover", return_value=response) as discover:
            result = engine.run_cycle(config(), self.output, online=True, provider="none", environment={}, root=self.root)
        self.assertEqual(discover.call_args.kwargs["data_class"], "public")
        self.assertEqual(discover.call_args.kwargs["limit"], 2)
        self.assertEqual(discover.call_args.kwargs["providers"], list(engine.discovery.PROVIDERS))
        self.assertEqual(result["discovery"]["requests"], 4)

    def test_output_symlinks_rejected(self):
        (self.root / "target").mkdir()
        self.output.symlink_to(self.root / "target", target_is_directory=True)
        with self.assertRaises(ValueError):
            engine.run_cycle(config(), self.output, environment={}, root=self.root)
        self.output.unlink()
        self.output.mkdir()
        protected = self.root / "protected.txt"
        protected.write_text("Do not overwrite")
        (self.output / "cycle.json").symlink_to(protected)
        with self.assertRaises(ValueError):
            engine.run_cycle(config(), self.output, environment={}, root=self.root)
        self.assertEqual(protected.read_text(), "Do not overwrite")

    def test_cli_actual_offline_writes_three_outputs(self):
        file = self.root / "config.json"
        file.write_text(json.dumps(config()))
        with mock.patch("sys.stdout", new=io.StringIO()) as stream:
            rc = main(["--config", str(file), "--output-dir", str(self.output), "--provider", "none"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stream.getvalue())["status"], "disabled")
        self.assertEqual({p.name for p in self.output.iterdir()}, {"cycle.json", "proposal.json", "report.md"})

    def test_invalid_config_rejected_before_discovery(self):
        for mutate in [lambda x: x["discovery"].update(providers=["http://127.0.0.1"]),
                       lambda x: x["discovery"].update(limit_per_provider=True),
                       lambda x: x["project"].update(repository_url="https://user:password@github.com/a/b"),
                       lambda x: x.update(allow_model_calls="true"),
                       lambda x: x["providers"]["openai"].update(endpoint="https://evil.example"),
                       lambda x: x["project"].update(public_context="sk-proj-" + "a" * 60)]:
            value = config()
            mutate(value)
            with self.subTest(value=value), mock.patch.object(engine.discovery, "discover") as discover, self.assertRaises(ValueError):
                engine.run_cycle(value, self.output, environment={}, root=self.root)
            discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
