"""Run and retain the complete structural-plasticity toy benchmark.

Every chosen seed and all three conditions are preserved. No rule asserts that
guided rewiring must win. Uses the actual plugin worker with its POSIX 5 s CPU
limit when available. No biological datasets, external calls or model downloads.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.plugins import validate


def verify(seed=42, replicates=5, maximum=False):
    submitted = {"seed": seed, "replicates": replicates}
    if maximum:
        submitted.update(steps_per_phase=180, replicates=5, edge_budget=30,
                         rewiring_interval=10, learning_rate=.12, weight_decay=.02)
    parameters = validate("structural_plasticity", submitted)
    start = time.monotonic()
    child_cpu_before = None
    if os.name == "posix":
        import resource
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        child_cpu_before = usage.ru_utime + usage.ru_stime
    child = subprocess.run([sys.executable, "-I", str(ROOT / "plugin_worker.py"), "structural_plasticity"],
        input=json.dumps(parameters, allow_nan=False), capture_output=True, text=True,
        cwd=ROOT, timeout=12, env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"})
    elapsed = time.monotonic() - start
    if child.returncode:
        raise AssertionError(f"Bounded plugin worker failed: exit {child.returncode}")
    if len(child.stdout.encode("utf-8")) > 1024 * 1024:
        raise AssertionError("Worker output exceeded 1 MiB")
    result = json.loads(child.stdout, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite result")))["result"]
    assert len(result["trials"]) == parameters["replicates"]
    assert len(result["table"]) == 3 * parameters["replicates"]
    assert result["validation"]["test_used_for_fitting"] is False
    assert result["validation"]["test_used_for_rewiring"] is False
    for trial in result["trials"]:
        assert {c["condition"] for c in trial["conditions"]} == {"fixed", "random", "guided"}
        assert len({c["initial_state_hash"] for c in trial["conditions"]}) == 1
        assert len({c["training_batches_hash"] for c in trial["conditions"]}) == 1
        assert all(c["metrics"]["active_edges"] == parameters["edge_budget"] + 6 for c in trial["conditions"])
    cpu = None
    if child_cpu_before is not None:
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = usage.ru_utime + usage.ru_stime - child_cpu_before
        assert cpu < 5, "Worker used its entire CPU soft limit"
    return {"passed": True, "checked_at": datetime.now(timezone.utc).isoformat(),
        "execution": "actual_plugin_worker_subprocess", "cpu_limit_seconds": 5 if os.name == "posix" else None,
        "measured_child_cpu_seconds": cpu, "wall_seconds": elapsed,
        "computational_stress_boundary": maximum, "output_bytes": len(child.stdout.encode("utf-8")),
        "parameters": parameters, "result": result,
        "source_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in ("workbench/morphogenesis.py", "workbench/plugins.py", "plugin_worker.py")},
        "interpretation": "Descriptive synthetic benchmark. No superiority requirement, no biological validation.",
        "external_network_requests": 0, "brain_or_genome_data_used": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--replicates", type=int, default=5)
    parser.add_argument("--maximum", action="store_true", help="Exercise the upper computational boundary under the actual worker limits")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.seed, args.replicates, args.maximum)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "parameters": result["parameters"],
        "child_cpu_seconds": result["measured_child_cpu_seconds"], "output_bytes": result["output_bytes"],
        "aggregates": result["result"]["aggregates"]}, ensure_ascii=False, indent=2, allow_nan=False))
