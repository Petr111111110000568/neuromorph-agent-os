import importlib.util
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("blackwall", ROOT / "blackwall.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

class InventoryTest(unittest.TestCase):
    def test_suffix_filters_are_conservative(self):
        self.assertTrue(b.looks_like_model(Path("model-0001.safetensors")))
        self.assertTrue(b.looks_like_model(Path("weights.gguf")))
        self.assertFalse(b.looks_like_model(Path("secrets.txt")))
        self.assertFalse(b.looks_like_model(Path("setup.sh")))
        self.assertFalse(b.looks_like_model(Path("other.bin")))

    def test_scanner_no_symlink_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "spaces"
            root.mkdir()
            (root / "one.gguf").write_bytes(b"test")
            (root / "two.safetensors").write_bytes(b"test2")
            (root / "unrelated.txt").write_text("x")
            secret = Path(d) / "secret"
            secret.mkdir()
            (secret / "must-not-see.gguf").write_bytes(b"hidden")
            (root / "linked").symlink_to(secret, target_is_directory=True)
            result = b.scan_models([root])
            self.assertEqual(len(result["items"]), 2)
            self.assertTrue(all("must-not-see" not in x["path"] for x in result["items"]))
            self.assertFalse(result["models_loaded"])

    def test_scan_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(20):
                (Path(d) / f"m{i}.gguf").write_bytes(b"a")
            result = b.scan_models([Path(d)], max_files=3, max_matches=99)
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["items"]), 3)

    def test_optional_project_budget_is_read_not_modified(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "config").mkdir()
            (root / "workbench").mkdir()
            (root / "workbench/__main__.py").write_text("\n")
            f = root / "config/resource_policy.json"
            f.write_text('{"daily_spend_limit_usd":0,"paid_model_calls_allowed":false}')
            project = b.find_project_root(str(root))
            self.assertTrue(b.read_project_policy(project)["safe_zero_spend"])
            self.assertEqual(f.read_text(), '{"daily_spend_limit_usd":0,"paid_model_calls_allowed":false}')

    def test_mcp_config_hardens_offline_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            (root / "workbench").mkdir()
            (root / "scripts/run_offline_mcp.py").write_text("\n")
            (root / "workbench/__main__.py").write_text("\n")
            args = b.offline_mcp_configuration(root)["mcpServers"]["neuromorph_offline"]["args"]
            self.assertIn("-I", args)
            self.assertIn("run_offline_mcp.py", args[1])
            self.assertEqual(args[2], "--data-dir")

    def test_private_json_written_mode_600(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "audit.json"
            b.write_private_json(target, {"safe": True})
            self.assertTrue(json.loads(target.read_text())["safe"])
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_no_inadvertent_network_on_dry_run(self):
        with patch.object(b.request, "build_opener", side_effect=AssertionError("network")):
            result = b.submit_local_plan("inspect status", execute=False)
        self.assertEqual(result["state"], "dry_run")

    def test_inventory_local_commands_statuses_are_not_executed(self):
        with patch.object(b, "probe_local_services", return_value={}), patch.object(b, "default_spots", return_value=[]):
            report = b.inventory([], project=None, include_default=True, network=True)
        self.assertFalse(report["access"]["agent_commands_executed"])
        self.assertFalse(report["access"]["peer_to_peer_enabled"])
        self.assertIn("clawteam", report["installed_commands"])

if __name__ == "__main__":
    unittest.main()
