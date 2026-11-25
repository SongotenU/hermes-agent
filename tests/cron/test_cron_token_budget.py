"""Per-run token budget for cron fires (``cron.max_tokens_per_run`` / ``HERMES_CRON_MAX_TOKENS``).

A run that crosses the cap is hard-interrupted by the scheduler's own monitor thread — the same
ladder as the inactivity watchdog — and the delivery notice names the cap instead of blaming the
model service. 0/absent keeps today's behaviour (no cap, blocking fast path on the worker future).
"""

import threading
import time

import pytest


class TestTokenBudgetLoop:
    """The daemon-thread helper must not depend on the caller thread."""

    def test_fires_when_spend_crosses_limit(self):
        from cron.scheduler_liveness import token_budget_loop

        stop = threading.Event()
        spent = {"n": 0}
        results: list = []

        def _watch():
            results.append(token_budget_loop(
                tokens_used=lambda: spent["n"], limit_tokens=100, poll_s=0.02,
                stop=stop, future_done=lambda: False))

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
        time.sleep(0.05)
        spent["n"] = 150
        watcher.join(timeout=2.0)
        stop.set()
        assert results == [True]

    def test_stops_when_future_completes_first(self):
        from cron.scheduler_liveness import token_budget_loop

        fired = token_budget_loop(
            tokens_used=lambda: 10_000, limit_tokens=100, poll_s=0.02,
            stop=threading.Event(), future_done=lambda: True)
        assert fired is False

    def test_bad_tokens_reader_cannot_crash_the_watchdog(self):
        from cron.scheduler_liveness import token_budget_loop

        stop = threading.Event()
        results: list = []

        def _boom() -> int:
            raise RuntimeError("agent gone")

        watcher = threading.Thread(target=lambda: results.append(token_budget_loop(
            tokens_used=_boom, limit_tokens=100, poll_s=0.01,
            stop=stop, future_done=lambda: False)), daemon=True)
        watcher.start()
        time.sleep(0.05)
        stop.set()
        watcher.join(timeout=2.0)
        assert results == [False]


class TestBudgetResolution:
    def test_env_override_wins_over_config(self, monkeypatch):
        from cron import scheduler

        monkeypatch.setenv("HERMES_CRON_MAX_TOKENS", "250000")
        monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"max_tokens_per_run": 5}})
        assert scheduler._cron_token_budget() == 250000

    def test_zero_env_disables_even_with_config(self, monkeypatch):
        from cron import scheduler

        monkeypatch.setenv("HERMES_CRON_MAX_TOKENS", "0")
        monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"max_tokens_per_run": 500}})
        assert scheduler._cron_token_budget() is None

    def test_invalid_env_falls_through_to_config(self, monkeypatch):
        from cron import scheduler

        monkeypatch.setenv("HERMES_CRON_MAX_TOKENS", "banana")
        monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"max_tokens_per_run": 500}})
        assert scheduler._cron_token_budget() == 500

    def test_unset_everywhere_means_no_cap(self, monkeypatch):
        from cron import scheduler

        monkeypatch.delenv("HERMES_CRON_MAX_TOKENS", raising=False)
        monkeypatch.setattr(scheduler, "load_config", dict)
        assert scheduler._cron_token_budget() is None


class TestWatchdogWiring:
    def test_run_is_interrupted_and_raised_with_budget_text(self, monkeypatch):
        from cron import scheduler

        class Agent:
            def __init__(self):
                self.session_total_tokens = 0
                self.interrupted: list = []
                self._stop = threading.Event()

            def run_conversation(self, prompt, task_id=None):
                while not self._stop.is_set():
                    self.session_total_tokens += 400
                    time.sleep(0.01)
                return {"final_response": "stopped", "completed": False, "failed": True}

            def interrupt(self, message=None, **kwargs):
                self.interrupted.append(message)
                self._stop.set()

        monkeypatch.setattr(scheduler, "_CRON_POLL_INTERVAL", 0.05)
        monkeypatch.setattr(scheduler, "_cron_inactivity_seconds", lambda: 0.0)
        monkeypatch.setattr(scheduler, "_cron_token_budget", lambda: 300)

        agent = Agent()
        with pytest.raises(RuntimeError, match="cron.max_tokens_per_run"):
            scheduler._run_agent_with_watchdog(
                agent, "probe", {"schedule": {"kind": "every"}, "execution_id": ""},
                "probe-job", "probe", "probe-task", None)
        assert agent.interrupted

    def test_no_cap_keeps_the_blocking_fast_path(self, monkeypatch):
        from cron import scheduler

        class Agent:
            session_total_tokens = 0

            def run_conversation(self, prompt, task_id=None):
                self.session_total_tokens += 10_000
                return {"final_response": "ok"}

        monkeypatch.setattr(scheduler, "_cron_inactivity_seconds", lambda: 0.0)
        monkeypatch.setattr(scheduler, "_cron_token_budget", lambda: None)

        result = scheduler._run_agent_with_watchdog(
            Agent(), "probe", {"schedule": {"kind": "every"}, "execution_id": ""},
            "probe-job", "probe", "probe-task", None)
        assert result["final_response"] == "ok"


class TestFailureNotice:
    def test_summarizer_picks_token_budget_notice_with_numbers(self):
        from cron.scheduler import _summarize_cron_failure_for_delivery

        notice = _summarize_cron_failure_for_delivery(
            {"name": "nightly", "id": "job-1"},
            "Cron job 'nightly' used 350,000 tokens (limit 300,000 per run) — "
            "stopped by cron.max_tokens_per_run")
        assert "cron.max_tokens_per_run" in notice
        assert "350,000" in notice and "300,000" in notice
        assert "nightly" in notice
        assert "hermes cron run job-1" in notice

    def test_notice_without_numbers_still_reads(self):
        from cron.scheduler_failure_copy import token_budget_notice

        notice = token_budget_notice("nightly", "job-1")
        assert "per-run token budget" in notice
        assert "hermes cron run job-1" in notice
