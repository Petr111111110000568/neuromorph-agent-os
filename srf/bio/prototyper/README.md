# Biological Systems Prototyper — v0.3

Simulation-only subsystem for SRF.

## What it adds

- consequence propagation across biological-system dimensions;
- current-trait deltas and candidate emergent traits;
- abstract organ/system prototypes as topology + function contracts;
- cross-system compatibility and bottleneck analysis;
- uncertainty and model-disagreement scoring;
- explicit `SIMULATION_ONLY` status and human-gate policy boundary.

## Important boundary

The module does **not** encode wet-lab protocols, surgical procedures, delivery methods,
dosing, sequence-editing instructions, or a path from a digital-twin result to autonomous
human modification.

The intended loop is:

`baseline → abstract counterfactual → multiscale simulation → consequence report → evidence/replication → human research gate`
