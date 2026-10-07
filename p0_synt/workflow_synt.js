export const meta = {
  name: "p0-synt",
  description: "Bounded, single-master, simulation-only Synt evidence-implement-verify workflow.",
  phases: ["evidence", "implement", "verify", "report"]
};

const mission = "P0/Nanits: simulate only transitions of a persistent physical volume. " +
  "Read AGENTS.md and existing P0 worker returns; continue latest checkpoint. " +
  "No physical actuators, genetic interventions, wet-lab, API spending, unauthorized access, " +
  "policy bypass, recursive scheduling or duplicated supervisors. ";

phase("evidence");
const evidence = await agent(
  mission +
  "Role SYNTH-EVIDENCE: read-only. Verify original governmental documents (DARPA/DIA/ARPA-H/NIH/FDA), " +
  "papers, patents. Return a JSON object with outcome as pass or blocked, claims including URLs/DOIs, " +
  "negative evidence and blockers. No fabricated sources.",
  {key:"sint-evidence",label:"Synt: evidence",phase:"evidence",timeoutMs:600000}
);
if (!evidence || evidence.outcome !== "pass") {
  return {status:"blocked",stage:"evidence",evidence:evidence??null};
}
phase("implement");
const implementation = await agent(
  mission +
  "Role SYNTH-CONTROLS: only source writer. Implement one reproducible increment to p0_synt/sint_sim.py " +
  "and related tests. Run Python deterministic tests and return JSON with outcome pass or blocked, " +
  "changed_files, test commands, actual exitcodes and blockers. Only software simulation, no devices.",
  {key:"sint-implementation",label:"Synt: simulation",phase:"implement",timeoutMs:900000}
);
if (!implementation || implementation.outcome !== "pass") {
  return {status:"blocked",stage:"implement",evidence,implementation:implementation??null};
}
phase("verify");
const review = await agent(
  mission +
  "Role SYNTH-CRITIC: read-only independent verification. Re-run tests, inspect mass conservation, " +
  "containment, claim support, fault handling and zero-spend. Return JSON with outcome pass or blocked, " +
  "tests, negative_results, contradictions and blockers.",
  {key:"sint-critic",label:"Synt: independent critique",phase:"verify",timeoutMs:600000}
);
phase("report");
return {
  status:review && review.outcome==="pass"?"verified":"blocked",
  mode:"SIMULATION_ONLY",
  controller:"P0/Nanits",
  evidence,
  implementation,
  review:review??null,
  return_fields:["CLAIMS_VERIFIED","NEW_ARTIFACTS","TESTS","NEGATIVE_RESULTS","CONTRADICTIONS","BLOCKERS","NEXT_MASTER_ACTIONS"]
};
