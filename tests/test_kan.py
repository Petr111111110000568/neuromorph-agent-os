import json
import math
import unittest
from unittest.mock import patch

from workbench.kan import KAN, MLP, SPEC, basis_features, execute, make_data, train, validate


class KANTests(unittest.TestCase):
    def test_compositional_parameter_gradients_finite_difference(self):
        # Includes every coefficient and bias of BOTH layers, away from knots.
        for basis in ['spline', 'rbf', 'wavelet']:
            model = KAN(width=2, knots=5, basis=basis, seed=23)
            x, epsilon = [0.231, -0.347], 1e-6
            _, analytic = model.predict_gradient(x, True)
            for i, weight in enumerate(model.weights):
                model.weights[i] = weight + epsilon
                high = model.predict(x)
                model.weights[i] = weight - epsilon
                low = model.predict(x)
                model.weights[i] = weight
                self.assertAlmostEqual(analytic[i], (high - low) / (2 * epsilon), places=6,
                                       msg=f'{basis} parameter {i}')

    def test_basis_input_derivatives(self):
        for basis in ['spline', 'rbf', 'wavelet']:
            for x in [-2.2, -0.217, 0.113, 2.3]:
                values, derivatives = basis_features(x, 6, basis, 1.25)
                plus = basis_features(x + 1e-6, 6, basis, 1.25)[0]
                minus = basis_features(x - 1e-6, 6, basis, 1.25)[0]
                for d, a, b in zip(derivatives, plus, minus):
                    self.assertAlmostEqual(d, (a - b) / 2e-6, places=6)
                self.assertTrue(all(math.isfinite(v) for v in values))

    def test_mlp_gradient_baseline(self):
        model = MLP(4, 42)
        x = [0.23, -0.21]
        _, analytic = model.predict_gradient(x, True)
        for i, w in enumerate(model.weights):
            model.weights[i] = w + 1e-6
            high = model.predict(x)
            model.weights[i] = w - 1e-6
            low = model.predict(x)
            model.weights[i] = w
            self.assertAlmostEqual(analytic[i], (high - low) / 2e-6, places=6)

    def test_train_changes_both_layers_without_test_data(self):
        data = make_data(5, 64, 0, 'additive')
        for basis in ['spline', 'rbf', 'wavelet']:
            model = KAN(basis=basis)
            initial = model.weights[:]
            trace = train(model, data, [[i % 64 for i in range(step, step + 12)] for step in range(60)], 0.02)
            midpoint = 2 * model.width * model.stride
            self.assertNotEqual(initial[:midpoint], model.weights[:midpoint])
            self.assertNotEqual(initial[midpoint:model.bias_offset], model.weights[midpoint:model.bias_offset])
            self.assertLess(trace[-1]['y'], trace[0]['y'])

    def test_reproducibility_and_blind_holdout_contract(self):
        args = {'steps': 40, 'replicates': 1, 'seed': 7, 'basis': 'rbf'}
        a, b = execute(args), execute(args)
        self.assertEqual(a, b)
        self.assertFalse(a['validation']['test_used_for_fitting'])
        self.assertFalse(a['validation']['test_used_for_hyperparameter_selection'])
        self.assertEqual(len(a['model']['edge_functions']), 9)
        counts = {row['model']: row['parameters'] for row in a['table']}
        self.assertLessEqual(abs(counts['KAN'] - counts['MLP']), 2)
        json.dumps(a, allow_nan=False)

    def test_all_variants_finite_and_exported_curves_match(self):
        for basis in ['spline', 'rbf', 'wavelet']:
            result = execute({'steps': 40, 'replicates': 1, 'basis': basis})
            json.dumps(result, allow_nan=False)
            edge = result['model']['edge_functions'][0]
            for point in edge['points']:
                features, _ = basis_features(point['x'], 6, basis, 1.25)
                self.assertAlmostEqual(point['y'], sum(c * f for c, f in zip(edge['coefficients'], features)))

    def test_changing_holdout_labels_does_not_change_learned_model(self):
        args = {'steps': 40, 'replicates': 1, 'basis': 'wavelet', 'seed': 7}
        baseline = execute(args)
        def changed_test(seed, size, noise, task):
            data = make_data(seed, size, noise, task)
            return [(x, y + 100) for x, y in data] if seed == 1000010 else data
        with patch('workbench.kan.make_data', side_effect=changed_test):
            altered = execute(args)
        self.assertEqual(baseline['model']['edge_functions'], altered['model']['edge_functions'])
        self.assertEqual(baseline['model']['node_biases'], altered['model']['node_biases'])
        self.assertEqual(baseline['series'], altered['series'])
        self.assertNotEqual(baseline['table'][0]['test_mse'], altered['table'][0]['test_mse'])

    def test_input_bounds_and_unknowns(self):
        for value in [{'replicates': 4}, {'steps': 181}, {'basis': 'original_pykan'}, {'noise': float('nan')},
                      {'hidden_width': True}, {'knots': 6.0}, {'unknown': 1}, {'basis': []}]:
            with self.assertRaises(ValueError):
                validate(value)


if __name__ == '__main__':
    unittest.main()
