"""One-shot, loopback-only adapter to one documented anonymous free model.

This is a transport guard, not an OS sandbox. It never carries user credentials.
The remote model is untrusted and its text is never executed.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from ..autonomy.providers import NoRedirect, json_load

ENDPOINT = 'https://opencode.ai/inference/openai/v1/chat/completions'
MODEL = 'mimo-v2.5-free'
MAX_INPUT = 64000
MAX_OUTPUT = 128000
MAX_TOKENS = 1024
SECRET = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:hf_|sk-)[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9_]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}')


def validate_request(payload, task_id):
    if not isinstance(payload, dict) or payload.get('model') != MODEL:
        raise ValueError('model_not_allowed')
    if payload.get('tools') or payload.get('functions') or payload.get('tool_choice') not in (None, 'none'):
        raise ValueError('tools_not_allowed')
    messages = payload.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 8:
        raise ValueError('invalid_messages')
    result = []
    for message in messages:
        if (not isinstance(message, dict) or message.get('role') not in {'system', 'user'}
                or not isinstance(message.get('content'), str)
                or set(message) - {'role', 'content'}):
            raise ValueError('text_messages_only')
        result.append({'role': message['role'], 'content': message['content']})
    encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
    if len(encoded) > MAX_INPUT or SECRET.search(encoded.decode('utf-8')):
        raise ValueError('input_rejected')
    if not any(m['role'] == 'user' and task_id in m['content'] for m in result):
        raise ValueError('task_binding_missing')
    return result


def call_free(messages, transport=None, evidence=None):
    """One fixed POST; no auth, proxy, redirect, retry or paid fallback."""
    payload = {'model': MODEL, 'messages': messages, 'stream': False, 'max_tokens': MAX_TOKENS}
    request = Request(ENDPOINT, data=json.dumps(payload).encode('utf-8'), method='POST',
                      headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                               'User-Agent': 'NeuroMorf-DSH-Review/1'})
    send = transport or build_opener(ProxyHandler({}), NoRedirect()).open
    try:
        with send(request, timeout=45) as response:
            if response.status != 200 or response.geturl() != ENDPOINT:
                raise ValueError('unexpected_response')
            if response.headers.get_content_type() != 'application/json':
                raise ValueError('non_json_response')
            if response.headers.get('Content-Encoding', 'identity') not in {'identity', ''}:
                raise ValueError('compressed_response')
            raw = response.read(MAX_OUTPUT + 1)
            if len(raw) > MAX_OUTPUT:
                raise ValueError('output_limit')
            value = json_load(raw)
        if not isinstance(value, dict):
            raise ValueError('invalid_response_object')
        choices = value.get('choices')
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError('invalid_choices')
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError('invalid_choice_object')
        message = choice.get('message', {})
        if not isinstance(message, dict):
            raise ValueError('invalid_message_object')
        if message.get('tool_calls') or message.get('function_call'):
            raise ValueError('tool_output_rejected')
        content = message.get('content')
        if not isinstance(content, str) or not content.strip() or SECRET.search(content):
            raise ValueError('invalid_text')
        if choice.get('finish_reason') != 'stop':
            raise ValueError('incomplete_response')
        if evidence is not None:
            evidence.update({'http_status': 200, 'response_body_sha256': hashlib.sha256(raw).hexdigest(),
                             'response_bytes': len(raw),
                             'model_identity_basis': 'requested_alias_and_provider_transport_not_weight_attestation'})
        return content
    except HTTPError as exc:
        raise ValueError('remote_http_' + str(exc.code)) from None
    except (URLError, OSError):
        raise ValueError('remote_transport_failed') from None


class OneShot:
    def __init__(self, task_id, live=False, transport=None, reserve=None):
        self.task_id, self.live, self.transport, self.reserve = task_id, live, transport, reserve
        self.lock = threading.Lock()
        self.attempted = False
        self.receipt = {'task_id': task_id, 'provider': 'opencode_anonymous_free', 'model': MODEL,
                        'endpoint': ENDPOINT, 'live': live, 'upstream_requests': 0,
                        'harness_requests': 0, 'response_received': False,
                        'authentication_sent': False, 'automatic_retry': False,
                        'paid_fallback': False, 'status': 'waiting_for_harness'}

    def complete(self, payload):
        with self.lock:
            self.receipt['harness_requests'] += 1
            if self.attempted:
                raise ValueError('already_attempted')
            self.attempted = True  # Malformed first requests do not trigger retries either.
            self.receipt['status'] = 'request_rejected'
            messages = validate_request(payload, self.task_id)
            self.receipt['input_sha256'] = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
            if self.live:
                if self.reserve is None:
                    raise ValueError('durable_reservation_required')
                self.reserve()
                self.receipt['upstream_requests'] = 1
                self.receipt['status'] = 'request_started'
                try:
                    text = call_free(messages, self.transport, self.receipt)
                except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
                    reason = str(exc)
                    self.receipt['status'] = reason if re.fullmatch(r'[a-z0-9_]{1,60}', reason) else 'invalid_response'
                    raise ValueError(self.receipt['status']) from None
                self.receipt['status'] = 'response_received'
                self.receipt['response_received'] = True
            else:
                text = 'Protocol fixture: ' + self.task_id + '. No remote model was called.'
                self.receipt['status'] = 'protocol_fixture_received'
            self.receipt['output_text'] = text
            self.receipt['output_sha256'] = hashlib.sha256(text.encode()).hexdigest()
            return text


def server_for(guard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, content, mime='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            try:
                self.connection.settimeout(10)
                if self.path != '/v1/chat/completions' or self.headers.get('Transfer-Encoding'):
                    raise ValueError('route_rejected')
                length = int(self.headers.get('Content-Length', '0'))
                if not 1 <= length <= MAX_INPUT:
                    raise ValueError('input_limit')
                payload = json_load(self.rfile.read(length))
                text = guard.complete(payload)
                common = {'id': 'neuromorph-review', 'created': int(time.time()), 'model': MODEL}
                if payload.get('stream'):
                    chunks = [dict(common, object='chat.completion.chunk', choices=[{
                        'index': 0, 'delta': {'role': 'assistant', 'content': text}, 'finish_reason': None}]),
                              dict(common, object='chat.completion.chunk', choices=[{
                        'index': 0, 'delta': {}, 'finish_reason': 'stop'}])]
                    body = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
                    self.reply(200, body.encode(), 'text/event-stream')
                else:
                    body = dict(common, object='chat.completion', choices=[{'index': 0,
                        'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}])
                    self.reply(200, json.dumps(body).encode())
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OSError):
                self.reply(400, b'{"error":{"message":"request_refused","type":"invalid_request_error"}}')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    return server
