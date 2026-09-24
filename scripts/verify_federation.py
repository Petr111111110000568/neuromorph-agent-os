"""Prove the opt-in exchange over real loopback HTTP and separate CLI processes.

No external agent, Tor service, human genome, LLM or external network is used.
The only contribution is deterministic QC of synthetic aggregate metadata.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.federation.exchange import Exchange
from workbench.federation.transport import make_gateway


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def ensure_sanitized(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in {"invite_token", "member_token", "token_hash", "authorization", "api_key", "password"}:
                raise AssertionError("Proof contains a credential field")
            ensure_sanitized(item)
    elif isinstance(value, list):
        for item in value:
            ensure_sanitized(item)


def verify():
    process_ids = []
    observed_steps = []
    proof = None
    with tempfile.TemporaryDirectory(prefix="meta-federation-verify-") as folder:
        folder = Path(folder)
        exchange = Exchange(folder / "exchange.sqlite3")
        server = None
        thread = None
        try:
            offer = exchange.create_offer({
                "title": "Demo: synthetic aggregate metadata QC",
                "description": "Count records and missing values in a fixed synthetic aggregate table. No genome analysis.",
                "task_type": "dataset_qc", "requirements": ["Reproducible counts", "State demo limitations"],
                "reward_credits": 5, "max_assignments": 1, "data_class": "public",
            })
            resource_content = "Demo research summary: distinguish observed computations, benchmark validation and biological claims."
            resource = exchange.add_resource({
                "title": "Demo reproducibility summary", "summary": "Locally authored test content, no personal data.",
                "content": resource_content, "license": "CC0-1.0", "data_class": "public",
                "rights_confirmed": True, "redistribution_allowed": True,
                "kind": "research_summary", "cost_credits": 3,
            })
            invitation = exchange.create_invitation({"offer_id": offer["offer_id"]})
            invitation_path = folder / "invitation.json"
            fd = os.open(invitation_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(canonical(invitation))
            credentials = folder / "member.json"
            server = make_gateway(exchange, host="127.0.0.1", port=0)
            url = f"http://127.0.0.1:{server.server_address[1]}"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def run(arguments, *, expected_status=None, token=None):
                environment = dict(os.environ)
                for name in ("META_MEMBER_TOKEN", "META_INVITE_TOKEN", "META_FEDERATION_URL"):
                    environment.pop(name, None)
                if token is not None:
                    environment["META_MEMBER_TOKEN"] = token
                command = [sys.executable, "-m", "workbench.federation.client", "--url", url,
                           "--credentials", str(credentials), *arguments]
                with subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                    process_ids.append(process.pid)
                    try:
                        stdout, stderr = process.communicate(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
                        raise RuntimeError("Participant subprocess timed out") from None
                if invitation["invite_token"] in stdout + stderr:
                    raise AssertionError("Invitation credential leaked to subprocess output")
                if expected_status is None:
                    if process.returncode != 0:
                        # Do not echo request bodies or subprocess error contents.
                        raise AssertionError(f"Participant command {arguments[0]} failed (exit {process.returncode})")
                    value = json.loads(stdout)
                    ensure_sanitized(value)
                else:
                    if process.returncode == 0:
                        raise AssertionError("Expected access denial")
                    value = json.loads(stderr)
                    assert value["status"] == expected_status, "Unexpected denial status"
                observed_steps.append({"command": arguments[0], "exit_code": process.returncode,
                                       "http_denial": expected_status})
                return value

            offers = run(["offers"])
            assert any(item["offer_id"] == offer["offer_id"] for item in offers["items"])
            resources = run(["resources"])
            assert resource_content not in canonical(resources), "Public catalog leaked gated content"
            joined = run(["join", "--name", "Synthetic QC reference participant", "--capabilities", "dataset_qc",
                          "--invitation-file", str(invitation_path), "--accept-terms"])
            assert joined["credentials_saved"] is True
            assert run(["member"])["credits"] == 0
            run(["access", resource["resource_id"]], expected_status=403)
            run(["member"], expected_status=403, token="invalid-test-credential-" + "x" * 32)
            claimed = run(["claim", offer["offer_id"], "--idempotency-key", "demo-qc-v1"])
            replay = run(["claim", offer["offer_id"], "--idempotency-key", "demo-qc-v1"])
            assignment_id = claimed["assignment_id"]
            assert replay["assignment_id"] == assignment_id

            synthetic_rows = [{"group": "synthetic-A", "records": 10, "missing": 1},
                              {"group": "synthetic-B", "records": 15, "missing": 0},
                              {"group": "synthetic-C", "records": 5, "missing": 2}]
            record_count = sum(row["records"] for row in synthetic_rows)
            missing_count = sum(row["missing"] for row in synthetic_rows)
            result = {"kind": "demo_synthetic_aggregate_metadata_qc", "is_genomic_analysis": False,
                      "rows": len(synthetic_rows), "records": record_count, "missing": missing_count,
                      "missing_fraction": missing_count / record_count,
                      "input_sha256": hashlib.sha256(canonical(synthetic_rows).encode()).hexdigest()}
            submission = {"result": result,
                          "source_refs": ["urn:meta-harness:demo:synthetic-aggregate-metadata:v1"],
                          "limitations": ["Synthetic aggregate fixture only; no human genomic data.",
                                          "Demonstrates software exchange, not scientific or biological validation."]}
            submission_path = folder / "submission.json"
            submission_path.write_text(canonical(submission), encoding="utf-8")
            submitted = run(["submit", assignment_id, str(submission_path)])
            assert submitted["status"] == "submitted"
            assert run(["member"])["credits"] == 0, "Submission must not receive a reward before review"
            local_review = exchange.review_queue()["items"]
            assert len(local_review) == 1 and local_review[0]["result"] == result
            # Here the local test harness is the operator's reviewer; it verifies
            # the fixed demo values. No external participant can approve itself.
            assert result["records"] == 30 and result["missing"] == 3 and result["missing_fraction"] == .1
            review_body = {"assignment_id": assignment_id, "decision": "accepted",
                           "rationale": "Verified deterministic synthetic aggregate fixture: 30 records, 3 missing. Demo only."}
            exchange.review(review_body)
            balance_after_review = run(["member"])["credits"]
            assert balance_after_review == 5
            exchange.review(review_body)
            resubmitted = run(["submit", assignment_id, str(submission_path)])
            assert resubmitted["status"] == "accepted"
            assert run(["member"])["credits"] == 5, "Replay minted an extra reward"
            redeemed = run(["redeem", resource["resource_id"]])
            assert redeemed["granted"] is True and redeemed["charged_credits"] == 3 and redeemed["credits"] == 2
            access = run(["access", resource["resource_id"]])
            assert access["content"] == resource_content
            replay_redemption = run(["redeem", resource["resource_id"]])
            assert replay_redemption["charged_credits"] == 0 and replay_redemption["credits"] == 2
            exported = exchange.export()
            ensure_sanitized(exported)
            reward_entries = [item for item in exported["ledger"] if item["kind"] == "reward"]
            redemption_entries = [item for item in exported["ledger"] if item["kind"] == "redeem"]
            assert len(reward_entries) == len(redemption_entries) == 1
            assert exchange.status()["rewarded_credits"] == 5
            assert exchange.status()["redeemed_credits"] == 3
            proof = {
                "passed": True, "checked_at": datetime.now(timezone.utc).isoformat(),
                "protocol": "meta-harness-contribution/1", "transport": "real_loopback_http",
                "participant_execution": "separate_cli_process_per_command",
                "participant_subprocesses": len(process_ids), "distinct_observed_process_ids": len(set(process_ids)),
                "steps": observed_steps, "terms_explicitly_accepted": True,
                "invitation_file_used": True, "credential_file_protected": True,
                "unauthorized_access_denied": True, "invalid_member_denied": True,
                "public_catalog_hides_resource_content": True,
                "claim_idempotency_verified": True, "reward_requires_local_review": True,
                "duplicate_reward_prevented": True, "duplicate_redemption_not_charged": True,
                "reviewed_demo": result, "reward_entries": len(reward_entries),
                "redemption_entries": len(redemption_entries), "earned_credits": 5,
                "spent_credits": 3, "remaining_credits": 2, "resource_access_verified": True,
                "export_without_credentials_verified": True, "external_network_requests": 0,
                "external_agents_contacted": 0, "external_agents_recruited": 0,
                "human_genomic_data_used": False, "biological_validation": "not_performed",
                "physical_multihost_test": "not_performed", "tls_test": "not_performed",
            }
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError("Gateway thread did not stop")
            exchange.close()
    assert proof is not None
    proof["process_cleanup"] = "all_participant_processes_and_local_gateway_stopped"
    ensure_sanitized(proof)
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write only the sanitized proof to this path")
    args = parser.parse_args()
    result = verify()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("passed", "participant_subprocesses", "duplicate_reward_prevented",
          "resource_access_verified", "external_agents_contacted", "process_cleanup")}, ensure_ascii=False, indent=2))
