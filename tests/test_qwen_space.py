"""Offline tests of public-Space wire handling and fail-closed boundaries."""
import hashlib
import io
import json
import unittest
from email.message import Message
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from workbench.autonomy import qwen_space as qwen


def config():
    return {"version": "5.27.0", "api_prefix": "/gradio_api", "protocol": "sse_v3",
            "dependencies": [{"api_name": "add_message", "inputs": [33, 38, 60, 1],
                              "outputs": [33, 56, 22, 15, 20, 29, 1], "queue": True,
                              "show_api": True, "types": {"generator": True}}],
            "components": [{"id": i, "type": t} for i, t in
                           [(1, "state"), (60, "state"), (33, "antdxsender"),
                            (38, "antdform"), (29, "modelscopeprochatbot")]]}


def complete(text="Unverified research proposal", status="done"):
    history = [{"role": "user", "content": "Public prompt"},
               {"role": "assistant", "status": status, "loading": False,
                "content": [{"type": "tool", "content": "reasoning not returned"},
                            {"type": "text", "content": text}]}]
    return [{"__type__": "update"}] * 5 + [{"__type__": "update", "value": history}, None]


def sse(payload=None, event="complete"):
    return ("event: " + event + "\ndata: " + json.dumps(complete() if payload is None else payload) + "\n\n").encode()


class Response(io.BytesIO):
    status = 200

    def __init__(self, raw, url, mime="application/json"):
        super().__init__(raw)
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = mime

    def geturl(self):
        return self.url


class QwenSpaceTests(unittest.TestCase):
    def transport(self, stream=None, config_value=None, metadata_changes=None):
        metadata = {"sha": qwen.REVISION, "private": False, "gated": False,
                    "disabled": False, "runtime": {"sha": qwen.REVISION,
                    "stage": "RUNNING", "hardware": {"current": "cpu-basic"}}}
        metadata.update(metadata_changes or {})
        responses = {
            qwen.METADATA_URL: (json.dumps(metadata).encode(), "application/json"),
            qwen.CONFIG_URL: (json.dumps(config() if config_value is None else config_value).encode(), "application/json"),
            qwen.ENDPOINT: (json.dumps({"event_id": "a" * 32}).encode(), "application/json"),
            qwen.ENDPOINT + "/" + "a" * 32: (sse() if stream is None else stream, "text/event-stream"),
        }
        self.pins = {"app.py": hashlib.sha256(b"fixture app").hexdigest()}
        responses[qwen.SOURCE_BASE + "app.py"] = (b"fixture app", "text/plain")

        def send(request, timeout):
            raw, mime = responses[request.full_url]
            return Response(raw, request.full_url, mime)
        transport = Mock(side_effect=send)
        transport.responses = responses
        return transport

    def run_fixture(self, transport):
        with patch.object(qwen, "SOURCE_PINS", self.pins):
            return qwen.call_qwen_space("Public prompt", transport)

    def test_invalid_or_obvious_secret_prompt_never_sends(self):
        for prompt in (None, "", " " * 5, "x" * 4001, "\ud800", "\x00",
                       "hf_" + "x" * 25, "-----BEGIN RSA PRIVATE KEY-----"):
            transport = Mock()
            self.assertEqual(qwen.call_qwen_space(prompt, transport)["status"], "invalid_request")
            transport.assert_not_called()

    def test_one_generation_public_wire_and_only_answer_returned(self):
        transport = self.transport()
        result = self.run_fixture(transport)
        self.assertEqual(result["status"], "response_received")
        self.assertEqual(result["text"], "Unverified research proposal")
        self.assertNotIn("reasoning not returned", json.dumps(result))
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["request_count"], 1)
        self.assertTrue(result["unverified"])
        posts = [c.args[0] for c in transport.call_args_list if c.args[0].get_method() == "POST"]
        self.assertEqual(len(posts), 1)
        body = json.loads(posts[0].data)
        self.assertEqual(body["data"], ["Public prompt", {"model": qwen.MODEL,
            "sys_prompt": "You are a helpful and harmless assistant.", "thinking_budget": 1}, None, None])
        self.assertNotIn("session_hash", body)
        self.assertEqual(result["session_hash"], result["event_id"])
        for call in transport.call_args_list:
            request = call.args[0]
            self.assertIsNone(request.get_header("Authorization"))
            self.assertIsNone(request.get_header("Cookie"))
            limit = 30 if request.get_header("Accept") == "text/event-stream" else 10
            self.assertLessEqual(call.kwargs["timeout"], limit)

    def test_queue_gap_until_fifteen_second_heartbeat_is_allowed(self):
        transport = self.transport()
        original = transport.side_effect

        def queue_wait(request, timeout):
            if request.get_header("Accept") == "text/event-stream" and timeout < 15:
                raise TimeoutError("No bytes arrive before the first heartbeat")
            return original(request, timeout)

        transport.side_effect = queue_wait
        self.assertEqual(self.run_fixture(transport)["status"], "response_received")
        stream_call = transport.call_args_list[-1]
        self.assertEqual(stream_call.kwargs["timeout"], 30)

    def test_gradio_527_event_get_matches_server_assigned_session(self):
        transport = self.transport()
        original = transport.side_effect
        queue_key = None

        def gradio_527(request, timeout):
            nonlocal queue_key
            if request.get_method() == "POST":
                queue_key = json.loads(request.data).get("session_hash") or "a" * 32
            if request.get_header("Accept") == "text/event-stream":
                if request.full_url.rsplit("/", 1)[-1] != queue_key:
                    return Response(sse("404: Session not found.", event="error"),
                                    request.full_url, "text/event-stream")
            return original(request, timeout)

        transport.side_effect = gradio_527
        result = self.run_fixture(transport)
        self.assertEqual(result["status"], "response_received")
        self.assertEqual(result["event_id"], queue_key)

    def test_revision_and_config_drift_stop_before_post(self):
        changed = config()
        changed["dependencies"][0]["outputs"] = [33, 29]
        for transport in (self.transport(metadata_changes={"sha": "b" * 40}),
                          self.transport(config_value=changed)):
            result = self.run_fixture(transport)
            self.assertEqual(result["status"], "contract_changed")
            self.assertEqual(result["requests"], 0)
            self.assertTrue(all(c.args[0].get_method() == "GET" for c in transport.call_args_list))

    def test_config_schema_depth_is_bounded_separately_from_answer(self):
        nested = "leaf"
        for _ in range(36):
            nested = [nested]
        value = config()
        value["schema"] = nested
        self.assertEqual(self.run_fixture(self.transport(config_value=value))["status"], "response_received")
        for _ in range(15):
            nested = [nested]
        value["schema"] = nested
        self.assertEqual(self.run_fixture(self.transport(config_value=value))["status"], "invalid_response")
        with self.assertRaises(ValueError):
            qwen._load_config(b'{"version":1,"version":2}')

    def test_source_digest_drift_stops_before_post(self):
        transport = self.transport()
        transport.responses[qwen.SOURCE_BASE + "app.py"] = (b"changed", "text/plain")
        self.assertEqual(self.run_fixture(transport)["status"], "contract_changed")
        self.assertTrue(all(c.args[0].get_method() == "GET" for c in transport.call_args_list))

    def test_sse_generating_does_not_count_as_completed(self):
        for stream in (sse(event="generating"), sse(event="error"),
                       sse(complete(status="pending")), sse(["", complete()[5]])):
            transport = self.transport(stream)
            result = self.run_fixture(transport)
            self.assertFalse(result["response_received"])
            self.assertNotIn("text", result)
            self.assertEqual(result["requests"], 1)

    def test_fragmented_sse_and_heartbeat(self):
        raw = b"event: heartbeat\ndata: null\n\n" + sse(event="generating") + sse()

        class Fragmented(Response):
            def read1(self, size=-1):
                return super().read1(min(size, 3))

        response = Fragmented(raw, qwen.ENDPOINT, "text/event-stream")
        text, count = qwen._sse(response, qwen.time.monotonic() + 10)
        self.assertEqual(text, "Unverified research proposal")
        self.assertLessEqual(count, len(raw))

    def test_stream_bytes_and_slow_drip_have_bounds(self):
        transport = self.transport(b":" + b"x" * (qwen.MAX_LINE_BYTES + 1))
        self.assertEqual(self.run_fixture(transport)["status"], "response_too_large")
        transport = self.transport(b"event: heartbeat\ndata: null\n\n" * 15000)
        self.assertEqual(self.run_fixture(transport)["status"], "response_too_large")
        with patch.object(qwen.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(qwen._Failure) as error:
                qwen._sse(Response(b":x\n\n", "fixture"), 1)
        self.assertEqual(error.exception.status, "deadline_exceeded")

    def test_http_rate_limit_no_retry_and_no_error_body(self):
        transport = self.transport()
        original = transport.side_effect

        def limited(request, timeout):
            if request.get_method() == "POST":
                raise HTTPError(request.full_url, 429, "private error", {}, io.BytesIO(b"do not return"))
            return original(request, timeout)
        transport.side_effect = limited
        result = self.run_fixture(transport)
        self.assertEqual(result["status"], "rate_limited")
        self.assertNotIn("do not return", json.dumps(result))
        self.assertEqual(sum(c.args[0].get_method() == "POST" for c in transport.call_args_list), 1)

    def test_redirect_or_non_json_rejected(self):
        for bad_url, mime in (("https://example.com", "application/json"),
                              (qwen.METADATA_URL, "text/html")):
            transport = Mock(return_value=Response(b"{}", bad_url, mime))
            result = qwen.call_qwen_space("Public prompt", transport)
            self.assertEqual(result["status"], "invalid_response")
            self.assertEqual(result["requests"], 0)

    def test_event_id_cannot_select_another_path(self):
        transport = self.transport()
        transport.responses[qwen.ENDPOINT] = (b'{"event_id":"../../evil"}', "application/json")
        result = self.run_fixture(transport)
        self.assertEqual(result["status"], "invalid_response")
        self.assertEqual(transport.call_args.args[0].full_url, qwen.ENDPOINT)

    def test_network_process_is_terminated_at_absolute_deadline(self):
        receiver, sender, process, context = Mock(), Mock(), Mock(), Mock()
        context.Pipe.return_value = (receiver, sender)
        context.Process.return_value = process
        receiver.poll.return_value = False
        process.pid = 123
        process.is_alive.return_value = True
        with patch.object(qwen.multiprocessing, "get_context", return_value=context):
            result = qwen.call_qwen_space("Public prompt")
        self.assertEqual(result["status"], "deadline_exceeded")
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertLessEqual(receiver.poll.call_args.args[0], 120)
        self.assertEqual(result["requests"], 1)  # Conservative ambiguous submission.


if __name__ == "__main__":
    unittest.main()

