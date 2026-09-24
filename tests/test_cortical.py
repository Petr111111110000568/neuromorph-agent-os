import copy
import json
import math
import unittest
from unittest import mock

from workbench import cortical as model


class CorticalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parameters = model.validate({"train_episodes": 60, "test_episodes": 40, "replicates": 2})
        cls.result = model.execute(cls.parameters)

    def test_reproducible_and_controls_expose_task_order(self):
        self.assertEqual(self.result, model.execute(self.parameters))
        rows = {row["condition"]: row for row in self.result["aggregates"]}
        self.assertEqual(rows["context_cells"]["ambiguous_accuracy"], 1.)
        self.assertEqual(rows["markov_1"]["ambiguous_accuracy"], .5)
        self.assertEqual(rows["markov_2"]["ambiguous_accuracy"], 1.)
        self.assertAlmostEqual(rows["context_cells"]["cross_entropy_bits"], rows["markov_2"]["cross_entropy_bits"])
        self.assertEqual({row["seed"] for row in self.result["table"]}, {42, 43})

    def test_one_cell_capacity_ablation_loses_context(self):
        result = model.execute({**self.parameters, "cells_per_column": 1})
        row = next(row for row in result["aggregates"] if row["condition"] == "context_cells")
        self.assertEqual(row["ambiguous_accuracy"], .5)
        trial = self.result["trials"][0]
        # The same B symbol recruits different cells after the two origins.
        training = model.make_episodes(42, "train", 60, 0.)
        network, _ = model.train(self.parameters, training)
        a, _ = network.step("A", frozenset())
        x, _ = network.step("X", frozenset())
        ba, _ = network.step("B", a)
        bx, _ = network.step("B", x)
        self.assertTrue(ba.isdisjoint(bx))
        self.assertGreater(len(trial["state"]["synapses"]), 0)

    def test_test_targets_do_not_change_training_or_connections(self):
        ordinary = model.run_trial(self.parameters, 42)
        def flip(episodes):
            episodes = copy.deepcopy(episodes)
            for episode in episodes:
                tokens = episode["tokens"]
                tokens[2] = "D" if tokens[2] == "C" else "C"
            return episodes
        altered = model.run_trial(self.parameters, 42, test_transform=flip)
        self.assertEqual(ordinary["trained_state_hash"], altered["trained_state_hash"])
        self.assertEqual(ordinary["training_hash"], altered["training_hash"])
        self.assertEqual(ordinary["state"], altered["state"])
        self.assertNotEqual(ordinary["testing_hash"], altered["testing_hash"])
        first = next(row for row in ordinary["rows"] if row["condition"] == "context_cells")
        second = next(row for row in altered["rows"] if row["condition"] == "context_cells")
        self.assertEqual(first["ambiguous_accuracy"], 1.)
        self.assertEqual(second["ambiguous_accuracy"], 0.)
        with mock.patch.object(model, "evaluate", side_effect=AssertionError("Trainer reached evaluation")):
            model.train(self.parameters, model.make_episodes(42, "train", 60, 0.))

    def test_evaluation_freezes_network_and_baseline_counts(self):
        training = model.make_episodes(42, "train", 60, .1)
        testing = model.make_episodes(42, "test", 40, .1)
        network, baselines = model.train(self.parameters, training)
        before_network = network.snapshot()
        before_counts = {name: copy.deepcopy(baseline.counts) for name, baseline in baselines.items()}
        rows, _ = model.evaluate(network, baselines, testing)
        self.assertEqual(before_network, network.snapshot())
        for name, baseline in baselines.items():
            self.assertEqual(before_counts[name], baseline.counts)
        self.assertTrue(all(row["predictions"] == 120 for row in rows))
        self.assertFalse({e["session"] for e in training} & {e["session"] for e in testing})

    def test_actual_sparse_activity_synapses_and_probability_bounds(self):
        json.dumps(self.result, allow_nan=False)
        for trial in self.result["trials"]:
            state = trial["state"]
            self.assertEqual(len(state["nodes"]), 18 * self.parameters["cells_per_column"])
            incoming = {}
            ids = {node["id"] for node in state["nodes"]}
            for edge in state["synapses"]:
                self.assertIn(edge["source"], ids)
                self.assertIn(edge["target"], ids)
                self.assertGreaterEqual(edge["permanence"], .5)
                self.assertLessEqual(edge["permanence"], 1.)
                incoming.setdefault(edge["target"], set()).add(edge["source"])
            self.assertTrue(all(len(sources) <= 6 for sources in incoming.values()))
            for step in trial["activity"]:
                self.assertEqual(len(step["active_cells"]), 3)
                self.assertAlmostEqual(sum(step["next_probabilities"].values()), 1.)
                self.assertTrue(all(0. < value < 1. for value in step["next_probabilities"].values()))
            self.assertGreater(trial["costs"]["training_synapse_checks"], 0)
            self.assertGreater(trial["costs"]["evaluation_synapse_checks"], 0)
        self.assertFalse(self.result["model"]["full_brain_model"])
        self.assertFalse(self.result["model"]["htm_reference_implementation"])

    def test_noisy_labels_report_all_seeds_without_selecting_winners(self):
        result = model.execute({**self.parameters, "label_noise": .25})
        self.assertEqual(len(result["table"]), 8)
        self.assertFalse(result["validation"]["hyperparameters_tuned_on_test"])
        for row in result["table"]:
            for field in ("accuracy", "ambiguous_accuracy"):
                self.assertGreaterEqual(row[field], 0.)
                self.assertLessEqual(row[field], 1.)
            self.assertTrue(math.isfinite(row["cross_entropy_bits"]))

    def test_direct_entrypoint_rejects_invalid_or_unbounded_input(self):
        for parameters in ({"cells_per_column": 0}, {"cells_per_column": 7},
                           {"train_episodes": 401}, {"test_episodes": 301},
                           {"replicates": 6}, {"label_noise": math.nan},
                           {"label_noise": math.inf}, {"seed": True}, {"seed": 2.0},
                           {"test_feedback": 1}, None, []):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                model.execute(parameters)


if __name__ == "__main__":
    unittest.main()
