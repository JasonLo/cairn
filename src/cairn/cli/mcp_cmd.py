"""`cairn mcp` — run the MCP server over stdio or HTTP.

See ADR-0009, ADR-0010, and ADR-0012. Requires the ``[mcp]`` install extra.
"""

from __future__ import annotations

from pathlib import Path

import typer

_VALID_TRANSPORTS = ("stdio", "streamable-http", "sse")
_VALID_AUTH_MODES = ("none", "token")
_HTTP_TRANSPORTS = ("streamable-http", "sse")


def mcp(
    cairn_path: Path | None = typer.Option(
        None,
        "--cairn-path",
        help=(
            "Convenience: register a single ad-hoc cairn at this path (in addition "
            "to any cairns in the user-level registry) before starting the server. "
            "The name defaults to the directory basename. Useful for one-off runs "
            "and CI; for normal use, register cairns with `cairn register`."
        ),
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help=(
            "Name to use for the ad-hoc cairn registered via --cairn-path. "
            "Defaults to the directory basename (minus a `-cairn` suffix)."
        ),
    ),
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help=(
            "Transport to use: 'stdio' (default, for claude mcp add), "
            "'streamable-http' (long-running HTTP server), or 'sse' (SSE HTTP). "
            "stdio keeps the same trust surface as before. "
            "HTTP is opt-in; default binding is 127.0.0.1 (safe for single-user). "
            "Binding 0.0.0.0 expands the trust surface — use a reverse proxy "
            "with auth for group deployments."
        ),
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Host to bind for HTTP transports (default: 127.0.0.1).",
    ),
    port: int = typer.Option(
        8765,
        "--port",
        help="Port to listen on for HTTP transports (default: 8765).",
    ),
    path: str = typer.Option(
        "/mcp",
        "--path",
        help="URL path for the MCP endpoint in HTTP transports (default: /mcp).",
    ),
    auth: str = typer.Option(
        "none",
        "--auth",
        help=(
            "Bearer-token gate for HTTP transports. "
            "'none' (default) — no server-side auth; suitable for stdio or for "
            "127.0.0.1 single-user setups. "
            "'token' — require Authorization: Bearer <token> against tokens "
            "issued by `cairn token issue`. Only valid with --transport "
            "streamable-http or sse."
        ),
    ),
    auth_issuer: str | None = typer.Option(
        None,
        "--auth-issuer",
        help=(
            "Issuer URL stamped into OAuth metadata when --auth token is set. "
            "Defaults to http://<host>:<port>. Cosmetic for the bearer-token "
            "flow but must be a syntactically valid URL."
        ),
    ),
) -> None:
    """Run the MCP server over stdio (default) or HTTP."""
    if transport not in _VALID_TRANSPORTS:
        typer.echo(
            f"error: invalid --transport '{transport}'. "
            f"Valid values: {', '.join(_VALID_TRANSPORTS)}.",
            err=True,
        )
        raise typer.Exit(code=1)

    if auth not in _VALID_AUTH_MODES:
        typer.echo(
            f"error: invalid --auth '{auth}'. "
            f"Valid values: {', '.join(_VALID_AUTH_MODES)}.",
            err=True,
        )
        raise typer.Exit(code=1)

    if auth == "token" and transport not in _HTTP_TRANSPORTS:
        typer.echo(
            "error: --auth token requires --transport streamable-http or sse. "
            "(Stdio has no network surface to authenticate.)",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        from ..mcp.server import _ensure_registry_loadable, build_server
    except ImportError as exc:
        typer.echo(
            f"error: MCP server requires the [mcp] extra. "
            f"Install with: pip install 'cairn[mcp]'\n"
            f"(import error: {exc})",
            err=True,
        )
        raise typer.Exit(code=1) from None

    if cairn_path is not None:
        # Register the ad-hoc cairn in-process (not persisted to disk).
        from ..mcp import server as server_module
        from ..registry import (
            RegisteredCairn,
            RegistryError,
            load_registry,
            validate_name,
        )

        resolved = cairn_path.expanduser().resolve()
        ad_hoc_name = name or _default_name_for(resolved)
        try:
            validate_name(ad_hoc_name)
        except RegistryError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from None

        persisted = load_registry()
        ad_hoc = RegisteredCairn(name=ad_hoc_name, path=resolved)
        combined = [*persisted, ad_hoc]

        # Patch load_registry so build_server sees the combined list.
        def patched_load_registry(*args, **kwargs):  # type: ignore[no-untyped-def]
            return combined

        import cairn.registry as registry_mod

        server_module.load_registry = patched_load_registry  # type: ignore[assignment]
        registry_mod.load_registry = patched_load_registry  # type: ignore[assignment]

    _ensure_registry_loadable()

    auth_enabled = auth == "token"
    if auth_enabled:
        # Surface an explicit error if no tokens are loadable — otherwise the
        # server would happily come up rejecting every request.
        from ..auth import AuthError, load_tokens, token_store_path
        from ._common import exit_on

        try:
            tokens = load_tokens()
        except AuthError as exc:
            exit_on(exc)
        active = [t for t in tokens if not t.is_revoked]
        if not active:
            typer.echo(
                f"error: --auth token requires at least one active token in "
                f"{token_store_path()}. Issue one with: cairn token issue <name>",
                err=True,
            )
            raise typer.Exit(code=1)

    issuer = auth_issuer or f"http://{host}:{port}"
    is_http = transport in _HTTP_TRANSPORTS
    server = build_server(
        auth_enabled=auth_enabled,
        auth_issuer=issuer,
        host=host if is_http else None,
        port=port if is_http else None,
        streamable_http_path=path if transport == "streamable-http" else None,
    )

    if transport == "stdio":
        server.run()
    elif transport == "streamable-http":
        server.run(transport="streamable-http")
    else:  # sse
        server.run(transport="sse")


def _default_name_for(p: Path) -> str:
    """Derive a default cairn name from a directory path."""
    base = p.name
    if base.endswith("-cairn"):
        base = base[: -len("-cairn")]
    import re
    base = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-")
    return base or "cairn"
