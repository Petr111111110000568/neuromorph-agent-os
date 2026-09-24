#!/usr/bin/env python3
"""Install the optional pinned Pi runtime without npm lifecycle scripts.

Default invocation prints a plan. --install uses the committed lock with npm ci;
--verify checks the existing runtime without contacting a model or logging in.
Dependencies and their receipt stay in ignored runtime/, never in source control.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "@earendil-works/pi-coding-agent"
VERSION = "0.87.1"
REVISION = "f07218c4d4bbc12bef056a7058c3dd49dfe41abe"
INTEGRITY = "sha512-m8ArJUtVcQMSe1lLE/Ei7vX/JV7O39sWmWBsXV2NOU70F0qCp8GubA24pT3LnwTmM6LL2xV80/h6sQg85n69ew=="
LOCK = ROOT / "scripts/pi-package-lock.json"
LOCK_SHA256 = "b237e58946348dbbe15dac27f3d9ceff794c834c2868141aa4e59df1ddc76f0f"
RUNTIME = ROOT / "runtime/harnesses/pi"
CACHE = ROOT / "runtime/harnesses/pi-cache"
RECEIPT = ROOT / "runtime/harnesses/pi-build.json"
ENTRY = Path("node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_lock():
    if digest(LOCK) != LOCK_SHA256:
        raise ValueError("committed_lock_checksum_mismatch")
    lock = json.loads(LOCK.read_text())
    packages = lock["packages"]
    main = packages["node_modules/" + PACKAGE]
    if main["version"] != VERSION or main["integrity"] != INTEGRITY:
        raise ValueError("package_pin_mismatch")
    for name, package in packages.items():
        if not name:
            continue
        if not package.get("resolved", "").startswith("https://registry.npmjs.org/"):
            raise ValueError("non_registry_dependency")
        if not package.get("integrity", "").startswith(("sha512-", "sha256-")):
            raise ValueError("dependency_integrity_missing")
    return lock


def clean_environment(node):
    # No provider credentials, npm token configuration, NODE_OPTIONS or user plugins.
    permitted = {"PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT",
                 "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
                 "https_proxy", "http_proxy", "all_proxy", "no_proxy",
                 "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"}
    env = {key: value for key, value in os.environ.items() if key in permitted}
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", os.defpath)
    env.update({"CI": "1", "NO_COLOR": "1", "PI_OFFLINE": "1",
                "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"})
    return env


def command(args, cwd, env, timeout=60):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=timeout)
    if result.returncode:
        # Avoid echoing arbitrary npm diagnostics that could contain local config.
        raise RuntimeError("command_failed:" + Path(str(args[0])).name + ":" + str(result.returncode))
    return result


def node_runtime():
    executable = shutil.which("node")
    if not executable:
        raise ValueError("node_not_found_requires_22_19_or_newer")
    node = Path(executable).resolve()
    result = command([node, "--version"], ROOT, clean_environment(node))
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", result.stdout)
    if not match or tuple(map(int, match.groups())) < (22, 19, 0):
        raise ValueError("node_requires_22_19_or_newer")
    return node, result.stdout.strip()


def verify(node, node_version, npm_version=None):
    if not (RUNTIME / ENTRY).is_file():
        raise ValueError("pi_not_installed")
    if digest(RUNTIME / "package-lock.json") != LOCK_SHA256:
        raise ValueError("installed_lock_checksum_mismatch")
    installed = json.loads((RUNTIME / "node_modules" / PACKAGE / "package.json").read_text())
    if installed["version"] != VERSION:
        raise ValueError("installed_version_mismatch")
    sys.path.insert(0, str(ROOT))
    from workbench.harnesses.registry import tree_digest
    entry_hash, node_hash, tree_hash = digest(RUNTIME / ENTRY), digest(node), tree_digest(RUNTIME)
    if npm_version is None:
        previous = json.loads(RECEIPT.read_text())
        if (previous["binary"]["sha256"] != entry_hash or
                previous["interpreter"]["sha256"] != node_hash or
                previous["runtime_tree"]["sha256"] != tree_hash):
            raise ValueError("runtime_changed_since_installation")
        npm_version = previous.get("npm_version")
    env = clean_environment(node)
    with tempfile.TemporaryDirectory(prefix="pi-bootstrap-check-") as temporary:
        cwd = Path(temporary)
        env["PI_CODING_AGENT_DIR"] = str(cwd / "agent")
        base = [node, "--max-old-space-size=256", RUNTIME / ENTRY]
        version = command(base + ["--version"], cwd, env)
        help_result = command(base + ["--help"], cwd, env)
    if version.stdout.strip() != VERSION:
        raise ValueError("cli_version_mismatch")
    required = ["--mode", "--no-session", "--no-tools", "--no-extensions",
                "--no-skills", "--no-prompt-templates", "--no-themes",
                "--no-context-files", "--no-approve", "--offline"]
    if any(flag not in help_result.stdout for flag in required):
        raise ValueError("cli_contract_changed")
    receipt = {
        "schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "installed_cli_verified", "package": PACKAGE, "version": VERSION,
        "source_revision": REVISION, "npm_integrity": INTEGRITY,
        "package_lock_sha256": LOCK_SHA256,
        "binary": {"path": str((RUNTIME / ENTRY).relative_to(ROOT)), "sha256": entry_hash},
        "interpreter": {"path": str(node), "sha256": node_hash, "version": node_version},
        "runtime_tree": {"path": str(RUNTIME.relative_to(ROOT)), "sha256": tree_hash},
        "npm_version": npm_version, "lifecycle_scripts_executed": False,
        "version_check": {"exit_code": version.returncode, "stdout": version.stdout.strip()},
        "help_check": {"exit_code": help_result.returncode,
                       "required_flags_present": required,
                       "stdout_sha256": hashlib.sha256(help_result.stdout.encode()).hexdigest()},
        "live_authenticated": False, "model_calls_performed": False,
        "oauth_performed": False, "protocol_tested": False,
        "notes": ["CLI execution verified; model inference and scientific validity are separate checks.",
                  "Pi process has host permissions; tool disabling is not an OS sandbox."]
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return receipt


def install(node):
    if RUNTIME.exists():
        raise ValueError("runtime_already_exists_use_verify_or_move_it_manually")
    npm_path = shutil.which("npm")
    if not npm_path:
        raise ValueError("npm_not_found")
    npm = Path(npm_path).resolve()
    CACHE.mkdir(parents=True, exist_ok=True)
    user_config, global_config = CACHE / "empty-user.npmrc", CACHE / "empty-global.npmrc"
    user_config.write_text("")
    global_config.write_text("")
    env = clean_environment(node)
    # npm CLI is run with the same explicit Node interpreter as the agent.
    npm_command = [node, npm]
    npm_version = command(npm_command + ["--version"], ROOT, env).stdout.strip()
    RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pi-install-", dir=RUNTIME.parent) as temporary:
        stage = Path(temporary)
        lock = checked_lock()
        package = {"name": lock["name"], "version": lock["version"], "private": True,
                   "dependencies": {PACKAGE: VERSION}}
        (stage / "package.json").write_text(json.dumps(package, indent=2) + "\n")
        shutil.copyfile(LOCK, stage / "package-lock.json")
        command(npm_command + ["ci", "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund",
                               "--registry=https://registry.npmjs.org/",
                               "--userconfig=" + str(user_config), "--globalconfig=" + str(global_config),
                               "--cache=" + str(CACHE / "npm")], stage, env, timeout=600)
        if digest(stage / "package-lock.json") != LOCK_SHA256:
            raise ValueError("npm_changed_committed_lock")
        stage.rename(RUNTIME)
    return npm_version


def pin_registry(receipt):
    """Explicitly pin only Pi's verified local executable, interpreter and tree."""
    registry = ROOT / "config/harnesses.json"
    for path in (registry, *registry.parents):
        if path.is_symlink():
            raise ValueError("registry_symlink_rejected")
        if path == ROOT:
            break
    value = json.loads(registry.read_text(encoding="utf-8"))
    entries = [entry for entry in value.get("harnesses", []) if entry.get("id") == "pi"]
    if len(entries) != 1:
        raise ValueError("unexpected_pi_registry_entries")
    entry = entries[0]
    if (entry.get("adapter") != "pi_jsonl" or entry.get("revision") != REVISION or
            entry.get("binary", {}).get("path") != receipt["binary"]["path"]):
        raise ValueError("unexpected_pi_registry_entry")
    entry["binary"] = dict(receipt["binary"])
    entry["interpreter"] = {key: receipt["interpreter"][key] for key in ("path", "sha256")}
    entry["runtime_tree"] = dict(receipt["runtime_tree"])
    temporary = registry.with_name("harnesses.json.pi-bootstrap-" + str(os.getpid()) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, registry)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--install", action="store_true")
    group.add_argument("--verify", action="store_true")
    parser.add_argument("--pin-registry", action="store_true",
                        help="After verification, update only Pi's local executable/runtime pins")
    args = parser.parse_args()
    if args.pin_registry and not (args.install or args.verify):
        parser.error("--pin-registry requires --install or --verify")
    checked_lock()
    if not args.install and not args.verify:
        print(json.dumps({"status": "plan_only", "package": PACKAGE, "version": VERSION,
                          "target": str(RUNTIME.relative_to(ROOT)), "node_requires": ">=22.19.0",
                          "lock_sha256": LOCK_SHA256, "lifecycle_scripts": "disabled",
                          "install_command": "python scripts/bootstrap_pi.py --install --pin-registry",
                          "model_calls": False}, indent=2))
        return
    node, node_version = node_runtime()
    npm_version = install(node) if args.install else None
    receipt = verify(node, node_version, npm_version)
    if args.pin_registry:
        pin_registry(receipt)
    print(json.dumps(dict(receipt, registry_updated=args.pin_registry), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
