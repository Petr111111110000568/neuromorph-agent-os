"""Bounded offline sequence benchmark and one-cell capacity ablation."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.cortical import execute


def verify():
    scenarios = {
        "default": {},
        "one_cell_ablation": {"cells_per_column": 1},
        "bounded_maximum_with_noise": {"train_episodes": 400, "test_episodes": 300,
                                       "cells_per_column": 6, "replicates": 5, "label_noise": .25},
    }
    runs = []
    for name, parameters in scenarios.items():
        started = time.perf_counter()
        result = execute(parameters)
        elapsed = time.perf_counter() - started
        assert result["validation"]["test_state_unchanged"]
        assert not result["validation"]["test_used_for_fitting"]
        assert all(trial["train_test_session_overlap"] == 0 for trial in result["trials"])
        runs.append({"scenario": name, "wall_seconds_observed": elapsed, "result": result})
    defaults = {row["condition"]: row for row in runs[0]["result"]["aggregates"]}
    assert defaults["context_cells"]["ambiguous_accuracy"] == 1.
    assert defaults["markov_1"]["ambiguous_accuracy"] == .5
    assert defaults["markov_2"]["ambiguous_accuracy"] == 1.
    ablated = next(row for row in runs[1]["result"]["aggregates"] if row["condition"] == "context_cells")
    assert ablated["ambiguous_accuracy"] == .5
    return {"verified_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
            "status": "passed", "scope": "offline synthetic sequence benchmark; no brain/biological validation",
            "runs": runs}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    proof = verify()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(proof, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": proof["status"], "runs": [
        {"scenario": run["scenario"], "wall_seconds_observed": run["wall_seconds_observed"],
         "aggregates": run["result"]["aggregates"]} for run in proof["runs"]]}, ensure_ascii=False))
