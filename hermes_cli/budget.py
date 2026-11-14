"""Daily token budget: a per-profile spend meter over the state DB.

``budget.daily_tokens`` (0 = off) turns the per-session token counters every run already
writes into a "how much have I used TODAY" line, rendered by ``/usage`` on every surface and
by ``hermes usage``. This is a METER, not an enforcement gate: the per-fire cron token cap
(``cron.max_tokens_per_run``) bounds runaway jobs, and a hard interactive block would make
Hermes unusable mid-day. Attribution follows a session's START timestamp — the counters on
that row are cumulative, so a session resumed across midnight counts on its start day.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional


def midnight_timestamp(now: Optional[_dt.datetime] = None) -> float:
    """Epoch seconds of local midnight for *now* (default: the current local time)."""
    now = now or _dt.datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def budget_settings(config: Optional[dict[str, Any]] = None) -> tuple[int, int]:
    """``(daily_tokens, warn_percent)`` from the ``budget`` config section.

    ``daily_tokens <= 0`` means no cap. Missing/invalid values fall back to 0 / 80; the warn
    percentage is clamped to 1..100 so a warn line is never unreachable.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config
            config = load_config() or {}
        except Exception:
            config = {}
    section = config.get("budget") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        section = {}

    def _as_int(key: str, default: int) -> int:
        try:
            return int(section.get(key, default))
        except (TypeError, ValueError):
            return default

    daily = max(0, _as_int("daily_tokens", 0))
    warn = min(100, max(1, _as_int("warn_percent", 80)))
    return daily, warn


def budget_snapshot(db: Any, *, now: Optional[_dt.datetime] = None,
                    config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Today's spend vs the configured cap, computed from *db* (a ``SessionDB``).

    ``percent`` is None when no cap is configured; ``warn`` is True at/above
    ``warn_percent`` while still under the cap; ``over`` is True at/above the cap.
    """
    daily_tokens, warn_percent = budget_settings(config)
    usage = db.usage_since(midnight_timestamp(now))
    used = int(usage.get("tokens", 0) or 0)
    cost = float(usage.get("cost_usd", 0.0) or 0.0)
    percent = (used / daily_tokens * 100.0) if daily_tokens > 0 else None
    return {
        "daily_tokens": daily_tokens,
        "warn_percent": warn_percent,
        "used_tokens": used,
        "cost_usd": cost,
        "percent": percent,
        "over": bool(percent is not None and used >= daily_tokens),
        "warn": bool(percent is not None and percent >= warn_percent and used < daily_tokens),
    }


def render_budget_line(snap: dict[str, Any], *, markdown: bool = False,
                       keys_prefix: str = "cli.usage") -> str:
    """One localized line for the snapshot (plain CLI text or gateway markdown)."""
    from agent.i18n import t

    used = f"{int(snap.get('used_tokens', 0)):,}"
    cap = int(snap.get("daily_tokens", 0) or 0)
    cap_part = f" / {cap:,} tokens ({float(snap.get('percent') or 0):.0f}%)" if cap > 0 else " tokens"
    cost = float(snap.get("cost_usd", 0.0) or 0.0)
    cost_part = f" · est ${cost:.2f}" if cost > 0 else ""
    key = "budget_today"
    if snap.get("over"):
        key = "budget_today_over"
    elif snap.get("warn"):
        key = "budget_today_warn"
    line = t(f"{keys_prefix}.{key}", used=used, cap_part=cap_part, cost_part=cost_part)
    return line if markdown else line.replace("**", "").replace("`", "")
