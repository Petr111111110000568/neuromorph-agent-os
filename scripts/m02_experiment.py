"""Two finite cloud runs over synthetic data, no model/handler downloads."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPOSITORY = "Petr111111110000568/neuromorph-agent-os"
WORKFLOW = ".github/workflows/m02-checkpoint.yml"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def identity(run_id=None):
    result = {"repository": os.environ.get("GITHUB_REPOSITORY", ""),
              "workflow_id": WORKFLOW, "branch": os.environ.get("GITHUB_REF_NAME", ""),
              "commit": os.environ.get("GITHUB_SHA", ""),
              "run_id": run_id or os.environ.get("GITHUB_RUN_ID", "")}
    require(result["repository"] == REPOSITORY and result["branch"] == "main", "unsupported_producer_scope")
    require(re.fullmatch(r"[0-9a-f]{40}", result["commit"]) is not None, "invalid_commit")
    require(re.fullmatch(r"[1-9][0-9]{0,19}", result["run_id"]) is not None, "invalid_run_id")
    require(os.environ.get("GITHUB_WORKFLOW_REF") == f"{REPOSITORY}/{WORKFLOW}@refs/heads/main", "wrong_workflow_ref")
    return result


def validate_producer(parent, current, wanted):
    """GitHub metadata is authenticated independently of the artifact's hashes."""
    require(str(parent.get("id")) == wanted["run_id"], "producer_id_mismatch")
    require(parent.get("id") != current.get("id"), "self_resume_forbidden")
    for run in (parent, current):
        require(run.get("repository", {}).get("full_name") == wanted["repository"], "producer_repository_mismatch")
        require(run.get("head_repository", {}).get("full_name") == wanted["repository"], "producer_fork_forbidden")
        require(run.get("head_branch") == wanted["branch"], "producer_branch_mismatch")
        require(run.get("head_sha") == wanted["commit"], "producer_commit_mismatch")
        require(run.get("path", "").split("@")[0] == wanted["workflow_id"], "producer_workflow_path_mismatch")
        require(run.get("run_attempt") == 1, "rerun_not_supported")
    require(type(parent.get("workflow_id")) is int and parent["workflow_id"] == current.get("workflow_id"), "producer_workflow_id_mismatch")
    require(parent.get("status") == "completed" and parent.get("conclusion") == "success", "producer_not_successful")
    require(parent.get("event") in {"push", "workflow_dispatch"}, "producer_event_rejected")
    return wanted


def github_run(run_id):
    require(re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)) is not None, "invalid_run_id")
    request = urllib.request.Request(f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{run_id}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "NeuroMorf-M02",
                 "Authorization": "Bearer " + os.environ["GH_TOKEN"]})
    # Run metadata stays on api.github.com; credentials never follow redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError("metadata_redirect_rejected")
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
        data = response.read(1024 * 1024 + 1)
    require(len(data) <= 1024 * 1024, "metadata_too_large")
    return json.loads(data)


def verify_producer(output):
    previous = os.environ.get("PREVIOUS_RUN_ID", "")
    expected_sha = os.environ.get("EXPECTED_MANIFEST_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha) is not None, "manifest_digest_required")
    wanted = identity(previous)
    current = identity()
    validate_producer(github_run(previous), github_run(current["run_id"]), wanted)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(canonical({"identity": wanted, "manifest_sha256": expected_sha}) + "\n", encoding="utf-8")
    print("M02_PRODUCER_VERIFIED=" + canonical(wanted))


def public_state(dispatcher, queue):
    snapshot = dispatcher.snapshot()
    fields = ("task_id", "project_id", "revision", "handoff_id", "input_sha256",
              "queue_job_id", "current_result_sha256", "status", "parent_handoff")
    tasks = [{key: item.get(key) for key in fields} for item in snapshot["tasks"]]
    jobs = [{key: job.get(key) for key in ("id", "status", "attempts", "max_attempts")}
            for job in queue.jobs()]
    return {"tasks": sorted(tasks, key=lambda x: x["task_id"]),
            "jobs": sorted(jobs, key=lambda x: x["id"]), "budget": snapshot["budget"],
            "counts": snapshot["counts"], "reviews": snapshot["reviews"],
            "transitions": snapshot.get("events", [])}


def execute(output):
    from workbench.m02_checkpoint import create_checkpoint, restore_checkpoint
    from workbench.project_dispatcher import ProjectDispatcher, run_fixture_once
    from workbench.network.queue import Queue
    current = identity()
    checked = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(checked == current["commit"], "checkout_commit_mismatch")
    require(not output.exists(), "output_already_exists")
    previous = os.environ.get("PREVIOUS_RUN_ID", "")
    digest = os.environ.get("EXPECTED_MANIFEST_SHA", "")
    require(bool(previous) == bool(digest), "resume_inputs_incomplete")
    state_dir = output.parent / "m02-state"
    fixture = json.loads((ROOT / "config/m02_fixture.json").read_text(encoding="utf-8"))
    restored = None
    if previous:
        approved = json.loads((output.parent / "producer.json").read_text(encoding="utf-8"))
        wanted = identity(previous)
        require(approved == {"identity": wanted, "manifest_sha256": digest}, "producer_verification_missing")
        restored = restore_checkpoint(output.parent / "incoming", state_dir, digest, wanted)
        seed = json.loads((state_dir / "seed-receipt.json").read_text(encoding="utf-8"))
        require(seed.get("phase") == "seed" and seed.get("identity") == wanted, "resume_requires_seed_receipt")
    else:
        state_dir.mkdir(parents=True, exist_ok=False)
    queue = Queue(state_dir / "queue.sqlite")
    dispatcher = ProjectDispatcher(state_dir / "control.sqlite", queue)
    try:
        if not previous:
            for project in fixture["projects"]:
                dispatcher.create_project(project)
            for task in fixture["tasks"]:
                dispatcher.create_task(task)
            dispatcher.tick()
            run_fixture_once(queue)
            dispatcher.tick()
            ready = [t for t in dispatcher.snapshot()["tasks"] if t["current_result_sha256"]]
            require(len(ready) == 1, "seed_expected_one_result")
            first = ready[0]
            dispatcher.review(first["task_id"], expected_revision=first["revision"],
                result_sha256=first["current_result_sha256"], decision="accepted", note="Synthetic fixture matches fixed expected result.")
            dispatcher.tick()
            final = public_state(dispatcher, queue)
            require(final["budget"]["reserved_attempts"] == 4 and final["budget"]["actual_attempts"] == 1, "seed_budget_mismatch")
            require(sum(t["status"] == "accepted" for t in final["tasks"]) == 1, "seed_first_effect_missing")
            phase = "seed"
            initial = None
        else:
            initial = public_state(dispatcher, queue)
            require(initial == seed["state"], "restored_business_state_mismatch")
            require(initial["budget"]["reserved_attempts"] == 4 and initial["budget"]["actual_attempts"] == 1, "restored_budget_mismatch")
            pending = [t for t in initial["tasks"] if t["status"] != "accepted"]
            require(len(pending) == 1, "resume_expected_one_pending_task")
            task_id = pending[0]["task_id"]
            run_fixture_once(queue)
            dispatcher.tick()
            target = next(t for t in dispatcher.snapshot()["tasks"] if t["task_id"] == task_id)
            dispatcher.review(task_id, expected_revision=target["revision"], result_sha256=target["current_result_sha256"],
                decision="rejected", note="Synthetic review requests one bounded revision; no scientific acceptance implied.")
            dispatcher.tick()
            run_fixture_once(queue)
            dispatcher.tick()
            target = next(t for t in dispatcher.snapshot()["tasks"] if t["task_id"] == task_id)
            require(target["revision"] == 2, "rework_revision_missing")
            dispatcher.review(task_id, expected_revision=target["revision"], result_sha256=target["current_result_sha256"],
                decision="accepted", note="Fixed synthetic rework completed and lineage retained.")
            dispatcher.deliver_outbox()
            final = public_state(dispatcher, queue)
            require(all(t["status"] == "accepted" for t in final["tasks"]), "resume_acceptance_missing")
            require(final["budget"]["reserved_attempts"] == 6 and final["budget"]["actual_attempts"] == 3, "resume_budget_mismatch")
            phase = "resume"
        receipt = {"schema_version": 1, "phase": phase, "identity": current,
                   "previous_run_id": previous or None, "previous_manifest_sha256": digest or None,
                   "restored_file_hashes": restored["files"] if restored else None,
                   "initial_state": initial, "state": final,
                   "input_sha256": hashlib.sha256(canonical(fixture).encode()).hexdigest(),
                   "model_calls": 0, "additional_spend_usd": 0,
                   "limitations": ["Synthetic fixed fixtures, not model research or scientific validation.",
                       "One local coordinator, not a shared SQLite LAN database.",
                       "Queue delivery is at-least-once; no exactly-once remote effect claim.",
                       "Immutable local receipts; no external mutable publication pointer.",
                       "Artifact retention is finite; missing checkpoint never resets budget."]}
    finally:
        dispatcher.close()
        queue.close()
    (state_dir / "seed-receipt.json").write_text(canonical(receipt) + "\n", encoding="utf-8")
    checkpoint_sha = create_checkpoint(state_dir, output, current)
    print("M02_MANIFEST_SHA256=" + checkpoint_sha)
    print("M02_PUBLIC_RECEIPT=" + canonical(receipt))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("verify-producer", "run"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "verify-producer":
            verify_producer(args.output)
        else:
            execute(args.output)
    except (ValueError, KeyError, OSError, json.JSONDecodeError):
        # Never print tokens, signed artifact URLs, raw DB values or stack locals.
        print("M02_FAILED: validation or state transition refused; no budget reset", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
