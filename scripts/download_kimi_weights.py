"""Download only the 96 pinned public Kimi-K3 weight shards. No code execution.

Standard library, anonymous HTTPS, resumable validated ranges, bounded retries.
The production entrypoint accepts one fixed destination and one pinned manifest.
Signed redirect URLs and remote error bodies are never written to logs/state.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import BaseHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
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


class RequestGate(BaseHandler):
    handler_order = 0

    def __init__(self, check):
        self.check = check

    def https_request(self, request):
        # Also invoked for each redirected request, not just the first HF URL.
        self.check()
        return request


def make_opener(request_gate=None):
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # No implicit proxies, cookies, auth handlers, Hugging Face cache or tokens.
    handlers = [ProxyHandler({}), FixedRedirect(), HTTPSHandler(context=context)]
    if request_gate is not None:
        handlers.append(RequestGate(request_gate))
    return build_opener(*handlers)


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


def hash_file(path, stop_file, progress=None, *, cancel=None):
    digest = hashlib.sha256()
    read = 0
    with safe_path(path, allow_missing=False).open("rb") as stream:
        while True:
            stop_requested(stop_file)
            if cancel is not None and cancel.is_set():
                raise Stopped()
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
        self.lock = threading.RLock()
        self.request_lock = threading.RLock()
        self.disk_lock = threading.RLock()
        self.cancel = threading.Event()
        self.first_error = None
        self.writers = set()
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
            gate = value.get("global_retry_at", max(value["retry_at"].values(), default=0))
            if type(gate) not in (int, float) or not 0 <= gate <= 32503680000:
                raise DownloadError("invalid_download_state")
            # Never preserve arbitrary strings from a previous modified state.
            self.value = {"schema_version": 1, "revision": REVISION, "manifest_sha256": MANIFEST_SHA256,
                "retries": value["retries"], "retry_at": value["retry_at"], "verified": value["verified"],
                "global_retry_at": gate, "history": []}
        else:
            self.value = {"schema_version": 1, "revision": REVISION, "manifest_sha256": MANIFEST_SHA256,
                "retries": {}, "retry_at": {}, "verified": [], "global_retry_at": 0, "history": []}
        self.value.update(active_files={}, verified_this_run=[], workers=1)

    def update(self, status, filename=None, *, offset=None, force=False):
        with self.lock:
            now = time.time()
            self.value.update(status=status, updated_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
            if filename is not None:
                if filename not in self.filenames:
                    raise DownloadError("invalid_progress_identity")
                self.value["current_file"] = filename
                if filename in self.writers:
                    active = self.value["active_files"].setdefault(filename, {"status": status, "bytes": 0})
                    active["status"] = status
                    if offset is not None:
                        active["bytes"] = offset
            if offset is not None:
                self.value["current_file_bytes"] = offset
            if force or time.monotonic() - self.last_progress >= 3:
                atomic_json(self.path, self.value)
                self.last_progress = time.monotonic()

    def event(self, code, filename=None):
        with self.lock:
            self.value["history"] = (self.value["history"] + [{"code": code, "file": filename}])[-20:]
            self.update(code, filename, force=True)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.value, allow_nan=False))

    def check_stop(self, stop_file):
        if self.cancel.is_set():
            raise Stopped()
        stop_requested(stop_file)

    def fail(self, error):
        with self.lock:
            if self.first_error is None:
                self.first_error = error
            self.cancel.set()

    def start_file(self, name):
        with self.lock:
            if name not in self.filenames or name in self.writers:
                raise DownloadError("duplicate_or_unknown_shard_writer")
            self.writers.add(name)
            try:
                self.update("starting_shard", name, offset=0, force=True)
            except BaseException:
                self.writers.discard(name)
                self.value["active_files"].pop(name, None)
                raise

    def end_file(self, name):
        with self.lock:
            self.writers.discard(name)
            self.value["active_files"].pop(name, None)
            self.update(self.value.get("status", "idle"), force=True)

    def verified(self, name, total):
        with self.lock:
            for key in ("verified", "verified_this_run"):
                if name not in self.value[key]:
                    self.value[key].append(name)
            self.update("shard_verified", name, offset=total, force=True)
            self.event("shard_verified", name)

    def retry_count(self, name):
        with self.lock:
            return self.value["retries"].get(name, 0)

    def wait_ready(self, name, stop_file, clock=time.time, sleep=time.sleep):
        while True:
            self.check_stop(stop_file)
            with self.lock:
                if self.value["retries"].get(name, 0) > MAX_RETRIES:
                    raise DownloadError("file_retry_limit_reached")
                until = max(self.value["global_retry_at"], self.value["retry_at"].get(name, 0))
            delay = until - clock()
            if delay > MAX_WAIT_SECONDS:
                raise DownloadError("retry_later")
            if delay <= 0:
                return
            sleep(min(1, delay))

    def global_backoff(self, headers, attempt, clock):
        with self.lock:
            deadline = clock() + retry_delay(headers, attempt, clock())
            if deadline > 32503680000:
                raise DownloadError("invalid_retry_deadline")
            self.value["global_retry_at"] = max(self.value["global_retry_at"], deadline)
            self.event("global_rate_limit_wait")

    def retry(self, name, headers, code, clock):
        with self.lock:
            used = self.value["retries"].get(name, 0)
            if used >= MAX_RETRIES:
                self.value["retries"][name] = MAX_RETRIES + 1
                self.event("file_retry_limit_reached", name)
                raise DownloadError("file_retry_limit_reached")
            deadline = clock() + retry_delay(headers, used + 1, clock())
            if deadline > 32503680000:
                raise DownloadError("invalid_retry_deadline")
            self.value["retries"][name] = used + 1
            self.value["retry_at"][name] = deadline
            self.event(code, name)

    def clear_retry_at(self, name):
        with self.lock:
            self.value["retry_at"].pop(name, None)

    def open_request(self, name, stop_file, opener, request, clock, sleep):
        # Serialize request admission and response headers only, not body streaming.
        # A 429 gate is durable before the next worker can admit a fresh request.
        with self.request_lock:
            self.wait_ready(name, stop_file, clock, sleep)
            try:
                return opener.open(request, timeout=TIMEOUT)
            except HTTPError as error:
                if error.code == 429 or (error.headers and error.headers.get("Retry-After")):
                    try:
                        self.global_backoff(error.headers, self.retry_count(name) + 1, clock)
                    except BaseException:
                        error.close()
                        raise
                raise


def wait_until(until, stop_file, clock=time.time, sleep=time.sleep):
    if until - clock() > MAX_WAIT_SECONDS:
        raise DownloadError("retry_later")
    while until > clock():
        stop_requested(stop_file)
        sleep(min(1, until - clock()))


def download_file(entry, root, state, stop_file, opener, *, disk_check=lambda: None,
                  clock=time.time, sleep=time.sleep):
    """Streaming helper; caller holds the exclusive download directory lock."""
    state.check_stop(stop_file)
    name = entry["path"]
    state.start_file(name)
    try:
        return _download_file(entry, root, state, stop_file, opener,
                              disk_check=disk_check, clock=clock, sleep=sleep)
    finally:
        state.end_file(name)


def _download_file(entry, root, state, stop_file, opener, *, disk_check, clock, sleep):
    name, total = entry["path"], entry["bytes"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise DownloadError("invalid_weight_name")
    final = safe_path(root / name)
    partial = safe_path(root / (name + ".part"))
    if final.exists() and partial.exists():
        raise DownloadError("conflicting_weight_files")
    state.check_stop(stop_file)
    if final.exists():
        if final.stat().st_size != total:
            raise DownloadError("weight_size_mismatch")
        state.update("verifying_existing", name, offset=0, force=True)
        digest = hash_file(final, stop_file, lambda n: state.update("verifying_existing", name, offset=n), cancel=state.cancel)
        if digest.hexdigest() != entry["sha256"]:
            raise DownloadError("existing_weight_hash_mismatch")
    else:
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > total:
            raise DownloadError("partial_size_exceeds_manifest")
        state.update("hashing_partial", name, offset=0, force=True)
        digest = hash_file(partial, stop_file, lambda n: state.update("hashing_partial", name, offset=n), cancel=state.cancel) if offset else hashlib.sha256()
        url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}"
        while offset < total:
            state.wait_ready(name, stop_file, clock, sleep)
            # Keep path enumeration coherent with another shard's atomic promotion.
            with state.disk_lock:
                disk_check()
            end = min(total - 1, offset + RANGE_BYTES - 1)
            request = Request(url, headers={"Range": f"bytes={offset}-{end}", "Accept-Encoding": "identity",
                "User-Agent": "NeuroMorf-Pinned-Weights/1", "Accept": "application/octet-stream"})
            retry_headers = None
            retry_code = None
            state.update("downloading", name, offset=offset, force=True)
            try:
                with state.open_request(name, stop_file, opener, request, clock, sleep) as response:
                    range_headers(response, offset, end, total)
                    # Headers have been validated BEFORE opening the file for append.
                    safe_path(partial)
                    with partial.open("ab") as stream:
                        if stream.tell() != offset:
                            raise DownloadError("partial_changed_during_download")
                        remaining = end - offset + 1
                        while remaining:
                            state.check_stop(stop_file)
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
                state.clear_retry_at(name)
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
            state.retry(name, retry_headers, retry_code, clock)
            state.wait_ready(name, stop_file, clock, sleep)
        if digest.hexdigest() != entry["sha256"]:
            raise DownloadError("downloaded_weight_hash_mismatch")
        with state.disk_lock:
            state.check_stop(stop_file)
            if partial.stat().st_size != total or final.exists():
                raise DownloadError("weight_promotion_conflict")
            safe_path(final)
            safe_path(partial)
            os.replace(partial, final)  # Only fully SHA256-verified bytes acquire final name.
    state.verified(name, total)


def download_files(entries, root, state, stop_file, *, workers=2, opener_factory=None,
                   disk_check=lambda: None, clock=time.time, sleep=time.sleep):
    """Bounded parallel shards, one writer/opener per shard and no shared body.

    Pending shards run before existing finals are rehashed. All selected entries
    must pass SHA verification in this process before the call can succeed.
    The caller holds the single OS directory lock for the complete operation.
    """
    if type(workers) is not int or not 1 <= workers <= 4:
        raise DownloadError("invalid_workers")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 96:
        raise DownloadError("invalid_download_batch")
    names = [entry["path"] for entry in entries]
    if (any(not isinstance(name, str) or not _NAME.fullmatch(name) or name not in state.filenames for name in names)
            or len(names) != len(set(names))):
        raise DownloadError("duplicate_or_unknown_shard_writer")
    state.check_stop(stop_file)
    with state.lock:
        if state.writers:
            raise DownloadError("download_batch_already_running")
        state.value["workers"] = workers
        state.update("parallel_batch_started", force=True)
    pending, existing = [], []
    for entry in entries:
        (existing if safe_path(root / entry["path"]).exists() else pending).append(entry)
    # A previously exhausted shard is a terminal batch condition, even when its
    # partial is large; do not start other network requests while rehashing it.
    if any(state.retry_count(entry["path"]) > MAX_RETRIES for entry in pending):
        raise DownloadError("file_retry_limit_reached")

    def perform(entry):
        try:
            state.check_stop(stop_file)
            opener = opener_factory() if opener_factory is not None else make_opener(
                lambda: state.wait_ready(entry["path"], stop_file, clock, sleep))
            download_file(entry, root, state, stop_file, opener,
                          disk_check=disk_check, clock=clock, sleep=sleep)
            return entry["path"]
        except BaseException as error:
            state.fail(error)
            raise

    verified = []
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kimi-shard")
    futures = []
    try:
        # Existing finals do not occupy a worker until pending network work ends.
        futures = [pool.submit(perform, entry) for entry in pending]
        for future in as_completed(futures):
            verified.append(future.result())
        # Sequential rehash avoids parallel full-file seeks on the target HDD.
        for entry in existing:
            verified.append(perform(entry))
    except BaseException as error:
        state.fail(error)
        for future in futures:
            future.cancel()
        raise state.first_error
    finally:
        # Active response reads are timeout-bounded and check cancel per chunk.
        # The OS directory lock remains held while all cooperative writers exit.
        pool.shutdown(wait=True, cancel_futures=True)
    return {"workers": workers, "processed_files": len(verified), "verified_files": sorted(verified)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download pinned Kimi-K3 weights only, to the authorized D: model directory")
    parser.add_argument("--manifest", default=str(Path(__file__).resolve().parents[1] / "config" / "kimi_weights_manifest.json"))
    parser.add_argument("--destination", default=TARGET)
    parser.add_argument("--max-files", type=int, default=96)
    parser.add_argument("--workers", type=int, default=2, help="Parallel shards (1-4); each has one append writer")
    args = parser.parse_args(argv)
    state = None
    try:
        if os.name != "nt" or Path(args.destination).absolute() != Path(TARGET).absolute():
            raise DownloadError("unsupported_or_unauthorized_destination")
        if not 1 <= args.max_files <= 96:
            raise DownloadError("invalid_max_files")
        if not 1 <= args.workers <= 4:
            raise DownloadError("invalid_workers")
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
                batch = download_files(manifest["files"][:args.max_files], root, state, stop,
                    workers=args.workers, disk_check=lambda: check_disk(root, manifest["files"]))
                current = state.snapshot()
                completed = args.max_files == 96 and batch["processed_files"] == 96 and len(current["verified_this_run"]) == 96
                state.event("complete" if completed else "bounded_batch_complete")
                current = state.snapshot()
                print(json.dumps({"status": current["status"], "verified_shards": len(current["verified"]),
                    "verified_this_run": len(current["verified_this_run"]), "workers": args.workers,
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
    except Exception:
        # Includes unexpected thread/transport failures; exception text may
        # contain a signed URL and must never become an unhandled traceback.
        code = "invalid_state_or_local_io"
    print(json.dumps({"status": code, "weights_only": True, "model_execution": False}))
    return 75 if code == "retry_later" else 2


if __name__ == "__main__":
    raise SystemExit(main())
