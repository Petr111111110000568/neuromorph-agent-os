"""Bounded trusted subprocess runner. Process limits are not a security sandbox."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from ..autonomy.providers import json_load

MAX_OUTPUT_BYTES = 256 * 1024
MAX_PROMPT_BYTES = 32000
MAX_EVENTS = 1000
SYSTEM_PROMPT = ("You are a software research assistant invoked by Meta-Harness. All tools are disabled. "
                 "Use only the supplied public prompt. Return a concise textual proposal with limits; "
                 "do not claim file access, test execution, deployment or biological validation. "
                 "Do not emit tool calls or operating-system commands for execution. "
                 "This process has resource limits but is not a security sandbox. "
                 "Do not provide biological intervention protocols. Your output remains untrusted text.")
LAUNCHER = """import os, resource, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_CPU, (30, 31))
address_space = int(sys.argv[1])
resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
resource.setrlimit(resource.RLIMIT_FSIZE, (8388608, 8388608))
resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
os.execve(sys.argv[2], sys.argv[2:], os.environ)
"""


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def capture(command, request, cwd, environment, timeout_seconds, adapter=None, address_space_bytes=2147483648):
    """Never invoke a shell; cap both streams and terminate the whole process group."""
    process = subprocess.Popen([sys.executable, "-I", "-c", LAUNCHER, str(address_space_bytes), *command],
                               cwd=cwd, env=environment, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, shell=False)
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    for name in ("stdout", "stderr"):
        stream = getattr(process, name)
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    os.set_blocking(process.stdin.fileno(), False)
    if request:
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        process.stdin.close()
    offset, total, status, pending_stdout = 0, 0, "exited", b""
    deadline, parent_exited = time.monotonic() + timeout_seconds, False
    try:
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "timeout"
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                if key.data == "stdin":
                    try:
                        offset += os.write(key.fd, request[offset:offset + 4096])
                    except BrokenPipeError:
                        offset = len(request)
                    if offset >= len(request):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fd, min(16384, MAX_OUTPUT_BYTES + 1 - total))
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffers[key.data].extend(chunk)
                total += len(chunk)
                if total > MAX_OUTPUT_BYTES:
                    status = "output_limit"
                    break
                if key.data == "stdout" and adapter:
                    pending_stdout += chunk
                    while b"\n" in pending_stdout:
                        line, pending_stdout = pending_stdout.split(b"\n", 1)
                        try:
                            event = json_load(line)
                            kind = event.get("Kind") if adapter == "unreal_jsonl" else event.get("type")
                            tool_event = kind in {"tool_call_status", "tool_execution_start", "tool_execution_update", "tool_execution_end"}
                            if kind == "model_response":
                                tool_event = tool_event or any(item.get("Type") in {"tool_call", "tool_result"}
                                    for item in event.get("Data", {}).get("Response", {}).get("Output", []))
                            if kind in {"message_start", "message_end"}:
                                tool_event = tool_event or any(item.get("type") == "toolCall"
                                    for item in event.get("message", {}).get("content", []))
                            if tool_event:
                                status = "policy_violation"
                                break
                        except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
                            pass  # Full parsing still rejects malformed output after exit.
                    if status != "exited":
                        break
            if status != "exited":
                break
            if process.poll() is not None and not parent_exited:
                # Descendants do not outlive a normally exiting harness either.
                parent_exited = True
                _kill_group(process)
    finally:
        _kill_group(process)
        process.wait(timeout=3)
        selector.close()
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, name)
            if stream and not stream.closed:
                stream.close()
    return {"status": status, "exit_code": process.returncode,
            "stdout": bytes(buffers["stdout"]), "stderr": bytes(buffers["stderr"])}


def _protocol_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or not parsed.port:
        raise ValueError("Protocol test endpoint must be an explicit loopback HTTP URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/v1"}:
        raise ValueError("Unexpected protocol test endpoint")
    return value.rstrip("/")


def parse_events(raw, adapter):
    lines = raw.split(b"\n")
    if not lines or len(lines) > MAX_EVENTS:
        raise ValueError("Invalid JSONL event count")
    events = [json_load(line) for line in lines if line.strip()]
    if any(not isinstance(event, dict) for event in events):
        raise ValueError("JSONL event must be an object")
    counts, responses, final_text = Counter(), 0, ""
    for event in events:
        kind = event.get("Kind") if adapter == "unreal_jsonl" else event.get("type")
        if not isinstance(kind, str):
            if event.get("type") == "error":
                return {"status": "runner_error", "output_text": "", "events": len(events), "model_responses": responses}
            raise ValueError("Missing event kind")
        counts[kind] += 1
        if kind in {"tool_call_status", "tool_execution_start", "tool_execution_update", "tool_execution_end"}:
            return {"status": "policy_violation", "output_text": "", "events": len(events), "model_responses": responses}
        if kind == "error":
            return {"status": "runner_error", "output_text": "", "events": len(events), "model_responses": responses}
        if adapter == "unreal_jsonl" and kind == "model_response":
            response = event["Data"]["Response"]
            responses += 1
            if response.get("Failure") or response.get("Stop") != "complete":
                return {"status": "incomplete_response", "output_text": "", "events": len(events), "model_responses": responses}
            pieces, final_pieces = [], []
            for item in response.get("Output", []):
                if item.get("Type") in {"tool_call", "tool_result"}:
                    return {"status": "policy_violation", "output_text": "", "events": len(events), "model_responses": responses}
                if item.get("Type") == "message" and item.get("Data", {}).get("Role") == "assistant":
                    text = item["Data"].get("Text", "")
                    if not isinstance(text, str):
                        raise ValueError("Invalid assistant text")
                    if item["Data"].get("Phase") != "commentary":
                        pieces.append(text)
                    if item["Data"].get("Phase") == "final_answer":
                        final_pieces.append(text)
            final_text = "\n".join(final_pieces or pieces).strip()
        elif adapter == "pi_jsonl" and kind == "message_end":
            message = event.get("message", {})
            if message.get("role") != "assistant":
                continue
            responses += 1
            if message.get("stopReason") not in {"stop", "length"} or message.get("errorMessage"):
                return {"status": "incomplete_response", "output_text": "", "events": len(events), "model_responses": responses}
            if message.get("stopReason") == "length":
                return {"status": "incomplete_response", "output_text": "", "events": len(events), "model_responses": responses}
            content = message.get("content", [])
            if any(piece.get("type") == "toolCall" for piece in content):
                return {"status": "policy_violation", "output_text": "", "events": len(events), "model_responses": responses}
            final_text = "\n".join(piece["text"] for piece in content if piece.get("type") == "text").strip()
    settled = adapter != "pi_jsonl" or counts["agent_settled"] > 0
    return {"status": "completed" if responses and final_text and settled else "empty_response",
            "output_text": final_text, "events": len(events), "model_responses": responses,
            "event_types": dict(counts)}


def run(entry, executable, prompt, allow_model_calls=False, provider="openai", model=None,
        environment=None, timeout_seconds=30, protocol_test_base_url=None):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("Prompt must contain 1–32000 UTF-8 bytes")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in prompt):
        raise ValueError("Prompt contains control characters")
    if type(allow_model_calls) is not bool or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("Invalid call gate or timeout")
    if provider not in entry["providers"]:
        raise ValueError("Unsupported provider for this adapter")
    model = model or entry["default_model"]
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}", model):
        raise ValueError("Invalid model identifier")
    env = os.environ if environment is None else environment
    key_name = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider]
    token = env.get(key_name, "")
    mode = "protocol_test" if protocol_test_base_url is not None else "live"
    base = {"status": "blocked_model_calls_not_allowed", "output_text": "", "exit_code": None,
            "provider": provider, "model": model, "mode": mode,
            "counts": {"events": 0, "model_responses": 0},
            "execution": {"tools_enabled": False, "generated_code_executed": False,
                          "isolation": "process_limits_not_sandbox", "wall_timeout_seconds": timeout_seconds,
                          "max_output_bytes": MAX_OUTPUT_BYTES, "api_request_cap": None, "api_token_cap": None}}
    if os.name != "posix":
        return dict(base, status="unsupported_platform_use_linux_or_wsl")
    if protocol_test_base_url is not None:
        if entry["adapter"] != "unreal_jsonl":
            raise ValueError("Protocol mock endpoint is supported only for Unreal")
        protocol_test_base_url = _protocol_url(protocol_test_base_url)
        token = "meta-harness-local-protocol-test"
    elif not allow_model_calls:
        return base
    if not token or len(token) > 2048 or any(c.isspace() for c in token):
        return dict(base, status="blocked_provider_missing")
    if token in prompt:
        return dict(base, status="credential_in_prompt_rejected")
    with tempfile.TemporaryDirectory(prefix="meta-harness-tools-disabled-") as directory:
        work = Path(directory)
        for name in ("workspace", "sessions", "tmp", "state", "pi-config"):
            (work / name).mkdir(mode=0o700)
        # No HOME, shell configuration, inherited proxies, OAuth or model-base overrides.
        child_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
                     "TMPDIR": str(work / "tmp"), "XDG_STATE_HOME": str(work / "state"),
                     "PI_CODING_AGENT_DIR": str(work / "pi-config"), "PI_OFFLINE": "1",
                     "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0", key_name: token}
        executable_command = [str(x) for x in executable] if isinstance(executable, list) else [str(executable)]
        if entry["adapter"] == "unreal_jsonl":
            child_env["UNREAL_HARNESS_LLM_PROVIDER"] = provider
            child_env["UNREAL_HARNESS_LLM_MAX_ATTEMPTS"] = "1"
            if protocol_test_base_url:
                child_env["UNREAL_HARNESS_LLM_BASE_URL"] = protocol_test_base_url
            command = [*executable_command, "-workspace", str(work / "workspace"),
                       "-session-directory", str(work / "sessions"), "-tool-heartbeat-interval", "0"]
            request = json.dumps({"prompt": prompt, "model": model, "system_prompt": SYSTEM_PROMPT,
                                  "max_attempts": 1, "thinking_level": "low",
                                  "disallowed_tools": ["Bash", "ViewImage", "SkillUse"]}).encode("utf-8")
        else:
            command = [*executable_command, "--mode", "json", "--no-session", "--no-tools", "--no-extensions",
                       "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
                       "--no-approve", "--offline",
                       "--provider", provider, "--model", model, "--system-prompt", SYSTEM_PROMPT]
            # Positional @path is Pi's attachment syntax, even after '--'. Stdin is plain text.
            request = prompt.encode("utf-8")
        address_space = 16 * 1024 ** 3 if entry["adapter"] == "pi_jsonl" else 2 * 1024 ** 3
        result = capture(command, request, str(work / "workspace"), child_env, timeout_seconds,
                         entry["adapter"], address_space_bytes=address_space)
    base.update(exit_code=result["exit_code"], stdout_bytes=len(result["stdout"]), stderr_bytes=len(result["stderr"]))
    if result["status"] != "exited":
        return dict(base, status=result["status"])
    if token.encode() in result["stdout"] or token.encode() in result["stderr"]:
        return dict(base, status="credential_in_output_rejected")
    if result["exit_code"] != 0:
        return dict(base, status="runner_failed")
    try:
        parsed = parse_events(result["stdout"], entry["adapter"])
        if token in parsed["output_text"]:
            return dict(base, status="credential_in_output_rejected", output_text="")
        base.update(status=parsed["status"], output_text=parsed["output_text"],
                    counts={"events": parsed["events"], "model_responses": parsed["model_responses"]},
                    event_types=parsed.get("event_types", {}))
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        base.update(status="invalid_runner_output", output_text="")
    return base
