"""Unit/integration tests against existing p0_synt.sint_sim baseline."""
import copy
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim_telemetry_adapter import SCHEMA, replay_dual_virtual_channels
from sint_sim import SafetyStop


def dual_frames():
    core = {"schema": SCHEMA, "mode": "SIMULATION_ONLY", "run_id": "SIM-20261009-X1",
            "frame": 11, "values": [1, 0, 0, 0, 0, 0], "uncertainty": 0.01, "quality": True}
    return ({**core, "channel": "SIM_A"}, {**core, "channel": "SIM_B"})


class TelemetryAdapterTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = dual_frames()
        self.target = [0, 0, 0, 0, 0, 1]

    def run_pair(self):
        return replay_dual_virtual_channels(self.a, self.b, self.target)

    def test_nominal_fixed_volume_hash_chain(self):
        result = self.run_pair()
        self.assertTrue(result["outcome"]["target_reached"])
        self.assertTrue(result["outcome"]["steps"] > 0)
        self.assertEqual(result["outcome"]["total_mass"], 1)
        self.assertFalse(result["physical_actuation"])
        self.assertFalse(result["genomic_output"])
        self.assertEqual(len(result["evidence"]["packet_sha256"]), 64)

    def test_replay_rejected(self):
        with self.assertRaisesRegex(SafetyStop, "Replay"):
            replay_dual_virtual_channels(self.a, self.b, self.target, last_frame=11)

    def test_uncertain_virtual_sensor_rejected(self):
        self.a["uncertainty"] = 0.2
        with self.assertRaisesRegex(SafetyStop, "uncertainty"):
            self.run_pair()

    def test_diverging_sensor_rejected(self):
        self.b["values"] = [0.5, 0.5, 0, 0, 0, 0]
        with self.assertRaisesRegex(SafetyStop, "disagreement"):
            self.run_pair()

    def test_untrusted_real_device_mode_rejected(self):
        self.a["mode"] = "PHYSICAL_ACTUATION"
        with self.assertRaisesRegex(SafetyStop, "Real actuator"):
            self.run_pair()

    def test_extended_with_genome_or_actuator_command_rejected(self):
        self.a["genetic_sequence"] = "not-a-sequence"
        with self.assertRaisesRegex(SafetyStop, "unknown commands"):
            self.run_pair()

    def test_mismatched_run_blocked(self):
        self.b["run_id"] = "SIM-20261009-X2"
        with self.assertRaisesRegex(SafetyStop, "Desynchronized"):
            self.run_pair()

    def test_duplicate_channel_blocked(self):
        self.b["channel"] = "SIM_A"
        with self.assertRaisesRegex(SafetyStop, "duplicate"):
            self.run_pair()

    def test_invalid_numeric_packet_blocked(self):
        self.a["values"] = [math.nan, 0, 0, 0, 0, 0]
        with self.assertRaisesRegex(SafetyStop, "invalid"):
            self.run_pair()

    def test_mass_conservation_mismatch_blocked(self):
        self.target = [0, 0, 0, 0, 0, 0.9]
        with self.assertRaisesRegex(SafetyStop, "conservation"):
            self.run_pair()

    def test_stuck_controller_hard_stops(self):
        self.target = [0, 0, 0, 0, 0, 1]
        with self.assertRaisesRegex(SafetyStop, "limit"):
            replay_dual_virtual_channels(self.a, self.b, self.target, max_transfer=0.25)

    def test_premature_true_quality_rejected(self):
        self.a["quality"] = 1  # bool must be explicit, not integer
        with self.assertRaisesRegex(SafetyStop, "low-quality"):
            self.run_pair()

    def test_cli_replay_generates_inert_json(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "telemetry.json"
            dest = Path(td) / "report.json"
            source.write_text(json.dumps({"packet_a": self.a, "packet_b": self.b, "target": self.target}))
            script = Path(__file__).resolve().parents[1] / "sim_telemetry_adapter.py"
            r = subprocess.run([sys.executable, str(script), "--input", str(source), "--output", str(dest)], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
            data = json.loads(dest.read_text())
            self.assertFalse(data["physical_actuation"])
            self.assertFalse(data["biological_validation"])

    def test_cli_invalid_payload_never_writes_report(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "malicious.json"
            dest = Path(td) / "report.json"
            source.write_text(json.dumps({"packet_a": self.a, "packet_b": self.b, "target": self.target, "device_command": "ACTUATE"}))
            script = Path(__file__).resolve().parents[1] / "sim_telemetry_adapter.py"
            r = subprocess.run([sys.executable, str(script), "--input", str(source), "--output", str(dest)], capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 2)
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()