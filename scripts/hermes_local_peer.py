"""One real, source-pinned Hermes SDK turn against a caller-owned local gateway.

Run only as a dedicated child. The parent owns durable reservation, deduplication,
the model lock, gateway request validation, STOP and process lifetime. This Python
I/O guard is defense in depth, NOT an OS/native-code sandbox. It does not configure
or communicate with the user's running Hermes desktop process.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import ssl
import threading
import time
from urllib.parse import urlsplit

COMMIT = "54fb5a42e8ecf2bf0326f7bd829a292ee0965451"
MODEL = "Qwen3-14B-Q4_K_M"
CONTEXT_TOKENS = 8192
MAX_TOKENS = 512
MAX_PROMPT = 4000
MAX_TEXT = 65536
SDK_SECONDS = 310
WALL_SECONDS = 350
MANIFEST_SHA256 = "a67ef5d83592af65a276cebf84a1bd0e042294eee541046e84451bc42248108b"
MANIFEST = Path(__file__).resolve().parents[1] / "config" / "hermes_local_peer.json"
ADAPTER_POLICY = ["dotenv_loading_disabled", "isolated_profile", "no_tools_context_8192",
                  "supported_nonstreaming_profile"]


class PeerError(ValueError):
    def __init__(self, code, *, sdk_result=None, api_errors=(), api_exception_chains=()):
        super().__init__(code)
        self.sdk_result = sdk_result
        self.api_errors = list(api_errors)[:4]
        self.api_exception_chains = list(api_exception_chains)[:4]


_AUDIT_CODES = frozenset({"credential_file_blocked", "external_file_read_blocked", "external_write_blocked",
    "external_connection_blocked", "external_dns_blocked", "runtime_execution_blocked",
    "filesystem_link_blocked", "source_bytecode_disabled", "path_type_blocked"})
_AUDIT_EVENTS = frozenset({"open", "socket.connect", "socket.connect_ex", "socket.getaddrinfo",
    "socket.bind", "socket.sendto", "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
    "pty.spawn", "os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate", "os.utime",
    "os.rename", "os.symlink", "os.link", "sqlite3.connect"})
_EXCEPTION_KINDS = frozenset({"APITimeoutError", "APIConnectionError", "APIStatusError", "BadRequestError",
    "PermissionError", "FileNotFoundError", "ReadTimeout", "WriteTimeout", "ConnectTimeout", "PoolTimeout",
    "ReadError", "WriteError", "ConnectError", "RemoteProtocolError", "LocalProtocolError", "TimeoutError",
    "RuntimeError", "ValueError", "TypeError", "OSError", "InterruptedError", "EmptyStreamError"})


def exception_chain(exc):
    """Types and fixed guard codes only; never exception text or request objects."""
    result, seen = [], set()
    while exc is not None and id(exc) not in seen and len(result) < 6:
        seen.add(id(exc))
        kind = type(exc).__name__
        item = {"kind": kind if kind in _EXCEPTION_KINDS else "other"}
        if type(exc) in (PermissionError, FileNotFoundError) and str(exc) in _AUDIT_CODES:
            item["guard_code"] = str(exc)
        result.append(item)
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    return result


def bounded_exception_chains(value):
    if type(value) is not list:
        return []
    result = []
    for chain in value[:4]:
        if type(chain) is not list:
            continue
        items = []
        for item in chain[:6]:
            if (type(item) is not dict or type(item.get("kind")) is not str
                    or item["kind"] not in _EXCEPTION_KINDS | {"other"}):
                continue
            clean = {"kind": item["kind"]}
            if type(item.get("guard_code")) is str and item["guard_code"] in _AUDIT_CODES:
                clean["guard_code"] = item["guard_code"]
            items.append(clean)
        result.append(items)
    return result


def bounded_audit_denials(value):
    if type(value) is not list:
        return []
    return [{"event": item["event"], "reason": item["reason"], "count": item["count"]}
        for item in value[:128] if type(item) is dict
        and type(item.get("event")) is str and item["event"] in _AUDIT_EVENTS
        and type(item.get("reason")) is str and item["reason"] in _AUDIT_CODES
        and type(item.get("count")) is int and 1 <= item["count"] <= 1000]


_ERROR_CODES = frozenset({"auth", "auth_permanent", "billing", "rate_limit", "upstream_rate_limit",
    "upstream_blocked", "overloaded", "server_error", "timeout", "ssl_cert_verification",
    "context_overflow", "payload_too_large", "image_too_large", "image_corrupt", "model_not_found",
    "provider_policy_blocked", "content_policy_blocked", "model_entitlement", "incomplete_response",
    "format_error", "role_alternation", "invalid_encrypted_content", "multimodal_tool_content_unsupported",
    "reasoning_mandatory", "thinking_signature", "long_context_tier", "oauth_long_context_beta_forbidden",
    "llama_cpp_grammar_pattern", "unknown", "interpreter_shutdown"})


def bounded_sdk_result(value):
    """Closed diagnostic projection; no answer, request, message, URL or credentials."""
    if type(value) is not dict:
        return {"result_was_object": False}
    result = {"result_was_object": value.get("result_was_object", True)
              if type(value.get("result_was_object", True)) is bool else True}
    for key in ("completed", "interrupted", "failed", "partial", "compression_deferred",
                "compression_exhausted", "failure_retryable"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    if type(value.get("api_calls")) is int and 0 <= value["api_calls"] <= 16:
        result["api_calls"] = value["api_calls"]
    if isinstance(value.get("failure_reason"), str) and value["failure_reason"] in _ERROR_CODES:
        result["failure_reason"] = value["failure_reason"]
    for key in ("status_code", "http_status"):
        if type(value.get(key)) is int and 100 <= value[key] <= 599:
            result[key] = value[key]
    return result


def bounded_api_error(value):
    result = {}
    if type(value.get("error_type")) is str and value["error_type"] in _EXCEPTION_KINDS:
        result["error_type"] = value["error_type"]
    if isinstance(value.get("reason"), str) and value["reason"] in _ERROR_CODES:
        result["reason"] = value["reason"]
    if type(value.get("status_code")) is int and 100 <= value["status_code"] <= 599:
        result["status_code"] = value["status_code"]
    for key in ("api_call_count", "retry_count", "max_retries"):
        if type(value.get(key)) is int and 0 <= value[key] <= 16:
            result[key] = value[key]
    return result


def require(condition, code):
    if not condition:
        raise PeerError(code)


def no_links(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            require(not stat.S_ISLNK(info.st_mode)
                    and not (getattr(info, "st_file_attributes", 0) & 0x400),
                    "linked_path_rejected")
    return path


def loopback_url(value):
    require(type(value) is str and re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/v1", value),
            "loopback_url_required")
    port = urlsplit(value).port
    require(port is not None and 1024 <= port <= 65535, "loopback_port_rejected")
    return port


def read_prompt(path):
    no_links(path)
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_PROMPT * 4 + 1)
    require(len(raw) <= MAX_PROMPT * 4, "prompt_limit")
    try:
        prompt = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PeerError("invalid_prompt_encoding") from exc
    require(1 <= len(prompt) <= MAX_PROMPT and prompt.strip() and "\0" not in prompt,
            "prompt_limit")
    return prompt


def profile(base_url):
    loopback_url(base_url)
    # This is the REAL local context, not the upstream 64K tool-calling default.
    return {
        "model": {"default": MODEL, "provider": "custom", "base_url": base_url,
                  "api_key": "no-key-required", "context_length": CONTEXT_TOKENS,
                  "streaming": False},
        "providers": {}, "fallback_providers": [], "mcp_servers": {}, "toolsets": [],
        "platform_toolsets": {"cli": []}, "plugins": {"enabled": []},
        "agent": {"max_turns": 1, "api_max_retries": 1, "auto_recovery_cycles": 0,
                  "environment_probe": False, "bot_mode_protocol": False,
                  "parallel_tool_call_guidance": False, "task_completion_guidance": False,
                  "stall_guards": False},
        "compression": {"enabled": False, "micro_compact": False},
        "memory": {"memory_enabled": False, "user_profile_enabled": False, "provider": ""},
        "skills": {"auto_load": []},
        "auxiliary": {"free_only": True, "transient_retries": 0,
                      "title_generation": {"enabled": False, "model_upgrade_enabled": False},
                      "background_review": {"enabled": False}},
        "security": {"allow_lazy_installs": False}, "updates": {"check": False},
        "checkpoints": {"enabled": False},
    }


def fresh_environment(home, python=sys.executable):
    home = no_links(home)
    require(not home.exists(), "fresh_home_required")
    home.mkdir(mode=0o700)
    for name in ("tmp", "config", "cache", "data", "work", "managed", "bundled-plugins"):
        (home / name).mkdir(mode=0o700)
    env = {
        "PATH": str(Path(python).parent), "HOME": str(home), "USERPROFILE": str(home),
        "HERMES_HOME": str(home), "HERMES_MANAGED_DIR": str(home / "managed"),
        "HERMES_BUNDLED_PLUGINS": str(home / "bundled-plugins"),
        "LOCALAPPDATA": str(home / "data"), "APPDATA": str(home / "config"),
        "TMP": str(home / "tmp"), "TEMP": str(home / "tmp"), "TMPDIR": str(home / "tmp"),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_DATA_HOME": str(home / "data"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_DISABLE_LAZY_INSTALLS": "1", "HERMES_SINGLE_QUERY_SESSION": "1",
        "HERMES_ENABLE_PROJECT_PLUGINS": "0", "DO_NOT_TRACK": "1",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
    }
    for name in ("SystemRoot", "WINDIR"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_source(source, env):
    """Validate accepted source metadata; no user config or credentials are opened."""
    source = no_links(source)
    raw = MANIFEST.read_bytes()
    require(len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == MANIFEST_SHA256,
            "manifest_pin_mismatch")
    manifest = json.loads(raw)
    require(manifest.get("source_commit") == COMMIT, "source_commit_mismatch")
    for name, expected in manifest["file_sha256"].items():
        require(re.fullmatch(r"[A-Za-z0-9_./-]+", name) and ".." not in name.split("/"),
                "manifest_path_rejected")
        path = no_links(source / name)
        require(path.is_file() and _sha(path) == expected, "source_file_changed")
    git = shutil.which("git")
    require(git is not None, "git_missing")
    for args, expected in ((["rev-parse", "HEAD"], COMMIT),
                           (["status", "--porcelain", "--untracked-files=no"], "")):
        result = subprocess.run(
            [git, "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
             "-C", str(source), *args], env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
        require(result.returncode == 0 and result.stdout.strip() == expected,
                "source_checkout_changed")
    return manifest


def audit_guard(port, *, home, source, output, read_roots=(), diagnostics=None):
    """Restricted Python I/O only; native extensions are not OS-sandboxed."""
    home, source, output = (Path(p).resolve() for p in (home, source, output))
    roots = (home, source, *(Path(p).resolve() for p in read_roots))
    # httpx/OpenSSL and platformdirs may inspect public OS metadata while
    # constructing a local client. Permit only fixed, non-secret files and the
    # system CA bundle; credentials and arbitrary external files remain blocked.
    public_system_files = {
        Path("/etc/hosts"), Path("/etc/resolv.conf"), Path("/etc/nsswitch.conf"),
        Path("/etc/os-release"), Path("/etc/localtime"), Path("/etc/machine-id"),
    }
    public_system_roots = {Path("/etc/ssl/certs")}
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\\Windows"))
        public_system_files.update({
            system_root / "System32" / "drivers" / "etc" / "hosts",
            system_root / "System32" / "drivers" / "etc" / "services",
        })
    public_system_files = {p.absolute() for p in public_system_files}
    public_system_roots = {p.absolute() for p in public_system_roots}
    # venv packages can resolve through a symlink into the interpreter's
    # stdlib/SSL installation. Derive those read-only roots instead of opening
    # an unrestricted /usr or user directory.
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = sysconfig.get_path(key)
        if value:
            public_system_roots.add(Path(value).absolute())
    with contextlib.suppress(Exception):
        verify = ssl.get_default_verify_paths()
        for value in (verify.cafile, verify.openssl_cafile, verify.capath, verify.openssl_capath):
            if value:
                path = Path(value).absolute()
                public_system_roots.add(path if path.is_dir() else path.parent)

    def within(path, root):
        return path == root or root in path.parents

    def path_for(value):
        if isinstance(value, int):
            return None  # already-open stdio and internal pipes
        if not isinstance(value, (str, bytes, os.PathLike)):
            raise PermissionError("path_type_blocked")
        return Path(os.fsdecode(value)).absolute()

    def checked_path(value, *, writing=False):
        path = path_for(value)
        if path is None:
            return
        if path == Path(os.devnull).absolute():
            return
        leaf = path.name.lower()
        if (leaf == ".env" or leaf.endswith(".env") or leaf.startswith(".env.")
                or leaf in {"auth.json", "credentials.json", "tokens.json", "token.json"}):
            raise PermissionError("credential_file_blocked")
        # Never execute an unverified bytecode cache left beside the accepted source.
        if within(path, source) and path.suffix.lower() in {".pyc", ".pyo"}:
            raise FileNotFoundError("source_bytecode_disabled")
        resolved = path.resolve()
        if writing:
            if not (within(resolved, home) or resolved == output):
                raise PermissionError("external_write_blocked")
        elif (not any(within(resolved, root) for root in roots)
              and resolved not in public_system_files
              and not any(within(resolved, root) for root in public_system_roots)):
            raise PermissionError("external_file_read_blocked")

    def guard(event, args):
        if event in {"socket.connect", "socket.connect_ex"}:
            address = args[1]
            if not (type(address) is tuple and len(address) == 2
                    and address[0] == "127.0.0.1" and address[1] == port):
                raise PermissionError("external_connection_blocked")
        elif event == "socket.getaddrinfo":
            if args[0] != "127.0.0.1" or args[1] not in (port, str(port)):
                raise PermissionError("external_dns_blocked")
        elif event in {"socket.bind", "socket.sendto", "subprocess.Popen", "os.system",
                       "os.exec", "os.posix_spawn", "pty.spawn"}:
            raise PermissionError("runtime_execution_blocked")
        elif event == "open":
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT
                                                         | os.O_TRUNC | os.O_APPEND)))
            checked_path(args[0], writing=writing)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate", "os.utime"}:
            checked_path(args[0], writing=True)
        elif event == "os.rename":
            checked_path(args[0], writing=True)
            checked_path(args[1], writing=True)
        elif event in {"os.symlink", "os.link"}:
            raise PermissionError("filesystem_link_blocked")
        elif event == "sqlite3.connect" and args[0] != ":memory:":
            checked_path(args[0], writing=True)
    def observed_guard(event, args):
        try:
            guard(event, args)
        except (PermissionError, FileNotFoundError) as exc:
            code = str(exc)
            if diagnostics is not None and code in _AUDIT_CODES:
                key = (event, code)
                diagnostics[key] = min(1000, diagnostics.get(key, 0) + 1)
            raise
    return observed_guard


class BoundedSink(io.TextIOBase):
    def __init__(self):
        self.count = 0

    def writable(self):
        return True

    def write(self, text):
        self.count += len(str(text).encode("utf-8", errors="replace"))
        require(self.count <= 131072, "sdk_log_limit")
        return len(text)


def install_profile_adaptations(base_url):
    """Explicit no-tools adapter; source files and desktop config remain unchanged.

    Upstream loads source/.env at import and applies a 64K tool-calling minimum
    even with zero tools. Disable dotenv, and relax ONLY that exact no-tools,
    custom/loopback/8192 route. No model/conversation/client implementation is mocked.
    """
    loopback_url(base_url)
    from hermes_cli import env_loader
    env_loader.load_hermes_dotenv = lambda **_kwargs: []
    from agent import agent_init
    original = agent_init._enforce_minimum_context

    def enforce(agent):
        if (agent.provider == "custom" and agent.base_url == base_url and agent.model == MODEL
                and agent.tools == [] and not agent.valid_tool_names
                and agent._config_context_length == CONTEXT_TOKENS
                and getattr(agent.context_compressor, "context_length", None) == CONTEXT_TOKENS
                and agent.compression_enabled is False):
            return
        return original(agent)

    agent_init._enforce_minimum_context = enforce


def run_sdk(prompt, base_url, home, factory, *, token):
    loopback_url(base_url)
    require(type(prompt) is str and 1 <= len(prompt) <= MAX_PROMPT
            and prompt.strip() and "\0" not in prompt, "prompt_limit")
    require(type(token) is str and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token),
            "ephemeral_gateway_token_required")
    agent = None
    try:
        agent = factory(
            base_url=base_url, api_key=token, provider="custom",
            requested_provider="custom", api_mode="chat_completions", model=MODEL,
            enabled_toolsets=[], disabled_toolsets=[], max_iterations=1, max_tokens=MAX_TOKENS,
            skip_context_files=True, load_soul_identity=False, skip_memory=True,
            skip_background_review=True, session_db=None, fallback_model=None,
            credential_pool=None, checkpoints_enabled=False, save_trajectories=False,
            verbose_logging=False, quiet_mode=True, stream_delta_callback=None,
            run_budget_seconds=SDK_SECONDS, cwd=str(Path(home) / "work"),
            request_overrides={"stream": False},
        )
        require(getattr(agent, "tools", None) == []
                and not getattr(agent, "valid_tool_names", None), "nonempty_tools_rejected")
        require(getattr(agent, "compression_enabled", None) is False,
                "compression_not_disabled")
        require(getattr(agent, "_fallback_chain", None) == []
                and getattr(agent, "_credential_pool", None) is None, "fallback_rejected")
        require(agent.base_url == base_url and agent.model == MODEL
                and agent.provider == "custom", "sdk_route_changed")
        client = getattr(agent, "client", None)
        require(client is not None and hasattr(client, "max_retries"), "unexpected_sdk_client")
        client.max_retries = 0
        client.timeout = SDK_SECONDS
        kwargs = getattr(agent, "_client_kwargs", None)
        require(type(kwargs) is dict, "unexpected_sdk_client")
        kwargs["max_retries"] = 0
        kwargs["timeout"] = SDK_SECONDS
        agent.suppress_status_output = True
        # Pinned SDK agent_init._apply_display_config officially maps
        # model.streaming=false to this per-session flag. request_overrides
        # alone does NOT select non-streaming in turn_api_call._should_stream.
        agent._disable_streaming = True
        api_errors = []
        api_exception_chains = []
        def observe_call(original):
            def call(*args, **kwargs):
                try:
                    return original(*args, **kwargs)
                except Exception as exc:
                    if len(api_exception_chains) < 4:
                        api_exception_chains.append(exception_chain(exc))
                    raise
            return call
        for method in ("_interruptible_api_call", "_interruptible_streaming_api_call"):
            original = getattr(agent, method, None)
            if callable(original):
                setattr(agent, method, observe_call(original))
        original_error_hook = getattr(agent, "_invoke_api_request_error_hook", None)
        if callable(original_error_hook):
            def error_hook(**details):
                if len(api_errors) < 4:
                    api_errors.append(bounded_api_error(details))
                return original_error_hook(**details)
            agent._invoke_api_request_error_hook = error_hook
        result = agent.run_conversation(prompt)
        if not (type(result) is dict and result.get("completed") is True
                and not result.get("interrupted") and not result.get("partial")
                and not result.get("failed")):
            raise PeerError("sdk_incomplete", sdk_result=bounded_sdk_result(result), api_errors=api_errors,
                            api_exception_chains=api_exception_chains)
        require(type(result.get("api_calls")) is int and result["api_calls"] == 1,
                "sdk_call_count_rejected")
        text = result.get("final_response")
        require(type(text) is str and text.strip() and token not in text
                and len(text.encode("utf-8")) <= MAX_TEXT, "sdk_response_rejected")
        return {"status": "response_received", "text": text, "model": MODEL,
                "source_commit": COMMIT, "api_calls": 1, "tools": [],
                "adapter_policy": list(ADAPTER_POLICY), "context_tokens": CONTEXT_TOKENS,
                "max_tokens": MAX_TOKENS, "output_execution": False,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
    finally:
        if agent is not None:
            agent.close()


def safe_failure(exc, stage="preflight"):
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        frames.append({"file": re.sub(r"[^A-Za-z0-9_.-]", "_",
                       Path(trace.tb_frame.f_code.co_filename).name)[:80],
                       "line": trace.tb_lineno})
        trace = trace.tb_next
    safe_codes = {"credential_file_blocked", "external_file_read_blocked", "external_write_blocked",
                  "external_connection_blocked", "external_dns_blocked", "runtime_execution_blocked",
                  "filesystem_link_blocked", "source_bytecode_disabled", "path_type_blocked"}
    reason = str(exc) if type(exc) is PeerError else "sdk_failed"
    if isinstance(exc, PermissionError) and str(exc) in safe_codes:
        reason = str(exc)
    result = {"status": "failed", "reason": reason, "stage": stage,
            "failure_kind": re.sub(r"[^A-Za-z0-9_]", "_", type(exc).__name__)[:64],
            "model": MODEL, "source_commit": COMMIT, "tools": [],
            "output_execution": False, "adapter_policy": list(ADAPTER_POLICY), "frames": frames[-6:]}
    if isinstance(exc, ModuleNotFoundError) and isinstance(exc.name, str) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]{0,119}", exc.name):
        result["missing_module"] = exc.name
    if type(exc) is PeerError and exc.sdk_result is not None:
        result["sdk_result"] = bounded_sdk_result(exc.sdk_result)
        result["api_errors"] = [bounded_api_error(item) for item in exc.api_errors if type(item) is dict][:4]
        result["api_exception_chains"] = bounded_exception_chains(exc.api_exception_chains)
    return result


def _watchdog(done, stop_file):
    deadline = time.monotonic() + WALL_SECONDS
    while not done.wait(0.2):
        if stop_file is not None and stop_file.exists():
            os._exit(125)  # parent records interrupted/unknown; never repeats the reserved call
        if time.monotonic() >= deadline:
            os._exit(124)


def execute(source_root, base_url, prompt_file, output_file, home_dir, stop_file=None):
    require(sys.version_info[:2] == (3, 14), "python_314_required")
    require(sys.platform in {"win32", "linux"}, "platform_not_verified")
    source, prompt_path, output, home = (no_links(v) for v in
                                       (source_root, prompt_file, output_file, home_dir))
    stop = no_links(stop_file) if stop_file else None
    require(output.parent.exists() and output.parent == home.parent == prompt_path.parent
            and not output.exists() and not home.exists(), "fresh_job_paths_required")
    require(source not in home.parents and home not in source.parents and source != home,
            "source_home_overlap")
    require(stop is None or (stop.parent == output.parent and stop.name == "STOP"),
            "stop_path_rejected")
    require(stop is None or not stop.exists(), "stopped_before_sdk")
    port = loopback_url(base_url)
    prompt = read_prompt(prompt_path)
    token = os.environ.get("NEUROMORPH_TANDEM_TOKEN", "")
    require(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token), "ephemeral_gateway_token_required")
    env = fresh_environment(home)
    verify_source(source, env)
    (home / "config.yaml").write_text(json.dumps(profile(base_url), ensure_ascii=False, indent=2)
                                     + "\n", encoding="utf-8")
    os.environ.clear()
    os.environ.update(env)
    os.chdir(home / "work")
    # Keep only interpreter/stdlib/venv search roots, never a caller's work directory.
    prefixes = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]
    # uv may expose a lexical venv site-packages entry whose resolved target is
    # in its immutable package cache. Keep that exact resolved import root in
    # the read set; do not admit arbitrary cwd/user paths.
    import_roots = []
    for value in tuple(sys.path):
        if not value:
            continue
        candidate = Path(value).resolve()
        if candidate.is_dir() and ("site-packages" in value or "python" in value.lower()
                                   or any(candidate == root or root in candidate.parents for root in prefixes)):
            import_roots.append(candidate)
    safe_paths = [p for p in sys.path if p and any(Path(p).resolve() == x
                  or x in Path(p).resolve().parents for x in prefixes)]
    sys.path[:] = [str(source), *safe_paths]
    sys.dont_write_bytecode = True
    audit_denials = {}
    sys.addaudithook(audit_guard(port, home=home, source=source, output=output,
                                read_roots=(*prefixes, *import_roots), diagnostics=audit_denials))
    done = threading.Event()
    monitor = threading.Thread(target=_watchdog, args=(done, stop), daemon=True)
    monitor.start()
    sink = BoundedSink()
    stage = "adapting_sdk"
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            install_profile_adaptations(base_url)
            stage = "importing_sdk"
            from run_agent import AIAgent
            stage = "running_sdk"
            result = run_sdk(prompt, base_url, home, AIAgent, token=token)
    except Exception as exc:
        result = safe_failure(exc, stage)
    finally:
        done.set()
        monitor.join(timeout=1)
    result["sdk_log_bytes"] = sink.count
    result["audit_denials"] = [{"event": event, "reason": reason, "count": count}
                              for (event, reason), count in sorted(audit_denials.items())]
    result["source_manifest_sha256"] = MANIFEST_SHA256
    with output.open("x", encoding="utf-8") as target:
        target.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("source-root", "base-url", "prompt-file", "output-file", "home-dir"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--stop-file")
    args = parser.parse_args(argv)
    try:
        result = execute(args.source_root, args.base_url, args.prompt_file, args.output_file,
                         args.home_dir, args.stop_file)
    except Exception as exc:
        result = safe_failure(exc)
    print(json.dumps({key: value for key, value in result.items() if key != "text"}, ensure_ascii=False))
    return 0 if result["status"] == "response_received" else 1


if __name__ == "__main__":
    raise SystemExit(main())
