"""Tests for the TUI gateway relaying sandbox sub-call progress to the client.

While ``execute_code`` runs, each dispatched sandbox call (the RPC chokepoint) emits
``tool.subcall`` through the agent's tool_progress_callback. ``_on_tool_progress`` must
forward it to the Ink/desktop client as transient activity chrome — named, never a
durable transcript row.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def server():
    # Mocks are scoped to the initial import only (see
    # tests/tui_gateway/test_protocol.py for the rationale).
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test_subcall")
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        import importlib

        mod = importlib.import_module("tui_gateway.server")

    yield mod
    mod._sessions.clear()


@pytest.fixture()
def emits(server, monkeypatch):
    captured: list = []
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload=None: captured.append((event, sid, payload)),
    )
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda sid: True)
    return captured


def test_subcall_relayed_with_parent_and_duration(server, emits):
    server._on_tool_progress(
        "sid-1",
        "tool.subcall",
        "web_search",
        "query='hermes agent'",
        None,
        parent="execute_code",
        duration=0.42,
        error=None,
    )
    assert emits == [(
        "tool.subcall",
        "sid-1",
        {"name": "web_search", "preview": "query='hermes agent'", "parent": "execute_code",
         "duration_s": 0.42, "error": None},
    )]


def test_subcall_error_carried_through(server, emits):
    server._on_tool_progress(
        "sid-1", "tool.subcall", "terminal", "cmd", None,
        parent="execute_code", duration=1.0, error="exit 1",
    )
    assert emits[0][2]["error"] == "exit 1"


def test_subcall_follows_tool_progress_gate(server, emits, monkeypatch):
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda sid: False)
    server._on_tool_progress(
        "sid-1", "tool.subcall", "web_search", "q", None,
        parent="execute_code", duration=0.1, error=None,
    )
    assert emits == []


def test_nameless_subcall_dropped(server, emits):
    server._on_tool_progress(
        "sid-1", "tool.subcall", "", "", None, parent="execute_code", duration=0.1, error=None,
    )
    assert emits == []
