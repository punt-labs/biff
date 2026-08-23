# Installing Biff

## Requirements

- **Python 3.13+**
- **macOS or Linux** (Windows is not supported)
- **Claude Code** CLI (`claude` on PATH)
- **GitHub CLI** (`gh auth login` completed --- biff resolves your identity from GitHub)

## One-Line Install

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/419ac99/install.sh | sh
```

The commit hash pins the installer to an auditable snapshot. It is updated on each release.

This installs [uv](https://docs.astral.sh/uv/) (if missing), installs `punt-biff` as a uv tool, registers the Claude Code plugin, and runs `biff doctor` to verify.

Restart Claude Code twice after installing:

1. First restart --- SessionStart hook runs initial setup
2. Second restart --- slash commands become active (`/who`, `/write`, etc.)

### Verify before running

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/419ac99/install.sh -o install.sh
shasum -a 256 install.sh
cat install.sh
sh install.sh
```

## Manual Install

If you already have `uv`:

```bash
uv tool install punt-biff
biff install
biff doctor
```

Or with pip:

```bash
pip install punt-biff
biff install
biff doctor
```

## What `biff install` Does

1. Registers the biff MCP server with Claude Code
2. Installs slash commands (`/who`, `/write`, `/read`, etc.)
3. Installs hooks (SessionStart, PostToolUse, SessionEnd)
4. Sets up the status bar (wraps your existing status line)

All files are deployed to `~/.claude/plugins/biff/`. The MCP server runs automatically when Claude Code starts.

## Enabling Biff in a Repo

Biff starts dormant in every repo. To enable it:

```text
> /biff enable
```

Or from the CLI:

```bash
biff enable
```

This fully activates biff in one verb: it writes the two committed enablement artifacts (commit them so biff is on for everyone who clones the repo) — the `.punt-labs/biff/enabled` marker and the `.github/workflows/biff-notify.yml` CI workflow — **and** deploys this clone's local git hooks. It is exactly equivalent to `/biff enable` in Claude Code; `biff disable` / `/biff disable` remove all three.

The per-clone git hooks (deployed by `biff enable`, or by the superset `biff install`) live in `.git/hooks/`, are never committed, and each clone deploys its own. They are resolved worktree/`core.hooksPath`-aware:

- **post-checkout** --- updates your plan when you switch branches
- **post-commit** --- updates your plan with the latest commit message
- **pre-push** --- suggests a `/wall` announcement when pushing to main

All hooks coexist with existing git hooks, gate on the committed marker, and are silent when biff is not enabled.

## Team Configuration

Commit `.punt-labs/biff/config.yaml` in your repo root:

```yaml
team:
  members: ["kai", "eric", "priya"]

relay:
  url: "tls://connect.ngs.global"
```

The `members` list controls who appears in `/who`. The `relay` section configures the NATS server for cross-machine communication.

Biff ships with a shared demo relay on Synadia Cloud so your team can start immediately.

## Relay Configuration

The demo relay works out of the box. To run your own NATS server, set the URL in the committed `config.yaml`:

```yaml
relay:
  url: "tls://your-nats-server:4222"
```

Authentication goes in the gitignored `.punt-labs/biff/config.local.yaml` instead — never in the committed file (pick at most one):

```yaml
relay:
  auth:
    token: "s3cret"                          # shared secret
    # nkeys_seed: "/path/to/user.nk"          # NKey seed file
    # credentials: "/path/to/user.creds"      # JWT + NKey creds (Synadia Cloud)
```

Use `nats://` for unencrypted local connections, `tls://` for encrypted remote connections.

If your relay sits behind a TLS-terminating load balancer or proxy (rather than a `nats-server` doing its own native TLS), also set `relay.tls_handshake_first: true` in `config.yaml` — without it, connections hang indefinitely. See [self-hosted-relay.md](self-hosted-relay.md#tls-behind-a-load-balancer) for why.

### Environment-variable relay overrides (CI, containers, systemd)

For headless invocations — a CI runner, a container, a systemd unit — where committing `config.yaml`/`config.local.yaml` isn't practical, five environment variables override the relay config for that process only:

| Variable | Maps to | Notes |
|---|---|---|
| `BIFF_RELAY_URL` | `relay.url` | e.g. `tls://relay.example.com:4222`. Never embed `user:pass@` — it is not masked in most CI variable UIs. |
| `BIFF_RELAY_TOKEN` | `relay.auth.token` | mutually exclusive with the next two |
| `BIFF_RELAY_NKEYS_SEED` | `relay.auth.nkeys_seed` | path to a `.nk` file, must exist at run time |
| `BIFF_RELAY_USER_CREDENTIALS` | `relay.auth.user_credentials` | path to a `.creds` file |
| `BIFF_RELAY_TLS_HANDSHAKE_FIRST` | `relay.tls_handshake_first` | `1`/`true`/`yes` → `true`; `0`/`false`/`no` → `false`; unset → unchanged |

Precedence, lowest to highest: `config.yaml` < `config.local.yaml` < `BIFF_RELAY_*` env vars < the `--relay-url` CLI flag (`serve`/`mcp` only).

**Repo-scoping gate.** Env vars only take effect when the SHARED `config.yaml` has no committed `relay.url` of its own, or that same shared file explicitly opts in:

```yaml
relay:
  url: "tls://your-nats-server:4222"
  allow_env_override: true   # required for BIFF_RELAY_* to override the line above
```

Without `allow_env_override: true`, a committed `relay.url` always wins — an ambient env var (a stray `export`, a misconfigured systemd unit) cannot silently redirect a team's traffic to a different relay. A personal relay entry in the gitignored `config.local.yaml` does **not** close this gate — only a relay committed in the shared `config.yaml` does.

**Auth requires a URL.** Setting one of the three auth vars without a resolved relay URL (from `BIFF_RELAY_URL` or the file layers) fails fast with a clear error, rather than silently sending that credential to the bundled demo relay.

**Human interactive use:** put `BIFF_RELAY_*` in that repo's `.envrc.local` (gitignored, direnv-scoped — unloaded automatically on `cd` out of the repo), not in a bare shell `export` or a shell profile such as `~/.zshrc`. A bare export is ambient for the rest of that shell session across every repo you `cd` into, which can leak one team's credential to another team's relay. See [relay-env-overrides.md](relay-env-overrides.md) for the full design rationale.

## Updating

```bash
uv tool install --force punt-biff
biff install
```

Or if installed via the plugin marketplace:

```bash
claude plugin update biff@punt-labs
```

## Uninstalling

```bash
biff uninstall
uv tool uninstall punt-biff
```

This removes the MCP server registration, slash commands, hooks, and status bar integration. Your messages on the relay are ephemeral and expire automatically.

## Verifying Your Installation

Run `biff doctor` at any time to check:

```bash
biff doctor
```

It runs ten checks (two required: plugin installed, NATS relay reachable; the rest informational): `gh` CLI, plugin installation, user commands, agent guide `@`-import, NATS relay, `.punt-labs/biff/config.yaml`, enablement, git hooks, CI workflow, and status line.
