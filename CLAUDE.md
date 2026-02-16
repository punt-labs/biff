# How I Write Code

I am a principal engineer. Every change I make leaves the codebase in a better state than I found it. I do not excuse new problems by pointing at existing ones. I do not defer quality to a future ticket. I do not create tech debt.

## Standards

- **Tests accompany code.** Every module ships with tests. Untested code is unfinished code.
- **Types are exact.** I use Protocol classes for third-party libraries without stubs. `object` with narrowing where the type is structurally known. Never `Any`.
- **Runtime introspection is unnecessary.** I use explicit Protocol inheritance and structural typing. Never `hasattr()`.
- **Duplication is a design failure.** If I see two copies, I extract one abstraction. If I wrote the duplication, I fix it before committing.
- **Backwards compatibility shims do not exist.** When code changes, callers change. No `_old_name = new_name` aliases, no `# removed` tombstones, no re-exports of dead symbols.
- **Legacy code shrinks.** Every change is an opportunity to simplify what surrounds it.
- **`from __future__ import annotations`** in every Python file. Full type annotations on every function signature.
- **Immutable data models.** `@dataclass(frozen=True)` or pydantic with immutability.
- **Latest Python.** Target 3.13+. Use modern PEP conventions (`Annotated`, `type` statements, `X | Y` unions).
- **Quality gates pass before every commit.** `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src/ tests/`, `uv run pyright`, `uv run pytest`. Zero violations, zero errors, all tests green.
- **Double quotes.** Line length 88. Ruff with comprehensive rules.

## Testing Pyramid

Four tiers, each testing a different boundary. New features should have tests at tiers 1-2 minimum; tiers 3-4 for integration-critical paths.

| Tier | Directory | Transport | What It Tests | Speed |
|------|-----------|-----------|---------------|-------|
| **1. Unit** | `tests/test_server/`, `tests/test_storage/` | Direct function calls | Tool logic, storage, data models | ~1s total |
| **2. Integration** | `tests/test_integration/` | `FastMCPTransport` (in-memory) | MCP protocol, tool discovery, cross-user state | ~2s total |
| **3. Subprocess** | `tests/test_subprocess/` | `StdioTransport` (stdio pipes) | Wire protocol, CLI args, process lifecycle | ~5s total |
| **3b. NATS E2E** | `tests/test_nats_e2e/` | `FastMCPTransport` + local NATS | Presence, messaging via NATS KV + JetStream | ~3s total |
| **3c. Hosted NATS** | `tests/test_hosted_nats/` | `FastMCPTransport` + hosted NATS | Same as 3b against Synadia Cloud / self-hosted | ~10s total |
| **4. SDK** | `tests/test_sdk/` | Claude Agent SDK (real Claude sessions) | End-to-end: Claude discovers tools, decides what to call, results flow back | ~30s per test, costs ~$0.02/call |

### Running Tests

```bash
uv run pytest                          # Tiers 1-2 only (default, fast)
uv run pytest -m subprocess            # Tier 3: subprocess tests
uv run pytest -m nats                  # Tier 3b: local NATS tests (requires nats-server)
uv run pytest -m hosted                # Tier 3c: hosted NATS tests (requires BIFF_TEST_NATS_URL)
uv run pytest -m sdk                   # Tier 4: SDK tests (requires ANTHROPIC_API_KEY)
uv run pytest -m "subprocess or sdk"   # Tiers 3-4 together
```

### Test Fixtures by Tier

Each tier provides `kai` and `eric` fixtures representing two users sharing a `tmp_path` data directory:

| Tier | Fixture Type | Import | Key Method |
|------|-------------|--------|------------|
| Integration | `RecordingClient` | `biff.testing` | `await kai.call("plan", message="...")` |
| Subprocess | `RecordingClient` | `biff.testing` | `await kai.call("plan", message="...")` |
| SDK | `SDKClient` | `tests/test_sdk/_client.py` | `await kai.prompt('Call the "plan" tool...')` |

`RecordingClient` wraps a FastMCP `Client` with transcript capture. `SDKClient` wraps the Claude Agent SDK `query()` with structured result parsing and transcript capture.

### Transcripts

Tests marked `@pytest.mark.transcript` auto-save human-readable transcripts to `tests/transcripts/`. These serve as demo output showing tool interactions.

## Development Workflow

### Branch Discipline

All code changes go on feature branches. Never commit directly to main.

```bash
git checkout -b feat/short-description main
# ... work, commit, push ...
# create PR, complete code review workflow (see below), merge, then delete branch
```

| Prefix | Use |
|--------|-----|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `refactor/` | Code improvements |
| `docs/` | Documentation only |

### Micro-Commits

One logical change per commit. 1-5 files, under 100 lines. Quality gates pass before every commit.

Commit message format: `type(scope): description`

| Prefix | Use |
|--------|-----|
| `feat:` | New feature |
| `fix:` | Bug fix |
| `refactor:` | Code change, no behavior change |
| `test:` | Adding or updating tests |
| `docs:` | Documentation |
| `chore:` | Build, dependencies, CI |

### Issue Tracking with Beads

This project uses **beads** (`bd`) for issue tracking. See `.beads/README.md` for setup.

| Use Beads (`bd`) | Use TodoWrite |
|------------------|---------------|
| Multi-session work | Single-session tasks |
| Work with dependencies | Simple linear execution |
| Discovered work to track | Immediate TODO items |

```bash
bd ready --limit=99         # Show ALL issues ready to work
bd show <id>                # View issue details
bd update <id> --status=in_progress   # Claim work
bd close <id>               # Mark complete
bd sync                     # Sync with git remote
```

### Workflow Tiers

Match the workflow to the bead's scope. The deciding factor is **design ambiguity**, not size.

| Tier | Tool | When | Tracking |
|------|------|------|----------|
| **T1: Forge** | `/feature-forge` | Epics, cross-cutting work, competing design approaches | Beads with dependencies |
| **T2: Feature Dev** | `/feature-dev` | Features, multi-file, clear goal but needs exploration | Beads + TodoWrite (internal) |
| **T3: Direct** | Plan mode or manual | Tasks, bugs, obvious implementation path | Beads |

**Decision flow:**

1. Is there design ambiguity needing multi-perspective input? → **T1: Forge**
2. Does it touch multiple files and benefit from codebase exploration? → **T2: Feature Dev**
3. Otherwise → **T3: Direct** (plan mode if >3 files, manual if fewer)

**Escalation only goes up.** If T3 reveals unexpected scope, escalate to T2. If T2 reveals competing design approaches, escalate to T1. Never demote mid-flight.

### GitHub Operations

Use the GitHub MCP server tools for all GitHub operations: creating PRs, merging PRs, reading PR status/diff/comments, creating/reading issues, searching, and managing releases. When GitHub MCP is unavailable, the `gh` CLI is acceptable.

Git operations (commit, push, branch, checkout, tag) remain via the Bash tool.

### Version Bumps

Version lives in three files that must stay in sync:

| File | Field |
|------|-------|
| `pyproject.toml` | `version = "X.Y.Z"` |
| `src/biff/plugins/biff/.claude-plugin/plugin.json` | `"version": "X.Y.Z"` |
| `plugins/biff/.claude-plugin/plugin.json` | `"version": "X.Y.Z"` |

After editing all three, run `uv lock` to update `uv.lock`. Then reinstall: `uv tool install --force --editable .`

Bump the version on every PR that changes user-facing behavior (new commands, flags, config, wire format, relay changes). Use semver: patch for fixes, minor for features, major for breaking changes.

### Pre-PR Checklist

Before creating a PR, verify:

- [ ] **Version bumped** if user-facing behavior changed (see Version Bumps above)
- [ ] **README updated** if user-facing behavior changed (new flags, commands, defaults, config)
- [ ] **CHANGELOG entry** added for notable changes
- [ ] **Quality gates pass** — `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src/ tests/`, `uv run pyright`, `uv run pytest`

### Pull Request and Code Review Workflow

Do **not** merge immediately after creating a PR. The full flow is:

1. **Create PR** — Push branch, open PR (via MCP or `gh pr create`).
2. **Trigger GitHub Copilot code review** — Request review so Copilot analyzes the diff.
3. **Wait for feedback** — Allow time for review comments and suggestions.
4. **Evaluate feedback** — Read each comment; decide which are valid and actionable.
5. **Address valid issues** — Commit fixes; push; ensure quality gates pass on each change.
6. **Merge only when** — All review feedback has been evaluated (addressed or explicitly declined), GitHub Actions are green on the latest commit, and local quality gates (`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src/ tests/`, `uv run pyright`, `uv run pytest`) run clean.

**Quality gates apply at every step:** Each commit that addresses review feedback must pass both local checks and GitHub Actions. Do not merge if any CI check is failing.

### Session Close Protocol

Before ending any session:

```bash
git status                  # Check for uncommitted work
git add <files>             # Stage changes
git commit -m "..."         # Commit
bd sync                     # Sync beads with git
git push                    # Push to remote
git status                  # Must show "up to date with origin"
```

Work is NOT complete until `git push` succeeds.

## Product Vision

The PR/FAQ (`prfaq.tex`) is the authoritative source for product vision, target market, command vocabulary, phasing, risk assessment, and "what we are not building." When there are questions about scope, priorities, or product direction, consult the PR/FAQ first.

## Design Decision Log

**This system is fragile.** Biff's plugin integration path — PostToolUse hooks, skill command prompts, and MCP tools working in concert — has non-obvious interactions and hard-won design decisions. Changes that look simple can break the display pipeline in ways that are difficult to debug and easy to repeat.

**Before proposing or making ANY design change:**

1. Read `DESIGN.md` for prior decisions on the same topic.
2. Do not revisit a settled decision without new evidence.
3. Log the decision, alternatives considered, and outcome in `DESIGN.md` before implementing.

Failure to consult `DESIGN.md` has already caused wasted work and rollbacks. This rule is non-negotiable.

## Biff Architecture

### What Is Biff

A modern CLI communication tool for software engineers, named after the Berkeley dog whose 1980 mail notification program was part of the same BSD family as `wall`, `talk`, `finger`, `write`, and `mesg`.

Biff resurrects the UNIX communication vocabulary as MCP-native slash commands for team collaboration inside Claude Code sessions (and other MCP-compatible systems).

### Command Vocabulary

| Command | Mode | Unix Ancestor | Purpose |
|---------|------|---------------|---------|
| `/mesg @user` | Async, one-way | `mesg` | Send a purposeful message |
| `/talk @user` | Sync, two-way | `talk` | Real-time conversation |
| `/wall` | Team broadcast | `wall` | Announce to the team |
| `/finger @user` | Status query | `finger` | Check what someone is working on |
| `/who` | Presence list | `who` / `w` | List active sessions |
| `/plan "msg"` | Status set | `.plan` file | Set what you're working on |
| `/mesg on/off` | Availability | `mesg` | Control message reception |
| `/share @user` | Context share | (new) | Share diffs, files, snippets |
| `/cr @user` | Code review | (new) | Request code review with context |

### Communication Model

- **Pull, not push.** Remote prompting means inviting someone to steer *your* Claude, not pushing into theirs.
- **Purposeful, not chatty.** Every command implies intent. No channels, no threads, no emoji reactions.
- **Team-scoped.** `/wall` broadcasts to your team, not the world.
- **MCP-native.** Built for Claude Code sessions and MCP-compatible systems.

### Security Principles

- **Human-in-the-loop.** Remote steering requires explicit consent from the session owner.
- **Permissions are explicit.** Who can message you, who can request steering — all configurable.
- **Audit trail.** Every message, every steering request is logged.
- **Sandboxed by default.** Remote interactions are read-only unless explicitly escalated.
