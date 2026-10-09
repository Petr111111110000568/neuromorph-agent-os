# P0 / Nanits — SYNTH agent instructions
Primary goal: implement and verify a **digital, simulation-only prototype** of a configurable physical volume ("Synt"), inspired by the fictional Synt in A. Efremov's "Symbiosis-2". Never claim the fiction describes an existing biological technology.
## Execution chain
One canonical coordinator: P0/Nanits master. Muse acts as an **engineering worker**, Dots as a **review/coordination worker** only after native Dot access is actually confirmed. Do not create duplicated supervisors, recursive schedulers, paid inference loops or external accounts.
Read existing P0 worker returns W1-W6 and LM1-LM4; never restart an accepted research cycle. Do not change unrelated scripts in Documents. Preserve local Qwen GPU-only routing, avoid CPU fallback and respect the repository's zero-spend policy.
For a complex task, use one explicit Muse workflow with bounded helpers:
- SYNTH-EVIDENCE: verify official DARPA, ARPA-H, DIA, NIH, FDA documentation and original papers; identify performers, patents, DOI and negative evidence; read-only.
- SYNTH-CONTROLS: implement a finite fixed-volume simulator, sensor estimation, bounded simulated actuation and verification; sole writer for source files.
- SYNTH-CRITIC: independently check software tests, safety gates, claims and provenance; read-only.
- SYNTH-MASTER: fan in only verified WORKER_RETURN and record deficiencies; single canonical output writer.
Parallel writers require isolated Git worktrees; readers stay read-only.
## Nonnegotiable evidence gate
Every claim contains source URL/DOI, demonstrated effect, model organism/system, type CLEAR/PATENT/CLINICAL/REGULATORY, evidence level, negative results, boundary of inference and date verified. Patent, proposal and fiction NEVER equal a validated technology. Do not pretend dark-web, restricted documents or an authenticated service is reachable without observed success.
## Hard technical gates
Do not issue actuator commands to real devices or people. No wet-lab recipes, doses, sequence-level genetic designs, self-experiment instructions, escalation of system permissions, credential extraction or policy bypass. Model fields and tissue-state changes in software only.
No Muse Spark usage when missing Meta credentials or without an approved spending route; Muse echo does not prove a live model response. Dots account onboarding requires native entitlement.
## Output
Return WORKER_RETURN with CLAIMS_VERIFIED, NEW_ARTIFACTS, TESTS, NEGATIVE_RESULTS, CONTRADICTIONS, BLOCKERS, NEXT_MASTER_ACTIONS. Distinguish: installed / configured / authenticated / running / tested / accepted. Record checks with real exit codes and stdout, never infer success.
