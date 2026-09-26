"""Offline HTTP boundary tests with stubbed control; never start a model."""
import http.client
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from workbench.cloud_auth import AuthError
from workbench.server import make_server
from workbench.service import ServiceError


class FixtureAuth:
    config = SimpleNamespace(public_origin="https://research.example")

    def session(self, cookie):
        if cookie != "fixture=owner":
            raise AuthError("unauthorized")
        return {"subject": "owner", "csrf_token": "csrf-fixture", "expires_at": 1234}

    def authorize_write(self, cookie, origin, token):
        session = self.session(cookie)
        if origin != self.config.public_origin or token != session["csrf_token"]:
            raise AuthError("csrf_rejected")
        return session


class OfflineHTTPBase:
    cloud = False

    def setUp(self):
        self.offline = Mock(spec=["snapshot", "start", "stop"])
        self.offline.snapshot.return_value = {
            "schema_version": 1, "running": False,
            "jobs": [{"id": "a" * 64, "question": "PRIVATE_LOCAL_QUESTION"}],
        }
        self.offline.start.return_value = {
            "id": "b" * 64, "status": "reserved", "duplicate_suppressed": False,
        }
        self.offline.stop.return_value = {"stop_requested": True}
        service = SimpleNamespace(offline=self.offline)
        self.server = make_server(service, port=0,
                                  cloud_auth=FixtureAuth() if self.cloud else None)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def request(self, path, data=None, *, cookie="", csrf="", host=None, origin=None,
                fetch_site=None):
        port = self.server.server_address[1]
        default_host = "research.example" if self.cloud else "127.0.0.1:" + str(port)
        default_origin = "https://research.example" if self.cloud else "http://" + default_host
        headers = {"Host": host or default_host, "Cookie": cookie}
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers.update({"Content-Type": "application/json",
                            "Origin": origin or default_origin, "X-CSRF-Token": csrf})
        elif origin is not None:
            headers["Origin"] = origin
        if fetch_site is not None:
            headers["Sec-Fetch-Site"] = fetch_site
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("POST" if body is not None else "GET", path, body, headers)
            response = connection.getresponse()
            content = response.read()
            return response.status, json.loads(content), dict(response.getheaders())
        finally:
            connection.close()

    def assert_controller_untouched(self):
        self.offline.snapshot.assert_not_called()
        self.offline.start.assert_not_called()
        self.offline.stop.assert_not_called()


class OfflineLocalHTTPTests(OfflineHTTPBase, unittest.TestCase):
    def test_local_snapshot_is_available_and_not_cached(self):
        status, result, headers = self.request("/api/offline")
        self.assertEqual(status, 200)
        self.assertEqual(result, self.offline.snapshot.return_value)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.offline.snapshot.assert_called_once_with()
        self.offline.start.assert_not_called()
        self.offline.stop.assert_not_called()

    def test_local_start_and_stop_dispatch_only_to_their_handlers(self):
        body = {"question": "What observation falsifies H?",
                "request_id": "http-offline-1", "public_data_confirmed": True}
        status, result, _ = self.request("/api/offline/start", body)
        self.assertEqual(status, 202)
        self.assertEqual(result, self.offline.start.return_value)
        self.offline.start.assert_called_once_with(body)
        self.offline.stop.assert_not_called()
        status, result, _ = self.request("/api/offline/stop", {})
        self.assertEqual((status, result), (200, {"stop_requested": True}))
        self.offline.stop.assert_called_once_with()
        self.offline.snapshot.assert_not_called()

    def test_stop_rejects_nonempty_or_nonobject_body_before_controller(self):
        for body in ({"scope": "all"}, [], {"force": True}, "stop"):
            with self.subTest(body=body):
                status, _, _ = self.request("/api/offline/stop", body)
                self.assertEqual(status, 400)
        self.assert_controller_untouched()

    def test_local_routes_reject_foreign_host_origin_and_fetch_site(self):
        for path, body in (("/api/offline", None), ("/api/offline/start", {}),
                           ("/api/offline/stop", {})):
            for options in ({"host": "attacker.example"},
                            {"origin": "https://attacker.example"},
                            {"fetch_site": "cross-site"}):
                with self.subTest(path=path, options=options):
                    status, _, _ = self.request(path, body, **options)
                    self.assertEqual(status, 403)
        self.assert_controller_untouched()

    def test_retryable_admission_error_is_returned_as_409(self):
        self.offline.start.side_effect = ServiceError("Admission busy", "offline_busy", 409)
        status, result, _ = self.request("/api/offline/start", {
            "question": "Public question", "request_id": "busy-id", "public_data_confirmed": True,
        })
        self.assertEqual(status, 409)
        self.assertEqual(result["error"]["code"], "offline_busy")
        self.offline.start.assert_called_once()
        self.offline.stop.assert_not_called()


class OfflineCloudHTTPTests(OfflineHTTPBase, unittest.TestCase):
    cloud = True

    def test_cloud_requires_session_before_offline_route(self):
        for path, body in (("/api/offline", None), ("/api/offline/start", {}),
                           ("/api/offline/stop", {})):
            with self.subTest(path=path):
                status, result, _ = self.request(path, body)
                self.assertEqual(status, 401)
                self.assertNotIn("PRIVATE_LOCAL_QUESTION", json.dumps(result))
        self.assert_controller_untouched()

    def test_authenticated_cloud_get_is_local_only_without_disclosure(self):
        status, result, headers = self.request("/api/offline", cookie="fixture=owner")
        self.assertEqual(status, 403)
        self.assertEqual(result["error"]["code"], "local_only")
        self.assertNotIn("PRIVATE_LOCAL_QUESTION", json.dumps(result))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assert_controller_untouched()

    def test_authenticated_cloud_start_and_stop_are_local_only(self):
        for path in ("/api/offline/start", "/api/offline/stop"):
            with self.subTest(path=path):
                status, result, _ = self.request(path, {}, cookie="fixture=owner",
                                                csrf="csrf-fixture")
                self.assertEqual(status, 403)
                self.assertEqual(result["error"]["code"], "local_only")
        self.assert_controller_untouched()

    def test_cloud_write_still_requires_csrf_before_local_only_check(self):
        for path in ("/api/offline/start", "/api/offline/stop"):
            with self.subTest(path=path):
                status, result, _ = self.request(path, {}, cookie="fixture=owner")
                self.assertEqual(status, 403)
                self.assertEqual(result["error"]["code"], "csrf_rejected")
        self.assert_controller_untouched()


if __name__ == "__main__":
    unittest.main()

