#!/usr/bin/env python3
"""P0-SINT-PHYS-001: held-out replay of published optical QRC memory model.

Calls the authors' original optical_qrc model and benchmark code at a fixed Git
commit. This does NOT simulate laboratory quantum measurements or vacuum-based AI.
No custom physical dynamics, fitted feedback parameters, or synthetic 'success'
claim is introduced here.
"""
from __future__ import annotations

import argparse
from functools import partial
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

from qrc.reservoir.global_phase_protocol import GlobalPhaseProtocol
from qrc.task.benchmarks import delay
from qrc.task.regression_task import RegressionTask

UPSTREAM_COMMIT = "71b3d799c689dbf0f9c2819f2411e47749d3585e"
NUM_WASHOUT, NUM_TRAIN, NUM_TEST = 3, 70, 30
NOISE_LEVEL = 0.05650294519888824
NUM_COPIES, TRIALS = 3, 5
DELAYS = (1, 2)


def load_authors_figure(path: Path):
    spec = importlib.util.spec_from_file_location("authors_figure_3e", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot import upstream figure code")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replay_one(parameters, alpha, mask, trial: int, tau: int, feedback: bool):
    """Specialized upstream reservoir and task, no replacement physics."""
    model = GlobalPhaseProtocol(NUM_COPIES, parameters)
    model.reset(
        noise_level=NOISE_LEVEL,
        alpha=alpha,
        beta=0.0,
        fb_mask=mask if feedback else np.zeros_like(mask),
        noise_seed=trial,
    )
    task = RegressionTask(binary=False)
    task.run(model, num_washout=NUM_WASHOUT, num_train=NUM_TRAIN,
             num_test=NUM_TEST, seed=2025 + trial)
    task.train(partial(delay, delay=tau))
    result = task.score()
    pred = np.asarray(result.y_pred_test, dtype=float)
    target = np.asarray(result.y_true_test, dtype=float)
    if pred.shape != (NUM_TEST,) or target.shape != (NUM_TEST,):
        raise AssertionError("Unexpected held-out test dimensions")
    if not np.all(np.isfinite(pred)) or not np.all(np.isfinite(target)):
        raise AssertionError("Invalid output: nonfinite prediction or target")
    target_sd = float(target.std())
    if target_sd < 1e-12:
        raise AssertionError("Degenerate held-out target")
    # Original authors' 'corrcoeff' is squared Pearson correlation, see
    # qrc/task/evaluation_metrics.py. Do not report it as raw Pearson r.
    capacity = float(result.corrcoeff)
    nrmse = float(np.sqrt(np.mean((pred - target)**2)) / target_sd)
    if not (np.isfinite(capacity) and np.isfinite(nrmse)):
        raise AssertionError("Invalid score")
    return {"memory_capacity_pearson_r_squared": capacity,
            "heldout_nrmse": nrmse}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    upstream = args.upstream.resolve()
    actual_rev = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"],
        text=True, timeout=10).strip()
    if actual_rev != UPSTREAM_COMMIT:
        raise SystemExit(f"BLOCKED_upstream_sha_mismatch: {actual_rev}")
    published_file = upstream / "simulations/fig3e_global_phase_memory.py"
    published = load_authors_figure(published_file)
    params = published.ALPHA_MASK_PARAMS[NUM_COPIES]
    alpha = np.asarray(params["alpha"], dtype=float)
    mask = np.asarray(params["mask"], dtype=float)
    if alpha.shape != (NUM_COPIES,) or mask.shape != (NUM_COPIES, NUM_COPIES):
        raise AssertionError("Upstream figure 3e parameters changed unexpectedly")

    results = []
    for tau in DELAYS:
        for trial in range(TRIALS):
            full = replay_one(published.RESERVOIR_PARAMETERS, alpha, mask,
                              trial=trial, tau=tau, feedback=True)
            ablated = replay_one(published.RESERVOIR_PARAMETERS, alpha, mask,
                                 trial=trial, tau=tau, feedback=False)
            results.append({
                "tau": tau, "trial": trial, "input_seed": 2025 + trial,
                "noise_seed": trial, "original_feedback": full,
                "zero_feedback_ablation": ablated
            })
    grouped = []
    for tau in DELAYS:
        subset = [x for x in results if x["tau"] == tau]
        grouped.append({
            "tau": tau, "paired_trials": len(subset),
            "mean_capacity_feedback": float(np.mean([
                x["original_feedback"]["memory_capacity_pearson_r_squared"]
                for x in subset])),
            "mean_capacity_no_feedback": float(np.mean([
                x["zero_feedback_ablation"]["memory_capacity_pearson_r_squared"]
                for x in subset])),
            "mean_nrmse_feedback": float(np.mean([
                x["original_feedback"]["heldout_nrmse"] for x in subset])),
            "mean_nrmse_no_feedback": float(np.mean([
                x["zero_feedback_ablation"]["heldout_nrmse"] for x in subset])),
        })
    receipt = {
        "task": "P0-SINT-PHYS-001",
        "status": "software_replay_only",
        "original_paper": "https://doi.org/10.1038/s41566-026-01880-9",
        "original_source": "https://github.com/EQ15T/optical_qrc",
        "original_source_commit": actual_rev,
        "authors_figure_3e_sha256": hashlib.sha256(
            published_file.read_bytes()).hexdigest(),
        "model": "authors' GlobalPhaseProtocol optical digital twin",
        "task_implementation": "authors' RegressionTask(delay)",
        "scope": "R=3, delays 1 and 2, five paired trials (subset of fig3e)",
        "control": "identical input/noise seeds, feedback mask set to zero",
        "noise_level": NOISE_LEVEL,
        "washout": NUM_WASHOUT, "train": NUM_TRAIN, "test": NUM_TEST,
        "metric_note": "authors' corrcoeff means Pearson correlation squared",
        "numerical_checks": "PASS",
        "lab_hardware_executed": False,
        "quantum_vacuum_computation_established": False,
        "autonomous_intelligence_established": False,
        "summary": grouped,
        "trials": results,
        "limitations": [
            "Digital-twin numerical replay, not independent optical experiment",
            "Paired ablation does not establish a statistical superiority claim",
            "Predefined parameters are drawn from published figure, not fitted here",
            "Train/test are distinct chronological segments per upstream RegressionTask",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, ensure_ascii=False,
                                     allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"task": receipt["task"], "status": receipt["status"],
                      "summary": grouped}, ensure_ascii=False))


if __name__ == "__main__":
    main()
