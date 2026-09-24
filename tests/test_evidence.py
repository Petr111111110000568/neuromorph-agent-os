import hashlib
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from workbench.society import evidence as e


class Response(io.BytesIO):
    def __init__(self, content, url, content_type="application/json", length=None, encoding=None):
        super().__init__(content)
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if length is not None:
            self.headers["Content-Length"] = str(length)
        if encoding is not None:
            self.headers["Content-Encoding"] = encoding

    def geturl(self):
        return self.url


def payload(*rows):
    return {"resultList": {"result": list(rows)}}


class EvidenceTests(unittest.TestCase):
    def test_html_normalization_hash_and_twelve_word_excerpt(self):
        abstract = "<p>No human <b>clinical trials</b> were available.</p><p>Simulation and mouse cell culture support only a computational model.</p><script>EXECUTE_SECRET_CODE</script>"
        normalized = "No human clinical trials were available. Simulation and mouse cell culture support only a computational model."
        fixture = payload({"title": "<b>Model</b> &amp; evidence", "id": "1234", "source": "MED", "doi": "10.1234/Test", "abstractText": abstract})
        with patch.object(e, "_fetch", return_value=fixture):
            output = e.collect("model", limit=1)
        item = output["items"][0]
        self.assertEqual(output["mode"], "public_abstract_triage")
        self.assertEqual(output["requests"], 1)
        self.assertEqual(item["title"], "Model & evidence")
        self.assertEqual(item["id"], "doi:10.1234/test")
        self.assertEqual(item["url"], "https://doi.org/10.1234/Test")
        self.assertEqual(item["abstract_sha256"], hashlib.sha256(normalized.encode()).hexdigest())
        self.assertEqual(len(item["excerpt"].split()), 12)
        self.assertTrue(item["excerpt"].endswith("…"))
        self.assertEqual(set(item["indicators"]), {"human", "animal", "computational", "in_vitro", "negation_present"})
        self.assertIn("human", item["indicator_matches"]["human"])
        self.assertTrue(any("отрицания" in warning for warning in item["limitations"]))
        self.assertNotIn("EXECUTE_SECRET_CODE", json.dumps(output))
        self.assertNotIn(normalized, json.dumps(output))
        self.assertNotIn("abstractText", item)

    def test_absent_and_non_text_abstracts_are_metadata_only(self):
        fixture = payload({"title": "One", "id": "1", "source": "MED"},
                          {"title": "Two", "abstractText": {"command": "never run"}},
                          {"title": "Three", "abstractText": "<script>malicious</script>"})
        with patch.object(e, "_fetch", return_value=fixture):
            output = e.collect("model")
        self.assertEqual(len(output["items"]), 3)
        for item in output["items"]:
            self.assertFalse(item["abstract_available"])
            self.assertIsNone(item["abstract_sha256"])
            self.assertEqual(item["indicators"], [])
            self.assertNotIn("excerpt", item)
        self.assertEqual(output["items"][0]["url"], "https://europepmc.org/article/MED/1")

    def test_indicator_word_boundaries_and_english_scope(self):
        self.assertEqual(e._indicators("Rates in humanity were unchanged."), {})
        self.assertEqual(set(e._indicators("In vitro HUMAN studies were not randomized.")), {"in_vitro", "human", "negation_present", "randomized"})
        # Absence of an English marker is not proof of absence of a study type.
        self.assertEqual(e._indicators("Исследование клеточной культуры"), {})

    def test_duplicate_doi_keeps_available_abstract_and_has_fixed_urls(self):
        fixture = payload({"title": "No abstract", "doi": "10.1234/Test", "url": "file:///private"},
                          {"title": "With abstract", "doi": "10.1234/test", "abstractText": "A computational model.", "commands": ["never run"]},
                          {"title": "Unsafe identifiers", "doi": "javascript:alert(1)", "source": "https://evil.invalid", "id": "../secrets"})
        with patch.object(e, "_fetch", return_value=fixture):
            output = e.collect("model")
        self.assertEqual(len(output["items"]), 2)
        self.assertTrue(output["items"][0]["abstract_available"])
        self.assertEqual(output["items"][1]["url"], "")
        self.assertNotIn("commands", json.dumps(output))
        self.assertNotIn("file:///private", json.dumps(output))

    def test_invalid_rows_are_skipped_but_invalid_container_rejected(self):
        self.assertEqual(e._parse(payload(None, {}, {"title": 7}), 3), [])
        for value in ([], {}, {"resultList": []}, {"resultList": {"result": {}}}, payload(*[{"title": "A"}] * 9)):
            with self.subTest(value=value), patch.object(e, "_fetch", return_value=value):
                output = e.collect("model")
                self.assertEqual(output["items"], [])
                self.assertEqual(output["errors"][0]["code"], "invalid_response")

    def test_abstract_character_overflow_fails_closed(self):
        with patch.object(e, "_fetch", return_value=payload({"title": "Huge", "abstractText": "x" * (e.MAX_ABSTRACT_CHARACTERS + 1)})):
            output = e.collect("model")
        self.assertEqual(output["items"], [])
        self.assertEqual(output["errors"][0]["code"], "invalid_response")

    def test_offline_never_fetches_or_claims_abstract_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "evidence_sources.json").write_text(json.dumps([
                {"id": "paper-one", "title": "Epigenetics model", "url": "https://example.org/paper", "description": "modeling"},
                {"id": "other", "title": "Volcano geography", "url": "https://example.org/other"},
            ]), encoding="utf-8")
            with patch.object(e, "_fetch", side_effect=AssertionError("Network forbidden")):
                output = e.collect("epigenetics", offline=True, root=root)
                empty = e.collect("nonmatchingzqx", offline=True, root=root)
        self.assertEqual(output["mode"], "offline_index")
        self.assertEqual(output["requests"], 0)
        self.assertEqual([item["id"] for item in output["items"]], ["paper-one"])
        self.assertFalse(output["items"][0]["abstract_available"])
        self.assertEqual(output["items"][0]["provenance"]["retrieval_kind"], "local_index_only")
        self.assertEqual(empty["items"], [])

    def test_input_types_and_bounds(self):
        for kwargs in ({"query": ""}, {"query": None}, {"query": "x" * 2001}, {"query": "x\x00"},
                       {"query": "x", "limit": True}, {"query": "x", "limit": 0},
                       {"query": "x", "limit": 9}, {"query": "x", "offline": "false"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                e.collect(**kwargs)
        with patch.object(e, "_fetch", return_value=payload()):
            self.assertEqual(e.collect(" a\n b\t c ")["query"], "a b c")

    def test_provider_errors_are_redacted_and_not_retried(self):
        for error, code in ((HTTPError("url", 429, "secret", {}, None), "http_error"),
                            (TimeoutError("secret"), "timeout"), (URLError("secret"), "network_error"),
                            (ValueError("secret"), "invalid_response")):
            with self.subTest(code=code), patch.object(e, "_fetch", side_effect=error) as fetch:
                output = e.collect("model")
                fetch.assert_called_once()
            self.assertEqual(output["errors"][0]["code"], code)
            self.assertEqual(output["requests"], 1)
            self.assertNotIn("secret", json.dumps(output))

    def test_fetch_uses_fixed_core_endpoint_and_no_redirect(self):
        self_test = self

        class Opener:
            def open(self, request, timeout):
                self_test.assertTrue(request.full_url.startswith(e.ENDPOINT + "?"))
                self_test.assertIn("resultType=core", request.full_url)
                self_test.assertIn("http%3A%2F%2F127.0.0.1", request.full_url)
                self_test.assertEqual(timeout, e.TIMEOUT_SECONDS)
                return Response(b'{"resultList":{"result":[]}}', request.full_url)

        with patch.object(e, "build_opener", return_value=Opener()) as factory:
            self.assertEqual(e._fetch("http://127.0.0.1", 1), payload())
        handlers = factory.call_args.args
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], e._NoRedirect)
        with self.assertRaises(ValueError):
            e._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid/")

    def test_fetch_rejects_bad_envelope_payload_size_and_json(self):
        cases = (
            lambda u: Response(b"{}", "https://evil.invalid/"),
            lambda u: Response(b"{}", u, content_type="text/html"),
            lambda u: Response(b"{}", u, encoding="gzip"),
            lambda u: Response(b"{}", u, length=e.MAX_RESPONSE_BYTES + 1),
            lambda u: Response(b"{}", u, length="-1"),
            lambda u: Response(b"{}", u, length="abc"),
            lambda u: Response(b" " * (e.MAX_RESPONSE_BYTES + 1), u),
            lambda u: Response(b'{"x":NaN}', u),
            lambda u: Response(b"not-json", u),
            lambda u: Response(b"\xff", u),
        )
        for fixture in cases:
            class Opener:
                def open(self, request, timeout):
                    return fixture(request.full_url)

            with self.subTest(fixture=fixture), patch.object(e, "build_opener", return_value=Opener()), self.assertRaises((ValueError, UnicodeError)):
                e._fetch("model", 1)

    def test_fetch_checks_elapsed_budget_after_read(self):
        class Opener:
            def open(self, request, timeout):
                return Response(b"{}", request.full_url)

        with patch.object(e, "build_opener", return_value=Opener()), patch.object(e.time, "monotonic", side_effect=[0, 0, e.READ_BUDGET_SECONDS + 1]), self.assertRaises(TimeoutError):
            e._fetch("model", 1)


if __name__ == "__main__":
    unittest.main()
