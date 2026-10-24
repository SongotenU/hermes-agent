"""Memory files that change mid-conversation reach the model without a prompt rebuild.

The MEMORY.md / USER.md blocks live in the system prompt, and a conversation keeps those bytes
for its whole life so the provider prefix cache stays warm. An edit made after the prompt was
built — another session on the same profile, an MCP bridge, a hand edit — therefore never
reaches the block the model reads. Every turn compares the block the prompt carries with the
files as a fresh load would read them and delivers the difference on the per-turn user-message
note channel, behind the cached prefix (the ``skills_index_delta`` pattern; the one-time drift
note itself is ported from mnemopi).

Each note states the FULL difference against the prompt, so the prompt plus the newest note is
the complete record of what the model was told. A fresh agent (the gateway builds one per turn)
reads that record back from the transcript once; a long-lived agent (CLI, TUI, Desktop) keeps it
on itself. A note is sent only when the difference changes. Entry rows are whitespace-collapsed
single lines, and both sides are capped by the memory files' own char limits, so the note can
never outgrow the files it describes.
"""
from __future__ import annotations

import logging
from typing import Any

from agent.message_content import flatten_message_text
from tools.memory_tool_store import ENTRY_DELIMITER, MEMORY_BLOCK_HEADERS

logger = logging.getLogger("run_agent")

_NOTE_PREFIX = "[System: The memory files changed after this conversation's system prompt was built. The memory block in your prompt is a frozen snapshot; the entries below are the current on-disk state."
_ACCURATE_AGAIN = " The memory in the system prompt is accurate again; earlier memory-change notes are superseded."
_ROW = "    - "
_FILE_FOR = {"memory": "MEMORY.md", "user": "USER.md"}
_FILE_TARGETS = {filename: target for target, filename in _FILE_FOR.items()}
_SEPARATOR = "═" * 46

_State = dict[str, set[str]]


def _key(entry: str) -> str:
    """Note-row identity of an entry: whitespace-collapsed so the newest note can be read back
    from the transcript as an exact record (entries are multi-line in the files)."""
    return " ".join(entry.split())


def _empty_state() -> _State:
    return {target: set() for target in _FILE_FOR}


def entries_in_prompt(prompt: Any) -> dict[str, list[str]]:
    """``{target: entries}`` parsed from ``prompt``'s rendered memory blocks; ``[]`` without one.

    The block shape is ``<separator>\\n<title> [usage]\\n<separator>\\n<entry>§<entry>``
    (``MemoryStore._render_block``); entries may span lines.
    """
    out: dict[str, list[str]] = {target: [] for target in _FILE_FOR}
    if not isinstance(prompt, str):
        return out
    for target, title in (("memory", MEMORY_BLOCK_HEADERS["memory"]), ("user", MEMORY_BLOCK_HEADERS["user"])):
        start = prompt.find(f"{_SEPARATOR}\n{title} [")
        if start == -1:
            continue
        body_start = prompt.find(f"]\n{_SEPARATOR}\n", start)
        if body_start == -1:
            continue
        tail = prompt[body_start + len(f"]\n{_SEPARATOR}\n"):]
        end = tail.find(f"\n{_SEPARATOR}\n")
        body = tail[:end] if end != -1 else tail
        out[target] = [e for e in (x.strip() for x in body.split(ENTRY_DELIMITER)) if e]
    return out


def _render(added: _State, removed: _State) -> str:
    if not any(added.values()) and not any(removed.values()):
        return f"{_NOTE_PREFIX}{_ACCURATE_AGAIN}]"
    parts = [_NOTE_PREFIX]
    for target in ("memory", "user"):
        if added.get(target):
            parts.append(f"{_FILE_FOR[target]} added:")
            parts += [f"{_ROW}{key}" for key in sorted(added[target])]
        if removed.get(target):
            parts.append(f"{_FILE_FOR[target]} removed:")
            parts += [f"{_ROW}{key}" for key in sorted(removed[target])]
    return "\n".join(parts) + "]"


def _parse(note: str) -> tuple[_State, _State]:
    """Inverse of :func:`_render` for the text that follows ``_NOTE_PREFIX``."""
    added, removed = _empty_state(), _empty_state()
    sink: set[str] | None = None
    for line in note.split("\n"):
        if line.endswith(" added:") and (target := _FILE_TARGETS.get(line[: -len(" added:")])):
            sink = added[target]
        elif line.endswith(" removed:") and (target := _FILE_TARGETS.get(line[: -len(" removed:")])):
            sink = removed[target]
        elif sink is not None and line.startswith(_ROW):
            sink.add(line[len(_ROW):].removesuffix("]"))
    return added, removed


def _announced(conversation_history: Any) -> tuple[_State, _State]:
    """The difference named by the newest memory note anywhere in the transcript: a note far back
    still describes the current state when nothing changed since it was sent."""
    for msg in reversed(conversation_history or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        sidecar = msg.get("api_content")
        text = (sidecar if isinstance(sidecar, str) else "") + "\n" + flatten_message_text(msg.get("content"))
        if _NOTE_PREFIX in text:
            return _parse(text.rsplit(_NOTE_PREFIX, 1)[1])
    return _empty_state(), _empty_state()


def stage_memory_delta_note(agent: Any, prompt: Any, conversation_history: Any) -> bool:
    """Stage a one-shot note when the memory files on disk differ from ``prompt``'s blocks by
    something the model has not been told yet. Returns whether it staged.

    MoA and codex_app_server turns never stamp the ``api_content`` sidecar, so the note could not
    be read back from the transcript; those modes skip it, like the skills-index note."""
    if getattr(agent, "provider", None) == "moa" or getattr(agent, "api_mode", None) == "codex_app_server":
        return False
    store = getattr(agent, "_memory_store", None)
    if store is None or not isinstance(prompt, str):
        return False
    enabled = {"memory": bool(getattr(agent, "_memory_enabled", True)),
               "user": bool(getattr(agent, "_user_profile_enabled", True))}
    if not any(enabled.values()):
        return False
    stored = {target: {_key(e) for e in entries_in_prompt(prompt).get(target, [])}
              for target in _FILE_FOR if enabled[target]}
    current = {target: {_key(e) for e in store.fresh_entries_from_disk(target)}
               for target in _FILE_FOR if enabled[target]}
    told = getattr(agent, "_memory_delta_told", None)
    key = hash(prompt)
    if isinstance(told, tuple) and told[0] == key:
        told_added, told_removed = told[1]
    else:
        told_added, told_removed = _announced(conversation_history)
    # What the model believes the files hold: the prompt's blocks as amended by the newest note.
    # A rebuilt prompt (compaction) that now carries the current files needs no further note.
    believed = {t: (stored[t] | told_added.get(t, set())) - told_removed.get(t, set()) for t in stored}
    if believed == current:
        agent._memory_delta_told = (key, (told_added, told_removed))
        return False
    added = {t: {e for e in current[t] if e not in stored[t]} for t in stored}
    removed = {t: {e for e in stored[t] if e not in current[t]} for t in stored}
    agent._memory_delta_told = (key, (added, removed))
    agent._memory_delta_note = _render(added, removed)
    logger.info(
        "Session %s: memory files changed (+%d/-%d) since the system prompt was built; delivering "
        "the change as a turn note (prefix cache preserved).",
        getattr(agent, "session_id", None),
        sum(len(v) for v in added.values()), sum(len(v) for v in removed.values()),
    )
    return True
