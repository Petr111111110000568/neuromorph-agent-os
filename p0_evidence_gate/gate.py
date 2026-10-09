"""P0 evidence and offline-simulator gate.

Read-only public research metadata; no DNA, live organisms, wet-lab or equipment IO.
Python standard library only. This is not a biosafety or medical certification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = "p0-evidence-gate/1.0"
EVIDENCE_KINDS = frozenset({"PRIMARY", "PROGRAM", "PATENT", "PREPRINT", "UNVERIFIED"})
ENVIRONMENTS = frozenset({"NONE", "IN_VITRO", "PHANTOM", "EX_VIVO", "ANIMAL", "HUMAN"})
REQUIRED = frozenset({"id", "source_url", "kind", "environment", "observed", "limitations", "safe_next_step"})
SIM_TARGET = "virtual_model"
SIM_COMMANDS = frozenset({"READ_STATE", "SIMULATED_TICK"})


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _check_source_url(raw: str) -> bool:
    try:
        u = urlsplit(raw)
    except ValueError:
        return False
    return u.scheme == "https" and bool(u.hostname) and not u.username and not u.password


def validate_catalog(catalog: object) -> dict:
    """Validate the documented evidence scope, not the scientific truth of the articles."""
    if not isinstance(catalog, dict) or catalog.get("schema") != SCHEMA:
        raise ValueError("unsupported catalog schema")
    records = catalog.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("nonempty records list required")
    seen = set()
    for r in records:
        if not isinstance(r, dict) or not REQUIRED.issubset(r):
            raise ValueError("incomplete evidence record")
        if set(r) - (REQUIRED | {"doi", "publisher_date", "access_status"}):
            raise ValueError("unrecognized evidence record field")
        if not isinstance(r["id"], str) or not r["id"].startswith("P0-") or r["id"] in seen:
            raise ValueError("invalid or duplicate evidence id")
        seen.add(r["id"])
        if r["kind"] not in EVIDENCE_KINDS or r["environment"] not in ENVIRONMENTS:
            raise ValueError("unsupported evidence kind or environment")
        if r["kind"] in {"PROGRAM", "PATENT", "UNVERIFIED"} and r["environment"] != "NONE":
            raise ValueError("program, patent or unverified item cannot claim experimental validation")
        if not isinstance(r["source_url"], str) or not _check_source_url(r["source_url"]):
            raise ValueError("invalid provenance URL")
        for field in ("observed", "safe_next_step"):
            if not isinstance(r[field], str) or not r[field].strip():
                raise ValueError("empty evidence statement")
        if not isinstance(r["limitations"], list) or not r["limitations"] or any(
                not isinstance(s, str) or not s.strip() for s in r["limitations"]):
            raise ValueError("limitations must include at least one nonempty string")
    return {"schema": SCHEMA, "entries": len(records), "digest_sha256": sha256_json(catalog),
            "note": "Cryptographic digest of this document only; NOT a signature or independent verification"}


def simulate_only(request: object, state: object | None = None) -> dict:
    """Process a toy numerical state; refuses any real-world target or process payload."""
    if not isinstance(request, dict):
        return {"accepted": False, "reason": "invalid_request"}
    if set(request) != {"target", "command", "sequence", "uncertainty"}:
        return {"accepted": False, "reason": "unrecognized_or_missing_fields"}
    if request["target"] != SIM_TARGET or request["command"] not in SIM_COMMANDS:
        return {"accepted": False, "reason": "non_simulator_command_denied"}
    if not isinstance(request["sequence"], int) or isinstance(request["sequence"], bool) or request["sequence"] < 1:
        return {"accepted": False, "reason": "invalid_sequence"}
    uncertainty = request["uncertainty"]
    if not isinstance(uncertainty, (int, float)) or isinstance(uncertainty, bool) or not math.isfinite(uncertainty):
        return {"accepted": False, "reason": "invalid_uncertainty"}
    if not 0 <= uncertainty <= 1 or uncertainty > .20:
        return {"accepted": False, "reason": "uncertainty_hard_stop"}
    if state is None:
        state = {"sequence": 0, "virtual_mass": 9, "virtual_phase": 0}
    if not isinstance(state, dict) or set(state) != {"sequence", "virtual_mass", "virtual_phase"}:
        return {"accepted": False, "reason": "invalid_virtual_state"}
    if state["virtual_mass"] != 9 or not isinstance(state["virtual_phase"], int) or not 0 <= state["virtual_phase"] <= 3:
        return {"accepted": False, "reason": "conservation_or_state_guard"}
    if request["sequence"] != state["sequence"] + 1:
        return {"accepted": False, "reason": "replay_or_gap_detected"}
    if request["command"] == "SIMULATED_TICK" and state["virtual_phase"] >= 3:
        return {"accepted": False, "reason": "bounded_phase_guard"}
    next_state = {"sequence": request["sequence"],
                  "virtual_mass": state["virtual_mass"],
                  "virtual_phase": state["virtual_phase"] + (request["command"] == "SIMULATED_TICK")}
    return {"accepted": True, "scope": "OFFLINE_SIMULATION_ONLY", "state": next_state,
            "receipt_sha256": sha256_json({"request": request, "state": next_state})}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate public evidence, emit synthetic mock receipts")
    parser.add_argument("catalog", help="offline JSON evidence catalog")
    parser.add_argument("--demo", action="store_true", help="execute synthetic no-hardware simulation")
    args = parser.parse_args(argv)
    cat = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    summary = validate_catalog(cat)
    if args.demo:
        state = None
        trace = []
        for seq in range(1, 4):
            msg = {"target": SIM_TARGET, "command": "SIMULATED_TICK", "sequence": seq,
                   "uncertainty": 0.02}
            receipt = simulate_only(msg, state)
            if not receipt["accepted"]:
                raise RuntimeError("mock demo failed")
            state = receipt["state"]
            trace.append(receipt)
        summary["mock_trace"] = trace
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
