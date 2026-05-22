"""`cairn token issue | revoke | list` — manage MCP server bearer tokens.

Tokens live at ``~/.config/cairn/server_tokens.toml`` (mode 0600).
Issuance prints the raw token exactly once; it cannot be recovered from
the store afterwards. See ADR-0013.
"""

from __future__ import annotations

import typer

from ..auth import (
    AuthError,
    issue_token,
    load_tokens,
    revoke_token,
    token_store_path,
)
from ._common import exit_on

app = typer.Typer(
    no_args_is_help=True,
    help="Manage bearer tokens for the cairn MCP server.",
)


@app.command("issue")
def issue(
    name: str = typer.Argument(
        ...,
        help=(
            "Short principal name for this token (e.g. 'alice-laptop', "
            "'ci-bot'). Kebab-case, lowercase, max 31 chars. Becomes the "
            "request's client_id in server logs and audit trails."
        ),
    ),
    note: str | None = typer.Option(
        None,
        "--note",
        help="Optional free-form note stored alongside the token (e.g. who/why).",
    ),
) -> None:
    """Issue a new bearer token. The raw token is printed exactly once."""
    try:
        entry, raw = issue_token(name, note=note)
    except AuthError as exc:
        exit_on(exc)

    typer.echo(f"Issued token for '{entry.name}'.")
    typer.echo("")
    typer.echo("  Token (shown ONCE — copy it now):")
    typer.echo("")
    typer.echo(f"    {raw}")
    typer.echo("")
    typer.echo(
        "  Client setup — pass this to whatever runs `cairn` write commands\n"
        "  against the remote MCP server:\n"
        "\n"
        f"    export CAIRN_BEARER_TOKEN={raw}\n"
        "\n"
        "  Or persist it in ~/.config/cairn/credentials.toml keyed by endpoint URL.\n"
    )
    typer.echo(f"  Store: {token_store_path()}")


@app.command("revoke")
def revoke(
    name: str = typer.Argument(..., help="Token name to revoke."),
) -> None:
    """Revoke an active token. The store retains the entry as audit history."""
    try:
        changed = revoke_token(name)
    except AuthError as exc:
        exit_on(exc)
    if not changed:
        typer.echo(
            f"No active token named '{name}' in {token_store_path()}.",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"Revoked '{name}'.")


@app.command("list")
def list_tokens() -> None:
    """List all tokens (active and revoked). Hashes and timestamps only."""
    try:
        tokens = load_tokens()
    except AuthError as exc:
        exit_on(exc)

    if not tokens:
        typer.echo(
            f"No tokens issued (store: {token_store_path()}).\n"
            f"Issue one with: cairn token issue <name>"
        )
        return

    typer.echo(f"# Cairn MCP tokens ({token_store_path()})\n")
    name_width = max(len(t.name) for t in tokens)
    for t in sorted(tokens, key=lambda x: x.name):
        status = "revoked " if t.is_revoked else "active  "
        last = t.last_used_at.isoformat() if t.last_used_at else "(never)"
        typer.echo(
            f"  {t.name:<{name_width}}  {status}  "
            f"issued {t.created_at.isoformat()}  last-used {last}"
            + (f"  — {t.note}" if t.note else "")
        )
