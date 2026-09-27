"""Finite, local-only OpenClaw peer. Operator-pinned installation; no tools.

The process environment and provider inventory are isolated. This is not an OS
sandbox: the trusted OpenClaw package itself still executes with the user's rights.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit

VERSION = "2026.9.6"
MODEL = "Qwen3-14B-Q4_K_M"
PROVIDER = "neuromorph-local"
MANIFEST = "neuromorph-installed-manifest.json"
MAX_PROMPT = 24_000
MAX_STDOUT = 1_048_576
MAX_STDERR = 262_144
MAX_FINAL = 32_768
TIMEOUT = 300


class PeerError(RuntimeError):
    """Fixed, non-secret diagnostic code."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_file(path, maximum=65_536):
    with Path(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise PeerError("metadata_too_large")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise PeerError("invalid_metadata") from exc


def _plain_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise PeerError("linked_path_denied")
    return path


def validate_installation(node, package_root):
    """Verify the trusted operator manifest, not a model-provided checksum.

    The installation manifest anchors Node, entry point and package metadata.
    Dependency integrity remains the operator's pinned npm lock/install boundary.
    """
    node, root = _plain_path(node), _plain_path(package_root)
    try:
        manifest = _json_file(root / MANIFEST)
        package = manifest["package"]
        if (manifest["schema_version"] != 1 or package["name"] != "openclaw"
                or package["version"] != VERSION or package["entry"] != "openclaw.mjs"):
            raise PeerError("installation_pin_mismatch")
        entry = _plain_path(root / "openclaw.mjs")
        package_file = _plain_path(root / "package.json")
        for path, expected in (
            (node, manifest["node_sha256"]),
            (entry, package["entry_sha256"]),
            (package_file, package["package_json_sha256"]),
        ):
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise PeerError("installation_pin_mismatch")
            if sha256_file(path) != expected:
                raise PeerError("installation_pin_mismatch")
        actual = _json_file(package_file)
        if actual.get("name") != "openclaw" or actual.get("version") != VERSION:
            raise PeerError("installation_pin_mismatch")
        if actual.get("bin", {}).get("openclaw") != "openclaw.mjs":
            raise PeerError("installation_pin_mismatch")
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        raise PeerError("installation_unavailable") from exc
    return node, root, entry


def validate_base_url(base_url):
    try:
        parsed = urlsplit(base_url)
        valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                 and parsed.port is not None and 1 <= parsed.port <= 65535
                 and parsed.path == "/v1" and not parsed.query and not parsed.fragment
                 and parsed.username is None and parsed.password is None)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise PeerError("nonlocal_provider_denied")
    return base_url


def build_provider_config(base_url):
    validate_base_url(base_url)
    return {
        "env": {"shellEnv": {"enabled": False}},
        "agents": {"defaults": {
            "model": {"primary": f"{PROVIDER}/{MODEL}", "fallbacks": []},
            "skills": [],
        }},
        "models": {"mode": "replace", "catalogRefresh": {"enabled": False}, "providers": {PROVIDER: {
            "baseUrl": base_url,
            "apiKey": "${NEUROMORPH_TANDEM_TOKEN}",
            "api": "openai-completions",
            "timeoutSeconds": 240,
            "models": [{"id": MODEL, "name": MODEL, "input": ["text"],
                        "reasoning": False, "contextWindow": 8192, "maxTokens": 512,
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        "compat": {"supportsTools": False}}],
        }}},
        "skills": {"load": {"watch": False, "extraDirs": []}},
        "tools": {"profile": "minimal", "deny": ["*"],
                  "exec": {"mode": "deny"}, "elevated": {"enabled": False},
                  "codeMode": {"enabled": False}},
        "mcp": {"servers": {}},
    }


def build_environment(home, node, token, ambient=None):
    if not isinstance(token, str) or not token or len(token) > 512 or any(c in token for c in "\r\n\x00"):
        raise PeerError("invalid_local_token")
    ambient = os.environ if ambient is None else ambient
    env = {key: ambient[key] for key in ("SystemRoot", "WINDIR", "SYSTEMDRIVE") if key in ambient}
    home = Path(home)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / "appdata"), "LOCALAPPDATA": str(home / "localappdata"),
        "TEMP": str(home / "tmp"), "TMP": str(home / "tmp"),
        "OPENCLAW_HOME": str(home), "OPENCLAW_STATE_DIR": str(home / "state"),
        "OPENCLAW_AGENT_DIR": str(home / "agent"),
        "OPENCLAW_CONFIG_PATH": str(home / "openclaw.json"),
        "OPENCLAW_WORKSPACE_DIR": str(home / "workspace"),
        "OPENCLAW_OFFLINE": "1", "OPENCLAW_LOAD_SHELL_ENV": "0",
        "NEUROMORPH_TANDEM_TOKEN": token, "NO_COLOR": "1", "CI": "1",
    })
    path = [str(Path(node).parent)]
    if env.get("SystemRoot"):
        path.append(str(Path(env["SystemRoot"]) / "System32"))
    env["PATH"] = os.pathsep.join(path)
    return env


class _WindowsJob:
    """Kill descendants on close. Lifecycle containment, not an OS sandbox."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IoCounters(ctypes.Structure):
            _fields_ = [(field, ctypes.c_uint64) for field in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if (not self.handle or not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not self.api.AssignProcessToJobObject(self.handle, int(process._handle))):
            if self.handle:
                self.api.CloseHandle(self.handle)
            self.handle = None
            raise PeerError("process_containment_unavailable")

    def close(self):
        if self.handle:
            self.api.TerminateJobObject(self.handle, 1)
            self.api.CloseHandle(self.handle)
            self.handle = None


def run_bounded(args, *, cwd, env, timeout=TIMEOUT):
    """Drain bounded pipes concurrently and terminate the owned process tree."""
    process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               shell=False, start_new_session=os.name != "nt",
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    job = None
    overflow = threading.Event()
    buffers = [bytearray(), bytearray()]
    readers = []

    def drain(stream, output, limit):
        try:
            while chunk := stream.read(8192):
                remaining = limit - len(output)
                output.extend(chunk[:max(remaining, 0)])
                if len(chunk) > remaining:
                    overflow.set()
        finally:
            stream.close()

    def terminate():
        if job:
            job.close()
        elif os.name == "nt":
            if process.poll() is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    try:
        if os.name == "nt":
            job = _WindowsJob(process)
        for stream, output, limit in ((process.stdout, buffers[0], MAX_STDOUT),
                                      (process.stderr, buffers[1], MAX_STDERR)):
            thread = threading.Thread(target=drain, args=(stream, output, limit), daemon=True)
            readers.append(thread)
            thread.start()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if overflow.is_set():
                raise PeerError("process_output_limit")
            if time.monotonic() >= deadline:
                raise PeerError("process_timeout")
            time.sleep(0.025)
        # A launcher can exit before a child closes inherited pipes.
        terminate()
        for thread in readers:
            thread.join(3)
        if any(thread.is_alive() for thread in readers):
            raise PeerError("process_cleanup_incomplete")
        if overflow.is_set():
            raise PeerError("process_output_limit")
        return subprocess.CompletedProcess(args, process.returncode, bytes(buffers[0]), bytes(buffers[1]))
    finally:
        terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _cli_failure_code(stdout, returncode):
    """Classify SDK JSON only; never forward its messages, paths or headers."""
    if returncode == 2:
        return "openclaw_timeout"
    try:
        value = json.loads(stdout)
    except (ValueError, UnicodeError, RecursionError):
        return "openclaw_failed"
    if not isinstance(value, dict):
        return "openclaw_failed"
    if value.get("status") == "timeout":
        return "openclaw_timeout"
    error = value.get("error")
    if not isinstance(error, dict):
        return "openclaw_failed"
    codes = {error.get(key) for key in ("type", "kind", "code") if isinstance(error.get(key), str)}
    for known, code in (
        ({"timeout", "TimeoutError", "request_timeout"}, "openclaw_timeout"),
        ({"config_invalid", "invalid_config", "config_validation", "config_error"}, "openclaw_config_invalid"),
        ({"authentication_error", "unauthorized", "invalid_api_key"}, "openclaw_auth_rejected"),
        ({"context_length_exceeded", "context_window_too_small"}, "openclaw_context_limit"),
        ({"rate_limit_exceeded", "rate_limit_error"}, "openclaw_rate_limited"),
    ):
        if codes & known:
            return code
    message = error.get("message")
    if isinstance(message, str) and len(message) <= 4096:
        # Fixed prefixes identify documented CLI failures; the suffix is ignored.
        for prefix, code in (
            ("Invalid config", "openclaw_config_invalid"),
            ("Config validation failed", "openclaw_config_invalid"),
            ("Model context window too small", "openclaw_context_limit"),
            ("No API key found for provider", "openclaw_auth_missing"),
        ):
            if message.startswith(prefix):
                return code
    return "openclaw_cli_rejected" if "cli_error" in codes else "openclaw_failed"


def validate_result(result, token):
    stdout, stderr = result.stdout, result.stderr
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        raise PeerError("invalid_process_result")
    if len(stdout) > MAX_STDOUT or len(stderr) > MAX_STDERR:
        raise PeerError("process_output_limit")
    if token.encode() in stdout or token.encode() in stderr:
        raise PeerError("credential_in_output")
    if result.returncode != 0:
        raise PeerError(_cli_failure_code(stdout, result.returncode))
    try:
        sdk = json.loads(stdout, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeError) as exc:
        raise PeerError("invalid_sdk_json") from exc
    if not isinstance(sdk, dict) or sdk.get("ok") is not True or sdk.get("status") != "ok":
        raise PeerError("unsuccessful_sdk_result")
    if sdk.get("provider") != PROVIDER or sdk.get("model") != MODEL:
        raise PeerError("unexpected_model_identity")
    final = sdk.get("final")
    if not isinstance(final, str) or not final.strip() or len(final.encode("utf-8")) > MAX_FINAL:
        raise PeerError("invalid_final")
    if sdk.get("codeModeEngaged") not in (None, False):
        raise PeerError("unexpected_tool_activity")
    summary = sdk.get("toolSummary")
    if summary is not None and (not isinstance(summary, dict) or type(summary.get("calls")) is not int
                                or summary.get("calls") != 0
                                or summary.get("tools", []) != []):
        raise PeerError("unexpected_tool_activity")
    turns = sdk.get("assistantTurns")
    if turns is not None and (type(turns) is not int or turns != 1):
        raise PeerError("unexpected_assistant_turns")
    if sdk.get("error") is not None:
        raise PeerError("unsuccessful_sdk_result")
    return sdk


def _write_new_json(path, value):
    # Exclusive creation never overwrites another receipt or operator file.
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")


def run_peer(*, node, package_root, base_url, prompt_file, output_file, home_dir,
             token, runner=run_bounded, ambient=None):
    node, package_root, entry = validate_installation(node, package_root)
    config = build_provider_config(base_url)
    home, prompt_file, output_file = map(_plain_path, (home_dir, prompt_file, output_file))
    env = build_environment(home, node, token, ambient)
    if home.exists() or output_file.exists():
        raise PeerError("run_path_already_exists")
    with prompt_file.open("rb") as stream:
        prompt = stream.read(MAX_PROMPT + 1)
    try:
        text = prompt.decode("utf-8")
    except UnicodeError as exc:
        raise PeerError("invalid_prompt") from exc
    if not text.strip() or len(prompt) > MAX_PROMPT or token in text:
        raise PeerError("invalid_prompt")
    home.mkdir(parents=True, exist_ok=False)
    for name in ("state", "agent", "workspace", "appdata", "localappdata", "tmp"):
        (home / name).mkdir()
    _write_new_json(home / "openclaw.json", config)
    local_prompt = home / "workspace" / "task.txt"
    local_prompt.write_bytes(prompt)
    # No .env exists in any loader-owned working/state/home directory.
    args = [str(node), str(entry), "agent", "exec", "--config", str(home / "openclaw.json"),
            "--cwd", str(home / "workspace"), "--state-dir", str(home / "state"),
            "--model", f"{PROVIDER}/{MODEL}", "--code-mode", "direct", "--thinking", "off",
            "--timeout", "240", "--message-file", str(local_prompt), "--json"]
    result = runner(args, cwd=str(home / "workspace"), env=env, timeout=TIMEOUT)
    sdk = validate_result(result, token)
    receipt = {"schema": 1, "harness": "openclaw", "package_version": VERSION,
               "model": MODEL, "provider": PROVIDER, "final": sdk["final"],
               "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
               "response_sha256": hashlib.sha256(sdk["final"].encode()).hexdigest(),
               "tool_activity_reported": "toolSummary" in sdk,
               "sdk": sdk}
    _write_new_json(output_file, receipt)
    return receipt


def make_role_runner(node, package_root, *, runner=run_bounded):
    """Return coordinator callback(stage,prompt,base_url,token,stage_dir)->str."""
    def role_runner(stage, prompt, base_url, token, stage_dir):
        if not isinstance(stage, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", stage):
            raise PeerError("invalid_stage")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT:
            raise PeerError("invalid_prompt")
        if isinstance(token, str) and token and token in prompt:
            raise PeerError("invalid_prompt")
        stage_dir = _plain_path(stage_dir)
        stage_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = stage_dir / "openclaw-prompt.txt"
        with prompt_file.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(prompt)
        result = run_peer(node=node, package_root=package_root, base_url=base_url,
                          prompt_file=prompt_file, output_file=stage_dir / "openclaw-result.json",
                          home_dir=stage_dir / "openclaw-home", token=token, runner=runner)
        return result["final"]
    return role_runner


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("node", "package-root", "base-url", "prompt-file", "output-file", "home-dir"):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args(argv)
    try:
        run_peer(**vars(args), token=os.environ.get("NEUROMORPH_TANDEM_TOKEN", ""))
    except (PeerError, OSError, ValueError) as exc:
        # Never print SDK stderr, arbitrary exception text, env or credentials.
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, PeerError) else "peer_io_error"}))
        return 1
    print(json.dumps({"ok": True, "output_written": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
