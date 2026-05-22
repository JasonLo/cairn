"""User-level registry of cairns served by the MCP server.

The registry maps a short *cairn name* (used as an MCP API parameter) to a
filesystem path. It lives at ``~/.config/cairn/server.toml`` (or under
``$XDG_CONFIG_HOME/cairn/server.toml`` when set) and is a small TOML file:

    [cairns]
    stellaforge = "/Users/cranmer/projects/stellaforge-cairn"
    nanogpt     = "/Users/cranmer/projects/nanogpt-cairn"

The registry exists only when the user runs an MCP server. Direct-CLI users
(no MCP) never need to touch it. See ADR-0010.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover — exercised on 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]

from .errors import CairnError
from .paths import is_cairn_root

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
"""Kebab-case identifier — reused by the token store (see ``cairn.auth``)."""


def cairn_config_dir() -> Path:
    """Resolve ``~/.config/cairn/`` (or its XDG override).

    Shared by the registry and the bearer-token store so both files
    land in the same directory.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "cairn"


@dataclass(frozen=True)
class RegisteredCairn:
    name: str
    path: Path


class RegistryError(CairnError):
    pass


def registry_path() -> Path:
    """Resolve the registry file location, honoring XDG_CONFIG_HOME."""
    return cairn_config_dir() / "server.toml"


def load_registry(path: Path | None = None) -> list[RegisteredCairn]:
    """Return the registered cairns. Empty list if the file doesn't exist."""
    p = path or registry_path()
    if not p.is_file():
        return []
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(
            f"registry file {p} is not valid TOML: {exc}"
        ) from None
    cairns_section = data.get("cairns", {})
    if not isinstance(cairns_section, dict):
        raise RegistryError(
            f"registry file {p}: expected a [cairns] table"
        )
    out: list[RegisteredCairn] = []
    for name, raw_path in sorted(cairns_section.items()):
        if not isinstance(raw_path, str):
            raise RegistryError(
                f"registry file {p}: value for '{name}' must be a string path"
            )
        out.append(RegisteredCairn(name=name, path=Path(raw_path).expanduser()))
    return out


def _format_registry(cairns: list[RegisteredCairn]) -> str:
    lines = [
        "# Cairn MCP server registry — managed by `cairn register` / `cairn unregister`.",
        "# One entry per cairn that the MCP server should serve.",
        "",
        "[cairns]",
    ]
    for c in sorted(cairns, key=lambda x: x.name):
        # Use forward slashes for portability and basic TOML string escaping.
        path_str = str(c.path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{c.name} = "{path_str}"')
    lines.append("")
    return "\n".join(lines)


def save_registry(cairns: list[RegisteredCairn], path: Path | None = None) -> None:
    """Write the registry file, creating parent dirs as needed."""
    p = path or registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_format_registry(cairns), encoding="utf-8")


def validate_name(name: str) -> None:
    if not NAME_PATTERN.match(name):
        raise RegistryError(
            f"invalid cairn name '{name}': must match {NAME_PATTERN.pattern} "
            f"(kebab-case, lowercase, starts with a letter, max 31 chars)"
        )


def register(name: str, path: Path, *, registry: Path | None = None) -> None:
    """Add or update an entry in the registry."""
    validate_name(name)
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise RegistryError(f"path is not a directory: {resolved}")
    if not is_cairn_root(resolved):
        raise RegistryError(
            f"path {resolved} is not a cairn root "
            f"(missing .cairn marker and state/collaborators.yaml)"
        )
    existing = load_registry(registry)
    updated = [c for c in existing if c.name != name]
    updated.append(RegisteredCairn(name=name, path=resolved))
    save_registry(updated, registry)


def unregister(name: str, *, registry: Path | None = None) -> bool:
    """Remove an entry from the registry. Returns True iff something was removed."""
    existing = load_registry(registry)
    updated = [c for c in existing if c.name != name]
    if len(updated) == len(existing):
        return False
    save_registry(updated, registry)
    return True


def lookup(name: str, *, registry: Path | None = None) -> RegisteredCairn | None:
    for c in load_registry(registry):
        if c.name == name:
            return c
    return None


def resolve_single_or_named(
    name: str | None, *, registry: Path | None = None
) -> RegisteredCairn:
    """Resolve the cairn for a tool call.

    Single-cairn convenience: if ``name`` is None and the registry has exactly
    one entry, return it. If the registry has zero or more-than-one entries
    and ``name`` is None, raise a clear error listing the options.
    """
    cairns = load_registry(registry)
    if name is not None:
        match = next((c for c in cairns if c.name == name), None)
        if match is None:
            registered_names = ", ".join(c.name for c in cairns) or "(none)"
            raise RegistryError(
                f"no cairn named '{name}' registered. Known: {registered_names}. "
                f"Add one with `cairn register <name> <path>`."
            )
        return match
    if len(cairns) == 1:
        return cairns[0]
    if not cairns:
        raise RegistryError(
            "no cairns registered. Run `cairn register <name> <path>` to add one, "
            "or pass `cairn` explicitly to this tool."
        )
    names = ", ".join(c.name for c in cairns)
    raise RegistryError(
        f"multiple cairns registered ({names}); pass `cairn` explicitly to disambiguate."
    )
