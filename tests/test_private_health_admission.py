"""Run the template's actual admission program against disposable Git histories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = Path(os.environ.get("PRIVATE_HEALTH_WORKFLOW", ROOT / "templates/private-openkylin-health.yml"))


class PrivateHealthAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = {
            "PATH": os.defpath, "LANG": "C", "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Fixture", "GIT_COMMITTER_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        self.git("init", "-q", "-b", "main")
        (self.repo / "config.txt").write_text("trusted\n")
        self.before = self.commit()
        self.name = "requests/health-20261020-001.json"
        self.target = self.repo / self.name
        self.target.parent.mkdir()
        self.target.write_text(json.dumps({
            "schema_version": 1, "request_id": "health-20261020-001", "op": "health_snapshot"
        }))
        self.after = self.commit()
        self.event = {
            "ref": "refs/heads/main", "repository": {"private": True},
            "forced": False, "created": False, "deleted": False,
            "before": self.before, "after": self.after,
            # Real Actions events omit added/modified/removed.
            "head_commit": {"id": self.after}, "commits": [{"id": self.after}],
        }
        template = TEMPLATE.read_text()
        self.program = textwrap.dedent(template.split("<<'PY'\n", 1)[1].split("\n          PY", 1)[0])

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env, check=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10
        ).stdout.decode().strip()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def amend(self):
        self.git("add", "-A")
        self.git("commit", "--amend", "--no-edit", "-q")
        self.after = self.git("rev-parse", "HEAD")
        self.event["after"] = self.after
        self.event["head_commit"]["id"] = self.after
        self.event["commits"] = [{"id": self.after}]

    def run_admission(self, expected_ok, env_override=None):
        event_file = self.root / "event.json"
        event_file.write_text(json.dumps(self.event))
        out = self.root / "github-output"
        env = dict(self.env, GITHUB_EVENT_PATH=str(event_file),
                   GITHUB_WORKSPACE=str(self.repo), GITHUB_OUTPUT=str(out),
                   GITHUB_REF="refs/heads/main", GITHUB_SHA=self.after,
                   GITHUB_EVENT_NAME="push")
        env.update(env_override or {})
        result = subprocess.run(
            [sys.executable, "-I", "-"], input=self.program, text=True,
            env=env, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if expected_ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(out.read_text(), "request_id=health-20261020-001\n")
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertFalse(out.exists(), "Refused requests must not produce admission output")
        return result

    def misleading_payload(self):
        self.event["head_commit"].update(added=[self.name], modified=[], removed=[])

    def test_real_actions_event_accepts_one_committed_request(self):
        self.run_admission(True)

    def test_misleading_payload_cannot_hide_modified_file(self):
        (self.repo / "config.txt").write_text("changed\n")
        self.amend()
        self.misleading_payload()
        self.run_admission(False)

    def test_misleading_payload_cannot_hide_deleted_file(self):
        (self.repo / "config.txt").unlink()
        self.amend()
        self.misleading_payload()
        self.run_admission(False)

    def test_two_added_requests_rejected(self):
        (self.repo / "requests/health-second.json").write_text(self.target.read_text())
        self.amend()
        self.run_admission(False)

    def test_multiple_commits_in_push_rejected(self):
        self.git("reset", "--soft", self.before)
        self.target.unlink()
        (self.repo / "config.txt").write_text("earlier change\n")
        self.commit()
        self.target.write_text(json.dumps({
            "schema_version": 1, "request_id": "health-20261020-001", "op": "health_snapshot"}))
        self.after = self.commit()
        self.event["after"] = self.after
        self.misleading_payload()
        self.run_admission(False)

    def test_merge_commit_rejected(self):
        self.git("checkout", "-qb", "side", self.before)
        (self.repo / "side.txt").write_text("side\n")
        self.commit()
        self.git("checkout", "-q", "main")
        self.git("merge", "--no-ff", "-qm", "merge fixture", "side")
        self.after = self.git("rev-parse", "HEAD")
        self.event["after"] = self.after
        self.misleading_payload()
        self.run_admission(False)

    def test_missing_before_history_fails_closed(self):
        self.event["before"] = "a" * 40
        self.run_admission(False)

    def test_wrong_checkout_rejected(self):
        self.git("checkout", "-q", self.before)
        self.run_admission(False)

    def test_wrong_run_sha_rejected(self):
        self.run_admission(False, {"GITHUB_SHA": self.before})

    def test_private_main_push_required(self):
        for key, value in [("forced", True), ("created", True), ("deleted", True),
                           ("ref", "refs/heads/other"), ("repository", {"private": False})]:
            with self.subTest(key=key):
                previous = self.event[key]
                self.event[key] = value
                self.run_admission(False)
                self.event[key] = previous

    def test_missing_force_flag_rejected(self):
        del self.event["forced"]
        self.run_admission(False)

    def test_non_push_event_rejected(self):
        self.run_admission(False, {"GITHUB_EVENT_NAME": "pull_request"})

    def test_invalid_sha_rejected(self):
        for value in ["0" * 40, "-option", "../path", None]:
            with self.subTest(value=value):
                self.event["before"] = value
                self.run_admission(False)

    def test_request_id_must_match_filename(self):
        self.target.write_text('{"schema_version":1,"request_id":"other","op":"health_snapshot"}')
        self.amend()
        self.run_admission(False)

    def test_bool_schema_is_not_integer(self):
        self.target.write_text('{"schema_version":true,"request_id":"health-20261020-001","op":"health_snapshot"}')
        self.amend()
        self.run_admission(False)

    def test_duplicate_json_key_rejected(self):
        self.target.write_text('{"schema_version":0,"schema_version":1,"request_id":"health-20261020-001","op":"health_snapshot"}')
        self.amend()
        self.run_admission(False)

    def test_large_request_rejected(self):
        self.target.write_text(self.target.read_text() + " " * 1024)
        self.amend()
        self.run_admission(False)

    def test_symlink_request_rejected(self):
        self.target.unlink()
        self.target.symlink_to("../config.txt")
        self.amend()
        self.run_admission(False)

    def test_executable_request_rejected(self):
        self.target.chmod(0o755)
        self.amend()
        self.run_admission(False)

    def test_request_path_must_match_allowlist(self):
        self.target.rename(self.target.with_name("bad_name.json"))
        self.amend()
        self.run_admission(False)

    def test_reads_committed_blob_not_dirty_worktree(self):
        self.target.write_text("not the committed request")
        self.run_admission(True)

    def test_depth_two_checkout_is_sufficient(self):
        clone = self.root / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "2", self.repo.as_uri(), str(clone)],
                       env=self.env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.repo = clone
        self.run_admission(True)


if __name__ == "__main__":
    unittest.main()
