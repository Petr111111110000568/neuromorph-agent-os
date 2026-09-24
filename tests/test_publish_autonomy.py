import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "publish_autonomy.py"
spec = importlib.util.spec_from_file_location("publish_autonomy", MODULE)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def proposal():
    return {"schema_version": 1, "status": "validated", "cycle_id": "a" * 64,
            "title": "A bounded research proposal", "summary": "Requires review.",
            "files": [{"path": "docs/contributions/proposal.md", "content": "# Proposal\nBounded work.\n"}],
            "tasks": [], "review_required": True, "code_executed": False}


class FakeGitHub:
    repository = "owner/research"

    def __init__(self, candidate=None):
        self.candidate = candidate or proposal()
        self.calls = []
        self.pull = None
        self.branch = False
        self.race = False
        self.pull_race = False
        self.deny = None
        self.bad_branch = False
        self.open_count = 0
        self.tree = []
        self.truncated = False

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if self.deny == path:
            raise publisher.GitHubError(403)
        if method == "GET" and path.startswith("/pulls?state=all"):
            return [self.pull] if self.pull else []
        if path == "/pulls?state=open&per_page=100":
            return [{"head": {"ref": publisher.BRANCH_PREFIX + str(i)}} for i in range(self.open_count)]
        if path == "/":
            return {"default_branch": "main"}
        if path == "/git/ref/heads/main":
            return {"object": {"sha": "a" * 40}}
        if path.startswith("/git/ref/heads/"):
            if not self.branch:
                raise publisher.GitHubError(404)
            return {"object": {"sha": "c" * 40}}
        if path == "/git/commits/" + "a" * 40:
            return {"tree": {"sha": "b" * 40}}
        if path == "/git/trees/" + "b" * 40 + "?recursive=1":
            return {"tree": self.tree, "truncated": self.truncated}
        if method == "POST" and path == "/git/trees":
            return {"sha": "c" * 40}
        if method == "POST" and path == "/git/commits":
            return {"sha": "d" * 40}
        if method == "POST" and path == "/git/refs":
            self.branch = True
            if self.race:
                raise publisher.GitHubError(422)
            return {"ref": payload["ref"]}
        if path.startswith("/compare/"):
            files = [{"filename": f["path"], "sha": publisher.blob_sha(f["content"]), "status": "added"} for f in self.candidate["files"]]
            if self.bad_branch:
                files.append({"filename": ".github/workflows/evil.yml", "sha": "e" * 40, "status": "added"})
            return {"files": files}
        if method == "POST" and path == "/pulls":
            self.pull = {**payload, "number": 7, "state": "open"}
            if self.pull_race:
                raise publisher.GitHubError(422)
            return self.pull
        raise AssertionError((method, path, payload))


class PublisherTests(unittest.TestCase):
    def test_draft_created_and_repeat_deduplicated_without_write(self):
        api = FakeGitHub()
        first = publisher.publish(proposal(), api)
        self.assertEqual("pull_request_created", first["status"])
        self.assertTrue(api.pull["draft"])
        self.assertFalse(first["candidate_executed"])
        self.assertFalse(first["auto_merge"])
        self.assertEqual("main", api.pull["base"])
        api.calls.clear()
        second = publisher.publish(proposal(), api)
        self.assertEqual("already_proposed", second["status"])
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))
        # A different run/date with identical file content is the same proposal.
        changed = proposal()
        changed["cycle_id"] = "b" * 64
        self.assertEqual(first["proposal_sha256"], publisher.publish(changed, api)["proposal_sha256"])

    def test_existing_partial_branch_and_ref_creation_race_recover(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                api = FakeGitHub()
                api.branch = partial
                api.race = not partial
                result = publisher.publish(proposal(), api)
                self.assertEqual("pull_request_created", result["status"])
                self.assertFalse(any(method in ("PATCH", "DELETE", "PUT") for method, _, _ in api.calls))

    def test_pr_race_deduplicates(self):
        api = FakeGitHub()
        api.pull_race = True
        self.assertEqual("already_proposed", publisher.publish(proposal(), api)["status"])

    def test_conflicting_branch_never_proposed(self):
        api = FakeGitHub()
        api.branch, api.bad_branch = True, True
        result = publisher.publish(proposal(), api)
        self.assertEqual("artifact_only", result["status"])
        self.assertFalse(any(method == "POST" for method, _, _ in api.calls))

    def test_disabled_pr_api_preserves_artifact_and_existing_branch(self):
        api = FakeGitHub()
        api.deny = "/pulls"
        result = publisher.publish(proposal(), api)
        self.assertEqual("artifact_only", result["status"])
        self.assertEqual("github_auth_or_policy", result["reason"])
        self.assertEqual(403, result["http_status"])
        self.assertTrue(api.branch)

    def test_no_credentials_no_proposal_and_open_limit(self):
        self.assertEqual("missing_github_credentials", publisher.publish(proposal())["reason"])
        self.assertEqual("skipped", publisher.publish({"schema_version": 1, "status": "none", "files": []})["status"])
        api = FakeGitHub()
        api.open_count = 3
        self.assertEqual("open_proposal_limit", publisher.publish(proposal(), api)["reason"])
        self.assertFalse(any(method == "POST" for method, _, _ in api.calls))

    def test_allowed_paths_and_bounds_are_checked_again(self):
        bad_paths = [".github/workflows/ci.yml", "workbench/autonomy.py", "docs/contributions/../hack.py",
                     "docs/contributions//x.md", "/docs/contributions/x.md", "tests/proposals/__init__.py",
                     "docs/contributions/.hidden.md", "docs/contributions/a.sh", "docs\\contributions\\x.md",
                     "docs/contributions/AGENTS.md", "docs/contributions/agents/rules.md", "docs/contributions/config/key.json",
                     "docs/contributions/CLAUDE.md", "docs/contributions/gEmInI.md", "docs/contributions/CODEX.md"]
        for path in bad_paths:
            with self.subTest(path=path):
                data = proposal()
                data["files"][0]["path"] = path
                with self.assertRaises(ValueError):
                    publisher.publish(data, FakeGitHub())
        for mutation in (lambda p: p.update(code_executed=True),
                         lambda p: p.update(review_required=False),
                         lambda p: p["files"][0].update(content="x" * 12001),
                         lambda p: p["files"].append(copy.deepcopy(p["files"][0]))):
            data = proposal()
            mutation(data)
            with self.assertRaises(ValueError):
                publisher.validate_proposal(data)

    def test_base_tree_cannot_be_overwritten_or_traversed_through_symlink(self):
        for entry in ({"path": "docs/contributions/proposal.md", "mode": "100644", "type": "blob"},
                      {"path": "docs/contributions", "mode": "120000", "type": "blob"},
                      {"path": "docs", "mode": "160000", "type": "commit"}):
            with self.subTest(entry=entry):
                api = FakeGitHub()
                api.tree = [entry]
                self.assertEqual("artifact_only", publisher.publish(proposal(), api)["status"])
                self.assertFalse(any(method == "POST" for method, _, _ in api.calls))
        api = FakeGitHub()
        api.truncated = True
        self.assertEqual("artifact_only", publisher.publish(proposal(), api)["status"])
        self.assertFalse(any(method == "POST" for method, _, _ in api.calls))

    def test_generated_code_remains_data_and_no_files_are_materialized(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "executed"
            data = proposal()
            data["files"] = [{"path": "workbench/experiments/proposal.py", "content": f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\n"}]
            api = FakeGitHub(data)
            self.assertEqual("pull_request_created", publisher.publish(data, api)["status"])
            self.assertFalse(sentinel.exists())
            tree_payload = next(payload for method, path, payload in api.calls if path == "/git/trees")
            self.assertEqual("100644", tree_payload["tree"][0]["mode"])

    def test_cli_artifact_fallback_without_token(self):
        with tempfile.TemporaryDirectory() as directory:
            inp, out = Path(directory) / "proposal.json", Path(directory) / "receipt.json"
            inp.write_text(json.dumps(proposal()), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(0, publisher.main(["--proposal", str(inp), "--output", str(out)]))
            self.assertEqual("artifact_only", json.loads(out.read_text())["status"])

    def test_transport_blocks_redirect_and_does_not_reflect_error_body(self):
        with self.assertRaises(publisher.GitHubError):
            publisher.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid/")
        api = publisher.GitHub("owner/research", "top-secret")
        from urllib.error import HTTPError
        with patch.object(api.opener, "open", side_effect=HTTPError("https://api.github.com/", 401, "top-secret", {}, None)):
            with self.assertRaises(publisher.GitHubError) as caught:
                api.request("GET", "/")
        self.assertNotIn("top-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
