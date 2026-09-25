"""Read-only cloud health receipt; ledger bytes are data, never imported code."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import time

STATE_PATH = "docs/contributions/continuous-state.json"
MAX_BYTES = 65536
OUTCOMES = {"response_received", "invalid_model_json", "invalid_python", "no_result",
            "no_public_metadata", "provider_unavailable", "transport_unavailable", "rate_limited",
            "deadline_exceeded", "response_too_large", "incomplete_response", "invalid_response",
            "dependency_unavailable", "failed", "request_failed", "access_denied",
            "contract_changed", "invalid_request", "interrupted"}


def assess(raw, now):
    result = {"status": "degraded", "reasons": [], "model_call_performed": False,
              "code_executed_from_ledger": False}
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
            raise ValueError("size")
        def unique(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError("duplicate")
                obj[key] = value
            return obj
        state = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(state, dict):
            raise ValueError("shape")
        if type(now) is not int or not 0 <= now < 10**12:
            raise ValueError("time")
        for key in ("attempts", "successes", "next_due", "failures", "phase"):
            if type(state.get(key)) is not int or not 0 <= state[key] < 10**12:
                raise ValueError("counter")
        if (state["successes"] > state["attempts"] or state["failures"] > state["attempts"]
                or not 0 <= state["phase"] < 6):
            raise ValueError("counter")
        if type(state.get("provider_blocked")) is not bool:
            raise ValueError("blocked")
        pending = state.get("pending")
        if pending is not None and not isinstance(pending, dict):
            raise ValueError("pending")
        journal = state.get("journal")
        if type(journal) is not list or len(journal) > 32:
            raise ValueError("journal")
        last_outcome = None
        if journal:
            latest = journal[-1]
            if (type(latest) is not dict or type(latest.get("status")) is not str
                    or latest["status"] not in OUTCOMES
                    or type(latest.get("attempt")) is not int
                    or not 1 <= latest["attempt"] <= state["attempts"]
                    or type(latest.get("at")) is not int or not 0 <= latest["at"] <= now):
                raise ValueError("journal")
            if pending is None and latest["attempt"] != state["attempts"]:
                raise ValueError("unfinalized_attempt")
            last_outcome = latest["status"]
        elif state["successes"] or (state["attempts"] and pending is None):
            raise ValueError("missing_journal")
        result.update({key: state[key] for key in ("attempts", "successes", "next_due",
                                                 "provider_blocked", "failures")})
        result["pending"] = pending is not None
        result["last_phase"] = state["phase"]
        result["last_outcome"] = last_outcome
        if state["failures"] or (last_outcome is not None and last_outcome != "response_received"):
            result["reasons"].append("last_attempt_failed_or_interrupted")
        if state["provider_blocked"]:
            result["reasons"].append("provider_blocked")
        if pending is not None:
            result["reasons"].append("unfinished_reservation")
        if not state["successes"]:
            result["reasons"].append("no_successful_model_response")
        # The trusted worker never schedules beyond its seven-day maximum cooldown.
        if state["next_due"] > now + 7 * 86400:
            result["reasons"].append("next_due_beyond_max_cooldown")
        # Allow schedule jitter and a full skipped cron slot; never repair/reset the ledger here.
        if now > state["next_due"] + 18 * 3600:
            result["reasons"].append("overdue_more_than_18h")
        if not result["reasons"]:
            result["status"] = "scheduled_or_waiting"
    except (ValueError, TypeError, UnicodeError, RecursionError):
        result["reasons"] = ["invalid_or_missing_ledger"]
    return result


def git_read(repo, argument, limit):
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"})
    base = ["git", "-C", str(repo), "--no-pager", "cat-file"]
    # Immutable Git objects let us reject oversized data before capturing its bytes.
    kind = subprocess.run(base + ["-t", argument], capture_output=True,
                          timeout=15, env=env, check=False)
    size = subprocess.run(base + ["-s", argument], capture_output=True,
                          timeout=15, env=env, check=False)
    if (kind.returncode or kind.stdout != b"blob\n" or size.returncode
            or not re.fullmatch(rb"[0-9]{1,12}\n", size.stdout)
            or int(size.stdout) > limit):
        raise ValueError("git_read_failed")
    command = base + ["-p", argument]
    completed = subprocess.run(command, capture_output=True, timeout=15, env=env, check=False)
    if completed.returncode or len(completed.stdout) != int(size.stdout):
        raise ValueError("git_read_failed")
    return completed.stdout


def commit_id(repo):
    env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1"}
    p = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
                       capture_output=True, timeout=15, env=env, check=False)
    value = p.stdout.decode("ascii", errors="replace").strip()
    if p.returncode or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("invalid_commit")
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trusted", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    now = int(time.time())
    result = {"checked_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
              "scope": "github_continuous_ledger_only", "additional_spend": 0,
              "other_providers_live_checked": False}
    try:
        result["main_commit"] = commit_id(args.trusted)
        result["ledger_commit"] = commit_id(args.ledger)
        result.update(assess(git_read(args.ledger, result["ledger_commit"] + ":" + STATE_PATH, MAX_BYTES), now))
    except (OSError, ValueError, subprocess.SubprocessError):
        result.update({"status": "degraded", "reasons": ["ledger_checkout_or_read_failed"],
                       "model_call_performed": False, "code_executed_from_ledger": False})
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    (output / "receipt.json").write_text(text, encoding="utf-8")
    (output / "report.md").write_text(
        "# Облачный контроль совета\n\n"
        "Отчёт проверяет сохранённый журнал, не вызывает модели и не исполняет предложения. "
        "Он работает в GitHub Actions независимо от ПК и квоты Codex.\n\n```json\n" + text +
        "```\n\n`scheduled_or_waiting` означает, что журнал не просрочен и не содержит "
        "незавершённого резерва, блокировки или последнего неудачного запуска. "
        "Проверяются поля состояния и последний исход, не корректность исследований или вся схема проекта. "
        "Это не гарантия следующего ответа модели. "
        "Kimi, SourceCraft, DeepSeek и HF этим запуском не проверялись.\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

