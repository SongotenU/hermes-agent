"""Vision on the managed llama.cpp runtime follows the LIVE endpoint, not a port in config.

The stable port moves to an ephemeral one whenever something else holds it (a second Hermes process
booting the same runtime, a leftover socket), so a base_url pasted into ``auxiliary.vision`` outlives
its server and strands the task on a 404 — while leaving it empty fell through to an unrelated client
entirely. The supervisor state file is the one source of truth both paths already share.
"""
import json
import os

import pytest

from agent import auxiliary_client as aux

LIVE_BASE = "http://127.0.0.1:50705/v1"
LIVE_KEY = "state-key"


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "runtimes" / "llamacpp" / "server.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("hermes_cli.local_runtime.supervisor.state_path", lambda: path)
    return path


def _write_state(path, *, pid, base=LIVE_BASE, key=LIVE_KEY):
    path.write_text(json.dumps({"base_url": base, "api_key": key, "pid": pid}), encoding="utf-8")


def _client_capture(seen):
    def _fake(provider, **kwargs):
        seen["provider"] = provider
        seen["base"] = kwargs.get("explicit_base_url")
        seen["key"] = kwargs.get("explicit_api_key")
        return "client", "model"

    return _fake


def test_managed_endpoint_reads_the_live_state_file(state_file):
    _write_state(state_file, pid=os.getpid())
    assert aux.managed_local_endpoint() == (LIVE_BASE, LIVE_KEY)


def test_managed_endpoint_is_empty_without_a_live_server(state_file):
    assert aux.managed_local_endpoint() == ("", "")  # no state file at all

    _write_state(state_file, pid=2**30)  # a pid that cannot be alive
    assert aux.managed_local_endpoint() == ("", "")

    state_file.write_text("{not json", encoding="utf-8")
    assert aux.managed_local_endpoint() == ("", "")


def test_llamacpp_vision_uses_the_live_endpoint_when_config_has_none(state_file, monkeypatch):
    _write_state(state_file, pid=os.getpid())
    seen: dict = {}
    monkeypatch.setattr(aux, "resolve_provider_client", _client_capture(seen))

    provider, client, model = aux.resolve_vision_provider_client(provider="llamacpp", model="local-vl")

    # The alias keeps the custom transport, pointed at the live port with the persisted key.
    assert seen == {"provider": "custom", "base": LIVE_BASE, "key": LIVE_KEY}
    assert (provider, client, model) == ("custom", "client", "model")


def test_a_configured_base_url_still_wins(state_file, monkeypatch):
    """An explicit endpoint (a user-run server, a remote box) is never second-guessed."""
    _write_state(state_file, pid=os.getpid())
    seen: dict = {}
    monkeypatch.setattr(aux, "resolve_provider_client", _client_capture(seen))

    aux.resolve_vision_provider_client(
        provider="llamacpp", model="local-vl", base_url="http://10.0.0.5:9999/v1", api_key="explicit")

    assert seen["base"] == "http://10.0.0.5:9999/v1"
    assert seen["key"] == "explicit"


def test_managed_endpoint_tolerates_a_state_file_without_a_pid(state_file):
    state_file.write_text(json.dumps({"base_url": LIVE_BASE, "api_key": LIVE_KEY}), encoding="utf-8")
    assert aux.managed_local_endpoint() == (LIVE_BASE, LIVE_KEY)
