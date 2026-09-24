from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class ChangeClass(str, Enum):
    REGULATORY = "regulatory"
    CELL_STATE = "cell-state"
    TISSUE_ARCHITECTURE = "tissue-architecture"
    ORGAN_TOPOLOGY = "organ-topology"
    SYSTEM_COUPLING = "system-coupling"
    MULTISCALE = "multiscale"

class ConsequenceDimension(str, Enum):
    FUNCTION = "function"
    STRUCTURE = "structure"
    METABOLIC_LOAD = "metabolic-load"
    HOMEOSTASIS = "homeostasis"
    VASCULAR = "vascular"
    IMMUNE = "immune"
    NEURAL = "neural"
    ENDOCRINE = "endocrine"
    MECHANICAL = "mechanical"
    DEVELOPMENTAL = "developmental"
    SYSTEMIC = "systemic"
    UNCERTAINTY = "uncertainty"

@dataclass
class TraitImpact:
    trait: str
    baseline: float
    predicted: float
    confidence: float
    direction: str = "increase"
    rationale: str = ""

    @property
    def delta(self) -> float:
        return self.predicted - self.baseline

@dataclass
class PhenotypeDelta:
    feature: str
    delta: float
    confidence: float
    timescale: str = "long-term"
    emergent: bool = False
    affected_systems: list[str] = field(default_factory=list)

@dataclass
class OrganPrototype:
    organ_id: str
    role: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    topology_only: bool = True

@dataclass
class BiologicalSubject:
    subject_id: str
    genome_reference: str | None = None
    baseline_traits: dict[str, float] = field(default_factory=dict)
    baseline_systems: dict[str, float] = field(default_factory=dict)
    uncertainty: dict[str, float] = field(default_factory=dict)
    provenance: list[str] = field(default_factory=list)

@dataclass
class DigitalTwin:
    twin_id: str
    subject_id: str
    scales: list[str] = field(default_factory=lambda: [
        "molecular", "cellular", "tissue", "organ", "systemic"
    ])
    model_versions: list[str] = field(default_factory=list)
    calibrated: bool = False

@dataclass
class BiologicalChange:
    change_id: str
    label: str
    change_class: ChangeClass
    target_systems: list[str] = field(default_factory=list)
    target_traits: list[str] = field(default_factory=list)
    new_traits: list[str] = field(default_factory=list)
    organ_prototypes: list[OrganPrototype] = field(default_factory=list)
    abstract_intensity: float = 0.5
    reversibility: str = "unknown"
    rationale: str = ""
    # Deliberately abstract: this object contains no wet-lab, surgical,
    # delivery, dosing, sequence-editing, or procedural instructions.

@dataclass
class ConsequenceScore:
    dimension: ConsequenceDimension
    score: float
    confidence: float
    explanation: str

@dataclass
class ConsequenceReport:
    change_id: str
    status: str
    trait_impacts: list[TraitImpact] = field(default_factory=list)
    phenotype_deltas: list[PhenotypeDelta] = field(default_factory=list)
    system_scores: list[ConsequenceScore] = field(default_factory=list)
    compatibility_risks: list[str] = field(default_factory=list)
    emergent_properties: list[str] = field(default_factory=list)
    bottlenecks: list[str] = field(default_factory=list)
    uncertainty: float = 1.0
    model_disagreement: float = 0.0
    human_gate_required: bool = True
    notes: list[str] = field(default_factory=list)
