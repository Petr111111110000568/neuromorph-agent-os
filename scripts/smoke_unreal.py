#!/usr/bin/env python3
"""Test the installed real Unreal binary through Meta-Harness and a loopback fixture.

No external AI API is contacted. A deterministic HTTP fixture supplies a Responses
SSE event; it is protocol evidence, not an AI research result.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.harnesses import HarnessRegistry
from workbench.harnesses import runner

TEXT = "META_HARNESS_UNREAL_PROTOCOL_OK"


def run_smoke():
    requests, observed, errors = [], {}, []

    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 64 * 1024:
                    raise ValueError("unexpected_request_size")
                body = json.loads(self.rfile.read(size))
                if self.path != "/v1/responses":
                    raise ValueError("unexpected_provider_path")
                if self.headers.get("Authorization") != "Bearer meta-harness-local-protocol-test":
                    raise ValueError("expected_synthetic_credential_only")
                if body.get("tools") not in (None, []):
                    raise ValueError("tools_were_exposed")
                if body.get("stream") is not True or body.get("store") is not False:
                    raise ValueError("unexpected_provider_contract")
                requests.append({"method": "POST", "path": self.path, "body": body,
                                 "synthetic_credential_verified": True})
                response = {"id": "resp-meta-harness-fixture", "status": "completed", "output": [
                    {"id": "msg-meta-harness-fixture", "type": "message", "role": "assistant",
                     "status": "completed", "phase": "final_answer",
                     "content": [{"type": "output_text", "text": TEXT}]}],
                    "usage": {"input_tokens": 12, "output_tokens": 8}}
                payload = ("event: response.completed\ndata: " + json.dumps({
                    "type": "response.completed", "response": response}) + "\n\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (ValueError, TypeError, KeyError) as error:
                errors.append(str(error))
                self.send_error(400, "fixture contract failure")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = runner.capture

    def observe_real_process(*args, **kwargs):
        result = original(*args, **kwargs)
        observed["stdout_sha256"] = hashlib.sha256(result["stdout"]).hexdigest()
        observed["stderr_bytes"] = len(result["stderr"])
        observed["raw_events"] = [json.loads(line) for line in result["stdout"].splitlines() if line.strip()]
        # Stderr is useful on failure; it contains no inherited real credentials.
        if result["exit_code"] != 0:
            observed["stderr"] = result["stderr"].decode("utf-8", errors="replace")[:5000]
        return result

    try:
        with patch.object(runner, "capture", side_effect=observe_real_process):
            result = HarnessRegistry(ROOT).run("unreal", "Return the protocol fixture answer.",
                model="mock-model", environment={}, timeout_seconds=30,
                protocol_test_base_url=f"http://127.0.0.1:{server.server_address[1]}/v1")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    events = observed.get("raw_events", [])
    event_types = dict(Counter(event.get("Kind", "unknown") for event in events))
    success = (result.get("status") == "completed" and result.get("output_text") == TEXT
               and len(requests) == 1 and not errors and event_types.get("model_response") == 1
               and not event_types.get("tool_call_status"))
    receipt_path = ROOT / "runtime/harnesses/unreal-build.json"
    build_receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
    return {"schema_version": 1, "harness_id": "unreal", "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "protocol_tested" if success else "protocol_test_failed", "protocol_tested": success,
            "live_authenticated": False, "external_model_requests": 0, "fixture_http_requests": len(requests),
            "tools_advertised": 0 if requests and all(not q["body"].get("tools") for q in requests) else None,
            "generated_code_executed": False, "upstream_build": build_receipt,
            "adapter_result": result, "requests": requests, "event_types": event_types,
            "observed_process": observed, "fixture_errors": errors,
            "limits": ["Local deterministic fixture; no live model inference or account authentication.",
                       "Process limits are not an operating-system security sandbox.",
                       "Successful protocol exchange does not validate scientific claims."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "data/unreal_validation.json"))
    args = parser.parse_args(argv)
    report = run_smoke()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "protocol_tested", "live_authenticated", "fixture_http_requests", "event_types")}, ensure_ascii=False))
    return 0 if report["protocol_tested"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
