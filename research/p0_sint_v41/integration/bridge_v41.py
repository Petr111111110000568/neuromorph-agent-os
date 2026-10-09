"""P0 v41: VIRTUAL_ONLY composition of existing v40 evidence gate and v1 shape surrogate.

No biological control, genetic design or physical output. Fixtures have no clinical meaning.
"""
from __future__ import annotations
from pathlib import Path
import hashlib
import json
from baseline_v40.src.organ_safety_gate_v40 import OrganPreflight, fake_frame, simulated_command
from geometry_v1.sint_sim.core import SCENARIOS, run_scenario


# Entirely artificial mapping from software faults to synthetic monitor inputs.
# It is NOT an empirical model of tissue physiology or actinomycete behavior.
MOCK_MAPPING = {
    "localization_loss": {"identity_confidence": 0.40},
    "containment_breach": {"off_target": 0.40},
    "thermal_drift": {"inflammation": 0.40},
    "actuator_saturation": {"burden": 0.40},
    "sensor_dropout": {"welfare_witness": 0.50},
    "uncertainty_spike": {"uncertainty": 0.40},
    "replayed_telemetry": {},  # Explicitly create a step-mismatched frame.
}


def bridge_scenario(name: str, steps: int = 90) -> dict:
    geometry = run_scenario(name, steps)
    gate = OrganPreflight()
    outcomes = []
    first_gate_hold = None
    for record in geometry["trace"]:
        step = record["telemetry"]["step"]
        # Not linked to any real actuator; v40 accepts only simulated noop/observe.
        cmd = simulated_command(gate.key, step, step + 1, "SIMULATED_SIGNAL_NOOP")
        accepted, reason = gate.inspect_command(cmd)
        mock = MOCK_MAPPING.get(name, {}) if step >= 18 else {}
        stamp = step - 1 if step >= 18 and name == "replayed_telemetry" else step
        frame = fake_frame(stamp, **mock)
        decision = gate.inspect_frame(frame)
        if decision.decision == "VIRTUAL_HOLD" and first_gate_hold is None:
            first_gate_hold = step
        outcomes.append({
            "step": step,
            "geometry_stopped": record["command"]["latched_stop"],
            "v40_command_accepted": accepted,
            "v40_command_reason": reason,
            "v40_decision": decision.decision,
            "v40_hold_reasons": list(decision.reasons),
            "physical_release": False,
        })
    assert not any(x["physical_release"] for x in outcomes)
    assert gate.journal.validate()
    return {
        "name": name,
        "scenarios_joined": "v40 fictional monitor + v1 virtual volume-conserving shape model",
        "steps": steps,
        "geometry_first_stop": geometry["first_stop_step"],
        "v40_first_hold": first_gate_hold,
        "geometry_area_ratio": geometry["initial_and_final_area_ratio"],
        "geometry_audit_valid": geometry["audit_valid"],
        "v40_audit_valid": gate.journal.validate(),
        "virtual_only": True,
        "real_world_model_calibrated": False,
        "real_world_actuation_enabled": False,
        "live_animal_approved": False,
        "both_halted_after_fault": all(x["geometry_stopped"] and x["v40_decision"] == "VIRTUAL_HOLD" for x in outcomes[18:]) if name != "nominal" else False,
        "v40_journal_root": gate.audit()["root"],
        "geometry_audit_root": geometry["audit_tip_sha256"],
        "outcomes": outcomes,
    }


def run_all(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    all_results = [bridge_scenario(name) for name in SCENARIOS]
    summary = {"revision": "v0.41", "execution": "SIMULATION_ONLY", "cases": [],
               "real_world_actuation_enabled": False, "organogenesis_verified": False}
    for item in all_results:
        (output / (item["name"]+"_bridge.json")).write_text(json.dumps(item, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        summary["cases"].append({k: v for k, v in item.items() if k not in ("outcomes", "v40_journal_root", "geometry_audit_root")})
    data = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2)
    (output / "summary_v41.json").write_text(data, encoding="utf-8")
    summary["summary_sha256"] = hashlib.sha256(data.encode("utf-8")).hexdigest()
    return summary
