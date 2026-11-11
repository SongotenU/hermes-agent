"""Shared persistent approval-mode command logic.

Approval mode is profile-scoped configuration, not conversation state. Changing it affects
subsequent terminal guard checks immediately because approval.py loads config on each check; it must
not rebuild a live agent or mutate its system prompt/tool schema, preserving the prompt-cache
prefix.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from typing import Optional

VALID_APPROVAL_MODES = ("manual", "smart", "off")


@dataclass(frozen=True)
class ApprovalModeResult:
    ok: bool
    mode: str
    changed: bool
    message: str


def _effective_mode() -> str:
    """Return the exact mode enforced by the terminal approval guard."""
    from tools.approval_context import _get_approval_mode
    return _get_approval_mode()


def run_approval_mode_command(requested_mode: Optional[str]) -> ApprovalModeResult:
    """Inspect or persist ``approvals.mode`` through canonical config APIs."""
    current = _effective_mode()
    requested = (requested_mode or "").strip().lower()

    if not requested:
        return ApprovalModeResult(True, current, False, f"Approval mode: {current} (persistent profile setting).")
    if requested not in VALID_APPROVAL_MODES:
        return ApprovalModeResult(False, current, False, "Usage: /approvals [manual|smart|off]")

    # set_config_value is the canonical managed-scope/write-safety chokepoint. It reports managed
    # policy through stderr + SystemExit, and the fail-closed write guard raises RuntimeError on an
    # unparseable config.yaml; capture both for slash-command output instead of terminating the
    # interactive worker.
    from hermes_cli.config import set_config_value
    output = StringIO()
    try:
        with redirect_stdout(output), redirect_stderr(output):
            set_config_value("approvals.mode", requested)
    except SystemExit:
        detail = output.getvalue().strip() or "Approval mode is managed and cannot be changed."
        return ApprovalModeResult(False, current, False, detail)
    except Exception as exc:
        return ApprovalModeResult(False, current, False, f"Failed to save approval mode: {exc}")

    effective = _effective_mode()
    if effective != requested:
        return ApprovalModeResult(
            False, effective, False,
            f"Approval mode remains {effective}; the requested value did not become effective.",
        )
    return ApprovalModeResult(
        True, effective, effective != current, f"Approval mode: {effective} (persistent profile setting).",
    )


def run_forget_approvals_command() -> str:
    """Revoke session grants + the persisted allowlist; render the outcome line.

    Security-relevant side effect lives here (shared by the CLI slash handler) so
    the mixin stays a thin renderer; the gateway handler keeps its own admin gate.
    """
    from tools.approval import forget_all_approval_grants

    result = forget_all_approval_grants()
    return (
        f"Forgot {result['session']} session grant(s) and "
        f"{result['permanent']} persisted pattern(s)."
    )


class CLIApprovalCommandsMixin:
    """Slash handlers for approval surfaces (/approvals, /forget-approvals).

    Split from ``cli_commands_mixin.py`` (file-size ratchet); both commands mutate
    profile-wide security policy, so they live next to their shared logic."""

    def _handle_approvals_command(self, cmd_original: str) -> None:
        """Show or persist the profile-wide dangerous-command approval mode."""
        from cli_render import _cprint as _cp

        parts = (cmd_original or "").strip().split(None, 1)
        result = run_approval_mode_command(parts[1] if len(parts) > 1 else None)
        _cp(f"  {result.message}")

    def _handle_forget_approvals_command(self, cmd_original: str) -> None:
        """Revoke this session's grants and every persisted 'always' pattern."""
        from cli_render import _cprint as _cp

        _cp(f"  {run_forget_approvals_command()}")
