"""A live local session whose routing entry vanished heals from the receipt instead of refusing.

Regression for the #140642 class: the cold restore branch rebuilds the routing entry from the
receipt, but the live branch (a session already registered this process) refused with
``admission_conflict`` forever once the entry was gone (pruned stale index, crashed-gateway
self-heal, desktop auto-resume racing a store prune). The receipt is the route's truth and the
live session already passed every ownership check, so the live branch must rebuild the entry
exactly like the cold branch and only keep refusing a non-stray foreign target.
"""
from types import SimpleNamespace

import pytest


def _build_runtime(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authorities import SessionAuthorities
    from gateway import run
    root = tmp_path / '.hermes'
    root.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv('HERMES_HOME', str(root))
    # The autouse isolation fixture pins hermes_state.DEFAULT_DB_PATH to its own fake home once
    # hermes_state is imported; per-home stores must follow the scoped HERMES_HOME here.
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    registry = SessionAuthorities(root, multiplexed=True)
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={},
                             _draining=False, session_authorities=registry)
    return runner, registry, root


@pytest.mark.asyncio
async def test_live_restore_rebuilds_a_missing_routing_entry(tmp_path, monkeypatch, caplog):
    import logging
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_local_recovery import restore_local_session

    runner, registry, root = _build_runtime(tmp_path, monkeypatch)
    authority = await initialize_session_authority(runner, profile_id=str(root), instance_id='one')
    registry.add(root, authority, name='default')
    actor = Principal('owner', str(root), frozenset({'session:create', 'session:read'}), 'socket')
    ref = create_local_session(authority, actor, {'request_id': 'heal', 'source': 'gui',
                                                  'cwd': str(tmp_path), 'model': 'frozen',
                                                  'toolsets': []})
    restore_local_session(authority, ref.session_id)  # cold restore: registers the live session
    store = runner.session_store
    route = next(key for key, entry in store._entries.items() if entry.session_id == ref.session_id)
    with store._lock:
        del store._entries[route]  # a stale-index prune dropped the routing entry
    assert ref.session_id in authority.sessions  # the next restore runs the LIVE branch

    with caplog.at_level(logging.WARNING, logger='gateway.session_local_recovery'):
        restore_local_session(authority, ref.session_id)  # heals; must not raise admission_conflict

    with store._lock:
        healed = store._entry_locked(route)
    assert healed is not None and healed.session_id == ref.session_id
    assert any('rebuilding it from the receipt' in record.getMessage() for record in caplog.records)
