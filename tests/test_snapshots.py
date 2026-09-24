"""Source-copy boundaries: pinned identity, ZIP traversal, licensing and no execution."""
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError
import zipfile

from workbench.society import snapshots


MIT = '''MIT License

Copyright (c) 2026 Example Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
'''
COMMIT = "a" * 40


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = snapshots.SnapshotStore(self.root, self.root / "state")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def archive(self, extra=None, license_text=MIT, root="demo"):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr(root + "/LICENSE", license_text)
            zipped.writestr(root + "/NOTICE", "Example source notice, retained verbatim.\n")
            zipped.writestr(root + "/run.py", "raise RuntimeError('This code must never execute')\n")
            for name, content in (extra or []):
                zipped.writestr(name, content)
        return raw.getvalue()

    def inspect_local(self, raw=None, **fields):
        path = self.root / "input.zip"
        path.write_bytes(raw if raw is not None else self.archive())
        return self.store.inspect({"repository": "owner/demo", "commit": COMMIT,
                                   "license_spdx": "MIT", "archive_path": str(path), **fields})

    def test_local_source_persists_license_notice_and_no_execution(self):
        result = self.inspect_local()
        self.assertEqual(result["status"], "source_inspected_not_executed")
        self.assertEqual(result["provenance"]["mode"], "user_supplied_archive_unverified_origin")
        self.assertIsNone(result["provenance"]["source_url"])
        self.assertFalse(result["verification"]["source_executed"])
        self.assertFalse(result["verification"]["agent_installed"])
        target = self.root / "state" / result["storage_path"]
        self.assertEqual((target / "files/demo/LICENSE").read_text(), MIT)
        self.assertIn("notice", (target / "files/demo/NOTICE").read_text())
        self.assertEqual(len(result["archive_sha256"]), 64)
        self.assertEqual(len(result["license_sha256"]), 64)
        self.assertEqual(result["file_count"], 3)
        self.assertEqual(len(result["notices"]), 1)
        self.assertEqual(self.store.items(), [result])
        self.assertEqual(json.loads((target / "inspection.json").read_text()), result)
        self.store.close()
        self.store = snapshots.SnapshotStore(self.root, self.root / "state")
        self.assertEqual(self.store.items(), [result])
        self.assertEqual((target / "files/demo/run.py").stat().st_mode & 0o111, 0)

    def test_remote_fixed_host_exact_commit_and_pinned_root(self):
        archive = self.archive(root="demo-" + COMMIT)
        with mock.patch.object(snapshots, "_download", return_value=archive) as download:
            result = self.store.inspect({"repository": "owner/demo", "commit": COMMIT.upper(), "license_spdx": "MIT"})
        download.assert_called_once_with(f"https://codeload.github.com/owner/demo/zip/{COMMIT}")
        self.assertEqual(result["provenance"]["mode"], "public_github_pinned_archive")
        self.assertFalse(result["provenance"]["repository_identity_verified"])
        with mock.patch.object(snapshots, "_download", return_value=self.archive()), self.assertRaisesRegex(ValueError, "root"):
            self.store.inspect({"repository": "owner/demo", "commit": COMMIT, "license_spdx": "MIT"})

    def test_unknown_license_restricted_license_and_declared_mismatch(self):
        for text in ("Custom license, all rights reserved.", MIT + "\nCommons Clause applies.",
                     MIT + "\nThis source may not be used for military purposes.",
                     MIT + "\nPlease observe the separate mandatory usage terms.",
                     "The text below is an example only. No rights are granted.\n" + MIT,
                     "Commercial use requires a separately purchased written authorization.\n" + MIT,
                     MIT.replace("free of charge", "subject to a mandatory royalty"),
                     MIT.replace("without restriction", "without restriction except a separate approval"),
                     "SPDX-License-Identifier: MIT"):
            with self.subTest(text=text[-60:]), self.assertRaises(ValueError):
                self.inspect_local(self.archive(license_text=text))
        with self.assertRaisesRegex(ValueError, "declared"):
            self.inspect_local(license_spdx="Apache-2.0")
        self.assertEqual(self.store.items(), [])
        self.assertEqual(list((self.root / "state/snapshots").iterdir()), [])

    def test_complete_isc_and_bsd_templates_and_nonstandard_clauses(self):
        isc = '''ISC License
Copyright (c) 2026 Example Author

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
'''
        bsd = '''BSD 2-Clause License
Copyright (c) 2026, Example Author
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
'''
        self.assertEqual(snapshots._license_kind(isc), "ISC")
        self.assertEqual(snapshots._license_kind(bsd), "BSD-2-Clause")
        bsd3 = bsd.replace("BSD 2-Clause", "BSD 3-Clause").replace(
            '\nTHIS SOFTWARE', '\n3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.\nTHIS SOFTWARE')
        self.assertEqual(snapshots._license_kind(bsd3), "BSD-3-Clause")
        for body in (isc, bsd, bsd3):
            with self.assertRaises(ValueError):
                snapshots._license_kind("Commercial use requires a separately purchased written authorization.\n" + body)
            with self.assertRaises(ValueError):
                snapshots._license_kind(body.replace("are permitted", "are permitted only by invitation").replace("is hereby granted", "is hereby granted only by invitation"))

    def test_nested_third_party_custom_license_rejected(self):
        raw = self.archive([("demo/vendor/LICENSE", "Private custom license.")])
        with self.assertRaisesRegex(ValueError, "license"):
            self.inspect_local(raw)

    def test_unsafe_paths_symlinks_duplicates_and_file_directory_conflicts(self):
        for name in ("../escape", "/tmp/escape", "demo/../../escape", "demo\\escape", "demo/C:/escape",
                     "demo/CON", "demo/space ", "demo/dot.", "demo/./escape", "demo//escape",
                     "demo/license", "demo/run.py/child"):
            with self.subTest(path=name), self.assertRaises(ValueError):
                self.inspect_local(self.archive([(name, "bad")]))
        link = zipfile.ZipInfo("demo/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(ValueError, "links"):
            self.inspect_local(self.archive([(link, "../../escape")]))
        self.assertFalse((self.root / "escape").exists())
        self.assertEqual(self.store.items(), [])

    def test_compressed_expanded_count_and_ratio_bounds(self):
        with mock.patch.object(snapshots, "MAX_ARCHIVE_BYTES", 10), self.assertRaisesRegex(ValueError, "compressed"):
            self.inspect_local()
        with mock.patch.object(snapshots, "MAX_EXPANDED_BYTES", 10), self.assertRaisesRegex(ValueError, "expanded"):
            self.inspect_local()
        with mock.patch.object(snapshots, "MAX_MEMBERS", 2), self.assertRaisesRegex(ValueError, "entry"):
            self.inspect_local()
        with self.assertRaisesRegex(ValueError, "ratio"):
            self.inspect_local(self.archive([("demo/bomb.txt", b"x" * (2 * 1024 * 1024))]))

    def test_invalid_zip_and_input_are_not_records(self):
        with self.assertRaisesRegex(ValueError, "ZIP"):
            self.inspect_local(b"not a ZIP archive")
        base = {"repository": "owner/demo", "commit": COMMIT, "license_spdx": "MIT"}
        changes = [{"repository": "https://evil.invalid/archive"}, {"repository": "owner/../repo"},
                   {"commit": "main"}, {"commit": "x" * 40}, {"license_spdx": "custom"},
                   {"license_spdx": ["MIT"]}, {"token": "do-not-store"}, {"archive_path": 3}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.inspect({**base, **change})
        with self.assertRaises(ValueError):
            self.store.inspect([])
        self.assertEqual(self.store.items(), [])

    def test_failed_database_write_removes_extracted_copy(self):
        self.store._conn.close()
        with self.assertRaises(Exception):
            self.inspect_local()
        self.assertEqual(list((self.root / "state/snapshots").iterdir()), [])

    def test_network_redirect_and_oversize_rejected_without_following(self):
        with self.assertRaisesRegex(ValueError, "redirect"):
            snapshots._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid/")
        response = mock.MagicMock()
        response.geturl.return_value = "https://codeload.github.com/owner/demo/zip/" + COMMIT
        response.status = 200
        response.headers = {"Content-Length": str(snapshots.MAX_ARCHIVE_BYTES + 1)}
        response.__enter__.return_value = response
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(snapshots, "build_opener", return_value=opener), self.assertRaisesRegex(ValueError, "compressed"):
            snapshots._download(response.geturl())
        response.read1.assert_not_called()
        opener.open.side_effect = URLError("possible-sensitive-network-details")
        with mock.patch.object(snapshots, "build_opener", return_value=opener), self.assertRaisesRegex(ValueError, "download failed") as ctx:
            snapshots._download(response.geturl())
        self.assertNotIn("sensitive", str(ctx.exception))

    def test_download_budget_checked_after_one_underlying_read(self):
        url = f"https://codeload.github.com/owner/demo/zip/{COMMIT}"
        response = mock.MagicMock()
        response.geturl.return_value = url
        response.status = 200
        response.headers = {}
        response.__enter__.return_value = response
        response.read1.return_value = b"slow bytes"
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(snapshots, "build_opener", return_value=opener), \
                mock.patch.object(snapshots.time, "monotonic", side_effect=[0, 0, 26]), \
                self.assertRaisesRegex(ValueError, "time budget"):
            snapshots._download(url)
        response.read1.assert_called_once()
        response.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
