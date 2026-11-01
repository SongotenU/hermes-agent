"""Skill revocations — administrator list refusing skills by id/version/digest/source.

Port of the OpenClaude ``skills verify``/``revocations.json`` semantics (C6,
``docs/skills.md``) onto Hermes' skills hub guard. ADMIN HOSTED: the custodian
copy lives in the super-patches repo (repo root ``revocations.json``, git is the
record), while runtime reads the operator file at
``~/.hermes/skills-revocations.json`` (or ``HERMES_SKILLS_REVOCATIONS`` override).

File shape: ``{"version": 1, "entries": [...]}`` (a bare list also works).

Entry match rule (same as the source): an entry matches when EVERY field it
names matches. Fields: ``id`` (skill name — covers all versions), ``version``,
``digest`` (``sha256:<hex>``, 16 or 64 hex chars; matches the candidate's full
content digest), and ``source`` (hub source id — ``official``, ``github``…).
An entry naming NO revocation field is skipped as malformed. Optional
``reason`` string is shown when an install is refused and by ``verify``.

A revoked skill is a HARD install block (no ``--force`` bypass): a revocation
is an admin's decision, not a scan verdict. A malformed revocations file fails
closed (install refused) so a typo cannot silently disable the list.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

FIELDS = ("id", "version", "digest", "source")
_HEX = re.compile(r"^(?:[0-9a-f]{16}|[0-9a-f]{64})$")
BAD_FILE_MESSAGE = "revocations file malformed or unreadable"
DEFAULT_FILENAME = "skills-revocations.json"
ENV_VAR = "HERMES_SKILLS_REVOCATIONS"
NEEDS_SOURCE = "call with source= to evaluate source-scoped entries"


def revocations_path(path: str | os.PathLike | None = None) -> Optional[Path]:
    """File the list is read from: explicit argument, then env override, then the
    operator file under the Hermes home. ``None`` when nothing is configured."""
    if path is not None:
        return Path(path)
    env = os.environ.get(ENV_VAR) or ""
    if env:
        return Path(env)
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / DEFAULT_FILENAME


def load_revocations(path: str | os.PathLike | None = None) -> list[dict[str, Any]]:
    """Entries from the operator file: explicit path, then env, then default.
    Missing file → ``[]``. Malformed/unreadable → ``ValueError`` (fail closed)."""
    resolved = revocations_path(path)
    if resolved is None:
        return []
    try:
        data = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{BAD_FILE_MESSAGE}: {resolved}: {exc}") from exc
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and isinstance(data.get("entries"), list):
        entries = data["entries"]
    elif isinstance(data, dict) and isinstance(data.get("revocations"), list):
        entries = data["revocations"]
    else:
        raise ValueError(f"{BAD_FILE_MESSAGE}: {resolved}: not an entry list")
    return [entry for entry in entries if entry is not None]


def normalize_digest(value: Any) -> str:
    """Lowercase hex (16 or 64 chars) without the ``sha256:`` prefix; ``""`` otherwise."""
    hex_part = str(value or "").strip().lower()
    hex_part = hex_part.removeprefix("sha256:")
    return hex_part if _HEX.match(hex_part) else ""


def _digests_match(entry_digest: Any, candidate_digest: Any) -> bool:
    """Same-content rule: the shorter side's FULL length must match the longer
    side's prefix — a 64-hex entry and the 16-hex ``content_hash`` both work."""
    entry_hex = normalize_digest(entry_digest)
    candidate_hex = normalize_digest(candidate_digest)
    if not entry_hex or not candidate_hex:
        return False
    shared = min(len(entry_hex), len(candidate_hex))
    return entry_hex[:shared] == candidate_hex[:shared]


def revocation_match(entries: list[dict[str, Any]], *, skill_id: str,
                     version: Any = None, digest: Any = "",
                     source: Optional[str] = None) -> Optional[dict[str, Any]]:
    """First matching entry (every field the entry NAMES must match) or ``None``."""
    candidate_id = str(skill_id or "").strip().lower()
    candidate_version = "" if version is None else str(version)
    candidate_source = None if source is None else str(source).strip().lower()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        named = {field: entry[field] for field in FIELDS if entry.get(field)}
        if not named:
            continue  # names no revocation field — not an actionable entry
        if "id" in named and str(named["id"]).strip().lower() != candidate_id:
            continue
        if "version" in named and str(named["version"]) != candidate_version:
            continue
        if "digest" in named and not _digests_match(named["digest"], digest):
            continue
        if "source" in named:
            if candidate_source is None:  # cannot confirm the source — never match
                continue
            if str(named["source"]).strip().lower() != candidate_source:
                continue
        return entry
    return None


def block_for_bundle(skill_id: str, version: Any, skill_path: str | os.PathLike | Path,
                     source: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Revocation entry refusing this bundle at install time, or ``None`` when it
    may proceed. Digest uses the SAME canonical content hash the hub verifies
    (``tools.skills_guard``, imported lazily to keep the CLI start cheap); the
    prefix rule lets one list entry cover both the 16-hex and 64-hex forms."""
    from tools.skills_guard import content_digest_full
    try:
        entries = load_revocations()
    except ValueError as exc:
        return {"reason": str(exc)}
    if not entries:
        return None
    path = Path(skill_path)
    candidate = f"sha256:{content_digest_full(path)}"
    return revocation_match(entries, skill_id=skill_id, version=version,
                            digest=candidate, source=source)


def verify_installed(entries: list[dict[str, Any]],
                     installed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``verify`` rows for the hub's installed skills: ``name``, ``revoked``,
    ``reason``. Digest is computed LIVE from the on-disk content — the operator
    file wins over lock-file metadata by construction."""
    from tools.skills_guard import content_digest_full
    rows: list[dict[str, Any]] = []
    for record in installed:
        name = str(record.get("name", "?"))
        install_path = str(record.get("install_path") or "")
        dir_path = Path(install_path)
        if not install_path or not dir_path.is_dir():
            reason = "(not on disk — cannot verify content digest)"
            revoked = revocation_match(entries, skill_id=name, version=record.get("version"),
                                       source=record.get("source")) is not None
            rows.append({"name": name, "revoked": revoked, "reason": reason if revoked else None})
            continue
        candidate = f"sha256:{content_digest_full(dir_path)}"
        matched = revocation_match(entries, skill_id=name, version=record.get("version"),
                                   digest=candidate, source=record.get("source"))
        if matched is None:
            rows.append({"name": name, "revoked": False, "reason": None})
        else:
            rows.append({"name": name, "revoked": True,
                         "reason": str(matched.get("reason") or "revoked by administrator")})
    return rows


def describe_entry(entry: dict[str, Any]) -> str:
    """Compact ``id=x, digest=y — reason`` line for the ``revocations`` listing."""
    parts = [f"{field}={entry[field]}" for field in FIELDS if entry.get(field)]
    reason = str(entry.get("reason") or "").strip()
    named = ", ".join(parts) if parts else "(no fields — malformed entry)"
    return f"{named}" + (f" — {reason}" if reason else "")
