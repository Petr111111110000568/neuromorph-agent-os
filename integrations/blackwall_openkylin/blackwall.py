#!/usr/bin/env python3
"""Read-only openKylin swarm component inventory and bounded local integration.

Pure Python 3.11+ stdlib. No cloud calls, credential reads, arbitrary command
execution, model inference, or downloads. All probes use known loopback URLs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
from urllib import error, request

BASE_DIR = Path(__file__).resolve().parent
COMPONENT_COMMANDS = {
    "clawteam": ("clawteam",),
    "swarmclaw": ("swarmclaw",),
    "openclaw": ("openclaw",),
    "openclaw_enterprise": ("openclaw-enterprise",),
    "hermes": ("hermes", "hermes-agent"),
    "deepseek_harness": ("deepseek-harness", "deepseek"),
    "qwen_studio": ("qwen-studio", "qwen", "qwen-code"),
    "kimi_runtime": ("k3", "kimi", "kimi-k3"),
    "kylinbot": ("kylinbot", "kylin-bot", "kylin-botshell"),
    "ollama": ("ollama",),
    "llama_cpp_server": ("llama-server",),
    "vllm": ("vllm",),
    "podman": ("podman",),
    "docker": ("docker",),
    "tmux": ("tmux",),
    "git": ("git",),
    "node": ("node",),
    "npm": ("npm",),
    "ffmpeg": ("ffmpeg",),
    "whisper": ("whisper",),
}
WEIGHT_SUFFIXES = {".gguf", ".safetensors", ".onnx", ".pt", ".pth", ".tflite", ".mlmodel"}
SKIP_DIRS = {".git", ".venv", "node_modules", ".tox", "dist", "build", "__pycache__"}
# Fixed, strictly localhost probe set; no URL provided by a model or file.
LOCAL_PROBES = {
    "meta_harness": ("http://127.0.0.1:8765/api/status", "json"),
    "ollama": ("http://127.0.0.1:11434/api/tags", "json"),
    "kimi_gateway": ("http://127.0.0.1:8090/v1/models", "json"),
    "lm_studio": ("http://127.0.0.1:1234/v1/models", "json"),
    "swarmclaw_ui": ("http://127.0.0.1:3456/", "http"),
}

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    result = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            if re.fullmatch(r"[A-Z_]+", k):
                result[k] = v.strip().strip('"')
    except (OSError, UnicodeError):
        pass
    return result

def detect_binaries(which=shutil.which) -> dict[str, dict]:
    output = {}
    for name, aliases in COMPONENT_COMMANDS.items():
        chosen = next(((alias, which(alias)) for alias in aliases if which(alias)), None)
        output[name] = {"state": "path_detected" if chosen else "not_detected",
                        "alias": chosen[0] if chosen else None,
                        "executable": chosen[1] if chosen else None,
                        "tested": False}
    return output

def default_spots(home: Path) -> list[Path]:
    return [home / "Spaces", home / "Models", home / "models",
            home / ".cache" / "huggingface" / "hub", home / ".ollama" / "models"]

def looks_like_model(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in WEIGHT_SUFFIXES:
        return True
    return suffix == ".bin" and bool(re.search(r"model|pytorch|weights", path.name, re.I))

def scan_models(roots: list[Path], max_files: int = 5000, max_depth: int = 5,
                max_matches: int = 250, *, max_dirs: int = 1000) -> dict:
    """Stat only; cap directories yielded by os.walk across all roots.

    Reaching max_dirs conservatively marks the scan truncated, even when
    the last permitted directory happens to be the last one in the tree.
    This is not a bound on individual directory width or filesystem latency.
    """
    if type(max_dirs) is not int or max_dirs < 0:
        raise ValueError("max_dirs must be a non-negative integer")
    seen = set()
    found = []
    examined = 0
    visited_dirs = 0
    skipped = []
    truncated = False
    for root in roots:
        if visited_dirs >= max_dirs:
            truncated = True
            break
        root = Path(root).expanduser()
        if not root.is_dir() or root.is_symlink():
            skipped.append(str(root))
            continue
        canonical = str(root.resolve())
        if canonical in seen:
            continue
        seen.add(canonical)
        for here, dirs, files in os.walk(root, topdown=True, followlinks=False):
            visited_dirs += 1
            here_path = Path(here)
            depth = len(here_path.relative_to(root).parts)
            dirs[:] = [d for d in sorted(dirs) if depth < max_depth and
                       d not in SKIP_DIRS and not d.startswith(".") and
                       not (here_path / d).is_symlink()]
            for filename in sorted(files):
                examined += 1
                if examined > max_files or len(found) >= max_matches:
                    truncated = True
                    break
                candidate = here_path / filename
                if candidate.is_symlink() or not looks_like_model(candidate):
                    continue
                try:
                    stat = candidate.stat()
                except OSError:
                    continue
                found.append({"path": str(candidate), "format": candidate.suffix.lower().lstrip("."),
                              "bytes": stat.st_size, "state": "file_detected_not_validated"})
            # Stop before advancing the generator: the next yield would
            # already have enumerated one more directory.
            if truncated or visited_dirs >= max_dirs:
                truncated = True
                break
        if truncated:
            break
    return {"items": found, "examined": examined, "truncated": truncated,
            "visited_dirs": visited_dirs,
            "skipped_roots": skipped, "models_loaded": False}

def probe_local_services(timeout=0.75) -> dict:
    output = {}
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    for name, (url, kind) in LOCAL_PROBES.items():
        result = {"state": "not_verified", "endpoint": url, "models": [],
                  "authenticated": False}
        req = request.Request(url, method="GET", headers={"Accept": "application/json"})
        try:
            with opener.open(req, timeout=timeout) as response:
                raw = response.read(128 * 1024 + 1)
                if len(raw) > 128 * 1024:
                    result["state"] = "response_too_large"
                elif kind == "json":
                    data = json.loads(raw.decode("utf-8"))
                    if isinstance(data, dict):
                        result["state"] = "responding"
                        entries = data.get("models", [])
                        if isinstance(entries, list):
                            result["models"] = [str(e.get("name", e.get("id", "")))[:120]
                                                for e in entries[:80] if isinstance(e, dict)]
                    else:
                        result["state"] = "unexpected_body"
                else:
                    result["state"] = "responding"
        except error.HTTPError as exc:
            result["state"] = "http_error"
            result["http_status"] = exc.code
        except (OSError, error.URLError, ValueError, UnicodeError):
            # Do not serialize exception payloads: HTTP clients may include secrets.
            result["state"] = "unreachable"
        output[name] = result
    return output

def find_project_root(provided: str | None) -> Path | None:
    roots = [Path(provided).expanduser()] if provided else [Path.cwd(), Path.home() / "neuromorph-agent-os"]
    for root in roots:
        if root.is_dir() and (root / "workbench" / "__main__.py").is_file() and (root / "config" / "resource_policy.json").is_file():
            return root.resolve()
    return None

def read_project_policy(project_root: Path | None) -> dict:
    if not project_root:
        return {"state": "project_not_detected"}
    try:
        cfg = json.loads((project_root / "config/resource_policy.json").read_text("utf-8"))
        spend = cfg.get("daily_spend_limit_usd")
        paid = cfg.get("paid_model_calls_allowed")
        return {"state": "read", "project_root": str(project_root),
                "daily_spend_limit_usd": spend,
                "paid_model_calls_allowed": paid,
                "safe_zero_spend": spend == 0 and paid is False}
    except (OSError, ValueError, TypeError):
        return {"state": "invalid_policy", "safe_zero_spend": False}

def inventory(spaces: list[str], project: str | None,
              include_default: bool = True, network: bool = True) -> dict:
    roots = ([*default_spots(Path.home())] if include_default else []) + [Path(p) for p in spaces]
    system = os_release()
    project_root = find_project_root(project)
    return {"schema_version": 1, "checked_at_utc": utcnow(),
            "host": platform.node(),
            "system": {"id": system.get("ID", "unknown"), "version": system.get("VERSION_ID", "unknown"),
                       "arch": platform.machine(), "is_openkylin": "openkylin" in system.get("ID", "").lower(),
                       "python": platform.python_version()},
            "installed_commands": detect_binaries(),
            "models_on_disk": scan_models(roots),
            "local_services": probe_local_services() if network else {"state": "not_probed"},
            "meta_harness_policy": read_project_policy(project_root),
            "access": {"cloud_model_calls": "not_configured_by_this_tool",
                       "mcp_tools_granted": False, "peer_to_peer_enabled": False,
                       "agent_commands_executed": False},
            "limitations": ["Executable detection is not a successful agent session",
                            "File extension does not prove valid weights or loadable format",
                            "Loopback HTTP response is not model inference or authentication",
                            "This tool has no access to unmounted Spaces or a disconnected PC"]}

def write_private_json(target: Path, value: dict):
    target = target.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("Refusing to overwrite symlink")
    temporary = target.with_name(target.name + ".tmp-" + str(os.getpid()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    target.chmod(0o600)

def offline_mcp_configuration(project_root: Path) -> dict:
    launcher = project_root / "scripts" / "run_offline_mcp.py"
    if not launcher.is_file() or not (project_root / "workbench" / "__main__.py").is_file():
        raise ValueError("Verified Meta-Harness checkout required (offline MCP launcher absent)")
    # The client must explicitly import this template; creation does not execute it.
    return {"mcpServers": {"neuromorph_offline": {
        "command": sys.executable,
        "args": ["-I", str(launcher.resolve()), "--data-dir", str(project_root / "state")],
        "env": {}}}}

def submit_local_plan(question: str, execute: bool) -> dict:
    if not (1 <= len(question) <= 2000) or "\x00" in question:
        raise ValueError("Question must have 1..2000 characters without NUL")
    if not execute:
        return {"state": "dry_run", "action": "POST /api/plan (loopback only)", "question": question}
    body = json.dumps({"question": question}).encode("utf-8")
    req = request.Request("http://127.0.0.1:8765/api/plan", data=body,
                          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.build_opener(request.ProxyHandler({}), NoRedirect()).open(req, timeout=5) as resp:
            raw = resp.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                raise ValueError("Response limit exceeded")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Invalid response")
            return {"state": "controller_replied", "response": payload}
    except error.HTTPError as exc:
        return {"state": "controller_http_error", "status": exc.code}
    except error.URLError:
        return {"state": "controller_unreachable"}

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Blackwall/OpenKylin bounded agent swarm integration")
    subs = parser.add_subparsers(dest="command", required=True)
    d = subs.add_parser("doctor", help="inspect local components and optional Spaces folders")
    d.add_argument("--spaces", action="append", default=[], help="Local Spaces directory; repeatable")
    d.add_argument("--project", help="Path to verified Meta-Harness checkout")
    d.add_argument("--no-defaults", action="store_true", help="Scan only explicit --spaces paths")
    d.add_argument("--offline", action="store_true", help="Do not probe even localhost URLs")
    d.add_argument("--output", help="Write private JSON report (600 permissions)")
    m = subs.add_parser("mcp-config", help="generate offline-only stdio MCP client template")
    m.add_argument("--project", required=True)
    m.add_argument("--output", required=True)
    p = subs.add_parser("plan", help="submit a research plan to existing Meta-Harness controller")
    p.add_argument("--question", required=True)
    p.add_argument("--execute", action="store_true", help="actually send one request to local controller")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = inventory(args.spaces, args.project, not args.no_defaults, not args.offline)
            if args.output:
                write_private_json(Path(args.output), result)
                print("Report saved to", args.output)
            else:
                print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.command == "mcp-config":
            root = find_project_root(args.project)
            if root is None:
                raise ValueError("Not a Meta-Harness checkout")
            value = offline_mcp_configuration(root)
            write_private_json(Path(args.output), value)
            print("MCP template saved to", args.output, "(not activated)")
        elif args.command == "plan":
            print(json.dumps(submit_local_plan(args.question, args.execute), indent=2, ensure_ascii=False))
    except (OSError, ValueError) as exc:
        print("ERROR:", type(exc).__name__, str(exc), file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

