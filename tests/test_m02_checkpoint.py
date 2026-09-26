"""M02 checkpoint boundaries using real SQLite writes, no model/network calls.

Execute in the approved cloud test runner. This suite does not instantiate the
parallel dispatcher implementation and does not duplicate Queue lease tests.
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from workbench import m02_checkpoint as checkpoint
from workbench.network.queue import Queue


IDENTITY = {
    "repository": "Petr111111110000568/neuromorph-agent-os",
    "workflow_id": ".github/workflows/m02-checkpoint.yml",
    "branch": "main",
    "commit": "2b731fbba92fc8427b7296f80579e9d6a8e02e9c",
    "run_id": "36270000001",
}


class M02CheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.bundle = self.root / "checkpoint"
        self.restored = self.root / "restored"
        connection = sqlite3.connect(self.source / "control.sqlite")
        with connection:
            connection.execute("CREATE TABLE budgets (id TEXT PRIMARY KEY, reserved INTEGER, maximum INTEGER)")
            connection.execute("INSERT INTO budgets VALUES ('m02', 6, 8)")
            connection.execute("CREATE TABLE revisions (task TEXT PRIMARY KEY, revision INTEGER)")
            connection.execute("INSERT INTO revisions VALUES ('task-a', 2)")
        connection.close()
        queue = Queue(self.source / "queue.sqlite")
        try:
            queue.register_worker("fixture-worker", ["simulation"])
            job = queue.submit("simulation", {"fixture_id": "synthetic-m02"},
                               idempotency_key="m02:checkpoint-fixture", max_attempts=2)
            self.job_id = job["id"]
            claim = queue.claim("fixture-worker")
            self.assertIsNotNone(claim)
            # Raw lease tokens stay in memory only and are never printed or
            # included in the receipt. The real queue persists only digests.
        finally:
            queue.close()
        (self.source / "seed-receipt.json").write_text(json.dumps({
            "schema_version": 1, "synthetic": True, "external_model_calls": 0,
            "job_id": self.job_id, "reserved": 6,
        }), encoding="utf-8")

    def create(self):
        return checkpoint.create_checkpoint(self.source, self.bundle, dict(IDENTITY))

    def restore(self, digest, identity=None):
        return checkpoint.restore_checkpoint(self.bundle, self.restored, digest,
                                             dict(IDENTITY) if identity is None else identity)

    def change_manifest(self, change):
        path = self.bundle / checkpoint.MANIFEST
        value = json.loads(path.read_text(encoding="utf-8"))
        change(value)
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def assert_rejected(self, operation):
        with self.assertRaises(checkpoint.CheckpointError):
            operation()
        self.assertFalse(self.restored.exists())

    def symlink(self, target, link, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("host does not allow fixture symlinks")

    def test_restore_preserves_real_sqlite_budgets_revision_and_queue_attempt(self):
        digest = self.create()
        self.assertEqual(digest, hashlib.sha256((self.bundle / "manifest.json").read_bytes()).hexdigest())
        manifest = self.restore(digest)
        self.assertEqual(manifest["identity"], IDENTITY)
        connection = sqlite3.connect(self.restored / "control.sqlite")
        try:
            self.assertEqual(connection.execute("SELECT reserved, maximum FROM budgets").fetchone(), (6, 8))
            self.assertEqual(connection.execute("SELECT revision FROM revisions").fetchone(), (2,))
        finally:
            connection.close()
        queue = Queue(self.restored / "queue.sqlite")
        try:
            self.assertEqual(queue.get(self.job_id)["attempts"], 1)
            self.assertEqual(queue.get(self.job_id)["max_attempts"], 2)
            self.assertEqual(queue.by_idempotency_key("m02:checkpoint-fixture")["id"], self.job_id)
        finally:
            queue.close()
        self.assertEqual(set(manifest["files"]), set(checkpoint.FILES))
        self.assertFalse(any(path.name.endswith(("-wal", "-shm")) for path in self.bundle.iterdir()))

    def test_committed_wal_is_included_by_sqlite_backup(self):
        connection = sqlite3.connect(self.source / "control.sqlite")
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            with connection:
                connection.execute("UPDATE budgets SET reserved=8")
            wal = self.source / "control.sqlite-wal"
            self.assertGreater(wal.stat().st_size, 0)
            # No transaction/writer is active, but the connection keeps WAL
            # present; a direct byte copy of the main DB would lose this write.
            digest = self.create()
            self.restore(digest)
            restored = sqlite3.connect(self.restored / "control.sqlite")
            try:
                self.assertEqual(restored.execute("SELECT reserved FROM budgets").fetchone(), (8,))
            finally:
                restored.close()
        finally:
            connection.close()

    def test_restored_state_can_be_snapshotted_again_with_new_identity(self):
        self.restore(self.create())
        next_identity = {**IDENTITY, "run_id": "36270000002"}
        next_bundle = self.root / "checkpoint-next"
        digest = checkpoint.create_checkpoint(self.restored, next_bundle, next_identity)
        manifest = checkpoint.restore_checkpoint(next_bundle, self.root / "restored-next", digest, next_identity)
        self.assertEqual(manifest["identity"]["run_id"], "36270000002")

    def test_unchanged_quiescent_snapshot_has_stable_manifest_digest(self):
        first = self.create()
        second = checkpoint.create_checkpoint(self.source, self.root / "bundle-two", dict(IDENTITY))
        self.assertEqual(first, second)

    def test_reject_manifest_digest_before_json_or_database_open(self):
        self.create()
        with patch.object(checkpoint.sqlite3, "connect", side_effect=AssertionError("must not open database")):
            self.assert_rejected(lambda: self.restore("0" * 64))

    def test_verify_all_payloads_before_first_copy(self):
        digest = self.create()
        (self.bundle / "seed-receipt.json").write_text('{"tampered":true}', encoding="utf-8")
        with patch.object(checkpoint, "_copy_verified", side_effect=AssertionError("must not copy")):
            self.assert_rejected(lambda: self.restore(digest))

    def test_changed_payload_after_validation_is_rejected_during_copy(self):
        digest = self.create()
        copy = checkpoint._copy_verified
        def change_then_copy(source, target, expected, limit):
            if source.name == "seed-receipt.json":
                source.write_text('{"changed":true}', encoding="utf-8")
            return copy(source, target, expected, limit)
        with patch.object(checkpoint, "_copy_verified", side_effect=change_then_copy):
            self.assert_rejected(lambda: self.restore(digest))

    def test_reject_mismatched_producer_fields(self):
        digest = self.create()
        for field, value in (("repository", "someone/another"),
                             ("workflow_id", ".github/workflows/other.yml"),
                             ("branch", "different-branch"), ("commit", "a" * 40),
                             ("run_id", "36270000002")):
            with self.subTest(field=field):
                self.assert_rejected(lambda: self.restore(digest, {**IDENTITY, field: value}))

    def test_identity_has_closed_schema_and_bounded_strings(self):
        bad_values = [
            {**IDENTITY, "extra": "x"}, {k: v for k, v in IDENTITY.items() if k != "commit"},
            {**IDENTITY, "run_id": 123}, {**IDENTITY, "run_id": "0"},
            {**IDENTITY, "workflow_id": "../../wrong.yml"}, {**IDENTITY, "commit": "F" * 40},
            {**IDENTITY, "branch": "main/../other"}, {**IDENTITY, "repository": "a" * 241},
        ]
        for identity in bad_values:
            with self.subTest(identity=identity):
                with self.assertRaises(checkpoint.CheckpointError):
                    checkpoint.create_checkpoint(self.source, self.bundle, identity)
                self.assertFalse(self.bundle.exists())

    def test_reject_unknown_missing_and_noninteger_manifest_schema(self):
        self.create()
        original = (self.bundle / "manifest.json").read_bytes()
        mutations = [lambda m: m.update(extra=True), lambda m: m.pop("identity"),
                     lambda m: m.update(schema_version=2), lambda m: m.update(schema_version=True)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                (self.bundle / "manifest.json").write_bytes(original)
                self.assert_rejected(lambda: self.restore(self.change_manifest(mutation)))

    def test_reject_unknown_missing_or_traversing_manifest_filename(self):
        self.create()
        original = (self.bundle / "manifest.json").read_bytes()
        for key in ("../outside.sqlite", "/absolute.sqlite", "queue.sqlite/child", "queue.sqlite:stream"):
            with self.subTest(key=key):
                (self.bundle / "manifest.json").write_bytes(original)
                def replace_name(manifest):
                    manifest["files"][key] = manifest["files"].pop("queue.sqlite")
                self.assert_rejected(lambda: self.restore(self.change_manifest(replace_name)))
        (self.bundle / "manifest.json").write_bytes(original)
        self.assert_rejected(lambda: self.restore(self.change_manifest(lambda m: m["files"].pop("queue.sqlite"))))

    def test_reject_file_entry_schema_size_and_digest(self):
        self.create()
        original = (self.bundle / "manifest.json").read_bytes()
        mutations = [lambda e: e.update(extra=1), lambda e: e.update(bytes=True),
                     lambda e: e.update(bytes=0),
                     lambda e: e.update(bytes=checkpoint.MAX_DATABASE_BYTES + 1),
                     lambda e: e.update(sha256="not-a-digest")]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                (self.bundle / "manifest.json").write_bytes(original)
                digest = self.change_manifest(lambda m: mutation(m["files"]["queue.sqlite"]))
                self.assert_rejected(lambda: self.restore(digest))

    def test_reject_duplicate_json_keys_even_with_matching_digest(self):
        self.create()
        raw = (self.bundle / "manifest.json").read_bytes()
        raw = b'{"schema_version":1,' + raw[1:]
        (self.bundle / "manifest.json").write_bytes(raw)
        self.assert_rejected(lambda: self.restore(hashlib.sha256(raw).hexdigest()))

    def test_missing_manifest_or_payload_and_extra_bundle_file_are_rejected(self):
        digest = self.create()
        for name in ("manifest.json", "control.sqlite", "seed-receipt.json"):
            with self.subTest(name=name):
                path = self.bundle / name
                saved = path.read_bytes()
                path.unlink()
                self.assert_rejected(lambda: self.restore(digest))
                path.write_bytes(saved)
        (self.bundle / "unlisted.txt").write_text("not admitted", encoding="utf-8")
        self.assert_rejected(lambda: self.restore(digest))

    def test_existing_destination_is_never_overwritten_even_when_empty(self):
        digest = self.create()
        self.restored.mkdir()
        with self.assertRaises(checkpoint.CheckpointError):
            self.restore(digest)
        (self.restored / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(checkpoint.CheckpointError):
            self.restore(digest)
        self.assertEqual((self.restored / "keep.txt").read_text(), "keep")
        with self.assertRaises(checkpoint.CheckpointError):
            self.create()

    def test_destination_created_during_staging_is_not_replaced(self):
        digest = self.create()
        publish = checkpoint._publish_stage
        def race(stage, destination):
            destination.mkdir()
            (destination / "other-owner").write_text("reserved", encoding="utf-8")
            return publish(stage, destination)
        with patch.object(checkpoint, "_publish_stage", side_effect=race):
            with self.assertRaises(checkpoint.CheckpointError):
                self.restore(digest)
        self.assertEqual((self.restored / "other-owner").read_text(), "reserved")
        self.assertEqual({p.name for p in self.restored.iterdir()}, {"other-owner"})

    def test_publication_failure_has_no_manifest_and_does_not_touch_source(self):
        digest = self.create()
        expected = (self.bundle / "control.sqlite").read_bytes()
        link = os.link
        def fail_second(source, destination):
            if Path(destination).name == "queue.sqlite":
                raise OSError(errno.EIO, "injected fixture disk failure")
            return link(source, destination)
        with patch.object(checkpoint.os, "link", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.restore(digest)
        self.assertFalse(self.restored.exists())
        self.assertEqual((self.bundle / "control.sqlite").read_bytes(), expected)

    def test_filesystem_without_hardlinks_uses_exclusive_verified_copy(self):
        digest = self.create()
        with patch.object(checkpoint.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "fixture no hardlinks")):
            manifest = self.restore(digest)
        self.assertEqual(manifest["identity"], IDENTITY)
        for name in checkpoint.FILES:
            self.assertEqual((self.restored / name).read_bytes(), (self.bundle / name).read_bytes())

    def test_restore_rejects_symlink_payload(self):
        digest = self.create()
        (self.bundle / "queue.sqlite").unlink()
        self.symlink(self.source / "queue.sqlite", self.bundle / "queue.sqlite")
        self.assert_rejected(lambda: self.restore(digest))

    def test_create_rejects_symlink_database_and_sidecar(self):
        original = self.source / "control.sqlite"
        moved = self.root / "original.sqlite"
        original.rename(moved)
        self.symlink(moved, original)
        with self.assertRaises(checkpoint.CheckpointError):
            self.create()
        original.unlink()
        moved.rename(original)
        self.symlink(self.source / "seed-receipt.json", self.source / "control.sqlite-wal")
        with self.assertRaises(checkpoint.CheckpointError):
            self.create()
        self.assertFalse(self.bundle.exists())

    def test_reject_symlink_directory_and_parent_traversal(self):
        digest = self.create()
        linked = self.root / "linked"
        self.symlink(self.bundle, linked, directory=True)
        self.assert_rejected(lambda: checkpoint.restore_checkpoint(linked, self.restored, digest, IDENTITY))
        self.assert_rejected(lambda: checkpoint.restore_checkpoint(
            self.bundle, self.root / "unused" / ".." / "restored", digest, IDENTITY))

    def test_reject_nonjson_receipt_and_non_sqlite_source_without_reset(self):
        receipt = self.source / "seed-receipt.json"
        for raw in (b'[]', b'{"n":NaN}', b'{"n":1e9999}', b'{"a":1,"a":2}', b'{"unfinished":'):
            receipt.write_bytes(raw)
            with self.assertRaises(checkpoint.CheckpointError):
                self.create()
            self.assertFalse(self.bundle.exists())
        receipt.write_text('{"synthetic":true}', encoding="utf-8")
        (self.source / "control.sqlite").write_bytes(b"not SQLite")
        with self.assertRaises(checkpoint.CheckpointError):
            self.create()
        self.assertFalse(self.bundle.exists())
        self.assertEqual((self.source / "control.sqlite").read_bytes(), b"not SQLite")

    def test_source_extra_files_and_actual_size_limits_fail_closed(self):
        extra = self.source / "credentials.json"
        extra.write_text('{"fixture":true}', encoding="utf-8")
        with self.assertRaises(checkpoint.CheckpointError):
            self.create()
        extra.unlink()
        with patch.object(checkpoint, "MAX_DATABASE_BYTES", 512):
            with self.assertRaises(checkpoint.CheckpointError):
                self.create()
        self.assertFalse(self.bundle.exists())
        self.assertTrue((self.source / "queue.sqlite").is_file())


if __name__ == "__main__":
    unittest.main()
