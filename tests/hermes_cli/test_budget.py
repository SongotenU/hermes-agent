"""Daily token budget meter (`budget.daily_tokens`): state-DB aggregation, flags, rendering.

The meter reads the per-session token counters every run already writes; sessions are
attributed to their START day. It is a meter, not an enforcement gate — no run is ever blocked.
"""

import datetime as dt

import pytest


class TestMidnight:
    def test_midnight_of_fixed_local_time(self):
        from hermes_cli.budget import midnight_timestamp

        now = dt.datetime(2026, 10, 28, 15, 30, 12)
        assert midnight_timestamp(now) == dt.datetime(2026, 10, 28, 0, 0, 0).timestamp()

    def test_midnight_is_start_of_same_day(self):
        from hermes_cli.budget import midnight_timestamp

        now = dt.datetime.now()
        ts = midnight_timestamp(now)
        back = dt.datetime.fromtimestamp(ts)
        assert (back.year, back.month, back.day) == (now.year, now.month, now.day)
        assert (back.hour, back.minute, back.second) == (0, 0, 0)


class TestSettings:
    def test_defaults_when_absent(self):
        from hermes_cli.budget import budget_settings

        assert budget_settings({}) == (0, 80)

    def test_configured_values(self):
        from hermes_cli.budget import budget_settings

        assert budget_settings({"budget": {"daily_tokens": 2_000_000, "warn_percent": 90}}) == (2_000_000, 90)

    def test_warn_percent_is_clamped(self):
        from hermes_cli.budget import budget_settings

        assert budget_settings({"budget": {"warn_percent": 0}})[1] == 1
        assert budget_settings({"budget": {"warn_percent": 250}})[1] == 100

    def test_invalid_and_negative_values_fall_back(self):
        from hermes_cli.budget import budget_settings

        assert budget_settings({"budget": {"daily_tokens": "banana", "warn_percent": "oops"}}) == (0, 80)
        assert budget_settings({"budget": {"daily_tokens": -5}}) == (0, 80)
        assert budget_settings({"budget": "not-a-dict"}) == (0, 80)


class _StubDB:
    def __init__(self, tokens: int, cost: float = 0.0):
        self._tokens, self._cost = tokens, cost
        self.called_with: float | None = None

    def usage_since(self, since_ts: float):
        self.called_with = since_ts
        return {"tokens": self._tokens, "cost_usd": self._cost}


class TestSnapshot:
    def test_no_cap_is_a_meter_without_percent(self):
        from hermes_cli.budget import budget_snapshot

        snap = budget_snapshot(_StubDB(12_345, 0.5), config={})
        assert snap["used_tokens"] == 12_345
        assert snap["cost_usd"] == 0.5
        assert snap["percent"] is None
        assert snap["over"] is False and snap["warn"] is False

    def test_under_cap(self):
        from hermes_cli.budget import budget_snapshot

        snap = budget_snapshot(_StubDB(500), config={"budget": {"daily_tokens": 1000}})
        assert snap["percent"] == pytest.approx(50.0)
        assert snap["over"] is False and snap["warn"] is False

    def test_warn_band(self):
        from hermes_cli.budget import budget_snapshot

        snap = budget_snapshot(_StubDB(850), config={"budget": {"daily_tokens": 1000, "warn_percent": 80}})
        assert snap["warn"] is True and snap["over"] is False

    def test_over_cap(self):
        from hermes_cli.budget import budget_snapshot

        snap = budget_snapshot(_StubDB(1200), config={"budget": {"daily_tokens": 1000}})
        assert snap["over"] is True and snap["warn"] is False

    def test_midnight_timestamp_passed_to_db(self):
        from hermes_cli.budget import budget_snapshot, midnight_timestamp

        now = dt.datetime(2026, 10, 28, 9, 0, 0)
        db = _StubDB(1)
        budget_snapshot(db, now=now, config={})
        assert db.called_with == midnight_timestamp(now)


class TestUsageSince:
    def _db(self, tmp_path):
        from hermes_state import SessionDB

        return SessionDB(tmp_path / "state.db")

    def test_sums_only_sessions_started_since_bound(self, tmp_path):
        db = self._db(tmp_path)
        now = dt.datetime.now().timestamp()

        db.create_session("old", source="cli")
        db.create_session("fresh", source="cli")
        db.create_session("child", source="cli")
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, input_tokens = ?, output_tokens = ?, "
            "estimated_cost_usd = ? WHERE id = ?",
            (now - 3 * 86400, 900_000, 100_000, 9.0, "old"))
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, input_tokens = ?, output_tokens = ?, "
            "estimated_cost_usd = ? WHERE id = ?",
            (now - 60, 1_000, 500, 0.25, "fresh"))
        # A delegated child row: spend that happened must count, unlike the sidebar total.
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, input_tokens = ?, output_tokens = ?, "
            "estimated_cost_usd = ?, parent_session_id = ? WHERE id = ?",
            (now - 30, 200, 100, 0.05, "fresh", "child"))
        db._conn.commit()

        since = now - 3600
        usage = db.usage_since(since)
        assert usage["tokens"] == 1_000 + 500 + 200 + 100
        assert usage["cost_usd"] == pytest.approx(0.30)

    def test_empty_store_is_zero(self, tmp_path):
        db = self._db(tmp_path)
        assert db.usage_since(dt.datetime.now().timestamp() - 3600) == {"tokens": 0, "cost_usd": 0.0}


class TestRender:
    SNAP_CAPPED = {"daily_tokens": 2000, "warn_percent": 80, "used_tokens": 1200,
                   "cost_usd": 1.25, "percent": 60.0, "over": False, "warn": False}

    def test_capped_line_has_numbers_and_cost(self):
        from hermes_cli.budget import render_budget_line

        line = render_budget_line(self.SNAP_CAPPED, markdown=False)
        assert "1,200 / 2,000 tokens (60%)" in line
        assert "est $1.25" in line
        assert "**" not in line and "`" not in line

    def test_markdown_variant_keeps_formatting(self):
        from hermes_cli.budget import render_budget_line

        line = render_budget_line(self.SNAP_CAPPED, markdown=True, keys_prefix="gateway.usage")
        assert "**" in line

    def test_over_variant_names_the_config_key(self):
        from hermes_cli.budget import render_budget_line

        snap = dict(self.SNAP_CAPPED, used_tokens=2600, percent=130.0, over=True, warn=False)
        line = render_budget_line(snap, markdown=False)
        assert "over today's budget" in line and "budget.daily_tokens" in line

    def test_warn_variant(self):
        from hermes_cli.budget import render_budget_line

        snap = dict(self.SNAP_CAPPED, used_tokens=1700, percent=85.0, warn=True)
        line = render_budget_line(snap, markdown=False)
        assert "nearing today's budget" in line

    def test_no_cap_line_still_reads(self):
        from hermes_cli.budget import render_budget_line

        snap = {"daily_tokens": 0, "warn_percent": 80, "used_tokens": 12_345,
                "cost_usd": 0.0, "percent": None, "over": False, "warn": False}
        line = render_budget_line(snap, markdown=False)
        assert "12,345 tokens" in line
        assert "est $" not in line
