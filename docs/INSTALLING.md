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

Or with Homebrew (macOS/Linux):

```bash
brew tap punt-labs/tap
brew install punt-labs/tap/biff
biff install
biff doctor
```

## What `biff install` Does

1. Registers the biff MCP server with Claude Code
2. Installs slash commands (`/who`, `/write`, `/read`, etc.)
3. Installs hooks (SessionStart, PostToolUse, SessionEnd)

The status bar is opt-in: run `biff install-statusline` to wrap your existing
status line (`biff uninstall-statusline` restores it). Neither `biff install`
nor the SessionStart hook touches it, and SessionStart grants no permissions --
`biff enable` adds biff's allow rules to the repo's `.claude/settings.json`.

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

The per-clone git hooks (deployed by `biff enable`) live in `.git/hooks/`, are never committed, and each clone deploys its own. They are resolved worktree/`core.hooksPath`-aware:

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
  type: nats
  url: "tls://your-nats-server:4222"
```

The `members` list controls who appears in `/who`. The `relay` section chooses how sessions find each other; leave it out to stay on the local relay.

## Relay Configuration

With no `relay` section, biff uses the **local relay**: sessions on this machine find each other and exchange messages through files in the repo's private zone, `.punt-labs/local/biff/relay/` in the main checkout. It needs no network, no server and no credentials. The directory and its files are private to you (directories `0700`, files `0600`), and biff ignores `.punt-labs/local/biff/` in git so relay state never enters shared history. `biff doctor` and `biff status` print the directory.

A linked worktree resolves to its main checkout, so every worktree of one checkout shares one relay. Separate clones each have their own relay, even when they share a directory name; sessions in two clones of one repo reach each other only through a NATS relay. `--data-dir` overrides the directory.

To reset a repo's local relay, delete `.punt-labs/local/biff/relay/` in its main checkout. The relay is real time on the machine only; it never leaves it.

Choose another relay with `relay.type` in `config.yaml` (shared) or `config.local.yaml` (yours). These are advanced setups, for sessions that must see each other across machines.

| `relay.type` | Relay | Needs |
|--------------|-------|-------|
| `local` (the default) | Files on this machine | Nothing |
| `demo` | The shared public NATS relay on Synadia Cloud, with credentials bundled in the package | Network access; it is shared with every other demo user |
| `nats` | A NATS server you run: the [self-hosted Docker relay](self-hosted-relay.md) or any other | `relay.url`, and usually credentials |

```yaml
relay:
  type: demo
```

To use your own NATS server, set the type and URL in the committed `config.yaml`:

```yaml
relay:
  type: nats
  url: "tls://your-nats-server:4222"
```

A `relay` section with only `url` still means `type: nats`. A bare `url: ""` means the local relay.

**Which file decides.** The relay is chosen per file, not key by key: the highest-precedence file that sets `type` or `url` (`config.local.yaml` over `config.yaml`) decides which relay it is. So a personal `url: nats://mine` (no `type`) beats a committed `type: demo`, a personal `url: ""` gives the local relay whatever is committed, and a personal `type: local` beats a committed `url`. Credentials (`token`, `nkeys_seed`, `user_credentials`, `auth`) and `tls_handshake_first` come from the file that chose the relay or one above it, so a personal token applies to a committed relay but a committed token never follows you to a personal relay.

**Errors.** Biff stops with a message naming the file and the problem, and never falls back silently, when: `relay` is not a mapping; `url` is not a string; the section has an unknown key (allowed: `type`, `url`, `auth`, `token`, `nkeys_seed`, `user_credentials`, `tls_handshake_first`, `allow_env_override`); `tls_handshake_first` or `allow_env_override` is not `true` or `false`; the top level has `relays:`; `type: nats` has no `url`; the type is unknown; `type: local` or `type: demo` is combined, in one file, with `url` or any credential (`type: demo` also with `tls_handshake_first`, since your own credentials must never go to the public demo relay); or credentials are set with no relay chosen. `config.local.yaml` that is malformed YAML is ignored, as it always was.

**Upgrading from a version that defaulted to the demo relay.** A repo that set no relay used to connect to the demo relay. It now uses the local relay, and its sessions no longer see sessions on other machines. To keep the demo relay, add this to `.punt-labs/biff/config.yaml` (or to `config.local.yaml` for yourself only; a personal `url` or `type` there beats the committed one):

```yaml
relay:
  type: demo
```

Repos that already set `relay.url` are unchanged.

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

**Auth requires a URL.** Setting one of the three auth vars without a resolved relay URL (from `BIFF_RELAY_URL` or the file layers) fails fast with a clear error, rather than silently ignoring that credential on the local relay.

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
