"""FastMCP server exposing Tier-1 cairn tools.

Tier-1 tools (per ADR-0009):
- whoami
- status
- get_open_questions
- get_action_items
- add_decision
- add_finding
- add_action
- complete_action

Every tool takes a ``cairn`` parameter naming the target cairn (per
ADR-0010). When the registry has exactly one cairn, the parameter
defaults to that one; otherwise it's required.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from git import Repo

# Import FastMCP at module-import time — this module is only imported when
# the user actually runs `cairn mcp`, which already requires the [mcp] extra.
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from ..errors import CairnError
from ..git_ops import commit, get_user_identity
from ..ids import next_id
from ..io.frontmatter import dump as frontmatter_dump
from ..io.state_io import (
    load_actions,
    load_collaborators,
    load_questions,
    load_state,
    write_actions,
    write_collaborators,
    write_decisions,
    write_questions,
)
from ..paths import MARKER_FILE, CairnPaths
from ..registry import (
    RegisteredCairn,
    RegistryError,
    load_registry,
    resolve_single_or_named,
)
from ..schemas import ActionItem, Collaborator, Decision, FindingFrontmatter, OpenQuestion
from ..schemas.findings import FINDING_FILENAME
from ..status.render import render_json
from ..status.snapshot import build_status, state_for_branch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve(cairn: str | None) -> tuple[RegisteredCairn, CairnPaths]:
    """Resolve a cairn parameter to (registry entry, CairnPaths)."""
    entry = resolve_single_or_named(cairn)
    return entry, CairnPaths(root=entry.path)


def _validate_author(paths: CairnPaths, author: str) -> None:
    collabs = load_collaborators(paths)
    known = {c.id for c in collabs}
    if author not in known:
        suggestion = ""
        if known:
            close = ", ".join(sorted(known))
            suggestion = f" Known ids: {close}."
        raise RegistryError(
            f"unknown author '{author}' in this cairn.{suggestion} "
            f"Register a new collaborator via the `add_collaborator` MCP tool "
            f"(or `cairn collaborator add` on the host CLI)."
        )


def _suggest_collaborator_match(paths: CairnPaths, collabs: list) -> dict[str, Any]:
    """Try to suggest which existing collaborator matches the calling git user."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        git_email = result.stdout.strip() if result.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        git_email = None

    suggested_id: str | None = None
    if git_email:
        for c in collabs:
            if c.email and c.email.lower() == git_email.lower():
                suggested_id = c.id
                break
        if suggested_id is None:
            # Fall back: github handle vs local-part of the email
            local_part = git_email.split("@", 1)[0].lower()
            for c in collabs:
                if c.github and c.github.lower() == local_part:
                    suggested_id = c.id
                    break

    return {"git_email": git_email, "suggested_id": suggested_id}


def _parse_date_param(value: str | None) -> datetime:
    """Parse an optional ISO 8601 date string; default to now (UTC)."""
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistryError(
            f"invalid date '{value}': expected ISO 8601 (e.g. '2026-05-19T08:34:00Z'). {exc}"
        ) from None
    if parsed.tzinfo is None:
        # Naive datetime — assume UTC.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _validate_related(paths: CairnPaths, related: list[str]) -> None:
    if not related:
        return
    state = load_state(paths)
    id_index = state.id_index()
    bad = [r for r in related if r not in id_index]
    if bad:
        raise RegistryError(
            f"--related refers to unknown entity ids: {', '.join(bad)}"
        )


def _slugify(text: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().strip()).strip("-")
    return s[:60] or "untitled"


# ---------------------------------------------------------------------------
# FastMCP server + tool definitions
# ---------------------------------------------------------------------------


SERVER_INSTRUCTIONS = """\
Cairn maintains a research group's shared project memory that lives \
alongside the code repos, not inside any one of them, and is designed for \
a world where AI agents contribute alongside humans. It provides \
persistent shared memory of decisions, findings, open questions, and \
action items, plus richer artifacts like meeting notes, explorations, and \
registered collaborators. It supports asynchronous contribution and recall \
— collaborators (and their agents) can ask what was decided, what was \
discussed, and what's outstanding without sitting through the original \
conversation.

The user experience goal is that using cairn does not significantly \
redirect how collaborators work. They keep using their normal tools — AI \
coding agents, git, Zoom, Slack, email, paper, conversation — at their \
normal rhythm. Your role as an agent is to capture the work being done \
and act as a facilitator: surface prior context (decisions, findings, \
open questions) when it's relevant to what the user is doing now.

Like a thoughtful note-taker at a meeting, an agent may occasionally ask \
focused clarifying questions in service of accurate capture: "Is this the \
decision wording?", "Should I file this as an action — assign to whom?", \
"Looks like Q-005 is resolved; close it?" These are the questions a good \
collaborator asks. They are not the questions a tool asks: users do not \
ask the agent "please run cairn decision add --text…", do not propose \
new rituals or interrupt the user's current flow, and do not \
micro-confirm every capture inline — debrief at the end of the session \
instead.

If the project does not already have an associated cairn — or has one \
that has not yet been seeded from the project's existing git history, \
documentation, and PRs — follow the `bootstrap_from_repo` workflow before \
live capture begins.
"""


def build_server(
    *,
    auth_enabled: bool = False,
    auth_issuer: str | None = None,
    host: str | None = None,
    port: int | None = None,
    streamable_http_path: str | None = None,
) -> FastMCP:
    """Construct the FastMCP server and register the Tier-1 tools.

    With ``auth_enabled=True`` the server installs a
    :class:`CairnTokenVerifier` so HTTP requests must carry a known
    active bearer token. ``auth_issuer`` is the placeholder issuer URL
    stamped into OAuth metadata — cosmetic for the token-only flow but
    must be a syntactically valid URL.

    ``host`` / ``port`` / ``streamable_http_path`` are bind-time
    settings that live on the FastMCP constructor (not ``run()``).
    """
    fastmcp_kwargs: dict[str, Any] = {
        "name": "cairn",
        "instructions": SERVER_INSTRUCTIONS,
    }
    if host is not None:
        fastmcp_kwargs["host"] = host
    if port is not None:
        fastmcp_kwargs["port"] = port
    if streamable_http_path is not None:
        fastmcp_kwargs["streamable_http_path"] = streamable_http_path

    if auth_enabled:
        from mcp.server.auth.settings import AuthSettings
        from pydantic import AnyHttpUrl

        from .auth import CairnTokenVerifier

        issuer = auth_issuer or "http://localhost"
        fastmcp_kwargs["token_verifier"] = CairnTokenVerifier()
        fastmcp_kwargs["auth"] = AuthSettings(
            issuer_url=AnyHttpUrl(issuer),
            resource_server_url=AnyHttpUrl(issuer),
            required_scopes=["cairn:rw"],
        )

    mcp = FastMCP(**fastmcp_kwargs)

    # ---- Identity / status ------------------------------------------------

    @mcp.tool(
        description=(
            "Return the calling client's resolved identity for a cairn. Lists "
            "all registered collaborators plus, if available, a suggested "
            "match against the calling git user (resolved from git config). "
            "If the suggested match is null and your git email looks plausible, "
            "register yourself with `add_collaborator`."
        )
    )
    def whoami(cairn: str | None = None) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        collabs = load_collaborators(paths)
        suggested = _suggest_collaborator_match(paths, collabs)
        return {
            "cairn": entry.name,
            "cairn_path": str(paths.root),
            "git_email": suggested.get("git_email"),
            "suggested_id": suggested.get("suggested_id"),
            "collaborators": [
                {"id": c.id, "name": c.name, "email": c.email, "github": c.github}
                for c in collabs
            ],
        }

    @mcp.tool(description="Compact project-state summary for a cairn.")
    def status(cairn: str | None = None) -> dict[str, Any]:
        _, paths = _resolve(cairn)
        state = state_for_branch(paths, None)
        snap = build_status(paths, state, branch="current")
        import json
        result = json.loads(render_json(snap))
        # When the cairn is essentially empty, hint that the
        # bootstrap_from_repo skill may apply.
        is_empty = (
            result.get("decision_count", 0) == 0
            and result.get("open_question_count", 0) == 0
            and result.get("incomplete_action_count", 0) == 0
            and result.get("finding_count", 0) == 0
        )
        result["suggested_next"] = (
            "This cairn has no decisions, findings, questions, or actions yet. "
            "If it sits alongside an existing code repo, consider running the "
            "`bootstrap_from_repo` skill (see `list_skills` / `get_skill`) "
            "before live capture to seed the cairn from the repo's history."
            if is_empty else None
        )
        return result

    # ---- Reads ------------------------------------------------------------

    @mcp.tool(description="List open questions for a cairn.")
    def get_open_questions(
        cairn: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        _, paths = _resolve(cairn)
        questions = load_questions(paths)
        result = [q.model_dump(mode="json") for q in questions]
        if limit:
            result = result[:limit]
        return result

    @mcp.tool(
        description=(
            "List action items for a cairn, optionally filtered. "
            "`status` accepts: 'open', 'complete', 'cancelled', or omit "
            "for all. `due_before` is an ISO calendar date (YYYY-MM-DD). "
            "Note: action `status` (the completion state) is a different "
            "axis from the time-bucket breakdown surfaced in `status` "
            "(overdue / due_this_week / upcoming / no_due_date)."
        )
    )
    def get_action_items(
        cairn: str | None = None,
        assignee: str | None = None,
        status: str | None = None,
        due_before: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        _, paths = _resolve(cairn)
        actions = load_actions(paths)
        out: list[ActionItem] = []
        cutoff = date.fromisoformat(due_before) if due_before else None
        for a in actions:
            if assignee and a.assignee != assignee:
                continue
            if status and a.status != status:
                continue
            if cutoff and (a.due_date is None or a.due_date > cutoff):
                continue
            out.append(a)
        result = [a.model_dump(mode="json") for a in out]
        if limit:
            result = result[:limit]
        return result

    # ---- Writes -----------------------------------------------------------

    @mcp.tool(
        description=(
            "Record a **decision** — a commitment with rationale ('we will "
            "do X because Y'). Use when the user surfaces a choice the team "
            "has made, even tentatively. Distinct from a *finding* (an "
            "observed fact), an *open question* (uncertainty), and an "
            "*action item* (assigned pending work). Open questions are "
            "typically resolved by a decision; findings often motivate "
            "decisions. Mirrors `cairn decision add`.\n\n"
            "`related` is a list of canonical entity IDs (e.g., ['D-003', "
            "'Q-007', 'A-014']) — NOT finding paths or slugs and NOT "
            "collaborator ids. Each must resolve in the cairn or the call "
            "fails. (Findings can't yet appear in `related` — see the "
            "recommendations doc.)\n\n"
            "**For retroactive population from history**: pass `date` (ISO "
            "8601, e.g. '2025-11-15T14:30:00Z') to backdate the decision to "
            "when it was actually made; pass `source_commits` (list of "
            "SHAs) and `source_prs` (list of PR numbers / URLs) to record "
            "the git artifacts the decision came from as structured fields "
            "rather than burying them in `context`. All three are optional "
            "and default to now / empty."
        )
    )
    def add_decision(
        author: str,
        text: str,
        cairn: str | None = None,
        context: str | None = None,
        related: list[str] | None = None,
        supersedes: str | None = None,
        date: str | None = None,
        source_commits: list[str] | None = None,
        source_prs: list[str] | None = None,
    ) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        related = related or []
        _validate_author(paths, author)
        _validate_related(paths, related)
        state = load_state(paths)
        if supersedes is not None and not any(
            d.id == supersedes for d in state.decisions
        ):
            raise RegistryError(f"--supersedes refers to unknown decision: {supersedes}")

        new_id = next_id("D", state.decision_ids())
        when = _parse_date_param(date)
        try:
            new_decision = Decision.model_validate(
                {
                    "id": new_id,
                    "date": when,
                    "author": author,
                    "decision": text,
                    "context": context,
                    "related": related,
                    "supersedes": supersedes,
                    "source_commits": source_commits or [],
                    "source_prs": source_prs or [],
                }
            )
        except ValidationError as exc:
            raise RegistryError(f"schema validation failed: {exc}") from None

        decisions = list(state.decisions)
        if supersedes:
            for idx, d in enumerate(decisions):
                if d.id == supersedes:
                    decisions[idx] = d.model_copy(update={"superseded_by": new_id})
                    break
        decisions.append(new_decision)

        write_decisions(paths, decisions)
        repo = Repo(paths.root)
        sha = commit(
            repo,
            [paths.decisions_yaml],
            message=f"{new_id}: {text[:60]}",
            author=get_user_identity(repo),
        )
        return {
            "cairn": entry.name,
            "id": new_id,
            "commit_sha": sha[:12],
            "path": str(paths.decisions_yaml.relative_to(paths.root)),
        }

    @mcp.tool(
        description=(
            "Record a **finding** — an observed fact or learned constraint "
            "('we discovered X', 'turns out Y', 'measurement showed Z'). "
            "Use when the user surfaces empirical learning, not a "
            "commitment (decision), uncertainty (open question), or "
            "assignment (action item). Findings often motivate decisions. "
            "Mirrors `cairn finding add`.\n\n"
            "`related` is a list of canonical entity IDs (e.g., ['D-003', "
            "'Q-007', 'G-002']) — same constraint as add_decision. Each "
            "must resolve in the cairn.\n\n"
            "**For retroactive population from history**: pass `date` (ISO "
            "8601, e.g. '2025-11-15T14:30:00Z') to backdate to when the "
            "finding was actually observed; the finding's filename uses "
            "this date too. Pass `source_commits` and `source_prs` to "
            "record git provenance as structured fields. All three are "
            "optional and default to now / empty."
        )
    )
    def add_finding(
        author: str,
        title: str,
        cairn: str | None = None,
        body: str | None = None,
        related: list[str] | None = None,
        slug: str | None = None,
        date: str | None = None,
        source_commits: list[str] | None = None,
        source_prs: list[str] | None = None,
    ) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        related = related or []
        _validate_author(paths, author)
        _validate_related(paths, related)

        repo = Repo(paths.root)
        try:
            exploration = repo.active_branch.name
        except Exception:
            exploration = None

        final_slug = slug or _slugify(title)
        when = _parse_date_param(date)
        today = when.date().isoformat()
        filename = f"{today}-{final_slug}.md"
        if not FINDING_FILENAME.match(filename):
            raise RegistryError(
                f"resulting filename '{filename}' violates finding naming convention"
            )

        target = paths.findings / filename
        if target.exists():
            raise RegistryError(f"finding file already exists: {target.name}")

        try:
            fm = FindingFrontmatter.model_validate(
                {
                    "date": when,
                    "author": author,
                    "title": title,
                    "slug": final_slug,
                    "related": related,
                    "exploration": exploration,
                    "source_commits": source_commits or [],
                    "source_prs": source_prs or [],
                }
            )
        except ValidationError as exc:
            raise RegistryError(f"schema validation failed: {exc}") from None

        body_text = body or f"# {title}\n\nTODO: write up the finding.\n"
        if not body_text.endswith("\n"):
            body_text += "\n"
        fm_dict = fm.model_dump(mode="json", exclude_none=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(frontmatter_dump(fm_dict, body_text), encoding="utf-8")

        sha = commit(
            repo,
            [target],
            message=f"Log finding {today}-{final_slug}",
            author=get_user_identity(repo),
        )
        return {
            "cairn": entry.name,
            "slug": final_slug,
            "date": today,
            "path": str(target.relative_to(paths.root)),
            "commit_sha": sha[:12],
            "exploration": exploration,
        }

    @mcp.tool(
        description=(
            "Record an **action item** — assigned pending work, optionally "
            "with a due date ('P owes W by D'). Use when the user commits "
            "someone to a piece of follow-up work. Distinct from decisions "
            "(commitments with rationale) and findings (observed facts). "
            "Complete with `complete_action`. Mirrors `cairn action add`.\n\n"
            "`related` is a list of canonical entity IDs; same constraint "
            "as add_decision.\n\n"
            "Pass `date` (ISO 8601) to backdate the action's creation "
            "timestamp — useful when reconstructing forgotten assignments "
            "from past conversations. `due_date` is a calendar date "
            "(YYYY-MM-DD), distinct from `date`."
        )
    )
    def add_action(
        text: str,
        assignee: str,
        cairn: str | None = None,
        due_date: str | None = None,
        related: list[str] | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        related = related or []
        _validate_author(paths, assignee)
        _validate_related(paths, related)

        state = load_state(paths)
        new_id = next_id("A", state.action_ids())
        when = _parse_date_param(date)
        try:
            new_action = ActionItem.model_validate(
                {
                    "id": new_id,
                    "created": when,
                    "assignee": assignee,
                    "text": text,
                    "due_date": due_date,
                    "related": related,
                    "status": "open",
                }
            )
        except ValidationError as exc:
            raise RegistryError(f"schema validation failed: {exc}") from None

        actions = [*load_actions(paths), new_action]
        write_actions(paths, actions)
        repo = Repo(paths.root)
        sha = commit(
            repo,
            [paths.action_items_yaml],
            message=f"{new_id}: {text[:60]}",
            author=get_user_identity(repo),
        )
        return {
            "cairn": entry.name,
            "id": new_id,
            "commit_sha": sha[:12],
        }

    @mcp.tool(description="Mark an action item complete (mirrors `cairn action complete`).")
    def complete_action(
        id: str,
        by: str,
        cairn: str | None = None,
    ) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        _validate_author(paths, by)

        actions = load_actions(paths)
        target_idx = next((i for i, a in enumerate(actions) if a.id == id), None)
        if target_idx is None:
            raise RegistryError(f"unknown action id: {id}")
        action = actions[target_idx]
        if action.status == "complete":
            raise RegistryError(f"action {id} is already complete")

        now = datetime.now(timezone.utc).replace(microsecond=0)
        actions[target_idx] = action.model_copy(
            update={
                "status": "complete",
                "completed_at": now,
                "completed_by": by,
            }
        )
        write_actions(paths, actions)
        repo = Repo(paths.root)
        sha = commit(
            repo,
            [paths.action_items_yaml],
            message=f"Complete {id}",
            author=get_user_identity(repo),
        )
        return {
            "cairn": entry.name,
            "id": id,
            "commit_sha": sha[:12],
        }

    @mcp.tool(
        description=(
            "Register a new collaborator (mirrors `cairn collaborator add`). "
            "Required for any cairn the first time it's used and whenever a "
            "new contributor joins.\n\n"
            "`type` accepts:\n"
            "- `human` (default) — a person, attributed via git authorship.\n"
            "- `ai-collaborator` — a configured AI agent with its own "
            "identity (literature monitor, critique agent, etc.).\n"
            "- `group` — a named multi-person aggregate (`consensus`, "
            "`core-team`). Use when authorship is shared and no single "
            "human is the primary author.\n"
            "- `unknown` — explicit \"we don't know who authored this\" "
            "placeholder. The `bootstrap_from_repo` skill uses "
            "`id=\"repo-history\"` for findings extracted from project "
            "docs / TODO markers / commit history."
        )
    )
    def add_collaborator(
        id: str,
        name: str,
        role: str,
        cairn: str | None = None,
        type: str = "human",
        email: str | None = None,
        github: str | None = None,
        expertise: list[str] | None = None,
        current_focus: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        existing = load_collaborators(paths)
        if any(c.id == id for c in existing):
            raise RegistryError(f"collaborator id '{id}' is already in use")
        data: dict[str, Any] = {
            "id": id,
            "name": name,
            "role": role,
            "type": type,
            "email": email,
            "github": github,
            "expertise": expertise or [],
            "current_focus": current_focus,
            "notes": notes,
        }
        data = {k: v for k, v in data.items() if v not in (None, [], "")}
        try:
            new_collab = Collaborator.model_validate(data)
        except ValidationError as exc:
            raise RegistryError(f"schema validation failed: {exc}") from None
        combined = [*existing, new_collab]
        write_collaborators(paths, combined)
        repo = Repo(paths.root)
        sha = commit(
            repo,
            [paths.collaborators_yaml],
            message=f"Add collaborator '{id}'",
            author=get_user_identity(repo),
        )
        return {
            "cairn": entry.name,
            "id": id,
            "commit_sha": sha[:12],
        }

    @mcp.tool(
        description=(
            "Record an **open question** — uncertainty awaiting resolution "
            "('we don't yet know X', 'is it true that Y?', 'should we Z?'). "
            "Use when the user surfaces something to investigate or decide. "
            "Distinct from a *decision* (commitment), *finding* (observed "
            "fact), and *action item* (assigned work). Resolved with "
            "`resolve_open_question`, typically pointing at the decision "
            "that answered it.\n\n"
            "`related` is a list of canonical entity IDs; same constraint "
            "as add_decision.\n\n"
            "Pass `date` (ISO 8601) to backdate the question to when it "
            "was actually first raised — useful when reconstructing "
            "open questions from a project's TODO markers or historical "
            "discussion. Defaults to now (UTC)."
        )
    )
    def add_open_question(
        raised_by: str,
        question: str,
        cairn: str | None = None,
        related: list[str] | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        related = related or []
        _validate_author(paths, raised_by)
        _validate_related(paths, related)
        state = load_state(paths)
        new_id = next_id("Q", state.question_ids())
        when = _parse_date_param(date)
        try:
            new_q = OpenQuestion.model_validate(
                {
                    "id": new_id,
                    "raised_by": raised_by,
                    "date": when,
                    "question": question,
                    "status": "open",
                    "related": related,
                }
            )
        except ValidationError as exc:
            raise RegistryError(f"schema validation failed: {exc}") from None
        questions = [*load_questions(paths), new_q]
        write_questions(paths, questions)
        repo = Repo(paths.root)
        sha = commit(
            repo,
            [paths.open_questions_yaml],
            message=f"{new_id}: {question[:60]}",
            author=get_user_identity(repo),
        )
        return {
            "cairn": entry.name,
            "id": new_id,
            "commit_sha": sha[:12],
        }

    # ---- Project semantics (overview / current focus / related repos) -----
    #
    # These are the preferred surface for narrative project content. Each
    # operates on a single semantic section; the underlying storage (today,
    # sections of PROJECT.md; tomorrow, possibly separate files) is hidden.
    # See the get_project_md / set_project_md pair below for the escape
    # hatch.

    @mcp.tool(
        description=(
            "Return the project's one-or-two-paragraph **Overview** — what "
            "this project is about, who's on it, what stage it's in. "
            "Returns empty string if the section is missing."
        )
    )
    def get_project_overview(cairn: str | None = None) -> dict[str, Any]:
        return _get_project_section(cairn, "Overview")

    @mcp.tool(
        description=(
            "Replace the project's **Overview** section. Commits with "
            "attribution. Use read-modify-write: call get_project_overview "
            "first if you need the existing prose; otherwise just write."
        )
    )
    def set_project_overview(
        author: str,
        content: str,
        cairn: str | None = None,
    ) -> dict[str, Any]:
        return _set_project_section(cairn, "Overview", content, author)

    @mcp.tool(
        description=(
            "Return the **Current focus** section — 2 to 4 items describing "
            "what the team is actively working on now. This is the section "
            "that changes most often. Empty string if missing."
        )
    )
    def get_current_focus(cairn: str | None = None) -> dict[str, Any]:
        return _get_project_section(cairn, "Current focus")

    @mcp.tool(
        description=(
            "Replace the project's **Current focus** section. Use when the "
            "team's active work has shifted. Higher change cadence than "
            "Overview, so prefer narrow updates here over editing the whole "
            "project narrative."
        )
    )
    def set_current_focus(
        author: str,
        content: str,
        cairn: str | None = None,
    ) -> dict[str, Any]:
        return _set_project_section(cairn, "Current focus", content, author)

    @mcp.tool(
        description=(
            "Return the **Related repositories** section — a free-form "
            "markdown list of the project's working code / data / paper "
            "repos. Structured cross-references (decisions/findings → "
            "commit SHA) are future work; for now this is prose."
        )
    )
    def get_related_repositories(cairn: str | None = None) -> dict[str, Any]:
        return _get_project_section(cairn, "Related repositories")

    @mcp.tool(
        description=(
            "Replace the **Related repositories** section. Use when a new "
            "code/data/paper repo joins the project or an existing one "
            "moves."
        )
    )
    def set_related_repositories(
        author: str,
        content: str,
        cairn: str | None = None,
    ) -> dict[str, Any]:
        return _set_project_section(cairn, "Related repositories", content, author)

    # ---- Escape hatch: full PROJECT.md ------------------------------------

    @mcp.tool(
        description=(
            "Escape hatch: return the cairn's PROJECT.md content verbatim. "
            "Prefer the semantic tools (get_project_overview, "
            "get_current_focus, get_related_repositories) — they hide the "
            "storage shape and won't break if PROJECT.md is later split "
            "into multiple files. Use this only when you need the whole "
            "document, e.g., for human review."
        )
    )
    def get_project_md(cairn: str | None = None) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        if not paths.project_md.is_file():
            return {"cairn": entry.name, "exists": False, "content": ""}
        return {
            "cairn": entry.name,
            "exists": True,
            "content": paths.project_md.read_text(encoding="utf-8"),
        }

    @mcp.tool(
        description=(
            "Escape hatch: overwrite the cairn's PROJECT.md verbatim. "
            "**Prefer the semantic setters** (set_project_overview, "
            "set_current_focus, set_related_repositories) for normal "
            "edits — they preserve sections you don't intend to touch and "
            "won't surface here once PROJECT.md is split into files. Use "
            "this only when you need to rewrite the whole document at once."
        )
    )
    def set_project_md(
        author: str,
        content: str,
        cairn: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        _validate_author(paths, author)
        if not content.endswith("\n"):
            content += "\n"
        paths.project_md.write_text(content, encoding="utf-8")
        repo = Repo(paths.root)
        sha = commit(
            repo,
            [paths.project_md],
            message=message or "Update PROJECT.md",
            author=get_user_identity(repo),
        )
        return {
            "cairn": entry.name,
            "path": str(paths.project_md.relative_to(paths.root)),
            "commit_sha": sha[:12],
        }

    # ---- Helpers shared by the semantic project tools ---------------------

    def _get_project_section(cairn_name: str | None, section: str) -> dict[str, Any]:
        from .. import project_md as pm

        entry, paths = _resolve(cairn_name)
        if not paths.project_md.is_file():
            return {"cairn": entry.name, "section": section, "exists": False, "content": ""}
        text = paths.project_md.read_text(encoding="utf-8")
        content = pm.get_section(text, section)
        return {
            "cairn": entry.name,
            "section": section,
            "exists": content is not None,
            "content": content or "",
        }

    def _set_project_section(
        cairn_name: str | None, section: str, content: str, author: str
    ) -> dict[str, Any]:
        from .. import project_md as pm

        entry, paths = _resolve(cairn_name)
        _validate_author(paths, author)
        existing = (
            paths.project_md.read_text(encoding="utf-8")
            if paths.project_md.is_file()
            else ""
        )
        if not existing:
            # Seed a minimal PROJECT.md with the H1 from the cairn name.
            existing = f"# {entry.name}\n\n"
        updated = pm.set_section(existing, section, content)
        paths.project_md.write_text(updated, encoding="utf-8")
        repo = Repo(paths.root)
        sha = commit(
            repo,
            [paths.project_md],
            message=f"Update {section} in PROJECT.md",
            author=get_user_identity(repo),
        )
        return {
            "cairn": entry.name,
            "section": section,
            "commit_sha": sha[:12],
        }

    # ---- Lookup reads (closes the related-ID loop) ------------------------

    @mcp.tool(description="List collaborators for a cairn.")
    def list_collaborators(cairn: str | None = None) -> list[dict[str, Any]]:
        _, paths = _resolve(cairn)
        return [c.model_dump(mode="json") for c in load_collaborators(paths)]

    @mcp.tool(
        description=(
            "List decisions, optionally filtered. Newest first. Useful for "
            "populating `related: ['D-NNN']` on a new write without parsing "
            "files. Pass `query` for substring match on the decision text."
        )
    )
    def list_decisions(
        cairn: str | None = None,
        author: str | None = None,
        query: str | None = None,
        limit: int | None = 20,
    ) -> list[dict[str, Any]]:
        _, paths = _resolve(cairn)
        from ..io.state_io import load_decisions

        decisions = list(reversed(load_decisions(paths)))
        if author:
            decisions = [d for d in decisions if d.author == author]
        if query:
            q = query.lower()
            decisions = [
                d for d in decisions
                if q in d.decision.lower() or (d.context and q in d.context.lower())
            ]
        if limit:
            decisions = decisions[:limit]
        return [d.model_dump(mode="json") for d in decisions]

    @mcp.tool(description="Fetch a single decision by ID.")
    def get_decision(id: str, cairn: str | None = None) -> dict[str, Any]:
        _, paths = _resolve(cairn)
        from ..io.state_io import load_decisions

        for d in load_decisions(paths):
            if d.id == id:
                return d.model_dump(mode="json")
        raise RegistryError(f"no decision with id '{id}'")

    @mcp.tool(
        description=(
            "List findings (summaries only — call get_finding for full body). "
            "Newest first by date+slug. Useful for surfacing recent findings "
            "or building cross-references."
        )
    )
    def list_findings(
        cairn: str | None = None,
        since: str | None = None,
        limit: int | None = 20,
    ) -> list[dict[str, Any]]:
        _, paths = _resolve(cairn)
        from ..io import frontmatter as fm

        if not paths.findings.is_dir():
            return []
        cutoff = date.fromisoformat(since) if since else None
        out: list[dict[str, Any]] = []
        for child in paths.findings.iterdir():
            if not child.is_file() or child.suffix != ".md" or child.name == ".gitkeep":
                continue
            m = FINDING_FILENAME.match(child.name)
            if not m:
                continue
            try:
                data, _ = fm.load(child)
            except (ValueError, OSError):
                continue
            d_str = m.group("date")
            if cutoff:
                try:
                    if date.fromisoformat(d_str) < cutoff:
                        continue
                except ValueError:
                    continue
            out.append(
                {
                    "slug": m.group("slug"),
                    "date": d_str,
                    "title": data.get("title") if isinstance(data, dict) else None,
                    "author": data.get("author") if isinstance(data, dict) else None,
                    "related": data.get("related", []) if isinstance(data, dict) else [],
                    "exploration": data.get("exploration") if isinstance(data, dict) else None,
                    "path": str(child.relative_to(paths.root)),
                }
            )
        out.sort(key=lambda r: (r["date"], r["slug"]), reverse=True)
        if limit:
            out = out[:limit]
        return out

    @mcp.tool(
        description=(
            "Fetch a single finding (frontmatter + full markdown body) "
            "by slug or path."
        )
    )
    def get_finding(
        cairn: str | None = None,
        slug: str | None = None,
        date: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        _, paths = _resolve(cairn)
        from ..io import frontmatter as fm

        target = None
        if path:
            target = paths.root / path
        elif slug:
            # If date given, exact match; else pick the most recent with that slug.
            candidates = [
                p for p in paths.findings.iterdir()
                if p.is_file() and p.name.endswith(f"-{slug}.md")
            ]
            if date:
                candidates = [p for p in candidates if p.name.startswith(date + "-")]
            if not candidates:
                raise RegistryError(
                    f"no finding with slug '{slug}'"
                    + (f" on {date}" if date else "")
                )
            target = max(candidates, key=lambda p: p.name)
        else:
            raise RegistryError("get_finding requires either `slug` or `path`")
        if not target.is_file():
            raise RegistryError(f"finding not found at {target}")
        data, body = fm.load(target)
        return {
            "path": str(target.relative_to(paths.root)),
            "frontmatter": data,
            "body": body,
        }

    @mcp.tool(
        description=(
            "Mark an open question as answered (mirrors a CLI command not yet "
            "shipped). `answered_by` is a related entity id — typically the "
            "decision that resolved the question (D-NNN), but may be a "
            "finding's slug or another question."
        )
    )
    def resolve_open_question(
        id: str,
        answered_by: str,
        actor: str,
        cairn: str | None = None,
    ) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        _validate_author(paths, actor)
        state = load_state(paths)
        if answered_by not in state.id_index():
            raise RegistryError(f"answered_by refers to unknown entity: {answered_by}")
        questions = load_questions(paths)
        target_idx = next((i for i, q in enumerate(questions) if q.id == id), None)
        if target_idx is None:
            raise RegistryError(f"unknown question id: {id}")
        questions[target_idx] = questions[target_idx].model_copy(
            update={"status": "answered", "answered_by": answered_by}
        )
        from ..io.state_io import write_questions
        write_questions(paths, questions)
        repo = Repo(paths.root)
        sha = commit(
            repo,
            [paths.open_questions_yaml],
            message=f"{id}: answered by {answered_by}",
            author=get_user_identity(repo),
        )
        return {"cairn": entry.name, "id": id, "commit_sha": sha[:12]}

    # ---- Exploration tools (move up from Tier 2) --------------------------

    @mcp.tool(
        description=(
            "Open a cairn exploration — a tracked alternative line of inquiry. "
            "Materialized as a git branch <as_id>/<slug> in the cairn plus a "
            "manifest under explorations/. Use when the user wants the "
            "RATIONALE of an alternative tracked (design alternative, "
            "methodology choice). For ordinary git branching in a project "
            "repo, the user should use git directly — this is a cairn-level "
            "concept."
        )
    )
    def start_exploration(
        description: str,
        as_id: str,
        cairn: str | None = None,
    ) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        _validate_author(paths, as_id)
        # Reuse the CLI helpers — they already handle slug, manifest, commits.
        from ..cli.exploration_cmd import (
            _append_to_explorations_readme,
            _explorations_index_entry,
            _kebab,
            _manifest_body,
        )

        try:
            slug = _kebab(description)
        except ValueError as exc:
            raise RegistryError(str(exc)) from None

        branch_name = f"{as_id}/{slug}"
        repo = Repo(paths.root)
        if branch_name in [h.name for h in repo.heads]:
            raise RegistryError(
                f"exploration '{branch_name}' already exists (git branch present)"
            )

        today = datetime.now(timezone.utc).date().isoformat()
        manifest_path = paths.explorations / as_id / f"{slug}.md"
        # Update explorations/README.md on the current branch
        line = _explorations_index_entry(branch_name, as_id, today, description)
        _append_to_explorations_readme(paths.explorations / "README.md", line)
        commit(
            repo,
            [paths.explorations / "README.md"],
            message=f"Open exploration {branch_name}",
            author=get_user_identity(repo),
        )
        # Create the branch + manifest commit
        new_head = repo.create_head(branch_name)
        new_head.checkout()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            _manifest_body(branch_name, as_id, today, description), encoding="utf-8"
        )
        sha = commit(
            repo,
            [manifest_path],
            message=f"{branch_name}: open exploration manifest",
            author=get_user_identity(repo),
        )
        return {
            "cairn": entry.name,
            "name": branch_name,
            "branch_name": branch_name,
            "merge_state": "active",
            "manifest": str(manifest_path.relative_to(paths.root)),
            "commit_sha": sha[:12],
        }

    @mcp.tool(
        description=(
            "Close a cairn exploration (mirrors `cairn exploration close`). "
            "status must be 'merged' or 'abandoned'. For 'merged', the "
            "exploration's git branch must be an ancestor of main."
        )
    )
    def close_exploration(
        name: str,
        status: str,
        reason: str,
        closed_by: str,
        cairn: str | None = None,
    ) -> dict[str, Any]:
        entry, paths = _resolve(cairn)
        _validate_author(paths, closed_by)
        status = status.lower()
        if status not in {"merged", "abandoned"}:
            raise RegistryError("status must be 'merged' or 'abandoned'")
        if not reason.strip():
            raise RegistryError("reason must not be empty")
        from ..cli.exploration_cmd import (
            _closure_block,
            _is_merged_into_main,
            _move_explorations_readme_row,
            _read_manifest,
            _split_exploration_name,
        )

        owner, slug = _split_exploration_name(name)
        repo = Repo(paths.root)
        if name not in [h.name for h in repo.heads]:
            raise RegistryError(f"exploration '{name}' does not exist locally")
        is_merged, main_name = _is_merged_into_main(repo, name)
        if main_name is None:
            raise RegistryError("could not locate 'main' or 'master' in the cairn")
        if status == "merged" and not is_merged:
            raise RegistryError(
                f"'{name}' is not an ancestor of '{main_name}'; merge first"
            )
        # Switch to main first
        repo.git.checkout(main_name)
        manifest_rel = f"explorations/{owner}/{slug}.md"
        manifest_text = _read_manifest(repo, name, manifest_rel)
        if manifest_text is None:
            raise RegistryError(f"no manifest found for '{name}'")
        closed_at = datetime.now(timezone.utc).date().isoformat()
        merge_sha = repo.commit(name).hexsha[:12] if status == "merged" else None
        closure = _closure_block(status, closed_at, closed_by, reason.strip(), merge_sha)
        updated_manifest = manifest_text.rstrip("\n") + "\n" + closure
        manifest_path = paths.root / manifest_rel
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(updated_manifest, encoding="utf-8")
        readme_path = paths.explorations / "README.md"
        _move_explorations_readme_row(
            readme_path, name, owner, closed_at, status, reason.strip()
        )
        sha = commit(
            repo,
            [readme_path, manifest_path],
            message=f"Close exploration {name} ({status}): {reason.strip()[:60]}",
            author=get_user_identity(repo),
        )
        return {
            "cairn": entry.name,
            "name": name,
            "status": status,
            "merge_sha": merge_sha,
            "commit_sha": sha[:12],
        }

    # ---- Skills discoverability -------------------------------------------

    @mcp.tool(
        description=(
            "List the procedural skills shipped in this cairn (under "
            "`skills/<name>/SKILL.md`). Each skill is a markdown procedure "
            "the agent should follow when its trigger fires — for example, "
            "`bootstrap_from_repo` (seed a cairn from an existing code "
            "repo), `orient` (read PROJECT.md + status at session start), "
            "`debrief` (end-of-session batch capture)."
        )
    )
    def list_skills(cairn: str | None = None) -> list[dict[str, Any]]:
        _, paths = _resolve(cairn)
        out: list[dict[str, Any]] = []
        skills_dir = paths.skills
        if not skills_dir.is_dir():
            return out
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            text = skill_md.read_text(encoding="utf-8")
            description = _extract_skill_description(text)
            out.append({"name": child.name, "description": description})
        return out

    @mcp.tool(
        description=(
            "Return a skill's full procedural prose by name. Pair with "
            "list_skills to discover what's available. Reading a skill "
            "before triggering its workflow gives the agent guidance on "
            "ordering, what fields to populate, and which other tools to "
            "compose."
        )
    )
    def get_skill(name: str, cairn: str | None = None) -> dict[str, Any]:
        _, paths = _resolve(cairn)
        skill_md = paths.skills / name / "SKILL.md"
        if not skill_md.is_file():
            raise RegistryError(
                f"no skill named '{name}' (looked for {skill_md}). "
                f"Use `list_skills` to see what's available."
            )
        text = skill_md.read_text(encoding="utf-8")
        return {
            "name": name,
            "description": _extract_skill_description(text),
            "content": text,
        }

    return mcp


def _extract_skill_description(text: str) -> str | None:
    """Pull the `description:` field from a SKILL.md's YAML frontmatter, if any."""
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end < 0:
        return None
    frontmatter = text[3:end]
    # Cheap line scan — avoid pulling in yaml here.
    for line in frontmatter.splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


def _ensure_registry_loadable() -> None:
    """Validate that the registry file (if present) is parseable at startup."""
    try:
        load_registry()
    except RegistryError as exc:
        # Re-raise as a generic exception so the CLI launcher can surface it.
        raise RuntimeError(str(exc)) from None


def run() -> None:
    """Entry point for `cairn mcp`. Runs the server over stdio."""
    _ensure_registry_loadable()
    server = build_server()
    server.run()  # stdio is FastMCP's default


# Silence unused-import linting from helpers imported only for re-export.
_ = (CairnError, MARKER_FILE)
