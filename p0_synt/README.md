# P0 Synt — Muse/Dots deployment artifacts (7 October 2026)

This directory contains the initial **simulation-only** Synt digital twin for P0/Nanits. It is not a biological symbiont and issues no commands to physical actuators.

## Verified on the user's authorized Windows/WSL environment

- Muse Code CLI 1.4.3 is installed on Windows and Ubuntu 24.04 WSL.
- Muse project skill `p0-sint`: `muse skills validate .agents/skills/p0-sint` returned `valid p0-sint`.
- `muse exec --provider echo` completed. This verifies CLI dispatch only, **not model inference**.
- Live `muse exec` stopped with `missing meta credentials` on both Windows and WSL. Meta login/API key and approval of any charge are required; no paid calls were made.
- Local Python tests: **7 passed**, smoke test: **SIMULATED_AND_VERIFIED**, six fixed cells, eight steps, mass=1, target reached, SHA-256 event chain stored locally in `evidence/`.
- P0 local GPU gateway port 8790 was listening but `/health` returned HTTP 503 on the inspected attempt. No Qwen fallback to CPU was started.
- Native ChatGPT Dot is a cloud entitlement, not an installable Windows/WSL binary. No Dot creation or native authenticated connection was confirmed.

## Run the safe local validation

```bash
cd p0_synt
python3 -m unittest discover -s tests -v
python3 sint_sim.py smoke
muse skills validate .agents/skills/p0-sint
muse exec --provider echo --workspace . --trust-workspace "P0_SINT_ECHO_TEST"
```

The simulation's `evidence/` outputs should not be interpreted as scientific evidence or cryptographic immutability: SHA-256 chaining detects accidental/individual record alteration, not a full history rewrite.

## What remains to complete native deployment

1. Meta Muse Code authentication, approved spending controls or confirmed no-cost entitlement, and a real Muse Spark response with verifiable agent/tool trace.
2. Native Dot availability for this ChatGPT account; setup is through ChatGPT desktop web/desktop app only after entitlement is offered. See `DOT_BOOTSTRAP.md`.
3. Explicit connection and end-to-end test of agent-to-agent handoff. One P0 coordinator must remain canonical; Muse and Dot workers must not independently rewrite the master ledger.
4. The local GPU service must return healthy and verifiable inference before any Qwen worker is resumed.

Do not automatically relaunch prior disabled watchdogs or duplicate supervisors. Do not enable `--yolo`, paid inference, real actuators, human interventions or experimental genetic procedures.

Official sources:
- https://dev.meta.ai/docs/muse-code
- https://dev.meta.ai/docs/muse-code/extending
- https://help.openai.com/en/articles/20001530-getting-started-with-your-dot
