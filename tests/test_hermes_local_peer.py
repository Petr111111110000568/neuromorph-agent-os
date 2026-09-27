"""Contract checks; fake constructor/transport never executes a model or SDK."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hermes_local_peer.py"
SPEC = importlib.util.spec_from_file_location("hermes_local_peer_under_test", SCRIPT)
peer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(peer)
BASE_URL = "http://127.0.0.1:24567/v1"
TOKEN = "a" * 64  # inert fixture token, never a user credential


class FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.base_url = kwargs["base_url"]
        self.model = kwargs["model"]
        self.provider = kwargs["provider"]
        self.tools = []
        self.valid_tool_names = set()
        self.compression_enabled = False
        self._fallback_chain = []
        self._credential_pool = None
        self.client = types.SimpleNamespace(max_retries=3)
        self._client_kwargs = {}
        self.closed = False
        self.result = {"completed": True, "api_calls": 1, "final_response": "Bounded critique."}

    def run_conversation(self, prompt):
        self.prompt = prompt
        return self.result

    def close(self):
        self.closed = True


class LocalPeerTests(unittest.TestCase):
    def test_manifest_bytes_are_independently_pinned(self):
        raw = peer.MANIFEST.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), peer.MANIFEST_SHA256)
        manifest = json.loads(raw)
        self.assertEqual(manifest["source_commit"], peer.COMMIT)
        self.assertEqual(manifest["required_python"], "3.14")
        self.assertEqual(manifest["context_tokens"], 8192)
        self.assertIn("agent/agent_init.py", manifest["file_sha256"])
        self.assertIn("hermes_cli/env_loader.py", manifest["file_sha256"])

    def test_endpoint_is_exact_numeric_loopback(self):
        self.assertEqual(peer.loopback_url(BASE_URL), 24567)
        for bad in ("https://127.0.0.1:24567/v1", "http://localhost:24567/v1",
                    "http://127.0.0.1:80/v1", "http://127.0.0.1:24567/v1?q=1",
                    "http://127.0.0.1:24567/v1/", "http://127.0.0.1:99999/v1",
                    "http://127.0.0.1:24567@evil.test/v1", "http://192.168.0.1:24567/v1"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                peer.loopback_url(bad)

    def test_prompt_limit_and_strict_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prompt.txt"
            path.write_text("Исследование", encoding="utf-8")
            self.assertEqual(peer.read_prompt(path), "Исследование")
            for bad in (b"\xff", b"\x00a", b" ", b"x" * (peer.MAX_PROMPT + 1)):
                path.write_bytes(bad)
                with self.assertRaises(peer.PeerError):
                    peer.read_prompt(path)

    def test_fresh_home_does_not_copy_credentials_or_desktop_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "hermes-home"
            with patch.dict(os.environ, {"OPENAI_API_KEY": "not-a-real-secret",
                            "HERMES_HOME": "unrelated-user-profile",
                            "HERMES_DASHBOARD_SESSION_TOKEN": "not-a-real-token",
                            "HTTPS_PROXY": "http://untrusted.test"}):
                env = peer.fresh_environment(home)
            for name in ("OPENAI_API_KEY", "HERMES_DASHBOARD_SESSION_TOKEN", "HTTPS_PROXY"):
                self.assertNotIn(name, env)
            self.assertEqual(env["HERMES_HOME"], str(home))
            self.assertEqual(env["HERMES_MANAGED_DIR"], str(home / "managed"))
            self.assertFalse((home / ".env").exists())
            with self.assertRaisesRegex(peer.PeerError, "fresh_home_required"):
                peer.fresh_environment(home)

    def test_profile_no_tools_memory_fallback_and_honest_context(self):
        config = peer.profile(BASE_URL)
        self.assertEqual(config["model"]["context_length"], 8192)
        self.assertEqual(config["plugins"]["enabled"], [])
        self.assertEqual(config["toolsets"], [])
        self.assertEqual(config["mcp_servers"], {})
        self.assertEqual(config["fallback_providers"], [])
        self.assertFalse(config["compression"]["enabled"])
        self.assertFalse(config["memory"]["memory_enabled"])
        self.assertFalse(config["auxiliary"]["background_review"]["enabled"])

    def test_sdk_real_constructor_contract_and_single_completed_answer(self):
        agents = []
        def factory(**kwargs):
            agents.append(FakeAgent(**kwargs))
            return agents[-1]
        result = peer.run_sdk("public question", BASE_URL, Path("isolated-home"), factory, token=TOKEN)
        agent = agents[0]
        self.assertEqual(result["status"], "response_received")
        self.assertEqual(result["text"], "Bounded critique.")
        self.assertEqual(result["api_calls"], 1)
        self.assertEqual(result["context_tokens"], 8192)
        self.assertEqual(agent.kwargs["max_tokens"], 512)
        self.assertEqual(agent.kwargs["api_key"], TOKEN)
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(agent.kwargs["max_iterations"], 1)
        self.assertEqual(agent.kwargs["enabled_toolsets"], [])
        self.assertTrue(agent.kwargs["skip_context_files"])
        self.assertTrue(agent.kwargs["skip_background_review"])
        self.assertTrue(agent.kwargs["skip_memory"])
        self.assertIsNone(agent.kwargs["credential_pool"])
        self.assertIsNone(agent.kwargs["session_db"])
        self.assertEqual(agent.client.max_retries, 0)
        self.assertEqual(agent.client.timeout, peer.SDK_SECONDS)
        self.assertEqual(agent._client_kwargs["max_retries"], 0)
        self.assertTrue(agent.closed)
        self.assertNotIn("isolated-home", json.dumps(result))

    def test_failure_or_extra_request_is_not_a_valid_answer(self):
        cases = [{"completed": False}, {"partial": True}, {"interrupted": True},
                 {"failed": True}, {"api_calls": 2}, {"api_calls": True},
                 {"api_calls": None}, {"final_response": ""},
                 {"final_response": "echo " + TOKEN},
                 {"final_response": "x" * (peer.MAX_TEXT + 1)}]
        for changed in cases:
            with self.subTest(changed=changed.keys()):
                agent = FakeAgent(base_url=BASE_URL, model=peer.MODEL, provider="custom")
                agent.result.update(changed)
                with self.assertRaises(peer.PeerError):
                    peer.run_sdk("public", BASE_URL, Path("home"), lambda **kw: agent, token=TOKEN)
                self.assertTrue(agent.closed)

    def test_tools_fallback_credentials_or_route_change_rejected_before_conversation(self):
        for attr, value in (("tools", [{"function": {"name": "execute"}}]),
                            ("_fallback_chain", [{"model": "paid"}]),
                            ("_credential_pool", object()),
                            ("base_url", "https://external.test/v1"),
                            ("compression_enabled", True)):
            with self.subTest(attribute=attr):
                agent = FakeAgent(base_url=BASE_URL, model=peer.MODEL, provider="custom")
                setattr(agent, attr, value)
                with self.assertRaises(peer.PeerError):
                    peer.run_sdk("public", BASE_URL, Path("home"), lambda **kw: agent, token=TOKEN)
                self.assertFalse(hasattr(agent, "prompt"))
                self.assertTrue(agent.closed)

    def test_guard_allows_only_gateway_and_rejects_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            guard = peer.audit_guard(24567, home=root / "home", source=root / "source",
                                     output=root / "result.json")
            guard("socket.connect", (None, ("127.0.0.1", 24567)))
            guard("socket.getaddrinfo", ("127.0.0.1", 24567, 0, 0, 0))
            for event, args in (("socket.connect", (None, ("127.0.0.1", 63065))),
                                ("socket.connect", (None, ("1.1.1.1", 443))),
                                ("socket.getaddrinfo", ("external.test", 443)),
                                ("subprocess.Popen", ("cmd",)), ("socket.bind", (None,)),
                                ("os.system", ("echo forbidden",))):
                with self.subTest(event=event), self.assertRaises(PermissionError):
                    guard(event, args)

    def test_guard_never_opens_credentials_even_inside_source_or_home(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home, source, output = root / "home", root / "source", root / "result.json"
            guard = peer.audit_guard(24567, home=home, source=source, output=output)
            for path in (source / ".env", home / "auth.json", home / "tokens.json",
                         source / ".env.local", root / "private" / "config.yaml"):
                with self.subTest(file=path.name), self.assertRaises(PermissionError):
                    guard("open", (str(path), "r", os.O_RDONLY))
            guard("open", (str(source / "run_agent.py"), "r", os.O_RDONLY))
            guard("open", (str(home / "config.yaml"), "w", os.O_WRONLY))
            guard("open", (str(output), "x", os.O_WRONLY | os.O_CREAT))
            with self.assertRaises(PermissionError):
                guard("open", (str(source / "run_agent.py"), "w", os.O_WRONLY))
            with self.assertRaises(FileNotFoundError):
                guard("open", (str(source / "__pycache__" / "run_agent.pyc"), "r", os.O_RDONLY))
            guard("sqlite3.connect", (":memory:",))
            guard("sqlite3.connect", (str(home / "state.db"),))
            with self.assertRaises(PermissionError):
                guard("sqlite3.connect", (str(root / "desktop" / "state.db"),))

    def test_context_adapter_is_only_exact_no_tools_profile(self):
        calls = []
        env_loader = types.ModuleType("hermes_cli.env_loader")
        env_loader.load_hermes_dotenv = lambda **kw: calls.append("dotenv")
        agent_init = types.ModuleType("agent.agent_init")
        agent_init._enforce_minimum_context = lambda obj: calls.append("upstream")
        cli, package = types.ModuleType("hermes_cli"), types.ModuleType("agent")
        cli.env_loader, package.agent_init = env_loader, agent_init
        with patch.dict(sys.modules, {"hermes_cli": cli, "hermes_cli.env_loader": env_loader,
                                     "agent": package, "agent.agent_init": agent_init}):
            peer.install_profile_adaptations(BASE_URL)
            self.assertEqual(env_loader.load_hermes_dotenv(project_env="must-not-read.env"), [])
            obj = types.SimpleNamespace(provider="custom", base_url=BASE_URL, model=peer.MODEL,
                      tools=[], valid_tool_names=set(), _config_context_length=8192,
                      context_compressor=types.SimpleNamespace(context_length=8192),
                      compression_enabled=False)
            agent_init._enforce_minimum_context(obj)
            self.assertEqual(calls, [])
            for attr, value in (("tools", ["shell"]), ("provider", "lmstudio"),
                                ("base_url", "http://127.0.0.1:9999/v1"),
                                ("_config_context_length", 65536)):
                old = getattr(obj, attr)
                setattr(obj, attr, value)
                agent_init._enforce_minimum_context(obj)
                setattr(obj, attr, old)
            self.assertEqual(calls, ["upstream"] * 4)

    def test_changed_source_rejected_before_git_and_no_raw_exception_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / "manifest.json"
            source = root / "source"
            source.mkdir()
            (source / "run_agent.py").write_bytes(b"changed")
            raw = json.dumps({"source_commit": peer.COMMIT, "file_sha256": {
                "run_agent.py": hashlib.sha256(b"pinned").hexdigest()}}).encode()
            manifest.write_bytes(raw)
            with patch.object(peer, "MANIFEST", manifest), patch.object(peer, "MANIFEST_SHA256",
                    hashlib.sha256(raw).hexdigest()), patch.object(peer.subprocess, "run") as run:
                with self.assertRaisesRegex(peer.PeerError, "source_file_changed"):
                    peer.verify_source(source, {})
                run.assert_not_called()
        result = peer.safe_failure(RuntimeError("secret /private/location https://signed.example"))
        self.assertEqual(result["reason"], "sdk_failed")
        self.assertNotIn("signed.example", json.dumps(result))
        self.assertNotIn("private/location", json.dumps(result))

    def test_incomplete_diagnostics_never_include_error_text_or_message_history(self):
        agent = FakeAgent(base_url=BASE_URL, model=peer.MODEL, provider="custom")
        agent.result.update(completed=False, failed=True, api_calls=1, failure_reason="format_error",
                            status_code=400, error="private " + TOKEN,
                            messages=[{"content": TOKEN}], final_response="error " + TOKEN)
        agent._invoke_api_request_error_hook = lambda **details: None
        original_turn = agent.run_conversation
        def turn(prompt):
            agent._invoke_api_request_error_hook(reason="format_error", status_code=400,
                api_call_count=1, retry_count=0, max_retries=1, error_message=TOKEN, api_kwargs={"key": TOKEN})
            return original_turn(prompt)
        agent.run_conversation = turn
        with self.assertRaises(peer.PeerError) as raised:
            peer.run_sdk("public", BASE_URL, Path("home"), lambda **kw: agent, token=TOKEN)
        diagnostic = peer.safe_failure(raised.exception, "running_sdk")
        self.assertEqual(diagnostic["sdk_result"]["failure_reason"], "format_error")
        self.assertEqual(diagnostic["sdk_result"]["status_code"], 400)
        self.assertEqual(diagnostic["api_errors"][0]["status_code"], 400)
        self.assertNotIn(TOKEN, json.dumps(diagnostic))
        self.assertNotIn("messages", diagnostic["sdk_result"])
        self.assertNotIn("error_message", diagnostic["api_errors"][0])


if __name__ == "__main__":
    unittest.main()
