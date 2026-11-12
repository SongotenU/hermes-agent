"""`/seance` — read-only consult of a saved session (oh-my-pi ``seance`` parity).

Ports the design, not the code: a dedicated auxiliary model reads the ENTIRE
saved transcript and answers a question with citations, while the source is
opened strictly read-only (``sqlite3`` URI ``mode=ro``), never through
:class:`hermes_state.SessionDB` (which migrates/writes on open). No tools, no
session, no memory: one stateless completion, so there is no write surface at
all (Hermes divergence from omp's read/grep/glob tool loop — the transcript is
rendered up-front into the prompt instead).

Contracts pinned here:
* the source DB bytes are unchanged by a consult;
* a read-only (``chmod 444``) DB still consults successfully;
* unknown session ids fail closed with ``SeanceError``;
* the assignment prompt carries the untrusted-evidence + citation rules;
* oversized transcripts keep head AND tail with a truncation marker.
"""

import hashlib
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB


def _make_db(tmp_path: Path, session_id: str = "se-1", turns: int = 3) -> Path:
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session(session_id, "cli", model="test/model")
    for i in range(turns):
        db.append_message(session_id, "user", f"question {i} about widgets")
        db.append_message(session_id, "assistant", f"answer {i}: widget facts #{i}  ok")
    db.close()
    return db_path


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StubLLM:
    """Records the exact messages it was called with; returns fixed content."""

    def __init__(self, content: str = "The widget facts are [m3] and [m4]."):
        self.calls = []
        self.content = content

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class TestReadOnlyConsult:

    def test_consults_session_without_touching_source(self, tmp_path):
        from agent.seance import run_seance

        db_path = _make_db(tmp_path)
        before = _hash(db_path)
        # Sidecars the SessionDB WRITER created before the consult (WAL, locks, test home)
        # are baseline; the consult itself must add none and change no bytes.
        baseline_files = {p.name for p in tmp_path.iterdir()}
        stub = StubLLM()
        result = run_seance("se-1", "what are the widget facts?", db_path=db_path, call_llm_fn=stub)

        assert result.answer == "The widget facts are [m3] and [m4]."
        assert result.session_id == "se-1"
        assert result.transcript_chars > 0
        assert result.truncated is False
        # Source bytes are unchanged. SQLite itself must materialize empty -wal/-shm
        # sidecars to ATTACH read-only to a WAL database — nothing else may appear.
        assert _hash(db_path) == before
        new_files = {p.name for p in tmp_path.iterdir()} - baseline_files
        assert new_files <= {"state.db-wal", "state.db-shm"}

    def test_read_only_file_still_consults(self, tmp_path):
        from agent.seance import run_seance

        db_path = _make_db(tmp_path)
        os.chmod(db_path, 0o444)
        try:
            stub = StubLLM("ok")
            result = run_seance("se-1", "anything?", db_path=db_path, call_llm_fn=stub)
            assert result.answer == "ok"
        finally:
            os.chmod(db_path, 0o644)

    def test_unknown_session_fails_closed(self, tmp_path):
        from agent.seance import SeanceError, run_seance

        db_path = _make_db(tmp_path)
        with pytest.raises(SeanceError, match="no-such-session"):
            run_seance("no-such-session", "q", db_path=db_path, call_llm_fn=StubLLM())

    def test_missing_db_fails_closed(self, tmp_path):
        from agent.seance import SeanceError, run_seance

        with pytest.raises(SeanceError, match="state.db"):
            run_seance("se-1", "q", db_path=tmp_path / "missing" / "state.db", call_llm_fn=StubLLM())


class TestPromptAndRendering:

    def test_prompt_carries_untrusted_and_citation_rules(self, tmp_path):
        from agent.seance import run_seance

        db_path = _make_db(tmp_path)
        stub = StubLLM()
        run_seance("se-1", "what happened?", db_path=db_path, call_llm_fn=stub, task="seance")

        assert len(stub.calls) == 1
        call = stub.calls[0]
        assert call["task"] == "seance"
        system = call["messages"][0]
        assert system["role"] == "system"
        for needle in ("untrusted evidence", "cite", "never", "[m"):
            assert needle in system["content"]
        user = call["messages"][1]["content"]
        assert "what happened?" in user
        assert "question 0 about widgets" in user  # full transcript, not excerpts
        assert "[m3]" in user  # numbered rows are citable

    def test_transcript_numbers_match_message_ids(self, tmp_path):
        from agent import seance

        db_path = _make_db(tmp_path)
        rendered = seance.render_transcript(db_path, "se-1", max_chars=100_000)
        # Rows carry [m<row-id>] cites matching messages.id (first row id is 1 here).
        assert "[m1] user: question 0 about widgets" in rendered.text
        assert "[m2] assistant: answer 0" in rendered.text

    def test_oversized_transcript_keeps_head_and_tail(self, tmp_path):
        from agent import seance

        db_path = tmp_path / "state.db"
        db = SessionDB(db_path)
        db.create_session("se-big", "cli", model="m")
        db.append_message("se-big", "user", "START " + "a" * 50_000)
        for i in range(50):
            db.append_message("se-big", "user", f"middle {i} " + "b" * 2_000)
        db.append_message("se-big", "assistant", "END " + "z" * 50_000)
        db.close()

        rendered = seance.render_transcript(db_path, "se-big", max_chars=20_000)
        assert rendered.truncated is True
        assert rendered.text.startswith("[m1] user: START")
        # The newest row is kept from its beginning, cite tag included.
        assert "[m52] assistant: END" in rendered.text
        assert "truncated" in rendered.text
        assert rendered.chars <= 20_000 + 2_000  # marker slack

    def test_tool_result_bodies_are_clipped(self, tmp_path):
        from agent import seance

        db_path = tmp_path / "state.db"
        db = SessionDB(db_path)
        db.create_session("se-tool", "cli", model="m")
        db.append_message("se-tool", "user", "run it")
        db.append_message("se-tool", "tool", "x" * 30_000, tool_call_id="c0", tool_name="terminal")
        db.close()

        rendered = seance.render_transcript(db_path, "se-tool", max_chars=100_000)
        assert rendered.text.count("x") < 12_000  # head+tail clip, not the full 30k
        assert "clipped" in rendered.text
