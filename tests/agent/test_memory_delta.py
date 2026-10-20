"""Memory files that change mid-conversation reach the model, behind the cached system prompt.

MEMORY.md / USER.md are frozen in the system prompt for the prefix cache, so an external change
(another session, an MCP bridge, a hand edit) is delivered on the per-turn note channel — the
skills-index delta pattern, ported from mnemopi's one-time drift note.
"""
from types import SimpleNamespace

from agent.memory_delta import stage_memory_delta_note
from agent.turn_context import consume_memory_delta_note
from tools.memory_tool_store import ENTRY_DELIMITER, MemoryStore


def _write(tmp_path, filename, text):
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / filename).write_text(text, encoding="utf-8")


def _store():
    store = MemoryStore()
    store.load_from_disk()
    return store


def _agent(store):
    return SimpleNamespace(provider="openrouter", api_mode="chat_completions", session_id="s1",
                           _memory_store=store, _memory_enabled=True, _user_profile_enabled=True)


def _turn(agent, prompt, history):
    """One turn: stage, then the consume ``_merge_gateway_notes`` does; returns the note sent."""
    stage_memory_delta_note(agent, prompt, history)
    note = consume_memory_delta_note(agent)
    history.append({"role": "user", "content": "hi", **({"api_content": f"hi\n\n{note}"} if note else {})})
    history.append({"role": "assistant", "content": "ok"})
    return note


def test_long_lived_agent_is_told_each_memory_change_once(tmp_path, monkeypatch):
    """Every turn compares disk against the prompt's frozen block. The note names only what
    changed, is sent once per change (a fresh gateway agent reading the transcript included,
    however far back the note is), and retires itself when the change is undone."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(tmp_path, "MEMORY.md", "First note.")
    store = _store()
    agent = _agent(store)
    prompt = "Identity.\n\n" + store.format_for_system_prompt("memory")
    history = []
    assert _turn(agent, prompt, history) == ""

    _write(tmp_path, "MEMORY.md", "First note." + ENTRY_DELIMITER + "Second note added externally.")
    note = _turn(agent, prompt, history)
    assert "MEMORY.md added:" in note and "    - Second note added externally." in note
    assert "First note." not in note
    assert _turn(agent, prompt, history) == ""
    history += [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}] * 150
    assert _turn(_agent(_store()), prompt, history) == ""  # fresh gateway agent, same record

    _write(tmp_path, "MEMORY.md", "Second note added externally.")
    note = _turn(agent, prompt, history)
    assert "MEMORY.md removed:" in note and "    - First note." in note
    assert "MEMORY.md added:" in note and "Second note added externally." in note
    assert _turn(agent, prompt, history) == ""

    # A compaction rebuild that now carries the current file needs no further note.
    rebuilt = "Identity.\n\n" + _store().format_for_system_prompt("memory")
    assert _turn(_agent(_store()), rebuilt, history) == ""


def test_note_retires_when_disk_matches_the_prompt_again(tmp_path, monkeypatch):
    """A change that is undone (the file returns to the prompt's exact state) sends one
    "accurate again" note and then goes quiet — the prompt needs no rebuild."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(tmp_path, "MEMORY.md", "First note.")
    store = _store()
    agent = _agent(store)
    prompt = "Identity.\n\n" + store.format_for_system_prompt("memory")
    history = []
    _write(tmp_path, "MEMORY.md", "First note." + ENTRY_DELIMITER + "Added later.")
    assert "Added later." in _turn(agent, prompt, history)
    _write(tmp_path, "MEMORY.md", "First note.")
    note = _turn(agent, prompt, history)
    assert "accurate again" in note
    assert _turn(agent, prompt, history) == ""


def test_user_profile_changes_are_named_separately(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(tmp_path, "USER.md", "Likes tea.")
    store = _store()
    agent = _agent(store)
    prompt = "Identity.\n\n" + store.format_for_system_prompt("user")
    history = []
    _write(tmp_path, "USER.md", "Likes tea." + ENTRY_DELIMITER + "Switched to coffee.")
    note = _turn(agent, prompt, history)
    assert "USER.md added:" in note and "Switched to coffee." in note
    assert "MEMORY.md" not in note


def test_multiline_entries_keep_a_single_line_identity(tmp_path, monkeypatch):
    """Note rows are whitespace-collapsed single lines so the newest note can be read back
    from the transcript as the record of what was announced."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(tmp_path, "MEMORY.md", "First note.")
    store = _store()
    agent = _agent(store)
    prompt = "Identity.\n\n" + store.format_for_system_prompt("memory")
    history = []
    _write(tmp_path, "MEMORY.md", "First note." + ENTRY_DELIMITER + "Line one\nline two\nline three.")
    note = _turn(agent, prompt, history)
    assert "    - Line one line two line three." in note
    # Read back from the transcript: a fresh agent must not re-announce it.
    history += [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}] * 10
    assert _turn(_agent(_store()), prompt, history) == ""


def test_stage_skips_without_a_store_disabled_targets_or_sidecar_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(tmp_path, "MEMORY.md", "First note.")
    no_store = SimpleNamespace(provider="openrouter", api_mode="chat_completions", session_id="s")
    assert stage_memory_delta_note(no_store, "Identity.", []) is False
    disabled = _agent(_store())
    disabled._memory_enabled = False
    disabled._user_profile_enabled = False
    assert stage_memory_delta_note(disabled, "Identity.", []) is False
    moa = _agent(_store())
    moa.provider = "moa"
    assert stage_memory_delta_note(moa, "Identity.", []) is False
