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
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


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
        result.append(item)
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
        package, node_path, python_path, source = (Path(p).resolve(strict=True) for p in
            (openclaw_package, node, hermes_python, hermes_source))
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
                if type(exc).__name__ == 'PeerError' and re.fullmatch(r'[A-Za-z0-9_]{1,120}', str(exc)):
                    observation['reason'] = str(exc)
                raise
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
        receipt.update(status="success", deduplication_verified=True,
            responses_sha256=[a["output_sha256"] for a in result["answers"]],
            hermes_manifest_sha256=hash_file(ROOT / "config" / "hermes_local_peer.json"))
    except Exception as exc:
        receipt["failure_kind"] = type(exc).__name__
    finally:
        receipt.update(fixture_completions=len(calls), harness_stages=observations,
                       elapsed_seconds=round(time.monotonic() - started, 3))
        receipt["hermes_diagnostics"] = sanitized_hermes_diagnostic(data)
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
