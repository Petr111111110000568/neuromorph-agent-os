#!/usr/bin/env python3
"""Publish bounded proposal data through GitHub's Git API; never execute it.

This script is deliberately independent of workbench and generated files. It is
run from the trusted default-branch checkout, with the token scoped to this step.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import urllib.error
import urllib.parse
import urllib.request

MAX_INPUT_BYTES = 256 * 1024
MAX_FILES = 5
MAX_FILE_BYTES = 12_000
MAX_TOTAL_BYTES = 48_000
MAX_OPEN_PROPOSALS = 3
PREFIXES = ("docs/contributions/", "tests/proposals/", "workbench/experiments/")
FORBIDDEN_SEGMENTS = {".git", ".github", ".env", "config", "scripts", "agents.md", "agents", "claude.md", "gemini.md", "codex.md"}
BRANCH_PREFIX = "autonomy/proposal-"


class GitHubError(Exception):
    """A deliberately non-reflective error: response bodies may contain secrets."""
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"GitHub API status {status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GitHubError(code)


class GitHub:
    def __init__(self, repository: str, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository):
            raise ValueError("invalid_repository")
        if repository.split("/")[1] in (".", ".."):
            raise ValueError("invalid_repository")
        self.repository = repository
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method: str, path: str, payload=None):
        if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\r\n\\#"):
            raise ValueError("invalid_api_path")
        url = "https://api.github.com/repos/" + self.repository + path
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json", "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Meta-Harness-Autonomy/0.9",
        })
        try:
            with self.opener.open(req, timeout=30) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as error:
            raise GitHubError(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GitHubError(0) from None
        if len(raw) > 2_000_000:
            raise ValueError("api_response_too_large")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid_api_response") from None


def validate_proposal(proposal: dict) -> dict:
    """Apply our own checks even when the unprivileged generator says valid."""
    if not isinstance(proposal, dict) or type(proposal.get("schema_version")) is not int or proposal["schema_version"] != 1:
        raise ValueError("invalid_schema")
    if proposal.get("status") == "none" and proposal.get("files") == []:
        return {"status": "none", "files": []}
    if proposal.get("status") != "validated" or proposal.get("review_required") is not True or proposal.get("code_executed") is not False:
        raise ValueError("invalid_proposal_state")
    if not isinstance(proposal.get("cycle_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", proposal["cycle_id"]):
        raise ValueError("invalid_cycle_id")
    for key, limit in (("title", 160), ("summary", 6000)):
        value = proposal.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
            raise ValueError("invalid_" + key)
    files = proposal.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("invalid_file_count")
    checked, seen, total = [], set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise ValueError("invalid_file_record")
        path, content = item["path"], item["content"]
        if not isinstance(path, str) or len(path) > 200 or not re.fullmatch(r"[A-Za-z0-9_./-]+", path):
            raise ValueError("invalid_path")
        pure = PurePosixPath(path)
        if str(pure) != path or pure.is_absolute() or any(p.startswith(".") or p.startswith("__") or p.lower() in FORBIDDEN_SEGMENTS for p in pure.parts):
            raise ValueError("invalid_path")
        if not path.startswith(PREFIXES) or pure.suffix not in (".md", ".txt", ".json", ".py") or path in seen:
            raise ValueError("path_not_allowed")
        if not isinstance(content, str) or not content or "\0" in content:
            raise ValueError("invalid_content")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("invalid_content") from None
        if size > MAX_FILE_BYTES:
            raise ValueError("file_too_large")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("proposal_too_large")
        seen.add(path)
        checked.append({"path": path, "content": content})
    checked.sort(key=lambda item: item["path"])
    return {"status": "validated", "cycle_id": proposal["cycle_id"],
            "title": proposal["title"], "summary": proposal["summary"], "files": checked}


def fingerprint(proposal: dict) -> str:
    # Deduplicate the proposed changes, regardless of a new date or provider.
    raw = json.dumps(proposal["files"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def blob_sha(content: str) -> str:
    data = content.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def sha(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("invalid_api_sha")
    return value


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _pulls(api, branch: str):
    query = urllib.parse.urlencode({"state": "all", "head": api.repository.split("/")[0] + ":" + branch, "per_page": 100})
    result = api.request("GET", "/pulls?" + query)
    if not isinstance(result, list):
        raise ValueError("invalid_api_pulls")
    return result


def _verify_branch(api, base: str, branch: str, files: list[dict]) -> None:
    # A branch surviving a partial failure/race must contain exactly the proposal.
    result = api.request("GET", "/compare/" + _quote(base) + "..." + _quote(branch))
    changes = result.get("files", [])
    expected = {item["path"]: blob_sha(item["content"]) for item in files}
    if not changes or len(changes) != len(expected):
        raise ValueError("branch_conflict")
    seen = set()
    for item in changes:
        if item.get("status") != "added" or expected.get(item.get("filename")) != item.get("sha") or item.get("filename") in seen:
            raise ValueError("branch_conflict")
        seen.add(item["filename"])


def _verify_new_paths(api, tree_sha: str, files: list[dict]) -> None:
    """Verify against the pinned remote base, not the generator's checkout."""
    result = api.request("GET", "/git/trees/" + tree_sha + "?recursive=1")
    if result.get("truncated") is not False or not isinstance(result.get("tree"), list):
        raise ValueError("incomplete_base_tree")
    entries = {entry["path"]: entry for entry in result["tree"]}
    for item in files:
        pure = PurePosixPath(item["path"])
        if item["path"] in entries:
            raise ValueError("existing_file_not_allowed")
        for parent in pure.parents:
            entry = entries.get(str(parent))
            if entry and (entry.get("type") != "tree" or entry.get("mode") != "040000"):
                raise ValueError("parent_is_not_directory")


def publish(proposal: dict, api=None) -> dict:
    checked = validate_proposal(proposal)
    if checked["status"] == "none":
        return {"status": "skipped", "reason": "no_validated_proposal", "artifact_available": True}
    if api is None:
        return {"status": "artifact_only", "reason": "missing_github_credentials", "artifact_available": True}
    digest = fingerprint(checked)
    branch = BRANCH_PREFIX + digest[:24]
    marker = "<!-- meta-harness-proposal:" + digest + " -->"
    result = {"artifact_available": True, "proposal_sha256": digest, "branch": branch,
              "candidate_executed": False, "auto_merge": False}
    try:
        existing = _pulls(api, branch)
        for pull in existing:
            if marker in (pull.get("body") or ""):
                return {**result, "status": "already_proposed", "number": pull["number"], "state": pull.get("state")}
        if existing:
            raise ValueError("branch_conflict")
        opened = api.request("GET", "/pulls?state=open&per_page=100")
        if not isinstance(opened, list):
            raise ValueError("invalid_api_pulls")
        # A full page is treated conservatively rather than missing older proposals.
        count = sum(str(p.get("head", {}).get("ref", "")).startswith(BRANCH_PREFIX) for p in opened)
        if len(opened) >= 100 or count >= MAX_OPEN_PROPOSALS:
            return {**result, "status": "artifact_only", "reason": "open_proposal_limit"}
        repository = api.request("GET", "/")
        base = repository.get("default_branch")
        if not isinstance(base, str) or not base or len(base) > 200:
            raise ValueError("invalid_default_branch")
        base_commit = sha(api.request("GET", "/git/ref/heads/" + _quote(base))["object"]["sha"])
        base_tree = sha(api.request("GET", "/git/commits/" + base_commit)["tree"]["sha"])
        _verify_new_paths(api, base_tree, checked["files"])
        branch_ref = "/git/ref/heads/" + _quote(branch)
        branch_exists = True
        try:
            api.request("GET", branch_ref)
        except GitHubError as error:
            if error.status != 404:
                raise
            branch_exists = False
        if not branch_exists:
            tree = [{"path": item["path"], "mode": "100644", "type": "blob", "content": item["content"]} for item in checked["files"]]
            tree_sha = sha(api.request("POST", "/git/trees", {"base_tree": base_tree, "tree": tree})["sha"])
            if tree_sha == base_tree:
                return {**result, "status": "skipped", "reason": "no_changes"}
            commit = sha(api.request("POST", "/git/commits", {
                "message": "Research proposal " + digest[:24] + "\n\n" + marker,
                "tree": tree_sha, "parents": [base_commit],
            })["sha"])
            try:
                api.request("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": commit})
            except GitHubError as error:
                if error.status != 422:
                    raise
                # Another run won the create race. Never overwrite its branch.
        _verify_branch(api, base_commit, branch, checked["files"])
        body = (marker + "\n\n## Предложение автоматического цикла\n\n" + checked["summary"]
                + "\n\nИсточник: цикл `" + checked["cycle_id"] + "`."
                + "\n\nКод предложения не запускался. Научные утверждения не подтверждены автоматически. "
                + "Требуются проверка источников, ревью и отдельные испытания перед слиянием. "
                + "Изменения ядра, workflows и учётных данных этим каналом не допускаются. "
                + "CI от GITHUB_TOKEN может ожидать стандартного подтверждения GitHub. Автослияние выключено.")
        try:
            pull = api.request("POST", "/pulls", {"title": "[AI proposal] " + checked["title"],
                    "body": body, "head": branch, "base": base, "draft": True, "maintainer_can_modify": True})
        except GitHubError as error:
            if error.status == 422:
                matches = _pulls(api, branch)
                for pull in matches:
                    if marker in (pull.get("body") or ""):
                        return {**result, "status": "already_proposed", "number": pull["number"], "state": pull.get("state")}
            raise
        return {**result, "status": "pull_request_created", "number": pull["number"], "draft": True}
    except GitHubError as error:
        return {**result, "status": "artifact_only", "reason": "github_auth_or_policy" if error.status in (401, 403) else "github_api_error", "http_status": error.status}
    except (ValueError, KeyError, TypeError):
        return {**result, "status": "artifact_only", "reason": "remote_state_conflict_or_invalid_response"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", default="runtime/autonomy/proposal.json")
    parser.add_argument("--output", default="runtime/autonomy/publish.json")
    args = parser.parse_args(argv)
    try:
        path = Path(args.proposal)
        if not path.is_file():
            result = {"status": "skipped", "reason": "proposal_absent", "artifact_available": True}
        else:
            if path.stat().st_size > MAX_INPUT_BYTES:
                raise ValueError("proposal_too_large")
            proposal = json.loads(path.read_text(encoding="utf-8"))
            token, repository = os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
            api = GitHub(repository, token) if token and repository else None
            result = publish(proposal, api)
    except (ValueError, OSError, UnicodeError, RecursionError):
        result = {"status": "artifact_only", "reason": "invalid_proposal_or_environment", "artifact_available": True}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
