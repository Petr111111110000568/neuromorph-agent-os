"""Regression tests for the global directory traversal budget (real filesystem)."""
import importlib.util
import inspect
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("blackwall_directory_test", ROOT / "blackwall.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class DirectoryBudgetTest(unittest.TestCase):
    def scan(self, roots, **kwargs):
        self.assertIn("max_dirs", inspect.signature(b.scan_models).parameters)
        return b.scan_models(roots, **kwargs)

    def test_default_budget_stops_real_wide_empty_tree(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i in range(1200):
                (root / f"d{i:04}").mkdir()
            with patch.object(b.os, "scandir", wraps=os.scandir) as opened:
                result = b.scan_models([root], max_files=1)
            self.assertLessEqual(opened.call_count, 1000)
            self.assertEqual(opened.call_count, 1000)
            self.assertEqual(result["visited_dirs"], 1000)
            self.assertEqual(result["examined"], 0)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["items"], [])

    def test_explicit_budget_has_no_extra_directory_read(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i in range(30):
                (root / f"d{i:02}" / "nested").mkdir(parents=True)
            with patch.object(b.os, "scandir", wraps=os.scandir) as opened:
                result = self.scan([root], max_dirs=7)
            self.assertEqual(opened.call_count, 7)
            self.assertEqual(result["visited_dirs"], 7)
            self.assertTrue(result["truncated"])

    def test_budget_is_global_across_roots(self):
        with tempfile.TemporaryDirectory() as d:
            roots = [Path(d) / f"root{i}" for i in range(4)]
            for root in roots:
                root.mkdir()
            with patch.object(b.os, "scandir", wraps=os.scandir) as opened:
                result = self.scan(roots, max_dirs=2)
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(result["visited_dirs"], 2)
            self.assertTrue(result["truncated"])

    def test_zero_budget_does_not_open_a_directory(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(b.os, "scandir", side_effect=AssertionError("directory opened")):
                result = self.scan([Path(d)], max_dirs=0)
            self.assertEqual(result["visited_dirs"], 0)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["items"], [])

    def test_empty_roots_are_not_truncated(self):
        result = self.scan([], max_dirs=0)
        self.assertEqual(result["visited_dirs"], 0)
        self.assertFalse(result["truncated"])

    def test_invalid_directory_budgets_are_rejected(self):
        self.assertIn("max_dirs", inspect.signature(b.scan_models).parameters)
        for value in (-1, True, False, 1.5, None, "2"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    b.scan_models([], max_dirs=value)

    def test_complete_small_tree_preserves_model_detection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "child").mkdir()
            (root / "one.gguf").write_bytes(b"fixture")
            (root / "child" / "two.onnx").write_bytes(b"fixture")
            result = self.scan([root], max_dirs=3)
            self.assertEqual(result["visited_dirs"], 2)
            self.assertEqual(len(result["items"]), 2)
            self.assertFalse(result["truncated"])
            self.assertFalse(result["models_loaded"])

    def test_exact_budget_is_conservatively_truncated(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(b.os, "scandir", wraps=os.scandir) as opened:
                result = self.scan([Path(d)], max_dirs=1)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(result["visited_dirs"], 1)
            self.assertTrue(result["truncated"])

    def test_duplicate_roots_do_not_spend_budget_twice(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = self.scan([root, root], max_dirs=2)
            self.assertEqual(result["visited_dirs"], 1)
            self.assertFalse(result["truncated"])

    def test_depth_pruning_still_applies(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "child").mkdir()
            (root / "child" / "hidden.gguf").write_bytes(b"fixture")
            result = self.scan([root], max_dirs=10, max_depth=0)
            self.assertEqual(result["visited_dirs"], 1)
            self.assertEqual(result["items"], [])
            self.assertFalse(result["truncated"])


if __name__ == "__main__":
    unittest.main()
