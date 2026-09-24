"""One bounded request to a fixed, explicitly credentialed inference endpoint."""
from __future__ import annotations

import http.client
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ENDPOINTS = {
    "openai": "https://api.openai.com/v1/responses",
    "anthropic": "https://api.anthropic.com/v1/messages",
}
KEY_NAMES = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
MAX_RESPONSE_BYTES = 192 * 1024
TIMEOUT_SECONDS = 45


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Inference redirects are disabled")


def json_load(raw):
    """Strict JSON for untrusted model output and configuration, with bounds."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("JSON byte limit exceeded")
        raw = raw.decode("utf-8")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("JSON byte limit exceeded")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON constant")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 24 or count > 12000:
            raise ValueError("JSON structure limit exceeded")
        if isinstance(item, str):
            if any(ord(c) < 32 and c not in "\n\r\t" for c in item):
                raise ValueError("Control character rejected")
            item.encode("utf-8")  # Reject unpaired Unicode surrogates.
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite JSON number")
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
        elif isinstance(item, dict):
            stack.extend((v, depth + 1) for pair in item.items() for v in pair)
    return value


def select_provider(requested, environment):
    if requested not in {"auto", "none", *ENDPOINTS}:
        raise ValueError("Unsupported inference provider")
    if requested == "auto":
        return next((p for p, key in KEY_NAMES.items() if environment.get(key, "").strip()), "none")
    return requested


def call_model(provider, settings, instructions, context, token):
    """Never retry, follow redirects, execute tools, or return a raw error body."""
    if provider not in ENDPOINTS:
        raise ValueError("Unsupported inference provider")
    if not token or len(token) > 2048 or any(c.isspace() for c in token):
        return {"status": "blocked_provider_missing", "requests": 0, "response_received": False}
    common = {"model": settings["model"]}
    user_text = json.dumps(context, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if provider == "openai":
        body = dict(common, instructions=instructions, input=user_text,
                    max_output_tokens=settings["max_output_tokens"], store=False,
                    text={"format": {"type": "json_object"}})
        auth = {"Authorization": "Bearer " + token}
    else:
        body = dict(common, system=instructions,
                    messages=[{"role": "user", "content": user_text}],
                    max_tokens=settings["max_output_tokens"])
        auth = {"x-api-key": token, "anthropic-version": "2023-06-01"}
    request = Request(ENDPOINTS[provider], data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json", "Accept": "application/json",
                               "User-Agent": "Meta-Harness-Autonomy/0.9", **auth}, method="POST")
    result = {"status": "request_failed", "requests": 1, "response_received": False,
              "endpoint": ENDPOINTS[provider], "provider": provider, "model": settings["model"]}
    try:
        # No environment proxy discovery: no unapproved proxy gets an API credential.
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200 or response.geturl() != ENDPOINTS[provider]:
                raise ValueError("Non-success or redirected inference response")
            if response.headers.get_content_type() != "application/json":
                raise ValueError("Non-JSON inference response")
            if response.headers.get("Content-Encoding", "identity").lower() not in {"identity", ""}:
                raise ValueError("Compressed inference response rejected")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            result["response_received"] = True
            payload = json_load(raw)
        if not isinstance(payload, dict):
            raise ValueError("Expected inference object")
        if provider == "openai":
            if payload.get("status") != "completed":
                return dict(result, status="incomplete_response")
            pieces = [part.get("text", "") for item in payload.get("output", []) if isinstance(item, dict)
                      and item.get("type") == "message" for part in item.get("content", [])
                      if isinstance(part, dict) and part.get("type") == "output_text"]
        else:
            if payload.get("stop_reason") != "end_turn":
                return dict(result, status="incomplete_response")
            pieces = [part.get("text", "") for part in payload.get("content", [])
                      if isinstance(part, dict) and part.get("type") == "text"]
        if not pieces or any(not isinstance(piece, str) for piece in pieces):
            raise ValueError("No text proposal")
        model_text = "".join(pieces)
        if token in model_text:
            raise ValueError("Credential echo rejected")
        return dict(result, status="response_received", proposal=json_load(model_text))
    except HTTPError as exc:
        # Do not read/print server-provided error messages or request headers.
        result["http_status"] = exc.code
        result["status"] = ({401: "access_denied", 403: "access_denied", 429: "rate_limited",
                              400: "request_rejected", 404: "model_or_endpoint_unavailable"}.get(
                                  exc.code, "provider_unavailable"))
        return result
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return dict(result, status="invalid_response")
    except (URLError, OSError, http.client.HTTPException):
        return dict(result, status="transport_unavailable")
