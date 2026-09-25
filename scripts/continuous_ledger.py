"""Fixed-path, compare-and-swap storage for the unattended proposal branch.

This trusted helper never imports or executes files from the ledger branch.
The worker owns state semantics and must reserve an attempt before inference.
Only the caller's publication step receives a GitHub token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

BRANCH = "autonomy/continuous"
STATE_PATH = "docs/contributions/continuous-state.json"
REPORT_PATH = "docs/contributions/continuous-latest.md"
CANDIDATE_PATH = "workbench/experiments/continuous_candidate.py"
ALLOWED_PATHS = frozenset((STATE_PATH, REPORT_PATH, CANDIDATE_PATH))
MAX_STATE_BYTES = 64 * 1024
MAX_REPORT_BYTES = 16 * 1024
MAX_CANDIDATE_BYTES = 8 * 1024
MAX_API_BYTES = 2 * 1024 * 1024


class LedgerError(ValueError):
    """Non-reflective failure: never include an API body or credential."""

    def __init__(self, reason, status=None):
        self.reason = reason
        self.status = status
        super().__init__(reason)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise LedgerError("redirect_refused", code)


class GitHub:
    def __init__(self, repository, token):
        if (not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository)
                or repository.split("/")[1] in {".", ".."}):
            raise LedgerError("invalid_repository")
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise LedgerError("missing_github_token")
        self.repository = repository
        self._token = token
        self._opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, payload=None):
        if (method not in {"GET", "POST", "PATCH"} or not isinstance(path, str)
                or not path.startswith("/") or path.startswith("//")
                or any(c in path for c in "\r\n\\#")):
            raise LedgerError("invalid_api_request")
        url = "https://api.github.com/repos/" + self.repository + ('' if path == '/' else path)
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token,
            "Accept": "application/vnd.github+json", "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Meta-Harness-Continuous-Ledger/1",
        })
        try:
            with self._opener.open(request, timeout=30) as response:
                if response.geturl() != url:
                    raise LedgerError("unexpected_api_location")
                raw = response.read(MAX_API_BYTES + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            raise LedgerError("github_api_error", status) from None
        except (URLError, TimeoutError, OSError):
            raise LedgerError("github_transport_error") from None
        if len(raw) > MAX_API_BYTES:
            raise LedgerError("api_response_too_large")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise LedgerError("invalid_api_response") from None


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise LedgerError("invalid_commit_or_blob_sha")
    return value


def _ref(api, branch):
    entry = api.request("GET", "/git/ref/heads/" + quote(branch, safe=""))
    if not isinstance(entry, dict) or not isinstance(entry.get("object"), dict):
        raise LedgerError("invalid_branch_reference")
    if entry["object"].get("type", "commit") != "commit":
        raise LedgerError("branch_is_not_commit")
    return _sha(entry["object"].get("sha"))


def _base(api):
    repository = api.request("GET", "/")
    if not isinstance(repository, dict) or repository.get("private") is not False:
        raise LedgerError("public_repository_required")
    branch = repository.get("default_branch")
    if not isinstance(branch, str) or not branch or len(branch) > 200 or branch == BRANCH:
        raise LedgerError("invalid_default_branch")
    return _ref(api, branch)


def _state_bytes(state):
    if type(state) is not dict:
        raise LedgerError("state_must_be_object")
    stack, count = [(state, 0)], 0
    while stack:
        value, depth = stack.pop()
        count += 1
        if depth > 20 or count > 12000:
            raise LedgerError("state_structure_exceeds_limit")
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise LedgerError("state_keys_must_be_strings")
            stack.extend((item, depth + 1) for pair in value.items() for item in pair)
        elif type(value) is list:
            stack.extend((item, depth + 1) for item in value)
        elif value is not None and type(value) not in (str, int, float, bool):
            raise LedgerError("invalid_state_value")
        elif type(value) is float and not math.isfinite(value):
            raise LedgerError("nonfinite_state_value")
    try:
        raw = (json.dumps(state, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise LedgerError("invalid_state_encoding") from None
    if len(raw) > MAX_STATE_BYTES:
        raise LedgerError("state_too_large")
    return raw


def _decode_state(raw):
    if len(raw) > MAX_STATE_BYTES:
        raise LedgerError("state_too_large")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise LedgerError("duplicate_state_key")
            result[key] = value
        return result

    try:
        state = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise LedgerError("invalid_stored_state") from None
    _state_bytes(state)
    return state


def fetch_state(api):
    """Return (state, ledger_head, default_head); only a missing branch is new.

    State is read at an immutable commit, never at a moving branch name.
    The caller must validate its own schema before using or changing this state.
    """
    base_sha = _base(api)
    try:
        head_sha = _ref(api, BRANCH)
    except LedgerError as error:
        if error.status == 404:
            return None, None, base_sha
        raise
    try:
        entry = api.request("GET", "/contents/" + STATE_PATH + "?ref=" + head_sha)
    except LedgerError as error:
        if error.status == 404:
            raise LedgerError("existing_ledger_missing_state") from None
        raise
    if (not isinstance(entry, dict) or entry.get("type") != "file"
            or entry.get("encoding") != "base64" or type(entry.get("size")) is not int
            or not 0 < entry["size"] <= MAX_STATE_BYTES or not isinstance(entry.get("content"), str)):
        raise LedgerError("invalid_state_blob")
    encoded = entry["content"]
    if len(encoded) > MAX_STATE_BYTES * 2:
        raise LedgerError("state_blob_too_large")
    try:
        raw = base64.b64decode(encoded.replace("\n", ""), validate=True)
    except ValueError:
        raise LedgerError("invalid_state_base64") from None
    digest = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if len(raw) != entry["size"] or digest != _sha(entry.get("sha")):
        raise LedgerError("state_blob_integrity_mismatch")
    return _decode_state(raw), head_sha, base_sha


def _text(value, maximum, reason):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise LedgerError(reason)
    try:
        if len(value.encode("utf-8")) > maximum:
            raise LedgerError(reason)
    except UnicodeError:
        raise LedgerError(reason) from None
    return value


def _tree(api, parent, initial):
    commit = api.request("GET", "/git/commits/" + parent)
    if not isinstance(commit, dict) or not isinstance(commit.get("tree"), dict):
        raise LedgerError("invalid_parent_commit")
    tree_sha = _sha(commit["tree"].get("sha"))
    listing = api.request("GET", "/git/trees/" + tree_sha + "?recursive=1")
    if (not isinstance(listing, dict) or listing.get("truncated") is not False
            or not isinstance(listing.get("tree"), list)):
        raise LedgerError("incomplete_parent_tree")
    entries = {}
    for item in listing["tree"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or item["path"] in entries:
            raise LedgerError("invalid_parent_tree")
        entries[item["path"]] = item
    if not initial and STATE_PATH not in entries:
        raise LedgerError("existing_ledger_missing_state")
    for path in ALLOWED_PATHS:
        entry = entries.get(path)
        if entry and (initial or entry.get("type") != "blob" or entry.get("mode") != "100644"):
            raise LedgerError("protected_existing_ledger_path")
        for parent_path in PurePosixPath(path).parents:
            entry = entries.get(str(parent_path))
            if entry and (entry.get("type") != "tree" or entry.get("mode") != "040000"):
                raise LedgerError("ledger_parent_is_not_directory")
    return tree_sha


def commit_state(api, expected_head, state, report, candidate=None):
    """Publish fixed bounded files and return the new immutable ledger HEAD.

    expected_head=None is bootstrap only; an existing ledger can never be reset.
    On a concurrent update the non-force Git ref update fails; never auto-retry.
    """
    content = _state_bytes(state).decode("utf-8")
    files = [{"path": STATE_PATH, "mode": "100644", "type": "blob", "content": content},
             {"path": REPORT_PATH, "mode": "100644", "type": "blob",
              "content": _text(report, MAX_REPORT_BYTES, "invalid_report")}]
    if candidate is not None:
        files.append({"path": CANDIDATE_PATH, "mode": "100644", "type": "blob",
                      "content": _text(candidate, MAX_CANDIDATE_BYTES, "invalid_candidate")})
    if expected_head is not None:
        _sha(expected_head)
    base_sha = _base(api)
    try:
        current = _ref(api, BRANCH)
    except LedgerError as error:
        if error.status != 404:
            raise
        current = None
    if current != expected_head:
        raise LedgerError("concurrent_ledger_update")
    parent = current if current is not None else base_sha
    tree_sha = _tree(api, parent, current is None)
    result = api.request("POST", "/git/trees", {"base_tree": tree_sha, "tree": files})
    new_tree = _sha(result.get("sha") if isinstance(result, dict) else None)
    result = api.request("POST", "/git/commits", {
        "message": "Record bounded autonomous research state", "tree": new_tree, "parents": [parent]})
    new_head = _sha(result.get("sha") if isinstance(result, dict) else None)
    try:
        if current is None:
            api.request("POST", "/git/refs", {"ref": "refs/heads/" + BRANCH, "sha": new_head})
        else:
            api.request("PATCH", "/git/refs/heads/" + quote(BRANCH, safe=""),
                        {"sha": new_head, "force": False})
    except LedgerError as error:
        if error.status in (409, 422):
            raise LedgerError("concurrent_ledger_update", error.status) from None
        raise
    if _ref(api, BRANCH) != new_head:
        raise LedgerError("ledger_write_receipt_mismatch")
    return new_head
