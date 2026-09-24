"""A small context-cell network inspired by cortical temporal-memory motifs.

Independent implementation: not HTM, CORNN/OCAS, Monty, or a brain simulator.
Symbol columns are fixed; dendritic connections are learned from training
episodes only. A count-calibrated readout is an explicit engineering choice.
"""
from collections import Counter, defaultdict
import hashlib
import json
import math
import random
import statistics

VERSION = "context-cell-network/1"
SYMBOLS = ("A", "X", "B", "C", "D", "Z")
WIDTH = 3
THRESHOLD = 2
MAX_SYNAPSES = 6


def _parameter(title, default, low, high, integer=True):
    return {"title": title, "type": "integer" if integer else "number",
            "default": default, "minimum": low, "maximum": high}


SPEC = {
    "id": "cortical_sequence", "version": "0.8.0",
    "name": "Контекстные клетки: последовательности",
    "description": "Обучаемые дендритные связи и конкуренция клеток в колонках; сравнение с моделями Маркова на синтетической грамматике.",
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "train_episodes": _parameter("Эпизоды обучения", 160, 40, 400),
        "test_episodes": _parameter("Независимые тестовые эпизоды", 120, 40, 300),
        "cells_per_column": _parameter("Клетки на колонку", 4, 1, 6),
        "label_noise": _parameter("Вероятность смены C/D", 0., 0., .25, False),
        "replicates": _parameter("Последовательные seed", 3, 1, 5),
        "seed": _parameter("Начальный seed", 42, 0, 2147483647)}},
    "limitations": [
        "Самостоятельная упрощённая модель контекстных клеток; не реализация HTM, ОКАС/CORNN или Monty.",
        "Фиксированные разреженные коды символов, один сегмент на клетку и частотный выход — инженерные допущения.",
        "Train/test — независимые эпизоды одной грамматики; это не перенос на неизвестную грамматику.",
        "Модель второго порядка достаточна для этой задачи; преимущество нейросетевой архитектуры не предполагается.",
        "Нет биологического моделирования, человеческого сознания, органоидов или прогноза изменений организма."]}


def validate(parameters):
    if not isinstance(parameters, dict):
        raise ValueError("Параметры должны быть объектом")
    properties = SPEC["parameters"]["properties"]
    if set(parameters) - set(properties):
        raise ValueError("Неизвестный параметр")
    result = {}
    for key, rule in properties.items():
        value = parameters.get(key, rule["default"])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError("Некорректный числовой параметр")
        if rule["type"] == "integer" and not isinstance(value, int):
            raise ValueError("Требуется целое число")
        if not rule["minimum"] <= value <= rule["maximum"]:
            raise ValueError("Параметр за пределами диапазона")
        result[key] = value
    return result


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def make_episodes(seed, split, count, noise):
    """Balanced origins with split-specific independent order/noise RNGs."""
    rng = random.Random(f"{VERSION}:{seed}:{split}")
    origins = ["A" if i % 2 == 0 else "X" for i in range(count)]
    rng.shuffle(origins)
    result = []
    for index, origin in enumerate(origins):
        target = "C" if origin == "A" else "D"
        if rng.random() < noise:
            target = "D" if target == "C" else "C"
        result.append({"session": f"{split}-{index}", "tokens": [origin, "B", target, "Z"]})
    return result


class ContextCells:
    """Fixed proximal encodings; bounded learned distal synaptic graph.

    A segment is activated by >=2 connected active presynaptic cells.
    On a column burst, its least-used cell wins. Training adds/reinforces
    distal synapses. At capacity, source sets can merge and lose context.
    This intentionally small mechanism is not the published HTM algorithm.
    """
    def __init__(self, cells_per_column):
        if isinstance(cells_per_column, bool) or not isinstance(cells_per_column, int) or not 1 <= cells_per_column <= 6:
            raise ValueError("cells_per_column must be an integer between 1 and 6")
        self.cells_per_column = cells_per_column
        self.columns = {symbol: list(range(index * WIDTH, (index + 1) * WIDTH))
                        for index, symbol in enumerate(SYMBOLS)}
        self.cells = [{"id": index, "column": index // cells_per_column,
                       "synapses": {}, "uses": 0}
                      for index in range(len(SYMBOLS) * WIDTH * cells_per_column)]
        self.synapse_checks = 0

    def _matches(self, cell, active):
        self.synapse_checks += len(cell["synapses"])
        return sum(source in active and permanence >= .5
                   for source, permanence in cell["synapses"].items()) >= THRESHOLD

    def predicted_cells(self, active):
        return [cell["id"] for cell in self.cells if self._matches(cell, active)]

    def step(self, symbol, previous, learn=False):
        if symbol not in self.columns:
            raise ValueError("Unknown input symbol")
        predictions = set(self.predicted_cells(previous)) if previous else set()
        winners, burst_columns = [], []
        for column in self.columns[symbol]:
            group = self.cells[column * self.cells_per_column:(column + 1) * self.cells_per_column]
            predicted = [cell for cell in group if cell["id"] in predictions]
            if not previous:
                winner = group[0]  # Explicit shared reset state, not learned from test.
            elif predicted:
                winner = max(predicted, key=lambda cell: (cell["uses"], -cell["id"]))
            else:
                burst_columns.append(column)
                winner = min(group, key=lambda cell: (cell["uses"], cell["id"]))
            winners.append(winner["id"])
            if learn and previous:
                synapses = winner["synapses"]
                for source in sorted(previous):
                    if source in synapses:
                        synapses[source] = min(1., synapses[source] + .05)
                    elif len(synapses) < MAX_SYNAPSES:
                        synapses[source] = .6
                winner["uses"] += 1
        return frozenset(winners), {"symbol": symbol, "active_cells": winners,
                                   "predicted_before_input": sorted(predictions),
                                   "burst_columns": burst_columns}

    def probabilities(self, active):
        predictions = set(self.predicted_cells(active))
        scores = {}
        for symbol, columns in self.columns.items():
            # Frequency-calibrated readout, not a biological firing equation.
            scores[symbol] = sum(cell["uses"] for cell in self.cells
                                 if cell["id"] in predictions and cell["column"] in columns) / WIDTH
        return _normalize(scores), sorted(predictions)

    def snapshot(self):
        nodes, edges = [], []
        for cell in self.cells:
            symbol = SYMBOLS[cell["column"] // WIDTH]
            nodes.append({"id": cell["id"], "column": cell["column"],
                          "symbol": symbol, "uses": cell["uses"]})
            edges.extend({"source": source, "target": cell["id"], "permanence": permanence}
                         for source, permanence in sorted(cell["synapses"].items()))
        return {"nodes": nodes, "synapses": edges, "columns": self.columns,
                "cells_per_column": self.cells_per_column,
                "segment_limit_per_cell": 1, "synapse_limit_per_segment": MAX_SYNAPSES}


def _normalize(scores):
    # Same Laplace pseudo-count for every model. No test calibration.
    total = sum(scores.values()) + .1 * len(SYMBOLS)
    return {symbol: (scores.get(symbol, 0.) + .1) / total for symbol in SYMBOLS}


class Markov:
    def __init__(self, order):
        self.order = order
        self.counts = defaultdict(Counter)

    def fit(self, episodes):
        for episode in episodes:
            tokens = episode["tokens"]
            for index in range(1, len(tokens)):
                key = tuple(tokens[max(0, index - self.order):index]) if self.order else ()
                self.counts[key][tokens[index]] += 1

    def probabilities(self, history):
        key = tuple(history[-self.order:]) if self.order else ()
        return _normalize(self.counts.get(key, {}))


def train(parameters, episodes):
    """Training accepts no test inputs and never invokes evaluation."""
    network = ContextCells(parameters["cells_per_column"])
    baselines = {f"markov_{order}": Markov(order) for order in range(3)}
    for episode in episodes:
        active = frozenset()
        for symbol in episode["tokens"]:
            active, _ = network.step(symbol, active, learn=True)
    for baseline in baselines.values():
        baseline.fit(episodes)
    return network, baselines


def evaluate(network, baselines, episodes):
    """Read-only model evaluation, apart from a diagnostic operation counter."""
    names = ["context_cells", *baselines]
    totals = {name: {"correct": 0., "nll": 0., "n": 0, "ambiguous_correct": 0.,
                     "ambiguous_nll": 0., "ambiguous_n": 0} for name in names}
    activity = []
    for episode_index, episode in enumerate(episodes):
        tokens, active, history = episode["tokens"], frozenset(), []
        for index, symbol in enumerate(tokens):
            if index:
                probabilities, predicted = network.probabilities(active)
                forecasts = {"context_cells": probabilities,
                             **{name: model.probabilities(history) for name, model in baselines.items()}}
                for name, forecast in forecasts.items():
                    # Fractional tie score equals expected accuracy of uniform tie breaking.
                    largest = max(forecast.values())
                    tied = [s for s in SYMBOLS if abs(forecast[s] - largest) < 1e-12]
                    correct = 1 / len(tied) if symbol in tied else 0.
                    nll = -math.log2(forecast[symbol])
                    record = totals[name]
                    record["n"] += 1
                    record["correct"] += correct
                    record["nll"] += nll
                    if index == 2:
                        record["ambiguous_n"] += 1
                        record["ambiguous_correct"] += correct
                        record["ambiguous_nll"] += nll
            active, trace = network.step(symbol, active, learn=False)
            history.append(symbol)
            if episode_index < 4:
                next_probabilities, next_cells = network.probabilities(active)
                activity.append({"session": episode["session"], "step": index, **trace,
                                 "predicted_next_cells": next_cells,
                                 "next_probabilities": next_probabilities})
    rows = []
    for name, record in totals.items():
        rows.append({"condition": name, "accuracy": record["correct"] / record["n"],
                     "cross_entropy_bits": record["nll"] / record["n"],
                     "ambiguous_accuracy": record["ambiguous_correct"] / record["ambiguous_n"],
                     "ambiguous_cross_entropy_bits": record["ambiguous_nll"] / record["ambiguous_n"],
                     "predictions": record["n"], "ambiguous_predictions": record["ambiguous_n"]})
    return rows, activity


def run_trial(parameters, seed, test_transform=None):
    training = make_episodes(seed, "train", parameters["train_episodes"], parameters["label_noise"])
    testing = make_episodes(seed, "test", parameters["test_episodes"], parameters["label_noise"])
    if test_transform is not None:
        testing = test_transform(testing)
    network, baselines = train(parameters, training)
    before = network.snapshot()
    training_checks = network.synapse_checks
    rows, activity = evaluate(network, baselines, testing)
    after = network.snapshot()
    if before != after:
        raise AssertionError("Evaluation changed the learned state")
    return {"seed": seed, "rows": [{"seed": seed, **row} for row in rows],
            "training_hash": _hash(training), "testing_hash": _hash(testing),
            "trained_state_hash": _hash(before), "state": before,
            "activity": activity, "test_state_unchanged": True,
            "costs": {"training_synapse_checks": training_checks,
                      "evaluation_synapse_checks": network.synapse_checks - training_checks},
            "train_test_session_overlap": len({e["session"] for e in training} & {e["session"] for e in testing})}


def execute(parameters):
    parameters = validate(parameters)
    trials = [run_trial(parameters, parameters["seed"] + index) for index in range(parameters["replicates"])]
    table = [row for trial in trials for row in trial["rows"]]
    aggregates = []
    for name in ("context_cells", "markov_0", "markov_1", "markov_2"):
        selected = [row for row in table if row["condition"] == name]
        aggregates.append({"condition": name, **{key: statistics.mean(row[key] for row in selected)
                           for key in ("accuracy", "cross_entropy_bits", "ambiguous_accuracy", "ambiguous_cross_entropy_bits")}})
    return {
        "summary": "Контекстные клетки обучаются на отдельных эпизодах A–B–C–Z / X–B–D–Z. Тестирование замораживает связи; показаны все seed и контроль второго порядка.",
        "metrics": [{"label": f"Точность после B · {row['condition']}", "value": row["ambiguous_accuracy"]} for row in aggregates],
        "table": table, "aggregates": aggregates,
        "series": [{"name": name, "points": [{"x": row["seed"], "y": row["ambiguous_accuracy"]}
                     for row in table if row["condition"] == name]}
                   for name in ("context_cells", "markov_0", "markov_1", "markov_2")],
        "trials": trials,
        "model": {"id": VERSION, "biological_model": False, "full_brain_model": False,
                  "htm_reference_implementation": False, "ocas_implementation": False,
                  "monty_implementation": False, "input_width": WIDTH,
                  "input_columns": len(SYMBOLS) * WIDTH, "distal_activation_threshold": THRESHOLD,
                  "likelihood_readout": "training segment-use counts plus fixed Laplace pseudo-count 0.1",
                  "motifs": ["sparse fixed proximal codes", "context cells in columns", "distal predictive segments", "local synapse reinforcement"]},
        "validation": {"test_used_for_fitting": False, "test_state_unchanged": True,
                       "split": "Independent episodes of the same balanced finite grammar; repeated symbol patterns expected",
                       "start_symbol_scored": False, "tie_accuracy": "uniform tie expected accuracy",
                       "hyperparameters_tuned_on_test": False, "train_test_session_overlap": 0},
        "parameters": parameters, "limitations": SPEC["limitations"]}
