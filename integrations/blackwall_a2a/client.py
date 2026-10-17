"""Operator-invoked loopback client for the Blackwall A2A HTTP+JSON subset."""
from __future__ import annotations
import argparse
import json
import os
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .bridge import CMD_RE, canonical


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Remote redirects are forbidden")


def request(url, token=None, body=None):
    headers = {"A2A-Version": "1.0", "Accept": "application/a2a+json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        headers["Content-Type"] = "application/a2a+json"
        data = canonical(body).encode("utf-8")
    req = Request(url, headers=headers, data=data,
                  method="POST" if data is not None else "GET")
    with build_opener(NoRedirect()).open(req, timeout=6) as response:
        raw = response.read(90001)
    if len(raw) > 90000:
        raise RuntimeError("Unexpected oversized peer response")
    return json.loads(raw)


def run(port, command, token):
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid local port")
    if not isinstance(command, str) or not CMD_RE.fullmatch(command):
        raise ValueError("Only fixed read-only commands supported")
    if not token or len(token) < 32:
        raise ValueError("BLACKWALL_A2A_TOKEN >=32 chars is required")
    base = f"http://127.0.0.1:{port}"
    card = request(base + "/.well-known/agent-card.json")
    supported = card.get("supportedInterfaces", [])
    if not any(i.get("protocolBinding") == "HTTP+JSON" and
               i.get("protocolVersion") == "1.0" and i.get("url") == base
               for i in supported):
        raise ValueError("Agent card did not advertise this exact local interface")
    message_id = str(uuid.uuid4())
    envelope = {"message": {"messageId": message_id, "role": "ROLE_USER",
                             "parts": [{"text": command}]},
                "configuration": {"acceptedOutputModes": ["application/json"]}}
    result = request(base + "/message:send", token, envelope)
    task = result["task"]
    if task["status"]["state"] != "TASK_STATE_COMPLETED":
        return {"task": task, "verified": False}
    from_get = request(base + "/tasks/" + task["id"], token)
    if from_get != task:
        raise RuntimeError("Task readback mismatch")
    return {"task": task, "verified": True}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8791)
    p.add_argument("--command", choices=["status", "audit", "cognition", "plan 4 2"],
                   default="cognition")
    args = p.parse_args(argv)
    result = run(args.port, args.command, os.environ.get("BLACKWALL_A2A_TOKEN", ""))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
