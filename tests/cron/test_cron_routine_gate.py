"""Cron routine gate (opt-in): skip — not queue — due occurrences when busy or too soon.

Mercury's routine gate (ADR-020 lineage), fitted to Hermes' at-most-once tick: the gate runs
AFTER ``advance_next_runs``, so a refused occurrence is consumed exactly like a fire — it can
neither re-fire next tick nor double-run — and the dropped job's execution receipt is settled
so the ledger never keeps a phantom claim. Both knobs are off by default.
"""

import time

import pytest


class TestSettings:
    def test_defaults_off(self, monkeypatch):
        from cron import scheduler

        monkeypatch.delenv("HERMES_CRON_SKIP_WHEN_BUSY", raising=False)
        monkeypatch.delenv("HERMES_CRON_MIN_INTERVAL", raising=False)
        monkeypatch.setattr(scheduler, "load_config", dict)
        assert scheduler._cron_routine_gate_settings() == (False, 0.0)

    def test_config_values(self, monkeypatch):
        from cron import scheduler

        monkeypatch.delenv("HERMES_CRON_SKIP_WHEN_BUSY", raising=False)
        monkeypatch.delenv("HERMES_CRON_MIN_INTERVAL", raising=False)
        monkeypatch.setattr(scheduler, "load_config",
                            lambda: {"cron": {"skip_when_busy": True, "min_interval_seconds": 90}})
        assert scheduler._cron_routine_gate_settings() == (True, 90.0)

    def test_env_overrides_config(self, monkeypatch):
        from cron import scheduler

        monkeypatch.setenv("HERMES_CRON_SKIP_WHEN_BUSY", "1")
        monkeypatch.setenv("HERMES_CRON_MIN_INTERVAL", "30")
        monkeypatch.setattr(scheduler, "load_config",
                            lambda: {"cron": {"skip_when_busy": False, "min_interval_seconds": 90}})
        assert scheduler._cron_routine_gate_settings() == (True, 30.0)

    def test_invalid_env_falls_through_to_config(self, monkeypatch):
        from cron import scheduler

        monkeypatch.delenv("HERMES_CRON_SKIP_WHEN_BUSY", raising=False)
        monkeypatch.setenv("HERMES_CRON_MIN_INTERVAL", "banana")
        monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"min_interval_seconds": 45}})
        assert scheduler._cron_routine_gate_settings() == (False, 45.0)


def _gate_env(monkeypatch, *, skip_busy=False, min_interval=0.0, in_flight=(), settled=None):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "_cron_routine_gate_settings", lambda: (skip_busy, min_interval))
    monkeypatch.setattr(scheduler, "hermes_home_key", lambda p: str(p))
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: "/h")
    monkeypatch.setattr(scheduler, "_running_job_ids", {("/h", jid) for jid in in_flight})
    monkeypatch.setattr(scheduler, "_running_fire_owners", {})
    if settled is not None:
        monkeypatch.setattr(
            scheduler, "settle_unstarted_execution",
            lambda execution_id, job_id, error: settled.append((execution_id, job_id, error)))
    return scheduler


def _job(job_id, *, last_run_at=None, schedule=None, execution_id=None):
    return {"id": job_id, "name": job_id, "last_run_at": last_run_at,
            "schedule": schedule or {"kind": "interval", "seconds": 300},
            "execution_id": execution_id or f"exec-{job_id}"}


class TestGate:
    def test_gate_off_returns_input_unchanged(self, monkeypatch):
        sched = _gate_env(monkeypatch)
        jobs = [_job("a"), _job("b")]
        assert sched._apply_routine_gate(jobs) == jobs

    def test_busy_profile_skips_and_settles_receipt(self, monkeypatch):
        settled: list = []
        sched = _gate_env(monkeypatch, skip_busy=True, in_flight=["other"], settled=settled)
        kept = sched._apply_routine_gate([_job("a"), _job("b")])
        assert kept == []
        assert [s[1] for s in settled] == ["a", "b"]
        assert all("profile busy" in s[2] for s in settled)

    def test_own_in_flight_run_does_not_block_itself(self, monkeypatch):
        """A job already running is deduped by the submit guard, not refused as 'busy' by itself."""
        sched = _gate_env(monkeypatch, skip_busy=True, in_flight=["a"])
        assert [j["id"] for j in sched._apply_routine_gate([_job("a")])] == ["a"]

    def test_first_job_accepted_then_rest_skipped_in_one_tick(self, monkeypatch):
        settled: list = []
        sched = _gate_env(monkeypatch, skip_busy=True, settled=settled)
        kept = sched._apply_routine_gate([_job("a"), _job("b"), _job("c")])
        assert [j["id"] for j in kept] == ["a"]
        assert [s[1] for s in settled] == ["b", "c"]

    def test_min_interval_skips_recent_recurring_run(self, monkeypatch):
        settled: list = []
        sched = _gate_env(monkeypatch, min_interval=60.0, settled=settled)
        kept = sched._apply_routine_gate([_job("r", last_run_at=time.time() - 10)])
        assert kept == []
        assert "min interval 60s" in settled[0][2]

    def test_min_interval_allows_old_enough_run(self, monkeypatch):
        sched = _gate_env(monkeypatch, min_interval=60.0)
        kept = sched._apply_routine_gate([_job("r", last_run_at=time.time() - 120)])
        assert [j["id"] for j in kept] == ["r"]

    def test_min_interval_never_gates_one_shots(self, monkeypatch):
        sched = _gate_env(monkeypatch, min_interval=3600.0)
        one_shot = _job("o", last_run_at=time.time() - 5, schedule={"kind": "once", "at": "2026-01-01T00:00:00Z"})
        assert [j["id"] for j in sched._apply_routine_gate([one_shot])] == ["o"]

    def test_recurring_without_last_run_is_not_gated(self, monkeypatch):
        sched = _gate_env(monkeypatch, min_interval=60.0)
        assert [j["id"] for j in sched._apply_routine_gate([_job("r", last_run_at=None)])] == ["r"]

    def test_missing_execution_id_is_tolerated(self, monkeypatch):
        """Older test doubles / restored rows may carry no execution id — settle is skipped."""
        settled: list = []
        sched = _gate_env(monkeypatch, min_interval=60.0, settled=settled)
        job = _job("r", last_run_at=time.time() - 10)
        job["execution_id"] = None
        assert sched._apply_routine_gate([job]) == []
        assert settled == []
