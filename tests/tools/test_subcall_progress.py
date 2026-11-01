"""Transient sub-call progress for execute_code (sandbox RPC).

DSH parity: every code-driven tool dispatch is a visible event. Hermes keeps the
sandbox call log internal (``code_execution_rpc.tool_call_log``); this seam lets each
finished sub-call reach the surfaces as transient chrome via ``tool.subcall``.

Covers the module contract (no emitter = silent no-op; failures never propagate), the
RPC chokepoint emission, the executor-installed emitter's event shape, and that the
emitter rides ContextVars into a spawned RPC thread (the per-call sandbox path).
"""

from __future__ import annotations

import threading

from agent.tool_executor import _ToolCallRef
from tools import subcall_progress
from tools.code_execution_rpc import _handle_rpc_request
from tools.subcall_progress import make_agent_emitter
from tools.thread_context import propagate_context_to_thread


def _dispatch_ok(tool_name, tool_args):
    return '{"ok": true}'


def _rpc(request, *, dispatch=_dispatch_ok, log=None, counter=None):
    return _handle_rpc_request(
        request,
        allowed_tools=frozenset({"web_search", "terminal"}),
        tool_call_counter=counter if counter is not None else [0],
        max_tool_calls=10,
        dispatch=dispatch,
        tool_call_log=log if log is not None else [],
        call_start=0.0,
        where="test",
    )


class TestModuleContract:
    def test_no_emitter_is_a_silent_noop(self):
        subcall_progress.emit_subcall("web_search", "q=x", 0.4, None)

    def test_emitter_receives_coerced_arguments(self):
        seen = []
        token = subcall_progress.set_subcall_emitter(lambda *a: seen.append(a))
        try:
            subcall_progress.emit_subcall("web_search", {"q": "x"}, 0.42, "boom")
        finally:
            subcall_progress.reset_subcall_emitter(token)
        assert seen == [("web_search", "{'q': 'x'}", 0.42, "boom")]

    def test_failing_emitter_never_raises(self):
        def _boom(*args):
            raise ZeroDivisionError("boom")
        token = subcall_progress.set_subcall_emitter(_boom)
        try:
            subcall_progress.emit_subcall("web_search", "q", 0.1, None)
        finally:
            subcall_progress.reset_subcall_emitter(token)

    def test_reset_restores_previous_emitter(self):
        outer, inner = [], []
        outer_token = subcall_progress.set_subcall_emitter(lambda *a: outer.append(a))
        inner_token = subcall_progress.set_subcall_emitter(lambda *a: inner.append(a))
        subcall_progress.reset_subcall_emitter(inner_token)
        try:
            subcall_progress.emit_subcall("t", "p", 0.1, None)
        finally:
            subcall_progress.reset_subcall_emitter(outer_token)
        assert len(outer) == 1 and inner == []

    def test_emitter_rides_contextvars_into_spawned_threads(self):
        seen = []
        token = subcall_progress.set_subcall_emitter(lambda *a: seen.append(a))
        try:
            worker = propagate_context_to_thread(
                lambda: subcall_progress.emit_subcall("web_search", "q=x", 0.2, None))
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
        finally:
            subcall_progress.reset_subcall_emitter(token)
        assert seen == [("web_search", "q=x", 0.2, None)]


class TestRpcChokepoint:
    def test_dispatched_call_emits_progress(self):
        seen = []
        token = subcall_progress.set_subcall_emitter(lambda *a: seen.append(a))
        try:
            result = _rpc({"tool": "web_search", "args": {"query": "x"}})
        finally:
            subcall_progress.reset_subcall_emitter(token)
        assert result == '{"ok": true}'
        assert len(seen) == 1
        name, preview, duration, error = seen[0]
        assert name == "web_search" and "query" in preview and error is None and duration >= 0

    def test_failed_dispatch_emits_error(self):
        seen = []
        token = subcall_progress.set_subcall_emitter(lambda *a: seen.append(a))
        try:
            def _boom(tool_name, tool_args):
                raise RuntimeError("nope")
            _rpc({"tool": "web_search", "args": {}}, dispatch=_boom)
        finally:
            subcall_progress.reset_subcall_emitter(token)
        assert len(seen) == 1 and "nope" in (seen[0][3] or "")

    def test_refused_call_emits_nothing(self):
        # Budget/allow-list refusals never dispatch: no call, no progress event.
        seen = []
        token = subcall_progress.set_subcall_emitter(lambda *a: seen.append(a))
        try:
            result = _rpc({"tool": "not_allowed", "args": {}})
        finally:
            subcall_progress.reset_subcall_emitter(token)
        assert "not available" in result and seen == []

    def test_log_entry_still_recorded(self):
        log = []
        _rpc({"tool": "web_search", "args": {"query": "x"}}, log=log)
        assert [entry["tool"] for entry in log] == ["web_search"]


class TestKernelPath:
    """A session kernel's serving thread is frozen at kernel creation; each request must
    re-install the CURRENT cell's emitter (tools/code_kernel._rpc_forever._dispatch)."""

    def test_cell_authority_captures_the_cells_emitter(self):
        from tools.code_kernel import CellAuthority
        def sentinel(*args):
            return None
        token = subcall_progress.set_subcall_emitter(sentinel)
        try:
            authority = CellAuthority(task_id="t1")
        finally:
            subcall_progress.reset_subcall_emitter(token)
        assert authority.subcall_emitter is sentinel

    def test_cell_authority_without_a_surface_is_none(self):
        from tools.code_kernel import CellAuthority
        authority = CellAuthority(task_id="t1")
        assert authority.subcall_emitter is None

    def test_refreshed_emitter_routes_emission_to_the_current_cell(self):
        from tools.code_kernel import CellAuthority
        seen, stale = [], []
        token = subcall_progress.set_subcall_emitter(lambda *a: seen.append(a))
        try:
            authority = CellAuthority(task_id="t1")
        finally:
            subcall_progress.reset_subcall_emitter(token)
        # This thread now carries a stale emitter (like the serving thread's frozen context)…
        subcall_progress.set_subcall_emitter(lambda *a: stale.append(a))
        # …and the per-request refresh (what _dispatch does) re-installs the cell's own.
        subcall_progress.set_subcall_emitter(authority.subcall_emitter)
        _rpc({"tool": "web_search", "args": {"query": "k"}})
        assert len(seen) == 1 and stale == []


class TestExecutorEmitter:
    def _agent(self, recorder):
        class _Agent:
            tool_progress_callback = staticmethod(recorder)
        return _Agent()

    def test_event_shape(self):
        seen = []
        ref = _ToolCallRef(name="execute_code", args={}, task_id="t1", call_id="c1", trace=[])
        emitter = make_agent_emitter(self._agent(lambda *a, **kw: seen.append((a, kw))), ref.name)
        emitter("web_search", "query=x", 0.42, None)
        assert seen == [(("tool.subcall", "web_search", "query=x", None),
                         {"parent": "execute_code", "duration": 0.42, "error": None})]

    def test_failing_surface_callback_is_contained(self):
        # Containment is emit_subcall's contract (it is the only caller).
        emitter = make_agent_emitter(self._agent(lambda *a, **kw: 1 / 0), "execute_code")
        token = subcall_progress.set_subcall_emitter(emitter)
        try:
            subcall_progress.emit_subcall("web_search", "q", 0.1, None)  # must not raise
        finally:
            subcall_progress.reset_subcall_emitter(token)

    def test_unset_callback_is_a_noop(self):
        class _Agent:
            tool_progress_callback = None
        ref = _ToolCallRef(name="execute_code", args={}, task_id="t1", call_id="c1", trace=[])
        emitter = make_agent_emitter(_Agent(), ref.name)
        emitter("web_search", "q", 0.1, None)  # must not raise
