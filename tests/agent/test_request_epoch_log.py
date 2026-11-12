"""Request-epoch logging (DeepSeek Harness ``request/header`` port).

Every conversation request logs its envelope epoch once: a change names WHY the
provider-side prompt cache can no longer hit; an unchanged envelope after a history
replacement logs a series restart; ordinary append-only requests stay silent
(silence == warm).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from agent.request_epoch import log_request_epoch

_LOGGER_NAME = "agent.conversation_loop"


class _Agent:
    def __init__(self, **over):
        self.model = "test/model"
        self.provider = "test-provider"
        self.api_mode = "chat_completions"
        self.base_url = "https://api.example.test/v1"
        self.session_id = "sess-1"
        self.log_prefix = ""
        for key, value in over.items():
            setattr(self, key, value)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _epoch(agent=None, *, system="sys", tools=None, messages=None, **over):
    agent = agent or _Agent(**over)
    capture = _Capture()
    log = logging.getLogger(_LOGGER_NAME)
    previous_level = log.level
    log.setLevel(logging.INFO)
    log.addHandler(capture)
    try:
        log_request_epoch(
            agent,
            system_message=system,
            tools_for_api=tools if tools is not None else [{"function": {"name": "a"}}],
            api_messages=messages if messages is not None else [{"role": "user", "content": "hi"}],
        )
    finally:
        log.removeHandler(capture)
        log.setLevel(previous_level)
    return agent, capture.lines


class TestRequestEpoch:
    def test_first_request_logs_initial_with_fingerprints(self):
        _, lines = _epoch()

        assert len(lines) == 1
        assert "reason=initial" in lines[0]
        assert "model=test/model" in lines[0]
        assert "provider=test-provider" in lines[0]
        assert "system=" in lines[0]
        assert "tools=" in lines[0]

    def test_first_request_carrying_restored_history_logs_resume(self):
        history = [{"role": "user", "content": str(i)} for i in range(4)]
        _, lines = _epoch(messages=history)

        assert len(lines) == 1
        assert "reason=resume" in lines[0]

    def test_unchanged_envelope_stays_silent(self):
        agent, lines = _epoch()
        assert len(lines) == 1

        # Same envelope, append-only growth: the cache prefix stays warm — no log.
        _, second = _epoch(agent=agent, messages=[{"role": "user", "content": "hi"},
                                                  {"role": "assistant", "content": "yo"},
                                                  {"role": "user", "content": "more"}])
        assert second == []

    def test_envelope_change_names_the_changed_fields(self):
        agent, _ = _epoch()

        _, lines = _epoch(agent=agent, system="sys")
        assert lines == []  # control: unchanged

        agent.model = "other/model"
        _, lines = _epoch(agent=agent)
        assert len(lines) == 1
        assert "reason=change" in lines[0]
        assert "changes=model" in lines[0]
        assert "model=test/model->other/model" in lines[0]

    def test_tools_and_system_changes_are_detected(self):
        agent, _ = _epoch()

        _, lines = _epoch(agent=agent, tools=[{"function": {"name": "b"}}])
        assert "changes=tools" in lines[0]

        _, lines = _epoch(agent=agent, system="sys2")
        assert "changes=system" in lines[0]

    def test_history_replacement_restarts_the_series(self):
        agent, _ = _epoch(messages=[{"role": "user", "content": str(i)} for i in range(6)])

        # Same envelope, shorter transcript (compression replaced history) -> series.
        _, lines = _epoch(agent=agent, messages=[{"role": "user", "content": "summary"}])
        assert len(lines) == 1
        assert "reason=series" in lines[0]
        assert "starts_series=true" in lines[0]

    def test_session_rotation_reports_scope_change_and_series_restart(self):
        agent, _ = _epoch()

        # A rotation (compression child session) changes the cache scope — the provider
        # cache cannot carry across scopes — and restarts the message series.
        agent.session_id = "sess-2"
        _, lines = _epoch(agent=agent)
        assert len(lines) == 1
        assert "reason=change" in lines[0]
        assert "changes=scope" in lines[0]
        assert "starts_series=true" in lines[0]

    def test_change_with_series_restart_flags_both(self):
        agent, _ = _epoch(messages=[{"role": "user", "content": str(i)} for i in range(6)])

        agent.model = "other/model"
        _, lines = _epoch(agent=agent, messages=[{"role": "user", "content": "summary"}])
        assert len(lines) == 1
        assert "reason=change" in lines[0]
        assert "changes=model" in lines[0]
        assert "starts_series=true" in lines[0]

    def test_logging_failure_never_raises(self):
        class _Exploding:
            session_id = "sess-x"
            log_prefix = ""

            @property
            def model(self):
                raise RuntimeError("boom")

        # Must not raise: epoch logging is hot-path observability.
        log_request_epoch(
            _Exploding(), system_message="s", tools_for_api=[],
            api_messages=[{"role": "user", "content": "hi"}],
        )


def test_build_api_request_logs_the_epoch(monkeypatch):
    """The epoch log fires from the loop's single request-assembly point."""
    import agent.conversation_loop as loop
    import agent.turn_api_request as tar

    calls = []
    monkeypatch.setattr(tar, "log_request_epoch", lambda agent, **kw: calls.append(kw))

    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent._empty_content_retries = 0
    agent._is_openrouter_url.return_value = False
    agent._is_copilot_url.return_value = False
    agent._build_api_kwargs.return_value = {"messages": []}
    agent.tools = []

    monkeypatch.setattr(
        loop, "_redecorate_prompt_cache_for_provider",
        lambda a, msgs, **kw: (msgs, None, kw.get("tools_for_api") or []),
    )

    api_messages = [{"role": "user", "content": "hi"}]
    tar.build_api_request(
        agent, api_messages=api_messages, _moa_prepared_request=None, tools_for_api=[],
        system_message="sys", messages=api_messages, original_user_message="hi",
        approx_tokens=1, total_chars=2, retry_count=0, api_call_count=1,
        api_request_id="r1", api_start_time=0.0, effective_task_id="t", turn_id="turn",
    )

    assert len(calls) == 1
    assert calls[0]["system_message"] == "sys"
    assert calls[0]["api_messages"] is api_messages
