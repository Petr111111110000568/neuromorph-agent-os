from __future__ import annotations
from enum import Enum
from dataclasses import dataclass

class CapabilityClass(str, Enum):
    DATA_ANALYSIS = "data-analysis"
    SIMULATION = "simulation"
    CLINICAL_RESEARCH = "clinical-research"
    HUMAN_INTERVENTION = "human-intervention"

@dataclass
class SafetyDecision:
    allowed: bool
    capability: CapabilityClass
    human_gate_required: bool
    reason: str

class SafetyGateway:
    """Policy boundary between computational prototyping and intervention."""

    def classify_change(self, *, change_class: str, organ_prototype: bool = False) -> SafetyDecision:
        if organ_prototype:
            return SafetyDecision(
                allowed=True,
                capability=CapabilityClass.SIMULATION,
                human_gate_required=True,
                reason="Organ/system redesign is permitted only as an abstract digital-twin counterfactual.",
            )
        return SafetyDecision(
            allowed=True,
            capability=CapabilityClass.SIMULATION,
            human_gate_required=True,
            reason=f"Change class '{change_class}' is handled as simulation-only counterfactual analysis.",
        )
