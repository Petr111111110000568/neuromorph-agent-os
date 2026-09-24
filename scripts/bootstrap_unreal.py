#!/usr/bin/env python3
"""Explicit, pinned installation of the optional Unreal Agent Linux runner.

Default mode only prints the installation plan. --install downloads a checksummed
Go distribution, verifies a clean pinned source checkout, and builds the runner.
No model request is made and no generated program is executed by this script.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://github.com/unreallabsai/unreal-agent.git"
SOURCE_COMMIT = "1b9f778453f411c029b39b85102aaefb95e7e48d"
GO_VERSION = "1.27.1"
GO_ARCHIVE = "go1.27.1.linux-amd64.tar.gz"
GO_URL = "https://go.dev/dl/" + GO_ARCHIVE
GO_SHA256 = "63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445"
GO_SIZE = 70_553_950
GO_MODULE = "golang.org/toolchain@v0.0.1-go1.27.1.linux-amd64"
GO_MODULE_URL = "https://proxy.golang.org/golang.org/toolchain/@v/v0.0.1-go1.27.1.linux-amd64.zip"
GO_MODULE_H1 = "h1:MeqkXdYlyiVdqJXENOTyX7xd8QjDM/mxR52RKOFBS0M="
GO_MODULE_SIZE = 75_704_807
BINARY = ROOT / "runtime/harnesses/unreal-agent-runner"
RECEIPT = ROOT / "runtime/harnesses/unreal-build.json"


def ensure_project_output(path):
    path = Path(path)
    if path != ROOT and ROOT not in path.parents:
        raise ValueError("output_outside_project")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("output_symlink_rejected")
        if component == ROOT:
            break


def pin_registry(binary_hash):
    """Explicitly pin the just-built executable in a local checkout only."""
    registry = ROOT / "config/harnesses.json"
    ensure_project_output(registry)
    value = json.loads(registry.read_text(encoding="utf-8"))
    entries = value.get("harnesses", value.get("entries", []))
    entry = next((entry for entry in entries if entry.get("id") == "unreal"), None)
    if not entry or entry.get("adapter") != "unreal_jsonl" or entry.get("revision") != SOURCE_COMMIT:
        raise ValueError("unexpected_unreal_registry_entry")
    if entry.get("binary", {}).get("path") != str(BINARY.relative_to(ROOT)):
        raise ValueError("unexpected_binary_registry_path")
    entry["binary"]["sha256"] = binary_hash
    temporary = registry.with_name("harnesses.json.bootstrap.tmp")
    ensure_project_output(temporary)
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, registry)


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_toolchain(archive):
    archive = Path(archive)
    if archive.is_file() and archive.stat().st_size == GO_SIZE and checksum(archive) == GO_SHA256:
        return
    partial = archive.with_suffix(archive.suffix + ".partial")
    partial.unlink(missing_ok=True)
    size = 0
    try:
        with urllib.request.urlopen(GO_URL, timeout=60) as response, partial.open("xb") as output:
            if not response.geturl().startswith(("https://go.dev/", "https://dl.google.com/")):
                raise ValueError("unexpected_toolchain_download_host")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > GO_SIZE:
                    raise ValueError("toolchain_size_exceeded")
                output.write(chunk)
        if size != GO_SIZE or checksum(partial) != GO_SHA256:
            raise ValueError("toolchain_checksum_mismatch")
        os.replace(partial, archive)
    finally:
        partial.unlink(missing_ok=True)


def extract_toolchain(archive, destination):
    """Never allow links, traversal, device nodes or archive-defined permissions."""
    destination = Path(destination)
    total = 0
    with tarfile.open(archive, "r:gz") as bundle:
        for entry in bundle:
            relative = PurePosixPath(entry.name)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "go":
                raise ValueError("unsafe_toolchain_archive_path")
            if not (entry.isdir() or entry.isfile()):
                raise ValueError("unsafe_toolchain_archive_type")
            total += entry.size
            if total > 1024 * 1024 * 1024:
                raise ValueError("toolchain_archive_limit")
            target = destination.joinpath(*relative.parts)
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = bundle.extractfile(entry)
                if stream is None:
                    raise ValueError("invalid_archive_file")
                with stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(0o755 if entry.mode & 0o111 else 0o644)


def module_hash(path):
    """Go's documented x/mod/sumdb/dirhash.Hash1 over all ZIP file entries."""
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)):
            raise ValueError("duplicate_toolchain_zip_entry")
        if sum(entry.file_size for entry in bundle.infolist()) > 1024 * 1024 * 1024:
            raise ValueError("toolchain_zip_limit")
        for name in sorted(names):
            if "\n" in name:
                raise ValueError("invalid_zip_filename")
            content_hash = hashlib.sha256()
            with bundle.open(name) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    content_hash.update(chunk)
            digest.update((content_hash.hexdigest() + "  " + name + "\n").encode("utf-8"))
    return "h1:" + base64.b64encode(digest.digest()).decode("ascii")


def download_module_toolchain(archive):
    archive = Path(archive)
    if archive.is_file() and archive.stat().st_size == GO_MODULE_SIZE and module_hash(archive) == GO_MODULE_H1:
        return
    partial = archive.with_suffix(".partial")
    partial.unlink(missing_ok=True)
    size = 0
    try:
        with urllib.request.urlopen(GO_MODULE_URL, timeout=60) as response, partial.open("xb") as output:
            if not response.geturl().startswith(("https://proxy.golang.org/", "https://storage.googleapis.com/proxy-golang-org-prod/")):
                raise ValueError("unexpected_module_download_host")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > GO_MODULE_SIZE:
                    raise ValueError("module_toolchain_size_exceeded")
                output.write(chunk)
        if size != GO_MODULE_SIZE or module_hash(partial) != GO_MODULE_H1:
            raise ValueError("module_toolchain_checksum_mismatch")
        os.replace(partial, archive)
    finally:
        partial.unlink(missing_ok=True)


def extract_module_toolchain(archive, destination):
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            name = entry.filename
            if not name.startswith(GO_MODULE + "/"):
                raise ValueError("invalid_module_zip_prefix")
            relative = PurePosixPath(name[len(GO_MODULE) + 1:])
            mode = entry.external_attr >> 16
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or (mode & 0o170000) == 0o120000:
                raise ValueError("unsafe_module_zip_path")
            target = Path(destination) / "go" / str(relative)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry) as stream, target.open("xb") as output:
                shutil.copyfileobj(stream, output)
            executable = str(relative).startswith(("bin/", "pkg/tool/"))
            target.chmod(0o755 if executable else 0o644)


def command(args, cwd, env, timeout=300):
    started = time.monotonic()
    result = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout, check=False)
    record = {"command": [str(a) for a in args], "exit_code": result.returncode,
              "seconds": round(time.monotonic() - started, 3),
              "stdout": result.stdout[-24000:], "stderr": result.stderr[-12000:]}
    if result.returncode:
        raise RuntimeError(json.dumps(record, ensure_ascii=False))
    return record


def build(source, cache, run_tests=True, toolchain_format="module"):
    source, cache = Path(source).resolve(), Path(cache).resolve()
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise ValueError("this_pinned_toolchain_supports_linux_amd64_only")
    if cache == ROOT or ROOT in cache.parents or source == ROOT or ROOT in source.parents:
        raise ValueError("toolchain_and_upstream_source_must_be_outside_project")
    minimal = {"PATH": os.defpath, "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    git = shutil.which("git")
    if not git:
        raise ValueError("git_required")
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        command([git, "-c", "credential.helper=", "clone", "--no-checkout", SOURCE_URL, str(source)], source.parent, minimal)
        command([git, "checkout", "--detach", SOURCE_COMMIT], source, minimal)
    revision = command([git, "rev-parse", "HEAD"], source, minimal)["stdout"].strip()
    dirty = command([git, "status", "--porcelain", "--untracked-files=all"], source, minimal)["stdout"].strip()
    if revision != SOURCE_COMMIT or dirty:
        raise ValueError("upstream_checkout_must_match_clean_pinned_commit")
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / (GO_ARCHIVE if toolchain_format == "archive" else "go1.27.1-linux-amd64-module.zip")
    if toolchain_format == "archive":
        download_toolchain(archive)
    elif toolchain_format == "module":
        download_module_toolchain(archive)
    else:
        raise ValueError("invalid_toolchain_format")
    # Always re-extract the verified archive; do not trust a stale executable.
    with tempfile.TemporaryDirectory(prefix="verified-go-", dir=cache) as directory:
        extracted = Path(directory)
        if toolchain_format == "archive":
            extract_toolchain(archive, extracted)
        else:
            extract_module_toolchain(archive, extracted)
        go = extracted / "go/bin/go"
        env = {**minimal, "GOROOT": str(extracted / "go"), "GOCACHE": str(cache / "build-cache"),
               "GOPATH": str(cache / "gopath"), "GOMODCACHE": str(cache / "module-cache"),
               "GOTOOLCHAIN": "local", "GOWORK": "off", "GOENV": "off", "CGO_ENABLED": "0",
               "GOPROXY": "https://proxy.golang.org", "GOSUMDB": "sum.golang.org",
               "GOOS": "linux", "GOARCH": "amd64"}
        # Public module downloads use the host's configured network route. No API
        # credentials are inherited, and proxy values are never logged.
        proxy_names = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
        for name in proxy_names:
            if os.environ.get(name):
                env[name] = os.environ[name]
        version = command([go, "version"], source, env)
        if "go" + GO_VERSION + " linux/amd64" not in version["stdout"]:
            raise ValueError("unexpected_go_version")
        modules = command([go, "mod", "download"], source, env)
        module_integrity = command([go, "mod", "verify"], source, env)
        ensure_project_output(BINARY)
        ensure_project_output(RECEIPT)
        BINARY.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="unreal-build-", dir=BINARY.parent) as directory2:
            binary = Path(directory2) / "unreal-agent-runner"
            compilation = command([go, "build", "-mod=readonly", "-trimpath", "-buildvcs=false", "-o", binary,
                                   "./cmd/unreal-agent-runner"], source, env, timeout=600)
            tests = command([go, "test", "-mod=readonly", "./cmd/unreal-agent-runner", "./cmd/internal/agentrunner"],
                            source, env, timeout=300) if run_tests else None
            binary.chmod(0o755)
            binary_hash = checksum(binary)
            os.replace(binary, BINARY)
        receipt = {"schema_version": 1, "status": "built", "source_url": SOURCE_URL,
                   "source_commit": revision, "source_clean": True, "license": "MIT",
                   "toolchain_format": toolchain_format,
                   "toolchain_url": GO_URL if toolchain_format == "archive" else GO_MODULE_URL,
                   "toolchain_sha256": checksum(archive),
                   "toolchain_module_h1": GO_MODULE_H1 if toolchain_format == "module" else None,
                   "toolchain_bytes": archive.stat().st_size, "go_version": version["stdout"].strip(),
                   "binary_path": str(BINARY.relative_to(ROOT)), "binary_sha256": binary_hash,
                   "binary_bytes": BINARY.stat().st_size, "modules": modules, "module_integrity": module_integrity,
                   "network_proxy_environment_names": [name for name in proxy_names if name in env],
                   "build": compilation, "upstream_tests": tests,
                   "model_requests": 0, "protocol_tested": False}
        RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Explicitly download/build the pinned optional harness")
    parser.add_argument("--source-dir", default=str(ROOT.parent / "unreal-agent-upstream"))
    parser.add_argument("--toolchain-dir", default=str(ROOT.parent / "meta-harness-toolchains"))
    parser.add_argument("--skip-upstream-tests", action="store_true")
    parser.add_argument("--toolchain-format", choices=("archive", "module"), default="module",
                        help="Official Go archive, or official module ZIP with pinned sum.golang.org h1")
    parser.add_argument("--pin-registry", action="store_true", help="Update only the local Unreal executable hash after a verified build")
    args = parser.parse_args(argv)
    if not args.install:
        print(json.dumps({"status": "plan_only", "source_commit": SOURCE_COMMIT,
                          "toolchain_format": args.toolchain_format,
                          "go_url": GO_MODULE_URL if args.toolchain_format == "module" else GO_URL,
                          "go_checksum": GO_MODULE_H1 if args.toolchain_format == "module" else GO_SHA256,
                          "binary": str(BINARY),
                          "next": "Run with --install to perform this installation."}, indent=2))
        return 0
    result = build(args.source_dir, args.toolchain_dir, run_tests=not args.skip_upstream_tests,
                   toolchain_format=args.toolchain_format)
    if args.pin_registry:
        pin_registry(result["binary_sha256"])
    print(json.dumps({key: result[key] for key in ("status", "source_commit", "go_version", "binary_path", "binary_sha256", "binary_bytes")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
