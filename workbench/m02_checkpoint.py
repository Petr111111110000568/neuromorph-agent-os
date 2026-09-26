"""Bounded, explicit M02 snapshots, with no bootstrap or executable payload.

The caller must quiesce both databases before creating a checkpoint: two SQLite
backups are individually consistent, not a cross-database transaction. Backup
includes committed WAL pages. Only trusted synthetic M02 state belongs here.

An expected digest establishes integrity, not producer authenticity. The cloud
caller verifies repository/workflow/run metadata independently before download.
Publication stages and verifies all files, exclusively reserves the destination,
and publishes the manifest last. Readers require that commit marker. There is no
portable stdlib atomic directory rename-without-replacement; an incomplete
directory must never be treated as a checkpoint. Caller-owned directories must
not be concurrently modified by other processes.
"""
from __future__ import annotations

from contextlib import closing
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time


SCHEMA_VERSION = 1
FILES = ("control.sqlite", "queue.sqlite", "seed-receipt.json")
MANIFEST = "manifest.json"
MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 2 * MAX_DATABASE_BYTES + MAX_RECEIPT_BYTES
MAX_MANIFEST_BYTES = 16 * 1024
_CHUNK = 128 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_FIELDS = {"repository", "workflow_id", "branch", "commit", "run_id"}
_SIDECARS = tuple(name + suffix for name in FILES[:2] for suffix in ("-wal", "-shm", "-journal"))


class CheckpointError(ValueError):
    """The checkpoint cannot be admitted; no state reset is permitted."""


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _identity(value):
    if not isinstance(value, dict) or set(value) != _IDENTITY_FIELDS:
        raise CheckpointError("checkpoint identity has an unsupported schema")
    if not all(isinstance(item, str) and 1 <= len(item) <= 240 for item in value.values()):
        raise CheckpointError("checkpoint identity requires bounded strings")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]):
        raise CheckpointError("invalid repository identity")
    if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml", value["workflow_id"]):
        raise CheckpointError("invalid workflow path identity")
    branch = value["branch"]
    if (not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_./-]*", branch)
            or ".." in branch or "//" in branch or branch.endswith(("/", ".", ".lock"))):
        raise CheckpointError("invalid branch identity")
    if not re.fullmatch(r"[0-9a-f]{40}", value["commit"]):
        raise CheckpointError("invalid commit identity")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value["run_id"]):
        raise CheckpointError("invalid run identity")
    return dict(value)


def _bad_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _path(value):
    path = Path(value)
    if ".." in path.parts:
        raise CheckpointError("parent traversal is not allowed")
    return Path(os.path.abspath(path))


def _directory(value):
    path = _path(value)
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except OSError as exc:
            raise CheckpointError("checkpoint directory is unavailable") from exc
        if _bad_link(info) or not stat.S_ISDIR(info.st_mode):
            raise CheckpointError("checkpoint directories must not be links")
    return path


def _new_destination(value, source):
    path = _path(value)
    _directory(path.parent)
    if path == source or source in path.parents:
        raise CheckpointError("destination must be outside source directory")
    if os.path.lexists(path):
        raise CheckpointError("checkpoint destination already exists")
    return path


def _file_info(path, limit, *, allow_empty=False):
    try:
        info = path.lstat()
    except OSError as exc:
        raise CheckpointError("checkpoint file is missing") from exc
    if _bad_link(info) or not stat.S_ISREG(info.st_mode):
        raise CheckpointError("checkpoint files must be regular non-link files")
    if not (0 if allow_empty else 1) <= info.st_size <= limit:
        raise CheckpointError("checkpoint file exceeds its size bounds")
    return info


def _open_read(path, limit):
    before = _file_info(path, limit)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CheckpointError("checkpoint file cannot be opened") from exc
    try:
        current = os.fstat(descriptor)
        if (_bad_link(current) or not stat.S_ISREG(current.st_mode)
                or (before.st_dev, before.st_ino, before.st_size) !=
                (current.st_dev, current.st_ino, current.st_size)):
            raise CheckpointError("checkpoint file changed before reading")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _hash_file(path, limit):
    digest, length = hashlib.sha256(), 0
    with _open_read(path, limit) as stream:
        while block := stream.read(_CHUNK):
            length += len(block)
            if length > limit:
                raise CheckpointError("checkpoint file grew beyond its size bound")
            digest.update(block)
    return {"bytes": length, "sha256": digest.hexdigest()}


def _read_small(path, limit):
    with _open_read(path, limit) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise CheckpointError("checkpoint JSON exceeds its size bound")
    return raw


def _json_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CheckpointError("duplicate JSON field")
            result[key] = value
        return result

    def constant(_):
        raise CheckpointError("non-finite JSON number")

    def bounded(value, depth=0):
        if depth > 16:
            raise CheckpointError("checkpoint JSON is too deeply nested")
        children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
        for child in children:
            bounded(child, depth + 1)

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(result, dict):
            raise CheckpointError("checkpoint JSON must be an object")
        bounded(result)
        # Reject overflowing JSON exponents, which parse_constant does not see.
        _canonical(result)
        return result
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise CheckpointError("invalid checkpoint JSON") from exc


def _limit(name):
    return MAX_RECEIPT_BYTES if name == "seed-receipt.json" else MAX_DATABASE_BYTES


def _validate_manifest(value, expected_identity):
    if not isinstance(value, dict) or set(value) != {"schema_version", "identity", "files"}:
        raise CheckpointError("unsupported checkpoint manifest schema")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise CheckpointError("unsupported checkpoint schema version")
    identity = _identity(value["identity"])
    if identity != expected_identity:
        raise CheckpointError("checkpoint producer identity mismatch")
    entries = value["files"]
    if not isinstance(entries, dict) or set(entries) != set(FILES):
        raise CheckpointError("checkpoint file allowlist mismatch")
    total = 0
    for name, entry in entries.items():
        if not isinstance(entry, dict) or set(entry) != {"bytes", "sha256"}:
            raise CheckpointError("unsupported checkpoint file schema")
        if type(entry["bytes"]) is not int or not 0 < entry["bytes"] <= _limit(name):
            raise CheckpointError("invalid checkpoint file size")
        if not isinstance(entry["sha256"], str) or not _SHA.fullmatch(entry["sha256"]):
            raise CheckpointError("invalid checkpoint file digest")
        total += entry["bytes"]
    if total > MAX_TOTAL_BYTES:
        raise CheckpointError("checkpoint total exceeds its size bound")
    return value


def _write_new(path, raw):
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _copy_verified(source, target, expected, limit):
    digest, length = hashlib.sha256(), 0
    created = None
    try:
        with _open_read(source, limit) as stream, target.open("xb") as output:
            info = os.fstat(output.fileno())
            created = (info.st_dev, info.st_ino)
            while block := stream.read(_CHUNK):
                length += len(block)
                if length > limit:
                    raise CheckpointError("checkpoint file grew during copy")
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if length != expected["bytes"] or not hmac.compare_digest(digest.hexdigest(), expected["sha256"]):
            raise CheckpointError("checkpoint changed during copy")
    except BaseException:
        if created is not None:
            try:
                info = target.lstat()
                if (info.st_dev, info.st_ino) == created and not _bad_link(info):
                    target.unlink()
            except OSError:
                pass
        raise


def _snapshot_database(source, target):
    _file_info(source, MAX_DATABASE_BYTES)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = source.with_name(source.name + suffix)
        if os.path.lexists(sidecar):
            _file_info(sidecar, MAX_DATABASE_BYTES, allow_empty=True)
    deadline = time.monotonic() + 15
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=2)) as original:
            original.execute("PRAGMA query_only = ON")
            page_size = original.execute("PRAGMA page_size").fetchone()[0]
            page_count = original.execute("PRAGMA page_count").fetchone()[0]
            if page_size * page_count > MAX_DATABASE_BYTES:
                raise CheckpointError("SQLite snapshot exceeds its size bound")

            def progress(status, remaining, total):
                if total * page_size > MAX_DATABASE_BYTES or time.monotonic() > deadline:
                    raise CheckpointError("SQLite snapshot exceeded its resource bound")

            # The staging directory is exclusively created by this process.
            with closing(sqlite3.connect(target, timeout=2)) as snapshot:
                original.backup(snapshot, pages=128, progress=progress, sleep=0.02)
                snapshot.execute("PRAGMA journal_mode = DELETE")
    except sqlite3.Error as exc:
        raise CheckpointError("cannot snapshot SQLite checkpoint") from exc
    _file_info(target, MAX_DATABASE_BYTES)


def _cleanup_stage(stage):
    # Only the fixed files made by this operation; never recursively delete an
    # input directory or a concurrently inserted directory tree.
    for name in (*FILES, MANIFEST, *_SIDECARS):
        path = stage / name
        try:
            if stat.S_ISREG(path.lstat().st_mode) and not _bad_link(path.lstat()):
                path.unlink()
        except OSError:
            pass
    try:
        stage.rmdir()
    except OSError:
        pass


def _publish_stage(stage, destination):
    # mkdir is exclusive on Windows and POSIX; rename alone could replace an
    # existing empty destination directory on POSIX.
    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise CheckpointError("checkpoint destination could not be reserved") from exc
    made = []
    try:
        for name in (*FILES, MANIFEST):
            target = destination / name
            try:
                os.link(stage / name, target)
            except OSError as exc:
                if exc.errno not in {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}:
                    raise
                limit = MAX_MANIFEST_BYTES if name == MANIFEST else _limit(name)
                expected = _hash_file(stage / name, limit)
                _copy_verified(stage / name, target, expected, limit)
            info = target.lstat()
            made.append((target, info.st_dev, info.st_ino))
    except BaseException:
        for target, device, inode in reversed(made):
            try:
                info = target.lstat()
                if (info.st_dev, info.st_ino) == (device, inode) and not _bad_link(info):
                    target.unlink()
            except FileNotFoundError:
                pass
        try:
            destination.rmdir()
        except OSError:
            pass
        raise


def create_checkpoint(source_dir, destination, identity) -> str:
    """Snapshot quiescent trusted SQLite state; return manifest file SHA-256."""
    identity = _identity(identity)
    source = _directory(source_dir)
    destination = _new_destination(destination, source)
    names = {item.name for item in source.iterdir()}
    # Restored state retains its previous manifest; it is not copied as the
    # authority for a new checkpoint, whose identity is supplied by the caller.
    if not set(FILES) <= names or names - set(FILES) - set(_SIDECARS) - {MANIFEST}:
        raise CheckpointError("source checkpoint file allowlist mismatch")
    receipt = _read_small(source / "seed-receipt.json", MAX_RECEIPT_BYTES)
    _json_object(receipt)
    stage = Path(tempfile.mkdtemp(prefix=".m02-checkpoint-", dir=destination.parent))
    try:
        for name in FILES[:2]:
            _snapshot_database(source / name, stage / name)
        _write_new(stage / "seed-receipt.json", receipt)
        manifest = {"schema_version": SCHEMA_VERSION, "identity": identity,
                    "files": {name: _hash_file(stage / name, _limit(name)) for name in FILES}}
        _validate_manifest(manifest, identity)
        encoded = _canonical(manifest)
        _write_new(stage / MANIFEST, encoded)
        _publish_stage(stage, destination)
        return hashlib.sha256(encoded).hexdigest()
    finally:
        _cleanup_stage(stage)


def restore_checkpoint(bundle_dir, destination, expected_manifest_sha, expected_identity) -> dict:
    """Verify the complete fixed bundle before copying it into fresh state.

    Restore never opens SQLite, bootstraps tables, returns reservations, or
    interprets the receipt as executable instructions.
    """
    identity = _identity(expected_identity)
    if not isinstance(expected_manifest_sha, str) or not _SHA.fullmatch(expected_manifest_sha):
        raise CheckpointError("invalid expected checkpoint digest")
    source = _directory(bundle_dir)
    destination = _new_destination(destination, source)
    if {item.name for item in source.iterdir()} != set(FILES) | {MANIFEST}:
        raise CheckpointError("checkpoint bundle contains missing or extra files")
    raw = _read_small(source / MANIFEST, MAX_MANIFEST_BYTES)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_manifest_sha):
        raise CheckpointError("checkpoint manifest digest mismatch")
    manifest = _validate_manifest(_json_object(raw), identity)
    # Verify every payload before any payload copy, database open, or state write.
    for name in FILES:
        actual = _hash_file(source / name, _limit(name))
        expected = manifest["files"][name]
        if actual["bytes"] != expected["bytes"] or not hmac.compare_digest(actual["sha256"], expected["sha256"]):
            raise CheckpointError("checkpoint payload digest mismatch")
    _json_object(_read_small(source / "seed-receipt.json", MAX_RECEIPT_BYTES))
    stage = Path(tempfile.mkdtemp(prefix=".m02-checkpoint-", dir=destination.parent))
    try:
        for name in FILES:
            _copy_verified(source / name, stage / name, manifest["files"][name], _limit(name))
        _write_new(stage / MANIFEST, raw)
        _publish_stage(stage, destination)
        return manifest
    finally:
        _cleanup_stage(stage)
