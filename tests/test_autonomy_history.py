"""Bounded discovery history: persistence, provenance, failures and file safety."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from workbench.autonomy import history


def timestamp(hour=0):
    return (datetime(2026, 9, 25, tzinfo=timezone.utc) +
            timedelta(hours=hour)).isoformat()


def candidate(ident="catalog:one", revision="v1", hour=0):
    return {
        "id": ident, "name": "Исследовательский источник", "entity_type": "resource",
        "source_url": "https://example.test/source", "capabilities": ["search"],
        "status": "discovered", "endpoint_contacted": False, "enrolled": False,
        "verification_scope": "public_catalog_metadata_only",
        "provenance": {"provider": "test", "source_id": ident, "revision": revision,
                       "license": "MIT", "raw_sha256": hashlib.sha256(revision.encode()).hexdigest(),
                       "retrieved_at": timestamp(hour)},
    }


def cycle(items=None, hour=0, identity="same-content", reports=None):
    return {
        "cycle_id": hashlib.sha256(identity.encode()).hexdigest(),
        "created_at": timestamp(hour), "mode": "online", "status": "disabled",
        "discovery": {"query": "public research", "requests": 1,
                      "provider_reports": reports or [{"provider": "test", "status": "ok"}],
                      "candidates": [candidate(hour=hour)] if items is None else items},
        "inference": {"status": "disabled", "requests": 0},
    }


class DiscoveryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "history.sqlite"

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return connection.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()

    def snapshot(self):
        return {table: self.rows(table) for table in ("runs", "resources", "observations")}

    def test_digest_ignores_recursive_observation_times_but_keeps_revision_and_hash(self):
        original = candidate()
        changed_time = deepcopy(original)
        changed_time["provenance"]["retrieved_at"] = timestamp(10)
        changed_time["nested"] = [{"seen_at": timestamp(1), "value": "a"}]
        original["nested"] = [{"seen_at": timestamp(0), "value": "a"}]
        before = deepcopy(original)
        self.assertEqual(history.candidate_digest(original), history.candidate_digest(changed_time))
        self.assertEqual(original, before)
        for key, value in (("revision", "v2"), ("license", "Apache-2.0"),
                           ("raw_sha256", "b" * 64), ("source_id", "another")):
            with self.subTest(key=key):
                changed = deepcopy(original)
                changed["provenance"][key] = value
                self.assertNotEqual(history.candidate_digest(original), history.candidate_digest(changed))

    def test_restart_observation_and_exact_replay_are_idempotent(self):
        first = history.record_cycle(self.path, cycle())
        self.assertEqual(first["new_resources"], 1)
        self.assertEqual(first["counts"], {"resources": 1, "observations": 1, "runs": 1})
        # Each call closes its connection; reopen has no dependence on process memory.
        second = history.record_cycle(self.path, cycle(hour=2))
        self.assertEqual(second["unchanged_resources"], 1)
        self.assertEqual(second["counts"], {"resources": 1, "observations": 1, "runs": 2})
        resource = self.rows("resources")[0]
        self.assertEqual(resource[1], history._timestamp(timestamp(0)))
        self.assertEqual(resource[2], history._timestamp(timestamp(2)))
        self.assertEqual(json.loads(resource[4])["provenance"]["retrieved_at"], timestamp(2))
        before = self.snapshot()
        replay = history.record_cycle(self.path, cycle(hour=2))
        self.assertEqual(replay["status"], "already_recorded")
        self.assertEqual(self.snapshot(), before)

    def test_equivalent_timezones_share_run_identity(self):
        artifact = cycle()
        history.record_cycle(self.path, artifact)
        artifact["created_at"] = "2026-09-25T03:00:00+03:00"
        self.assertEqual(history.record_cycle(self.path, artifact)["status"], "already_recorded")

    def test_revision_change_retains_before_and_after_cards(self):
        history.record_cycle(self.path, cycle())
        changed = candidate(revision="v2", hour=1)
        result = history.record_cycle(self.path, cycle([changed], hour=1, identity="new-revision"))
        self.assertEqual(result["changed_resources"], 1)
        self.assertEqual(result["counts"]["observations"], 2)
        self.assertEqual(json.loads(self.rows("resources")[0][4])["provenance"]["revision"], "v2")
        observations = sorted(self.rows("observations"), key=lambda row: row[2])
        self.assertEqual([row[3] for row in observations], ["new", "changed"])
        self.assertEqual([json.loads(row[5])["provenance"]["revision"] for row in observations], ["v1", "v2"])

    def test_no_discovery_results_or_failed_provider_does_not_delete_resources(self):
        history.record_cycle(self.path, cycle())
        before = self.rows("resources")
        result = history.record_cycle(self.path, cycle([], hour=2, reports=[
            {"provider": "test", "status": "unavailable", "error": "bounded_timeout"}]))
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(self.rows("resources"), before)
        self.assertEqual(result["counts"]["observations"], 1)

    def test_out_of_order_revision_cannot_downgrade_current_card(self):
        history.record_cycle(self.path, cycle([candidate(revision="v2", hour=2)], hour=2))
        result = history.record_cycle(self.path, cycle([candidate(revision="v1")], identity="older"))
        self.assertEqual(result["historical_observations"], 1)
        resource = self.rows("resources")[0]
        self.assertEqual(resource[1], history._timestamp(timestamp(0)))
        self.assertEqual(resource[2], history._timestamp(timestamp(2)))
        self.assertEqual(json.loads(resource[4])["provenance"]["revision"], "v2")
        self.assertIn("historical", [row[3] for row in self.rows("observations")])

    def test_duplicate_card_dedup_and_conflicting_duplicate_rejection(self):
        result = history.record_cycle(self.path, cycle([candidate(), candidate(hour=1)]))
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["counts"]["observations"], 1)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
            history.record_cycle(self.path, cycle([candidate(), candidate(revision="v2")], hour=1))
        self.assertEqual(self.snapshot(), before)

    def test_same_run_with_conflicting_content_is_rejected_without_mutation(self):
        history.record_cycle(self.path, cycle())
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "conflicting content"):
            history.record_cycle(self.path, cycle([candidate(revision="v2")]))
        self.assertEqual(self.snapshot(), before)

    def test_same_time_different_cycle_conflict_rolls_back_all_candidates(self):
        history.record_cycle(self.path, cycle())
        before = self.snapshot()
        conflicting = cycle([candidate("catalog:a-new"), candidate(revision="v2")],
                            identity="another-cycle-same-time")
        with self.assertRaisesRegex(ValueError, "same-time resource"):
            history.record_cycle(self.path, conflicting)
        self.assertEqual(self.snapshot(), before)

    def test_exception_rolls_back_run_card_observation_and_retention(self):
        history.record_cycle(self.path, cycle())
        before = self.snapshot()
        changed = cycle([candidate(revision="v2", hour=1), candidate("catalog:two", hour=1)], hour=1)
        real_trim = history._trim

        def fail_after_trim(connection):
            real_trim(connection)
            raise RuntimeError("injected failure after writes")

        with patch.object(history, "MAX_RESOURCES", 1), patch.object(history, "_trim", fail_after_trim):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                history.record_cycle(self.path, changed)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(Path(str(self.path) + "-journal").exists())
        retry = history.record_cycle(self.path, changed)
        self.assertEqual(retry["new_resources"], 1)
        self.assertEqual(retry["changed_resources"], 1)

    def test_retention_bounds_all_tables_and_preserves_recent_resources(self):
        with patch.object(history, "MAX_RESOURCES", 2), \
             patch.object(history, "MAX_OBSERVATIONS", 3), \
             patch.object(history, "MAX_RUNS", 2):
            history.record_cycle(self.path, cycle([candidate("catalog:a"), candidate("catalog:b")]))
            history.record_cycle(self.path, cycle([candidate("catalog:a", "v2", 1),
                                                  candidate("catalog:c", hour=1)], hour=1))
            result = history.record_cycle(self.path, cycle([candidate("catalog:a", "v3", 2),
                                                           candidate("catalog:c", "v2", 2)], hour=2))
            self.assertEqual(result["counts"], {"resources": 2, "observations": 3, "runs": 2})
            self.assertGreater(result["retention"]["observations"], 0)
            self.assertEqual(result["retention"]["runs"], 1)
            self.assertEqual({row[0] for row in self.rows("resources")}, {"catalog:a", "catalog:c"})
            retained_runs = {row[0] for row in self.rows("runs")}
            self.assertTrue(all(row[0] in retained_runs for row in self.rows("observations")))
            # Replaying outside retention is allowed but remains bounded and does not
            # collide with orphan observation primary keys.
            replay = history.record_cycle(self.path, cycle([candidate("catalog:a"), candidate("catalog:b")]))
            self.assertLessEqual(replay["counts"]["resources"], 2)
            self.assertLessEqual(replay["counts"]["runs"], 2)
            self.assertLessEqual(replay["counts"]["observations"], 3)

    def test_missing_parent_and_existing_ordinary_file_are_not_modified(self):
        missing = self.directory / "missing" / "history.sqlite"
        with self.assertRaisesRegex(ValueError, "parent directory"):
            history.record_cycle(missing, cycle())
        self.assertFalse(missing.parent.exists())
        self.path.write_bytes(b"ordinary user file")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Not a SQLite"):
            history.record_cycle(self.path, cycle())
        self.assertEqual(self.path.read_bytes(), before)

    def test_existing_foreign_schema_is_rejected_unchanged(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE ordinary_data (value TEXT)")
            connection.execute("INSERT INTO ordinary_data VALUES ('keep')")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Not a Meta-Harness"):
            history.record_cycle(self.path, cycle())
        self.assertEqual(self.path.read_bytes(), before)

    def test_unexpected_trigger_in_owned_database_is_rejected_before_execution(self):
        history.record_cycle(self.path, cycle())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TRIGGER unexpected AFTER INSERT ON runs
                BEGIN DELETE FROM resources; END""")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            history.record_cycle(self.path, cycle(hour=1))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.rows("resources")), 1)

    def test_preexisting_sidecars_are_rejected_and_not_deleted(self):
        history.record_cycle(self.path, cycle())
        before = self.path.read_bytes()
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.path) + suffix)
                sidecar.write_bytes(b"foreign file must remain")
                with self.assertRaisesRegex(ValueError, "sidecar"):
                    history.record_cycle(self.path, cycle(hour=1))
                self.assertEqual(sidecar.read_bytes(), b"foreign file must remain")
                self.assertEqual(self.path.read_bytes(), before)
                sidecar.unlink()

    def test_oversized_database_is_rejected_without_changes(self):
        history.record_cycle(self.path, cycle())
        before = self.path.read_bytes()
        with patch.object(history, "MAX_DB_BYTES", len(before) - 1):
            with self.assertRaisesRegex(ValueError, "size limit"):
                history.record_cycle(self.path, cycle(hour=1))
        self.assertEqual(self.path.read_bytes(), before)

    def test_symlink_file_is_rejected(self):
        original = self.directory / "original.sqlite"
        history.record_cycle(original, cycle())
        try:
            self.path.symlink_to(original)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("Symlink creation unavailable: " + str(exc))
        before = original.read_bytes()
        with self.assertRaisesRegex(ValueError, "symlink or reparse"):
            history.record_cycle(self.path, cycle(hour=1))
        self.assertEqual(original.read_bytes(), before)

    def test_symlink_parent_is_rejected(self):
        original = self.directory / "real"
        original.mkdir()
        alias = self.directory / "alias"
        try:
            alias.symlink_to(original, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("Symlink creation unavailable: " + str(exc))
        with self.assertRaisesRegex(ValueError, "symlink or reparse"):
            history.record_cycle(alias / "history.sqlite", cycle())
        self.assertFalse((original / "history.sqlite").exists())

    def test_hardlink_database_is_rejected(self):
        original = self.directory / "original.sqlite"
        history.record_cycle(original, cycle())
        try:
            os.link(original, self.path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("Hardlink creation unavailable: " + str(exc))
        before = original.read_bytes()
        with self.assertRaisesRegex(ValueError, "one regular file"):
            history.record_cycle(self.path, cycle(hour=1))
        self.assertEqual(original.read_bytes(), before)

    def test_reparse_point_attribute_is_rejected(self):
        class Info:
            st_mode = 0o100600
            st_file_attributes = 0x400
        with self.assertRaisesRegex(ValueError, "symlink or reparse"):
            history._reject_link(self.path, Info())

    def test_invalid_timestamps_are_rejected_before_database_creation(self):
        for bad in ("2026-09-25", "2026-09-25T00:00:00", "2026-02-30T00:00:00Z",
                    "1969-12-31T23:59:59Z", "2026-09-25T00:00:00+25:00", 3):
            with self.subTest(value=bad):
                artifact = cycle()
                artifact["created_at"] = bad
                with self.assertRaises(ValueError):
                    history.record_cycle(self.path, artifact)
                self.assertFalse(self.path.exists())
        artifact = cycle()
        artifact["discovery"]["candidates"][0]["provenance"]["retrieved_at"] = "unknown"
        with self.assertRaises(ValueError):
            history.record_cycle(self.path, artifact)
        self.assertFalse(self.path.exists())

    def test_input_limits_and_read_only_scope_are_checked_before_writes(self):
        variants = []
        too_many = cycle([candidate("id:" + str(i)) for i in range(33)])
        variants.append(too_many)
        too_large = cycle()
        too_large["discovery"]["candidates"][0]["name"] = "x" * history.MAX_CANDIDATE_BYTES
        variants.append(too_large)
        invalid_id = cycle()
        invalid_id["cycle_id"] = "not-a-hash"
        variants.append(invalid_id)
        for field in ("endpoint_contacted", "enrolled"):
            active = cycle()
            active["discovery"]["candidates"][0][field] = True
            variants.append(active)
        nonfinite = cycle()
        nonfinite["discovery"]["candidates"][0]["score"] = float("nan")
        variants.append(nonfinite)
        recursive = cycle()
        recursive["discovery"]["candidates"][0]["loop"] = recursive
        variants.append(recursive)
        for i, artifact in enumerate(variants):
            with self.subTest(case=i), self.assertRaises(ValueError):
                history.record_cycle(self.path, artifact)
            self.assertFalse(self.path.exists())

    def test_only_public_summary_and_cards_are_stored(self):
        artifact = cycle()
        artifact["inference"]["private_response"] = "excluded-provider-response"
        artifact["proposal"] = {"files": [{"content": "excluded-proposal-text"}]}
        artifact["plan"] = ["excluded-plan"]
        history.record_cycle(self.path, artifact)
        text = json.dumps(self.snapshot(), ensure_ascii=False)
        self.assertNotIn("excluded-provider-response", text)
        self.assertNotIn("excluded-proposal-text", text)
        self.assertNotIn("excluded-plan", text)
        self.assertIn("Исследовательский источник", text)


if __name__ == "__main__":
    unittest.main()

