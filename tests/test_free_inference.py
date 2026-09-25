"""Offline boundary tests; no actual credentials or inference calls."""
import io
import json
import unittest
from email.message import Message
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from workbench.autonomy import free_inference as free

TOKEN = "fake-test-only-credential"


def payload(**changes):
    value = {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "Ответ как данные"}}], "usage": {"cost": 0}}
    value.update(changes)
    return value


class Response(io.BytesIO):
    status = 200

    def __init__(self, value=None, raw=None, mime="application/json", url=free.ENDPOINT, encoding=None):
        super().__init__(raw if raw is not None else json.dumps(value, ensure_ascii=False).encode("utf-8"))
        self.headers = Message()
        self.headers["Content-Type"] = mime
        if encoding:
            self.headers["Content-Encoding"] = encoding
        self.url = url
        self.read_limits = []

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.read_limits.append(size)
        return super().read(size)


class FreeInferenceTests(unittest.TestCase):
    def call(self, response, model="openrouter/free"):
        transport = Mock(return_value=response)
        result = free.call_free_model("Текст исследования", model, TOKEN, transport)
        return result, transport

    def test_only_free_routes_and_credential_validated_before_transport(self):
        for model in ("openai/gpt-5", "openrouter/auto", "org/model:free:online",
                      "org/model:free?x=y", "org/model:free\n", "https://evil/free", None):
            with self.subTest(model=model):
                transport = Mock()
                self.assertEqual(free.call_free_model("x", model, TOKEN, transport)["status"], "blocked_nonfree_model")
                transport.assert_not_called()
        for token in (None, "", "space key", "x\ny", "ю", "x" * 2049):
            with self.subTest(token_type=type(token).__name__):
                transport = Mock()
                self.assertEqual(free.call_free_model("x", "openrouter/free", token, transport)["requests"], 0)
                transport.assert_not_called()

    def test_fixed_zero_price_request_and_unicode_data_return(self):
        for model in ("openrouter/free", "org/example-v1.2:free"):
            with self.subTest(model=model):
                response = Response(payload())
                result, transport = self.call(response, model)
                self.assertEqual(result["status"], "response_received")
                self.assertEqual(result["text"], "Ответ как данные")
                self.assertEqual(result["cost_observation"], "provider_reported_zero")
                transport.assert_called_once()
                request = transport.call_args.args[0]
                self.assertEqual(request.full_url, free.ENDPOINT)
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
                body = json.loads(request.data)
                self.assertEqual(body["model"], model)
                self.assertEqual(body["provider"]["max_price"], dict.fromkeys(("prompt", "completion", "request", "image"), 0))
                self.assertFalse(body["provider"]["allow_fallbacks"])
                self.assertTrue(body["provider"]["require_parameters"])
                self.assertFalse(body["stream"])
                self.assertEqual(body["plugins"], [])
                self.assertNotIn("tools", body)
                self.assertNotIn("models", body)
                self.assertEqual(body["max_tokens"], 3000)
                self.assertEqual(transport.call_args.kwargs["timeout"], 45)
                self.assertEqual(response.read_limits, [192 * 1024 + 1])
                self.assertTrue(response.closed)
                self.assertNotIn(TOKEN, json.dumps(result))

    def test_missing_cost_is_not_reported_as_verified_zero(self):
        result, _ = self.call(Response(payload(usage={})))
        self.assertEqual(result["cost_observation"], "not_reported")
        self.assertIn("not_account_verified", result["pricing_assurance"])
        for cost in (0.1, -1, "0", True):
            with self.subTest(cost=cost):
                result, _ = self.call(Response(payload(usage={"cost": cost})))
                self.assertEqual(result["status"], "pricing_violation")
                self.assertNotIn("text", result)

    def test_errors_are_redacted_and_never_retried(self):
        for error, expected in ((HTTPError(free.ENDPOINT, 429, TOKEN, {}, io.BytesIO(TOKEN.encode())), "rate_limited"),
                                (HTTPError(free.ENDPOINT, 402, TOKEN, {}, None), "credit_or_account_blocked"),
                                (URLError(TOKEN), "transport_unavailable")):
            with self.subTest(expected=expected):
                transport = Mock(side_effect=error)
                result = free.call_free_model("x", "openrouter/free", TOKEN, transport)
                self.assertEqual(result["status"], expected)
                self.assertNotIn(TOKEN, json.dumps(result))
                transport.assert_called_once()

    def test_strict_response_size_encoding_json_and_location(self):
        responses = [Response(raw=b"x" * (free.MAX_RESPONSE_BYTES + 1)),
            Response(raw=b'{"x":1,"x":2}'), Response(raw=b'{"x":NaN}'),
            Response(raw=b'\xff'), Response(payload(), mime="text/html"),
            Response(payload(), url="https://attacker.invalid"),
            Response(payload(), encoding="gzip"), Response(raw=(b"[" * 30 + b"0" + b"]" * 30))]
        for response in responses:
            with self.subTest(index=responses.index(response)):
                result, _ = self.call(response)
                self.assertEqual(result["status"], "invalid_response")
                self.assertNotIn("text", result)

    def test_tools_incomplete_outputs_and_credential_echo_rejected(self):
        tool = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant",
            "content": "x", "tool_calls": [{"function": {"name": "shell"}}]}}]}
        for value in (tool, payload(id=TOKEN), payload(choices=[]), payload(error={"message": TOKEN})):
            result, _ = self.call(Response(value))
            self.assertEqual(result["status"], "invalid_response")
            self.assertNotIn(TOKEN, json.dumps(result))
        value = payload()
        value["choices"][0]["finish_reason"] = "length"
        result, _ = self.call(Response(value))
        self.assertEqual(result["status"], "incomplete_response")
        self.assertNotIn("text", result)

    def test_prompt_limits_and_secret_before_http(self):
        for prompt in ("", " ", None, "x" * (free.MAX_PROMPT_BYTES + 1), "\ud800", "echo " + TOKEN):
            transport = Mock()
            result = free.call_free_model(prompt, "openrouter/free", TOKEN, transport)
            self.assertEqual(result["requests"], 0)
            transport.assert_not_called()

    def test_default_transport_disables_redirect_and_environment_proxies(self):
        with patch.object(free, "build_opener") as opener:
            opener.return_value.open.return_value = Response(payload())
            result = free.call_free_model("x", "openrouter/free", TOKEN)
            self.assertEqual(result["status"], "response_received")
            handlers = opener.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            with self.assertRaises(ValueError):
                handlers[1].redirect_request(None, None, 302, "", {}, "https://evil.invalid")
            opener.return_value.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
