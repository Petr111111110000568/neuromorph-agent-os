"""Cloud identity boundary and the actual finite seed/resume composition."""
import contextlib
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("m02_experiment", ROOT / "scripts/m02_experiment.py")
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


class ProducerValidationTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"repository": experiment.REPOSITORY, "workflow_id": experiment.WORKFLOW,
                         "branch": "main", "commit": "a" * 40, "run_id": "123"}
        self.parent = {"id": 123, "repository": {"full_name": experiment.REPOSITORY},
                       "head_repository": {"full_name": experiment.REPOSITORY}, "head_branch": "main",
                       "head_sha": "a" * 40, "path": experiment.WORKFLOW, "workflow_id": 77,
                       "run_attempt": 1, "status": "completed", "conclusion": "success", "event": "push"}
        self.current = dict(self.parent, id=124, status="in_progress", conclusion=None)

    def test_exact_producer(self):
        self.assertEqual(experiment.validate_producer(self.parent, self.current, self.identity), self.identity)

    def test_no_latest_fork_other_workflow_or_failed_run(self):
        mutations = [("id", 125), ("repository", {"full_name": "foreign/repo"}),
                     ("head_repository", {"full_name": "fork/repo"}), ("head_sha", "b" * 40),
                     ("head_branch", "other"), ("path", ".github/workflows/other.yml"),
                     ("workflow_id", 78), ("run_attempt", 2), ("conclusion", "failure"),
                     ("status", "in_progress"), ("event", "pull_request")]
        for key, value in mutations:
            with self.subTest(field=key):
                changed = deepcopy(self.parent)
                changed[key] = value
                with self.assertRaises(ValueError):
                    experiment.validate_producer(changed, self.current, self.identity)

    def test_cannot_resume_self_or_changed_current_code(self):
        with self.assertRaises(ValueError):
            experiment.validate_producer(self.parent, self.parent, self.identity)
        with self.assertRaises(ValueError):
            experiment.validate_producer(self.parent, dict(self.current, head_sha="b" * 40), self.identity)

    def test_two_closed_runs_preserve_seed_and_monotonic_budget(self):
        env = {"GITHUB_REPOSITORY": experiment.REPOSITORY, "GITHUB_SHA": "a" * 40,
               "GITHUB_REF_NAME": "main", "GITHUB_RUN_ID": "123",
               "GITHUB_WORKFLOW_REF": f"{experiment.REPOSITORY}/{experiment.WORKFLOW}@refs/heads/main",
               "PREVIOUS_RUN_ID": "", "EXPECTED_MANIFEST_SHA": ""}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True), \
             patch.object(experiment.subprocess, "check_output", return_value="a" * 40):
            root = Path(directory).resolve()
            seed_output = root / "seed" / "m02"
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture):
                experiment.execute(seed_output)
            sha = next(line.split("=", 1)[1] for line in capture.getvalue().splitlines() if line.startswith("M02_MANIFEST_SHA256="))
            seed = json.loads((seed_output / "seed-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(seed["state"]["budget"]["reserved_attempts"], 4)
            self.assertEqual(seed["state"]["budget"]["actual_attempts"], 1)
            resume_root = root / "resume"
            shutil.copytree(seed_output, resume_root / "incoming")
            (resume_root / "producer.json").write_text(json.dumps({"identity": self.identity, "manifest_sha256": sha}), encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "124", "PREVIOUS_RUN_ID": "123", "EXPECTED_MANIFEST_SHA": sha}), \
                 contextlib.redirect_stdout(io.StringIO()):
                experiment.execute(resume_root / "m02")
            final = json.loads((resume_root / "m02" / "seed-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(final["initial_state"], seed["state"])
            self.assertEqual(final["state"]["budget"]["reserved_attempts"], 6)
            self.assertEqual(final["state"]["budget"]["actual_attempts"], 3)
            self.assertTrue(all(t["status"] == "accepted" for t in final["state"]["tasks"]))
            serialized = json.dumps(final)
            self.assertNotIn("lease_token", serialized)
            self.assertNotIn("lease_token_digest", serialized)
            self.assertNotIn("lease_digest", serialized)
            self.assertNotIn("completion_digest", serialized)
            self.assertEqual(final["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
