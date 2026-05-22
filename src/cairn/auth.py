"""Bearer-token store for the Cairn MCP HTTP server.

Tokens live in ``~/.config/cairn/server_tokens.toml`` (mode 0600),
hashed at rest. Public API: :func:`issue_token`, :func:`revoke_token`,
:func:`verify_token`, :func:`load_tokens`, :func:`token_store_path`.

Importable without the ``[mcp]`` extra so the ``cairn token`` CLI
works on a bare install. See ADR-0013 for design rationale.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover — exercised on 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]

from .errors import CairnError
from .registry import NAME_PATTERN, cairn_config_dir

TOKEN_PREFIX = "cairn_"
TOKEN_RANDOM_BYTES = 32  # 256 bits


@dataclass(frozen=True)
class StoredToken:
    """A single entry in the token store."""

    name: str
    token_sha256: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    note: str | None = None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class AuthError(CairnError):
    """Token-store errors (parse, validation, IO)."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def token_store_path() -> Path:
    """Resolve the token-store file location.

    Shares ``cairn_config_dir()`` with the registry so operators have a
    single place to look for server-side state.
    """
    return cairn_config_dir() / "server_tokens.toml"


# ---------------------------------------------------------------------------
# Token generation + hashing
# ---------------------------------------------------------------------------


def generate_token() -> str:
    """Return a new high-entropy bearer token (URL-safe, prefixed)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_RANDOM_BYTES)


def hash_token(token: str) -> str:
    """Return the sha256 hex digest of a token (canonical store form)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_name(name: str) -> None:
    """Reject names that wouldn't round-trip through the TOML table key."""
    if not NAME_PATTERN.match(name):
        raise AuthError(
            f"invalid token name '{name}': must match {NAME_PATTERN.pattern} "
            f"(kebab-case, lowercase, starts with a letter, max 31 chars)"
        )


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def _parse_dt(value: object, *, field: str, name: str) -> datetime:
    if isinstance(value, datetime):
        # tomllib returns naive datetimes for unqualified RFC3339 — assume UTC.
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AuthError(
                f"token '{name}': invalid {field} value '{value}'"
            ) from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise AuthError(f"token '{name}': {field} must be an ISO 8601 string")


def load_tokens(path: Path | None = None) -> list[StoredToken]:
    """Return all stored tokens (revoked included). Empty if file is absent."""
    p = path or token_store_path()
    if not p.is_file():
        return []
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise AuthError(f"token store {p} is not valid TOML: {exc}") from None

    tokens_section = data.get("tokens", {})
    if not isinstance(tokens_section, dict):
        raise AuthError(f"token store {p}: expected a [tokens] table")

    out: list[StoredToken] = []
    for name, entry in sorted(tokens_section.items()):
        if not isinstance(entry, dict):
            raise AuthError(
                f"token store {p}: entry for '{name}' must be a table"
            )
        try:
            validate_name(name)
        except AuthError as exc:
            raise AuthError(f"token store {p}: {exc}") from None

        hex_digest = entry.get("token_sha256")
        if not isinstance(hex_digest, str) or len(hex_digest) != 64:
            raise AuthError(
                f"token store {p}: '{name}' missing or malformed token_sha256"
            )

        created_raw = entry.get("created_at")
        if created_raw is None:
            raise AuthError(f"token store {p}: '{name}' missing created_at")
        created_at = _parse_dt(created_raw, field="created_at", name=name)

        last_used = entry.get("last_used_at")
        last_used_at = (
            _parse_dt(last_used, field="last_used_at", name=name)
            if last_used is not None
            else None
        )

        revoked = entry.get("revoked_at")
        revoked_at = (
            _parse_dt(revoked, field="revoked_at", name=name)
            if revoked is not None
            else None
        )

        note = entry.get("note")
        if note is not None and not isinstance(note, str):
            raise AuthError(f"token store {p}: '{name}' note must be a string")

        out.append(
            StoredToken(
                name=name,
                token_sha256=hex_digest.lower(),
                created_at=created_at,
                last_used_at=last_used_at,
                revoked_at=revoked_at,
                note=note,
            )
        )
    return out


def _escape_toml_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _format_tokens(tokens: list[StoredToken]) -> str:
    lines = [
        "# Cairn MCP bearer-token store — managed by `cairn token issue/revoke`.",
        "# Tokens are stored only as sha256 hashes; raw tokens are shown once at issuance.",
        "# This file MUST be mode 0600 (read/write owner only).",
        "",
    ]
    for t in sorted(tokens, key=lambda x: x.name):
        lines.append(f"[tokens.{t.name}]")
        lines.append(f'token_sha256 = "{t.token_sha256}"')
        lines.append(f'created_at = "{t.created_at.isoformat()}"')
        if t.last_used_at is not None:
            lines.append(f'last_used_at = "{t.last_used_at.isoformat()}"')
        if t.revoked_at is not None:
            lines.append(f'revoked_at = "{t.revoked_at.isoformat()}"')
        if t.note is not None:
            lines.append(f'note = "{_escape_toml_string(t.note)}"')
        lines.append("")
    return "\n".join(lines)


def save_tokens(tokens: list[StoredToken], path: Path | None = None) -> None:
    """Write the token store, enforcing mode 0600 on the file."""
    p = path or token_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    content = _format_tokens(tokens)

    # Write atomically via a temp file, then chmod and rename so the file
    # never exists publicly-readable between create and chmod.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    with contextlib.suppress(OSError):  # non-POSIX systems may not honor chmod
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Mutation helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def issue_token(
    name: str,
    *,
    note: str | None = None,
    path: Path | None = None,
) -> tuple[StoredToken, str]:
    """Issue a new token for ``name``.

    Returns ``(stored_entry, raw_token)``. The raw token is the only
    chance to capture the secret — it is not stored on disk and cannot
    be recovered later.

    Raises ``AuthError`` if a non-revoked token already exists for
    ``name``. To rotate, revoke first.
    """
    validate_name(name)
    tokens = load_tokens(path)
    for t in tokens:
        if t.name == name and not t.is_revoked:
            raise AuthError(
                f"token '{name}' already exists and is active. "
                f"Revoke it first with `cairn token revoke {name}`."
            )

    raw = generate_token()
    entry = StoredToken(
        name=name,
        token_sha256=hash_token(raw),
        created_at=_now(),
        note=note,
    )
    # Replace any prior revoked entry for the same name so the active row
    # is unique by name (revocation history is captured in commit history,
    # not in the store itself).
    updated = [t for t in tokens if t.name != name]
    updated.append(entry)
    save_tokens(updated, path)
    return entry, raw


def revoke_token(name: str, *, path: Path | None = None) -> bool:
    """Mark the token named ``name`` as revoked.

    Returns ``True`` if something changed (i.e. an active entry was
    revoked); ``False`` if no active entry exists for that name.
    """
    tokens = load_tokens(path)
    changed = False
    updated: list[StoredToken] = []
    for t in tokens:
        if t.name == name and not t.is_revoked:
            updated.append(replace(t, revoked_at=_now()))
            changed = True
        else:
            updated.append(t)
    if changed:
        save_tokens(updated, path)
    return changed


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_token(
    token: str, *, path: Path | None = None, touch: bool = False
) -> StoredToken | None:
    """Return the matching active ``StoredToken``, or ``None`` if invalid.

    Comparison is constant-time on the hex digest. Revoked entries do
    not match.

    When ``touch=True`` and the token matches, ``last_used_at`` is set
    to now and the store is written back in the same pass (one read +
    one write).  The touch write is best-effort: if it fails the
    successful match is still returned.  Callers wanting pure-read
    verification (tests, dry-runs) leave ``touch=False``.
    """
    if not token:
        return None
    presented = hash_token(token)
    try:
        tokens = load_tokens(path)
    except AuthError:
        return None

    match_idx: int | None = None
    for i, t in enumerate(tokens):
        if t.is_revoked:
            continue
        if secrets.compare_digest(t.token_sha256, presented):
            match_idx = i
            break
    if match_idx is None:
        return None

    if not touch:
        return tokens[match_idx]

    touched = replace(tokens[match_idx], last_used_at=_now())
    tokens[match_idx] = touched
    with contextlib.suppress(OSError):  # observability only — never block on touch
        save_tokens(tokens, path)
    return touched
