"""Pinned-file admission and owner/project binding; no model/network calls."""
import concurrent.futures
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from workbench.m02_receipts import M02ReceiptLibrary
from workbench.service import ServiceError
from workbench.store import Store, canonical
from workbench.studio import StudioWorkspace


ROOT = Path(__file__).resolve().parents[1]
SEED = "m02-seed-36277624672"
RESUME = "m02-resume-36277674850"
SEED_SHA = "a9af1d917bd6e616e9ce44ed6637724ea701686c83abdf7a21e6b1f5d4ab7bbc"
RESUME_SHA = "02050443fed052fb8e1f4d4030c80e8d23e11bcb57b27f5c5b05aa4382869a9c"


class M02ReceiptLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.sqlite"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.studio = StudioWorkspace(self.store)
        self.library = M02ReceiptLibrary(self.store)

    def project(self, owner="local", key="project"):
        return self.studio.save({"kind": "project", "title": "Finite experiment",
                                 "body": "Historical cloud evidence", "idempotency_key": key}, owner)

    @staticmethod
    def request(project, receipt_id=SEED, key="import-one"):
        return {"project_id": project["id"], "receipt_id": receipt_id, "idempotency_key": key}

    def count(self):
        return self.store.db.execute("SELECT COUNT(*) FROM studio_m02_imports").fetchone()[0]

    def copied_root(self):
        destination = Path(self.tmp.name) / "fixture-root"
        for relative in ("config/m02_receipt_registry.json", "data/m02_receipts/seed.json",
                         "data/m02_receipts/resume.json"):
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        return destination

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(ServiceError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_real_pinned_receipts_and_public_projection_have_distinct_hash_scope(self):
        state = self.library.snapshot()
        seed, resume = state["catalog"]
        self.assertEqual([seed["receipt_id"], resume["receipt_id"]], [SEED, RESUME])
        self.assertEqual([seed["canonical_sha256"], resume["canonical_sha256"]], [SEED_SHA, RESUME_SHA])
        for item, filename in ((seed, "seed.json"), (resume, "resume.json")):
            original = json.loads((ROOT / "data/m02_receipts" / filename).read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(canonical(original).encode()).hexdigest(), item["canonical_sha256"])
            self.assertEqual(item["receipt_representation"], "bounded_projection")
            self.assertNotEqual(item["receipt"], original)
            self.assertNotEqual(hashlib.sha256(canonical(item["receipt"]).encode()).hexdigest(), item["canonical_sha256"])
            self.assertNotIn("restored_file_hashes", item["receipt"])
            self.assertNotIn("transitions", item["receipt"]["state"])
            self.assertNotIn("sha256", canonical(item["summary"]))
            for private in ("lease_token", "lease_digest", "completion_digest", "completion_fingerprint", "outbox"):
                self.assertNotIn(private, canonical(item))
        self.assertEqual(resume["receipt"]["previous_run_id"], seed["identity"]["run_id"])
        self.assertNotEqual(resume["receipt"]["previous_manifest_sha256"], SEED_SHA)
        self.assertEqual((seed["summary"]["reserved_attempts"], seed["summary"]["actual_attempts"]), (4, 1))
        self.assertEqual((resume["summary"]["reserved_attempts"], resume["summary"]["actual_attempts"]), (6, 3))
        self.assertTrue(state["capabilities"]["historical"])
        for flag in ("execution", "network_calls", "model_calls", "scientific_validation", "live_verification"):
            self.assertFalse(state["capabilities"][flag])
        seed["summary"]["reserved_attempts"] = 999
        self.assertEqual(self.library.snapshot()["catalog"][0]["summary"]["reserved_attempts"], 4)

    def test_import_persists_exact_association_and_does_not_edit_normal_artifacts(self):
        project = self.project()
        before = self.studio.snapshot()
        request = self.request(project)
        first = self.library.import_receipt(request)
        self.assertEqual(first, self.library.import_receipt(copy.deepcopy(request)))
        self.assertEqual(first["project_id"], project["id"])
        self.assertEqual(first["canonical_sha256"], SEED_SHA)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.studio.snapshot()["artifacts"], before["artifacts"])
        self.assertEqual(self.studio.snapshot()["events"], before["events"])
        other_store = Store(self.path)
        try:
            other = M02ReceiptLibrary(other_store)
            self.assertEqual(other.snapshot()["imports"], [first])
            self.assertEqual(other.import_receipt(request), first)
        finally:
            other_store.close()

    def test_owner_and_project_kind_are_checked_before_import_and_replay(self):
        project = self.project("alice")
        self.assert_error("not_found", self.library.import_receipt, self.request(project), owner="bob")
        task = self.studio.save({"kind": "task", "title": "Not a project", "project_id": project["id"],
                                 "idempotency_key": "task"}, "alice")
        self.assert_error("not_found", self.library.import_receipt, self.request(task), owner="alice")
        self.assertEqual(self.count(), 0)
        first = self.library.import_receipt(self.request(project), owner="alice")
        self.assertEqual(self.library.snapshot("bob")["imports"], [])
        self.assertEqual(self.library.snapshot("alice")["imports"], [first])
        self.assert_error("not_found", self.library.import_receipt, self.request(project), owner="bob")
        self.assertEqual(self.count(), 1)

    def test_idempotency_conflicts_bind_project_and_receipt_and_new_alias_is_rejected(self):
        project = self.project()
        another = self.project(key="another")
        first = self.library.import_receipt(self.request(project))
        self.assert_error("idempotency_conflict", self.library.import_receipt, self.request(project, RESUME))
        self.assert_error("idempotency_conflict", self.library.import_receipt, self.request(another))
        self.assert_error("receipt_already_imported", self.library.import_receipt, self.request(project, key="new-key"))
        self.assertEqual(self.library.snapshot()["imports"], [first])
        self.assertEqual(self.count(), 1)

    def test_user_cannot_supply_self_signed_receipt_registry_hash_owner_or_root(self):
        project = self.project()
        arbitrary = {"identity": "untrusted", "model_calls": 999}
        forged_hash = hashlib.sha256(canonical(arbitrary).encode()).hexdigest()
        attacks = [{"receipt": arbitrary, "canonical_sha256": forged_hash},
                   {"registry": {"canonical_sha256": forged_hash}},
                   {"root": str(ROOT)}, {"owner": "other"}, {"path": "data/m02_receipts/seed.json"},
                   {"url": "https://example.org/untrusted.json"}]
        for attack in attacks:
            with self.subTest(fields=set(attack)):
                self.assert_error("invalid_request", self.library.import_receipt, {**self.request(project), **attack})
        self.assert_error("receipt_not_found", self.library.import_receipt, self.request(project, "untrusted-receipt"))
        self.assertEqual(self.count(), 0)

    def test_concurrent_connections_create_one_immutable_association(self):
        project = self.project()
        request = self.request(project)
        other_store = Store(self.path)
        other = M02ReceiptLibrary(other_store)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda library: library.import_receipt(request), [self.library, other]))
            self.assertEqual(results[0], results[1])
            self.assertEqual(self.count(), 1)
        finally:
            other_store.close()

    def test_concurrent_distinct_keys_cannot_duplicate_same_owner_project_receipt(self):
        project = self.project()
        other_store = Store(self.path)
        other = M02ReceiptLibrary(other_store)

        def attempt(pair):
            library, key = pair
            try:
                return library.import_receipt(self.request(project, key=key))
            except ServiceError as exc:
                return exc.code

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, [(self.library, "first"), (other, "second")]))
            self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
            self.assertIn("receipt_already_imported", results)
            self.assertEqual(self.count(), 1)
        finally:
            other_store.close()

    def test_loading_is_lazy_and_missing_registry_cannot_bootstrap_or_mutate(self):
        project = self.project()
        library = M02ReceiptLibrary(self.store, root=Path(self.tmp.name) / "missing")
        error = self.assert_error("receipt_library_unavailable", library.import_receipt, self.request(project))
        self.assertEqual(error.status, 503)
        self.assertNotIn(self.tmp.name, error.message)
        self.assertEqual(self.count(), 0)

    def test_modified_source_is_rejected_again_after_previously_successful_catalog(self):
        root = self.copied_root()
        project = self.project()
        library = M02ReceiptLibrary(self.store, root=root)
        self.assertEqual(len(library.snapshot()["catalog"]), 2)
        path = root / "data/m02_receipts/seed.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["state"]["budget"]["reserved_attempts"] = 0
        path.write_text(canonical(data), encoding="utf-8")
        self.assert_error("receipt_library_unavailable", library.import_receipt, self.request(project))
        self.assertEqual(self.count(), 0)

    def test_registry_identity_and_path_cannot_point_to_another_run_or_file(self):
        root = self.copied_root()
        registry_path = root / "config/m02_receipt_registry.json"
        original = json.loads(registry_path.read_text(encoding="utf-8"))
        library = M02ReceiptLibrary(self.store, root=root)
        for changed in ("identity", "path"):
            registry = copy.deepcopy(original)
            if changed == "identity":
                registry["receipts"][0]["identity"]["run_id"] = "1"
            else:
                registry["receipts"][0]["path"] = "../../outside.json"
            registry_path.write_text(canonical(registry), encoding="utf-8")
            self.assert_error("receipt_library_unavailable", library.snapshot)
        self.assertEqual(self.count(), 0)

    def test_duplicate_fields_nonfinite_deep_or_oversized_json_never_mutates(self):
        root = self.copied_root()
        project = self.project()
        path = root / "data/m02_receipts/seed.json"
        original = path.read_text(encoding="utf-8")
        library = M02ReceiptLibrary(self.store, root=root)
        invalid = [original.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
                   '{"schema_version": NaN}', '[' * 35 + '0' + ']' * 35, ' ' * (128 * 1024 + 1)]
        for raw in invalid:
            path.write_text(raw, encoding="utf-8")
            self.assert_error("receipt_library_unavailable", library.import_receipt, self.request(project))
        self.assertEqual(self.count(), 0)

    def test_whitespace_differences_preserve_canonical_pinned_identity(self):
        root = self.copied_root()
        path = root / "data/m02_receipts/seed.json"
        path.write_text(canonical(json.loads(path.read_text(encoding="utf-8"))), encoding="utf-8")
        self.assertEqual(M02ReceiptLibrary(self.store, root=root).snapshot()["catalog"][0]["canonical_sha256"], SEED_SHA)

    def test_receipt_links_cannot_escape_the_operator_root(self):
        root = self.copied_root()
        path = root / "data/m02_receipts/seed.json"
        target = Path(self.tmp.name) / "outside.json"
        shutil.copyfile(path, target)
        path.unlink()
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable on this cloud runner")
        self.assert_error("receipt_library_unavailable", M02ReceiptLibrary(self.store, root=root).snapshot)

    def test_resource_limit_preserves_existing_immutable_record(self):
        project = self.project()
        first = self.library.import_receipt(self.request(project))
        with patch("workbench.m02_receipts.MAX_IMPORTS_PER_OWNER", 1):
            self.assert_error("workspace_limit", self.library.import_receipt, self.request(project, RESUME, "resume"))
            self.assertEqual(self.library.import_receipt(self.request(project)), first)
        self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main()
