"""Approval audit ledger for ``state.db`` (``approval_events``).

Port of DeepSeek Harness' approval audit pair (``packages/interaction/user-approval``):
every human approval ask is durably paired with its decision under one fresh
``request_id``, carrying the turn/tool-call/session correlation ids that
``tools.approval_context`` already binds for approval hooks. The events are log-only —
they never enter the model transcript.

Writes are best-effort by design: the approval flow is safety-critical, audit
observability is not (same stance as ``tools.approval_context._fire_approval_hook``).
A failed append logs at debug level and returns False; it never blocks or alters a
decision. (Deliberate divergence from DSH, which rejects a request closed when either
audit append cannot commit.)

The connection mirrors ``tools.async_delegation``: a short-lived raw connection to the
profile's ``state.db`` with the canonical SCHEMA_SQL replayed via
``reconcile_state_schema``, so this writer can never grow a second hand-maintained
shape for the table.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("hermes_state")

_EVENT_KINDS = ("asked", "decided")


def _db_path():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state.db"


def _connect():
    from hermes_cli.sqlite_util import open_db
    from hermes_constants import mkdir_under_hermes_home
    from hermes_state import _secure_state_db_files

    path = _db_path()
    # A late replay or writer must not resurrect a removed named profile (#123265).
    mkdir_under_hermes_home(path.parent)
    _secure_state_db_files(path, create_main=True)

    def _initialize(conn):
        from hermes_state_repair import apply_durability_barriers
        from hermes_state_schema import reconcile_state_schema

        # Preserve the journal mode SessionDB configured on state.db (see
        # tools/async_delegation): forcing WAL from short-lived connections collides
        # with live transcript/FTS writers.
        apply_durability_barriers(conn)
        reconcile_state_schema(conn)

    # wal=False: SessionDB owns state.db's journal mode.
    conn = open_db(path, db_label="state.db (approval audit)", busy_timeout_ms=10_000,
                   wal=False, initialize=_initialize)
    _secure_state_db_files(path)
    return conn


def record_approval_event(
    *,
    session_id: str,
    request_id: str,
    event: str,
    outcome: Optional[str] = None,
    choice: Optional[str] = None,
    surface: str = "",
    pattern_key: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
    detail: str = "",
) -> bool:
    """Append one audit row (``event`` = 'asked' | 'decided'). Best-effort: returns False
    on any failure (logged at debug), never raises, never blocks."""
    if event not in _EVENT_KINDS:
        return False
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO approval_events (session_id, request_id, event, outcome, choice,"
                " surface, pattern_key, turn_id, tool_call_id, detail, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id or "", request_id, event, outcome, choice, surface or "",
                 pattern_key or "", turn_id or "", tool_call_id or "", detail or "",
                 time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        logger.debug("approval audit append failed (event=%s request_id=%s)",
                     event, request_id, exc_info=True)
        return False


def list_approval_events(session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Audit rows for one session, oldest first (the ``limit`` most recent when capped).
    Best-effort: an unreadable store returns []."""
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id, session_id, request_id, event, outcome, choice, surface,"
                " pattern_key, turn_id, tool_call_id, detail, timestamp"
                " FROM approval_events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id or "", int(limit)),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.debug("approval audit read failed (session_id=%s)", session_id, exc_info=True)
        return []
    return [dict(row) for row in reversed(rows)]
