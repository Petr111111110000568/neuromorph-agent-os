"""P0/Synt: bounded fixed-volume simulation, never connected to an actuator.

The persistent spatial volume is represented by a line of equal fixed cells.
Mass/state concentration changes between cells; geometry remains fixed.
This model is *not* a model of human morphogenesis or a medical device.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path


class SafetyStop(RuntimeError):
    """Fail closed if observations, conservation, or bounds are invalid."""


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass
class SyntVolume:
    state: list[float]
    target: list[float]
    max_edge_flux: float = 0.2
    tolerance: float = 1e-9
    events: list[dict] = field(default_factory=list)
    _head: str = "0" * 64

    def __post_init__(self) -> None:
        self.state = list(self.state)
        self.target = list(self.target)
        if not 0 < self.max_edge_flux <= 1:
            raise SafetyStop("Invalid bounded actuation")
        if len(self.state) < 2 or len(self.state) != len(self.target):
            raise SafetyStop("Fixed volume requires equally sized state and target")
        for seq in (self.state, self.target):
            if any(not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 or x > 1 for x in seq):
                raise SafetyStop("Invalid or unobservable cell")
        if abs(sum(self.state) - sum(self.target)) > self.tolerance:
            raise SafetyStop("Target violates state conservation")
        self._initial_mass = sum(self.state)

    @property
    def volume(self) -> int:
        return len(self.state)

    def sense(self, observation: list[float] | None = None) -> list[float]:
        obs = list(self.state) if observation is None else list(observation)
        if len(obs) != self.volume or any(not isinstance(x, (int, float)) or not math.isfinite(x) or not 0 <= x <= 1 for x in obs):
            raise SafetyStop("Sensor failed")
        return obs

    def estimate(self, observation: list[float]) -> list[float]:
        obs = self.sense(observation)
        if abs(sum(obs) - self._initial_mass) > self.tolerance:
            raise SafetyStop("Sensor conservation mismatch")
        return obs

    def _record(self, transitions: list[dict]) -> None:
        obj = {
            "mode": "SIMULATION_ONLY",
            "volume_cells": self.volume,
            "state": [round(v, 10) for v in self.state],
            "transitions": transitions,
            "previous": self._head,
        }
        obj["sha256"] = hashlib.sha256(canonical(obj)).hexdigest()
        self._head = obj["sha256"]
        self.events.append(obj)

    def verify(self) -> None:
        if len(self.state) != self.volume or any(not math.isfinite(x) or x < -self.tolerance or x > 1 + self.tolerance for x in self.state):
            raise SafetyStop("Containment/integrity fault")
        if abs(sum(self.state) - self._initial_mass) > self.tolerance:
            raise SafetyStop("Mass conservation fault")

    def step(self) -> list[dict]:
        # SENSE -> ESTIMATE -> BOUNDED SIMULATED ACTUATION -> VERIFY.
        estimate = self.estimate(self.sense())
        transfers = []
        for edge in range(self.volume - 1):
            prefix_excess = sum(estimate[:edge + 1]) - sum(self.target[:edge + 1])
            if abs(prefix_excess) <= self.tolerance:
                continue
            if prefix_excess > 0:
                source, dest = edge, edge + 1
            else:
                source, dest = edge + 1, edge
            value = min(abs(prefix_excess), self.max_edge_flux, max(0, self.state[source]), max(0, 1 - self.state[dest]))
            if value <= self.tolerance:
                continue
            self.state[source] -= value
            self.state[dest] += value
            estimate[source] -= value
            estimate[dest] += value
            transfers.append({"from": source, "to": dest, "amount": round(value, 10)})
        self.verify()
        self._record(transfers)
        return transfers

    def run(self, max_steps: int = 100) -> dict:
        if max_steps <= 0 or max_steps > 100000:
            raise SafetyStop("Invalid maximum simulated steps")
        for _ in range(max_steps):
            if max(abs(a - b) for a, b in zip(self.state, self.target)) <= self.tolerance:
                break
            if not self.step():
                raise SafetyStop("No feasible bounded transition")
        else:
            raise SafetyStop("Did not converge within bounded steps")
        self.verify()
        return {
            "status": "SIMULATED_AND_VERIFIED",
            "mode": "NO_PHYSICAL_ACTUATION",
            "fixed_volume_cells": self.volume,
            "steps": len(self.events),
            "total_mass": round(self._initial_mass, 10),
            "target_reached": all(abs(a - b) <= self.tolerance for a, b in zip(self.state, self.target)),
            "final_state": [round(x, 10) for x in self.state],
            "last_event_sha256": self._head,
        }

    def verify_event_chain(self) -> bool:
        previous = "0" * 64
        for row in self.events:
            unsigned = {k: v for k, v in row.items() if k != "sha256"}
            if row.get("previous") != previous or hashlib.sha256(canonical(unsigned)).hexdigest() != row.get("sha256"):
                return False
            previous = row["sha256"]
        return True


def smoke() -> dict:
    sim = SyntVolume([1, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 1], max_edge_flux=0.125)
    result = sim.run()
    if not result["target_reached"] or not sim.verify_event_chain():
        raise SafetyStop("Independent smoke checks failed")
    root = Path(__file__).resolve().parent / "evidence"
    root.mkdir(exist_ok=True)
    (root / "smoke.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (root / "smoke_events.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in sim.events) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("smoke",))
    args = parser.parse_args()
    try:
        out = smoke() if args.command == "smoke" else {}
        print(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return 0
    except SafetyStop as e:
        print(json.dumps({"status": "SAFETY_STOP", "reason": str(e)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
