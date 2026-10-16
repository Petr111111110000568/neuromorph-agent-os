"""Offline tests for the read-only diagnostic report; not live-PC acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("openkylin_health", ROOT / "scripts" / "openkylin_health.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class HealthReportTests(unittest.TestCase):
    def test_report_is_bounded_and_has_no_identity_or_command_lines(self):
        report = module.make_report()
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["scope"], "read_only_local_host_diagnostics")
        self.assertNotIn("hostname", json.dumps(report).lower())
        self.assertNotIn("token", json.dumps(report).lower())
        self.assertNotIn("commandline", json.dumps(report).lower())
        self.assertLess(len(json.dumps(report).encode("utf-8")), 16000)

    def test_atomic_private_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "sub" / "probe.json"
            module.save_report(target, {"schema_version": 1, "ok": True})
            self.assertEqual(json.loads(target.read_text()), {"schema_version": 1, "ok": True})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_service_probe_is_fixed_and_non_shell(self):
        with mock.patch.object(module.shutil, "which", return_value="/usr/bin/systemctl"):
            with mock.patch.object(module, "_command", return_value="active\n") as run:
                module.make_report()
                expected = [c.args[0] for c in run.call_args_list if c.args[0][0] == "systemctl"]
                self.assertEqual(expected, [["systemctl", "is-active", x] for x in module.SERVICES])

    def test_command_timeout_returns_no_output(self):
        with mock.patch.object(module.subprocess, "run", side_effect=module.subprocess.TimeoutExpired("x", 1)):
            self.assertIsNone(module._command(["systemctl", "is-active", "ssh"]))


if __name__ == "__main__":
    unittest.main()
