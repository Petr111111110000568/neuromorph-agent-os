"""Engineering toy model of structural plasticity, not a biological model.

There is no claim that these equations implement S. V. Savelyev's theory.
The magnitude-pruning/gradient-growth comparison is a small dynamic sparse
learning experiment, with separate training and held-out evaluation pathways.
"""
import copy
import hashlib
import json
import math
import random
import statistics

INPUTS = 6
HIDDEN = 6
POSSIBLE_EDGES = INPUTS * HIDDEN
TRAIN_SAMPLES = 80
TEST_SAMPLES = 80
BATCH_SIZE = 8
CONDITIONS = ("fixed", "random", "guided")
MODEL_VERSION = "structural-plasticity-toy/1"


def _hash(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _rng(seed, label):
    return random.Random(f"{MODEL_VERSION}:{seed}:{label}")


def target(x, phase):
    offset = 0 if phase == "old" else 3
    return .6 * math.tanh(1.5 * x[offset]) - .4 * math.tanh(1.5 * x[offset + 1]) + .2 * x[offset + 2]


def _dataset(seed, split, phase, count):
    rng = _rng(seed, f"{split}-{phase}")
    rows = []
    for _ in range(count):
        x = [rng.uniform(-1, 1) for _ in range(INPUTS)]
        rows.append((x, target(x, phase)))
    return rows


def make_problem(seed, steps_per_phase):
    """Separate deterministic RNG streams; the trainer receives no test rows."""
    old_train = _dataset(seed, "train", "old", TRAIN_SAMPLES)
    new_train = _dataset(seed, "train", "new", TRAIN_SAMPLES)
    old_test = _dataset(seed, "test", "old", TEST_SAMPLES)
    new_test = _dataset(seed, "test", "new", TEST_SAMPLES)
    batch_rng = _rng(seed, "training-batch-order")
    batches = []
    for rows in (old_train, new_train):
        for _ in range(steps_per_phase):
            batches.append([rows[batch_rng.randrange(len(rows))] for _ in range(BATCH_SIZE)])
    train_points = {tuple(x) for x, _ in old_train + new_train}
    test_points = {tuple(x) for x, _ in old_test + new_test}
    if train_points & test_points:
        raise AssertionError("Training and test features must be disjoint")
    return {"batches": batches, "old_test": old_test, "new_test": new_test,
            "fingerprints": {"old_train": _hash(old_train), "new_train": _hash(new_train),
                             "old_test": _hash(old_test), "new_test": _hash(new_test),
                             "training_batches": _hash(batches)},
            "train_test_feature_overlap": 0}


def initial_state(seed, edge_budget):
    rng = _rng(seed, "initialization")
    active = sorted(rng.sample(range(POSSIBLE_EDGES), edge_budget))
    weights = [rng.gauss(0, .3) if i in active else 0.0 for i in range(POSSIBLE_EDGES)]
    return {"weights": weights, "outputs": [rng.gauss(0, .25) for _ in range(HIDDEN)], "active": active}


def _incoming(active):
    result = [[] for _ in range(HIDDEN)]
    for index in active:
        result[index // INPUTS].append(index)
    return result


def _forward(x, state, incoming):
    hidden = [math.tanh(sum(state["weights"][i] * x[i % INPUTS] for i in indices)) for indices in incoming]
    prediction = sum(weight * activation for weight, activation in zip(state["outputs"], hidden))
    return prediction, hidden


def _edges(state):
    return ([{"source": f"x{i % INPUTS}", "target": f"h{i // INPUTS}", "weight": state["weights"][i]}
             for i in state["active"]] +
            [{"source": f"h{i}", "target": "y", "weight": value} for i, value in enumerate(state["outputs"])])


def train_condition(parameters, condition, initial, batches, seed):
    """Only training inputs are accepted here. No evaluation-driven selection.

    Growth scores use a minibatch gradient at the pre-update state. Both
    rewirers prune the smallest post-update active weight; they differ only
    in selection from edges that were inactive before the pruning event.
    """
    if condition not in CONDITIONS:
        raise ValueError("Unknown comparison condition")
    state = copy.deepcopy(initial)
    initial_hash = _hash(state)
    active = list(state["active"])
    incoming = _incoming(active)
    random_growth = _rng(seed, "random-growth")
    total_steps = len(batches)
    snapshots = [{"step": 0, "state": copy.deepcopy(state)}]
    events = []
    counts = {"training_forward_edge_visits": 0, "training_backward_edge_visits": 0,
              "weight_update_visits": 0, "candidate_gradient_visits": 0,
              "structural_selection_visits": 0, "evaluation_edge_visits": 0}
    clip_events = 0
    budget = parameters["edge_budget"]
    for step, batch in enumerate(batches, 1):
        rewire = condition != "fixed" and step % parameters["rewiring_interval"] == 0 and step < total_steps
        dormant = [i for i in range(POSSIBLE_EDGES) if i not in active] if rewire else []
        gradients = [0.] * POSSIBLE_EDGES
        output_gradients = [0.] * HIDDEN
        candidate_gradients = [0.] * POSSIBLE_EDGES
        for x, y in batch:
            prediction, hidden = _forward(x, state, incoming)
            derivative = 2 * (prediction - y)
            hidden_derivative = [derivative * state["outputs"][j] * (1 - hidden[j] ** 2) for j in range(HIDDEN)]
            for j in range(HIDDEN):
                output_gradients[j] += derivative * hidden[j]
            for index in active:
                gradients[index] += hidden_derivative[index // INPUTS] * x[index % INPUTS]
            if rewire and condition == "guided":
                for index in dormant:
                    candidate_gradients[index] += hidden_derivative[index // INPUTS] * x[index % INPUTS]
        batch_n = len(batch)
        # Clip the norm of the mean active/output gradient to 10. This is an
        # explicit numerical safeguard, not a neuronal mechanism.
        norm = math.sqrt(sum((gradients[i] / batch_n) ** 2 for i in active) +
                         sum((value / batch_n) ** 2 for value in output_gradients))
        scale = min(1., 10. / norm) if norm else 1.
        clip_events += int(scale < 1.)
        lr, decay = parameters["learning_rate"], parameters["weight_decay"]
        for index in active:
            state["weights"][index] -= lr * (scale * gradients[index] / batch_n + decay * state["weights"][index])
        for j in range(HIDDEN):
            state["outputs"][j] -= lr * (scale * output_gradients[j] / batch_n + decay * state["outputs"][j])
        counts["training_forward_edge_visits"] += batch_n * (budget + HIDDEN)
        counts["training_backward_edge_visits"] += batch_n * (budget + HIDDEN)
        counts["weight_update_visits"] += budget + HIDDEN
        if rewire:
            removed = min(active, key=lambda i: (abs(state["weights"][i]), i))
            if condition == "guided":
                grown = min(dormant, key=lambda i: (-abs(candidate_gradients[i]), i))
                counts["candidate_gradient_visits"] += batch_n * len(dormant)
                counts["structural_selection_visits"] += len(dormant)
            else:
                grown = random_growth.choice(dormant)
            counts["structural_selection_visits"] += POSSIBLE_EDGES + len(active)
            removed_weight = state["weights"][removed]
            active.remove(removed)
            active.append(grown)
            active.sort()
            state["weights"][removed] = 0.
            state["weights"][grown] = 0.
            state["active"] = list(active)
            incoming = _incoming(active)
            events.append({"step": step, "removed_index": removed, "grown_index": grown,
                           "removed_weight": removed_weight, "grown_initial_weight": 0.,
                           "active_sparse_edges_after": len(active),
                           "growth_signal": "training_minibatch_gradient" if condition == "guided" else "seeded_random"})
        if len(active) != budget:
            raise AssertionError("Structural edge budget changed")
        if step % 20 == 0 or step in (parameters["steps_per_phase"], total_steps):
            snapshots.append({"step": step, "state": copy.deepcopy(state)})
    return {"condition": condition, "state": state, "snapshots": snapshots, "events": events,
            "initial_state_hash": initial_hash, "final_state_hash": _hash(state),
            "training_batches_hash": _hash(batches), "costs": counts, "gradient_clip_events": clip_events,
            "initial_edges": _edges(initial), "final_edges": _edges(state)}


def evaluate(state, rows):
    """Read-only held-out evaluation: no optimizer, RNG or topology updates."""
    incoming = _incoming(state["active"])
    return statistics.mean((_forward(x, state, incoming)[0] - y) ** 2 for x, y in rows)


def run_trial(parameters, seed, test_transform=None):
    problem = make_problem(seed, parameters["steps_per_phase"])
    initial = initial_state(seed, parameters["edge_budget"])
    # Used by the leakage regression test only; this callback never reaches
    # training data, initialization, scheduling or the public plugin schema.
    if test_transform is not None:
        problem["old_test"] = test_transform(copy.deepcopy(problem["old_test"]))
        problem["new_test"] = test_transform(copy.deepcopy(problem["new_test"]))
    conditions = []
    for name in CONDITIONS:
        trained = train_condition(parameters, name, initial, problem["batches"], seed)
        trajectory = []
        for snapshot in trained.pop("snapshots"):
            old_mse = evaluate(snapshot["state"], problem["old_test"])
            new_mse = evaluate(snapshot["state"], problem["new_test"])
            trajectory.append({"step": snapshot["step"], "old_test_mse": old_mse, "new_test_mse": new_mse})
            trained["costs"]["evaluation_edge_visits"] += (len(problem["old_test"]) + len(problem["new_test"])) * (parameters["edge_budget"] + HIDDEN)
        before_shift = next(point for point in trajectory if point["step"] == parameters["steps_per_phase"])
        final = trajectory[-1]
        costs = trained["costs"]
        costs["cumulative_active_edge_visits"] = (costs["training_forward_edge_visits"] +
             costs["training_backward_edge_visits"] + costs["weight_update_visits"])
        costs["total_counted_visits"] = (costs["cumulative_active_edge_visits"] + costs["candidate_gradient_visits"] +
                                         costs["structural_selection_visits"] + costs["evaluation_edge_visits"])
        metrics = {"old_mse_after_phase1": before_shift["old_test_mse"],
                   "new_mse_before_shift": before_shift["new_test_mse"],
                   "new_mse_after_shift": final["new_test_mse"],
                   "old_mse_after_shift": final["old_test_mse"],
                   "forgetting_delta": final["old_test_mse"] - before_shift["old_test_mse"],
                   "active_edges": parameters["edge_budget"] + HIDDEN,
                   "rewiring_events": len(trained["events"]),
                   "final_topology_changed_edges": len(set(trained["state"]["active"]) ^ set(initial["active"])) // 2,
                   "cumulative_active_edge_visits": costs["cumulative_active_edge_visits"],
                   "candidate_gradient_visits": costs["candidate_gradient_visits"],
                   "evaluation_edge_visits": costs["evaluation_edge_visits"],
                   "total_counted_visits": costs["total_counted_visits"]}
        trained.pop("state")
        trained.update({"metrics": metrics, "trajectory": trajectory})
        conditions.append(trained)
    return {"seed": seed, "fingerprints": problem["fingerprints"],
            "train_test_feature_overlap": problem["train_test_feature_overlap"], "conditions": conditions}


def structural_plasticity(parameters):
    trials = [run_trial(parameters, (parameters["seed"] + i) % 2147483648) for i in range(parameters["replicates"])]
    table = [{"seed": trial["seed"], "condition": condition["condition"], **condition["metrics"]}
             for trial in trials for condition in trial["conditions"]]
    aggregates = []
    series = []
    metrics = []
    for name in CONDITIONS:
        rows = [row for row in table if row["condition"] == name]
        mean_new = statistics.mean(row["new_mse_after_shift"] for row in rows)
        aggregates.append({"condition": name, "seeds": len(rows),
            "new_mse_after_shift_mean": mean_new,
            "new_mse_after_shift_std": statistics.stdev(row["new_mse_after_shift"] for row in rows) if len(rows) > 1 else 0.,
            "old_mse_after_phase1_mean": statistics.mean(row["old_mse_after_phase1"] for row in rows),
            "old_mse_after_shift_mean": statistics.mean(row["old_mse_after_shift"] for row in rows),
            "forgetting_delta_mean": statistics.mean(row["forgetting_delta"] for row in rows),
            "total_counted_visits_mean": statistics.mean(row["total_counted_visits"] for row in rows)})
        metrics.append({"label": f"Test MSE после смены · {name}", "value": mean_new})
        sequences = [next(item for item in trial["conditions"] if item["condition"] == name)["trajectory"] for trial in trials]
        for key, label in (("new_test_mse", "новая зависимость"), ("old_test_mse", "старая зависимость")):
            series.append({"name": f"{name} · {label}", "points": [
                {"x": point["step"], "y": statistics.mean(sequence[i][key] for sequence in sequences)}
                for i, point in enumerate(sequences[0])]})
    paired = []
    for trial in trials:
        lookup = {item["condition"]: item for item in trial["conditions"]}
        paired.append({"seed": trial["seed"],
            "guided_minus_fixed_new_mse": lookup["guided"]["metrics"]["new_mse_after_shift"] - lookup["fixed"]["metrics"]["new_mse_after_shift"],
            "guided_minus_random_new_mse": lookup["guided"]["metrics"]["new_mse_after_shift"] - lookup["random"]["metrics"]["new_mse_after_shift"]})
    return {
        "summary": "Сравнены фиксированная разреженная сеть, случайный рост связей и рост по тренировочному градиенту при одинаковом бюджете рёбер. Все заданные seed сохранены; превосходство перестройки заранее не предполагается.",
        "metrics": metrics, "series": series, "table": table, "aggregates": aggregates,
        "paired_comparisons": paired, "trials": trials,
        "model": {"id": MODEL_VERSION, "inputs": INPUTS, "hidden_nodes": HIDDEN, "outputs": 1,
            "equation": "h_j=tanh(sum_i m_ji*w_ji*x_i); prediction=sum_j v_j*h_j",
            "training": "minibatch mean squared error; SGD plus weight decay; active gradient norm capped at 10",
            "old_target": "0.6*tanh(1.5*x0)-0.4*tanh(1.5*x1)+0.2*x2",
            "new_target": "0.6*tanh(1.5*x3)-0.4*tanh(1.5*x4)+0.2*x5",
            "fixed_output_edges": HIDDEN, "sparse_input_hidden_edges": parameters["edge_budget"],
            "possible_input_hidden_edges": POSSIBLE_EDGES, "nodes_grow": False,
            "savelyev_algorithm": False, "biological_model": False, "brain_or_genome_data": False,
            "cost_unit": "counted edge visits; not FLOPs, joules or neuronal metabolic energy"},
        "validation": {"train_test_split": "independent seeded synthetic datasets per phase",
            "train_samples_per_phase": TRAIN_SAMPLES, "test_samples_per_phase": TEST_SAMPLES,
            "batch_size": BATCH_SIZE, "shift_at_step": parameters["steps_per_phase"],
            "test_used_for_fitting": False, "test_used_for_rewiring": False, "test_driven_early_stopping": False,
            "identical_initialization_within_seed": True, "identical_training_batches_within_seed": True,
            "fixed_active_edge_budget": True, "all_requested_seeds_reported": True,
            "hyperparameter_search": False, "confidence_interval": "not_estimated",
            "scientific_generalization": "not_established", "biological_validation": "not_performed"},
        "interpretation": "Положительный forgetting_delta означает увеличение ошибки на старой зависимости; отрицательная парная разность MSE означает меньшую ошибку guided в данном запуске. Это описательные результаты синтетического benchmark, не доказательство механизма интеллекта.",
        "sources": [{"id": "savelyev-memory-chapter", "url": "https://zavtra.ru/books/s_v_savel_ev_sozrevanie_pamyati", "role": "conceptual motivation, not algorithm specification"},
                    {"id": "rigl", "url": "https://arxiv.org/abs/1911.11134", "role": "related dynamic sparse training; this is not a reproduction of the published benchmark"}],
    }
