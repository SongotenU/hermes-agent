"""Post-compaction active-state reminder (B15): live work survives the boundary.

Pins: nothing live → no row; running procs + live delegations compose a bounded
block; a second compaction strips the old reminder and refreshes (never
duplicates); registry failures fail OPEN (no reminder, no crash).
"""

from types import SimpleNamespace

import agent.post_compact_state as pcs


class TestCompose:

    def test_nothing_live_returns_none(self):
        assert pcs.compose_active_state_block([], False) is None

    def test_processes_and_delegations_compose(self):
        block = pcs.compose_active_state_block(
            [{"process_id": "proc_1", "command": "npm run dev"}, {"process_id": "proc_2", "command": "npm test"}],
            True,
        )
        assert block is not None
        assert block.startswith(pcs.ACTIVE_STATE_MARKER)
        assert "2 background process(es) still RUNNING" in block
        assert "proc_1: npm run dev" in block
        assert "Delegated subagent(s) are still LIVE" in block

    def test_process_list_capped_with_remainder(self):
        procs = [{"process_id": f"p{i}", "command": "x"} for i in range(8)]
        block = pcs.compose_active_state_block(procs, False)
        assert block is not None
        assert "8 background process(es)" in block
        assert "… and 3 more" in block


class TestFold:

    def test_fold_appends_synthetic_row(self, monkeypatch):
        monkeypatch.setattr(pcs, "collect_running_procs", lambda a: [{"process_id": "p", "command": "cmd"}])
        monkeypatch.setattr(pcs, "live_delegations", lambda a: False)
        messages = [{"role": "user", "content": "go"}]
        pcs.fold_active_state_reminder(SimpleNamespace(), messages)
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"].startswith(pcs.ACTIVE_STATE_MARKER)
        assert messages[-1].get("_active_state_synthetic") is True

    def test_second_fold_refreshes_not_duplicates(self, monkeypatch):
        monkeypatch.setattr(pcs, "collect_running_procs", lambda a: [{"process_id": "p", "command": "cmd"}])
        monkeypatch.setattr(pcs, "live_delegations", lambda a: False)
        messages = [{"role": "user", "content": "go"}]
        pcs.fold_active_state_reminder(SimpleNamespace(), messages)
        pcs.fold_active_state_reminder(SimpleNamespace(), messages)
        rows = [m for m in messages if str(m.get("content", "")).startswith(pcs.ACTIVE_STATE_MARKER)]
        assert len(rows) == 1

    def test_nothing_live_leaves_messages_untouched(self, monkeypatch):
        monkeypatch.setattr(pcs, "collect_running_procs", lambda a: [])
        monkeypatch.setattr(pcs, "live_delegations", lambda a: False)
        messages = [{"role": "user", "content": "go"}]
        pcs.fold_active_state_reminder(SimpleNamespace(), messages)
        assert messages == [{"role": "user", "content": "go"}]

    def test_registry_failures_fail_open(self):
        # Real lookups with a bare object: every failure path must return no block.
        assert pcs.build_active_state_block(SimpleNamespace()) is None
