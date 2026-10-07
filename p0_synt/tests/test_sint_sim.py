"""Regression tests for a purely simulated, fixed-volume Synt control loop."""
import math
import unittest

from sint_sim import SafetyStop, SyntVolume


class SyntSimulationTests(unittest.TestCase):
    def test_fixed_volume_converges(self):
        sim = SyntVolume([1, 0, 0, 0], [0, 0, 0, 1], 0.1)
        result = sim.run()
        self.assertTrue(result["target_reached"])
        self.assertEqual(result["fixed_volume_cells"], 4)
        self.assertEqual(result["mode"], "NO_PHYSICAL_ACTUATION")

    def test_conservation_and_limits(self):
        sim = SyntVolume([0.9, 0.1, 0, 0], [0, 0, 0.5, 0.5], 0.125)
        result = sim.run()
        self.assertAlmostEqual(sum(result["final_state"]), 1.0)
        self.assertTrue(all(0 <= x <= 1 for x in sim.state))
        for event in sim.events:
            for move in event["transitions"]:
                self.assertLessEqual(move["amount"], 0.125 + 1e-10)
                self.assertEqual(abs(move["from"] - move["to"]), 1)

    def test_sensor_failure_stops(self):
        sim = SyntVolume([1, 0], [0, 1])
        for bad in ([math.nan, 0], [2, -1], [0.5], [0.4, 0.4]):
            with self.assertRaises(SafetyStop):
                sim.estimate(bad)

    def test_invalid_target_rejected(self):
        with self.assertRaises(SafetyStop):
            SyntVolume([1, 0], [0, 0.5])
        with self.assertRaises(SafetyStop):
            SyntVolume([1, 0], [1.1, -0.1])
        with self.assertRaises(SafetyStop):
            SyntVolume([1, 0], [0, 1], 2)

    def test_event_chain_is_verifiable(self):
        sim = SyntVolume([1, 0, 0], [0, 0, 1], 0.2)
        sim.run()
        self.assertTrue(sim.verify_event_chain())
        sim.events[0]["state"][0] = 4.0
        self.assertFalse(sim.verify_event_chain())

    def test_bounded_nonconvergence_fails_closed(self):
        sim = SyntVolume([1, 0, 0], [0, 0, 1], 0.01)
        with self.assertRaises(SafetyStop):
            sim.run(max_steps=1)

    def test_no_external_actuator_interface(self):
        sim = SyntVolume([1, 0], [0, 1])
        self.assertFalse(hasattr(sim, "send_to_hardware"))
        self.assertFalse(hasattr(sim, "send_to_patient"))


if __name__ == "__main__":
    unittest.main()
