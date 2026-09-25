"""Scheduler tests use an injected clock and cycle, without network or waiting."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbench.autonomy import daemon
from test_autonomy import config


class FakeClock:
    def __init__(self):
        self.wall = 1790294400.0
        self.steady = 10.0
        self.sleeps = []
        self.on_sleep = None

    def time(self):
        return self.wall

    def monotonic(self):
        return self.steady

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.wall += seconds
        self.steady += seconds
        if self.on_sleep:
            self.on_sleep()


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.state = self.root / "schedule.json"
        self.history = self.root / "history.sqlite"
        self.stop = self.root / "stop"
        self.clock = FakeClock()
        self.runner = Mock(return_value={"cycle_id": "a" * 64, "status": "disabled"})
        self.writer = Mock(return_value={"status": "recorded"})
        self.settings = config()

    def run_job(self, **options):
        arguments = dict(stop_file=self.stop, interval=900, max_cycles=3, root=self.root,
                         clock=self.clock.time, monotonic=self.clock.monotonic,
                         sleep=self.clock.sleep, cycle_runner=self.runner,
                         history_writer=self.writer)
        arguments.update(options)
        return daemon.run_schedule(self.settings, self.output, self.history, self.state, **arguments)

    def read_state(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_once_resumes_only_when_due_and_never_calls_model(self):
        first = self.run_job(once=True)
        self.assertEqual(first["completed_cycles"], 1)
        self.assertEqual(first["next_due"], self.clock.wall + 900)
        self.assertEqual(self.run_job(once=True)["status"], "not_due")
        self.assertEqual(self.runner.call_count, 1)
        self.clock.wall += 900
        self.clock.steady += 900
        self.assertEqual(self.run_job(once=True)["completed_cycles"], 2)
        self.assertEqual(self.writer.call_count, 2)
        self.assertEqual(self.clock.sleeps, [])
        for invocation in self.runner.call_args_list:
            self.assertEqual(invocation.kwargs["provider"], "none")
            self.assertEqual(invocation.kwargs["environment"], {})
            self.assertEqual(invocation.kwargs["root"], self.root)
            self.assertIs(invocation.kwargs["online"], True)

    def test_finite_schedule_obeys_count_and_five_second_sleep_chunks(self):
        result = self.run_job()
        self.assertEqual(result["status"], "limit_reached")
        self.assertEqual(result["completed_cycles"], 3)
        self.assertEqual(self.runner.call_count, 3)
        self.assertEqual(sum(self.clock.sleeps), 1800)
        self.assertTrue(all(0 < duration <= 5 for duration in self.clock.sleeps))
        self.assertEqual(self.run_job()["status"], "limit_reached")
        self.assertEqual(self.runner.call_count, 3)

    def test_stop_file_before_first_cycle_or_during_sleep(self):
        self.stop.touch()
        self.assertEqual(self.run_job()["status"], "stopped")
        self.runner.assert_not_called()
        self.stop.unlink()
        self.clock.on_sleep = self.stop.touch
        result = self.run_job()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["completed_cycles"], 1)
        self.assertEqual(self.clock.sleeps, [5])
        self.assertEqual(self.runner.call_count, 1)

    def test_clock_jump_forward_does_not_accelerate_in_process_pacing(self):
        def jump():
            self.clock.wall += 10000
        self.clock.on_sleep = jump
        result = self.run_job(max_cycles=2)
        self.assertEqual(result["completed_cycles"], 2)
        self.assertEqual(sum(self.clock.sleeps), 900)

    def test_offline_is_forwarded(self):
        self.run_job(once=True, offline=True)
        self.assertIs(self.runner.call_args.kwargs["online"], False)

    def test_changed_config_or_limits_reject_resume_without_reset(self):
        self.run_job(once=True)
        original = self.state.read_bytes()
        self.settings["discovery"]["query"] = "changed"
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            self.run_job(once=True)
        self.assertEqual(self.state.read_bytes(), original)
        self.settings = config()
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            self.run_job(once=True, max_cycles=4)
        self.assertEqual(self.runner.call_count, 1)

    def test_output_failure_preserves_due_count_and_stops_without_retry(self):
        self.runner.side_effect = OSError("private-path-must-not-be-stored")
        with self.assertRaises(OSError):
            self.run_job()
        state = self.read_state()
        self.assertEqual(state["completed_cycles"], 0)
        self.assertEqual(state["next_due"], state["created_at"])
        self.assertEqual(state["last_status"], "cycle_failed")
        self.assertNotIn("private-path", self.state.read_text())
        self.assertEqual(self.runner.call_count, 1)
        self.writer.assert_not_called()

    def test_history_failure_does_not_advance_checkpoint(self):
        self.writer.side_effect = ValueError("invalid history")
        with self.assertRaises(ValueError):
            self.run_job(once=True)
        self.assertEqual(self.read_state()["completed_cycles"], 0)
        self.writer.side_effect = None
        self.assertEqual(self.run_job(once=True)["completed_cycles"], 1)
        self.assertEqual(self.runner.call_count, 2)

    def test_checkpoint_failure_after_history_allows_documented_replay(self):
        actual = daemon._save_state

        def fail_completed(path, state):
            if state["completed_cycles"]:
                raise OSError("interrupted checkpoint")
            actual(path, state)

        with patch.object(daemon, "_save_state", side_effect=fail_completed):
            with self.assertRaises(OSError):
                self.run_job(once=True)
        self.assertEqual(self.read_state()["completed_cycles"], 0)
        self.assertEqual(self.writer.call_count, 1)
        self.assertEqual(self.run_job(once=True)["completed_cycles"], 1)
        self.assertEqual(self.writer.call_count, 2)

    def test_invalid_policy_is_rejected_before_state_or_cycle(self):
        directory = self.root / "config"
        directory.mkdir()
        (directory / "resource_policy.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_job()
        self.assertFalse(self.state.exists())
        self.runner.assert_not_called()

    def test_corrupt_or_forged_checkpoint_is_not_overwritten(self):
        self.run_job(once=True)
        value = self.read_state()
        value["next_due"] += 100000
        self.state.write_text(json.dumps(value), encoding="utf-8")
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, "deadline"):
            self.run_job()
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.runner.call_count, 1)

    def test_path_overlap_missing_parent_and_bounded_parameters(self):
        for options in ({"interval": 899}, {"interval": 86401}, {"max_cycles": 0},
                        {"max_cycles": 1001}, {"max_cycles": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_job(**options)
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.run_job(stop_file=self.state)
        self.history = self.root / "missing" / "history.sqlite"
        with self.assertRaisesRegex(ValueError, "parent"):
            self.run_job()
        self.runner.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_process_lock_blocks_other_process_and_is_released(self):
        path = self.root / "test.lock"
        code = ("from workbench.autonomy.daemon import _job_lock; import sys\n"
                "try:\n"
                "    with _job_lock(sys.argv[1]): pass\n"
                "except ValueError: sys.exit(9)\n")
        command = [sys.executable, "-B", "-c", code, str(path)]
        with daemon._job_lock(path):
            result = subprocess.run(command, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 9, result.stderr)
        result = subprocess.run(command, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), b"1")

    def test_stop_file_reparse_is_rejected_without_following(self):
        actual = daemon._safe_path
        self.run_job(once=True)
        with patch.object(daemon, "_safe_path", side_effect=lambda p, **kw:
                          (_ for _ in ()).throw(ValueError("reparse"))
                          if Path(p) == self.stop else actual(p, **kw)):
            with self.assertRaisesRegex(ValueError, "reparse"):
                self.run_job()
        self.assertEqual(self.runner.call_count, 1)

    def test_cli_uses_explicit_paths_and_has_no_model_provider_option(self):
        config_path = self.root / "input.json"
        config_path.write_text(json.dumps(config()), encoding="utf-8")
        with patch.object(daemon, "run_schedule", return_value={"status": "not_due"}) as schedule:
            with patch("builtins.print"):
                result = daemon.main(["--config", str(config_path), "--output-dir", str(self.output),
                                      "--history-db", str(self.history), "--state-file", str(self.state),
                                      "--once", "--offline"])
        self.assertEqual(result, 0)
        self.assertIs(schedule.call_args.kwargs["offline"], True)
        self.assertIs(schedule.call_args.kwargs["once"], True)


if __name__ == "__main__":
    unittest.main()

