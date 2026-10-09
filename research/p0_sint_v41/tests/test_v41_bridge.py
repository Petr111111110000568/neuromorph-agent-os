import unittest
from integration.bridge_v41 import bridge_scenario
from geometry_v1.sint_sim.core import SCENARIOS

class TestBridge(unittest.TestCase):
    def test_joint_nominal_stays_monitoring_and_nonphysical(self):
        v = bridge_scenario('nominal')
        self.assertIsNone(v['v40_first_hold'])
        self.assertIsNone(v['geometry_first_stop'])
        self.assertTrue(all(x['v40_decision']=='SIMULATED_MONITOR_OK' for x in v['outcomes']))
        self.assertFalse(v['real_world_actuation_enabled'])
    def test_each_fault_closes_both_gates(self):
        for scenario in SCENARIOS:
            if scenario == 'nominal': continue
            with self.subTest(scenario=scenario):
                v = bridge_scenario(scenario)
                self.assertEqual(v['geometry_first_stop'],18)
                self.assertEqual(v['v40_first_hold'],18)
                self.assertTrue(v['both_halted_after_fault'])
    def test_no_real_world_permission(self):
        for scenario in SCENARIOS:
            v = bridge_scenario(scenario)
            self.assertFalse(v['live_animal_approved'])
            self.assertFalse(v['real_world_model_calibrated'])
            self.assertTrue(all(not x['physical_release'] for x in v['outcomes']))
    def test_both_journals_valid(self):
        for scenario in SCENARIOS:
            v = bridge_scenario(scenario)
            self.assertTrue(v['v40_audit_valid'])
            self.assertTrue(v['geometry_audit_valid'])
    def test_invariant_volume(self):
        for scenario in SCENARIOS:
            self.assertAlmostEqual(bridge_scenario(scenario)['geometry_area_ratio'],1.0, 10)

if __name__ == '__main__': unittest.main()
