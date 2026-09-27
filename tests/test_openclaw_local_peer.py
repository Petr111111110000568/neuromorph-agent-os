"""Inert fixtures only: no real OpenClaw, download or model call."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "openclaw_local_peer.py"
SPEC = importlib.util.spec_from_file_location("tested_openclaw_local_peer", SOURCE)
peer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(peer)


class OpenClawPeerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.node = self.root / "node.exe"
        self.node.write_bytes(b"inert node fixture")
        self.package = self.root / "package"
        self.package.mkdir()
        (self.package / "openclaw.mjs").write_bytes(b"// inert entry fixture")
        (self.package / "package.json").write_text(json.dumps({
            "name": "openclaw", "version": peer.VERSION, "bin": {"openclaw": "openclaw.mjs"}}))
        self.manifest = {"schema_version": 1, "node_sha256": peer.sha256_file(self.node), "package": {
            "name": "openclaw", "version": peer.VERSION, "entry": "openclaw.mjs",
            "entry_sha256": peer.sha256_file(self.package / "openclaw.mjs"),
            "package_json_sha256": peer.sha256_file(self.package / "package.json")}}
        self.save_manifest()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Find a concrete counterexample in this public contract.", encoding="utf-8")
        self.calls = []

    def save_manifest(self):
        (self.package / peer.MANIFEST).write_text(json.dumps(self.manifest))

    def envelope(self, **changes):
        body = {"ok": True, "status": "ok", "final": "The first effect must be checked separately.",
                "model": peer.MODEL, "provider": peer.PROVIDER,
                "assistantTurns": 1, "toolSummary": {"calls": 0, "tools": []},
                "codeModeEngaged": False, "sessionId": "fixture-session"}
        body.update(changes)
        return subprocess.CompletedProcess([], 0, json.dumps(body).encode(), b"")

    def fake(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return self.envelope()

    def run_peer(self, **changes):
        params = dict(node=self.node, package_root=self.package, base_url="http://127.0.0.1:8768/v1",
                      prompt_file=self.prompt, output_file=self.root / "result.json",
                      home_dir=self.root / "isolated", token="private-test-token", runner=self.fake,
                      ambient={"SystemRoot": "C:\\Windows", "OPENAI_API_KEY": "forbidden", "NODE_OPTIONS": "bad"})
        params.update(changes)
        return peer.run_peer(**params)

    def test_finite_call_has_exact_model_and_no_ambient_credentials_or_shell(self):
        result = self.run_peer()
        args, options = self.calls[0]
        self.assertEqual(args[:4], [str(self.node), str(self.package / "openclaw.mjs"), "agent", "exec"])
        self.assertNotIn("--fallback", args)
        self.assertNotIn("--auth-env-only", args)
        self.assertEqual(options["timeout"], 300)
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertNotIn("NODE_OPTIONS", options["env"])
        self.assertEqual(result["sdk"]["toolSummary"]["calls"], 0)
        config_text = (self.root / "isolated" / "openclaw.json").read_text()
        self.assertNotIn("private-test-token", config_text)
        self.assertIn("${NEUROMORPH_TANDEM_TOKEN}", config_text)
        self.assertNotIn("private-test-token", (self.root / "result.json").read_text())
        self.assertFalse(list((self.root / "isolated").rglob(".env")))

    def test_closed_provider_and_tool_configuration(self):
        cfg = peer.build_provider_config("http://127.0.0.1:8768/v1")
        self.assertEqual(cfg["models"]["mode"], "replace")
        self.assertEqual(cfg["models"]["catalogRefresh"], {"enabled": False})
        self.assertEqual(list(cfg["models"]["providers"]), [peer.PROVIDER])
        self.assertEqual(cfg["tools"]["deny"], ["*"])
        self.assertEqual(cfg["tools"]["exec"], {"mode": "deny"})
        self.assertEqual(cfg["agents"]["defaults"]["model"]["fallbacks"], [])
        model = cfg["models"]["providers"][peer.PROVIDER]["models"][0]
        self.assertEqual((model["contextWindow"], model["maxTokens"]), (8192, 512))
        self.assertFalse(model["compat"]["supportsTools"])

    def test_nonloopback_credentials_and_ambiguous_urls_rejected_before_process(self):
        for url in ("https://example.com/v1", "http://localhost:8080/v1", "http://127.0.0.1/v1",
                    "http://x@127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1?x=y",
                    "http://127.0.0.1:8080/v1/", "http://127.0.0.2:8080/v1"):
            with self.subTest(url=url), self.assertRaisesRegex(peer.PeerError, "nonlocal"):
                self.run_peer(base_url=url)
        self.assertEqual(self.calls, [])

    def test_modified_node_entry_or_package_metadata_fails_pin(self):
        for path in (self.node, self.package / "openclaw.mjs", self.package / "package.json"):
            original = path.read_bytes()
            path.write_bytes(original + b"tampered")
            with self.subTest(path=path), self.assertRaisesRegex(peer.PeerError, "pin_mismatch"):
                self.run_peer()
            path.write_bytes(original)
        self.assertEqual(self.calls, [])

    def test_missing_manifest_and_manifest_entry_path_are_not_admitted(self):
        (self.package / peer.MANIFEST).unlink()
        with self.assertRaisesRegex(peer.PeerError, "installation_unavailable"):
            self.run_peer()
        self.manifest["package"]["entry"] = "../other.mjs"
        self.save_manifest()
        with self.assertRaisesRegex(peer.PeerError, "pin_mismatch"):
            self.run_peer()
        self.assertEqual(self.calls, [])

    def test_large_valid_pinned_package_metadata_is_admitted(self):
        package_file = self.package / "package.json"
        value = json.loads(package_file.read_text())
        value["exports_fixture"] = "x" * 150_000
        package_file.write_text(json.dumps(value), encoding="utf-8")
        self.manifest["package"]["package_json_sha256"] = peer.sha256_file(package_file)
        self.save_manifest()
        result = self.run_peer()
        self.assertEqual(result["package_version"], peer.VERSION)
        self.assertEqual(len(self.calls), 1)

    def test_package_metadata_over_256_kib_is_rejected_before_launch(self):
        package_file = self.package / "package.json"
        value = json.loads(package_file.read_text())
        value["exports_fixture"] = "x" * (256 * 1024)
        package_file.write_text(json.dumps(value), encoding="utf-8")
        self.manifest["package"]["package_json_sha256"] = peer.sha256_file(package_file)
        self.save_manifest()
        with self.assertRaisesRegex(peer.PeerError, "metadata_too_large"):
            self.run_peer()
        self.assertEqual(self.calls, [])

    def test_preexisting_home_with_dotenv_never_used(self):
        home = self.root / "isolated"
        home.mkdir()
        (home / ".env").write_text("OPENAI_API_KEY=untrusted")
        with self.assertRaisesRegex(peer.PeerError, "run_path_already_exists"):
            self.run_peer()
        self.assertEqual(self.calls, [])

    def test_symlinked_home_parent_rejected(self):
        target, link = self.root / "target", self.root / "link"
        target.mkdir()
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("symlink permission unavailable")
        with self.assertRaisesRegex(peer.PeerError, "linked_path"):
            self.run_peer(home_dir=link / "new-home")
        self.assertEqual(self.calls, [])

    def test_oversize_or_secret_prompt_prevents_launch(self):
        for value in ("x" * (peer.MAX_PROMPT + 1), "secret: private-test-token", " "):
            self.prompt.write_text(value)
            with self.subTest(length=len(value)), self.assertRaisesRegex(peer.PeerError, "invalid_prompt"):
                self.run_peer()
        self.assertEqual(self.calls, [])

    def test_wrong_model_tools_multiple_turns_and_error_envelopes_rejected(self):
        for change in ({"model": "other"}, {"provider": "openai"}, {"ok": False},
                       {"final": ""}, {"status": "timeout"}, {"codeModeEngaged": True},
                       {"assistantTurns": 2}, {"toolSummary": {"calls": 1, "tools": ["exec"]}},
                       {"error": {"message": "failed"}}):
            with self.subTest(change=change), self.assertRaises(peer.PeerError):
                peer.validate_result(self.envelope(**change), "private-test-token")

    def test_omitted_optional_stats_are_not_fabricated_as_zero(self):
        result = self.envelope()
        body = json.loads(result.stdout)
        del body["toolSummary"]
        del body["assistantTurns"]
        result.stdout = json.dumps(body).encode()
        self.assertNotIn("toolSummary", peer.validate_result(result, "private-test-token"))

    def test_secret_stdout_stderr_output_limits_and_nonzero_are_rejected(self):
        cases = [subprocess.CompletedProcess([], 0, b"private-test-token", b""),
                 subprocess.CompletedProcess([], 0, b"{}", b"private-test-token"),
                 subprocess.CompletedProcess([], 0, b"x" * (peer.MAX_STDOUT + 1), b""),
                 subprocess.CompletedProcess([], 9, b"{}", b"failure"),
                 subprocess.CompletedProcess([], 0, b"not json", b"")]
        for result in cases:
            with self.subTest(size=len(result.stdout)), self.assertRaises(peer.PeerError):
                peer.validate_result(result, "private-test-token")

    def test_timeout_propagates_without_receipt_or_retry(self):
        def timeout(args, **kwargs):
            self.calls.append(args)
            raise peer.PeerError("process_timeout")
        with self.assertRaisesRegex(peer.PeerError, "process_timeout"):
            self.run_peer(runner=timeout)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.root / "result.json").exists())

    def test_failed_cli_classification_never_emits_untrusted_error_details(self):
        cases = [
            ({"type": "cli_error", "message": "Invalid config at C:/private/data"}, "openclaw_config_invalid"),
            ({"kind": "context_length_exceeded", "message": "private prompt"}, "openclaw_context_limit"),
            ({"code": "invalid_api_key", "message": "private header"}, "openclaw_auth_rejected"),
            ({"type": "cli_error", "message": "unknown private value"}, "openclaw_cli_rejected"),
            ({"type": "invented_untrusted_code"}, "openclaw_failed"),
        ]
        for error, expected in cases:
            result = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "error": error}).encode(), b"")
            with self.subTest(expected=expected), self.assertRaises(peer.PeerError) as caught:
                peer.validate_result(result, "private-test-token")
            self.assertEqual(str(caught.exception), expected)

    def test_callback_returns_real_final_and_does_not_replay_existing_stage(self):
        callback = peer.make_role_runner(self.node, self.package, runner=self.fake)
        stage = self.root / "stage"
        final = callback("review", "Public review", "http://127.0.0.1:8768/v1", "private-test-token", stage)
        self.assertIn("first effect", final)
        with self.assertRaises(FileExistsError):
            callback("review", "Public review", "http://127.0.0.1:8768/v1", "private-test-token", stage)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
