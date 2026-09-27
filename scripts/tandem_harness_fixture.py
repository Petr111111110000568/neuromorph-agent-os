"""Cloud-only real OpenClaw CLI + real Hermes SDK, deterministic local transport.

No LLM is loaded and no inference provider is contacted. The model_runner and
model manifest verifier are explicit fixture injections; CLI/SDK subprocesses,
HTTP requests, reservations, result matching and deduplication are real.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FixtureError(ValueError):
    """Fixed diagnostic code, never a generated path, token or file content."""

    def __init__(self, code, *, link_kind=None, target_scope=None, limit_metadata=None):
        super().__init__(code)
        self.link_kind = link_kind
        self.target_scope = target_scope
        self.limit_metadata = limit_metadata


def scan_ephemeral_token(stage_dir, token):
    """Scan ONLY the freshly created SDK stage after its process has terminated.

    Links may resolve only inside this fresh stage, with canonical-path dedup.
    External links and special files fail closed. Limits are 2 MiB/file, 16 MiB
    total, 1024 entries and depth 32. A limit means unverified, not a clean scan.
    No file path or matched bytes are returned, even on failure.
    """
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise FixtureError("invalid_ephemeral_token")
    needle = token.encode("ascii")
    root = Path(stage_dir).absolute()
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or getattr(root_info, "st_file_attributes", 0) & 0x400:
        raise FixtureError("ephemeral_token_scan_link_rejected", target_scope="root_is_link")
    root = root.resolve(strict=True)
    maximum_file, maximum_total = 2 * 1024 * 1024, 16 * 1024 * 1024
    entries, total, files, internal_links = 0, 0, 0, 0
    visited = set()
    pending = [(root, 0)]
    def limit(kind, path, size=0):
        parts = {part.lower() for part in path.relative_to(root).parts}
        category = ("node_compile_cache" if any("compile-cache" in part for part in parts)
                    else "sqlite" if path.suffix in {".db", ".sqlite", ".sqlite3"}
                    else "state" if "state" in parts else "other")
        return FixtureError("ephemeral_token_scan_limit", limit_metadata={
            "limit_kind": kind, "category": category, "observed": min(max(0, size), 2147483647)})
    try:
        while pending:
            path, depth = pending.pop()
            entries += 1
            if entries > 1024 or depth > 32:
                raise limit("entries" if entries > 1024 else "depth", path,
                            entries if entries > 1024 else depth)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                kind = "symlink" if stat.S_ISLNK(info.st_mode) else "junction_or_reparse"
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise FixtureError("ephemeral_token_scan_link_rejected", link_kind=kind,
                                       target_scope="unresolvable") from exc
                if resolved != root and root not in resolved.parents:
                    raise FixtureError("ephemeral_token_scan_link_rejected", link_kind=kind,
                                       target_scope="outside_stage")
                internal_links += 1
                path = resolved
                info = path.lstat()
            canonical = path.resolve(strict=True)
            if canonical != root and root not in canonical.parents:
                raise FixtureError("ephemeral_token_scan_link_rejected", target_scope="outside_stage")
            if canonical in visited:
                continue
            visited.add(canonical)
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as directory:
                    for item in directory:
                        if entries + len(pending) >= 1024:
                            raise limit("entries", path, entries + len(pending))
                        pending.append((path / item.name, depth + 1))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise FixtureError("ephemeral_token_scan_special_file")
            if info.st_size > maximum_file or total + info.st_size > maximum_total:
                raise limit("file_bytes" if info.st_size > maximum_file else "total_bytes", path,
                            info.st_size if info.st_size > maximum_file else total + info.st_size)
            consumed, overlap = 0, b""
            with path.open("rb") as stream:
                while block := stream.read(65536):
                    consumed += len(block)
                    total += len(block)
                    if consumed > maximum_file or total > maximum_total:
                        raise limit("file_bytes" if consumed > maximum_file else "total_bytes", path,
                                    consumed if consumed > maximum_file else total)
                    data = overlap + block
                    if needle in data:
                        raise FixtureError("ephemeral_token_persisted")
                    overlap = data[-(len(needle) - 1):]
            if consumed != info.st_size:
                raise FixtureError("ephemeral_token_scan_file_changed")
            files += 1
    except OSError as exc:
        raise FixtureError("ephemeral_token_scan_io_failure") from exc
    return {"files_scanned": files, "bytes_scanned": total, "ephemeral_token_absent": True,
            "internal_links_checked": internal_links}


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as target:
        json.dump(value, target, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        target.write("\n")


def prepare_openclaw_manifest(node, package):
    """Fixture authority is the reviewed workflow's exact npm ci lock/install."""
    metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    if (metadata.get("name") != "openclaw" or metadata.get("version") != "2026.9.6"
            or metadata.get("bin", {}).get("openclaw") != "openclaw.mjs"):
        raise ValueError("Unexpected installed OpenClaw fixture package")
    manifest = {"schema_version": 1, "node_sha256": hash_file(node), "package": {
        "name": "openclaw", "version": "2026.9.6", "entry": "openclaw.mjs",
        "entry_sha256": hash_file(package / "openclaw.mjs"),
        "package_json_sha256": hash_file(package / "package.json")},
        "fixture_only": True, "installation_authority": "reviewed_workflow_exact_npm_lock"}
    path = package / "neuromorph-installed-manifest.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("Existing fixture installation manifest differs")
    else:
        write_new(path, manifest)
    return manifest


def sanitized_hermes_diagnostic(data_dir):
    """Read only this fixture's wrapper result, never raw SDK logs/profile files."""
    result = []
    for path in (data_dir / "tandem").glob("*/critique/hermes-result.json"):
        try:
            if path.stat().st_size > 65536:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if type(value) is not dict:
            continue
        if value.get("status") != "failed":
            continue
        item = {}
        for key in ("reason", "stage", "failure_kind", "missing_module"):
            text = value.get(key)
            if isinstance(text, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,120}", text):
                item[key] = text
        frames = value.get("frames", [])
        item["frames"] = [{"file": frame["file"], "line": frame["line"]}
            for frame in frames if isinstance(frame, dict)
            and isinstance(frame.get("file"), str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", frame["file"])
            and type(frame.get("line")) is int and 0 <= frame["line"] < 100000][:6]
        if type(value.get("sdk_result")) is dict:
            peer = load_module(ROOT / "scripts" / "hermes_local_peer.py", "fixture_peer_diagnostic")
            item["sdk_result"] = peer.bounded_sdk_result(value["sdk_result"])
            errors = value.get("api_errors", [])
            if type(errors) is list:
                item["api_errors"] = [peer.bounded_api_error(e) for e in errors[:4] if type(e) is dict]
            item["api_exception_chains"] = peer.bounded_exception_chains(value.get("api_exception_chains"))
            item["audit_denials"] = peer.bounded_audit_denials(value.get("audit_denials"))
        result.append(item)
    return result


def project_gateway_diagnostic(value):
    """Whitelist exact gateway schema and identifiers; never arbitrary names/values."""
    from workbench.tandem import _API_FIELDS, _DIAGNOSTIC_CODES
    keys = {"schema_version", "stage", "scope", "response_attempts", "http_response_counts",
            "validation_code_counts", "api_field_names", "unknown_api_fields_omitted",
            "counters_saturated", "values_recorded"}
    if (type(value) is not dict or set(value) != keys or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["stage"] not in {"plan", "critique", "synthesis"}
            or value["scope"] != "reply_decisions_not_client_delivery"
            or type(value["counters_saturated"]) is not bool or value["values_recorded"] is not False):
        raise FixtureError("gateway_diagnostic_rejected")
    for key in ("response_attempts", "unknown_api_fields_omitted"):
        if type(value[key]) is not int or not 0 <= value[key] <= 1000:
            raise FixtureError("gateway_diagnostic_rejected")
    for key, allowed in (("http_response_counts", {"200", "400", "401", "403", "404", "409", "413", "503", "other"}),
                         ("validation_code_counts", _DIAGNOSTIC_CODES)):
        mapping = value[key]
        if (type(mapping) is not dict or set(mapping) - allowed
                or any(type(n) is not int or not 0 <= n <= 1000 for n in mapping.values())):
            raise FixtureError("gateway_diagnostic_rejected")
    names = value["api_field_names"]
    if (type(names) is not list or len(names) > len(_API_FIELDS)
            or any(type(name) is not str or name not in _API_FIELDS for name in names)
            or len(set(names)) != len(names)):
        raise FixtureError("gateway_diagnostic_rejected")
    return json.loads(json.dumps(value))


def sanitized_gateway_diagnostics(data_dir):
    result = []
    for path in sorted((data_dir / "tandem").glob("*/*/gateway-diagnostic.json"))[:3]:
        try:
            if path.is_symlink() or path.stat().st_size > 4096:
                raise FixtureError("gateway_diagnostic_rejected")
            if Path(data_dir).resolve() not in path.resolve().parents:
                raise FixtureError("gateway_diagnostic_rejected")
            with path.open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise FixtureError("gateway_diagnostic_rejected")
            result.append(project_gateway_diagnostic(json.loads(raw)))
        except (OSError, ValueError, TypeError, UnicodeError):
            result.append({"diagnostic_rejected": True})
    return result


def execute(openclaw_package, node, hermes_python, hermes_source, output_dir):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("Cloud fixture requires the accepted GitHub Actions workflow")
    output = Path(output_dir).resolve()
    if output.exists():
        raise ValueError("Fresh fixture output directory required")
    output.mkdir(parents=True)
    receipt = {"schema_version": 1, "status": "failed", "mode": "deterministic_protocol_fixture",
        "actual_harness_processes": True, "external_model_calls": 0, "local_model_calls": 0,
        "fixture_completions": 0, "scientific_validation": False,
        "openclaw_version": "2026.9.6", "hermes_source_commit": "54fb5a42e8ecf2bf0326f7bd829a292ee0965451",
        "model_inference_executed": False,
        "limitations": ["The actual SDKs exchange fixed fixture text, not LLM answers.",
            "Model manifest verification is explicitly injected for this transport fixture.",
            "No live model quality, scientific validity or native desktop login is tested."]}
    calls, observations = [], []
    started = time.monotonic()
    data = output / "state"
    data.mkdir()
    try:
        package, node_path, source = (Path(p).resolve(strict=True) for p in
            (openclaw_package, node, hermes_source))
        # POSIX venv/bin/python is a symlink to the base interpreter. Preserve
        # its invocation path: resolving the last component drops the venv and
        # therefore its installed SDK dependencies (e.g. python-dotenv).
        python_input = Path(hermes_python).absolute()
        python_path = python_input.parent.resolve(strict=True) / python_input.name
        if not python_path.is_file():
            raise ValueError("SDK venv interpreter missing")
        manifest = prepare_openclaw_manifest(node_path, package)
        receipt["openclaw_installation_manifest_sha256"] = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        git = shutil.which("git")
        if not git:
            raise ValueError("Git is required for source verification")
        glue = load_module(ROOT / "scripts" / "run_local_tandem.py", "fixture_tandem_glue")
        from workbench.tandem import TandemCoordinator
        from workbench.autonomy import local_review
        fixture_root = output / "fixture-model-root"
        model_folder = fixture_root / "runtime" / "local-model"
        model_folder.mkdir(parents=True)
        fixture_manifest = {"schema_version": 1, "fixture_only": True,
                            "description": "No model or model executable is installed or invoked."}
        write_new(model_folder / "manifest.json", fixture_manifest)
        config = {"schema_version": 1, "model_root": str(fixture_root), "data_dir": str(data),
            "node": str(node_path), "openclaw_package": str(package),
            "hermes_python": str(python_path), "hermes_source": str(source), "git": str(Path(git).resolve())}
        actual_runner = glue.make_runner(config)

        def role_runner(stage, prompt, base_url, token, stage_dir):
            observation = {"stage": stage, "status": "started"}
            observations.append(observation)
            try:
                answer = actual_runner(stage, prompt, base_url, token, stage_dir)
            except Exception as exc:
                observation.update(status="failed", failure_kind=type(exc).__name__)
                if type(exc).__name__ in {'PeerError', 'FixtureError'} and re.fullmatch(r'[A-Za-z0-9_]{1,120}', str(exc)):
                    observation['reason'] = str(exc)
                raise
            try:
                observation["token_scan"] = scan_ephemeral_token(stage_dir, token)
            except (FixtureError, OSError) as exc:
                # This cloud fixture has a deterministic fake model, never a
                # live provider. Record the failure and exercise independent
                # SDK stages with NEW ephemeral tokens; overall acceptance
                # below remains fail-closed. This is not production admission.
                observation["scan_error"] = (str(exc) if isinstance(exc, FixtureError)
                                              else "ephemeral_token_scan_io_failure")
                if isinstance(exc, FixtureError):
                    if exc.limit_metadata is not None:
                        observation["token_scan_limit"] = exc.limit_metadata
                    observation["token_scan_link"] = {key: value for key, value in {
                        "kind": exc.link_kind, "target_scope": exc.target_scope}.items()
                        if value in {"symlink", "junction_or_reparse", "outside_stage", "unresolvable", "root_is_link"}}
            observation["status"] = "completed"
            return answer

        def fixture_completion(executable, model, prompt, *, max_tokens, timeout, stop_file, context_size):
            if max_tokens != 512 or context_size != 8192 or timeout != 300 or len(calls) >= 3:
                raise ValueError("Unexpected fixture inference contract")
            ordinal = len(calls) + 1
            calls.append({"ordinal": ordinal, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
            text = ("Проверка транспорта " + str(ordinal)
                    + ": фиксированный текст. Модель и научный эксперимент не запускались.")
            return {"status": "unverified_proposal", "text": text, "output_format": local_review.OUTPUT_FORMAT}

        council = TandemCoordinator(fixture_root, data, role_runner=role_runner,
            profile_ids=glue.profile_ids(config),
            model_runner=fixture_completion, manifest_verifier=lambda *args: (fixture_manifest, {}))
        task, question = "TANDEM-PROTOCOL-FIXTURE-001", "Проверь конечную передачу плана, критики и синтеза без модели."
        result = council.run(task, question, public_data_confirmed=True)
        receipt.update(council_status=result["status"], reserved_calls=result["reserved_calls"],
                       responses_received=result["responses_received"], accepted_stages=result["accepted_stages"])
        if result["status"] != "completed" or len(calls) != 3 or result["responses_received"] != 3:
            raise ValueError("Actual SDK tandem fixture incomplete")
        replay = council.run(task, question, public_data_confirmed=True)
        if replay != result or len(calls) != 3:
            raise ValueError("Fixture replay repeated an inference or changed the receipt")
        if any("scan_error" in observed for observed in observations):
            raise FixtureError("ephemeral_token_scan_failed")
        receipt.update(status="success", deduplication_verified=True,
            responses_sha256=[a["output_sha256"] for a in result["answers"]],
            hermes_manifest_sha256=hash_file(ROOT / "config" / "hermes_local_peer.json"))
    except Exception as exc:
        receipt["failure_kind"] = type(exc).__name__
    finally:
        receipt.update(fixture_completions=len(calls), harness_stages=observations,
                       elapsed_seconds=round(time.monotonic() - started, 3))
        receipt["hermes_diagnostics"] = sanitized_hermes_diagnostic(data)
        receipt["gateway_diagnostics"] = sanitized_gateway_diagnostics(data)
        write_new(output / "fixture-receipt.json", receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("openclaw-package", "node", "hermes-python", "hermes-source", "output-dir"):
        parser.add_argument("--" + name, required=True)
    result = execute(**vars(parser.parse_args(argv)))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
