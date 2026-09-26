"""Download only the 96 pinned public Kimi-K3 weight shards. No code execution.

Standard library, anonymous HTTPS, resumable validated ranges, bounded retries.
The production entrypoint accepts one fixed destination and one pinned manifest.
Signed redirect URLs and remote error bodies are never written to logs/state.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
import uuid

REPOSITORY = "moonshotai/Kimi-K3"
REVISION = "f831ab66814297da540d832a5235f8e904f29d06"
MANIFEST_SHA256 = "c1105756598137e827b1b99486519a2c735b0b680ae6ffc2c4cc276806ced14d"
TOTAL_BYTES = 1560936091448
TARGET = "D:/NeuroMorf/models/Kimi-K3-" + REVISION
# Root's live pinned-shard Range 0-0 probe observed this exact CDN on 2026-09-26.
ALLOWED_HOSTS = frozenset({"huggingface.co", "us.aws.cdn.hf.co"})
RESERVE_BYTES = 120 * 1024**3
READ_BYTES = 1024**2
RANGE_BYTES = 256 * 1024**2
MAX_RETRIES = 5
TIMEOUT = 30
MAX_WAIT_SECONDS = 300
_NAME = re.compile(r"model-(\d{5})-of-000096\.safetensors\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class DownloadError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Stopped(DownloadError):
    def __init__(self):
        super().__init__("stopped")


def safe_path(value, *, directory=False, allow_missing=True):
    path = Path(value).absolute()
    if path.anchor.startswith("\\\\"):
        raise DownloadError("network_path_rejected")
    for component in (*reversed(path.parents), path):
        if ":" in component.name or component.name.rstrip(" .") != component.name:
            raise DownloadError("invalid_local_path")
        try:
            info = component.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise DownloadError("missing_local_file") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DownloadError("link_or_reparse_point_rejected")
        if component != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise DownloadError("invalid_local_directory")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DownloadError("invalid_local_file")
    return path


def atomic_json(path, value):
    path = safe_path(path)
    raw = (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(raw) > 65536:
        raise DownloadError("state_size_limit")
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(path)
        os.replace(temporary, path)
        if os.name != "nt":
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def exclusive_lock(path):
    path = safe_path(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        info, current = os.fstat(descriptor), path.lstat()
        if info.st_nlink != 1 or info.st_size > 1 or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise DownloadError("invalid_download_lock")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DownloadError("download_already_running") from None
        locked = True
        if info.st_size == 0:
            os.write(descriptor, b"1")
            os.fsync(descriptor)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def load_manifest(path):
    with safe_path(path, allow_missing=False).open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise DownloadError("manifest_pin_mismatch")
    value = json.loads(raw)
    if (value.get("schema_version") != 1 or value.get("repository") != REPOSITORY or value.get("revision") != REVISION
            or value.get("shard_count") != 96 or value.get("total_bytes") != TOTAL_BYTES
            or not isinstance(value.get("files"), list) or len(value["files"]) != 96):
        raise DownloadError("manifest_contract_mismatch")
    for index, entry in enumerate(value["files"], 1):
        if (set(entry) != {"path", "bytes", "sha256"} or entry["path"] != f"model-{index:05d}-of-000096.safetensors"
                or type(entry["bytes"]) is not int or entry["bytes"] <= 0
                or not isinstance(entry["sha256"], str) or not _SHA.fullmatch(entry["sha256"])):
            raise DownloadError("invalid_weight_entry")
    if sum(entry["bytes"] for entry in value["files"]) != TOTAL_BYTES:
        raise DownloadError("manifest_total_mismatch")
    return value


def allowed_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
                and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment)
    except ValueError:
        return False


class FixedRedirect(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not allowed_url(newurl):
            raise DownloadError("unapproved_redirect_host")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def make_opener():
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # No implicit proxies, cookies, auth handlers, Hugging Face cache or tokens.
    return build_opener(ProxyHandler({}), FixedRedirect(), HTTPSHandler(context=context))


def range_headers(response, offset, end, total):
    if response.status != 206:
        raise DownloadError("range_not_honoured")
    expected = f"bytes {offset}-{end}/{total}"
    if response.headers.get("Content-Range") != expected:
        raise DownloadError("content_range_mismatch")
    if response.headers.get("Content-Length") != str(end - offset + 1):
        raise DownloadError("content_length_mismatch")
    if response.headers.get("Content-Encoding", "identity").lower() not in {"identity", ""}:
        raise DownloadError("encoded_weight_response")
    if not allowed_url(response.geturl()):
        raise DownloadError("unapproved_response_host")


def stop_requested(stop_file):
    if safe_path(stop_file).exists():
        raise Stopped()


def hash_file(path, stop_file, progress=None):
    digest = hashlib.sha256()
    read = 0
    with safe_path(path, allow_missing=False).open("rb") as stream:
        while True:
            stop_requested(stop_file)
            chunk = stream.read(READ_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            read += len(chunk)
            if progress:
                progress(read)
    return digest


def available_bytes(root, files):
    amount = 0
    for entry in files:
        final = safe_path(root / entry["path"])
        partial = safe_path(root / (entry["path"] + ".part"))
        if final.exists() and partial.exists():
            raise DownloadError("conflicting_weight_files")
        path = final if final.exists() else partial
        if path.exists():
            size = path.stat().st_size
            if size > entry["bytes"] or (path == final and size != entry["bytes"]):
                raise DownloadError("weight_size_mismatch")
            amount += size
    return amount


def check_disk(root, files, free_bytes=None):
    remaining = sum(entry["bytes"] for entry in files) - available_bytes(root, files)
    free = shutil.disk_usage(root).free if free_bytes is None else free_bytes
    if free < remaining + RESERVE_BYTES:
        raise DownloadError("insufficient_disk_reserve")
    return remaining


def retry_delay(headers, attempt, now):
    raw = headers.get("Retry-After") if headers else None
    if raw:
        try:
            delay = int(raw) if raw.strip().isdigit() else parsedate_to_datetime(raw).timestamp() - now
            return max(0, float(delay))
        except (ValueError, TypeError, OverflowError):
            pass
    return min(120, 2 ** attempt)


class State:
    def __init__(self, root, filenames):
        self.path = safe_path(root / "download-state.json")
        self.filenames = set(filenames)
        self.last_progress = 0.0
        if self.path.exists():
            with self.path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise DownloadError("invalid_download_state")
            self.value = json.loads(raw)
            value = self.value
            if (not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("revision") != REVISION
                    or value.get("manifest_sha256") != MANIFEST_SHA256 or not isinstance(value.get("retries"), dict)
                    or set(value["retries"]) - self.filenames
                    or any(type(n) is not int or not 0 <= n <= MAX_RETRIES + 1 for n in value["retries"].values())
                    or not isinstance(value.get("retry_at"), dict) or set(value["retry_at"]) - self.filenames
                    or any(type(n) not in (int, float) or not 0 <= n <= 32503680000 for n in value["retry_at"].values())
                    or not isinstance(value.get("verified"), list) or set(value["verified"]) - self.filenames
                    or len(value["verified"]) != len(set(value["verified"]))):
                raise DownloadError("invalid_download_state")
            # Never preserve arbitrary strings from a previous modified state.
            self.value = {"schema_version": 1, "revision": REVISION, "manifest_sha256": MANIFEST_SHA256,
                "retries": value["retries"], "retry_at": value["retry_at"], "verified": value["verified"], "history": []}
        else:
            self.value = {"schema_version": 1, "revision": REVISION, "manifest_sha256": MANIFEST_SHA256,
                "retries": {}, "retry_at": {}, "verified": [], "history": []}

    def update(self, status, filename=None, *, offset=None, force=False):
        now = time.time()
        self.value.update(status=status, updated_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
        if filename is not None:
            if filename not in self.filenames:
                raise DownloadError("invalid_progress_identity")
            self.value["current_file"] = filename
        if offset is not None:
            self.value["current_file_bytes"] = offset
        if force or time.monotonic() - self.last_progress >= 3:
            atomic_json(self.path, self.value)
            self.last_progress = time.monotonic()

    def event(self, code, filename=None):
        self.value["history"] = (self.value["history"] + [{"code": code, "file": filename}])[-20:]
        self.update(code, filename, force=True)


def wait_until(until, stop_file, clock=time.time, sleep=time.sleep):
    if until - clock() > MAX_WAIT_SECONDS:
        raise DownloadError("retry_later")
    while until > clock():
        stop_requested(stop_file)
        sleep(min(1, until - clock()))


def download_file(entry, root, state, stop_file, opener, *, disk_check=lambda: None,
                  clock=time.time, sleep=time.sleep):
    """Streaming helper; caller holds the exclusive download directory lock."""
    name, total = entry["path"], entry["bytes"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise DownloadError("invalid_weight_name")
    final = safe_path(root / name)
    partial = safe_path(root / (name + ".part"))
    if final.exists() and partial.exists():
        raise DownloadError("conflicting_weight_files")
    stop_requested(stop_file)
    if final.exists():
        if final.stat().st_size != total:
            raise DownloadError("weight_size_mismatch")
        state.update("verifying_existing", name, offset=0, force=True)
        digest = hash_file(final, stop_file, lambda n: state.update("verifying_existing", name, offset=n))
        if digest.hexdigest() != entry["sha256"]:
            raise DownloadError("existing_weight_hash_mismatch")
    else:
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > total:
            raise DownloadError("partial_size_exceeds_manifest")
        state.update("hashing_partial", name, offset=0, force=True)
        digest = hash_file(partial, stop_file, lambda n: state.update("hashing_partial", name, offset=n)) if offset else hashlib.sha256()
        url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}"
        while offset < total:
            stop_requested(stop_file)
            if state.value["retries"].get(name, 0) > MAX_RETRIES:
                raise DownloadError("file_retry_limit_reached")
            wait_until(state.value["retry_at"].get(name, 0), stop_file, clock, sleep)
            disk_check()
            end = min(total - 1, offset + RANGE_BYTES - 1)
            request = Request(url, headers={"Range": f"bytes={offset}-{end}", "Accept-Encoding": "identity",
                "User-Agent": "NeuroMorf-Pinned-Weights/1", "Accept": "application/octet-stream"})
            retry_headers = None
            retry_code = None
            state.update("downloading", name, offset=offset, force=True)
            try:
                with opener.open(request, timeout=TIMEOUT) as response:
                    range_headers(response, offset, end, total)
                    # Headers have been validated BEFORE opening the file for append.
                    safe_path(partial)
                    with partial.open("ab") as stream:
                        if stream.tell() != offset:
                            raise DownloadError("partial_changed_during_download")
                        remaining = end - offset + 1
                        while remaining:
                            stop_requested(stop_file)
                            chunk = response.read(min(READ_BYTES, remaining))
                            if not chunk:
                                raise http.client.IncompleteRead(b"", remaining)
                            stream.write(chunk)
                            digest.update(chunk)
                            offset += len(chunk)
                            remaining -= len(chunk)
                            state.update("downloading", name, offset=offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                state.value["retry_at"].pop(name, None)
                state.update("downloading", name, offset=offset, force=True)
                continue
            except HTTPError as error:
                status = error.code
                retry_headers = error.headers
                error.close()
                if status not in (408, 429, 500, 502, 503, 504):
                    raise DownloadError("http_access_or_request_rejected") from None
                retry_code = "rate_limited" if status == 429 else "transient_http_error"
            except (URLError, TimeoutError, ConnectionError, http.client.HTTPException):
                retry_code = "transport_interrupted"
            except OSError:
                # Disk/write failures are terminal, not retried as network failures.
                raise DownloadError("local_io_failure") from None
            used = state.value["retries"].get(name, 0)
            if used >= MAX_RETRIES:
                state.value["retries"][name] = MAX_RETRIES + 1
                state.event("file_retry_limit_reached", name)
                raise DownloadError("file_retry_limit_reached")
            state.value["retries"][name] = used + 1
            state.value["retry_at"][name] = clock() + retry_delay(retry_headers, used + 1, clock())
            state.event(retry_code, name)
            wait_until(state.value["retry_at"][name], stop_file, clock, sleep)
        if digest.hexdigest() != entry["sha256"]:
            raise DownloadError("downloaded_weight_hash_mismatch")
        if partial.stat().st_size != total or final.exists():
            raise DownloadError("weight_promotion_conflict")
        safe_path(final)
        safe_path(partial)
        os.replace(partial, final)  # Only fully SHA256-verified bytes acquire final name.
    if name not in state.value["verified"]:
        state.value["verified"].append(name)
    state.update("shard_verified", name, offset=total, force=True)
    state.event("shard_verified", name)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download pinned Kimi-K3 weights only, to the authorized D: model directory")
    parser.add_argument("--manifest", default=str(Path(__file__).resolve().parents[1] / "config" / "kimi_weights_manifest.json"))
    parser.add_argument("--destination", default=TARGET)
    parser.add_argument("--max-files", type=int, default=96)
    args = parser.parse_args(argv)
    state = None
    try:
        if os.name != "nt" or Path(args.destination).absolute() != Path(TARGET).absolute():
            raise DownloadError("unsupported_or_unauthorized_destination")
        if not 1 <= args.max_files <= 96:
            raise DownloadError("invalid_max_files")
        manifest = load_manifest(args.manifest)
        root = safe_path(args.destination, directory=True)
        root.mkdir(parents=True, exist_ok=True)
        safe_path(root, directory=True, allow_missing=False)
        stop = safe_path(root / "STOP")
        with exclusive_lock(root / "download.lock"):
            state = State(root, [entry["path"] for entry in manifest["files"]])
            try:
                stop_requested(stop)
                check_disk(root, manifest["files"])
                state.event("started")
                opener = make_opener()
                for entry in manifest["files"][:args.max_files]:
                    download_file(entry, root, state, stop, opener,
                        disk_check=lambda: check_disk(root, manifest["files"]))
                completed = args.max_files == 96 and len(state.value["verified"]) == 96
                state.event("complete" if completed else "bounded_batch_complete")
                print(json.dumps({"status": state.value["status"], "verified_shards": len(state.value["verified"]),
                    "total_shards": 96, "manifest_sha256": MANIFEST_SHA256}))
                return 0
            except BaseException as error:
                # Keep error state changes inside the directory's exclusive lock.
                code = error.code if isinstance(error, DownloadError) else "interrupted" if isinstance(error, KeyboardInterrupt) else "invalid_state_or_local_io"
                try:
                    state.event(code)
                except (OSError, ValueError, DownloadError):
                    pass
                raise
    except KeyboardInterrupt:
        code = "interrupted"
    except DownloadError as error:
        code = error.code
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        code = "invalid_state_or_local_io"
    print(json.dumps({"status": code, "weights_only": True, "model_execution": False}))
    return 75 if code == "retry_later" else 2


if __name__ == "__main__":
    raise SystemExit(main())
