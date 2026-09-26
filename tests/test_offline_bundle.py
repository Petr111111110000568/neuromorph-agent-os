"""Offline installer invariants; fixtures are inert data, never executed."""
import hashlib
import errno
import json
from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import offline_bundle as bundle


class OfflineBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.tracked = []
        self.add("workbench/__main__.py", b"raise RuntimeError('must never execute during install')\n")
        self.add("README.md", b"Portable source fixture\n")
        self.add("config/resource_policy.json", b'{"max_spend":0}\n')
        self.add("scripts/offline_bundle.py", b"# installer fixture\n")
        self.git = patch.object(bundle, "_git", side_effect=self.fake_git)
        self.git.start()

    def tearDown(self):
        self.git.stop()
        self.tmp.cleanup()

    def add(self, name, data):
        file = self.source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        self.tracked.append(name)
        return file

    def fake_git(self, root, *args):
        if args[0] == "rev-parse":
            return b"a" * 40 + b"\n"
        if args[0] == "status":
            return b""
        return b"".join(b"100644 " + b"b" * 40 + b" 0\t" + n.encode() + b"\0" for n in self.tracked)

    def build(self, name="bundle.zip", **kwargs):
        target = self.root / name
        result = bundle.build(self.source, target, **kwargs)
        return target, result

    def component(self, dirname, files, licenses):
        root = self.root / dirname
        root.mkdir()
        rows = []
        for name, data in files.items():
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            rows.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        manifest = self.root / (dirname + "-manifest.json")
        manifest.write_text(json.dumps({"identity": dirname, "revision": "c" * 40,
                                       "files": rows, "license_files": licenses}), encoding="utf-8")
        return root, manifest

    def corrupt_archive(self, original, target, changed=None, extra=None):
        with zipfile.ZipFile(original) as src, zipfile.ZipFile(target, "w") as dst:
            for info in src.infolist():
                content = src.read(info.filename)
                if changed and info.filename == changed:
                    content = b"X" * len(content)
                dst.writestr(info, content)
            if extra:
                dst.writestr(extra[0], extra[1])

    def test_build_is_reproducible_and_excludes_state_secrets_untracked(self):
        for path in ("state/private.txt", "runtime/session.json", ".env", "docs/private.key", "data/environment_status.json"):
            self.add(path, b"DO NOT DISTRIBUTE")
        (self.source / "untracked.txt").write_text("private")
        first, a = self.build("one.zip")
        second, b = self.build("two.zip")
        self.assertEqual(a["sha256"], b["sha256"])
        self.assertEqual(first.read_bytes(), second.read_bytes())
        manifest = bundle.verify(first)
        names = {row["path"] for row in manifest["files"]}
        self.assertIn("app/workbench/__main__.py", names)
        self.assertIn("Launch.py", names)
        self.assertFalse(any("private" in n or "session" in n or "environment_status" in n for n in names))

    def test_install_checks_hashes_and_never_executes(self):
        archive, receipt = self.build()
        target = self.root / "installed"
        with patch.object(bundle.subprocess, "run", side_effect=AssertionError("No install execution")):
            result = bundle.install(archive, target, expected_sha256=receipt["sha256"])
        self.assertFalse(result["executed"])
        self.assertEqual((target / "app/workbench/__main__.py").read_bytes(),
                         (self.source / "workbench/__main__.py").read_bytes())
        self.assertFalse((target / "state").exists())
        self.assertTrue((target / "BUNDLE_MANIFEST.json").is_file())

    def test_install_refuses_existing_destination_even_empty(self):
        archive, receipt = self.build()
        target = self.root / "existing"
        target.mkdir()
        with self.assertRaises(bundle.BundleError):
            bundle.install(archive, target, expected_sha256=receipt["sha256"])
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_archive_pin_is_required_and_cannot_be_guessed(self):
        archive, receipt = self.build()
        for pin in (None, "", "f" * 64, "not a hash"):
            with self.subTest(pin=pin), self.assertRaises(bundle.BundleError):
                bundle.install(archive, self.root / "new", expected_sha256=pin)
        self.assertFalse((self.root / "new").exists())

    def test_changed_file_rejected_even_when_zip_digest_matches(self):
        archive, _ = self.build()
        altered = self.root / "altered.zip"
        self.corrupt_archive(archive, altered, changed="app/README.md")
        digest = bundle._digest(altered)[1]
        with self.assertRaises(bundle.BundleError):
            bundle.install(altered, self.root / "new", expected_sha256=digest)
        self.assertFalse((self.root / "new").exists())
        self.assertEqual(list(self.root.glob(".neuromorph-stage-*")), [])

    def test_unlisted_file_and_path_traversal_are_rejected(self):
        archive, _ = self.build()
        for name in ("unexpected.txt", "../outside.txt", "/absolute.txt", "C:/outside.txt", "a\\b", "AUX.txt", "a/../x"):
            altered = self.root / "bad.zip"
            self.corrupt_archive(archive, altered, extra=(name, b"bad"))
            with self.subTest(name=name), self.assertRaises(bundle.BundleError):
                bundle.verify(altered)
        self.assertFalse((self.root.parent / "outside.txt").exists())

    def test_symlink_zip_entry_rejected(self):
        archive, _ = self.build()
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        altered = self.root / "link.zip"
        self.corrupt_archive(archive, altered, extra=(link, b"../../outside"))
        with self.assertRaises(bundle.BundleError):
            bundle.verify(altered)

    def test_symlink_source_rejected(self):
        source = self.source / "docs/link.txt"
        source.parent.mkdir()
        try:
            source.symlink_to(self.source / "README.md")
        except OSError:
            self.skipTest("Symlink privilege unavailable")
        self.tracked.append("docs/link.txt")
        with self.assertRaises(bundle.BundleError):
            self.build()

    def test_dirty_source_and_existing_output_are_rejected(self):
        with patch.object(bundle, "_git", side_effect=lambda root, *args: b"a" * 40 if args[0] == "rev-parse" else b" M README.md"):
            with self.assertRaises(bundle.BundleError):
                self.build()
        archive, _ = self.build()
        before = archive.read_bytes()
        with self.assertRaises(bundle.BundleError):
            self.build()
        self.assertEqual(archive.read_bytes(), before)

    def test_model_component_is_pinned_and_placed_inside_app_runtime(self):
        model, manifest = self.component("model", {"runtime/local-model/model.gguf": b"weights",
            "runtime/local-model/LICENSE": b"Model terms", "runtime/local-model/manifest.json": b"{}"},
            ["runtime/local-model/LICENSE"])
        (model / "private.txt").write_text("must be excluded")
        archive, _ = self.build(model_root=model, model_manifest=manifest)
        verified = bundle.verify(archive)
        paths = {r["path"] for r in verified["files"]}
        self.assertIn("app/runtime/local-model/model.gguf", paths)
        self.assertNotIn("app/private.txt", paths)
        self.assertEqual(verified["model_portability"], "not_verified")

    def test_model_tampering_or_missing_license_fails_without_partial_archive(self):
        model, manifest = self.component("model", {"runtime/local-model/model.gguf": b"weights",
            "runtime/local-model/LICENSE": b"Model terms"}, ["runtime/local-model/LICENSE"])
        (model / "runtime/local-model/model.gguf").write_bytes(b"changed")
        with self.assertRaises(bundle.BundleError):
            self.build(model_root=model, model_manifest=manifest)
        self.assertFalse((self.root / "bundle.zip").exists())
        value = json.loads(manifest.read_text())
        value["license_files"] = []
        manifest.write_text(json.dumps(value))
        with self.assertRaises(bundle.BundleError):
            self.build(model_root=model, model_manifest=manifest)

    def test_embedded_python_path_is_rewritten_without_enabling_site(self):
        runtime, manifest = self.component("python", {"python.exe": b"inert executable fixture",
            "python313.zip": b"stdlib fixture", "python313._pth": b"python313.zip\n.\n#import site\n",
            "LICENSE.txt": b"Python license"}, ["LICENSE.txt"])
        archive, _ = self.build(python_root=runtime, python_manifest=manifest)
        bundle.verify(archive)
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(zipped.read("python/python313._pth"), b"python313.zip\n.\n../app\n")
            self.assertIn(b" -I ", zipped.read("Start.cmd"))
            self.assertIn(b"python3 -I", zipped.read("start.sh"))

    def test_size_limits_fail_before_install(self):
        with self.assertRaises(bundle.BundleError):
            self.build(max_bytes=1)
        archive, receipt = self.build()
        with self.assertRaises(bundle.BundleError):
            bundle.install(archive, self.root / "small", expected_sha256=receipt["sha256"], max_bytes=1)
        self.assertFalse((self.root / "small").exists())

    def test_case_collisions_and_duplicate_manifest_keys_rejected(self):
        self.add("readme.md", b"ignored root spelling")
        self.add("docs/Entry.md", b"one")
        self.add("docs/entry.md", b"two")
        with self.assertRaises(bundle.BundleError):
            self.build()
        with self.assertRaises(bundle.BundleError):
            bundle._json(b'{"files":[],"files":[]}')

    def test_publish_collision_never_overwrites_a_file(self):
        archive, receipt = self.build()
        destination = self.root / "installed"
        original = os.link
        seen = []
        def conflict(source, target):
            if not seen:
                seen.append(Path(target))
                Path(target).write_bytes(b"external file created concurrently")
            return original(source, target)
        with patch.object(bundle.os, "link", side_effect=conflict):
            with self.assertRaises(FileExistsError):
                bundle.install(archive, destination, expected_sha256=receipt["sha256"])
        self.assertEqual(seen[0].read_bytes(), b"external file created concurrently")

    def test_copy_uses_zip64_streams(self):
        original = zipfile.ZipFile.open
        writes = []
        def opened(archive, name, mode="r", *args, **kwargs):
            if mode == "w":
                writes.append(kwargs.get("force_zip64"))
            return original(archive, name, mode, *args, **kwargs)
        with patch.object(zipfile.ZipFile, "open", opened):
            self.build()
        self.assertIn(True, writes)

    def test_filesystem_without_hardlinks_uses_exclusive_stream_copy(self):
        archive, receipt = self.build()
        destination = self.root / "portable"
        with patch.object(bundle.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "No hard links")):
            result = bundle.install(archive, destination, expected_sha256=receipt["sha256"])
        self.assertFalse(result["executed"])
        self.assertEqual((destination / "app/README.md").read_bytes(), (self.source / "README.md").read_bytes())


if __name__ == "__main__":
    unittest.main()
