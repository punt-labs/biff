# Testing

This guide applies Punt Kit's [Testing standard][standard] and
[Python rules][python-rules]. Run `make check` before opening a PR.

Biff has a nine-tier testing pyramid. Each tier tests a different
boundary, at a different transport, with different infrastructure
requirements. Run `uv run pytest --collect-only` for current test
counts per tier — this file doesn't print them, because they drift
with every PR and a stale number in a doc is worse than no number.

Every tier above unit/integration has its own pytest marker, and no
marker is shared between two different tiers — `pytest -m <marker>`
always selects exactly one tier, never a superset.

## Trigger map (TST-1, TST-3)

| Trigger | Entry point and runner | Budget target | Result |
| --- | --- | --- | --- |
| Pre-commit | `make test-changed` on the developer machine | <30 s for a typical change | Blocks commit when the hook is installed |
| Pre-push | `make test-push` (default suite) on the developer machine | <3 min | Blocks push when the hook is installed |
| PR | [Test workflow][test-ci]: default suite with coverage, `make test-wheel` on Linux and macOS; [Subprocess workflow][subprocess-ci]: tiers 3a and 3b | <10 min wall time | Required `test` gate; `subprocess` and `relay-image` checks |
| Merge to main | The same workflows on the main SHA | <10 min wall time | Repeat the PR gate; keep SHA-keyed coverage |
| Nightly | [Nightly QA][nightly-ci] on upstream main, Ubuntu | 30 min local job; 15 min hosted and agent jobs; 5 min alert | Advisory for PRs; red run and a biff `wall` on failure |
| Release candidate | [Release workflow][release-ci]: `bash scripts/smoke-release.sh candidate VERSION WHEEL`, tier 3b on the candidate image digest | 10 min smoke job; 20 min image tests | Blocks PyPI upload and the image retag |
| Post-release | [Release workflow][release-ci]: `bash scripts/smoke-release.sh published VERSION`, tier 3b on the pulled image | <2 min target; 10 min job timeout | Red release run; yank or ship a fix |

Install the versioned hooks with `make hooks`. This refuses to change an
existing custom hook path; chain the biff test hooks from those hooks
instead. The hooks run any hook already in `.git/hooks`, and preserve the
default `commit-msg` hook, including Ethos trailers, unless Ethos already
handles trailers in the active hook.

Pre-commit selects tests from staged path names and runs them against the
working tree, so stage and test the same changes. A changed test runs
itself; a changed module runs the tests that import it. Shared fixtures,
configuration, tooling, scripts and unmapped source fall back to the whole
default suite. A warm one-file change (`src/biff/tty.py`) took 5 s in the
rollout Linux VM. The default suite took about 75 s warm there, so pre-push
fits its budget; the shared-file fallback can exceed the pre-commit target.

## Select a suite (TST-2)

| Suite | Selector | Prerequisites and scope |
| --- | --- | --- |
| Default | `make test` | Dev extra; tiers 1 and 2 |
| Unit | `make test-unit` | Tier 1: everything outside `tests/test_integration` |
| Integration | `make test-integration` | Tier 2: in-process FastMCP client and `LocalRelay` |
| Subprocess | `make test-subprocess` | Tier 3a: real `biff mcp` children over stdio |
| CLI multi-user | `make test-cli-multi-user` | Tier 2b: `nats-server` on `PATH` |
| Relay image | `make test-docker` | Tier 3b: Docker; builds the image unless `BIFF_RELAY_IMAGE` is set |
| Local NATS | `make test-nats-local` | Tier 3c: `nats-server` on `PATH` |
| E2E | `make test-e2e` | Installed wheel plus tiers 2b and 3c |
| Live | `make test-live`; `make test-stress` | Tier 3d: `BIFF_TEST_NATS_URL` plus `BIFF_TEST_NATS_CREDS`, `_TOKEN` or `_NKEYS_SEED`; never a PR test |
| Agent | `make test-agent` | Tier 4: scoped `ANTHROPIC_API_KEY` and the Claude Agent SDK |
| Channels | `make test-channels` | Tier 5: `tmux`, `nats-server` and a logged-in `claude` on `PATH` |
| Nightly | `make test-nightly` | Tiers 3a, 2b and 3c; `nats-server` on `PATH` |
| Quarantine | `make test-quarantine`; `uv run pytest -m quarantine` | List marks, or run them with their own prerequisites |

The suites that need `nats-server` or Docker skip themselves when it is
missing. Nightly installs a pinned, checksum-verified `nats-server`. Add
`and not quarantine` to any hand-written marker expression.

## Trusted tests and quarantines (TST-6, TST-9)

Nightly runs the hosted tier against the public demo relay, whose
credentials ship in the package, and caps each SDK query at three turns and
$0.05 (`tests/test_sdk/_client.py`). The provider key reaches only the
agent step; the biff MCP child gets none. The relay token reaches only the
failure alert. Do not run trusted tests on a PR or pass personal
credentials to them. The [Hosted NATS workflow][hosted-ci] reruns the
hosted tier, and optionally the stress subset, on demand.

Quarantine a confirmed flaky test, not an entire optional tier. Register
`@pytest.mark.quarantine(issue="PL-1234", owner="Name", expires="YYYY-MM-DD")`.
The expiry must be within 14 days; review the list on Nightly and fix or
remove the case by expiry. An expired or malformed mark fails collection.

## Open deviations (TST-5, TST-7, TST-8, TST-10)

- **Candidate scope.** The candidate and published checks exercise the CLI,
  the MCP server over stdio and the relay image, but not the hosted relay
  or a real Claude session against the exact candidate, nor the published
  plugin through a supported harness.
- **Coverage scope.** Coverage does not count child `biff mcp` processes or
  installed wheels (see [Coverage](#coverage)).
- **Not applicable.** Biff has no browser UI; CLI, MCP and the relay apply.

The [tracking issue][deviations] records those deviations. Owner: biff
maintainers. Review by 2026-12-07.

## Pyramid

```text
                    ┌───────────┐
                    │  Tier 5   │  Channels — a real claude TUI in tmux
                    ├───────────┤
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
| 5 | `claude_channels` | `tmux`, `nats-server` and a logged-in interactive `claude` |
| Manual | `systemd_user` | Linux with a running systemd user manager (`systemctl --user show-environment` exits 0). The test registers a uniquely named throwaway unit for biffd in `~/.config/systemd/user`, runs the real daemon under it, and removes both. Run with `make test-systemd`; no default run selects it |
| Manual | `keychain` | macOS and a person at the screen: the tests use the real keychain and raise a dialog. Run by hand with `pytest -m keychain`; no default run selects them |

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
  from Z specification partition analysis (docs/specs/talk.tex).
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

**Push-vs-poll demo** (`tests/test_relay_image/test_push_demo.py`,
biff-5ex/DES-062): the same push-vs-poll comparison as tier 3c's
`test_push_vs_poll_latency.py`, run end to end against the real
container instead of a bare `nats-server`. Marked
`@pytest.mark.transcript`, so it also saves a human-readable transcript
to `tests/transcripts/`. Run it directly, or via the one-command
wrapper:

```bash
./scripts/demo-push-vs-poll.sh
```

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

**Push-vs-poll comparison** (`test_push_vs_poll_latency.py`,
biff-5ex/DES-062): demonstrates the measurable push-detection claim
with an A/B lever on the sender side only — one arm publishes a
broadcast message straight to JetStream (bypassing `deliver()`'s wake
poke, the pre-biff-5ex shape), the other goes through the real
`deliver()`. The receiving MCP server is identical and honest in both
arms; the test prints a side-by-side latency comparison table and
separately demonstrates the companion load claim (steady-state
`get_unread_summary` call volume with no arrivals).

### Tier 3d: Hosted NATS E2E

Same as 3c but against a real remote relay — the shared demo relay on
Synadia Cloud by default, or any hosted/self-hosted NATS server you
point it at. Nightly QA runs it against the demo relay; run it locally
before merging relay changes (see [Trigger map](#trigger-map-tst-1-tst-3)).

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

### Tier 5: Claude channels

`tests/test_claude_channels/`, marker `claude_channels`. Channel events
reach only an interactive Claude Code session, never the Agent SDK or a raw
stdio client, so this tier starts a real `claude` in a detached tmux session
and reads its screen.

```bash
make test-channels
```

The rig (`tests/test_claude_channels/rig.py`) builds, under `/tmp` and outside
the checkout (Claude Code imports every `CLAUDE.md` above its working
directory), a git repo with biff enabled, a private `HOME` that holds
`biff.log` and the relay state, and a copy of `plugin/` whose server command
points at this checkout's `biff`. The `biff` and `biff-hook` shims set that home, the environment drops ambient `BIFF_*`, `CLAUDECODE` and `GIT_*` variables, and git reads no global config, so the tier can run inside Claude Code and never touches the operator's biff home (the fixture fails if a file there names the rig). Claude Code runs with
`--setting-sources project,local`, so the operator's installed plugins start no
second biff server. A second user, `bob`, writes mail and posts a wall through
the `biff` CLI and publishes a talk invite as a NATS frame (no CLI verb sends
one). The test polls `tmux capture-pane` until the event text shows, fails with
the captured pane and the `biff.log` tail on timeout, and then checks that
`biff.log` recorded the event as queued (the log cannot say the client displayed it; the pane is the proof).

`BIFF_CHANNEL_LAUNCHES` (comma-separated names from `LAUNCHES` in
`launches.py`; a blank or unknown value is an error) selects which channel-flag spellings run; the default is
the verified one. The verified launch and the variants that do not display
events are in [design-channels.md](docs/design/design-channels.md#verified-launch).

**Cost**: about a minute per launch, no model tokens beyond what Claude Code
spends reacting to each event. It needs a logged-in `claude`.

## Release checks

A `v*` tag runs `.github/workflows/release.yml`. Nothing is promoted until
the exact artifact that ships has done real work installed outside the
checkout:

| Stage | Job | What it proves |
|-------|-----|----------------|
| Candidate wheel | `test-install` | `tools/verify_testpypi.py` matches TestPyPI's bytes to this run's build, then `scripts/smoke-release.sh candidate` installs that wheel (dependencies from PyPI only) into a fresh venv, `HOME` and git repo and sends and reads a message through the installed CLI and the installed `biff mcp` server over stdio. |
| Candidate relay image | `relay-image-candidate`, `relay-image-test` | The image is built once and pushed as `biff-relay:<version>-candidate`; tier 3b runs against that digest (`BIFF_RELAY_IMAGE`) on amd64 and arm64 runners. |
| Promotion | `pypi`, `docker` | PyPI upload waits on both candidate checks; the release image tag is a retag of the tested digest, never a rebuild. |
| Published package | `published-smoke` | `scripts/smoke-release.sh published` installs `punt-biff==<version>` from production PyPI on Linux and macOS and repeats the CLI and MCP round trip. |
| Published relay image | `published-image-smoke` | Pulls `biff-relay:<version>`, checks it is the candidate digest, and runs tier 3b's presence and messaging tests against it (the full tier ran on that digest before promotion). |

Run the candidate check locally with `make smoke-release`. When a
post-release check fails, read the failing step first. A `pip install` or
`docker pull` that fails before the artifact runs can be registry
propagation or an outage, not the release: rerun the job. A failed check
against the installed package or the pulled image means the published
version is broken: yank or ship a fix, never retag.

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
analysis: `docs/specs/talk.tex` (talk handshake, conversation, hangup) and
`docs/specs/repl.tex` (session lifecycle, dispatch, prompt gate,
notifications). Run `/z-spec:audit docs/specs/talk.tex --test-dir=tests` (and
the same for `repl.tex`) for current partition coverage — a printed
percentage here would go stale the same way the old test counts did.

Both specs are type-checked with `fuzz` and model-checked with
`probcli` (no counter-examples, no deadlocks). Use `make fuzz`
and `make prob` to verify.

Remaining uncovered partitions are integration-level — they require a
full REPL loop and NATS subscription running together, which is what
tier 2b exists to cover; run the audit to see what's actually left.

## Coverage

Coverage has two structural floors, not incidental gaps: `nats_relay.py`
needs a live NATS server (tier 3c+) to exercise at all, and
`__main__.py` contains interactive REPL/talk loops that can't be fully
driven from unit tests. Everything else is expected to be near-total —
`commands/*` in particular has no legitimate excuse for a gap, since
`LocalRelay` makes it fully unit-testable with zero infrastructure.

### Measuring coverage

`make coverage` writes an HTML report to `htmlcov/`; `make coverage-ci`
writes `coverage.xml`. Both measure line and branch coverage of `src/biff`
executed in the default pytest process (coverage.py directly, because
pytest-cov conflicts with beartype's import hooks). They do not count code
running in `biff mcp` children, the installed wheel, shell scripts, or the
opt-in marker tiers.

The Tests workflow runs `make coverage-ci` on every PR and on main and
uploads `coverage.xml` as the `coverage-<sha>` artifact. Run `make coverage`
yourself for current numbers per module — a frozen table here is exactly
the kind of thing this file no longer does.

### Installed wheel

`make test-wheel` builds a wheel and runs `scripts/smoke-release.sh
candidate` against it: a clean virtual environment and HOME, the installed
CLI writing and reading a message, and the installed `biff mcp` server
returning it over stdio. The Tests workflow runs it on Ubuntu and macOS for
every PR; the required `test` check fails if it or the coverage job fails.

[standard]: https://github.com/punt-labs/punt-kit/blob/main/standards/testing.md
[python-rules]: https://github.com/punt-labs/punt-kit/blob/main/lang-rules/python/python-testing.md
[test-ci]: .github/workflows/test.yml
[subprocess-ci]: .github/workflows/subprocess-tests.yml
[nightly-ci]: .github/workflows/nightly.yml
[release-ci]: .github/workflows/release.yml
[hosted-ci]: .github/workflows/hosted-nats.yml
[deviations]: https://linear.app/punt-labs/issue/PL-1428/close-biff-testing-standard-deviations
