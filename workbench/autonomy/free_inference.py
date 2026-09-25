"""Disabled-by-integration OpenRouter free-route adapter; never executes output.

Only a documented zero-price route is requested, not an account billing guarantee.
The caller must explicitly supply a server-side OPENROUTER_API_KEY; importing this
module does not read credentials, send requests, or enable any runtime entrypoint.
"""
from __future__ import annotations

import http.client
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .providers import MAX_RESPONSE_BYTES, NoRedirect, json_load

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 45
MAX_OUTPUT_TOKENS = 3000
MAX_PROMPT_BYTES = 48 * 1024
_FREE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*:free\Z")


def call_free_model(prompt, model, token, transport=None):
    """Return bounded text data from one free-route request; no retries/fallbacks.

    transport is an optional trusted test callable with the signature
    transport(request, timeout=...) -> a urllib-style response context manager.
    It is not a configurable endpoint or an untrusted plugin hook.
    """
    base = {"status": "invalid_request", "requests": 0,
            "response_received": False, "provider": "openrouter",
            "pricing_assurance": "documented_zero_price_route_not_account_verified"}
    if not isinstance(model, str) or len(model) > 160 or not (
            model == "openrouter/free" or _FREE_MODEL.fullmatch(model)):
        return dict(base, status="blocked_nonfree_model")
    if not isinstance(token, str) or not token or len(token) > 2048 or any(
            ord(c) < 33 or ord(c) > 126 for c in token):
        return dict(base, status="blocked_provider_missing")
    if token in model or not isinstance(prompt, str) or not prompt.strip() or token in prompt:
        return base
    try:
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            return base
        # Validate Unicode and JSON bounds using the same strict parser as core.
        json_load(json.dumps({"prompt": prompt}, ensure_ascii=False))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return base
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": MAX_OUTPUT_TOKENS, "stream": False, "n": 1,
            "plugins": [], "provider": {"allow_fallbacks": False,
                "require_parameters": True,
                "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0}}}
    request = Request(ENDPOINT, data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "Meta-Harness-Free-Adapter/0.10"}, method="POST")
    result = dict(base, status="request_failed", requests=1, model=model)
    try:
        # Never forward a key to an environment-selected proxy or redirect target.
        send = transport if transport is not None else build_opener(ProxyHandler({}), NoRedirect()).open
        with send(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200 or response.geturl() != ENDPOINT:
                raise ValueError("Unexpected response location or status")
            if response.headers.get_content_type() != "application/json":
                raise ValueError("Expected JSON")
            if response.headers.get("Content-Encoding", "identity").lower() not in {"identity", ""}:
                raise ValueError("Compressed response rejected")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            result["response_received"] = True
            payload = json_load(raw)
        if not isinstance(payload, dict) or "error" in payload:
            raise ValueError("Invalid response envelope")
        # Do not return any provider field that could echo a credential.
        if token in json.dumps(payload, ensure_ascii=False):
            raise ValueError("Credential echo rejected")
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("Expected one completion")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("tool_calls") or message.get("function_call"):
            raise ValueError("Only assistant text accepted")
        if choice.get("finish_reason") != "stop":
            return dict(result, status="incomplete_response")
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Expected nonempty text")
        usage = payload.get("usage", {})
        if not isinstance(usage, dict):
            raise ValueError("Invalid usage")
        cost = usage.get("cost")
        if cost is not None and (type(cost) not in (int, float) or cost != 0):
            return dict(result, status="pricing_violation", cost_observation="nonzero_or_invalid")
        return dict(result, status="response_received", text=text,
                    cost_observation="provider_reported_zero" if cost is not None else "not_reported")
    except HTTPError as exc:
        # Never return raw error bodies, headers, prompts or exception strings.
        return dict(result, status={401: "access_denied", 403: "access_denied",
            402: "credit_or_account_blocked", 429: "rate_limited", 400: "request_rejected",
            404: "model_or_endpoint_unavailable"}.get(exc.code, "provider_unavailable"), http_status=exc.code)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return dict(result, status="invalid_response")
    except (URLError, OSError, http.client.HTTPException):
        return dict(result, status="transport_unavailable")
