import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from workbench.federation.client import (
    ClientError, ParticipantClient, MAX_BODY, _decode, _encode,
    load_credentials, main, save_credentials, validate_base_url,
)


class FederationClientTest(unittest.TestCase):
    def test_remote_http_credentials_and_url_injection_rejected(self):
        for url in ("http://example.org", "https://u:p@example.org", "https://example.org/path",
                    "https://example.org/?token=secret", "https://example.org/#fragment",
                    "http://127.0.0.1.example.org", "https://example.org\\@127.0.0.1", "https://example.org\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_base_url(url)
        self.assertEqual(validate_base_url("http://127.0.0.1:8767/"), "http://127.0.0.1:8767")
        self.assertEqual(validate_base_url("https://example.org"), "https://example.org")

    def test_nonfinite_duplicate_and_oversized_json_rejected(self):
        for raw in (b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1,"x":2}', b'"' + b'a' * MAX_BODY + b'"'):
            with self.assertRaises(ValueError):
                _decode(raw)
        with self.assertRaises(ValueError):
            _encode({"x": float("nan")})
        deep = []
        for _ in range(30):
            deep = [deep]
        with self.assertRaises(ValueError):
            _encode(deep)

    def test_credentials_private_bound_to_origin_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state" / "member.json"
            token = "x" * 48
            save_credentials(path, "http://127.0.0.1:8767", "member-demo", token)
            self.assertEqual(load_credentials(path, "http://127.0.0.1:8767"), token)
            with self.assertRaises(ValueError):
                load_credentials(path, "http://127.0.0.1:8768")
            with self.assertRaises(FileExistsError):
                save_credentials(path, "http://127.0.0.1:8767", "another", "y" * 48)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    load_credentials(path, "http://127.0.0.1:8767")

    @contextlib.contextmanager
    def server(self, mode, seen):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if mode == "redirect" and self.path == "/v1/member":
                    self.send_response(302)
                    self.send_header("Location", "/unexpected")
                    self.end_headers()
                    return
                raw = {"valid": b'{"items":[]}', "nonfinite": b'{"x":NaN}',
                       "oversized": b'"' + b'a' * MAX_BODY + b'"',
                       "redirect": b'{"stolen":true}'}[mode]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_authenticated_redirect_is_never_followed(self):
        seen = []
        with self.server("redirect", seen) as url:
            with self.assertRaises(ClientError) as raised:
                ParticipantClient(url, token="x" * 48).member()
            self.assertEqual(raised.exception.status, 302)
        self.assertEqual(seen, [("/v1/member", "Bearer " + "x" * 48)])

    def test_response_bounds_and_nonfinite_guard(self):
        for mode in ("nonfinite", "oversized"):
            with self.subTest(mode=mode), self.server(mode, []) as url:
                with self.assertRaises(ClientError):
                    ParticipantClient(url).offers()

    def test_environment_proxy_is_not_used(self):
        seen = []
        with self.server("valid", seen) as url:
            with mock.patch.dict(os.environ, {"http_proxy": "http://secret:secret@127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1", "no_proxy": "", "NO_PROXY": ""}):
                self.assertEqual(ParticipantClient(url).offers(), {"items": []})
        self.assertEqual(seen, [("/v1/offers", None)])

    def test_join_requires_acceptance_and_cli_never_prints_token(self):
        with self.assertRaises(ValueError):
            ParticipantClient().join("i" * 48, "test", ["review"], "hash", accepted_terms=False)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "credentials.json"
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"META_INVITE_TOKEN": "i" * 48}), \
                    mock.patch.object(ParticipantClient, "join", return_value={"member_id": "member-demo", "member_token": "s" * 48}), \
                    contextlib.redirect_stdout(out):
                code = main(["--credentials", str(path), "join", "--name", "demo", "--capabilities", "review",
                             "--terms-hash", "hash", "--accept-terms"])
            self.assertEqual(code, 0)
            self.assertNotIn("s" * 48, out.getvalue())
            self.assertNotIn("member_token", out.getvalue())
            self.assertTrue(json.loads(out.getvalue())["credentials_saved"])
            self.assertEqual(load_credentials(path, "http://127.0.0.1:8767"), "s" * 48)


if __name__ == "__main__":
    unittest.main()
