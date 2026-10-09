"""Offline mock evidence/telemetry tests: standard library, no devices or network."""
import copy
import json
from pathlib import Path
import unittest

from p0_evidence_gate.gate import validate_catalog, simulate_only, sha256_json

CATALOG = Path(__file__).resolve().parents[1] / "p0_evidence_gate" / "evidence_2026-10-09.json"


class EvidenceGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    def test_catalog_validation(self):
        self.assertEqual(validate_catalog(self.catalog)["entries"], 8)

    def test_stable_hash(self):
        self.assertEqual(sha256_json(self.catalog), sha256_json(json.loads(json.dumps(self.catalog))))

    def test_digest_changes_on_mutation(self):
        modified = copy.deepcopy(self.catalog)
        modified["records"][0]["observed"] = "unsupported claim"
        self.assertNotEqual(sha256_json(self.catalog), sha256_json(modified))

    def test_reject_program_as_animal_experiment(self):
        modified = copy.deepcopy(self.catalog)
        modified["records"][-1]["environment"] = "ANIMAL"
        with self.assertRaises(ValueError):
            validate_catalog(modified)

    def test_reject_missing_limitations(self):
        modified = copy.deepcopy(self.catalog)
        modified["records"][0]["limitations"] = []
        with self.assertRaises(ValueError):
            validate_catalog(modified)

    def test_reject_insecure_url(self):
        modified = copy.deepcopy(self.catalog)
        modified["records"][0]["source_url"] = "http://example.org"
        with self.assertRaises(ValueError):
            validate_catalog(modified)

    def test_reject_unknown_bio_design_fields(self):
        modified = copy.deepcopy(self.catalog)
        modified["records"][0]["construct_sequence"] = "FAKE"
        with self.assertRaises(ValueError):
            validate_catalog(modified)

    def test_reject_duplicate_id(self):
        modified = copy.deepcopy(self.catalog)
        modified["records"][1]["id"] = modified["records"][0]["id"]
        with self.assertRaises(ValueError):
            validate_catalog(modified)

    def test_reject_bad_schema(self):
        modified = copy.deepcopy(self.catalog)
        modified["schema"] = "untrusted-v2"
        with self.assertRaises(ValueError):
            validate_catalog(modified)

    def test_accept_synthetic_tick(self):
        msg = {"target":"virtual_model","command":"SIMULATED_TICK","sequence":1,"uncertainty":.03}
        receipt = simulate_only(msg)
        self.assertTrue(receipt["accepted"])
        self.assertEqual(receipt["state"]["virtual_mass"], 9)
        self.assertEqual(receipt["state"]["virtual_phase"], 1)

    def test_block_animal_target(self):
        msg = {"target":"animal","command":"SIMULATED_TICK","sequence":1,"uncertainty":0}
        self.assertFalse(simulate_only(msg)["accepted"])

    def test_block_biological_actuator(self):
        msg = {"target":"virtual_model","command":"ACTUATE_BACTERIA","sequence":1,"uncertainty":0}
        self.assertFalse(simulate_only(msg)["accepted"])

    def test_block_hardware_or_sequence_injection(self):
        msg = {"target":"virtual_model","command":"SIMULATED_TICK","sequence":1,
               "uncertainty":0,"dna_sequence":"FAKE"}
        self.assertEqual(simulate_only(msg)["reason"], "unrecognized_or_missing_fields")

    def test_block_uncertainty_high(self):
        msg = {"target":"virtual_model","command":"SIMULATED_TICK","sequence":1,"uncertainty":.25}
        self.assertEqual(simulate_only(msg)["reason"], "uncertainty_hard_stop")

    def test_block_nan_uncertainty(self):
        msg = {"target":"virtual_model","command":"SIMULATED_TICK","sequence":1,"uncertainty":float("nan")}
        self.assertFalse(simulate_only(msg)["accepted"])

    def test_block_replay(self):
        msg = {"target":"virtual_model","command":"SIMULATED_TICK","sequence":1,"uncertainty":0}
        first = simulate_only(msg)
        self.assertEqual(simulate_only(msg,first["state"])["reason"], "replay_or_gap_detected")

    def test_block_gap(self):
        msg = {"target":"virtual_model","command":"SIMULATED_TICK","sequence":2,"uncertainty":0}
        self.assertFalse(simulate_only(msg)["accepted"])

    def test_hard_stop_at_virtual_limit(self):
        state = None
        for seq in range(1,4):
            msg = {"target":"virtual_model","command":"SIMULATED_TICK","sequence":seq,"uncertainty":0}
            state = simulate_only(msg,state)["state"]
        fourth = {"target":"virtual_model","command":"SIMULATED_TICK","sequence":4,"uncertainty":0}
        self.assertEqual(simulate_only(fourth,state)["reason"], "bounded_phase_guard")

    def test_mass_conservation_guard(self):
        msg = {"target":"virtual_model","command":"SIMULATED_TICK","sequence":2,"uncertainty":0}
        bad_state = {"sequence":1,"virtual_mass":8,"virtual_phase":1}
        self.assertEqual(simulate_only(msg,bad_state)["reason"], "conservation_or_state_guard")

    def test_read_state_does_not_advance_virtual_phase(self):
        msg = {"target":"virtual_model","command":"READ_STATE","sequence":1,"uncertainty":0}
        self.assertEqual(simulate_only(msg)["state"]["virtual_phase"], 0)


if __name__ == "__main__":
    unittest.main()
