# SRF Plugin Fabric v0.3

Adds the Biological Systems Prototyper / Consequence Engine.

Core computational objects:
- `BiologicalSubject`
- `DigitalTwin`
- `BiologicalChange`
- `OrganPrototype`
- `ConsequenceReport`

The engine evaluates:
1. how a counterfactual change alters modeled existing traits;
2. candidate new/emergent traits;
3. system-level consequences and coupling;
4. structural/topological bottlenecks for abstract organ prototypes;
5. uncertainty and model disagreement;
6. provenance/safety status.

This release is intentionally simulation-only and does not generate operational instructions for modifying a human organism.
