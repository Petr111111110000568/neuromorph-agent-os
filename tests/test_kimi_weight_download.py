"""No network or model execution: streaming/range/durability fixtures only."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download_kimi_weights.py"
SPEC = importlib.util.spec_from_file_location("download_kimi_weights_fixture", SCRIPT)
download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download)


class Response(io.BytesIO):
    def __init__(self, data, start, end, total, *, status=206, headers=None, url="https://huggingface.co/test"):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Range": f"bytes {start}-{end}/{total}", "Content-Length": str(end - start + 1)}
        self.headers.update(headers or {})
        self.url = url

    def geturl(self):
        return self.url


class RangeOpener:
    def __init__(self, body, actions=None):
        self.body = body
        self.actions = list(actions or [])
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", request.get_header("Range")).groups())
        action = self.actions.pop(0) if self.actions else None
        if isinstance(action, Exception):
            raise action
        if callable(action):
            return action(start, end)
        return Response(self.body[start:end + 1], start, end, len(self.body))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class KimiWeightDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.body = b"0123456789abcdef"
        self.name = "model-00001-of-000096.safetensors"
        self.entry = {"path": self.name, "bytes": len(self.body), "sha256": hashlib.sha256(self.body).hexdigest()}
        self.state = download.State(self.root, [self.name])
        self.stop = self.root / "STOP"
        self.final = self.root / self.name
        self.partial = self.root / (self.name + ".part")
        self.clock = Clock()
        self.opener = RangeOpener(self.body)

    def run_download(self):
        with patch.object(download, "RANGE_BYTES", 4), patch.object(download, "READ_BYTES", 2):
            download.download_file(self.entry, self.root, self.state, self.stop, self.opener,
                clock=self.clock.time, sleep=self.clock.sleep)

    def test_streamed_shards_are_promoted_only_after_sha_and_no_auth(self):
        self.run_download()
        self.assertEqual(self.final.read_bytes(), self.body)
        self.assertFalse(self.partial.exists())
        self.assertEqual(self.state.value["verified"], [self.name])
        self.assertEqual(len(self.opener.requests), 4)
        for request in self.opener.requests:
            self.assertIsNone(request.get_header("Authorization"))
            self.assertIsNone(request.get_header("Cookie"))
            self.assertIn(download.REVISION, request.full_url)
            self.assertTrue(request.full_url.endswith(self.name))
        self.run_download()
        self.assertEqual(len(self.opener.requests), 4)

    def test_resume_hashes_existing_partial_and_requests_exact_tail(self):
        self.partial.write_bytes(self.body[:6])
        self.run_download()
        self.assertEqual(self.opener.requests[0].get_header("Range"), "bytes=6-9")
        self.assertEqual(self.final.read_bytes(), self.body)

    def test_200_never_appended_to_partial(self):
        self.partial.write_bytes(self.body[:4])
        self.opener = RangeOpener(self.body, [lambda start, end: Response(self.body, start, end, len(self.body), status=200)])
        with self.assertRaisesRegex(download.DownloadError, "range_not_honoured"):
            self.run_download()
        self.assertEqual(self.partial.read_bytes(), self.body[:4])
        self.assertFalse(self.final.exists())

    def test_bad_range_length_compression_host_rejected_before_write(self):
        for changes in ({"headers": {"Content-Range": "bytes 1-4/16"}},
                        {"headers": {"Content-Length": "16"}},
                        {"headers": {"Content-Encoding": "gzip"}},
                        {"url": "https://evil.example/weights"}):
            with self.subTest(changes=changes):
                self.opener = RangeOpener(self.body, [lambda start, end, opts=changes:
                    Response(self.body[start:end + 1], start, end, len(self.body), **opts)])
                with self.assertRaises(download.DownloadError):
                    self.run_download()
                self.assertFalse(self.partial.exists())
                self.assertFalse(self.final.exists())

    def test_short_response_retries_from_bytes_actually_saved(self):
        self.opener = RangeOpener(self.body, [lambda start, end: Response(self.body[:2], start, end, len(self.body))])
        self.run_download()
        self.assertEqual(self.opener.requests[1].get_header("Range"), "bytes=2-5")
        self.assertEqual(self.final.read_bytes(), self.body)
        self.assertEqual(self.state.value["retries"][self.name], 1)

    def test_corruption_keeps_part_and_cannot_gain_final_name(self):
        self.partial.write_bytes(b"X" + self.body[1:])
        with self.assertRaisesRegex(download.DownloadError, "hash_mismatch"):
            self.run_download()
        self.assertTrue(self.partial.exists())
        self.assertFalse(self.final.exists())
        self.assertEqual(self.opener.requests, [])

    def test_existing_final_is_rehashed_not_trusted_from_filename(self):
        self.final.write_bytes(b"X" + self.body[1:])
        with self.assertRaisesRegex(download.DownloadError, "existing_weight_hash_mismatch"):
            self.run_download()
        self.assertEqual(self.opener.requests, [])

    def test_stop_file_prevents_any_new_request(self):
        self.stop.touch()
        with self.assertRaises(download.Stopped):
            self.run_download()
        self.assertEqual(self.opener.requests, [])
        self.assertFalse(self.partial.exists())

    def test_five_retries_persist_across_restart_without_reset(self):
        self.opener = RangeOpener(self.body, [URLError("secret signed URL") for _ in range(8)])
        with self.assertRaisesRegex(download.DownloadError, "file_retry_limit_reached"):
            self.run_download()
        self.assertEqual(len(self.opener.requests), 6)
        self.assertEqual(self.state.value["retries"][self.name], 6)
        self.state = download.State(self.root, [self.name])
        with self.assertRaisesRegex(download.DownloadError, "file_retry_limit_reached"):
            self.run_download()
        self.assertEqual(len(self.opener.requests), 6)
        self.assertNotIn("secret", self.state.path.read_text())

    def test_rate_limit_long_retry_after_is_saved_and_never_retried_early(self):
        failure = HTTPError("https://SECRET-SIGNED-URL", 429, "SECRET-BODY", {"Retry-After": "3600"}, None)
        self.opener = RangeOpener(self.body, [failure])
        with self.assertRaisesRegex(download.DownloadError, "retry_later"):
            self.run_download()
        self.assertEqual(len(self.opener.requests), 1)
        self.state = download.State(self.root, [self.name])
        with self.assertRaisesRegex(download.DownloadError, "retry_later"):
            self.run_download()
        self.assertEqual(len(self.opener.requests), 1)
        self.assertNotIn("SECRET", self.state.path.read_text())

    def test_access_denied_does_not_retry_or_request_credentials(self):
        self.opener = RangeOpener(self.body, [HTTPError("https://SECRET", 403, "SECRET", {}, None)])
        with self.assertRaisesRegex(download.DownloadError, "http_access_or_request_rejected"):
            self.run_download()
        self.assertEqual(len(self.opener.requests), 1)
        self.assertEqual(self.state.value["retries"], {})

    def test_disk_reserve_counts_all_remaining_bytes_and_existing_partial(self):
        with self.assertRaisesRegex(download.DownloadError, "insufficient_disk_reserve"):
            download.check_disk(self.root, [self.entry], free_bytes=download.RESERVE_BYTES + 15)
        self.partial.write_bytes(self.body[:8])
        self.assertEqual(download.check_disk(self.root, [self.entry], free_bytes=download.RESERVE_BYTES + 8), 8)
        self.final.write_bytes(self.body)
        with self.assertRaisesRegex(download.DownloadError, "conflicting_weight_files"):
            download.check_disk(self.root, [self.entry], free_bytes=10**15)

    def test_redirects_require_exact_https_allowlist_and_hide_signed_urls(self):
        for url in ("http://huggingface.co/file", "https://huggingface.co.evil.example/file",
                    "https://user:password@huggingface.co/file", "https://huggingface.co:8443/file", "https://localhost/file"):
            self.assertFalse(download.allowed_url(url))
            with self.assertRaisesRegex(download.DownloadError, "unapproved_redirect_host"):
                download.FixedRedirect().redirect_request(Request("https://huggingface.co/file"), None, 302, "", {}, url)
        self.assertTrue(download.allowed_url("https://huggingface.co/file?signature=opaque"))

    def test_exclusive_os_lock_blocks_competing_download(self):
        with download.exclusive_lock(self.root / "download.lock"):
            with self.assertRaisesRegex(download.DownloadError, "download_already_running"):
                with download.exclusive_lock(self.root / "download.lock"):
                    self.fail("Concurrent lock was acquired")

    def test_manifest_exact_pin_and_96_shards(self):
        manifest = download.load_manifest(SCRIPT.parents[1] / "config" / "kimi_weights_manifest.json")
        self.assertEqual(len(manifest["files"]), 96)
        self.assertEqual(sum(x["bytes"] for x in manifest["files"]), 1560936091448)
        tampered = self.root / "manifest.json"
        tampered.write_text(json.dumps(manifest) + " ")
        with self.assertRaisesRegex(download.DownloadError, "manifest_pin_mismatch"):
            download.load_manifest(tampered)

    def test_progress_history_is_finite_and_contains_no_urls(self):
        for _ in range(30):
            self.state.event("fixture", self.name)
        self.assertEqual(len(self.state.value["history"]), 20)
        self.assertNotIn("https://", self.state.path.read_text())
        self.assertLess(self.state.path.stat().st_size, 65536)


if __name__ == "__main__":
    unittest.main()
