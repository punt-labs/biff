# How I Write Code

I am a principal engineer. Every change I make leaves the codebase in a better state than I found it. I do not excuse new problems by pointing at existing ones. I do not defer quality to a future ticket. I do not create tech debt.

## Standards

Follow [punt-kit standards](../punt-kit/standards/) for Python, workflow, GitHub, CLI, and plugins. Below are biff-specific overrides and additions.

- **Tests accompany code.** Every module ships with tests. Untested code is unfinished code.
- **Types are exact.** Protocol classes for third-party libraries without stubs. `object` with narrowing where the type is structurally known. Never `Any`.
- **Runtime introspection is unnecessary.** Explicit Protocol inheritance and structural typing. Never `hasattr()`.
- **Duplication is a design failure.** If I see two copies, I extract one abstraction. If I wrote the duplication, I fix it before committing.
- **Backwards compatibility shims do not exist.** When code changes, callers change. No `_old_name = new_name` aliases, no `# removed` tombstones, no re-exports of dead symbols.
- **Legacy code shrinks.** Every change is an opportunity to simplify what surrounds it.
- **`from __future__ import annotations`** in every Python file. Full type annotations on every function signature.
- **Immutable data models.** `@dataclass(frozen=True)` or pydantic with immutability.
- **Latest Python.** Target 3.13+. Use modern PEP conventions (`Annotated`, `type` statements, `X | Y` unions).
- **Quality gates pass before every commit.** `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src/ tests/`, `uv run pyright`, `uv run pytest`. Zero violations, zero errors, all tests green.
- **Markdown lint passes.** CI runs `npx markdownlint-cli2 "**/*.md"` on every push. All markdown files (DESIGN.md, CHANGELOG.md, README.md, CLAUDE.md) must pass. Common issues: fenced code blocks need a language tag (`text`, `bash`, `python`, `json`), lists need blank lines before and after.
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
uv run pytest -m hosted                # Tier 3c: hosted NATS tests (local only, see below)
uv run pytest -m sdk                   # Tier 4: SDK tests (requires ANTHROPIC_API_KEY)
uv run pytest -m "subprocess or sdk"   # Tiers 3-4 together
```

### CI vs Local Tests

GitHub Actions runs **Lint** and **Tests** (tiers 1-2) on every push/PR. The **Hosted NATS E2E** workflow is manual-only (`workflow_dispatch`) because session-scoped NATS connections hang in GitHub Actions' asyncio environment. Run hosted NATS tests locally before merging relay changes:

```bash
BIFF_TEST_NATS_URL=tls://connect.ngs.global \
BIFF_TEST_NATS_CREDS=src/biff/data/demo.creds \
uv run pytest -m hosted -v
```

### Test Fixtures by Tier

Each tier provides `kai` and `eric` fixtures representing two users sharing a `tmp_path` data directory:

| Tier | Fixture Type | Import | Key Method |
|------|-------------|--------|------------|
| Integration | `RecordingClient` | `biff.testing` | `await kai.call("plan", message="...")` |
| Subprocess | `RecordingClient` | `biff.testing` | `await kai.call("plan", message="...")` |
| SDK | `SDKClient` | `tests/test_sdk/_client.py` | `await kai.prompt('Call the "plan" tool...')` |

`RecordingClient` wraps a FastMCP `Client` with transcript capture. `SDKClient` wraps the Claude Agent SDK `query()` with structured result parsing and transcript capture.

Tests marked `@pytest.mark.transcript` auto-save human-readable transcripts to `tests/transcripts/`.

## Biff-Specific Workflow

### GitHub Operations

Use the GitHub MCP server tools for all GitHub operations: creating PRs, merging PRs, reading PR status/diff/comments, creating/reading issues, searching, and managing releases. When GitHub MCP is unavailable, the `gh` CLI is acceptable.

Git operations (commit, push, branch, checkout, tag) remain via the Bash tool.

### Push Policy

**All code changes go through a PR** — even small cleanups, script deletions, and one-line fixes. Branch protection requires CI checks and review; bypassing it risks broken main.

**Exceptions (direct push to main allowed):**

- `README.md` edits (but run `npx markdownlint-cli2 README.md` first)
- `bd sync` commits (beads bookkeeping, auto-generated)
- Release version bump commits from `/punt:release` (the release workflow is its own gate)

When in doubt, use a branch.

**After creating a PR**, wait for CI to finish before reading review feedback:

```bash
gh pr checks <number> --watch
```

Then check for Copilot and Bugbot review comments before merging.

### Quarry Knowledge Base

All punt-labs repos are indexed in quarry (local semantic search) as separate collections: biff, quarry, punt-kit, prfaq, langlearn-tts, public-website, claude-code-docs, and more. Use `/find <query>` to quickly look up design decisions, NATS configuration, test patterns, architecture details, and cross-project knowledge without reading entire files. Results are ranked by relevance with page references.

Use quarry first when answering questions about prior design decisions, TTL values, protocol details, or "why was X built this way." It is faster than grepping through DESIGN.md's 100+ pages.

### Installer Is Source of Truth

User-facing config (`~/.claude/commands/`, `~/.claude/plugins/biff/`, MCP registration, status line) is deployed by `biff install`. Do not hand-edit these paths — changes will be overwritten on next install. To change command behavior, edit the bundled source in `src/biff/plugins/biff/commands/` and re-run `biff install`.

### Release Process

Biff ships through two channels. Each has its own cadence and bar.

#### Version Source of Truth

Version lives in two files that must stay in sync:

| File | Field |
|------|-------|
| `pyproject.toml` | `version = "X.Y.Z"` |
| `.claude-plugin/plugin.json` | `"version": "X.Y.Z"` |

After editing both, run `uv lock` to update `uv.lock`.

Use semver: patch for fixes, minor for features, major for breaking changes. Bump on every PR that changes user-facing behavior.

**IMPORTANT:** `plugin.json` must always have `"name": "biff"` on main. Dev/prod isolation uses `.claude/commands/` for dev-only commands (project-local, not shipped to marketplace users). The plugin name is always `"biff"` — no `"biff-dev"` swapping.

**IMPORTANT:** Never use `uv tool install --force --editable .` as a release or testing step. Local editable installs break the status line (see DESIGN.md DES-011b) and do not represent what users experience. The only way to test is to release through the real channels.

#### Both Channels Ship Together

The marketplace plugin ships config (hooks, commands, plugin.json) but the `biff` CLI binary that it invokes (`biff serve`) comes from PyPI. If only the marketplace updates, users get new hooks calling old code. **Both channels must release on every version bump.**

#### Channel 1: Plugin Marketplace

The plugin marketplace serves Claude Code users via `claude plugin install biff@punt-labs` and `claude plugin update`. It pulls from the git tag on GitHub.

**When:** Every version bump merged to main.

#### Channel 2: PyPI (`punt-biff`)

PyPI serves `uv tool install punt-biff`. This ships the Python runtime (MCP server, relay, CLI).

**When:** Every version bump merged to main (same as Channel 1).

#### Release Process

Both channels release from a single workflow. The git tag triggers `.github/workflows/release.yml` which handles build → TestPyPI → test-install → PyPI automatically.

**Bar:**

- [ ] **Quality gates pass** — ruff, mypy, pyright, tier 1-2 tests
- [ ] **Hosted NATS tests pass locally** if relay code changed

**Process:**

```bash
# 1. Version already bumped in PR (pyproject.toml + plugin.json + uv lock)
# 2. CHANGELOG updated with release section
# 3. PR merged to main

# 4. Tag and push (triggers release.yml → TestPyPI → PyPI)
git checkout main && git pull origin main
git tag vX.Y.Z
git push origin vX.Y.Z

# 5. Create GitHub Release
gh release create vX.Y.Z --title "vX.Y.Z" --notes "See CHANGELOG.md"

# 6. Update marketplace registry (punt-labs/claude-plugins)
# The UI discovery reads from this file, NOT from individual repo plugin.json
# Update version in .claude-plugin/marketplace.json, commit, push

# 7. Verify both channels
claude plugin update biff@punt-labs     # Plugin → new version
uv tool install --upgrade punt-biff     # CLI → new version
biff doctor                             # Both match
```

**NEVER manually run `twine upload`.** The release workflow handles PyPI publication with TestPyPI verification. Manual upload bypasses that safety net.

**Specialized agents for release validation:**

| Agent | Role | When to Use |
|-------|------|-------------|
| `distributed-test-engineer` | Diagnoses distributed system test failures: NATS, asyncio, pytest-asyncio, MCP transport | Hosted NATS test hangs, connection lifecycle bugs |
| `leak-hunter` | Finds resource leaks: NATS consumers, asyncio tasks, file descriptors | Before any PyPI release, after relay code changes |

### Pre-PR Checklist

Before creating a PR, verify:

- [ ] **Version bumped** in both `pyproject.toml` and `plugin.json` if user-facing behavior changed
- [ ] **`plugin.json` name is `"biff"`** (not `"biff-dev"`)
- [ ] **CHANGELOG entry included in the PR diff** under `## Unreleased` (not retroactively on main)
- [ ] **README updated** if user-facing behavior changed
- [ ] **Quality gates pass**
- [ ] **Hosted NATS tests pass locally** if relay code changed — `BIFF_TEST_NATS_URL=tls://connect.ngs.global BIFF_TEST_NATS_CREDS=src/biff/data/demo.creds uv run pytest -m hosted -v`

### Workflow Tiers

Match the workflow to the bead's scope. The deciding factor is **design ambiguity**, not size.

| Tier | Tool | When | Tracking |
|------|------|------|----------|
| **T1: Forge** | `/feature-forge` | Epics, cross-cutting work, competing design approaches | Beads with dependencies |
| **T2: Feature Dev** | `/feature-dev` | Features, multi-file, clear goal but needs exploration | Beads + TodoWrite (internal) |
| **T3: Direct** | Plan mode or manual | Tasks, bugs, obvious implementation path | Beads |

**Escalation only goes up.** If T3 reveals unexpected scope, escalate to T2. If T2 reveals competing design approaches, escalate to T1. Never demote mid-flight.

## Knowledge Propagation Protocol

After merging a PR that introduces new patterns, design decisions, or hard-won debugging insights, propagate knowledge outward before closing the session:

### 1. Document in DESIGN.md

Log the decision in the appropriate design log (`DESIGN.md` or `DESIGN-INSTALLER.md`). Include: what changed, why, what was rejected, and what evidence drove the decision.

### 2. Propagate to punt-kit

If the pattern is reusable across projects:

- **Pattern file** — Create or update `punt-kit/patterns/<name>.md` if a new architectural pattern emerged (e.g., process tree walk, sentinel types, relay round-trip elimination).
- **Standard update** — Update `punt-kit/standards/*.md` if an existing standard was invalidated or needs refinement.
- **PR directly** for factual corrections to existing patterns. **Bead** in punt-kit for broader standards work.

### 3. Hand off to public-website

If the discovery is interesting to external developers (plugin authors, MCP server builders, Claude Code users):

- Create a **bead** in `public-website/` describing what to add to the existing blog post (or a new post if warranted).
- Include: the story arc (what broke, how we found it, what the fix teaches), technical details, and audience.

### 4. Update prfaq.tex and README

If the feature was on the roadmap, move it to "Shipped" in both `README.md` and `prfaq.tex`. Recompile the PDF. Features should never remain listed as "Next" after they ship.

### Checklist

```text
[ ] DESIGN.md updated (if design decision)
[ ] punt-kit patterns/ or standards/ updated (if reusable pattern)
[ ] public-website bead created (if externally interesting)
[ ] README.md and prfaq.tex updated (if shipped feature)
[ ] prfaq.pdf recompiled
```

## Product Vision

The PR/FAQ (`prfaq.tex`) is the authoritative source for product vision, target market, command vocabulary, phasing, risk assessment, and "what we are not building." When there are questions about scope, priorities, or product direction, consult the PR/FAQ first.

## Design Decision Logs

Two design logs exist. Both follow the same rules: consult before changing, do not revisit settled decisions without new evidence, log decisions before implementing.

| Log | Scope | Covers |
|-----|-------|--------|
| `DESIGN.md` | Runtime system | Display pipeline, session keys, transport, push notifications, relay protocol, config format |
| `DESIGN-INSTALLER.md` | Installation system | Two-phase install, plugin file delivery, MCP registration, status line stash-and-wrap, doctor checks, identity resolution, `.biff` init, uninstall |

**The display pipeline is fragile.** The PostToolUse hooks, skill command prompts, status line, and push notification system have non-obvious interactions and represent 12-16 hours of iteration. The split between `updatedMCPToolOutput` (panel summary) and `additionalContext` (model-emitted full output) exists because multi-line MCP output gets truncated behind a "Control-O for more" prompt. Changes that look simple can break the display pipeline in ways that are difficult to debug and easy to repeat.

**Before proposing or making ANY design change:**

1. Read `DESIGN.md` and `DESIGN-INSTALLER.md` for prior decisions on the same topic.
2. Do not revisit a settled decision without new evidence.
3. Log the decision, alternatives considered, and outcome in the appropriate log before implementing.

Failure to consult the design logs has already caused wasted work and rollbacks. This rule is non-negotiable.

## Biff Architecture

Biff is a CLI communication tool for software engineers, named after the Berkeley dog whose 1980 mail notification program was part of the same BSD family as `wall`, `talk`, `finger`, `write`, and `mesg`. It resurrects the Unix communication vocabulary as MCP-native slash commands. See `prfaq.tex` for the full product vision, command vocabulary, and phasing.

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
