"""Skills changes ride in-history system updates on capable routes (B10 producer wiring).

`_finish_change` is the single deferral seam for every /skills mutation. New order:
`--now` (cache clear) → in-history queue when the live agent's route is capable
(cache kept) → deferral notice. Pins that a capable route queues instead of
deferring, and an incapable/agentless context keeps today's notice verbatim.
"""

import io

from rich.console import Console

import hermes_cli.skills_hub as hub
import agent.in_history_system_updates as ihsu


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=120), buf


class _Agent:
    api_mode = "chat_completions"


def _pending(agent) -> list[str]:
    return list(getattr(agent, "_pending_in_history_updates", []) or [])


class TestFinishChange:

    def test_capable_route_queues_update_and_keeps_cache(self, monkeypatch):
        monkeypatch.setattr(ihsu, "_in_history_enabled", lambda: True)
        agent = _Agent()
        monkeypatch.setattr(hub, "_agent_provider", lambda: agent)
        console, out = _console()

        hub._finish_change(console, invalidate_cache=False,
                           what="Skill will be available", verb="activate",
                           detail="Skill 'demo' is installed and available now.")

        text = out.getvalue()
        assert "in-history system update" in text
        assert "next session" not in text
        assert _pending(agent) == ["Skill 'demo' is installed and available now."]

    def test_incapable_route_keeps_deferral_notice(self, monkeypatch):
        monkeypatch.setattr(ihsu, "_in_history_enabled", lambda: False)
        agent = _Agent()
        monkeypatch.setattr(hub, "_agent_provider", lambda: agent)
        console, out = _console()

        hub._finish_change(console, invalidate_cache=False)

        text = out.getvalue()
        assert "next session" in text
        assert _pending(agent) == []

    def test_no_live_agent_keeps_deferral_notice(self, monkeypatch):
        monkeypatch.setattr(ihsu, "_in_history_enabled", lambda: True)
        monkeypatch.setattr(hub, "_agent_provider", None)
        console, out = _console()

        hub._finish_change(console, invalidate_cache=False)

        assert "next session" in out.getvalue()

    def test_now_flag_still_clears_cache_directly(self, monkeypatch):
        cleared = []
        monkeypatch.setattr(hub, "_clear_skills_cache", lambda: cleared.append(True))
        monkeypatch.setattr(hub, "_agent_provider", lambda: _Agent())
        monkeypatch.setattr(ihsu, "_in_history_enabled", lambda: True)
        console, out = _console()

        hub._finish_change(console, invalidate_cache=True)

        assert cleared == [True]
        assert out.getvalue() == ""
