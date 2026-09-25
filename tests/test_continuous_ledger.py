import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "continuous_ledger.py"
SPEC = importlib.util.spec_from_file_location("continuous_ledger_tested", MODULE)
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)

BASE, HEAD, TREE, NEW_TREE, NEW_HEAD = (letter * 40 for letter in "abcde")


class FakeAPI:
    def __init__(self, head=None, state=None):
        self.head = head
        self.state = {"schema_version": 1, "attempts": 1} if state is None else state
        self.calls = []
        self.entries = ([{"path": ledger.STATE_PATH, "type": "blob", "mode": "100644"}]
                        if head is not None else [])
        self.missing_state = False
        self.raw = None
        self.bad_hash = False
        self.race = False
        self.private = False
        self.truncated = False

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET" and path == "/":
            return {"private": self.private, "default_branch": "main"}
        if method == "GET" and path == "/git/ref/heads/main":
            return {"object": {"type": "commit", "sha": BASE}}
        if method == "GET" and path == "/git/ref/heads/autonomy%2Fcontinuous":
            if self.head is None:
                raise ledger.LedgerError("github_api_error", 404)
            return {"object": {"type": "commit", "sha": self.head}}
        if method == "GET" and path == "/contents/" + ledger.STATE_PATH + "?ref=" + HEAD:
            if self.missing_state:
                raise ledger.LedgerError("github_api_error", 404)
            raw = self.raw if self.raw is not None else json.dumps(self.state).encode()
            digest = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            return {"type": "file", "encoding": "base64", "size": len(raw),
                    "content": base64.b64encode(raw).decode(), "sha": BASE if self.bad_hash else digest}
        if method == "GET" and path in ("/git/commits/" + HEAD, "/git/commits/" + BASE):
            return {"tree": {"sha": TREE}}
        if method == "GET" and path == "/git/trees/" + TREE + "?recursive=1":
            return {"tree": self.entries, "truncated": self.truncated}
        if method == "POST" and path == "/git/trees":
            return {"sha": NEW_TREE}
        if method == "POST" and path == "/git/commits":
            return {"sha": NEW_HEAD}
        if (method, path) in (("POST", "/git/refs"), ("PATCH", "/git/refs/heads/autonomy%2Fcontinuous")):
            if self.race:
                raise ledger.LedgerError("github_api_error", 422)
            self.head = payload["sha"]
            return {"object": {"sha": self.head}}
        raise AssertionError((method, path, payload))


class ContinuousLedgerTests(unittest.TestCase):
    def test_first_bootstrap_uses_main_and_fixed_paths(self):
        api = FakeAPI()
        self.assertEqual(ledger.fetch_state(api), (None, None, BASE))
        result = ledger.commit_state(api, None, {"attempts": 1}, "Reserved before inference.\n", "value = 1\n")
        self.assertEqual(result, NEW_HEAD)
        tree = next(payload for method, path, payload in api.calls if (method, path) == ("POST", "/git/trees"))
        self.assertEqual({entry["path"] for entry in tree["tree"]}, ledger.ALLOWED_PATHS)
        commit = next(payload for method, path, payload in api.calls if (method, path) == ("POST", "/git/commits"))
        self.assertEqual(commit["parents"], [BASE])
        ref = next(payload for method, path, payload in api.calls if (method, path) == ("POST", "/git/refs"))
        self.assertEqual(ref["ref"], "refs/heads/autonomy/continuous")
        self.assertFalse(any(path == "/pulls" for _, path, _ in api.calls))

    def test_existing_state_is_read_at_pinned_commit(self):
        api = FakeAPI(HEAD)
        self.assertEqual(ledger.fetch_state(api), (api.state, HEAD, BASE))
        self.assertTrue(any(path.endswith("?ref=" + HEAD) for _, path, _ in api.calls))
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_missing_state_cannot_reset_existing_branch(self):
        api = FakeAPI(HEAD)
        api.missing_state = True
        with self.assertRaisesRegex(ledger.LedgerError, "existing_ledger_missing_state"):
            ledger.fetch_state(api)
        with self.assertRaisesRegex(ledger.LedgerError, "concurrent_ledger_update"):
            ledger.commit_state(api, None, {}, "Reset denied.")
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_stale_expected_head_refused_before_writes(self):
        api = FakeAPI(NEW_HEAD)
        with self.assertRaisesRegex(ledger.LedgerError, "concurrent_ledger_update"):
            ledger.commit_state(api, HEAD, {}, "Concurrent update.")
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_compare_and_swap_uses_exact_parent_and_nonforce(self):
        api = FakeAPI(HEAD)
        self.assertEqual(ledger.commit_state(api, HEAD, {}, "New observation."), NEW_HEAD)
        commit = next(payload for method, path, payload in api.calls if (method, path) == ("POST", "/git/commits"))
        self.assertEqual(commit["parents"], [HEAD])
        update = next(payload for method, _, payload in api.calls if method == "PATCH")
        self.assertEqual(update, {"sha": NEW_HEAD, "force": False})

    def test_racing_reference_update_is_never_retried(self):
        for head in (None, HEAD):
            with self.subTest(head=head):
                api = FakeAPI(head)
                api.race = True
                with self.assertRaisesRegex(ledger.LedgerError, "concurrent_ledger_update"):
                    ledger.commit_state(api, head, {}, "Race.")
                writes = [path for method, path, _ in api.calls if method == "PATCH" or (method, path) == ("POST", "/git/refs")]
                self.assertEqual(len(writes), 1)

    def test_symlink_parent_and_initial_existing_files_are_refused(self):
        cases = [
            (HEAD, {"path": "docs", "type": "blob", "mode": "120000"}),
            (HEAD, {"path": ledger.STATE_PATH, "type": "blob", "mode": "120000"}),
            (None, {"path": ledger.CANDIDATE_PATH, "type": "blob", "mode": "100644"}),
        ]
        for head, entry in cases:
            with self.subTest(entry=entry):
                api = FakeAPI(head)
                api.entries = [item for item in api.entries if item["path"] != entry["path"]] + [entry]
                with self.assertRaises(ledger.LedgerError):
                    ledger.commit_state(api, head, {}, "Refused.")
                self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_payload_bounds_and_nonfinite_values_rejected_before_api(self):
        cases = [({"x": "a" * ledger.MAX_STATE_BYTES}, "R", None),
                 ({"x": float("nan")}, "R", None),
                 ({1: "nonstring key"}, "R", None),
                 ({}, "a" * (ledger.MAX_REPORT_BYTES + 1), None),
                 ({}, "R", "a" * (ledger.MAX_CANDIDATE_BYTES + 1))]
        for state, report, candidate in cases:
            with self.subTest(report_length=len(report)):
                api = FakeAPI()
                with self.assertRaises(ledger.LedgerError):
                    ledger.commit_state(api, None, state, report, candidate)
                self.assertEqual(api.calls, [])

    def test_corrupt_duplicate_and_nonfinite_stored_state_refused(self):
        for raw in (b'{"attempts":1,"attempts":0}', b'{"attempts":NaN}', b'[]', b'not json'):
            with self.subTest(raw=raw):
                api = FakeAPI(HEAD)
                api.raw = raw
                with self.assertRaises(ledger.LedgerError):
                    ledger.fetch_state(api)
        api = FakeAPI(HEAD)
        api.bad_hash = True
        with self.assertRaisesRegex(ledger.LedgerError, "state_blob_integrity_mismatch"):
            ledger.fetch_state(api)

    def test_private_repo_and_truncated_tree_refused(self):
        api = FakeAPI(HEAD)
        api.private = True
        with self.assertRaisesRegex(ledger.LedgerError, "public_repository_required"):
            ledger.fetch_state(api)
        api = FakeAPI(HEAD)
        api.truncated = True
        with self.assertRaisesRegex(ledger.LedgerError, "incomplete_parent_tree"):
            ledger.commit_state(api, HEAD, {}, "No incomplete state.")


if __name__ == "__main__":
    unittest.main()
