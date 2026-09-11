"""`_response_profile_name` must fall back to the launch profile — never raise.

Regression: a session dict's profile_home basename is the *install dir* name
when the session is scoped to the default profile (e.g. ".hermes"). The
desktop's session.activate crashed with FileNotFoundError("Profile '.hermes'
does not exist.") because the contract ("real non-launch profile, else the
launch one") was implemented by calling `_profile_home`, which raises instead
of returning None for unknown names.
"""

from __future__ import annotations

from pathlib import Path

import tui_gateway.server as server


def test_default_home_basename_falls_back_instead_of_raising(tmp_path, monkeypatch):
    # Mimic the real install layout: launch home is <tmp>/.hermes, and the only
    # named profiles live under <tmp>/.hermes/profiles/.
    launch_home = tmp_path / ".hermes"
    launch_home.mkdir()
    (launch_home / "profiles" / "worker").mkdir(parents=True)

    monkeypatch.setattr(server, "_hermes_home", str(launch_home))
    import hermes_cli.profiles as profiles_mod
    monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: launch_home / "profiles")

    junk = launch_home.name  # exactly what server.py:2062 passes for default-scoped sessions
    assert junk == ".hermes"

    result = server._response_profile_name(junk)  # must not raise
    assert result == server._current_profile_name()


def test_real_named_profile_still_resolves(tmp_path, monkeypatch):
    launch_home = tmp_path / ".hermes"
    (launch_home / "profiles" / "worker").mkdir(parents=True)

    monkeypatch.setattr(server, "_hermes_home", str(launch_home))
    import hermes_cli.profiles as profiles_mod
    monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: launch_home / "profiles")

    try:
        assert server._response_profile_name("worker") == "worker"
    finally:
        server._served_profile_homes.clear()


def test_empty_name_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_hermes_home", str(tmp_path / ".hermes"))
    assert server._response_profile_name("") == server._current_profile_name()
    assert server._response_profile_name(None) == server._current_profile_name()
