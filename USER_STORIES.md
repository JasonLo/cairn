# Cairn — User Stories

A working set of user stories spanning the three primary integration points: the **`cairn` Python package** (scaffolding and management), **agents using skills** to interact with a local cairn, and **MCP server queries** from chatbots and other agents.

Each story includes acceptance criteria that can serve directly as the basis for tests. The criteria are written to be specific enough to translate into pytest cases (for the Python package), end-to-end skill invocations (for agent stories), and MCP tool call assertions (for server stories).

---

## How to read these

**ID convention.** Stories are grouped by surface:
- **US-P-NN** — Python package
- **US-A-NN** — Agent / skill
- **US-M-NN** — MCP server / chatbot

**Story format.** Each story names an *Actor* (the person or agent initiating the action), a one-sentence *Story* in standard "As a / I want / so that" form, and an *Expected behavior* block of testable bullet points.

**Cross-cutting expectations** (attribution, queue discipline, source-of-truth properties) are listed at the end and apply to many stories. They're worth treating as invariants rather than per-story criteria.

---

## §1 — Python Tool Stories

The `cairn` Python package is the canonical tooling for creating and managing cairns. It provides a CLI for common operations and a Python API for programmatic use. All stories below should work both as CLI invocations and as library calls.

### US-P-01: Initialize a new cairn

**Actor**: Research group lead starting a new project
**Story**: As a PI starting a new project, I want to scaffold a cairn from scratch so my group can begin using it immediately.

**Expected behavior**
- Running `cairn init <project-name>` in an empty or new directory creates the full repository structure: `state/`, `knowledge/{meetings,findings,literature,provenance}/`, `skills/`, `explorations/`, `README.md`, `PROJECT.md`, `.gitignore`.
- All state YAML files (`decisions.yaml`, `open_questions.yaml`, `action_items.yaml`, `goals.yaml`, `collaborators.yaml`) exist as empty but schema-valid documents.
- `PROJECT.md` has a working orientation template with the project name interpolated and clear TODO markers for the parts a human should fill in.
- The directory is initialized as a git repository with an initial commit attributed to the invoking user.
- `cairn init` errors rather than overwriting if files already exist; `--force` is the explicit escape hatch.

### US-P-02: Initialize from a template

**Actor**: Research group lead
**Story**: As a PI, I want to scaffold from an existing template so I can reuse a structure my group has previously validated.

**Expected behavior**
- `cairn init --template <path-or-url>` accepts either a local path or a cookiecutter-style URL.
- Falls back to the canonical default template if no template flag is provided.
- Template variables (`project_name`, `pi_name`, `github_org`, etc.) are prompted interactively when not supplied as flags.
- A run with all variables supplied as flags is fully non-interactive (suitable for CI).

### US-P-03: Add a collaborator

**Actor**: PI, or a collaborator adding themselves
**Story**: As someone joining a project, I want to register myself in the cairn so my contributions are attributed.

**Expected behavior**
- `cairn collaborator add --id maria --name "Maria Santos" --role postdoc` creates a valid entry in `state/collaborators.yaml`.
- The `id` field must be unique within the cairn; collisions raise a clear error.
- Required fields (`id`, `name`, `role`) are enforced; optional fields (`expertise`, `github`, `recent_papers`, `notes`) are accepted but not required.
- The command can also accept a YAML file via `--yaml <file>` or stdin for bulk additions.
- The change is staged as a git commit; the committer is the invoking user.

### US-P-04: Record a decision

**Actor**: Anyone (human via CLI, or agent via Python API)
**Story**: As a participant in the project, I want to record a decision so the group has a canonical record.

**Expected behavior**
- `cairn decision add --author kyle --text "Use stratified resampling" --context "..." --related Q-007` creates a valid entry in `state/decisions.yaml`.
- The ID is auto-generated as `D-NNN` (incremented from existing IDs in the file).
- A timestamp (UTC ISO 8601) is recorded automatically.
- `--related` accepts a list of IDs (`Q-NN`, `D-NN`); the command verifies each refers to an existing entity and errors clearly otherwise.
- The author must be a known collaborator in `state/collaborators.yaml`; unknown authors produce a clear error.
- `--supersedes D-NN` marks an existing decision as superseded; the old decision is not deleted, it gets a `superseded_by` back-reference.

### US-P-05: Validate a cairn

**Actor**: Anyone making changes to a cairn (including in CI)
**Story**: As someone modifying a cairn, I want to confirm I haven't broken its structure.

**Expected behavior**
- `cairn validate` checks all state files and required directories and reports any violations.
- Specific checks performed: required directories exist; YAML files parse; each entry matches its schema; cross-references (`related`, `assignee`, `supersedes`) point to existing entities; meeting filenames match the `YYYY-MM-DD.md` convention.
- Exit code is non-zero on errors, zero on success (suitable for `pre-commit` and CI).
- Output reports all errors found, grouped by file, with enough context to fix (file path, entity ID, what's wrong).
- A `--strict` flag adds soft warnings (orphaned questions, decisions without authors).

### US-P-06: Project status snapshot

**Actor**: PI or collaborator checking in on the project
**Story**: As someone working on the project, I want a compact human-readable summary of where things stand.

**Expected behavior**
- `cairn status` writes a summary to stdout: count of open questions, count of incomplete action items (broken down by overdue / due this week / upcoming), active branches with their owners, recent decisions (last 5), date of most recent meeting.
- Output is brief enough to scan quickly (≤30 lines for typical projects).
- `--json` flag emits structured JSON for programmatic consumption by other tools.
- `--branch <name>` restricts the status to a specific branch's view.

### US-P-07: Import a meeting summary

**Actor**: Anyone who just held a meeting
**Story**: As someone running a meeting, I want to drop a Zoom AI summary (or similar) into the cairn without manual reformatting.

**Expected behavior**
- `cairn meeting import --from zoom <file>` parses Zoom AI's text or JSON export format.
- Creates `knowledge/meetings/<YYYY-MM-DD>.md` with front matter (attendees, date, duration, source) followed by the transcript or summary body.
- Speaker labels are matched against `state/collaborators.yaml`; unmapped speakers raise a warning but don't fail the import.
- Any decisions or action items inferred by the source are written to a separate staging file (e.g., `knowledge/meetings/<date>-staged.yaml`) for human review before they enter canonical state.
- An `--interactive` flag walks the user through promoting staged items into `state/`.

### US-P-08: Export an artifact

**Actor**: A group preparing a paper, dataset release, or similar external deliverable
**Story**: As a group ready to publish, we want to export part of the cairn as an ASTRA or RO-Crate artifact for external sharing.

**Expected behavior**
- `cairn artifact export --format <astra|ara|ro-crate> --output knowledge/provenance/<name>` creates a directory of artifacts in the chosen format.
- Accepts a `--scope` argument defining what to include: a branch name, a date range, a set of finding IDs, or a list of paths.
- The output passes validation against the chosen specification's schema (or the closest thing it has).
- Cross-references back to the source cairn (commit SHAs, finding IDs, decision IDs) are preserved so the artifact can be traced to its provenance.
- For early development, generating stub artifacts that require human enrichment is acceptable; the *structure* must be valid even if the content is incomplete.

### US-P-09: Plan an agenda

**Actor**: PI preparing for a group meeting
**Story**: As the meeting organizer, I want a draft agenda based on what's accumulated since the last meeting.

**Expected behavior**
- `cairn agenda draft --since <date|last-meeting>` writes a draft agenda to stdout (or `--output <file>`).
- Includes: items explicitly flagged for the next meeting, branches needing review, open questions raised since the cutoff, action items due before the next meeting, recent findings worth presenting.
- The draft is a plain markdown file with section headers; not committed automatically.
- Each agenda item carries a reference back to its source (`Q-012`, `branch:kyle/alt-loss`, etc.).

### US-P-10: Initialize a cairn for an existing project

**Actor**: A contributor to an ongoing project that already has one or more code / data / paper repos, deciding to add a cairn for group coordination going forward.

**Story**: As a contributor to an existing project, I want to spin up a cairn alongside our existing repos so the team can start using it for shared memory without having to migrate or restructure what we already have.

**Expected behavior**
- `cairn init <project-name>` (or AGENT-BOOTSTRAP) can be run from any directory; the cairn is created as a *new* git repo, **not inside** any existing project repo. A reasonable place is `~/projects/<project-name>-cairn/`, a sibling of the user's other project repos. The CLI does not enforce this — it just refuses to overwrite an existing target directory without `--force`.
- The bootstrapping agent, when it already has context from being in a Claude Code session inside one of the project's working repos, can pre-populate `PROJECT.md`'s "Overview", "Current focus", and "Related repositories" sections from that context (the project repo's top-level `README.md`, recent git log, contributors file). The user reviews and approves before the cairn's initial commit; the agent does not auto-commit pre-populated content.
- The cairn's `PROJECT.md` includes a **Related repositories** section listing the project's existing code/data/paper repos with a one-line description per repo. Format is free-form markdown; structured cross-referencing (decisions → repo + commit SHA, findings → file in a repo) is future work and out of scope for this story.
- `cairn init` does not modify any existing project repo. Bootstrap is purely additive.
- Pairing a project repo with a cairn — so agents working inside the project repo can discover which cairn it belongs to — is a separate, **opt-in** action the user takes after bootstrap (e.g., a `cairn link <project-repo>` command), never a side effect of `cairn init`. When the user explicitly invokes it, Cairn may write a single small pointer file (e.g., `cairn.toml`) at the project repo root recording the cairn's location (local path, or in future an MCP endpoint). This is the only circumstance under which Cairn writes into a project repo, and it is always user-initiated.
- The first collaborator registration (Step 5 of AGENT-BOOTSTRAP) and the rest of the standard bootstrap flow are unchanged.

---

### US-P-11: Serve a cairn (or many) over HTTP

**Actor**: Operator hosting an MCP server for themselves or a group.

**Story**: As an operator, I want to expose the MCP server over HTTP so multiple Claude Code sessions and background agents can share one running server.

**Expected behavior**
- `cairn mcp --transport {stdio,streamable-http,sse}` selects the transport; default stays `stdio` so existing setups continue unchanged. HTTP is strictly opt-in.
- HTTP mode accepts `--host` (default `127.0.0.1`), `--port` (default `8765`), `--path` (default `/mcp`).
- The tool surface (Tier-1/2/3 from ADR-0009) is identical across transports — same names, parameters, return shapes.
- Default binding `127.0.0.1` keeps the trust surface the same as stdio. Binding `0.0.0.0` is allowed but the help text names the tradeoff.
- A typo'd `--transport` value fails fast with a CLI error before any SDK import.

---

### US-P-12: Pair a project repo with a remote cairn

**Actor**: Collaborator on a project whose cairn is served by an HTTP MCP node off-machine.

**Story**: As a user with a project repo and a remote MCP server's URL, I want to record the pairing in the project repo so my agent (and my CLI) can reach the right cairn without re-configuring it.

**Expected behavior**
- `cairn link --endpoint <url> --name <cairn>` writes a remote-mode `cairn.toml` and prints client-neutral pairing info (endpoint + cairn name + the credential setup hints).
- `cairn.toml` supports three explicit modes: local-path (`path` only), local-registry (`name` only), remote-MCP (`endpoint + name`). The dead "endpoint alone" shape is explicitly rejected.
- `cairn link --endpoint <url>` probes the URL once and reports reachability before writing. A `--no-probe` escape hatch exists for offline pairing.
- Pairing travels with the repo. Credentials do not.

---

### US-P-13: Write to a remote cairn from the CLI

**Actor**: Human collaborator working in a project repo paired with a remote cairn, without an MCP-capable agent attached.

**Story**: As a contributor, I want `cairn decision add`, `cairn finding add`, `cairn action add`, and `cairn action complete` to work against a remote-mode project repo the same way they work locally. Remote mode must not silently strip humans of the file-shaped interface, breaking the substrate-as-specification commitment.

**Expected behavior**
- In a project repo with a remote-mode `cairn.toml`, the four Tier-1 write commands transparently dispatch over HTTP. No CLI shape changes.
- Credentials: `CAIRN_BEARER_TOKEN` env var first; then `~/.config/cairn/credentials.toml` keyed by endpoint URL (mode `0600` enforced on write). Never in `cairn.toml`, never committed.
- **Read-after-write confirmation (m13v)**: each remote write echoes the server-resolved cairn name and new entity ID back to the CLI (e.g., `"Recorded D-001 in cairn 'my-cairn' at https://…"`). This guards against a wrong `name` in `cairn.toml` silently writing to the wrong cairn — the server's resolved handle is shown, not the local pointer's string.
- Failures are CLI-shaped. Missing token: credential setup hint. HTTP 401/403: "authentication failed". Network unreachable: distinct from remote validation errors.
- Reads are out of scope for this slice; `cairn status` against a remote-mode repo continues to error.

---

### US-P-14: Gate the HTTP MCP server on a bearer token

**Actor**: Operator hosting an MCP server for themselves or a group, reachable beyond loopback or shared across collaborators on the same machine.

**Story**: As an operator, I want to require a bearer token on `cairn mcp` HTTP requests so I can host one server for my group without standing up a reverse-proxy stack just for auth.

**Expected behavior**
- `cairn mcp --auth {none,token}` selects the gate; default `none` so existing setups are unchanged. `--auth token` requires `--transport streamable-http` or `sse` (stdio rejects the combination with an actionable CLI error).
- `cairn token issue <name> [--note ...]` mints a new bearer token, prints it exactly once, and stores only its sha256 hash at `~/.config/cairn/server_tokens.toml` (mode `0600`). Re-issuing for an active name fails — operators must `cairn token revoke <name>` first.
- `cairn token revoke <name>` flips a `revoked_at` timestamp; the entry stays in the store for audit, the verifier refuses any token whose hash matches it.
- `cairn token list` enumerates names, status (active/revoked), `created_at`, `last_used_at`, and notes. Never prints hashes or raw tokens.
- The server fails fast at startup if `--auth token` is set and no active tokens exist, naming the store path and the issuance command.
- Tokens are 32 random bytes, urlsafe-base64-encoded, prefixed `cairn_`. Comparison is constant-time on the hex digest.
- `verify_token` accepts a presented bearer, returns the matching `StoredToken` or `None`. The verifier sets `client_id = <name>` and `scopes = ["cairn:rw"]` on the resulting `AccessToken`.
- **Scope boundary (deliberate)**: the verified principal name is **not** bound to the write tools' `author` parameter. Attribution remains the `author` claim, validated against `state/collaborators.yaml`. Binding is deferred to a follow-up ADR. See ADR-0013.

---

## §2 — Agent / Skill Stories

These stories cover agents (typically Claude Code, but the patterns generalize) interacting with a local cairn through skills. Each skill is a `SKILL.md` file in the cairn's `skills/` directory or in an agent's globally-installed skills.

### US-A-01: Orient at session start

**Actor**: Claude Code agent in a cairn directory
**Story**: As an agent starting a session in a cairn, I want to load enough context to be useful immediately without consuming excess context.

**Expected behavior**
- The agent reads `PROJECT.md` as its first step (one short file, fast).
- It reads `state/collaborators.yaml` to identify the user it's talking to (via local git config or the invoking user's identity).
- When asked "what's going on with this project?", it produces a coherent summary citing specific recent decisions and open questions.
- It does NOT load the full knowledge base, all meeting transcripts, or all findings into context up front; it loads them on demand.
- Time to first response on "what's going on?" is well under a minute on a cairn with a year of history.

### US-A-02: Log a finding mid-session

**Actor**: Claude Code agent
**Story**: As an agent helping a user during a working session, I want to log a finding the user has discovered, without breaking flow.

**Expected behavior**
- A `log-finding` skill is triggered when the user expresses something they want recorded ("we just learned that..."; "remember this finding...").
- The agent writes a new file to `knowledge/findings/<YYYY-MM-DD>-<short-slug>.md` with proper front matter (date, author, related references).
- The author field is set to the current user (not "claude" or "agent" — the human owns the finding).
- If the user is on a branch, the finding lands on that branch.
- The agent stages a git commit but defaults to asking for user confirmation before committing (configurable: an autocommit mode is acceptable for advanced users).

### US-A-03: Create an exploration

**Actor**: Claude Code agent
**Story**: As an agent helping a user explore an alternative approach, I want to create a exploration the group can review later without disrupting main.

**Expected behavior**
- A `start-exploration` skill creates a git branch in the cairn named `<user-id>/<short-description>` (kebab-case, derived from the user's stated goal).
- Adds an entry to `explorations/README.md` describing the exploration's purpose, owner, and date opened.
- The branch's first commit is a "exploration manifest" file (e.g., `explorations/<exploration-name>.md`) recording the proposed line of inquiry and its initial rationale.
- The user is left on the new branch with their session intact.
- If an exploration with the same name already exists, the agent prompts for resolution rather than silently overwriting.

### US-A-04: Mark an action item complete

**Actor**: Claude Code agent
**Story**: As an agent helping a user finish a task, I want to mark the corresponding action item complete.

**Expected behavior**
- A `complete-action` skill takes an action ID (resolvable from a short description if unique) and updates `state/action_items.yaml`.
- The completed action keeps its history: status changes to `complete`, completion timestamp and completer are recorded; the entry is NOT deleted.
- If completion implies a follow-up (a finding, a decision, a related question being resolved), the agent prompts the user to capture it.
- The commit message references the action ID for traceability.

### US-A-05: Search prior discussions

**Actor**: Claude Code agent answering a user's question
**Story**: As an agent, I want to find related prior context in the cairn so my answers are grounded, without dumping everything into the prompt.

**Expected behavior**
- A `search-history` skill (or local file scans guided by `PROJECT.md`) finds matching excerpts across meetings, findings, and decisions.
- For cairns without an MCP server, the skill uses local file reads and heuristics (filename matching, header scanning, tagged content) — no embeddings or external services required.
- Returns chunks with their source path, date, and author so the agent can cite them appropriately.
- Results are scoped to the current branch's view of the cairn.

### US-A-06: AI collaborator contribution

**Actor**: Scheduled AI literature monitor (autonomous agent)
**Story**: As a literature monitor running on a schedule, I want to add relevant new papers without polluting main.

**Expected behavior**
- The agent writes to its dedicated branch (e.g., `lit-monitor/2026-05-W3`), never to main.
- Each addition is a file in `knowledge/literature/` with the agent identity (`lit-monitor`) as the commit author, distinct from any human collaborator.
- An entry in the agent's namespaced review queue (e.g., `explorations/lit-monitor.md`) summarizes the new additions so humans can promote selected items to main via PR.
- The agent honors the permissions declared in its entry in `state/collaborators.yaml`: writes only within its scoped branches; never modifies canonical state directly.
- Validation runs after every batch; on failure, the agent rolls back its branch and surfaces the error rather than committing a broken state.

### US-A-07: Flag for the next meeting

**Actor**: Any contributor (human via CLI/skill, or AI agent)
**Story**: As a contributor with something to discuss, I want it surfaced on the next group meeting's agenda.

**Expected behavior**
- A `flag-for-meeting` skill or `cairn meeting flag` command adds an entry to a pending-agenda queue (e.g., `state/agenda_queue.yaml`).
- Each flagged item references its source by ID (a finding, question, branch, or action item).
- Flagging the same item twice is deduplicated; a second flag from a different contributor may add the contributor as an "interested party" but doesn't create a duplicate entry.
- The queue is what `cairn agenda draft` (US-P-09) reads from.

### US-A-08: Generate an artifact from a session

**Actor**: Claude Code agent at the end of a working session
**Story**: As an agent finishing a substantive piece of analysis, I want to bundle the work into an artifact ready for external review.

**Expected behavior**
- An `export-artifact` skill bundles the session's work — code changes, derived results, decisions made, findings logged — into a draft artifact in `knowledge/provenance/`.
- Calls into `cairn artifact export` under the hood; the skill's role is to know what scope to pass.
- The result is a draft (humans review before publishing); structure is valid against the chosen specification.
- A reference to the artifact is added to `state/decisions.yaml` or wherever appropriate, so the cairn knows the artifact exists.

### US-A-09: Close an exploration

**Actor**: Anyone wrapping up an exploration (or an agent helping them)
**Story**: As a contributor whose exploration is either merged or abandoned, I want to record its outcome so the cairn's exploration history stays accurate and the group can learn from it.

**Expected behavior**
- A `resolve-exploration` skill (and `cairn exploration close <name> --status merged|abandoned --reason "..."` CLI command) closes an exploration's record in the cairn.
- For `--status merged`: the command verifies the branch is actually merged into `main` (`git merge-base --is-ancestor <branch> main`) and refuses with a clear error if not. The branch ref itself can be deleted by the user separately; Cairn only records the outcome.
- For `--status abandoned`: no merge check; the branch can be left as a ref or deleted later.
- Appends a closure block to the exploration manifest at `explorations/<owner>/<slug>.md` containing `status`, `closed_at` (UTC ISO 8601), `closed_by` (collaborator id), `reason`, and (for merged) the merge commit SHA.
- Updates `explorations/README.md` on main: the row moves from the "Active explorations" table to a "Closed explorations" section, preserving the outcome and reason so it remains discoverable.
- Refuses to close a branch that has uncommitted changes in the working tree without an explicit `--force`.
- The closure commit is attributed to the invoking user and references the exploration name in its message.
- The exploration's manifest is never deleted — an abandoned exploration's record is part of the cairn's history of what was tried.

---

## §3 — MCP Server Stories

These stories cover clients (chatbots, dashboards, agents in other environments) querying a cairn through its MCP server. The MCP server reads from the cairn but does not own state. The tool names below are illustrative and may evolve.

### US-M-01: Compact project state for an agent

**Actor**: Claude Code agent in any session, on or off the cairn's host machine
**Story**: As an agent connecting via MCP, I want a compact orientation of the project's current state.

**Expected behavior**
- Tool `get_project_state(branch="main")` returns a structured summary: goals, recent decisions, open questions, active branches, recently active collaborators.
- The response is bounded in size (well within a session's available context budget — target ≤4k tokens).
- The branch parameter defaults to `main`; passing a branch name returns that branch's view.
- The response carries a version or timestamp the client can cache against and re-check cheaply.

### US-M-02: Semantic search across history

**Actor**: Agent or chatbot
**Story**: As an agent helping the user, I want to find prior discussions semantically related to a current question.

**Expected behavior**
- Tool `find_related_prior_discussion(query, limit=10, since=None)` returns ranked excerpts from meetings, findings, and discussion threads.
- Each result has source path, date, author, and a short excerpt suitable for citation.
- Search uses the MCP server's vector index, rebuilt on commits to the cairn; index lag is measured in minutes, not hours.
- Empty results return a structured empty response, not an error.

### US-M-03: Decisions on a topic

**Actor**: Chatbot or async client
**Story**: As a user asking the chatbot what was decided about something, I want a direct answer with citations.

**Expected behavior**
- Tool `get_decisions_about(topic, since=None)` returns matching decisions with text, date, author, context, and related question IDs.
- Topic matching is semantic but exact keywords always match.
- Each result includes the canonical decision ID for follow-up queries.
- Sorted by date descending (most recent first); a `--chronological` flag reverses if needed.

### US-M-04: Action items by person

**Actor**: User via Slack bot
**Story**: As a user asking "what is Maria working on?", I want a list of her current open action items.

**Expected behavior**
- Tool `get_action_items(assignee, status="open")` returns matching items: text, related questions/decisions, due date, status.
- Default sort is due date ascending (overdue items first).
- An unknown assignee returns an empty list with a hint about valid IDs, not an error.
- A reciprocal call `get_action_items(assignee=None, status="overdue")` returns all overdue items across the project.

### US-M-05: Branch summary

**Actor**: PI deciding whether to review a branch
**Story**: As a user, I want to understand what's happening on a particular branch without checking it out.

**Expected behavior**
- Tool `summarize_branch(branch_name)` returns: branch owner, date opened, age, recent commits (last 10), contributions made (proposed decisions, logged findings), what differs from main (added/modified/deleted files, with paths), whether the branch is active or dormant (defined by last commit date).
- Works identically for human-owned and AI-owned branches.
- The summary distinguishes contributions intended for merge (the "primary purpose" from the branch manifest) from incidental changes.

### US-M-06: Pre-meeting agenda

**Actor**: PI before a group meeting
**Story**: As the meeting organizer, I want a draft agenda based on what's accumulated since the last meeting.

**Expected behavior**
- Tool `draft_agenda(since="last_meeting")` returns a structured agenda.
- Includes: explicitly flagged items, branches needing review, open questions raised since the cutoff, action items due before the next meeting, recent findings worth presenting.
- The output is structured (a list of agenda items with type, source, brief description, and suggested time allocation) so a downstream tool (chatbot, email, dashboard) can render it appropriately.
- Does not commit anything to the cairn automatically; the agenda is a returned value.

### US-M-07: Submit an async contribution

**Actor**: User talking to a Slack bot during a hallway thought
**Story**: As a user, I want to add a thought to the cairn from Slack without switching contexts to my dev environment.

**Expected behavior**
- Tool `add_async_note(content, type, related=None)` accepts a contribution where `type` is one of `finding`, `question`, `idea`, `note`.
- The contribution lands in a pending-review queue (e.g., `state/async_queue.yaml`), NOT directly in `state/decisions.yaml` or `state/open_questions.yaml`.
- The contribution is attributed via the Slack user's mapped identity in `state/collaborators.yaml`; unmapped users get a friendly hint about how to map themselves.
- The client receives a confirmation including where the note landed and how to follow up to promote it to canonical state.

### US-M-08: Cross-project query

**Actor**: A collaborator on multiple projects
**Story**: As a user on project A who's also on project B, I want to know if any work from B is relevant to a current question in A.

**Expected behavior**
- When MCP servers for multiple cairns are reachable (or one server is configured with multiple cairns), tool `find_related_across_projects(query, projects=None)` returns matches across the named projects (or all the user is on).
- Each result clearly labels its source project, so the calling agent can cite it appropriately.
- Respects per-project access: only projects where the asking user appears in `state/collaborators.yaml` are searched.

### US-M-09: Health check

**Actor**: Devops or any client wanting to confirm the MCP server is alive and current
**Story**: As a client, I want to confirm the MCP server is responsive and that its view of the cairn is up to date.

**Expected behavior**
- Tool `health()` returns server uptime, last index rebuild timestamp, current cairn commit SHA the server is operating against, and the count of indexed documents.
- Latency is well under a second.
- If the index is significantly stale (lag exceeds a configurable threshold), the response includes a warning field but does not error.

---

## §4 — Cross-Cutting Properties

A handful of invariants hold across many stories above. These deserve their own tests beyond the per-story ones.

### Attribution is preserved end-to-end

Every write — by any actor, through any interface (CLI, skill, MCP submission) — records the actor's identity. Human collaborators are identified by git author; AI collaborators by their configured identity in `state/collaborators.yaml`. Tests should verify that a finding logged via skill, a contribution submitted via MCP, and a decision recorded via CLI all carry correct, distinct authorship.

### Writes from non-primary surfaces are queued

AI-collaborator writes and async-client (Slack, web form) submissions land in a review queue rather than directly modifying canonical state. Human collaborators using their own Claude Code session with direct file access can write canonically (they have full git access). Tests should verify that the same write operation from different actors takes different paths.

### The repo remains source of truth

Anything held by the MCP server (vector indices, caches, computed summaries) can be regenerated from the cairn. Stopping the MCP server, deleting its derived state, and restarting it produces an identical query surface. Tests should verify that a full rebuild from the repo yields the same query results as the live server before the rebuild.

### Operations are scoped to a single cairn by default

Commands run inside a cairn act on that cairn. Cross-cairn operations are explicit (US-M-08) and require the caller's other cairns to be discoverable. Tests should verify that a command run from inside cairn A cannot accidentally modify cairn B.

### Failures are visible

Schema violations, broken cross-references, malformed commits, and validation errors are reported with enough context to fix them. No silent corruption. Tests should verify that introducing a deliberate violation (a decision referring to a non-existent question, a collaborator with a duplicate ID) produces a clear error rather than being accepted.

### Branch semantics are honored

A read or write operation explicitly or implicitly scoped to a branch sees that branch's view, not main's. The MCP server's tools that take a `branch` parameter respect it consistently. Tests should verify that the same query (e.g., "what decisions exist?") returns different results on different branches when the branches diverge.

---

## §5 — Notes on Story Selection

These stories cover the substantive surface area but are not exhaustive. A few categories are deliberately deferred:

- **Voice-mode meeting participation.** Out of scope for the current spec; covered when meeting capture matures.
- **Visualization stories** (dashboards, branch graphs, person-by-time views). The MCP `get_project_state` and `summarize_branch` tools provide the data; the rendering layer is separate.
- **Multi-project meta-repo workflows.** Implied by US-M-08 but not detailed; the conventions for a user's meta-repo deserve their own story document once the pattern is exercised.
- **Backup, archive, and lifecycle management.** Important but not blocking for initial development.

Add stories to this document via PR as new capabilities are designed. The document itself lives in the Cairn architecture folder and should evolve alongside the implementation.
