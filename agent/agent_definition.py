"""Agent definition files — per-role identity + toolset restrictions.

Phase 4 of harness-hardening project. Port of Claude Code's agent-def system:
`.claude/agents/*.md` with YAML frontmatter (name, description, tools, model,
mcp_servers) + markdown body that augments the system prompt.

Precedence (highest wins):
1. ~/.hermes/hermes-agent/agents/         (user)
2. <plugin_dir>/agents/                   (plugin)
3. <package>/agents/                      (built-in)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class AgentDefinition:
    """Parsed agent definition from a .md file."""

    name: str
    description: str = ""
    toolsets: Optional[List[str]] = None
    model: Optional[str] = None
    mcp_servers: Optional[List[str]] = None
    body: str = ""


def _parse_agent_file(path: Path) -> Optional[AgentDefinition]:
    """Parse a single agent definition .md file."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("agent_definition: cannot read %s: %s", path, exc)
        return None

    name = path.stem

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) < 3:
            logger.warning("agent_definition: malformed frontmatter in %s", path)
            return None
        frontmatter_text = parts[1].strip()
        body = parts[2].strip()
    else:
        frontmatter_text = ""
        body = text.strip()

    meta: dict = {}
    if frontmatter_text:
        try:
            meta = yaml.safe_load(frontmatter_text) or {}
        except yaml.YAMLError as exc:
            logger.warning("agent_definition: YAML parse error in %s: %s", path, exc)
            return None

    return AgentDefinition(
        name=meta.get("name", name),
        description=meta.get("description", ""),
        toolsets=meta.get("toolsets") or meta.get("tools"),
        model=meta.get("model"),
        mcp_servers=meta.get("mcp_servers"),
        body=body,
    )


class AgentDefinitionLoader:
    """Loads agent definition .md files with source precedence."""

    def __init__(self):
        self._cache: Optional[Dict[str, AgentDefinition]] = None

    def _source_dirs(self) -> List[Path]:
        dirs: List[Path] = []
        user_dir = Path.home() / ".hermes" / "hermes-agent" / "agents"
        if user_dir.is_dir():
            dirs.append(user_dir)
        plugin_base = Path.home() / ".hermes" / "plugins"
        if plugin_base.is_dir():
            for p in sorted(plugin_base.iterdir()):
                ag = p / "agents"
                if ag.is_dir():
                    dirs.append(ag)
        builtin = Path(__file__).parent.parent / "agents"
        if builtin.is_dir():
            dirs.append(builtin)
        return dirs

    def load_all(self) -> Dict[str, AgentDefinition]:
        if self._cache is not None:
            return dict(self._cache)
        result: Dict[str, AgentDefinition] = {}
        for source_dir in reversed(self._source_dirs()):
            for md_file in sorted(source_dir.glob("*.md")):
                parsed = _parse_agent_file(md_file)
                if parsed is not None:
                    result[parsed.name] = parsed
        self._cache = result
        return dict(result)

    def load(self, name: str) -> Optional[AgentDefinition]:
        return self.load_all().get(name)

    def reload(self) -> None:
        self._cache = None


_loader: Optional[AgentDefinitionLoader] = None


def get_loader() -> AgentDefinitionLoader:
    global _loader
    if _loader is None:
        _loader = AgentDefinitionLoader()
    return _loader
