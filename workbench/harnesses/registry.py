"""Explicit local executable pins and truthful harness lifecycle status."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from ..autonomy.providers import json_load
from . import runner


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_digest(path):
    """Pin installed runtime files, including names and contained symlink targets."""
    root = Path(path).resolve()
    digest, total, count = hashlib.sha256(), 0, 0
    for item in sorted(root.rglob("*")):
        count += 1
        if count > 50000:
            raise ValueError("Runtime file count limit exceeded")
        relative = item.relative_to(root).as_posix()
        if item.is_symlink():
            if not item.resolve().is_relative_to(root):
                raise ValueError("Runtime symlink escapes installed tree")
            entry = [relative, "symlink", os.readlink(item)]
        elif item.is_file():
            total += item.stat().st_size
            if total > 1024 * 1024 * 1024:
                raise ValueError("Runtime size limit exceeded")
            entry = [relative, "file", file_digest(item)]
        elif item.is_dir():
            continue
        else:
            raise ValueError("Unexpected runtime file type")
        digest.update(json.dumps(entry, ensure_ascii=True, separators=(",", ":")).encode() + b"\n")
    return digest.hexdigest()


class HarnessRegistry:
    def __init__(self, root, registry_path=None):
        self.root = Path(root).resolve()
        self.path = Path(registry_path) if registry_path else self.root / "config/harnesses.json"

    def _entries(self):
        with self.path.open("rb") as stream:
            config = json_load(stream.read(100001))
        if not isinstance(config, dict) or config.get("schema_version") != 1 or not isinstance(config.get("harnesses"), list):
            raise ValueError("Invalid harness registry")
        entries, seen = [], set()
        if not 1 <= len(config["harnesses"]) <= 8:
            raise ValueError("Registry item limit exceeded")
        for entry in config["harnesses"]:
            if not isinstance(entry, dict) or entry.get("id") not in {"unreal", "pi", "openhands"} or entry["id"] in seen:
                raise ValueError("Unsupported or duplicate harness")
            if entry.get("adapter") != {"unreal": "unreal_jsonl", "pi": "pi_jsonl", "openhands": "external_worker_required"}[entry["id"]]:
                raise ValueError("Unsupported harness adapter")
            if not isinstance(entry.get("revision"), str) or len(entry["revision"]) > 100:
                raise ValueError("Invalid source revision")
            if not isinstance(entry.get("providers"), list) or any(p not in {"openai", "anthropic"} for p in entry["providers"]):
                raise ValueError("Invalid provider list")
            binary = entry.get("binary")
            if binary is not None and (not isinstance(binary, dict) or set(binary) != {"path", "sha256"}
                                       or not isinstance(binary["path"], str)):
                raise ValueError("Invalid executable configuration")
            if binary and binary["sha256"] is not None and not re.fullmatch(r"[a-f0-9]{64}", str(binary["sha256"])):
                raise ValueError("Invalid executable SHA256")
            seen.add(entry["id"])
            entries.append(entry)
        return entries

    def _inspect(self, entry):
        result = {"detected": False, "pin_verified": False, "build_status": "not_installed",
                  "executable_sha256": None}
        binary = entry.get("binary")
        if not binary:
            return result, None
        path = Path(binary["path"])
        if not path.is_absolute():
            if any(part in {".", ".."} for part in path.parts):
                raise ValueError("Executable traversal rejected")
            path = self.root / path
        for parent in (path, *path.parents):
            if parent.is_symlink():
                return dict(result, build_status="symlink_rejected"), None
        if not path.exists():
            return result, None
        if not path.is_file() or not os.access(path, os.X_OK) or path.stat().st_size > 512 * 1024 * 1024:
            return dict(result, build_status="invalid_executable"), None
        digest = file_digest(path)
        pinned = binary.get("sha256") == digest
        command = path
        additional = {}
        if entry["adapter"] == "pi_jsonl":
            interpreter, tree = entry.get("interpreter"), entry.get("runtime_tree")
            if not isinstance(interpreter, dict) or not isinstance(tree, dict):
                return dict(result, detected=True, build_status="untrusted_or_unpinned_runtime"), None
            node = Path(interpreter.get("path", ""))
            tree_path = self.root / tree.get("path", "")
            if not node.is_absolute() or not node.is_file() or node.is_symlink() or not os.access(node, os.X_OK):
                return dict(result, detected=True, build_status="invalid_interpreter"), None
            if not tree_path.is_dir() or tree_path.is_symlink() or not path.resolve().is_relative_to(tree_path.resolve()):
                return dict(result, detected=True, build_status="invalid_runtime_tree"), None
            node_hash, bundle_hash = file_digest(node), tree_digest(tree_path)
            pinned = pinned and node_hash == interpreter.get("sha256") and bundle_hash == tree.get("sha256")
            additional = {"interpreter_sha256": node_hash, "runtime_tree_sha256": bundle_hash}
            command = [node, "--max-old-space-size=256", path]
        return dict(result, detected=True, pin_verified=pinned, executable_sha256=digest,
                    build_status="pinned_executable" if pinned else "untrusted_or_unpinned_executable", **additional), command

    def _receipt(self, entry, inspection):
        path = self.root / "runtime/harnesses" / (entry["id"] + ".last-run.json")
        if any(parent.is_symlink() for parent in (path, *path.parents)) or not inspection["pin_verified"]:
            return {}
        try:
            with path.open("rb") as stream:
                value = json_load(stream.read(100001))
            pins_match = all(value.get(key) == inspection.get(key) for key in (
                "executable_sha256", "interpreter_sha256", "runtime_tree_sha256"))
            if pins_match and value.get("revision") == entry["revision"]:
                return value
        except (OSError, ValueError, TypeError, UnicodeError):
            pass
        return {}

    def status(self):
        items = []
        for entry in self._entries():
            inspection, _ = self._inspect(entry)
            receipt = self._receipt(entry, inspection)
            items.append({"id": entry["id"], "name": entry["name"], "upstream_url": entry["upstream_url"],
                          "revision": entry["revision"], "adapter": entry["adapter"],
                          "installation_status": entry["installation_status"], "providers": entry["providers"],
                          "default_model": entry["default_model"], **inspection,
                          "protocol_tested": receipt.get("protocol_tested") is True,
                          "live_authenticated": receipt.get("live_authenticated") is True,
                          "last_status": receipt.get("last_status"), "tools_enabled": False,
                          "execution_isolation": "process_limits_not_sandbox",
                          "platform_supported": os.name == "posix"})
        return {"schema_version": 1, "harnesses": items, "limits": {
            "max_prompt_utf8_bytes": runner.MAX_PROMPT_BYTES, "max_output_bytes": runner.MAX_OUTPUT_BYTES,
            "timeout_seconds_max": 60, "api_request_cap": None, "api_token_cap": None,
            "note": "Time and output limits do not bound provider token usage or total charges."}}

    def run(self, harness_id, prompt, allow_model_calls=False, provider="openai", model=None,
            environment=None, timeout_seconds=30, protocol_test_base_url=None):
        entry = next((e for e in self._entries() if e["id"] == harness_id), None)
        if entry is None:
            raise ValueError("Unknown harness")
        inspection, executable = self._inspect(entry)
        result = {"schema_version": 1, "harness_id": harness_id, "status": "not_installed", "output_text": "",
                  "evidence": {"revision": entry["revision"], **{key: inspection[key] for key in (
                      "executable_sha256", "interpreter_sha256", "runtime_tree_sha256") if key in inspection}}}
        if entry["adapter"] == "external_worker_required":
            return dict(result, status="external_worker_required")
        if not inspection["pin_verified"]:
            return dict(result, status=inspection["build_status"])
        result.update(runner.run(entry, executable, prompt, allow_model_calls=allow_model_calls, provider=provider,
                                 model=model, environment=environment, timeout_seconds=timeout_seconds,
                                 protocol_test_base_url=protocol_test_base_url))
        result["evidence"]["mode"] = result.get("mode")
        if result["status"] == "completed":
            previous = self._receipt(entry, inspection)
            receipt = {"schema_version": 1, "harness_id": harness_id, "revision": entry["revision"],
                       **{key: inspection[key] for key in ("executable_sha256", "interpreter_sha256",
                                                          "runtime_tree_sha256") if key in inspection},
                       "checked_at": datetime.now(timezone.utc).isoformat(), "last_status": "completed",
                       "protocol_tested": previous.get("protocol_tested") is True or result["mode"] == "protocol_test",
                       "live_authenticated": previous.get("live_authenticated") is True or result["mode"] == "live"}
            target = self.root / "runtime/harnesses"
            for parent in (target, *target.parents):
                if parent.is_symlink():
                    raise ValueError("Receipt path symlink rejected")
            target.mkdir(parents=True, exist_ok=True)
            path = target / (harness_id + ".last-run.json")
            if path.is_symlink():
                raise ValueError("Receipt symlink rejected")
            temporary = path.with_suffix(".tmp-" + str(os.getpid()))
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        return result
