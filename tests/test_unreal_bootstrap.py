import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

MODULE = Path(__file__).resolve().parents[1] / "scripts/bootstrap_unreal.py"
spec = importlib.util.spec_from_file_location("bootstrap_unreal", MODULE)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def test_default_is_plan_only(self):
        with patch.object(bootstrap, "build") as build, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, bootstrap.main([]))
        build.assert_not_called()

    def test_tar_extraction_rejects_traversal_links_and_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, kind in (("go/../../outside", tarfile.REGTYPE),
                               ("go/link", tarfile.SYMTYPE), ("go/device", tarfile.CHRTYPE)):
                with self.subTest(name=name):
                    archive = root / "archive.tar.gz"
                    with tarfile.open(archive, "w:gz") as tar:
                        item = tarfile.TarInfo(name)
                        item.type = kind
                        if kind == tarfile.SYMTYPE:
                            item.linkname = "/etc/passwd"
                        tar.addfile(item)
                    with self.assertRaises(ValueError):
                        bootstrap.extract_toolchain(archive, root / "output")
                    self.assertFalse((root / "outside").exists())

    def test_go_module_hash_matches_the_documented_sorted_content_algorithm(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "module.zip"
            entries = {"module@v1/b.txt": b"B", "module@v1/a.txt": b"A"}
            with zipfile.ZipFile(archive, "w") as bundle:
                for name, content in entries.items():
                    bundle.writestr(name, content)
            summary = "".join(hashlib.sha256(entries[name]).hexdigest() + "  " + name + "\n" for name in sorted(entries))
            expected = "h1:" + base64.b64encode(hashlib.sha256(summary.encode()).digest()).decode()
            self.assertEqual(expected, bootstrap.module_hash(archive))

    def test_project_output_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unsafe").symlink_to(root / "destination")
            with patch.object(bootstrap, "ROOT", root):
                with self.assertRaises(ValueError):
                    bootstrap.ensure_project_output(root / "unsafe" / "runner")

    def test_explicit_pin_changes_only_verified_unreal_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            registry = root / "config/harnesses.json"
            value = {"schema_version": 1, "harnesses": [
                {"id": "unreal", "adapter": "unreal_jsonl", "revision": bootstrap.SOURCE_COMMIT,
                 "binary": {"path": "runtime/harnesses/unreal-agent-runner", "sha256": None}},
                {"id": "untouched", "binary": None}]}
            registry.write_text(json.dumps(value))
            with patch.object(bootstrap, "ROOT", root), patch.object(bootstrap, "BINARY", root / "runtime/harnesses/unreal-agent-runner"):
                bootstrap.pin_registry("a" * 64)
            result = json.loads(registry.read_text())
            value["harnesses"][0]["binary"]["sha256"] = "a" * 64
            self.assertEqual(value, result)


if __name__ == "__main__":
    unittest.main()
