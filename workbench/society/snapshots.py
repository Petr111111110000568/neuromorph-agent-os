"""Inspect pinned public source archives without importing or executing them.

License text checks are deliberately conservative screening, not legal advice or
a claim that every embedded dependency grants identical rights.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import stat
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 40 * 1024 * 1024
MAX_MEMBERS = 1000
MAX_LICENSE_BYTES = 256 * 1024
MAX_RATIO = 200
ALLOWED_LICENSES = frozenset({"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC"})
_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}\Z")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}\Z")
_LICENSE_NAME = re.compile(r"(?:licen[sc]e|copying)(?:[.-](?:txt|md|rst))?\Z", re.I)
_NOTICE_NAME = re.compile(r"notice(?:[.-](?:txt|md|rst))?\Z", re.I)
_PROHIBITED_TERMS = re.compile(
    r"commons\s+clause|business\s+source\s+license|server\s+side\s+public\s+license|"
    r"non[ -]?commercial|not\s+for\s+commercial|research\s+(?:purposes|use)\s+only|"
    r"additional\s+(?:restrictions|usage\s+restrictions)|ethical\s+use\s+license|"
    r"source\s+available\s+license|hippocratic\s+license", re.I)
_CUSTOM_RESTRICTIONS = re.compile(
    r"(?:must|shall|may)\s+not\s+be\s+used|(?:you|users?)\s+(?:must|shall)\s+not\s+use|"
    r"prohibited\s+(?:uses?|purposes?)|additional\s+conditions|all\s+advertising\s+materials", re.I)
_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)
_APACHE_CANONICAL_SHA256 = frozenset({
    # Lowercase + whitespace-normalized full Apache 2.0, with/without its appendix.
    "948703bcf1cb4a2dafd21676dd01e40a58fe21bd9b425c000b9070eccb441092",
    "95cef6332b35354c12f9d666ab9ff47002e6f7ef937924896882b2e0cdb7a0d6",
})
_MIT_PERMISSIONS_SHA256 = "56959050891f7b737ac4e803051170c7197f02df73d80bd9a78d968e305bf558"
_BSD_BODY = r'''redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met: (?:1\. |\* )?redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer\. (?:2\. |\* )?redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution\. {third}this software is provided by {owner} "as is" and any express or implied warranties, including, but not limited to, the implied warranties of merchantability and fitness for a particular purpose are disclaimed\. in no event shall {owner} be liable for any direct, indirect, incidental, special, exemplary, or consequential damages \(including, but not limited to, procurement of substitute goods or services; loss of use, data, or profits; or business interruption\) however caused and on any theory of liability, whether in contract, strict liability, or tort \(including negligence or otherwise\) arising in any way out of the use of this software, even if advised of the possibility of such damage\.'''
_BSD_THIRD = r'''(?:3\. |\* )?neither the name of {owner} nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission\. '''
_ISC_BODY = r'''permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies\. the software is provided "as is" and {owner} disclaims all warranties with regard to this software including all implied warranties of merchantability and fitness\. in no event shall {owner} be liable for any special, direct, indirect, or consequential damages or any damages whatsoever resulting from loss of use, data or profits, whether in an action of contract, negligence or other tortious action, arising out of or in connection with the use or performance of this software\.'''
# Variable ownership labels may name entities; they cannot contain another clause.
_OWNER = r"[\w .,&()'\-]{1,160}"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("source redirects are not permitted")


def _download(url):
    # Honor the host's configured egress proxy; target and TLS verification stay fixed.
    opener = build_opener(_NoRedirect())
    request = Request(url, headers={"User-Agent": "Meta-Harness/0.6 source-inspector",
                                    "Accept": "application/zip", "Accept-Encoding": "identity"})
    deadline = time.monotonic() + 25
    try:
        with opener.open(request, timeout=10) as response:
            if response.geturl() != url or response.status != 200:
                raise ValueError("unexpected source response")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("encoded source responses are not accepted")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    declared = int(length)
                except (TypeError, ValueError):
                    raise ValueError("invalid source Content-Length") from None
                if declared < 0 or declared > MAX_ARCHIVE_BYTES:
                    raise ValueError("source archive exceeds compressed size limit")
            chunks, size = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise ValueError("source download exceeded time budget")
                # read1 returns after one underlying read instead of waiting for a
                # 64 KiB buffer to fill while a server trickles individual bytes.
                chunk = response.read1(min(65536, MAX_ARCHIVE_BYTES + 1 - size))
                if time.monotonic() > deadline:
                    raise ValueError("source download exceeded time budget")
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise ValueError("source archive exceeds compressed size limit")
                chunks.append(chunk)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        # Do not echo exception URLs, proxies, or environment-provided secrets.
        raise ValueError("public source download failed; no archive was installed") from exc


def _license_kind(text):
    """Match complete conservative templates; unusual variants require review."""
    lowered = text.lower()
    normalized = " ".join(lowered.split())
    if _PROHIBITED_TERMS.search(normalized) or _CUSTOM_RESTRICTIONS.search(normalized):
        raise ValueError("custom or restricted license terms require a separate review")
    if _sha(normalized.encode()) in _APACHE_CANONICAL_SHA256:
        return "Apache-2.0"
    starts = ("permission is hereby granted", "redistribution and use in source and binary forms",
              "permission to use, copy, modify, and/or distribute")
    positions = [lowered.find(start) for start in starts if lowered.find(start) >= 0]
    if not positions:
        raise ValueError("license text is unknown or outside the supported canonical templates")
    position = min(positions)
    prefix = lowered[:position].strip()
    permitted_headers = {"mit", "mit license", "the mit license (mit)", "isc", "isc license",
                         "bsd 2-clause license", "bsd 3-clause license", "all rights reserved."}
    copyright_seen = False
    for line in prefix.splitlines():
        line = line.strip()
        if not line or line in permitted_headers:
            continue
        if re.fullmatch(r"copyright\s*(?:\(c\)|©)?\s*(?:\d{4}(?:\s*[-,]\s*\d{4})*)[, ]+[\w .,&()'@<>\-]{1,180}", line):
            if re.search(r"\b(?:requires?|authorization|commercial|permission|prohibited|must|shall)\b", line):
                raise ValueError("copyright prefix contains nonstandard conditions")
            copyright_seen = True
            continue
        raise ValueError("nonstandard license prefix requires a separate review")
    if not copyright_seen:
        raise ValueError("license template requires a copyright ownership notice")
    body = " ".join(lowered[position:].split())
    if _sha(body.encode()) == _MIT_PERMISSIONS_SHA256:
        return "MIT"
    for kind in ("BSD-2-Clause", "BSD-3-Clause"):
        third = _BSD_THIRD.format(owner=_OWNER) if kind == "BSD-3-Clause" else ""
        if re.fullmatch(_BSD_BODY.format(third=third, owner=_OWNER), body):
            return kind
    if re.fullmatch(_ISC_BODY.format(owner=_OWNER), body):
        return "ISC"
    raise ValueError("modified license clauses or unsupported canonical variant require a separate review")


def _validated_members(zipped, repository, commit, remote):
    infos = zipped.infolist()
    if not infos or len(infos) > MAX_MEMBERS:
        raise ValueError("source archive has no entries or exceeds entry limit")
    result, seen, expanded, roots = [], set(), 0, set()
    for info in infos:
        name = info.orig_filename
        if (not name or "\\" in name or "\x00" in name or name.startswith("/") or
                any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise ValueError("unsafe archive path")
        parts = name.rstrip("/").split("/")
        if any(part in {"", ".", ".."} or ":" in part or part.endswith((" ", ".")) or
               _RESERVED.fullmatch(part) for part in parts):
            raise ValueError("unsafe archive path")
        folded = "/".join(parts).casefold()
        if folded in seen:
            raise ValueError("duplicate or case-conflicting archive entry")
        seen.add(folded)
        roots.add(parts[0])
        mode = (info.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not info.is_dir()):
            raise ValueError("symbolic links and special files are not accepted")
        if info.flag_bits & 1:
            raise ValueError("encrypted source archive entries are not accepted")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError("unsupported source compression")
        if info.file_size < 0 or info.compress_size < 0:
            raise ValueError("invalid archive size")
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES:
            raise ValueError("source archive exceeds expanded size limit")
        if info.file_size > 1024 * 1024 and info.file_size > max(1, info.compress_size) * MAX_RATIO:
            raise ValueError("source archive compression ratio exceeds limit")
        if info.is_dir() and info.file_size:
            raise ValueError("directory entries must not contain data")
        result.append((info, PurePosixPath(*parts)))
    if len(roots) != 1 or any(len(path.parts) < 2 and not info.is_dir() for info, path in result):
        raise ValueError("source archive requires one enclosing repository directory")
    if remote and roots != {repository.split("/")[1] + "-" + commit}:
        raise ValueError("source archive root does not match the pinned repository and commit")
    # A regular file cannot also be an ancestor of another archive member.
    regular = {str(path).casefold() for info, path in result if not info.is_dir()}
    for _, path in result:
        if any(str(parent).casefold() in regular for parent in path.parents if str(parent) != "."):
            raise ValueError("archive file conflicts with a directory")
    return result


class SnapshotStore:
    def __init__(self, root, state_dir):
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.directory = self.state_dir / "snapshots"
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.resolve().is_relative_to(self.state_dir):
            raise ValueError("snapshot directory must be inside the state directory")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.state_dir / "society_snapshots.sqlite3"), timeout=15,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        with self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS source_snapshots (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def inspect(self, body):
        if not isinstance(body, dict) or set(body) - {"repository", "commit", "license_spdx", "archive_path"}:
            raise ValueError("expected repository, commit, license_spdx and optional archive_path only")
        repository, commit, declared = body.get("repository"), body.get("commit"), body.get("license_spdx")
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository) or repository.split("/")[1] in {".", ".."}:
            raise ValueError("repository must be a public GitHub owner/repository name")
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise ValueError("commit must be an exact 40-character hexadecimal commit hash")
        commit = commit.lower()
        if not isinstance(declared, str) or declared not in ALLOWED_LICENSES:
            raise ValueError("license_spdx must be one of MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC")
        local = "archive_path" in body
        url = f"https://codeload.github.com/{repository}/zip/{commit}"
        if local:
            value = body["archive_path"]
            if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
                raise ValueError("archive_path must identify a local ZIP file")
            source = Path(value).expanduser()
            if not source.is_absolute():
                source = self.root / source
            if source.is_symlink() or not source.is_file() or source.suffix.lower() != ".zip":
                raise ValueError("archive_path must identify a regular local ZIP file, not a link")
            if source.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ValueError("source archive exceeds compressed size limit")
            with source.open("rb") as handle:
                raw = handle.read(MAX_ARCHIVE_BYTES + 1)
        else:
            raw = _download(url)
        if not raw or len(raw) > MAX_ARCHIVE_BYTES:
            raise ValueError("source archive is empty or exceeds compressed size limit")
        files, licenses, notices, main_licenses = [], [], [], []
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zipped:
                members = _validated_members(zipped, repository, commit, remote=not local)
                for info, path in members:
                    if info.is_dir():
                        continue
                    # ZIP validates CRC and the header's actual expanded length here.
                    data = zipped.read(info)
                    if len(data) != info.file_size:
                        raise ValueError("expanded source size does not match archive metadata")
                    files.append((path, data))
                    is_license = bool(_LICENSE_NAME.fullmatch(path.name)) or "licenses" in [p.lower() for p in path.parts[:-1]]
                    is_notice = bool(_NOTICE_NAME.fullmatch(path.name))
                    if is_license or is_notice:
                        if len(data) > MAX_LICENSE_BYTES:
                            raise ValueError("license or notice exceeds inspection limit")
                        try:
                            terms = data.decode("utf-8-sig")
                        except UnicodeDecodeError:
                            raise ValueError("license and notice text must be readable UTF-8") from None
                        if _PROHIBITED_TERMS.search(" ".join(terms.split())):
                            raise ValueError("custom or restricted license terms require a separate review")
                        entry = {"path": str(path), "sha256": _sha(data)}
                        if is_license:
                            entry["detected_spdx"] = _license_kind(terms)
                            licenses.append(entry)
                            if len(path.parts) == 2:
                                main_licenses.append(entry)
                        if is_notice:
                            notices.append(entry)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
            raise ValueError("invalid or unsupported ZIP source archive") from exc
        matching = [entry for entry in main_licenses if entry["detected_spdx"] == declared]
        if not matching:
            raise ValueError("repository root license text does not match the declared SPDX license")
        if any(entry["detected_spdx"] != declared for entry in main_licenses):
            raise ValueError("multiple root licenses require a separate licensing review")
        snapshot_id = "snapshot_" + secrets.token_hex(12)
        target = self.directory / snapshot_id
        # A freshly generated private directory prevents pre-existing archive-path links.
        target.mkdir(mode=0o700, exist_ok=False)
        try:
            (target / "source.zip").write_bytes(raw)
            extracted = target / "files"
            extracted.mkdir(mode=0o700)
            for relative, data in files:
                destination = extracted.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as handle:
                    handle.write(data)
                destination.chmod(0o600)  # Do not restore executable bits from foreign code.
            record = {
                "id": snapshot_id, "repository": repository, "commit": commit,
                "license_spdx": declared, "status": "source_inspected_not_executed",
                "created_at": _now(), "archive_sha256": _sha(raw), "archive_bytes": len(raw),
                "expanded_bytes": sum(len(data) for _, data in files), "file_count": len(files),
                "license_path": matching[0]["path"], "license_sha256": matching[0]["sha256"],
                "licenses": licenses, "notices": notices,
                "storage_path": str(target.relative_to(self.state_dir)),
                "manifest": [{"path": str(path), "sha256": _sha(data), "bytes": len(data)} for path, data in files],
                "provenance": {"mode": "user_supplied_archive_unverified_origin" if local else "public_github_pinned_archive",
                               "source_url": None if local else url, "repository_identity_verified": False,
                               "commit_provenance": "unverified_user_declaration" if local else "pinned_codeload_https_response"},
                "verification": {"archive_checked": True, "license_text_screened": True,
                                 "license_is_legal_guarantee": False, "source_executed": False,
                                 "agent_installed": False, "dependency_licenses_audited": False,
                                 "git_objects_independently_verified": False},
                "limitations": [
                    "License family screening is not a complete legal audit; inspect bundled and fetched dependency terms separately.",
                    "Repository ownership text is not verification of an organization or maintainer identity.",
                    "Pinned HTTPS retrieval and archive hash do not independently validate Git objects or author signatures.",
                    "Source is preserved without installation, imports, execution, account access or model weights.",
                    "A data client grants no authorization to controlled genomic datasets.",
                ],
            }
            (target / "inspection.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            with self._lock, self._conn:
                self._conn.execute("INSERT INTO source_snapshots VALUES (?,?)", (snapshot_id, json.dumps(record, ensure_ascii=False)))
            return record
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def items(self):
        with self._lock:
            return [json.loads(row["body"]) for row in self._conn.execute("SELECT body FROM source_snapshots ORDER BY rowid DESC").fetchall()]

    def close(self):
        with self._lock:
            self._conn.close()
