"""The registry must never convert metadata into secret storage or fake auth."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench.network.accounts import Accounts


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "accounts.sqlite3"
        self.accounts = Accounts(self.path)

    def tearDown(self):
        self.accounts.close()
        self.tmp.cleanup()

    @staticmethod
    def profile(**changes):
        return {"provider": "research_cloud", "domain": "research.example.org", "auth_mode": "api_key",
                "credential_env": "META_RESEARCH_CREDENTIAL", "scopes": ["models:read"],
                "setup_url": "https://research.example.org/settings", **changes}

    def test_public_providers_need_no_registration_but_are_not_remote_verified(self):
        profiles = {p["provider"]: p for p in self.accounts.list()["items"]}
        self.assertEqual(set(profiles), {"europepmc", "crossref"})
        for provider in profiles:
            self.assertFalse(profiles[provider]["remote_auth_verified"])
            self.assertFalse(profiles[provider]["credential_present"])
            request = self.accounts.request({"provider": provider, "reason": "metadata search"})
            self.assertEqual(request["status"], "no_registration_required")
            self.assertFalse(request["external_action_performed"])

    def test_persistence_idempotence_and_profile_update_keep_creation_time(self):
        saved = self.accounts.save(self.profile())
        request = self.accounts.request({"provider": "research_cloud", "reason": "model evaluation"})
        self.assertEqual(request["status"], "requires_user_action")
        self.assertEqual(request, self.accounts.request({"provider": "research_cloud", "reason": "model evaluation"}))
        self.accounts.close()
        self.accounts = Accounts(self.path)
        self.assertEqual(self.accounts.list()["requests"], [request])
        updated = self.accounts.save(self.profile(scopes=["models:read", "jobs:read"]))
        self.assertEqual(saved["created_at"], updated["created_at"])
        self.assertGreaterEqual(updated["updated_at"], saved["updated_at"])
        self.assertEqual(len(self.accounts.list()["items"]), 3)

    def test_environment_presence_is_dynamic_and_never_exports_secret(self):
        secret = "private-test-value-do-not-export"
        with patch.dict(os.environ, {"META_RESEARCH_CREDENTIAL": secret}):
            profile = self.accounts.save(self.profile())
            self.assertTrue(profile["credential_present"])
            self.assertEqual(profile["status"], "credential_present_unverified")
            self.assertFalse(profile["remote_auth_verified"])
            self.assertNotIn(secret, json.dumps(self.accounts.list()))
        with patch.dict(os.environ, {}, clear=True):
            profile = next(p for p in self.accounts.list()["items"] if p["provider"] == "research_cloud")
            self.assertFalse(profile["credential_present"])
            self.assertEqual(profile["status"], "requires_setup")
        self.accounts._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertNotIn(secret.encode(), self.path.read_bytes())

    def test_rejects_secret_fields_raw_credentials_and_fake_status(self):
        for changes in ({"api_key": "secret"}, {"token": "secret"}, {"status": "authenticated"},
                        {"credential_env": "sk-abcdefghijklmnopqrst"},
                        {"credential_env": "Bearer actual-secret"},
                        {"credential_env": "MY_API_KEY=secret"}, {"credential_env": "lowercase_name"}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                self.accounts.save(self.profile(**changes))
        self.accounts.save(self.profile())
        for changes in ({"token": "secret"}, {"status": "complete"}, {"reason": "api_key=raw-value"}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                self.accounts.request({"provider": "research_cloud", "reason": "research", **changes})

    def test_domain_and_setup_url_confinement(self):
        domains = ["https://example.org", "user@example.org", "example.org/path", "example.org:443",
                   "127.0.0.1", "localhost", "example.org\\other", "example..org", "-evil.example.org"]
        for domain in domains:
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                self.accounts.save(self.profile(domain=domain))
        urls = ["http://research.example.org/setup", "https://evil.example.org/setup",
                "https://research.example.org.evil.com/setup", "https://user:pass@research.example.org/setup",
                "https://research.example.org:444/setup", "https://research.example.org/setup?token=abc",
                "https://research.example.org/setup#secret", "https://research.example.org\\evil", None, []]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.accounts.save(self.profile(setup_url=url))

    def test_unreviewed_service_is_not_declared_public_and_providers_are_bounded(self):
        saved = self.accounts.save(self.profile(auth_mode="none", credential_env=""))
        self.assertEqual(saved["status"], "access_unverified")
        self.assertEqual(self.accounts.request({"provider": "research_cloud", "reason": "search"})["status"], "requires_user_action")
        with self.assertRaises(KeyError):
            self.accounts.request({"provider": "unknown", "reason": "search"})
        with self.assertRaises(ValueError):
            self.accounts.save(self.profile(provider="europepmc"))
        for changes in ({"scopes": "read"}, {"scopes": [True]}, {"scopes": ["x"] * 33},
                        {"auth_mode": "none"}, {"auth_mode": []}, {"provider": "bad/provider"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.accounts.save(self.profile(**changes))

    def test_public_metadata_profile_cannot_imply_write_access(self):
        original = next(p for p in self.accounts.list()["items"] if p["provider"] == "crossref")
        profile = {k: original[k] for k in ("provider", "domain", "auth_mode", "credential_env", "scopes", "setup_url")}
        profile["scopes"] = ["metadata:write"]
        self.accounts.save(profile)
        request = self.accounts.request({"provider": "crossref", "reason": "deposit metadata"})
        self.assertEqual(request["status"], "requires_user_action")

    def test_two_coordinators_share_one_idempotent_handoff(self):
        other = Accounts(self.path)
        try:
            body = {"provider": "europepmc", "reason": "shared metadata task"}
            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(self.accounts.request, body)
                b = pool.submit(other.request, body)
                self.assertEqual(a.result(), b.result())
            self.assertEqual(len(other.list()["requests"]), 1)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
