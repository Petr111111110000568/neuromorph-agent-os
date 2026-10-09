"""Offline dual-virtual-sensor adapter for existing P0/Synt simulator.

No network clients, no DNA, no physical actuator or laboratory I/O.
Accepts *only* synthetic simulator observations, emits an inert JSON report.
Never use an output as a command for a physical device or living system.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from sint_sim import SafetyStop, SyntVolume, canonical

SCHEMA = "p0.sint.virtual-observation.v1"
_ALLOWED_FIELDS = frozenset({"schema", "mode", "run_id", "frame", "channel", "values", "uncertainty", "quality"})
_CHANNELS = frozenset({"SIM_A", "SIM_B"})
_MAX_CELLS = 128
_MAX_UNCERTAINTY = 0.02
_MAX_CHANNEL_DELTA = 0.02
_MAX_TRANSFER = 0.125
_MAX_STEPS = 1000


def _values(seq: Any, *, label: str, length: int | None = None) -> list[float]:
    if not isinstance(seq, list) or not 2 <= len(seq) <= _MAX_CELLS or (length is not None and len(seq) != length):
        raise SafetyStop(f"{label}: invalid geometry")
    if any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1 for x in seq):
        raise SafetyStop(f"{label}: invalid or unobservable state")
    return [float(x) for x in seq]


def _frame(packet: Any) -> dict:
    if not isinstance(packet, dict) or set(packet) != _ALLOWED_FIELDS:
        raise SafetyStop("Malformed/extended telemetry: unknown commands rejected")
    if packet["schema"] != SCHEMA or packet["mode"] != "SIMULATION_ONLY":
        raise SafetyStop("Real actuator/device command not accepted")
    if not isinstance(packet["run_id"], str) or not re.fullmatch(r"SIM-[a-zA-Z0-9_-]{4,32}", packet["run_id"]):
        raise SafetyStop("No verified virtual run identity")
    if type(packet["frame"]) is not int or not 0 <= packet["frame"] <= 1_000_000:
        raise SafetyStop("Invalid observation sequence")
    if type(packet["channel"]) is not str or packet["channel"] not in _CHANNELS or packet["quality"] is not True:
        raise SafetyStop("Invalid or low-quality virtual sensor")
    observations = _values(packet["values"], label="observation")
    unc = packet["uncertainty"]
    if type(unc) not in (float, int) or not math.isfinite(unc) or not 0 <= unc <= _MAX_UNCERTAINTY:
        raise SafetyStop("Excessive/invalid observation uncertainty")
    return {**packet, "values": observations}


def replay_dual_virtual_channels(
    packet_a: dict, packet_b: dict, target: list[float],
    *, last_frame: int = -1, max_transfer: float = _MAX_TRANSFER,
) -> dict:
    """Estimate toy state and simulate bounded transfers; no external effects."""
    a, b = _frame(packet_a), _frame(packet_b)
    if a["channel"] != "SIM_A" or b["channel"] != "SIM_B":
        raise SafetyStop("Insufficient or duplicate virtual sensor channels")
    if a["run_id"] != b["run_id"] or a["frame"] != b["frame"]:
        raise SafetyStop("Desynchronized virtual observations")
    if a["frame"] <= last_frame:
        raise SafetyStop("Replay/stale telemetry blocked")
    if len(a["values"]) != len(b["values"]):
        raise SafetyStop("Sensor geometry disagreement")
    if not 0 < max_transfer <= _MAX_TRANSFER:
        raise SafetyStop("Bounded virtual transfer limit exceeded")
    if any(abs(x - y) > _MAX_CHANNEL_DELTA for x, y in zip(a["values"], b["values"])):
        raise SafetyStop("Virtual sensor disagreement: unobservable state")
    estimate = [(x + y) / 2 for x, y in zip(a["values"], b["values"])]
    final_target = _values(target, label="target", length=len(estimate))
    if abs(math.fsum(estimate) - math.fsum(final_target)) > 1e-9:
        raise SafetyStop("Virtual volume conservation mismatch")
    sim = SyntVolume(estimate, final_target, max_edge_flux=max_transfer)
    outcome = sim.run(max_steps=_MAX_STEPS)
    if not sim.verify_event_chain() or not outcome["target_reached"]:
        raise SafetyStop("Simulated transition/ledger verification failed")
    evidence = {"packet_sha256": hashlib.sha256(canonical({"a": a, "b": b})).hexdigest(),
                "event_chain_last_sha256": outcome["last_event_sha256"],
                "sensor_class": "TWO_SYNTHETIC_CHANNELS_NOT_INDEPENDENT_PHYSICAL_SENSORS"}
    return {"mode": "SIMULATION_ONLY", "schema": "p0.sint.virtual-report.v1",
            "run_id": a["run_id"], "frame": a["frame"],
            "observability": "TOY_ONLY", "biological_validation": False,
            "physical_actuation": False, "genomic_output": False,
            "closed_loop": "SENSE_ESTIMATE_BOUNDED_SIMULATED_ACTUATION_VERIFY",
            "outcome": outcome, "evidence": evidence}


def main() -> int:
    parser = argparse.ArgumentParser(description="P0/Synt virtual offline telemetry replay")
    parser.add_argument("--input", type=Path, required=True, help="JSON with packet_a, packet_b, target")
    parser.add_argument("--output", type=Path, required=True, help="inert report JSON only")
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"packet_a", "packet_b", "target"}:
            raise SafetyStop("Invalid virtual replay envelope")
        result = replay_dual_virtual_channels(data["packet_a"], data["packet_b"], data["target"])
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": "SIMULATED_AND_VERIFIED", "steps": result["outcome"]["steps"], "output": str(args.output)}))
        return 0
    except (SafetyStop, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "HARD_STOP", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())