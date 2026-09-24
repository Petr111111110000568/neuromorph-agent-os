from __future__ import annotations
from collections import defaultdict
from .models import (
    BiologicalSubject, DigitalTwin, BiologicalChange, ConsequenceReport,
    TraitImpact, PhenotypeDelta, ConsequenceScore, ConsequenceDimension,
    ChangeClass,
)

SYSTEM_MAP = {
    "cardiovascular": [ConsequenceDimension.VASCULAR, ConsequenceDimension.SYSTEMIC],
    "respiratory": [ConsequenceDimension.METABOLIC_LOAD, ConsequenceDimension.SYSTEMIC],
    "immune": [ConsequenceDimension.IMMUNE, ConsequenceDimension.HOMEOSTASIS],
    "neural": [ConsequenceDimension.NEURAL, ConsequenceDimension.METABOLIC_LOAD],
    "endocrine": [ConsequenceDimension.ENDOCRINE, ConsequenceDimension.HOMEOSTASIS],
    "musculoskeletal": [ConsequenceDimension.MECHANICAL, ConsequenceDimension.METABOLIC_LOAD],
    "digestive": [ConsequenceDimension.METABOLIC_LOAD, ConsequenceDimension.HOMEOSTASIS],
}

class ConsequenceEngine:
    """Counterfactual consequence engine for digital-twin experiments.

    It estimates *model-level* consequences and uncertainty. It is not an
    intervention planner and intentionally has no operational biological
    implementation layer.
    """

    def analyze(
        self,
        subject: BiologicalSubject,
        twin: DigitalTwin,
        change: BiologicalChange,
    ) -> ConsequenceReport:
        intensity = max(0.0, min(1.0, change.abstract_intensity))
        report = ConsequenceReport(change_id=change.change_id, status="SIMULATION_ONLY")

        # Current-trait changes: explicit target traits receive a model-level
        # directional delta; values are normalized rather than physical doses.
        for trait in change.target_traits:
            base = subject.baseline_traits.get(trait, 0.5)
            delta = round(0.25 * intensity, 3)
            report.trait_impacts.append(
                TraitImpact(
                    trait=trait,
                    baseline=base,
                    predicted=max(0.0, min(1.0, base + delta)),
                    confidence=0.45 if not twin.calibrated else 0.70,
                    direction="increase",
                    rationale="Counterfactual model perturbation; requires empirical calibration.",
                )
            )

        # New traits are represented as candidate phenotype features, not as
        # claims that the feature can actually be created in a human.
        for trait in change.new_traits:
            report.phenotype_deltas.append(
                PhenotypeDelta(
                    feature=trait,
                    delta=round(0.15 + 0.35 * intensity, 3),
                    confidence=0.25 if not twin.calibrated else 0.45,
                    emergent=True,
                    affected_systems=list(change.target_systems),
                )
            )
            report.emergent_properties.append(trait)

        # Organ prototypes are topology/function contracts only.
        for organ in change.organ_prototypes:
            report.emergent_properties.append(
                f"candidate organ-system function: {organ.organ_id} — {organ.role}"
            )
            if len(organ.dependencies) < 2:
                report.bottlenecks.append(
                    f"{organ.organ_id}: insufficient dependency specification in the model"
                )

        # System-level consequence propagation.
        dim_acc = defaultdict(list)
        for system in change.target_systems:
            for dim in SYSTEM_MAP.get(system.lower(), [ConsequenceDimension.SYSTEMIC]):
                dim_acc[dim].append(system)

        # Structural/topological changes carry broader uncertainty.
        if change.change_class in {ChangeClass.ORGAN_TOPOLOGY, ChangeClass.SYSTEM_COUPLING}:
            dim_acc[ConsequenceDimension.STRUCTURE].append("topology")
            dim_acc[ConsequenceDimension.HOMEOSTASIS].append("coupling")
            dim_acc[ConsequenceDimension.SYSTEMIC].append("whole-body coupling")

        for dim, systems in dim_acc.items():
            base_score = 0.25 + 0.55 * intensity
            if dim in {ConsequenceDimension.HOMEOSTASIS, ConsequenceDimension.SYSTEMIC}:
                base_score += 0.10
            confidence = 0.35 if not twin.calibrated else 0.60
            report.system_scores.append(
                ConsequenceScore(
                    dimension=dim,
                    score=min(1.0, round(base_score, 3)),
                    confidence=confidence,
                    explanation=f"Propagated from modeled systems: {', '.join(sorted(set(systems)))}",
                )
            )

        # Generic cross-system checks.
        if len(change.target_systems) >= 2:
            report.compatibility_risks.append(
                "Cross-system coupling: changing multiple systems may create nonlinear interactions."
            )
        if change.organ_prototypes:
            report.compatibility_risks.extend([
                "Topology compatibility must be checked against vascular, neural, immune and metabolic models.",
                "New system functions may create resource competition and feedback loops.",
                "Adult-state integration is structurally underdetermined without longitudinal calibration data.",
            ])

        uncertainty_terms = [
            subject.uncertainty.get("genome", 0.3),
            subject.uncertainty.get("phenotype", 0.4),
            0.25 if twin.calibrated else 0.55,
        ]
        if change.change_class in {ChangeClass.ORGAN_TOPOLOGY, ChangeClass.MULTISCALE}:
            uncertainty_terms.append(0.80)
        if change.organ_prototypes:
            uncertainty_terms.append(0.85)
        report.uncertainty = round(min(1.0, sum(uncertainty_terms) / len(uncertainty_terms)), 3)
        if change.organ_prototypes:
            report.uncertainty = max(report.uncertainty, 0.72)
        report.model_disagreement = round(min(1.0, report.uncertainty * 0.9), 3)

        if report.uncertainty > 0.65:
            report.notes.append("High uncertainty: candidate should remain in counterfactual/simulation branch.")
        if not twin.calibrated:
            report.notes.append("Digital twin is not calibrated; predictions are exploratory.")
        report.notes.append("No operational human-intervention instructions are generated.")
        return report
