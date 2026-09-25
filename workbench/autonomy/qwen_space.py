"""One anonymous public Qwen Space request; no credentials, retries or execution.

The public demo is not an SLA or an unlimited inference grant. Live generation
has not been verified by the contract research. Only explicitly public prompts
belong here. Importing this module performs no IO; scheduling is a separate job.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import multiprocessing
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .providers import NoRedirect, json_load

SPACE = "Qwen/Qwen3-Demo"
REVISION = "60e1db0778067d36b8a2793c350bf85cd461a298"
MODEL = "qwen3-235b-a22b"
ORIGIN = "https://qwen-qwen3-demo.hf.space"
METADATA_URL = "https://huggingface.co/api/spaces/" + SPACE
CONFIG_URL = ORIGIN + "/config"
ENDPOINT = ORIGIN + "/gradio_api/call/add_message"
SOURCE_BASE = "https://huggingface.co/spaces/" + SPACE + "/raw/" + REVISION + "/"
SOURCE_PINS = {
    "app.py": "3724ddcc9cc293e487ece3ab44f6d690177cfbab29a24f84f640f12f908e10bd",
    "config.py": "33285ab4c92d8b9795995aeb14c5ddb388bf2e50405f71bd1dc85a945c87873e",
    "ui_components/thinking_button.py": "594a0df440abe2ee79c40e2f5c067d52a960a92c629715db66c53be76db24df9",
}
TIMEOUT_SECONDS = 120
MAX_PROMPT_CHARS = 4000
# Wire traffic includes repeated full generating snapshots, not just the answer.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_PREFLIGHT_BYTES = 192 * 1024
MAX_LINE_BYTES = 128 * 1024
_EVENT_ID = re.compile(r"[a-f0-9]{32}\Z")
_SECRET = re.compile(r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:hf_|sk-)[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9_]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,})")


class _Failure(Exception):
    def __init__(self, status):
        self.status = status


def _base(status="invalid_request", requests=0):
    return {"status": status, "requests": requests, "request_count": requests,
            "http_requests": 0, "response_received": False, "unverified": True,
            "provider": "qwen_public_space", "endpoint": ENDPOINT,
            "space_revision": REVISION, "model": MODEL,
            "model_revision": "not_disclosed_by_hosted_alias",
            "thinking": True, "thinking_budget": 1024, "web_search": False,
            "remote_cancellation": "not_guaranteed", "automatic_retry": False,
            "billing": "anonymous_demo_no_user_credentials_quota_unknown"}


def _validate_prompt(prompt):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        return False
    try:
        json_load(json.dumps({"prompt": prompt}, ensure_ascii=False))
    except (ValueError, UnicodeError, RecursionError):
        return False
    return _SECRET.search(prompt) is None


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Failure("deadline_exceeded")
    return remaining


def _response(send, url, deadline, result, data=None, mime="application/json"):
    _remaining(deadline)
    request = Request(url, data=data, method="POST" if data is not None else "GET",
                      headers={"Accept": mime, "Accept-Encoding": "identity",
                               "Content-Type": "application/json",
                               "User-Agent": "Meta-Harness-Qwen-Public-Demo/0.10"})
    result["http_requests"] += 1
    # Gradio emits a heartbeat only every 15 seconds while the queue is idle.
    # Leave room for it; the separate process still enforces the total deadline.
    socket_timeout = 30 if mime == "text/event-stream" else 10
    response = send(request, timeout=min(socket_timeout, _remaining(deadline)))
    try:
        _remaining(deadline)
        if response.status != 200 or response.geturl() != url:
            raise _Failure("invalid_response")
        if response.headers.get_content_type() != mime:
            raise _Failure("invalid_response")
        if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise _Failure("invalid_response")
        return response
    except BaseException:
        response.close()
        raise


def _read(response, limit, deadline):
    chunks, length = [], 0
    while True:
        _remaining(deadline)
        chunk = response.read1(min(8192, limit + 1 - length))
        _remaining(deadline)
        if not chunk:
            return b"".join(chunks)
        length += len(chunk)
        if length > limit:
            raise _Failure("response_too_large")
        chunks.append(chunk)


def _check_config(config):
    if not isinstance(config, dict) or config.get("version") != "5.27.0" or config.get("api_prefix") != "/gradio_api" or config.get("protocol") != "sse_v3":
        raise _Failure("contract_changed")
    deps = [d for d in config.get("dependencies", []) if isinstance(d, dict) and d.get("api_name") == "add_message"]
    if len(deps) != 1:
        raise _Failure("contract_changed")
    dep = deps[0]
    if dep.get("inputs") != [33, 38, 60, 1] or dep.get("outputs") != [33, 56, 22, 15, 20, 29, 1] or dep.get("types", {}).get("generator") is not True or dep.get("queue") is not True or dep.get("show_api") is not True:
        raise _Failure("contract_changed")
    components = {c.get("id"): c for c in config.get("components", []) if isinstance(c, dict)}
    for state_id in (60, 1):
        if components.get(state_id, {}).get("type") != "state":
            raise _Failure("contract_changed")
    for component_id, expected_type in ((33, "antdxsender"), (38, "antdform"), (29, "modelscopeprochatbot")):
        if components.get(component_id, {}).get("type") != expected_type:
            raise _Failure("contract_changed")
    if config.get("auth_required") is True:
        raise _Failure("access_denied")


def _load_config(raw):
    # Gradio's actual JSON Schema has depth 36. Core model-output JSON allows
    # only 24, so use a separately bounded parser for this <=96 KiB document.
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate config key")
            value[key] = item
        return value

    def constant(_):
        raise ValueError("Non-finite config number")

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 48 or count > 12000:
            raise ValueError("Config structure limit exceeded")
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for pair in item.items() for v in pair)
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
        elif isinstance(item, str):
            item.encode("utf-8")
    return value


def _preflight(send, deadline, result):
    read_bytes = 0

    def get(url, limit, mime="application/json"):
        nonlocal read_bytes
        with _response(send, url, deadline, result, mime=mime) as response:
            raw = _read(response, min(limit, MAX_PREFLIGHT_BYTES - read_bytes), deadline)
        read_bytes += len(raw)
        return raw

    metadata = json_load(get(METADATA_URL, 16 * 1024))
    if not isinstance(metadata, dict) or metadata.get("private") is not False or metadata.get("gated") is not False or metadata.get("disabled") is not False:
        raise _Failure("contract_changed")
    runtime = metadata.get("runtime", {})
    if metadata.get("sha") != REVISION or runtime.get("sha") != REVISION:
        raise _Failure("contract_changed")
    if runtime.get("stage") != "RUNNING" or runtime.get("hardware", {}).get("current") != "cpu-basic":
        raise _Failure("provider_unavailable")
    for name, digest in SOURCE_PINS.items():
        raw = get(SOURCE_BASE + name, 40 * 1024, "text/plain")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise _Failure("contract_changed")
    config_raw = get(CONFIG_URL, 96 * 1024)
    _check_config(_load_config(config_raw))
    result["config_sha256"] = hashlib.sha256(config_raw).hexdigest()
    result["preflight_bytes"] = read_bytes


def _text_from_complete(payload):
    # /info omits skip_api components. Raw Gradio 5.27 SSE has seven outputs.
    if not isinstance(payload, list) or len(payload) != 7:
        raise _Failure("invalid_response")
    chatbot = payload[5]
    if not isinstance(chatbot, dict) or chatbot.get("__type__") != "update":
        raise _Failure("invalid_response")
    history = chatbot.get("value")
    if not isinstance(history, list) or len(history) != 2 or history[0].get("role") != "user":
        raise _Failure("invalid_response")
    message = history[-1]
    if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("status") != "done" or message.get("loading") is not False:
        raise _Failure("incomplete_response")
    content = message.get("content")
    if not isinstance(content, list):
        raise _Failure("invalid_response")
    # Thinking is a UI tool block, not executable instructions or answer text.
    parts = [part["content"] for part in content if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("content"), str)]
    text = "\n".join(parts)
    if not text.strip() or len(text.encode("utf-8")) > 64 * 1024:
        raise _Failure("incomplete_response")
    return text


def _sse(response, deadline):
    # Gradio 5.27 /call serializes full output snapshots in generating events.
    # Bound total wire traffic separately; retain only the complete event data.
    # Source: gradio@5.27.0/gradio/routes.py, simple_predict_get.process_msg.
    buffer = b""
    event = None
    data = bytearray()
    event_bytes = 0
    total = 0
    while True:
        _remaining(deadline)
        chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - total))
        _remaining(deadline)
        if not chunk:
            raise _Failure("incomplete_response")
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise _Failure("response_too_large")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if len(line) > MAX_LINE_BYTES:
                raise _Failure("response_too_large")
            event_bytes += len(line) + 1
            if event_bytes > MAX_EVENT_BYTES:
                raise _Failure("response_too_large")
            line = line.rstrip(b"\r")
            if not line:
                if event == "error":
                    raise _Failure("provider_unavailable")
                if event == "complete":
                    return _text_from_complete(json_load(bytes(data))), total
                if event not in (None, "heartbeat", "generating"):
                    raise _Failure("invalid_response")
                event, event_bytes = None, 0
                data.clear()
            elif line.startswith(b"event: "):
                if event is not None:
                    raise _Failure("invalid_response")
                event = line[7:].decode("ascii")
                if event not in {"complete", "error", "heartbeat", "generating"}:
                    raise _Failure("invalid_response")
            elif line.startswith(b"data: "):
                if event is None:
                    raise _Failure("invalid_response")
                if event == "complete":
                    if data:
                        data.extend(b"\n")
                    data.extend(line[6:])
                # Intermediate snapshots and upstream error bodies are discarded.
            elif not line.startswith(b":"):
                raise _Failure("invalid_response")
        if len(buffer) > MAX_LINE_BYTES or event_bytes + len(buffer) > MAX_EVENT_BYTES:
            raise _Failure("response_too_large")


def _perform(prompt, send, deadline, progress=None):
    result = _base("request_failed")
    try:
        _preflight(send, deadline, result)
        # Gradio 5.27 /call GET uses event_id as the queue lookup key. Sending
        # a different session_hash makes that GET return Session not found.
        # Omit it: Event assigns its fresh event_id as the new session key.
        body = {"data": [prompt, {"model": MODEL,
                "sys_prompt": "You are a helpful and harmless assistant.",
                "thinking_budget": 1}, None, None]}
        _remaining(deadline)
        result.update(requests=1, request_count=1)
        if progress:
            progress(dict(result))  # Checkpoint before ambiguous network submission.
        with _response(send, ENDPOINT, deadline, result,
                       json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")) as response:
            envelope = json_load(_read(response, 4096, deadline))
        event_id = envelope.get("event_id") if isinstance(envelope, dict) else None
        if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
            raise _Failure("invalid_response")
        result["event_id"] = event_id
        result["session_hash"] = event_id
        if progress:
            progress(dict(result))
        with _response(send, ENDPOINT + "/" + event_id, deadline, result,
                       mime="text/event-stream") as response:
            text, length = _sse(response, deadline)
        return dict(result, status="response_received", response_received=True,
                    text=text, response_bytes=length,
                    answer_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest())
    except _Failure as exc:
        return dict(result, status=exc.status)
    except HTTPError as exc:
        exc.close()
        return dict(result, status={401: "access_denied", 403: "access_denied",
                    429: "rate_limited"}.get(exc.code, "provider_unavailable"), http_status=exc.code)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        return dict(result, status="invalid_response")
    except (URLError, OSError, http.client.HTTPException):
        return dict(result, status="transport_unavailable")


def _worker(prompt, connection, deadline):
    try:
        # No cookie jar, auth handler, environment proxy, key lookup or redirect.
        send = build_opener(ProxyHandler({}), NoRedirect()).open
        result = _perform(prompt, send, deadline,
                          lambda value: connection.send(("progress", value)))
        connection.send(("result", result))
    finally:
        connection.close()


def _isolated(prompt):
    started = time.monotonic()
    # Reserve one second of the 120-second envelope for terminating/reaping.
    deadline = started + TIMEOUT_SECONDS - 1
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(prompt, sender, deadline), daemon=True)
    result = _base("deadline_exceeded", 1)  # Unknown child state is never retried.
    try:
        process.start()
        sender.close()
        while time.monotonic() < deadline:
            if not receiver.poll(max(0, deadline - time.monotonic())):
                break
            try:
                kind, value = receiver.recv()
            except EOFError:
                return dict(result, status="transport_unavailable")
            if kind == "result":
                return value
            if kind == "progress":
                result = dict(value, status="deadline_exceeded")
        return result
    except (OSError, RuntimeError, EOFError):
        return dict(result, status="transport_unavailable")
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=max(0, min(0.5, started + TIMEOUT_SECONDS - time.monotonic())))
            if process.is_alive():
                process.kill()
                process.join(timeout=max(0, started + TIMEOUT_SECONDS - time.monotonic()))
            if not process.is_alive():
                process.close()


def call_qwen_space(prompt, transport=None):
    """One public prompt, <=4000 characters; returned text is unverified data.

    transport is only a trusted, synchronous offline-test injection. Production
    runs inside a child process bounded by 120 seconds including preflight; a
    timeout terminates local work, not necessarily upstream DashScope inference.
    requests/request_count count attempted generation POSTs, not preflight GETs.
    No persistent six-hour/28-attempt budget is implemented at this layer.
    """
    if not _validate_prompt(prompt):
        return _base()
    if transport is not None:
        return _perform(prompt, transport, time.monotonic() + TIMEOUT_SECONDS)
    return _isolated(prompt)
