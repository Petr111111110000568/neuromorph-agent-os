"""Portable, streaming ZIP64 distribution. Installation never executes its contents.

Only clean, tracked application files in the fixed allowlist enter a bundle. Optional
model/Python trees require operator-supplied immutable manifests and license files.
Hashes detect corruption, not publisher authenticity; pin the archive hash separately.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

CHUNK = 1024 * 1024
MAX_FILES = 20000
MAX_MANIFEST = 8 * 1024 * 1024
DEFAULT_MAX_BYTES = 100 * 1024**3
STAMP = (2026, 1, 1, 0, 0, 0)
MANIFEST = "BUNDLE_MANIFEST.json"
ROOT_FILES = {"README.md", "AGENTS.md", "CONTRACT.md", "CONTRIBUTING.md",
              "NETWORK_CONTRACT.md", "SOCIETY_CONTRACT.md", "agent-project.json",
              "plugin_worker.py", "LICENSE", "NOTICE", "start.cmd", "start.sh"}
SOURCE_DIRS = {"workbench", "web", "config", "configs", "srf", "scripts", "tests",
               "browser-extension", "docs", "examples", "legacy", "notebooks"}
SOURCE_SUFFIXES = {".py", ".json", ".md", ".txt", ".html", ".css", ".js", ".mjs",
                   ".sh", ".cmd", ".ps1", ".yml", ".yaml", ".toml", ".csv",
                   ".svg", ".png", ".ipynb", ".c", ".h"}
EXCLUDED_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", "state",
                  "runtime", "secrets", "credentials", ".cache"}
RESERVED = {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
            *("LPT" + str(i) for i in range(1, 10))}


class BundleError(ValueError):
    pass


def safe_name(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise BundleError("Invalid relative path")
    if any(ord(c) < 32 for c in value) or any(c in value for c in "\\:*?\"<>|"):
        raise BundleError("Unsafe path")
    parts = value.split("/")
    if any(p in {"", ".", ".."} or p.endswith((" ", ".")) or
           p.split(".")[0].upper() in RESERVED for p in parts):
        raise BundleError("Unsafe path component")
    return value


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BundleError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BundleError("Invalid JSON manifest") from exc


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _regular(root, relative):
    root = Path(root).absolute()
    if not root.is_dir():
        raise BundleError("Source root must be a directory, not a link")
    _no_link_ancestors(root)
    current = root
    for part in safe_name(relative).split("/"):
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BundleError("Links and reparse points are excluded")
    if not stat.S_ISREG(current.stat().st_mode):
        raise BundleError("Only regular files can be packaged")
    return current


def _no_link_ancestors(path):
    for part in (Path(path), *Path(path).parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BundleError("Links and reparse points are excluded")


def _digest(path):
    value = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while data := stream.read(CHUNK):
            size += len(data)
            value.update(data)
    return size, value.hexdigest()


def _git(root, *args):
    command = [os.environ.get("NEUROMORPH_GIT", "git"), "-C", str(root), *args]
    try:
        return subprocess.run(command, check=True, capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleError("Git source inspection failed") from exc


def _allowed(path):
    parts = path.split("/")
    if any(p.lower() in EXCLUDED_PARTS for p in parts):
        return False
    filename = parts[-1].lower()
    if filename.startswith((".env", "id_rsa", "id_ed25519")) or filename.endswith((".pem", ".key", ".pfx", ".dpapi")):
        return False
    if len(parts) == 1:
        return path in ROOT_FILES
    if parts[0] == "data":
        return len(parts) == 2 and filename.endswith(".json") and filename != "environment_status.json"
    return parts[0] in SOURCE_DIRS and Path(filename).suffix in SOURCE_SUFFIXES


def source_entries(root):
    root = Path(root).absolute()
    commit = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BundleError("Expected immutable Git commit")
    if _git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise BundleError("Tracked source changes must be committed before packaging")
    entries = []
    for item in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not item:
            continue
        metadata, rawpath = item.split(b"\t", 1)
        mode, _, stage = metadata.decode("ascii").split()
        relative = rawpath.decode("utf-8")
        if not _allowed(relative):
            continue
        safe_name(relative)
        if stage != "0" or mode not in {"100644", "100755"}:
            raise BundleError("Unmerged or linked source entry")
        entries.append(("app/" + relative, _regular(root, relative), None))
    if not any(e[0] == "app/workbench/__main__.py" for e in entries):
        raise BundleError("Application entrypoint missing")
    return commit, entries


def _file_rows(value):
    if not isinstance(value, list) or not 0 < len(value) <= MAX_FILES:
        raise BundleError("Invalid manifest file list")
    seen = set()
    rows = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
            raise BundleError("Invalid file declaration")
        name = safe_name(row["path"])
        if name.casefold() in seen or type(row["bytes"]) is not int or row["bytes"] < 0:
            raise BundleError("Duplicate path or invalid size")
        if not isinstance(row["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            raise BundleError("Expected SHA-256")
        seen.add(name.casefold())
        rows.append(dict(row))
    return rows


def component_entries(root, manifest_path, prefix):
    if root is None and manifest_path is None:
        return None, []
    if root is None or manifest_path is None:
        raise BundleError("Component root requires a pinned manifest")
    path = Path(manifest_path)
    if path.stat().st_size > MAX_MANIFEST:
        raise BundleError("Manifest too large")
    declaration = _json(path.read_bytes())
    if not isinstance(declaration, dict) or set(declaration) != {"identity", "revision", "files", "license_files"}:
        raise BundleError("Component manifest requires identity, revision, files, license_files")
    for key in ("identity", "revision"):
        if not isinstance(declaration[key], str) or not 1 <= len(declaration[key]) <= 240:
            raise BundleError("Invalid component identity")
    if not re.fullmatch(r"[0-9a-f]{40,64}", declaration["revision"]):
        raise BundleError("Component revision must be an immutable hexadecimal digest")
    rows = _file_rows(declaration["files"])
    names = {r["path"] for r in rows}
    licenses = declaration["license_files"]
    if not isinstance(licenses, list) or not licenses or any(not isinstance(x, str) or x not in names for x in licenses):
        raise BundleError("Component license must be included")
    entries = [(prefix + "/" + r["path"], _regular(root, r["path"]), r) for r in rows]
    if prefix == "python":
        pth = [x for x in names if re.fullmatch(r"python[0-9]+\._pth", x)]
        if "python.exe" not in names or len(pth) != 1 or pth[0].replace("._pth", ".zip") not in names:
            raise BundleError("Expected Windows embeddable Python layout")
    return declaration, entries


LAUNCHER = '''"""Explicit user launch, never run by the installer."""
import os
from pathlib import Path
import runpy
import sys
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "app"))
os.chdir(root / "app")
sys.argv = ["workbench", "--data-dir", str(root / "state"), "local-network"]
runpy.run_module("workbench", run_name="__main__")
'''
WINDOWS_LAUNCH = '''@echo off\r
cd /d "%~dp0"\r
if exist "%~dp0python\\python.exe" (\r
  "%~dp0python\\python.exe" -I "%~dp0Launch.py"\r
) else (\r
  py -3 -I "%~dp0Launch.py"\r
)\r
if errorlevel 1 pause\r
'''
POSIX_LAUNCH = '''#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec python3 -I ./Launch.py
'''


def _zip_info(name):
    info = zipfile.ZipInfo(name, STAMP)
    info.compress_type = zipfile.ZIP_STORED  # reproducible and streaming even for large model files
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | (0o755 if name == "start.sh" else 0o644)) << 16
    return info


def build(source, output, *, model_root=None, model_manifest=None, python_root=None,
          python_manifest=None, max_bytes=DEFAULT_MAX_BYTES):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise BundleError("Output already exists")
    commit, entries = source_entries(source)
    components = {}
    for kind, root, manifest_path in (("model", model_root, model_manifest), ("python", python_root, python_manifest)):
        prefix = "app" if kind == "model" else "python"
        declaration, extra = component_entries(root, manifest_path, prefix)
        if declaration:
            if kind == "model" and any(not r["path"].startswith("runtime/local-model/") for r in declaration["files"]):
                raise BundleError("Model files must be inside runtime/local-model")
            components[kind] = declaration
            entries.extend(extra)
    generated = {"Launch.py": LAUNCHER.encode(), "Start.cmd": WINDOWS_LAUNCH.encode(), "start.sh": POSIX_LAUNCH.encode()}
    # Embeddable Python ignores PYTHONPATH. Its child -m workbench workers must see
    # app explicitly, while site packages and arbitrary environment paths stay off.
    for name, path, expected in list(entries):
        if re.fullmatch(r"python/python[0-9]+\._pth", name):
            if not expected or _digest(path) != (expected["bytes"], expected["sha256"]):
                raise BundleError("Python path declaration checksum mismatch")
            zipname = Path(name).name.replace("._pth", ".zip")
            generated[name] = (zipname + "\n.\n../app\n").encode("ascii")
            entries.remove((name, path, expected))
    seen = set()
    if len(entries) + len(generated) > MAX_FILES:
        raise BundleError("Too many files")
    for name, _, _ in entries:
        safe_name(name)
        if name.casefold() in seen:
            raise BundleError("Duplicate archive path")
        seen.add(name.casefold())
    total = sum(path.stat().st_size for _, path, _ in entries) + sum(map(len, generated.values()))
    if total > max_bytes or total < 0:
        raise BundleError("Bundle exceeds size budget")
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < total + MAX_MANIFEST:
        raise BundleError("Insufficient storage for archive")
    # Exclusive output creation protects existing files, including races after preflight.
    created = False
    try:
        with output.open("xb") as raw:
            created = True
            rows = []
            actual_total = sum(map(len, generated.values()))
            with zipfile.ZipFile(raw, "w", allowZip64=True) as archive:
                for name, path, expected in sorted(entries):
                    # Recheck link components just before opening, then compare immutable component hashes.
                    _no_link_ancestors(path)
                    size, digest = 0, hashlib.sha256()
                    with path.open("rb") as src, archive.open(_zip_info(name), "w", force_zip64=True) as dst:
                        while data := src.read(CHUNK):
                            size += len(data)
                            actual_total += len(data)
                            if actual_total > max_bytes:
                                raise BundleError("File exceeds budget while copying")
                            digest.update(data)
                            dst.write(data)
                    result = {"path": name, "bytes": size, "sha256": digest.hexdigest()}
                    if expected and (size != expected["bytes"] or result["sha256"] != expected["sha256"]):
                        raise BundleError("Component checksum mismatch: " + name)
                    rows.append(result)
                for name, data in sorted(generated.items()):
                    archive.writestr(_zip_info(name), data)
                    rows.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                manifest = {"schema_version": 1, "source_commit": commit, "components": components,
                            "files": sorted(rows, key=lambda r: r["path"]),
                            "execution": "manual_launch_only", "model_portability": "not_verified",
                            "python_path_policy": "stdlib_zip;runtime;../app;site_disabled"}
                encoded_manifest = _encoded(manifest)
                if len(encoded_manifest) > MAX_MANIFEST:
                    raise BundleError("Bundle manifest exceeds format limit")
                archive.writestr(_zip_info(MANIFEST), encoded_manifest)
        size, digest = _digest(output)
        return {"path": str(output), "bytes": size, "sha256": digest, "files": len(rows), "source_commit": commit}
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise


def _archive_manifest(archive, max_bytes):
    infos = archive.infolist()
    if len(infos) > MAX_FILES + 1:
        raise BundleError("Too many archive entries")
    seen = set()
    for info in infos:
        name = safe_name(info.filename)
        mode = info.external_attr >> 16
        if name.casefold() in seen or info.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
            raise BundleError("Duplicate or nonregular archive entry")
        if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise BundleError("Unsupported archive encoding")
        seen.add(name.casefold())
    if MANIFEST not in archive.namelist() or archive.getinfo(MANIFEST).file_size > MAX_MANIFEST:
        raise BundleError("Missing or excessive bundle manifest")
    value = _json(archive.read(MANIFEST))
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise BundleError("Unknown bundle schema")
    rows = _file_rows(value.get("files"))
    if {r["path"] for r in rows} != set(archive.namelist()) - {MANIFEST}:
        raise BundleError("Archive differs from manifest")
    if sum(r["bytes"] for r in rows) > max_bytes:
        raise BundleError("Extraction exceeds byte budget")
    for row in rows:
        if archive.getinfo(row["path"]).file_size != row["bytes"]:
            raise BundleError("Declared size mismatch")
    return value, rows


def _copy_verified(archive, rows, stage=None):
    for row in rows:
        digest, size = hashlib.sha256(), 0
        target = None
        if stage is not None:
            path = stage.joinpath(*row["path"].split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            target = path.open("xb")
        try:
            with archive.open(row["path"]) as src:
                while data := src.read(CHUNK):
                    size += len(data)
                    if size > row["bytes"]:
                        raise BundleError("Expanded file exceeds declared size")
                    digest.update(data)
                    if target:
                        target.write(data)
            if size != row["bytes"] or digest.hexdigest() != row["sha256"]:
                raise BundleError("Archive checksum mismatch: " + row["path"])
        finally:
            if target:
                target.close()


def verify(bundle, *, expected_sha256=None, max_bytes=DEFAULT_MAX_BYTES):
    if expected_sha256:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or _digest(bundle)[1] != expected_sha256:
            raise BundleError("Archive SHA-256 differs from operator pin")
    try:
        with zipfile.ZipFile(bundle) as archive:
            manifest, rows = _archive_manifest(archive, max_bytes)
            _copy_verified(archive, rows)
        return manifest
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        raise BundleError("Archive verification failed") from exc


def install(bundle, destination, *, expected_sha256, max_bytes=DEFAULT_MAX_BYTES):
    """Verify in staging, then publish with exclusive file creation; never run contents."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise BundleError("Destination must not exist; upgrades require a new directory")
    if not expected_sha256 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise BundleError("Installation requires an independently supplied archive SHA-256")
    if _digest(bundle)[1] != expected_sha256:
        raise BundleError("Archive SHA-256 differs from operator pin")
    parent = destination.parent
    if not parent.is_dir() or parent.is_symlink():
        raise BundleError("Installation parent must exist and must not be a link")
    _no_link_ancestors(parent)
    published, directories = [], []
    created = False
    with tempfile.TemporaryDirectory(prefix=".neuromorph-stage-", dir=parent) as temporary:
        stage = Path(temporary)
        with zipfile.ZipFile(bundle) as archive:
            manifest, rows = _archive_manifest(archive, max_bytes)
            # Two copies may be needed on filesystems without hard links (e.g. exFAT).
            if shutil.disk_usage(parent).free < 2 * sum(r["bytes"] for r in rows) + MAX_MANIFEST:
                raise BundleError("Insufficient extraction storage")
            _copy_verified(archive, rows, stage)
        (stage / MANIFEST).write_bytes(_encoded(manifest))
        try:
            destination.mkdir()  # exclusive; refuses even an existing empty directory
            created = True
            for source in sorted(stage.rglob("*")):
                target = destination / source.relative_to(stage)
                if source.is_dir():
                    target.mkdir()
                    directories.append(target)
                else:
                    # Same-filesystem hard-link creation never replaces a destination.
                    try:
                        os.link(source, target)
                    except OSError as exc:
                        if exc.errno not in {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}:
                            raise
                        # A filesystem without links still gets exclusive creation,
                        # bounded copies, and no execution. Existing paths fail here.
                        with target.open("xb") as out:
                            own = target.stat()
                            published.append((target, own.st_dev, own.st_ino))
                            with source.open("rb") as src:
                                shutil.copyfileobj(src, out, CHUNK)
                    else:
                        own = target.stat()
                        published.append((target, own.st_dev, own.st_ino))
            if os.name != "nt":
                (destination / "start.sh").chmod(0o755)
        except BaseException:
            for target, device, inode in reversed(published):
                try:
                    actual = target.lstat()
                    if stat.S_ISREG(actual.st_mode) and (actual.st_dev, actual.st_ino) == (device, inode):
                        target.unlink()
                except OSError:
                    pass
            for directory in reversed(directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if created:
                try:
                    destination.rmdir()
                except OSError:
                    pass
            raise
    return {"destination": str(destination), "files": len(rows), "executed": False,
            "source_commit": manifest.get("source_commit"), "model_portability": "not_verified"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build")
    make.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    make.add_argument("--output", type=Path, required=True)
    for kind in ("model", "python"):
        make.add_argument("--" + kind + "-root", type=Path)
        make.add_argument("--" + kind + "-manifest", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("bundle", type=Path)
    check.add_argument("--expected-sha256")
    extract = commands.add_parser("install")
    extract.add_argument("bundle", type=Path)
    extract.add_argument("destination", type=Path)
    extract.add_argument("--expected-sha256", required=True)
    for command in (make, check, extract):
        command.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    try:
        result = {"build": build, "verify": verify, "install": install}[command](**args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BundleError, OSError, zipfile.BadZipFile) as exc:
        parser.exit(2, "Bundle failed: " + str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
