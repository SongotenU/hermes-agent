"""Redirect SSRF guard for MCP HTTP clients.

A user-configured public MCP endpoint must not be able to redirect the client into private or
internal space (``302`` to ``169.254.169.254``, an RFC1918 host, or a cloud-metadata hostname).
A locally-configured private/loopback endpoint keeps its own redirects — blocking same-host
scheme upgrades (http → https on 127.0.0.1) would break the common local-server setup.

Behaviour-only tests: the guard factory is exercised through real async calls; nothing here
reads source text.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tools.mcp_tool_transport import _make_ssrf_redirect_guard

# IP literals so no DNS resolution is needed for the origin/target classification.
PUBLIC = "http://93.184.216.34/mcp"        # example.com
PUBLIC_OTHER = "http://93.184.216.35/next"
PRIVATE = "http://127.0.0.1:3000/mcp"


def _redirect(location: str, url: str = PUBLIC) -> SimpleNamespace:
    return SimpleNamespace(is_redirect=True, headers={"location": location}, url=url)


def _plain(url: str = PUBLIC) -> SimpleNamespace:
    return SimpleNamespace(is_redirect=False, headers={}, url=url)


def _run(guard, response) -> None:
    asyncio.run(guard(response))


def test_public_origin_redirect_to_loopback_blocked():
    guard = _make_ssrf_redirect_guard(PUBLIC)
    with pytest.raises(ValueError, match="Blocked redirect to private/internal address"):
        _run(guard, _redirect("http://127.0.0.1:8080/steal"))


def test_public_origin_redirect_to_rfc1918_blocked():
    guard = _make_ssrf_redirect_guard(PUBLIC)
    with pytest.raises(ValueError, match="Blocked redirect"):
        _run(guard, _redirect("http://10.0.0.5/internal"))


def test_public_origin_redirect_to_metadata_hostname_blocked():
    guard = _make_ssrf_redirect_guard(PUBLIC)
    with pytest.raises(ValueError, match="Blocked redirect"):
        _run(guard, _redirect("http://metadata.google.internal/computeMetadata/v1/"))


def test_public_origin_redirect_to_public_host_allowed():
    guard = _make_ssrf_redirect_guard(PUBLIC)
    _run(guard, _redirect(PUBLIC_OTHER))  # must not raise


def test_public_origin_scheme_upgrade_same_public_host_allowed():
    guard = _make_ssrf_redirect_guard(PUBLIC)
    _run(guard, _redirect("https://93.184.216.34/mcp"))  # must not raise


def test_private_origin_keeps_own_redirects():
    """A local endpoint's http → https upgrade on the same host must keep working."""
    guard = _make_ssrf_redirect_guard(PRIVATE)
    _run(guard, _redirect("https://127.0.0.1:3000/mcp"))  # must not raise


def test_non_redirect_responses_are_ignored():
    guard = _make_ssrf_redirect_guard(PUBLIC)
    _run(guard, _plain())  # must not raise


def test_origin_class_is_resolved_once_per_guard():
    """The origin is classified on the first redirect and cached for the client's lifetime."""
    calls: list[str] = []

    async def _fake_safe(url: str) -> bool:
        calls.append(url)
        return True

    from tools import url_safety

    original = url_safety.async_is_safe_url
    url_safety.async_is_safe_url = _fake_safe  # type: ignore[assignment]
    try:
        guard = _make_ssrf_redirect_guard(PUBLIC)
        _run(guard, _redirect(PUBLIC_OTHER))
        _run(guard, _redirect(PUBLIC_OTHER))
    finally:
        url_safety.async_is_safe_url = original  # type: ignore[assignment]
    # origin resolved once + one target validation per redirect = 3, with the origin only in slot 0.
    assert calls[0] == PUBLIC
    assert calls.count(PUBLIC) == 1
