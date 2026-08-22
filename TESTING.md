# Testing

Biff has an eight-tier testing pyramid. Each tier tests a different
boundary, at a different transport, with different infrastructure
requirements. Run `uv run pytest --collect-only` for current test
counts per tier — this file doesn't print them, because they drift
with every PR and a stale number in a doc is worse than no number.

Every tier above unit/integration has its own pytest marker, and no
marker is shared between two different tiers — `pytest -m <marker>`
always selects exactly one tier, never a superset.

## Pyramid

```text
                    ┌───────────┐
                    │  Tier 4   │  SDK — Claude picks the tools
                    ├───────────┤
                 ┌──┤  Tier 3d  │  Hosted NATS — real remote relay
                 │  │           │  (Synadia demo, or your own deploy)
                 │  ├───────────┤
              ┌──┤  │  Tier 3c  │  Local NATS — bare nats-server binary
              │  │  │           │
              │  │  ├───────────┤
           ┌──┤  │  │  Tier 3b  │  Docker relay image — the real
           │  │  │  │           │  ghcr.io/punt-labs/biff-relay
           │  │  │  │           │  container, built fresh per run
           │  │  │  ├───────────┤
        ┌──┤  │  │  │  Tier 3a  │  Subprocess — stdio pipes
        │  │  │  │  │           │
        │  │  │  │  ├───────────┤
        │  │  │  │  │  Tier 2b  │  CLI multi-user — cli_session + NATS
        │  │  │  │  │           │
        │  │  │  │  ├───────────┤
        │  │  │  │  │  Tier 2   │  Integration — FastMCPTransport
        │  │  │  │  │           │
        │  │  │  │  ├───────────┤
        │  │  │  │  │  Tier 1   │  Unit — direct function calls
        │  │  │  │  │           │
        └──┴──┴──┴──┴───────────┘
```

Tiers 3b, 3c, and 3d all exercise real NATS protocol, but against three
different deployments — a locally-built Docker image, a bare local
binary, and a real remote server — because those are three genuinely
different things to get wrong, and a bug in one doesn't imply a bug in
another. The Docker-image tier (biff-kmv) shipped alongside this doc update.

## Markers

| Tier | Marker | What it requires |
|------|--------|-------------------|
| 1, 2 | *(none — default collection)* | Nothing |
| 2b | `cli_multi_user` | Local `nats-server` binary on `PATH` |
| 3a | `subprocess` | Nothing (forces `LocalRelay` via `--relay-url ""`) |
| 3b | `nats_docker` | Docker running locally |
| 3c | `nats_local` | Local `nats-server` binary on `PATH` |
| 3d | `nats_hosted` | `BIFF_TEST_NATS_URL` / `BIFF_TEST_NATS_CREDS` (or `_TOKEN` / `_NKEYS_SEED`) pointed at a real relay |
| 3d (subset) | `stress` | Same as `nats_hosted` — scale stress tests specifically (`tests/test_hosted_nats/test_stress.py`) |
| 4 | `sdk` | `ANTHROPIC_API_KEY` |

`cli_multi_user` and `nats_local` both need a bare `nats-server` binary,
but they're deliberately distinct tiers with distinct markers — 2b
tests the CLI/`cli_session()` code path, 3c tests the full MCP server
over `FastMCPTransport`. Selecting one never accidentally pulls in the
other.

## Tiers

### Tier 1: Unit tests

Direct function calls. No transport, no server, no relay.

| Directory | What it covers |
|-----------|----------------|
| `tests/test_main.py` | CLI arg parsing, flag handling, command wiring, REPL launch |
| `tests/test_commands/` | Command logic via `LocalRelay` — formatting, edge cases, multi-user |
| `tests/test_server/` | MCP tool handlers, storage, data models |
| `tests/test_dispatch.py` | REPL command dispatcher — all commands + arg validation |
| `tests/test_repl_talk.py` | Talk subsystem — drain, handshake, accept, publish (Z spec coverage) |
| `tests/test_repl_loop.py` | REPL loop — prompt gate, dispatch, mode transitions, sync |
| `tests/test_repl_notify.py` | NotifyState — unread count, wall change, sync, boundaries |
| `tests/test_repl_readline.py` | Readline — completer, history |
| `tests/test_cli_session.py` | Session lifecycle — heartbeat, registration, wtmp, cleanup |
| `tests/test_config.py` | Config loading, git root detection, identity resolution |
| `tests/test_statusline.py` | Status bar rendering, formatting |
| `tests/test_relay.py` | `LocalRelay` operations |
| `tests/test_tty.py` | TTY name generation, hostname detection |
| Other unit files | Models, formatting, hooks, dormant relay, doctor, etc. |

**Mock strategy varies by what's under test:**

- `test_main.py` mocks `cli_session` (no NATS) and `commands.*` functions
  (no relay) to test CLI wiring in isolation. Verifies Typer parsed
  args correctly and forwarded them to the right command function.
- `test_commands/` uses `LocalRelay` backed by `tmp_path` — no mocking,
  no NATS. Tests command logic (formatting, multi-user state, edge
  cases) against real filesystem state.
- `test_server/` calls MCP tool handlers directly with constructed
  `ServerState`. Tests the tool layer without transport.
- `test_repl_loop.py` mocks `dispatch` and `_handle_repl_talk`, feeds
  lines via asyncio queue, and verifies prompt gate state and loop
  termination. No stdin thread, no NATS.
- `test_repl_talk.py` uses pre-loaded asyncio queues to test drain
  functions, handshake detection, and accept checking. Tests derived
  from Z specification partition analysis (docs/talk.tex).
- `test_cli_session.py` mocks `NatsRelay` to test session lifecycle
  (registration, wtmp events, heartbeat, cleanup on failure).

Mocks in this repo are scoped to external dependencies (`gh`, `vox`,
`shutil.which`) or to isolate one unit from another within tier 1 —
never a substitute for exercising the relay layer. `LocalRelay` is a
real, filesystem-backed implementation, not a mock; it's what "no
infrastructure" tiers run against.

### Tier 2: Integration tests

Full MCP server connected via `FastMCPTransport` (in-memory). Two users
(kai, eric) share a `tmp_path` data directory. Tests MCP protocol
compliance, tool discovery, cross-user state interactions.

```text
tests/test_integration/
├── conftest.py              # kai/eric fixtures via FastMCPTransport
├── test_e2e_presence.py     # Presence: who, finger, plan across users
├── test_last.py             # Session history
├── test_protocol.py         # MCP protocol: tool listing, error handling
├── test_talk.py             # Real-time talk sessions
├── test_tty_sessions.py     # Multi-session TTY management
├── test_tty.py              # TTY naming
├── test_wall.py             # Broadcast: post, read, clear, duration
└── test_workflows.py        # Multi-step workflows across commands
```

**Transport**: `FastMCPTransport` — in-process, no serialization overhead.
Same MCP protocol as production but without stdio or HTTP.
**Relay backend**: `LocalRelay`.

### Tier 2b: CLI multi-user tests

`tests/test_cli_multi_user/`, marker `cli_multi_user`. Two
`cli_session()` instances sharing a local NATS server. Tests
multi-user scenarios using `biff.commands` pure async functions —
the same code path as the interactive REPL, but without stdin threads
or display concerns.

```python
async with cli_session(user="kai") as kai:
    async with cli_session(user="eric") as eric:
        await commands.write(kai, "eric", "review the PR")
        result = await commands.read(eric)
        assert "review the PR" in result.text
```

This tier fills the gap between tier 2 (`LocalRelay`, no NATS) and
tier 3c (full MCP server over NATS). It exercises real NATS paths
(JetStream messaging, KV presence, talk notifications) at a fraction
of the complexity of MCP E2E tests.

**Scenarios**: presence, messaging, wall broadcasts, plan visibility,
talk handshake, session cleanup, wtmp history, mesg off.
**Transport**: `cli_session()` → `NatsRelay` → local `nats-server`.
No MCP protocol, no subprocess overhead.

```bash
uv run pytest -m cli_multi_user
```

### Tier 3a: Subprocess tests

Spawns real `biff mcp` subprocesses connected via
`StdioTransport`. Tests wire protocol, CLI argument parsing, process
lifecycle, and graceful shutdown.

```bash
uv run pytest -m subprocess
```

**Transport**: `StdioTransport` — real process, real stdio pipes. Uses
`--relay-url ""` to force `LocalRelay` (no NATS dependency).

### Tier 3b: Docker relay image tests

Builds `ghcr.io/punt-labs/biff-relay` fresh from this checkout's
`docker/` directory, runs the real container, and exercises it the
same way an operator would: presence/write/read round trips, JetStream
persistence across a container restart, and the entrypoint's
auth-refusal guard (a mounted `nats.conf` without `authorization` /
`accounts` / `nkeys` must make the container refuse to start, not
silently run unauthenticated).

This is the one tier that validates the *packaged* image rather than a
bare `nats-server` binary — config-file layering (`base.conf` vs
`base-with-user.conf`), the loopback-only monitoring bind, and the
auth guard added after PR #376's review cycle are only real for this
tier; tier 3c and 3d never touch `docker/` at all.

```bash
uv run pytest -m nats_docker
```

Requires Docker running locally (or in the CI runner).

### Tier 3c: Local NATS E2E

Full MCP servers backed by `NatsRelay`, connected via
`FastMCPTransport`. Requires a local `nats-server` binary. Two
directories share this tier: `tests/test_nats_e2e/` (MCP-level
scenarios — presence, messaging, wall, talk, KV watch survival,
notification latency) and `tests/test_nats/` (`NatsRelay` unit-level
tests against real NATS KV/JetStream — mirrors `tests/test_relay.py`
but exercises the NATS backend instead of the filesystem).

```bash
uv run pytest -m nats_local
```

**Cleanup**: An autouse fixture deletes NATS streams after each test
for full isolation.

### Tier 3d: Hosted NATS E2E

Same as 3c but against a real remote relay — the shared demo relay on
Synadia Cloud by default, or any hosted/self-hosted NATS server you
point it at. Manual-only, both locally and in CI (see
[Dev box vs CI](#dev-box-vs-ci) below).

```bash
BIFF_TEST_NATS_URL=tls://connect.ngs.global \
BIFF_TEST_NATS_CREDS=src/biff/data/demo.creds \
uv run pytest -m nats_hosted -v
```

**Connection budget**: Hosted accounts have low connection limits (e.g. 5
on Synadia starter). Fixtures use session-scoped relays — two NATS
connections total, reused across all tests.

**Cleanup**: Purges KV keys and stream messages but keeps infrastructure
intact. Avoids propagation delays from rapid create/delete cycles on
hosted servers.

**Stress subset**: `tests/test_hosted_nats/test_stress.py` carries its
own `stress` marker on top of `nats_hosted` — scale/load scenarios
against the same hosted relay, run separately (`uv run pytest -m
stress`) since they're deliberately heavier than the rest of the tier.

Run tier 3d locally before merging any relay code changes.

### Tier 4: SDK tests

Drives real Claude Code sessions via the Claude Agent SDK. Claude
discovers biff's MCP tools, decides which to call, and results flow
back through the full stack.

```bash
uv run pytest -m sdk
```

**Transport**: Claude Agent SDK → `biff mcp` subprocess.
Claude is the caller — tests validate that tool descriptions are clear
enough for the model to use correctly.

**Cost**: Real money per test, tens of seconds each. Requires
`ANTHROPIC_API_KEY`.

## Dev box vs CI

The tiers above split cleanly into "what runs automatically" and "what
you run deliberately." Neither list is arbitrary — every gap below has
a concrete blocking reason, tracked as a bead, not a permanent design
choice unless stated otherwise.

### On your dev box

| Tier | Command | When to run it |
|------|---------|-----------------|
| 1, 2 | `uv run pytest` (default) | Constantly. No external dependency; this is your inner loop. |
| 3a | `uv run pytest -m subprocess` | Before pushing anything touching CLI wiring or process lifecycle. No extra infra — forces `LocalRelay`. |
| 2b | `uv run pytest -m cli_multi_user` | When touching the CLI multi-user code path (`biff.commands` over a real relay). Needs `nats-server` on `PATH`; the fixture skips itself (not an error) if it's missing. |
| 3b | `uv run pytest -m nats_docker` | When touching `docker/`. Needs Docker running locally. |
| 3c | `uv run pytest -m nats_local` | When touching `NatsRelay`, relay selection, or anything JetStream/KV-shaped at the MCP-server level. Same `nats-server`-on-`PATH` requirement as 2b, distinct marker. |
| 3d | `uv run pytest -m nats_hosted -v` (with `BIFF_TEST_NATS_URL`/`BIFF_TEST_NATS_CREDS` set) | Before merging any relay code change — this is the only tier that proves the code works against a real, non-local NATS deployment. Deliberate and occasional, never in a loop (see connection budget above). |
| 4 | `uv run pytest -m sdk` | Rare, deliberate — before a change to MCP tool descriptions or anything the model's tool-selection depends on. Costs real money per run. |

### In CI

What's actually enforced on every push/PR (`subprocess-tests.yml` and
friends): **tiers 1, 2, 3a, and 3b.** Nothing merges without these green.
That's the whole gate today.

Everything else is deliberately outside that gate, for reasons that
are either a known, tracked gap or a genuine permanent constraint —
these are not the same thing, and the doc should never blur them:

- **Tier 2b / 3c (local NATS)** — no CI job. Blocked by biff-7xd:
  `nats-server` hangs outright in the CI environment. This is a *gap*,
  not a design choice — fix the hang, then wire it in.
- **Tier 3d (hosted)** — `hosted-nats.yml` exists but is
  `workflow_dispatch` only. Session-scoped NATS connections hang in
  GitHub Actions' asyncio environment. Run it locally before merging
  relay changes instead; don't wait for a CI green that will never
  come from this tier.
- **Tier 4 (SDK)** — local-only *by design*, not a gap. It costs real
  money per test; it will never be a default CI job.
- **Tier 3b (Docker image)** — the one gap that's now closed (biff-kmv).
  It runs in CI on every push/PR, the same trigger as the existing
  subprocess job (`relay-image` job in `subprocess-tests.yml`).

## Fixture model

Every tier above unit provides `kai` and `eric` fixtures — two users
sharing state through whatever transport that tier exercises.

| Tier | Fixture type | Key method |
|------|-------------|------------|
| Commands | `CliContext` + `LocalRelay` | `await commands.who(ctx)` |
| Integration | `RecordingClient` | `await kai.call("who")` |
| CLI multi-user | `CliContext` + `NatsRelay` | `await commands.who(kai)` |
| Subprocess | `RecordingClient` | `await kai.call("who")` |
| Local NATS E2E | `RecordingClient` | `await kai.call("who")` |
| Hosted NATS | `RecordingClient` | `await kai.call("who")` |
| SDK | `SDKClient` | `await kai.prompt('Call the "who" tool.')` |

`RecordingClient` wraps a FastMCP `Client` with transcript capture.
`SDKClient` wraps the Claude Agent SDK `query()` with structured result
parsing. Both record tool interactions into a shared `Transcript`.

Tests marked `@pytest.mark.transcript` auto-save human-readable
transcripts to `tests/transcripts/`.

## Z Specifications

Two formal Z specifications drive test generation via TTF partition
analysis: `docs/talk.tex` (talk handshake, conversation, hangup) and
`docs/repl.tex` (session lifecycle, dispatch, prompt gate,
notifications). Run `/z-spec:audit docs/talk.tex --test-dir=tests` (and
the same for `repl.tex`) for current partition coverage — a printed
percentage here would go stale the same way the old test counts did.

Both specs are type-checked with `fuzz` and model-checked with
`probcli` (no counter-examples, no deadlocks). Use `make fuzz`
and `make prob` to verify.

Remaining uncovered partitions are integration-level — they require a
full REPL loop and NATS subscription running together, which is what
tier 2b exists to cover; run the audit to see what's actually left.

## Running tests

```bash
# Default: tiers 1-2 (fast, no external dependencies)
uv run pytest

# Subprocess (tier 3a)
uv run pytest -m subprocess

# CLI multi-user (tier 2b, requires nats-server)
uv run pytest -m cli_multi_user

# Docker relay image (tier 3b, requires Docker)
uv run pytest -m nats_docker

# Local NATS (tier 3c, requires nats-server)
uv run pytest -m nats_local

# Hosted NATS (tier 3d, local only)
BIFF_TEST_NATS_URL=tls://connect.ngs.global \
BIFF_TEST_NATS_CREDS=src/biff/data/demo.creds \
uv run pytest -m nats_hosted -v

# SDK (tier 4, requires ANTHROPIC_API_KEY)
uv run pytest -m sdk
```

## Coverage

Coverage has two structural floors, not incidental gaps: `nats_relay.py`
needs a live NATS server (tier 3c+) to exercise at all, and
`__main__.py` contains interactive REPL/talk loops that can't be fully
driven from unit tests. Everything else is expected to be near-total —
`commands/*` in particular has no legitimate excuse for a gap, since
`LocalRelay` makes it fully unit-testable with zero infrastructure.

### Measuring coverage

```bash
# pytest-cov conflicts with beartype import hooks. Use coverage directly:
COVERAGE_CORE=sysmon uv run coverage run --source=biff -m pytest -q
uv run coverage report -m --omit="*/testing/*,*/data/*"
```

Run this yourself for current numbers per module — a frozen table here
is exactly the kind of thing this file no longer does.
