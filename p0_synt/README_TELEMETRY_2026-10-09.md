# P0/SINT: virtual telemetry adapter (2026-10-09)

This is a **delta** to the existing `p0_synt/` simulator in draft PR #23, not a replacement and not a physical or biological prototype.

## What actually runs

`sim_telemetry_adapter.py` reads two synthetic observation frames from a local JSON file, rejects non-simulation modes and unknown fields, checks simulated sensor agreement, enforces monotonic frame indices if a previous frame is supplied, validates a conserved virtual quantity and caps simulated transfer. It reuses the existing `SyntVolume` loop and checks the SHA-256 event chain. Outputs one **inert** JSON report.

```bash
cd p0_synt
python -m unittest discover -s tests -v
python sim_telemetry_adapter.py --input virtual_replay_sample.json --output virtual_replay_report.json
```

## Demonstrated vs missing

- `SIMULATION_ONLY` is the only input mode. `genomic_output=false`, `physical_actuation=false`, `biological_validation=false` are intrinsic report fields.
- Inputs and outputs are **not** SBOL3 device-ready genomes, live MQTT/CoAP commands, animal intervention prescriptions, or a remotely accessible laboratory workflow.
- Two channels are **not** independent hardware sensors and are not validated against experimental measurements. The abstract `volume_cells` and `max_transfer` are dimensionless and cannot be used as real doses or safety limits.
- The SHA-256 chain detects edits if trusted anchor is retained. It is not a tamper-proof WORM archive or a calibrated/regulated medical record.
- The `last_frame` API gate applies only when called with a trusted previous frame; standalone CLI does **not** persist a replay ledger across separate invocations.
- This update neither installs third-party packages nor connects to GitHub Actions, local GPU, MADSci device nodes, external labs, or live animals.

## Primary/source inventory

`EVIDENCE_DELTA_2026-10-09.json` adds a 2026 clinical xenograft outcome, a 2026 official ARPA-H performer update, and a checked public lab-software integration framework. It also rechecks prior peer-reviewed pig capsule, electromagnetic mouse implant, BioLAN, embryo precursor, DARPA and exchange standards. No organism genomes, DNA/RNA sequences, synthesis-ready recipes, execution protocols or laboratory equipment instructions are supplied.