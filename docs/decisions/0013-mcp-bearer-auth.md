# ADR-0013 — MCP server bearer-token authentication

**Status:** accepted
**Date:** 2026-05-21
**Supersedes:** nothing
**Related:** ADR-0009 (MCP v0, stdio), ADR-0010 (one server, many cairns), ADR-0012
(MCP HTTP transport, remote CLI dispatch)

---

## Context

ADR-0012 shipped HTTP transport for the MCP server but explicitly deferred built-in
auth: *"operators wanting auth must front the server with a reverse proxy. Future
ADR binds caller identity to the `author` claim once the deployment pattern is
known."*  The client side (`src/cairn/mcp/remote.py`) was already wired for it —
it sends `Authorization: Bearer <token>` from `CAIRN_BEARER_TOKEN` or
`~/.config/cairn/credentials.toml`, and maps HTTP 401/403 to `RemoteAuthError`.
The lock on the door was simply absent.

In practice the reverse-proxy-only stance is too high a barrier for the common
"shared cairn on a group box" deployment ADR-0012 named as the motivating case.
A user who wants to host one cairn for their group should not need to learn
nginx + certbot + auth_request before turning on the server.  Meanwhile, every
new transport (US-P-11's `0.0.0.0` HTTP, future websocket / SSE-to-LAN) widens
the unauthenticated surface.

This ADR closes the smallest useful slice of that gap.

## Decision

### Scope

In:

- A native server-side bearer-token gate on `cairn mcp --transport
  {streamable-http,sse}`.
- A token store local to the operator's machine, hashed at rest.
- A CLI surface (`cairn token issue | revoke | list`) for managing the store.
- A `--auth {none,token}` flag on `cairn mcp`, defaulting to `none` so existing
  setups continue to work unchanged.

Explicitly out:

- **Token → author binding.**  The verified token's principal name is recorded
  as the request's `client_id` (visible in server logs / future audit views)
  but is **not** mapped to the `author` parameter of write tools.  Attribution
  remains the `author` claim, validated against `state/collaborators.yaml`.
  Binding the two is a follow-up ADR once a concrete deployment surfaces the
  right mapping policy.
- **OAuth 2.0 / CIMD / DCR.**  Required for Claude connector directory entries
  per `claude.com/docs/connectors/building/authentication`; not required for
  group-internal deployments, which is the only target deployment shape today.
  Revisit when there is a concrete reason to be listed in the directory.
- **TLS termination.**  Out of scope for the binary itself.  Operators putting
  the server on a public address still front it with a reverse proxy or
  WireGuard / Tailscale.  The bearer gate is a defence-in-depth measure that
  composes with — not a replacement for — transport encryption.

### Token store

Location: `~/.config/cairn/server_tokens.toml` (XDG-aware), file mode `0600`,
sibling to the existing registry file from ADR-0010.

Schema (per token):

```toml
[tokens.<name>]
token_sha256 = "<64-char hex>"
created_at   = "2026-05-21T17:30:00+00:00"
last_used_at = "2026-05-21T18:05:11+00:00"   # optional, updated on hit
revoked_at   = "2026-05-21T19:00:00+00:00"   # optional; set ⇒ verifier refuses
note         = "free-form, optional"
```

**Tokens are stored only as their sha256 hash.**  The raw token is shown to the
operator exactly once — on `cairn token issue` — and is unrecoverable thereafter.
A stolen store file leaks token *names* and metadata, not live credentials.

**Revocation is a flag.**  `revoked_at` set ⇒ the entry stays in the store but
the verifier refuses any token whose hash matches it.  The audit trail outlives
the credential.

### Token format

`cairn_<urlsafe-base64-of-32-random-bytes>` (≈ 256 bits of entropy).  The prefix
is a soft hint for operators eyeballing logs; the prefix is not parsed by the
verifier.

### Verifier

`src/cairn/mcp/auth.py` exposes `CairnTokenVerifier`, implementing FastMCP's
`mcp.server.auth.provider.TokenVerifier` protocol.  On hit it returns an
`AccessToken` with `client_id = <token name>` and `scopes = ["cairn:rw"]`.
On miss (no match, or matched a revoked entry) it returns `None`, which
FastMCP translates to HTTP 401.

The verifier touches `last_used_at` on hit, best-effort (silent on IO error).

### CLI surface

`cairn token issue <name> [--note ...]`
  Issues a new token.  Refuses to overwrite an active entry — operators rotate
  by revoking first.  Prints the raw token and an env-var setup hint.

`cairn token revoke <name>`
  Sets `revoked_at`.  Exits non-zero if no active entry exists.

`cairn token list`
  Lists names, status, timestamps, and notes.  Never prints hashes (no value)
  or raw tokens (impossible).

### Server flag

`cairn mcp --auth {none,token}` (default `none`).  When `token`:

- The transport must be `streamable-http` or `sse` (stdio has no network
  surface to authenticate).
- The token store must contain at least one active token (else fail-fast at
  startup, with the path of the store and the issuance command).
- The server is built with `token_verifier=CairnTokenVerifier()` and an
  `AuthSettings` whose `issuer_url` defaults to `http://<host>:<port>` and is
  overridable via `--auth-issuer`.

### Trust model

| Mode | Default binding | Auth | Trust surface |
|---|---|---|---|
| `--transport stdio` | n/a | n/a | Same process / user (unchanged) |
| `--transport streamable-http --auth none` | 127.0.0.1 | none | Single user on host |
| `--transport streamable-http --auth token` | 127.0.0.1 | bearer | Anyone with a live token (composes with TLS / reverse proxy / VPN) |

`--auth none` over a non-loopback bind remains a footgun, but is still allowed
— matching ADR-0012's stance that bind-address policy is named in the help
text rather than enforced.

---

## Consequences

- **Existing setups unchanged.**  `cairn mcp` over stdio, and `cairn mcp
  --transport streamable-http` without `--auth`, behave identically to before.
- **The client side already speaks this.**  `remote.py` has been sending the
  bearer header since US-P-13; turning on `--auth token` activates the lock the
  client was already opening.
- **Attribution gap stands.**  A request can present token `alice-laptop` and
  claim `author = bob` — the server will record the write as bob's.  This is
  intentional under "scope out" above.  The follow-up ADR will bind the two
  once deployment shape decides whether tokens map 1:1 to collaborator ids,
  N:1, or M:N.
- **No token issuance to remote clients.**  Operators distribute tokens
  out-of-band (Signal, password manager, lossless 1:1 channel of choice).
  Matches the existing `~/.config/cairn/credentials.toml` resolution shape on
  the client side.
- **Audit history lives in the store + git.**  The store retains revoked
  entries; commits to the store are not made automatically (the store lives
  outside any cairn).  A future ADR may push token events into a per-host
  audit log if real deployments need it.

## Triggers for revisiting

- A concrete shared-cairn deployment hits write-attribution ambiguity → ADR
  binding `client_id` → `state/collaborators.yaml` id.
- A request to list the server in the Claude connector directory → OAuth (CIMD)
  ADR layered on top of this one.
- Repeated rotation friction (operators reissuing every N days) → expirable
  tokens with `expires_at` set at issuance, surfaced by the verifier.
- An audit requirement that survives store corruption → per-host append-only
  audit log.
