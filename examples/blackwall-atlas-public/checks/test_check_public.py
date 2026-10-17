#!/usr/bin/env python3
"""Regression checks mutate temporary copies, never the release package."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
from check_public import ROOT, validate


class PublicChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="atlas-check-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "demo"
        shutil.copytree(ROOT, self.root)

    def errors(self):
        return validate(self.root)[0]

    def edit_data(self, mutate):
        path = self.root / "demo-data.json"
        data = json.loads(path.read_text())
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_clean_package(self):
        self.assertEqual(self.errors(), [])

    def test_extra_file_rejected(self):
        (self.root / "unreviewed.txt").write_text("unexpected")
        self.assertTrue(self.errors())

    def test_hidden_directory_rejected(self):
        (self.root / ".metadata").mkdir()
        self.assertTrue(self.errors())

    def test_symlink_rejected(self):
        (self.root / "linked.txt").symlink_to("README.md")
        self.assertTrue(self.errors())

    def test_real_data_flag_rejected(self):
        self.edit_data(lambda data: data.update(synthetic=False))
        self.assertTrue(self.errors())

    def test_task_flag_rejected(self):
        self.edit_data(lambda data: data["tasks"][0].update(synthetic=False))
        self.assertTrue(self.errors())

    def test_duplicate_identifier_rejected(self):
        self.edit_data(lambda data: data["tasks"][0].update(id="DEMO-002"))
        self.assertTrue(self.errors())

    def test_unexpected_task_field_rejected(self):
        self.edit_data(lambda data: data["tasks"][0].update(contact="example"))
        self.assertTrue(self.errors())

    def test_email_rejected(self):
        self.edit_data(lambda data: data["tasks"][0].update(title="person" + "@" + "example.invalid"))
        self.assertTrue(self.errors())

    def test_remote_url_rejected(self):
        self.edit_data(lambda data: data["tasks"][0].update(title="https:" + "//example.invalid/"))
        self.assertTrue(self.errors())

    def test_private_path_rejected(self):
        self.edit_data(lambda data: data["tasks"][0].update(title="/" + "home/" + "example/private"))
        self.assertTrue(self.errors())

    def test_credential_shape_rejected(self):
        self.edit_data(lambda data: data["tasks"][0].update(title="ghp" + "_" + "A" * 30))
        self.assertTrue(self.errors())

    def test_missing_link_rejected(self):
        path = self.root / "index.html"
        path.write_text(path.read_text().replace('href="README.md"', 'href="missing.md"'))
        self.assertTrue(self.errors())

    def test_missing_fragment_rejected(self):
        path = self.root / "index.html"
        path.write_text(path.read_text().replace('href="#main"', 'href="#missing"'))
        self.assertTrue(self.errors())

    def test_parent_link_rejected(self):
        path = self.root / "README.md"
        path.write_text(path.read_text() + "\n[Escape](../README.md)\n")
        self.assertTrue(self.errors())

    def test_inline_handler_rejected(self):
        path = self.root / "index.html"
        path.write_text(path.read_text().replace("<body>", '<body onclick="alert(1)">'))
        self.assertTrue(self.errors())

    def test_extra_network_api_rejected(self):
        path = self.root / "app.js"
        path.write_text(path.read_text() + "\nnew " + "WebSocket" + "('blocked');\n")
        self.assertTrue(self.errors())

    def test_missing_allowlist_file_rejected(self):
        (self.root / "templates" / "task.md").unlink()
        self.assertTrue(self.errors())


if __name__ == "__main__":
    unittest.main(verbosity=2)
