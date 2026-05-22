"""Tests for US-P-14 — bearer-token auth for the MCP HTTP server.

Covers:
- the token store (issue / revoke / list / verify) at the auth.py level
- the `cairn token` CLI roundtrip
- the `cairn mcp --auth ...` flag validation
- the CairnTokenVerifier wired into FastMCP (requires the [mcp] extra)
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cairn.auth import (
    AuthError,
    StoredToken,
    generate_token,
    hash_token,
    issue_token,
    load_tokens,
    revoke_token,
    token_store_path,
    verify_token,
)
from cairn.cli.app import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ~/.config/cairn/* to tmp_path for every test."""
    cfg = tmp_path / "xdg"
    cfg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    return cfg


# ---------------------------------------------------------------------------
# auth.py — token store unit tests
# ---------------------------------------------------------------------------


def test_token_store_path_honors_xdg(xdg: Path) -> None:
    expected = xdg / "cairn" / "server_tokens.toml"
    assert token_store_path() == expected


def test_generated_tokens_are_prefixed_and_high_entropy() -> None:
    a = generate_token()
    b = generate_token()
    assert a.startswith("cairn_") and b.startswith("cairn_")
    assert a != b
    # urlsafe_b64(32 bytes) ≈ 43 chars; plus prefix.
    assert len(a) >= len("cairn_") + 40


def test_hash_token_is_deterministic_sha256_hex() -> None:
    raw = "cairn_abcdef"
    h = hash_token(raw)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
    assert h == hash_token(raw)
    assert h != hash_token("cairn_abcdeg")


def test_load_tokens_empty_when_file_missing(xdg: Path) -> None:
    assert load_tokens() == []


def test_issue_token_writes_hashed_entry_mode_0600(xdg: Path) -> None:
    entry, raw = issue_token("alice-laptop", note="primary")
    assert isinstance(entry, StoredToken)
    assert entry.name == "alice-laptop"
    assert entry.token_sha256 == hash_token(raw)
    assert entry.is_revoked is False
    assert entry.note == "primary"

    store = token_store_path()
    assert store.is_file()
    # The raw token must never land in the file on disk.
    content = store.read_text(encoding="utf-8")
    assert raw not in content
    assert entry.token_sha256 in content

    # POSIX mode check — skip on non-POSIX where the chmod is best-effort.
    if os.name == "posix":
        mode = stat.S_IMODE(store.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_issue_token_rejects_invalid_name(xdg: Path) -> None:
    with pytest.raises(AuthError) as exc:
        issue_token("Invalid Name")
    assert "invalid token name" in str(exc.value)


def test_issue_token_refuses_overwrite_of_active_name(xdg: Path) -> None:
    issue_token("alice")
    with pytest.raises(AuthError) as exc:
        issue_token("alice")
    assert "already exists" in str(exc.value)
    assert "revoke" in str(exc.value).lower()


def test_issue_after_revoke_replaces_entry(xdg: Path) -> None:
    _, _ = issue_token("alice")
    revoke_token("alice")
    entry, raw = issue_token("alice")
    assert entry.is_revoked is False
    # The new active row matches the new raw token.
    assert verify_token(raw) is not None


def test_revoke_token_marks_revoked_and_invalidates(xdg: Path) -> None:
    _, raw = issue_token("alice")
    assert verify_token(raw) is not None

    changed = revoke_token("alice")
    assert changed is True
    # Entry is retained, but verify rejects it.
    tokens = load_tokens()
    assert len(tokens) == 1
    assert tokens[0].is_revoked is True
    assert verify_token(raw) is None


def test_revoke_token_returns_false_for_unknown_name(xdg: Path) -> None:
    assert revoke_token("nobody") is False


def test_verify_token_rejects_empty_and_garbage(xdg: Path) -> None:
    issue_token("alice")
    assert verify_token("") is None
    assert verify_token("not-a-real-token") is None


def test_verify_token_does_not_touch_by_default(xdg: Path) -> None:
    _, raw = issue_token("alice")
    assert load_tokens()[0].last_used_at is None
    match = verify_token(raw)
    assert match is not None
    assert load_tokens()[0].last_used_at is None


def test_verify_token_touches_when_requested(xdg: Path) -> None:
    _, raw = issue_token("alice")
    assert load_tokens()[0].last_used_at is None
    match = verify_token(raw, touch=True)
    assert match is not None
    assert match.last_used_at is not None
    assert load_tokens()[0].last_used_at is not None


def test_load_tokens_rejects_malformed_hash(xdg: Path, tmp_path: Path) -> None:
    store = token_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        '[tokens.bad]\ntoken_sha256 = "tooshort"\ncreated_at = "2026-05-21T00:00:00+00:00"\n',
        encoding="utf-8",
    )
    with pytest.raises(AuthError) as exc:
        load_tokens()
    assert "token_sha256" in str(exc.value)


# ---------------------------------------------------------------------------
# `cairn token` CLI roundtrip
# ---------------------------------------------------------------------------


def test_token_issue_prints_raw_token_once_and_persists_hash(xdg: Path) -> None:
    res = runner.invoke(app, ["token", "issue", "alice-laptop"], catch_exceptions=False)
    assert res.exit_code == 0
    assert "Issued token for 'alice-laptop'" in res.output
    # Pull the printed token out of the output and verify it round-trips.
    tokens_printed = [
        word for word in res.output.split() if word.startswith("cairn_")
    ]
    assert tokens_printed, f"no cairn_-prefixed token in output: {res.output!r}"
    raw = tokens_printed[0]
    assert verify_token(raw) is not None


def test_token_list_reports_active_and_revoked(xdg: Path) -> None:
    issue_token("alice")
    issue_token("bob")
    revoke_token("bob")

    res = runner.invoke(app, ["token", "list"], catch_exceptions=False)
    assert res.exit_code == 0
    assert "alice" in res.output
    assert "bob" in res.output
    assert "active" in res.output
    assert "revoked" in res.output
    # Never leak hashes or raw tokens.
    assert "cairn_" not in res.output
    assert hash_token("anything") not in res.output  # not literal of course


def test_token_revoke_unknown_exits_nonzero(xdg: Path) -> None:
    res = runner.invoke(app, ["token", "revoke", "ghost"])
    assert res.exit_code != 0
    assert "No active token" in res.output


def test_token_list_empty_message(xdg: Path) -> None:
    res = runner.invoke(app, ["token", "list"], catch_exceptions=False)
    assert res.exit_code == 0
    assert "No tokens issued" in res.output


# ---------------------------------------------------------------------------
# `cairn mcp --auth` flag validation
# ---------------------------------------------------------------------------


def test_mcp_auth_invalid_value_rejected(xdg: Path) -> None:
    res = runner.invoke(app, ["mcp", "--auth", "bogus"], catch_exceptions=False)
    assert res.exit_code != 0
    assert "invalid --auth" in res.output


def test_mcp_auth_token_requires_http_transport(xdg: Path) -> None:
    res = runner.invoke(
        app, ["mcp", "--auth", "token", "--transport", "stdio"],
        catch_exceptions=False,
    )
    assert res.exit_code != 0
    assert "requires --transport streamable-http or sse" in res.output


def test_mcp_auth_token_requires_at_least_one_active_token(xdg: Path) -> None:
    # This guard runs after build_server() is imported, so the [mcp] extra
    # must be present — without it the import error message wins instead.
    pytest.importorskip("mcp.server.fastmcp")
    res = runner.invoke(
        app,
        ["mcp", "--auth", "token", "--transport", "streamable-http"],
        catch_exceptions=False,
    )
    assert res.exit_code != 0
    assert "at least one active token" in res.output
    assert "cairn token issue" in res.output


# ---------------------------------------------------------------------------
# FastMCP verifier wiring (requires the [mcp] extra — each test guards itself
# so the pure-auth tests above still run in CI without the extra).
# ---------------------------------------------------------------------------


def test_cairn_token_verifier_accepts_valid_and_rejects_invalid(xdg: Path) -> None:
    pytest.importorskip("mcp.server.fastmcp")
    from cairn.mcp.auth import CairnTokenVerifier

    _, raw = issue_token("alice")
    verifier = CairnTokenVerifier()

    access = asyncio.run(verifier.verify_token(raw))
    assert access is not None
    assert access.client_id == "alice"
    assert "cairn:rw" in access.scopes

    miss = asyncio.run(verifier.verify_token("cairn_not-a-real-token"))
    assert miss is None


def test_cairn_token_verifier_rejects_revoked(xdg: Path) -> None:
    pytest.importorskip("mcp.server.fastmcp")
    from cairn.mcp.auth import CairnTokenVerifier

    _, raw = issue_token("alice")
    revoke_token("alice")
    verifier = CairnTokenVerifier()
    assert asyncio.run(verifier.verify_token(raw)) is None


def _capture_fastmcp_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace FastMCP with a fake that records constructor kwargs.

    The fake's ``.tool`` decorator is a no-op so ``build_server`` finishes
    registering tools without invoking real FastMCP internals.
    """
    from cairn.mcp import server as server_module

    captured: dict[str, object] = {}

    class FakeFastMCP:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def tool(self, *_args, **_kwargs):
            return lambda fn: fn

    monkeypatch.setattr(server_module, "FastMCP", FakeFastMCP)
    return captured


def test_build_server_with_auth_enabled_passes_verifier_to_fastmcp(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_server(auth_enabled=True) must hand FastMCP a CairnTokenVerifier."""
    pytest.importorskip("mcp.server.fastmcp")
    from cairn.mcp import server as server_module
    from cairn.mcp.auth import CairnTokenVerifier

    issue_token("alice")
    captured = _capture_fastmcp_kwargs(monkeypatch)
    server_module.build_server(auth_enabled=True, auth_issuer="http://localhost:8765")

    assert isinstance(captured.get("token_verifier"), CairnTokenVerifier)
    assert captured.get("auth") is not None


def test_build_server_default_passes_no_verifier_to_fastmcp(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp.server.fastmcp")
    from cairn.mcp import server as server_module

    captured = _capture_fastmcp_kwargs(monkeypatch)
    server_module.build_server()

    assert "token_verifier" not in captured
    assert "auth" not in captured
