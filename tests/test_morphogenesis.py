import copy
import json
import math
import unittest
from unittest import mock

from workbench import morphogenesis as model
from workbench.plugins import execute, list_plugins, validate


class MorphogenesisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parameters = validate("structural_plasticity", {"steps_per_phase": 60, "replicates": 2, "seed": 42})
        cls.result = execute("structural_plasticity", cls.parameters)

    def test_reproducible_and_all_seeds_conditions_reported(self):
        self.assertEqual(self.result, execute("structural_plasticity", self.parameters))
        self.assertEqual({row["seed"] for row in self.result["table"]}, {42, 43})
        self.assertEqual(len(self.result["table"]), 6)
        for trial in self.result["trials"]:
            self.assertEqual({c["condition"] for c in trial["conditions"]}, set(model.CONDITIONS))
            self.assertEqual(len({c["initial_state_hash"] for c in trial["conditions"]}), 1)
            self.assertEqual(len({c["training_batches_hash"] for c in trial["conditions"]}), 1)

    def test_same_budget_and_actual_topology_events(self):
        budget = self.parameters["edge_budget"]
        def mask(edges):
            return {int(edge["target"][1:]) * 6 + int(edge["source"][1:])
                    for edge in edges if edge["source"].startswith("x")}
        for trial in self.result["trials"]:
            active_costs = set()
            for condition in trial["conditions"]:
                current = mask(condition["initial_edges"])
                self.assertEqual(len(current), budget)
                self.assertEqual(len(condition["final_edges"]), budget + 6)
                for event in condition["events"]:
                    self.assertIn(event["removed_index"], current)
                    self.assertNotIn(event["grown_index"], current)
                    current.remove(event["removed_index"])
                    current.add(event["grown_index"])
                    self.assertEqual(len(current), budget)
                    self.assertEqual(event["active_sparse_edges_after"], budget)
                self.assertEqual(current, mask(condition["final_edges"]))
                if condition["condition"] == "fixed":
                    self.assertFalse(condition["events"])
                    self.assertEqual(mask(condition["initial_edges"]), current)
                else:
                    self.assertTrue(condition["events"])
                    self.assertNotEqual(mask(condition["initial_edges"]), current)
                active_costs.add(condition["costs"]["cumulative_active_edge_visits"])
            self.assertEqual(len(active_costs), 1)

    def test_heldout_targets_cannot_influence_weights_topology_or_schedule(self):
        ordinary = model.run_trial(self.parameters, 42)
        changed_test = model.run_trial(self.parameters, 42,
            test_transform=lambda rows: [(x, y + 10.) for x, y in rows])
        for original, changed in zip(ordinary["conditions"], changed_test["conditions"]):
            self.assertEqual(original["final_state_hash"], changed["final_state_hash"])
            self.assertEqual(original["final_edges"], changed["final_edges"])
            self.assertEqual(original["events"], changed["events"])
            self.assertEqual(original["costs"], changed["costs"])
            self.assertEqual(original["training_batches_hash"], changed["training_batches_hash"])
            self.assertNotEqual(original["metrics"]["new_mse_after_shift"], changed["metrics"]["new_mse_after_shift"])
        problem = model.make_problem(42, 60)
        self.assertEqual(problem["train_test_feature_overlap"], 0)
        with mock.patch.object(model, "evaluate", side_effect=AssertionError("Training called held-out evaluation")):
            model.train_condition(self.parameters, "guided", model.initial_state(42, 18), problem["batches"], 42)

    def test_costs_include_growth_evaluation_and_finite_metrics(self):
        json.dumps(self.result, allow_nan=False)
        for trial in self.result["trials"]:
            for condition in trial["conditions"]:
                costs = condition["costs"]
                self.assertGreater(costs["evaluation_edge_visits"], 0)
                self.assertEqual(costs["total_counted_visits"], costs["cumulative_active_edge_visits"] +
                                 costs["candidate_gradient_visits"] + costs["structural_selection_visits"] + costs["evaluation_edge_visits"])
                if condition["condition"] == "guided":
                    self.assertGreater(costs["candidate_gradient_visits"], 0)
                    self.assertEqual(costs["structural_selection_visits"], len(condition["events"]) * 72)
                elif condition["condition"] == "random":
                    self.assertEqual(costs["candidate_gradient_visits"], 0)
                    self.assertEqual(costs["structural_selection_visits"], len(condition["events"]) * (36 + self.parameters["edge_budget"]))
                else:
                    self.assertEqual(costs["candidate_gradient_visits"], 0)
                    self.assertEqual(costs["structural_selection_visits"], 0)
                for value in condition["metrics"].values():
                    self.assertTrue(math.isfinite(value))
                m = condition["metrics"]
                self.assertAlmostEqual(m["forgetting_delta"], m["old_mse_after_shift"] - m["old_mse_after_phase1"])

    def test_evaluation_read_only_and_version_limits(self):
        state = model.initial_state(42, 18)
        original = copy.deepcopy(state)
        model.evaluate(state, model.make_problem(42, 60)["old_test"])
        self.assertEqual(state, original)
        spec = next(p for p in list_plugins() if p["id"] == "structural_plasticity")
        self.assertEqual(spec["version"], "0.7.0")
        self.assertFalse(self.result["model"]["savelyev_algorithm"])
        self.assertFalse(self.result["model"]["biological_model"])
        for invalid in ({"replicates": 6}, {"steps_per_phase": 181}, {"edge_budget": 36},
                        {"learning_rate": float("nan")}, {"seed": True}, {"test_feedback": 1}):
            with self.assertRaises(ValueError):
                validate("structural_plasticity", invalid)


if __name__ == "__main__":
    unittest.main()
