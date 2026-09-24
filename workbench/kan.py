"""Small compositional KAN, independent stdlib implementation; synthetic data only."""
import math
import random
import statistics


def _number(title, default, low, high, integer=False):
    return dict(title=title, type='integer' if integer else 'number', default=default,
                minimum=low, maximum=high)


SPEC = {
    'id': 'kan_benchmark', 'name': 'KAN: обучаемые функции на рёбрах', 'version': '0.8.0',
    'description': 'Композиционная KAN с тремя вариантами базиса; сравнение с MLP и линейной регрессией на синтетических данных.',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'basis': {'title': 'Базис рёбер', 'type': 'string', 'enum': ['spline', 'rbf', 'wavelet'], 'default': 'spline', 'maxLength': 12},
        'task': {'title': 'Синтетическая зависимость', 'type': 'string', 'enum': ['additive', 'interaction'], 'default': 'interaction', 'maxLength': 16},
        'hidden_width': _number('Скрытые узлы KAN', 3, 2, 4, True),
        'knots': _number('Узлы фиксированной сетки', 6, 4, 8, True),
        'steps': _number('Обновления Adam', 140, 40, 180, True),
        'train_samples': _number('Обучающие примеры', 96, 64, 128, True),
        'replicates': _number('Последовательные seed', 3, 1, 3, True),
        'learning_rate': _number('Скорость обучения KAN и MLP', 0.02, 0.003, 0.04),
        'noise': _number('Шум только обучающих ответов', 0.03, 0, 0.15),
        'seed': _number('Начальный seed', 42, 0, 2147483647, True)}},
    'limitations': [
        'Самостоятельная малая реализация; не исполнение оригинальных pykan, FastKAN или Wav-KAN.',
        'Spline: степень 1. RBF и Mexican-hat: фиксированные центры и масштабы, обучаются коэффициенты и линейная часть каждого ребра.',
        'Близкое число параметров и одинаковые данные/обновления не означают равные FLOPs или оптимальную настройку обеих архитектур.',
        'Две синтетические функции и несколько seed не устанавливают универсального преимущества KAN, биологическую точность или свойства мозга.']}


def validate(parameters):
    """Also guard direct users; the plugin registry supplies the same contract."""
    if not isinstance(parameters, dict):
        raise ValueError('Parameters must be an object')
    props = SPEC['parameters']['properties']
    if set(parameters) - set(props):
        raise ValueError('Unknown KAN parameter')
    out = {}
    for key, spec in props.items():
        value = parameters.get(key, spec['default'])
        if spec['type'] == 'string':
            if value not in spec['enum']:
                raise ValueError('Invalid ' + key)
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
                raise ValueError('Invalid ' + key)
            if spec['type'] == 'integer' and not isinstance(value, int):
                raise ValueError('Integer required: ' + key)
            if not spec['minimum'] <= value <= spec['maximum']:
                raise ValueError('Out of range: ' + key)
        out[key] = value
    return out


def basis_features(x, count, kind, bound):
    """Return values and analytic x derivatives, including a linear residual."""
    width = 2.0 * bound / (count - 1)
    values, derivatives = [x], [1.0]
    if kind == 'spline':
        # Cardinal degree-1 B-splines, clamped to the endpoint values outside.
        position = (x + bound) / width
        weights, slopes = [0.0] * count, [0.0] * count
        if position <= 0:
            weights[0] = 1.0
        elif position >= count - 1:
            weights[-1] = 1.0
        else:
            left = int(position)
            weights[left], weights[left + 1] = 1 - (position - left), position - left
            slopes[left], slopes[left + 1] = -1 / width, 1 / width
        return values + weights, derivatives + slopes
    for i in range(count):
        z = (x - (-bound + i * width)) / width
        if kind == 'rbf':
            b = math.exp(-z * z)
            d = -2 * z * b / width
        elif kind == 'wavelet':
            e = math.exp(-0.5 * z * z)
            b, d = (1 - z * z) * e, (z * z * z - 3 * z) * e / width
        else:
            raise ValueError('Unknown basis')
        values.append(b)
        derivatives.append(d)
    return values, derivatives


class KAN:
    """Two KAN layers. Every edge has a trainable scalar function."""
    def __init__(self, width=3, knots=6, basis='spline', seed=42):
        self.width, self.knots, self.basis = width, knots, basis
        self.stride = knots + 1
        self.bias_offset = 3 * width * self.stride
        rng = random.Random(seed)
        self.weights = []
        for edge in range(3 * width):
            self.weights.append(rng.uniform(-0.65, 0.65))
            self.weights.extend(rng.gauss(0, 0.025) for _ in range(knots))
        self.weights.extend([0.0] * (width + 1))

    def _edge(self, edge, features):
        offset = edge * self.stride
        return sum(self.weights[offset + i] * value for i, value in enumerate(features))

    def predict_gradient(self, inputs, gradient=False):
        features = [basis_features(x, self.knots, self.basis, 1.25)[0] for x in inputs]
        hidden = [self.weights[self.bias_offset + j] + self._edge(2 * j, features[0]) +
                  self._edge(2 * j + 1, features[1]) for j in range(self.width)]
        output = self.weights[-1]
        grad = [0.0] * len(self.weights) if gradient else None
        for j, value in enumerate(hidden):
            outer, derivative = basis_features(value, self.knots, self.basis, 2.0)
            edge = 2 * self.width + j
            output += self._edge(edge, outer)
            if gradient:
                offset = edge * self.stride
                grad[offset:offset + self.stride] = outer
                upstream = self._edge(edge, derivative)
                grad[self.bias_offset + j] = upstream
                for input_id in range(2):
                    offset = (2 * j + input_id) * self.stride
                    for k, f in enumerate(features[input_id]):
                        grad[offset + k] = upstream * f
        if gradient:
            grad[-1] = 1.0
        return output, grad

    def predict(self, inputs):
        return self.predict_gradient(inputs)[0]

    def edge_functions(self):
        rows = []
        for edge in range(3 * self.width):
            first = edge < 2 * self.width
            source = 'x' + str(edge % 2) if first else 'h' + str(edge - 2 * self.width)
            target = 'h' + str(edge // 2) if first else 'y'
            bound = 1.25 if first else 2.0
            points = []
            for k in range(33):
                x = -bound + 2 * bound * k / 32
                features, _ = basis_features(x, self.knots, self.basis, bound)
                points.append({'x': x, 'y': self._edge(edge, features)})
            offset = edge * self.stride
            rows.append({'source': source, 'target': target, 'layer': 0 if first else 1,
                         'basis': self.basis, 'coefficients': self.weights[offset:offset + self.stride],
                         'grid_domain': [-bound, bound], 'points': points})
        return rows


class MLP:
    def __init__(self, width, seed):
        self.width = width
        rng = random.Random(seed)
        self.weights = ([rng.uniform(-1, 1) for _ in range(2 * width)] +
                        [rng.uniform(-0.2, 0.2) for _ in range(width)] +
                        [rng.uniform(-0.3, 0.3) for _ in range(width)] + [0.0])

    def predict_gradient(self, x, gradient=False):
        h, w = self.width, self.weights
        activations = [math.tanh(w[2 * i] * x[0] + w[2 * i + 1] * x[1] + w[2 * h + i]) for i in range(h)]
        output = w[-1] + sum(w[3 * h + i] * a for i, a in enumerate(activations))
        grad = None
        if gradient:
            grad = [0.0] * len(w)
            grad[-1] = 1.0
            for i, a in enumerate(activations):
                upstream = w[3 * h + i] * (1 - a * a)
                grad[2 * i], grad[2 * i + 1] = upstream * x[0], upstream * x[1]
                grad[2 * h + i], grad[3 * h + i] = upstream, a
        return output, grad

    def predict(self, x):
        return self.predict_gradient(x)[0]


def target(x, task):
    a, b = x
    if task == 'additive':
        return math.sin(math.pi * a) + 0.5 * math.cos(math.pi * b)
    if task == 'interaction':
        return math.sin(math.pi * a * b) + 0.2 * math.cos(math.pi * a)
    raise ValueError('Unknown task')


def make_data(seed, size, noise, task):
    rng = random.Random(seed)
    return [((x := [rng.uniform(-1, 1), rng.uniform(-1, 1)]), target(x, task) + rng.gauss(0, noise)) for _ in range(size)]


def mse(model, samples):
    return statistics.fmean((model.predict(x) - y) ** 2 for x, y in samples)


def train(model, samples, batches, rate):
    size = len(model.weights)
    first, second = [0.0] * size, [0.0] * size
    trace = [{'x': 0, 'y': mse(model, samples)}]
    for step, batch in enumerate(batches, 1):
        g = [0.0] * size
        for index in batch:
            x, y = samples[index]
            prediction, grad = model.predict_gradient(x, True)
            multiplier = 2 * (prediction - y) / len(batch)
            for i in range(size):
                g[i] += multiplier * grad[i]
        for i in range(size):
            # No test-dependent stopping or choice of checkpoint.
            gi = max(-10.0, min(10.0, g[i]))
            first[i], second[i] = 0.9 * first[i] + 0.1 * gi, 0.999 * second[i] + 0.001 * gi * gi
            model.weights[i] -= rate * (first[i] / (1 - 0.9 ** step)) / (math.sqrt(second[i] / (1 - 0.999 ** step)) + 1e-8)
        if step % max(1, len(batches) // 10) == 0 or step == len(batches):
            trace.append({'x': step, 'y': mse(model, samples)})
    return trace


class Linear:
    def __init__(self, samples):
        matrix = [[0.0] * 4 for _ in range(3)]
        for inputs, y in samples:
            x = [1.0] + list(inputs)
            for i in range(3):
                for j in range(3):
                    matrix[i][j] += x[i] * x[j]
                matrix[i][3] += x[i] * y
        for i in range(3):
            pivot = max(range(i, 3), key=lambda j: abs(matrix[j][i]))
            matrix[i], matrix[pivot] = matrix[pivot], matrix[i]
            divisor = matrix[i][i]
            if abs(divisor) < 1e-12:
                raise ValueError('Degenerate linear data')
            matrix[i] = [value / divisor for value in matrix[i]]
            for j in range(3):
                if j != i:
                    scale = matrix[j][i]
                    matrix[j] = [a - scale * b for a, b in zip(matrix[j], matrix[i])]
        self.weights = [matrix[i][3] for i in range(3)]

    def predict(self, x):
        return self.weights[0] + sum(w * v for w, v in zip(self.weights[1:], x))


def execute(parameters):
    p = validate(parameters)
    table, series, first_model, predictions = [], [], None, []
    for replicate in range(p['replicates']):
        seed = p['seed'] + replicate
        train_data = make_data(seed, p['train_samples'], p['noise'], p['task'])
        # Separate RNG stream and noiseless targets; test never reaches train().
        test_data = make_data(seed + 1000003, 80, 0.0, p['task'])
        rng = random.Random(seed + 2000003)
        batches = [[rng.randrange(len(train_data)) for _ in range(12)] for _ in range(p['steps'])]
        kan = KAN(p['hidden_width'], p['knots'], p['basis'], seed + 3000003)
        mlp = MLP(max(2, round((len(kan.weights) - 1) / 4)), seed + 4000003)
        for name, model in [('KAN', kan), ('MLP', mlp), ('Linear', Linear(train_data))]:
            initial = mse(model, train_data)
            trace = [] if name == 'Linear' else train(model, train_data, batches, p['learning_rate'])
            table.append({'seed': seed, 'model': name, 'basis': p['basis'] if name == 'KAN' else 'tanh' if name == 'MLP' else 'linear',
                          'parameters': len(model.weights), 'train_mse_initial': initial,
                          'train_mse': mse(model, train_data), 'test_mse': mse(model, test_data),
                          'updates': p['steps'] if name != 'Linear' else 0})
            if replicate == 0 and trace:
                series.append({'name': name + ': train MSE, первый seed', 'points': trace})
        if replicate == 0:
            first_model = kan
            predictions = [{'x0': x[0], 'x1': x[1], 'target': y, 'kan': kan.predict(x), 'mlp': mlp.predict(x)} for x, y in test_data]
    metrics = []
    for name in ['KAN', 'MLP', 'Linear']:
        values = [row['test_mse'] for row in table if row['model'] == name]
        metrics += [{'label': name + ': средняя test MSE', 'value': statistics.fmean(values)},
                    {'label': name + ': SD по seed', 'value': statistics.stdev(values) if len(values) > 1 else 0.0}]
    return {'summary': 'Обучены функции всех рёбер двухслойной KAN. Итоговые модели проверены на отдельной синтетической выборке без подбора по test. Низкая test MSE лучше; преимущество не предполагается заранее.',
            'parameters': p, 'limitations': list(SPEC['limitations']), 'table': table, 'metrics': metrics, 'series': series,
            'model': {'type': 'compositional_univariate_edge_network', 'architecture': [2, p['hidden_width'], 1],
                      'basis': p['basis'], 'grid_adaptation': False, 'trainable_centres_and_scales': False,
                      'all_edge_functions_trained': True, 'edge_functions': first_model.edge_functions(),
                      'node_biases': first_model.weights[first_model.bias_offset:],
                      'visualized_replicate_seed': p['seed'], 'test_predictions': predictions},
            'validation': {'test_used_for_fitting': False, 'test_used_for_hyperparameter_selection': False,
                           'early_stopping': False, 'train_samples_per_seed': p['train_samples'], 'test_samples_per_seed': 80,
                           'test_targets': 'noiseless', 'paired_data_and_batches': True, 'equal_flops_claimed': False,
                           'initialization': 'independent architecture-specific RNG streams, reset each seed',
                           'optimizer': 'Adam beta1=.9 beta2=.999 eps=1e-8; coordinate gradient clip [-10,10]',
                           'batch_size': 12, 'biological_validation': False}}
