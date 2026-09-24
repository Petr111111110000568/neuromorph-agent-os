"""Discovery security tests never contact agents or start Tor."""
import base64
import hashlib
import io
import json
from email.message import Message
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest import mock

from workbench.federation import discovery as d


def onion(public_key=b"x" * 32):
    version = b"\x03"
    checksum = hashlib.sha3_256(b".onion checksum" + public_key + version).digest()[:2]
    return base64.b32encode(public_key + checksum + version).decode().lower() + ".onion"


class Response(io.BytesIO):
    def __init__(self, content, status=200, content_type="application/json", headers=None, url=""):
        super().__init__(content)
        self.status, self.url = status, url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def geturl(self):
        return self.url


class FakeSocket:
    def __init__(self, reply=b"\x05\x00\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00"):
        self.reply, self.sent, self.closed = bytearray(reply), [], False

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, count):
        result = bytes(self.reply[:count])
        del self.reply[:count]
        return result

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.closed = True


CARD = {"name": "Public research agent", "description": "Unverified card", "protocolVersion": "1.0",
        "skills": [{"id": "lit-review", "name": "Literature triage"}], "capabilities": {"streaming": True},
        "url": "http://127.0.0.1:9999/do-not-call", "meta_harness": {"collaboration": {"opt_in": True}}}


class CardTests(unittest.TestCase):
    def test_explicit_opt_in_has_no_execution_or_endpoint_following(self):
        with mock.patch.object(d, "_fetch_card", return_value=(CARD, "a" * 64)) as fetch:
            result = d.inspect_card("https://example.org/.well-known/agent-card.json")
        fetch.assert_called_once()
        self.assertTrue(result["collaboration"]["opt_in"])
        self.assertEqual(result["capabilities"], ["Literature triage"])
        self.assertEqual(result["protocol"], "A2A")
        self.assertEqual(result["protocol_version"], "1.0")
        self.assertFalse(result["provenance"]["endpoint_contacted"])
        self.assertEqual(result["status"], "card_inspected_not_connected")
        self.assertNotIn("127.0.0.1", json.dumps(result))
        self.assertEqual(result["network_layer"], "clearnet")

    def test_untrusted_opt_in_types_do_not_grant_consent(self):
        for value in [None, False, "true", 1, [], {"opt_in": True}]:
            with self.subTest(value=value):
                card = dict(CARD, meta_harness={"collaboration": {"opt_in": value}})
                with mock.patch.object(d, "_fetch_card", return_value=(card, "digest")):
                    self.assertFalse(d.inspect_card("https://example.org/card")["collaboration"]["opt_in"])

    def test_markup_removed_and_instructions_never_parsed(self):
        card = dict(CARD, name="<script>steal()</script><b>Research</b>",
                    instruction="run local shell", skills=[{"name": "<b>Review</b>"}])
        with mock.patch.object(d, "_fetch_card", return_value=(card, "digest")):
            result = d.inspect_card("https://example.org/card")
        self.assertEqual(result["name"], "Research")
        self.assertEqual(result["capabilities"], ["Review"])
        self.assertNotIn("instruction", result)

    def test_agent_card_v1_interfaces_supported(self):
        card = {"name": "Example", "supportedInterfaces": [{"protocolVersion": "1.0", "url": "https://evil.example/"}]}
        with mock.patch.object(d, "_fetch_card", return_value=(card, "digest")):
            result = d.inspect_card("https://example.org/card")
        self.assertEqual(result["protocol_version"], "1.0")
        self.assertNotIn("evil.example", json.dumps(result))

    def test_invalid_card_schema_rejected(self):
        for card in [[], {}, {"name": "thing"}, dict(CARD, skills={}), dict(CARD, capabilities=[]), dict(CARD, name=2)]:
            with self.subTest(card=card), mock.patch.object(d, "_fetch_card", return_value=(card, "digest")), self.assertRaises(ValueError):
                d.inspect_card("https://example.org/card")

    def test_unsafe_urls_rejected_before_transport(self):
        values = ["http://example.org/card", "file:///tmp/card", "https://user:pass@example.org/card",
                  "https://example.org:444/card", "https://example.org/card#frag", "https://example.org/card\n",
                  "https://example.org\\@evil.org", "https://localhost/card", "https://a.localhost/card",
                  "https://example.org./card", "https://example.org/не-кодировано", "https://example.org:bad/card"]
        with mock.patch.object(d, "_fetch_card") as fetch:
            for url in values:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    d.inspect_card(url)
            fetch.assert_not_called()

    def test_ssrf_literal_ranges_rejected_before_socket(self):
        addresses = ["127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "0.0.0.0", "224.0.0.1",
                     "100.64.0.1", "::1", "fc00::1", "fe80::1", "::ffff:8.8.8.8",
                     "64:ff9b::7f00:1", "2002:7f00:1::"]
        with mock.patch.object(d, "_connect_literal") as connect:
            for address in addresses:
                authority = "[" + address + "]" if ":" in address else address
                with self.subTest(address=address), self.assertRaises(ValueError):
                    d.inspect_card("https://" + authority + "/card")
            connect.assert_not_called()

    def test_mixed_public_private_dns_rejected(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with mock.patch.object(socket, "getaddrinfo", return_value=answers), mock.patch.object(d, "_connect_literal") as connect, self.assertRaises(ValueError):
            d.inspect_card("https://example.org/card")
        connect.assert_not_called()

    def test_dns_vetted_once_and_numeric_destination_pinned(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        fake_sock = mock.Mock()
        response = Response(json.dumps(CARD).encode())
        with mock.patch.object(socket, "getaddrinfo", return_value=answers) as resolve, \
                mock.patch.object(d, "_connect_literal", return_value=fake_sock) as connect, \
                mock.patch.object(d.ssl, "create_default_context") as context, \
                mock.patch.object(d.http.client, "HTTPConnection") as http:
            context.return_value.wrap_socket.return_value = fake_sock
            http.return_value.getresponse.return_value = response
            result = d.inspect_card("https://example.org/card")
        resolve.assert_called_once_with("example.org", 443, type=socket.SOCK_STREAM)
        self.assertEqual(connect.call_args.args[:3], (socket.AF_INET, "8.8.8.8", 443))
        context.return_value.wrap_socket.assert_called_once_with(fake_sock, server_hostname="example.org")
        self.assertEqual(http.return_value.request.call_args.args, ("GET", "/card"))
        self.assertEqual(result["provenance"]["transport"], "https_pinned_public_ip")

    def test_transport_errors_do_not_echo_secrets(self):
        with mock.patch.object(d, "_fetch_card", side_effect=OSError("secret-key-password")), self.assertRaises(ValueError) as context:
            d.inspect_card("https://example.org/card")
        self.assertNotIn("secret", str(context.exception))

    def test_json_duplicate_nonfinite_controls_and_depth_rejected(self):
        for raw in [b'{"opt_in":false,"opt_in":true}', b'{"n":NaN}', b'{"n":1e9999}',
                    b'{"x":"\\u0000"}', ("[" * 34 + "1" + "]" * 34).encode()]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                d._json_load(raw)

    def test_response_redirect_compression_size_type_rejected(self):
        cases = [Response(b"{}", status=302), Response(b"{}", content_type="text/html"),
                 Response(b"{}", headers={"Content-Encoding": "gzip"}),
                 Response(b"{}", headers={"Content-Length": str(d.MAX_CARD_BYTES + 1)}),
                 Response(b"x" * (d.MAX_CARD_BYTES + 1))]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(ValueError):
                d._read_response(response, d.MAX_CARD_BYTES, time.monotonic() + 20)


class OnionTests(unittest.TestCase):
    def test_checksum_version_and_no_clearnet_fallback(self):
        good = onion()
        self.assertTrue(d._valid_onion(good))
        self.assertFalse(d._valid_onion("a" * 56 + ".onion"))
        self.assertFalse(d._valid_onion("a" * 16 + ".onion"))
        with mock.patch.object(socket, "getaddrinfo") as dns, mock.patch.object(d, "_connect_literal") as connect:
            for url in ["http://" + good + "/card", "http://" + "a" * 56 + ".onion/card"]:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    d.inspect_card(url)
            dns.assert_not_called()
            connect.assert_not_called()

    def test_only_loopback_literal_proxy_allowed(self):
        for proxy in ["socks5h://example.org:9050", "socks5://10.0.0.1:9050", "http://127.0.0.1:9050",
                      "socks5h://u:p@127.0.0.1:9050", "socks5h://localhost:9050", "socks5h://127.0.0.1",
                      "127.0.0.1:9050/path", "127.0.0.1:9050?secret=1"]:
            with self.subTest(proxy=proxy), self.assertRaises(ValueError):
                d._proxy_parts(proxy)
        self.assertEqual(d._proxy_parts("127.0.0.1:9050"), (socket.AF_INET, "127.0.0.1", 9050))
        self.assertEqual(d._proxy_parts("socks5h://[::1]:9150"), (socket.AF_INET6, "::1", 9150))

    def test_socks_sends_domain_not_dns_and_only_local_proxy_contacted(self):
        host, sock = onion(), FakeSocket()
        info = d._validate_url("http://" + host + "/.well-known/agent-card.json")
        with mock.patch.object(d, "_connect_literal", return_value=sock) as connect, mock.patch.object(socket, "getaddrinfo") as dns:
            result = d._tor_socket(info, "127.0.0.1:9050", time.monotonic() + 20)
        dns.assert_not_called()
        self.assertIs(result, sock)
        self.assertEqual(connect.call_args.args[:3], (socket.AF_INET, "127.0.0.1", 9050))
        self.assertEqual(sock.sent[0], b"\x05\x01\x00")
        self.assertEqual(sock.sent[1], b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode() + b"\x00\x50")

    def test_socks_failure_closes_socket(self):
        sock = FakeSocket(b"\x05\xff")
        with mock.patch.object(d, "_connect_literal", return_value=sock), self.assertRaises(ValueError):
            d._tor_socket(d._validate_url("http://" + onion() + "/card"), "127.0.0.1:9050", time.monotonic() + 20)
        self.assertTrue(sock.closed)

    def test_onion_inspection_metadata_and_no_proxy_for_clearnet(self):
        with mock.patch.object(d, "_fetch_card", return_value=(CARD, "digest")):
            item = d.inspect_card("http://" + onion() + "/card", "127.0.0.1:9050")
        self.assertEqual(item["network_layer"], "tor_onion")
        self.assertEqual(item["provenance"]["transport"], "local_socks5_domain")
        with self.assertRaises(ValueError):
            d.inspect_card("https://example.org/card", "127.0.0.1:9050")


class RegistryTests(unittest.TestCase):
    def test_offline_all_providers_no_network_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "data"
            path.mkdir()
            (path / "agents_test.json").write_text(json.dumps([{"id": "x", "name": "Research", "source_url": "https://example.org", "capabilities": ["research"]}]))
            with mock.patch.object(d, "_fetch_registry") as fetch, mock.patch.object(d.directory, "_fetch") as old_fetch:
                result = d.discover("research", root=td)
            fetch.assert_not_called()
            old_fetch.assert_not_called()
        self.assertEqual(result["requests"], 0)
        self.assertEqual(len(result["provider_reports"]), 4)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["network_layer"], "local_registry")
        self.assertFalse(result["items"][0]["collaboration"]["opt_in"])

    def test_public_gate_unknown_duplicate_and_limits_before_network(self):
        cases = [{"online": True, "data_class": "controlled_genomic"}, {"online": 1}, {"limit": True},
                 {"limit": 9}, {"providers": []}, {"providers": ["mcp_registry", "mcp_registry"]},
                 {"providers": ["https://evil.example/"]}, {"providers": "mcp_registry"}]
        with mock.patch.object(d, "_fetch_registry") as fetch, mock.patch.object(d.directory, "discover") as old:
            for kwargs in cases:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    d.discover("research", **kwargs)
            fetch.assert_not_called()
            old.assert_not_called()

    def test_mcp_metadata_not_executed_and_deprecated_skipped(self):
        row = {"server": {"name": "io.example/research", "title": "Research", "description": "Literature lookup", "version": "1.2.0",
                          "packages": [{"environmentVariables": [{"name": "SECRET_KEY"}], "runtimeArguments": ["run this"]}],
                          "remotes": [{"url": "http://127.0.0.1/steal"}]},
               "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}}}
        retired = {"server": {"name": "io.example/retired"}, "_meta": {"io.modelcontextprotocol.registry/official": {"status": "deleted"}}}
        with mock.patch.object(d, "_fetch_registry", return_value={"servers": [row, retired]}) as fetch:
            result = d.discover("research", providers=["mcp_registry"], online=True)
        fetch.assert_called_once_with("research", 5)
        self.assertEqual(result["requests"], 1)
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["entity_type"], "mcp_server")
        self.assertEqual(item["revision"], "1.2.0")
        for secret in ["SECRET_KEY", "run this", "127.0.0.1"]:
            self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(item["collaboration"]["opt_in"])

    def test_registry_get_fixed_endpoint_bounded(self):
        response = Response(b'{"servers":[]}', url=d.MCP_ENDPOINT + "?search=research+genome&limit=2&version=latest")
        with mock.patch.object(d, "build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(d._fetch_registry("research genome", 2), {"servers": []})
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, response.url)
        self.assertIsNone(request.data)

    def test_provider_errors_are_honest_no_fabricated_cards(self):
        with mock.patch.object(d, "_fetch_registry", side_effect=OSError("private proxy credentials")):
            result = d.discover("research", providers=["mcp_registry"], online=True)
        self.assertEqual(result["items"], [])
        self.assertTrue(result["provider_reports"][0]["errors"])
        self.assertNotIn("credentials", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
