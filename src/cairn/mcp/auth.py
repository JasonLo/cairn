"""FastMCP ``TokenVerifier`` backed by the Cairn token store.

Token storage and hashing live in :mod:`cairn.auth`; this module is the
thin glue that turns a presented bearer header into an MCP
``AccessToken``. See ADR-0013.
"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path

from mcp.server.auth.provider import AccessToken, TokenVerifier

from ..auth import verify_token


class CairnTokenVerifier(TokenVerifier):
    """Bearer-token verifier backed by ``cairn.auth``'s token store."""

    def __init__(self, *, path: Path | None = None, scope: str = "cairn:rw") -> None:
        self._path = path
        self._scope = scope

    async def verify_token(self, token: str) -> AccessToken | None:
        match = verify_token(token, path=self._path, touch=True)
        if match is None:
            return None

        expires_at: int | None = None
        if match.revoked_at is not None:
            # Defensive: verify_token already filters revoked, but if a race
            # ever resurrected one, expose the timestamp as already-elapsed.
            expires_at = int(
                match.revoked_at.astimezone(timezone.utc).timestamp()
            )
        return AccessToken(
            token=token,
            client_id=match.name,
            scopes=[self._scope],
            expires_at=expires_at,
        )
