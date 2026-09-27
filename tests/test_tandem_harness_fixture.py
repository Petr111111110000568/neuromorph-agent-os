"""Fresh-file token scan contract; no SDK, model or network is executed."""
import importlib.util
import copy
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("tandem_fixture_scan_tests",
    Path(__file__).resolve().parents[1] / "scripts" / "tandem_harness_fixture.py")
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)
TOKEN = "test-token-" + "a" * 54


class TokenScanTests(unittest.TestCase):
    def test_gateway_diagnostic_keeps_only_fixed_schema_and_known_field_names(self):
        source = {"schema_version": 1, "stage": "critique", "scope": "reply_decisions_not_client_delivery",
                  "response_attempts": 1, "http_response_counts": {"400": 1},
                  "validation_code_counts": {"unsupported_fields": 1},
                  "api_field_names": ["model", "messages"], "unknown_api_fields_omitted": 1,
                  "counters_saturated": False, "values_recorded": False}
        projected = fixture.project_gateway_diagnostic(source)
        self.assertEqual(projected, source)
        self.assertIsNot(projected, source)
        for field, value in (("api_field_names", [TOKEN]), ("http_response_counts", {TOKEN: 1}),
                             ("validation_code_counts", {TOKEN: 1}), ("values_recorded", True),
                             ("response_attempts", 1001)):
            changed = copy.deepcopy(source)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(fixture.FixtureError):
                fixture.project_gateway_diagnostic(changed)
        changed = dict(source, raw_body=TOKEN)
        with self.assertRaises(fixture.FixtureError):
            fixture.project_gateway_diagnostic(changed)

    def test_checks_fresh_stage_only_and_reports_no_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "new-stage"
            stage.mkdir()
            (root / "unrelated-profile.env").write_text(TOKEN, encoding="utf-8")
            (stage / "config.json").write_text('{"apiKey":"${NEUROMORPH_TANDEM_TOKEN}"}', encoding="utf-8")
            result = fixture.scan_ephemeral_token(stage, TOKEN)
            self.assertTrue(result["ephemeral_token_absent"])
            self.assertEqual(result["files_scanned"], 1)
            self.assertEqual(set(result), {"ephemeral_token_absent", "files_scanned", "bytes_scanned",
                                           "internal_links_checked"})

    def test_detects_token_across_read_boundary_without_exposing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / "models.json").write_bytes(b"x" * (65536 - 20) + TOKEN.encode() + b"tail")
            with self.assertRaises(fixture.FixtureError) as raised:
                fixture.scan_ephemeral_token(stage, TOKEN)
            self.assertEqual(str(raised.exception), "ephemeral_token_persisted")
            self.assertNotIn(TOKEN, str(raised.exception))
            self.assertNotIn(directory, str(raised.exception))

    def test_oversized_file_is_not_a_clean_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            with (stage / "large.log").open("wb") as target:
                target.truncate(2 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(fixture.FixtureError, "ephemeral_token_scan_limit") as raised:
                fixture.scan_ephemeral_token(stage, TOKEN)
            self.assertEqual(raised.exception.limit_metadata,
                             {"limit_kind": "file_bytes", "category": "other", "observed": 2 * 1024 * 1024 + 1})

    def test_total_budget_is_enforced_across_many_small_files(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            for index in range(9):
                with (stage / (str(index) + ".log")).open("wb") as target:
                    target.truncate(2 * 1024 * 1024)
            with self.assertRaisesRegex(fixture.FixtureError, "ephemeral_token_scan_limit"):
                fixture.scan_ephemeral_token(stage, TOKEN)

    def test_symlink_is_rejected_without_reading_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            stage.mkdir()
            target = root / "private.txt"
            target.write_text(TOKEN, encoding="utf-8")
            try:
                (stage / "linked.txt").symlink_to(target)
            except OSError:
                self.skipTest("OS does not permit test symlinks")
            with self.assertRaisesRegex(fixture.FixtureError, "ephemeral_token_scan_link_rejected") as raised:
                fixture.scan_ephemeral_token(stage, TOKEN)
            self.assertEqual(raised.exception.target_scope, "outside_stage")

    def test_internal_state_alias_and_cycle_are_scanned_once(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            state = stage / "state"
            state.mkdir()
            (state / "history.json").write_text('{"safe":true}', encoding="utf-8")
            try:
                (stage / ".openclaw").symlink_to(state, target_is_directory=True)
                (state / "back-to-stage").symlink_to(stage, target_is_directory=True)
            except OSError:
                self.skipTest("OS does not permit test symlinks")
            result = fixture.scan_ephemeral_token(stage, TOKEN)
            self.assertEqual(result["files_scanned"], 1)
            self.assertEqual(result["internal_links_checked"], 2)
            self.assertTrue(result["ephemeral_token_absent"])

    def test_internal_alias_does_not_hide_a_persisted_token(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            state = stage / "state"
            state.mkdir()
            (state / "models.json").write_text(TOKEN, encoding="utf-8")
            try:
                (stage / ".openclaw").symlink_to(state, target_is_directory=True)
            except OSError:
                self.skipTest("OS does not permit test symlinks")
            with self.assertRaisesRegex(fixture.FixtureError, "ephemeral_token_persisted"):
                fixture.scan_ephemeral_token(stage, TOKEN)


if __name__ == "__main__":
    unittest.main()
