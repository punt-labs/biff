# biff

> Team messaging and presence commands for the terminal and Claude Code.

[![License](https://img.shields.io/github/license/punt-labs/biff)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/punt-labs/biff/test.yml?label=CI)](https://github.com/punt-labs/biff/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/punt-biff)](https://pypi.org/project/punt-biff/)
[![Python](https://img.shields.io/pypi/pyversions/punt-biff)](https://pypi.org/project/punt-biff/)
[![Working Backwards](https://img.shields.io/badge/Working_Backwards-hypothesis-lightgrey)](./prfaq.pdf)

<img src="https://raw.githubusercontent.com/punt-labs/biff/main/docs/biff.png" alt="The original biff mail notification app" width="116" align="right">

Biff gives a software team, and the coding agents working with it, the Unix communication commands `who`, `finger`, `write`, `talk`, `wall`, `mesg`, `last` and `.plan`. You use them as slash commands in Claude Code, as `biff` subcommands in any terminal, or in biff's interactive shell. Sessions on different machines see each other through a NATS relay (a message server); biff ships with a shared demo relay. The name comes from the 1980 Berkeley mail notification program, part of the same BSD family as `talk` and `wall`.

**Platforms:** macOS, Linux

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/7fb42c8/install.sh | sh
```

Restart Claude Code twice. Biff is off in every repo until you turn it on, so in your repo run:

```text
/biff enable
```

Then type `/who` to see your team.

### CLI Only (No Claude Code Plugin)

To install the biff CLI and MCP server without the Claude Code plugin (for another AI coding tool such as Codex or Cursor, a plain terminal, or an organization that blocks plugins), pass `--no-plugin`:

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/7fb42c8/install.sh | sh -s -- --no-plugin
```

The `BIFF_NO_PLUGIN=1` environment variable does the same, for contexts that cannot pass arguments through the pipe:

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/7fb42c8/install.sh | BIFF_NO_PLUGIN=1 sh
```

The installer also skips the plugin when `claude` or `git` is missing. Without the plugin it still installs the `biff` command, adds it to your PATH and runs the health check. To add the plugin later, run these two commands:

```bash
claude plugin marketplace add punt-labs/claude-plugins
```

```bash
claude plugin install biff@punt-labs
```

<details>
<summary>Manual install (if you already have uv)</summary>

```bash
uv tool install punt-biff
```

```bash
biff install
```

```bash
biff doctor
```

</details>

<details>
<summary>Homebrew install (macOS/Linux)</summary>

```bash
brew tap punt-labs/tap
```

```bash
brew install punt-labs/tap/biff
```

```bash
biff install
```

```bash
biff doctor
```

</details>

<details>
<summary>Verify before running</summary>

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/7fb42c8/install.sh -o install.sh
```

```bash
shasum -a 256 install.sh
```

```bash
cat install.sh
```

```bash
sh install.sh
```

</details>

## Features

- **Claude Code slash commands** --- `/who`, `/write`, `/read`, `/talk`, `/wall` and the rest run inside Claude Code through biff's MCP server; there is no separate app
- **CLI and interactive shell** --- every slash command has a `biff` subcommand; `biff` on its own starts a shell with history, tab completion and live notifications
- **Cross-machine presence and messaging** --- sessions connect through a NATS relay over TLS
- **Agents and humans in one list** --- agent sessions appear in `/who` next to people, set a `/plan`, and send and receive `/write` and `/talk`
- **Status bar** --- an opt-in addition to your Claude Code status line showing unread count, wall broadcasts and talk messages
- **Per-repo switch** --- `/biff enable` writes a committed marker file, so biff is on for everyone who clones the repo; a `.punt-labs/biff/config.yaml` file is optional

## What It Looks Like

### See Who's Online

```text
> /who

▶  NAME          K    MGR  REPO   IDLE  S  P  HOST
   kai:tty1                myapp  0m    +  +  m2-mb-air
   eric:tty2               myapp  1h    +  +  m2-mb-air
   priya:tty1              myapp  0m    -  +  priya-mbp
   claude:tty3   [A]  kai  myapp  3m    +  +  m2-mb-air
```

| Column | Meaning |
|--------|---------|
| `NAME` | Address to use with `/write` and `/talk` (`user:tty`) |
| `K` | `[A]` marks an agent session; blank for people |
| `MGR` | On agent rows, the person behind the agent (from `git config user.name`, or the OS user); hidden when no row has a value |
| `S` | `+` accepting messages, `-` do not disturb (`/mesg n`) |
| `P` | `+` the session has a plan; `/finger` shows it |

### Send a Message

```text
> /write kai "auth module looks good, just one nit on the error handling"

Message sent to kai.
```

### Check Your Inbox

```text
> /read

▶  3 new messages
   FROM        DATE              MESSAGE
   kai:tty1    Sat Feb 15 14:01  hey, ready for review?
   eric:tty2   Sat Feb 15 13:45  pushed the fix for the flaky test
   priya:tty1  Sat Feb 15 12:30  can you look at the migration script?
```

<details>
<summary>More examples: /finger, /plan, /last, /wall, /talk, /mesg</summary>

### Check What Someone Is Working On

```text
> /finger kai

▶  Login: kai                              Messages: on
   On since Sat Feb 15 14:01 (UTC) on tty1, idle 0:03
   Host: m2-mb-air  Dir: /Users/kai/code/myapp
   Plan:
    refactoring auth module
```

When the repo has ethos team data (`.punt-labs/ethos/teams/*.yaml`), `/finger` also shows the agent's role (a `Roles:` block when it has several). For an agent, `Launched by:` names the OS user who started it:

```text
> /finger claude

▶  Login: claude [agent]                   Messages: on
   Role: coo (engineering) — reports to ceo (jfreeman)
   On since Sat Feb 15 14:01 (UTC) on tty2, idle 0:03
   Launched by: kai
   No Plan.
```

### Set Your Status

```text
> /plan "debugging the websocket reconnect logic"

Plan: debugging the websocket reconnect logic
```

A [beads](https://github.com/steveyegge/beads) issue ID expands to the issue title:

```text
> /plan biff-ka4

Plan: biff-ka4: post-checkout hook: update plan from branch
```

### Session History

```text
> /last

▶  NAME       REPO   HOST       LOGIN             LOGOUT            DURATION
   kai:tty3   myapp  m2-mb-air  Sat Feb 22 14:01  still logged in   -
   kai:tty2   myapp  m2-mb-air  Sat Feb 22 11:30  Sat Feb 22 13:58  2:28
   eric:tty1  myapp  m2-mb-air  Sat Feb 22 09:15  Sat Feb 22 12:45  3:30
```

### Broadcast to the Team

```text
> /wall "release freeze --- do not push to main" 2h

Wall posted (2h):
   release freeze --- do not push to main
```

Teammates with the biff status bar installed see `WALL: release freeze` in bold red until the wall expires.

### Talk in Real Time

```text
> /talk kai "can you review PR #42?"

Invite sent to kai. When they accept, talk_read shows replies.
```

Talk works like BSD `talk`: you send an invite, and the conversation starts when the other side accepts. Talk messages are not stored; they reach only a session that is connected at the time. Either side can end the conversation.

Agent sessions in Claude Code receive talk invites and messages too: `/talk claude:tty3` reaches an agent. Biff does not interrupt an agent; the agent sees the invite on its next turn. Run `/read 1m` in the agent's session to have it check every minute, or `/read` to check now. See [Agent Workflow](docs/AGENT_WORKFLOW.md#real-time-agent-conversation) for details.

### Go Do-Not-Disturb

```text
> /mesg n

is n
```

Your status bar shows `(n)` instead of the unread count. Messages still arrive; `/mesg y` or `/read` shows them.

</details>

## Commands

| Command | Origin | What it does |
|---------|--------|--------------|
| `/who` | BSD `who` | List active sessions |
| `/finger user` | BSD `finger` | Show a user's sessions, status and plan |
| `/write user "text"` | BSD `write` | Send a message |
| `/read [duration \| n \| status]` | BSD `from` | Check your inbox; a duration such as `5m` checks on that interval, `n` stops, `status` shows the interval |
| `/plan "text"` | BSD `.plan` | Set your status line |
| `/tty name` | BSD `tty` | Name the current session |
| `/last [user] [count]` | BSD `last` | Show session login/logout history |
| `/wall "text" [duration]` | BSD `wall` | Broadcast to the team; `/wall clear` removes it |
| `/talk user ["message"]` | BSD `talk` | Start a real-time conversation |
| `/mesg y` \| `/mesg n` | BSD `mesg` | Accept or block messages |
| `/biff enable` \| `/biff disable` | --- | Turn biff on or off for this repo (same as `biff enable` / `biff disable`) |

Each command is also available with the plugin prefix, for example `/biff:read`.

### CLI

Every slash command has a matching `biff` subcommand. The CLI works outside Claude Code: in any terminal, SSH session or CI job.

| Command | What it does |
|---------|--------------|
| `biff` | Start the interactive shell |
| `biff who` | List active sessions |
| `biff finger kai` | Show what kai is working on |
| `biff write kai "review the PR"` | Send a message |
| `biff read` | Check your inbox |
| `biff plan "debugging websocket reconnect"` | Set your status (`--clear` removes it) |
| `biff last kai --count 10` | Session history, optionally filtered by user (default 25 entries) |
| `biff wall "deploy freeze" --duration 2h` | Broadcast to the team |
| `biff wall` | Show the active wall |
| `biff wall --clear` | Remove the active wall |
| `biff mesg off` | Go do-not-disturb (`on` to accept messages again; `y`/`n` also work) |
| `biff tty dev` | Name this session `dev` |
| `biff status` | Connection state and unread count |
| `biff talk kai "can you review?"` | Real-time conversation |

Setup and maintenance commands:

| Command | What it does |
|---------|--------------|
| `biff install` | Install the Claude Code plugin for your user account; changes nothing in any repo |
| `biff enable` | Turn biff on for the current repo (see [Setup](#setup)) |
| `biff disable` | Turn biff off for the current repo and remove what `enable` added |
| `biff doctor` | Check the installation |
| `biff install-statusline` | Add biff to your Claude Code status line |
| `biff uninstall-statusline` | Restore your original status line |
| `biff uninstall` | Remove the plugin and this clone's biff git hooks |
| `biff mcp` | Start the MCP server over stdio (the plugin runs this) |
| `biff serve` | Start the MCP server over HTTP (default `127.0.0.1:8419`) |
| `biff version` | Print the package version, and the plugin version if installed |

The MCP server remains available for initialization and tool discovery when the
relay is unreachable or rejects authentication. Tool calls report a relay
connection failure until connectivity is restored; established connections keep
reconnecting indefinitely after a disconnect.

Global flags go before the subcommand:

| Flag | What it does |
|------|--------------|
| `--json` | Print JSON instead of text, e.g. `biff --json who` |
| `--verbose`, `-v` | Debug logging to stderr |
| `--quiet`, `-q` | Suppress command output |
| `--user name` | Act as another identity, e.g. `biff --user github-actions wall "CI failed"` for a bot |
| `--no-telemetry` | Disable local usage capture for this invocation |
| `--version` | Print the version and exit |

On a Devin VM, GitHub may not return a user for its integration credentials.
Run `biff --user devin who` (and pass `--user devin` to other biff commands)
to use the team handle instead of the VM's OS username. GitHub remotes
rewritten through Devin's Git proxy still use the GitHub owner and repo for
presence and peer discovery.

### Diagnostics and usage

The CLI (including the REPL and status line), MCP server (stdio or HTTP),
and hooks (including detached SessionEnd cleanup) write diagnostics to the
same private file, `~/.punt-labs/biff/logs/biff.log`. The file records INFO
and above and rotates at 5 MiB with five backups. Terminal diagnostics go to
stderr (WARNING and above by default); MCP stdio stdout is for protocol
messages only. Biff no longer writes a separate `hook-errors.log`.

Local usage capture is on by default. It records supported CLI command
names and MCP tool names, durations and protocol outcomes in a separate
private SQLite store, `~/.punt-labs/biff/telemetry.db`, for 90 days. It
does not record command arguments, tool results, identities, or messages,
and Biff does not send usage data over the network. Set `PUNT_TELEMETRY=off`
or `BIFF_TELEMETRY=off`, or pass `biff --no-telemetry <command>` to disable
capture. Any off switch wins.

### Interactive Shell

Run `biff` with no arguments:

```text
biff <version> — kai:tty1
Commands: finger, last, mesg, plan, read, status, tty, wall, who, write, talk, timestamps, exit

kai:tty1 ▶ who
▶  NAME       K  REPO   IDLE  S  P  HOST
   kai:tty1      myapp  0m    +  +  m2-mb-air
   eric:tty2     myapp  1h    +  +  m2-mb-air
kai:tty1 ▶ timestamps on
Timestamps on.
kai:tty1 ▶ talk eric
Waiting for eric:tty2 to respond... (type 'end' to cancel)
Connected to eric:tty2. Type 'end' to return to REPL.

kai:tty1 ▶ can you look at the auth fix?
[14:32] eric:tty2 ▶ on it now
kai:tty1 ▶ end
Talk with eric:tty2 ended.
kai:tty1 ▶ exit
```

The shell logs your session in and out, keeps your presence current, and prints wall broadcasts and new-message alerts while you sit at the prompt. It has line editing, history (kept in `~/.punt-labs/biff/repl_history`) and tab completion. `timestamps on` / `timestamps off` adds a local `[HH:MM]` stamp to incoming talk messages for the current shell only.

### Named sessions

Commands from one shell share one durable session with one `ttyN`: the session, its presence row and its tty name outlive each `biff` process. A session unused for 8 hours goes dormant (name and inbox kept; the next command revives it), and a dormant session is removed after 3 days.

```bash
biff session new --tty main     # Create a session named "main" (errors if the name is taken)
biff session ls                 # This host's sessions: name, user, repo, state, id
biff session use main           # Make "main" this repo's default (no attach)
biff session end main           # End it; unread mail moves to your user mailbox
biff -s main write kai "hi"     # Run one command as session "main" (or set BIFF_SESSION)
```

Without `-s`, a command runs as the first of: `BIFF_SESSION`; the Claude Code session it is run from (`CLAUDE_CODE_SESSION_ID`, as a client of that session, creating nothing); this repo's default (`biff session use`, or the last `session new`); a fresh session. An explicit `-s` naming an unknown or ended session is an error and never creates one; an ended session is never revived, but `biff session new --tty <name>` may reuse the name.

## Setup

Biff needs a git repo and a GitHub identity. It reads your username and display name from `gh auth`.

Without a configuration file, biff uses the shared demo relay and finds peer repos in the GitHub organization that owns the repo's remote. Add a configuration file when you want a fixed team roster, your own relay, or a different set of peer repos. Commit it as `.punt-labs/biff/config.yaml`:

```yaml
team:
  members: ["kai", "eric", "priya"]

relay:
  url: "tls://connect.ngs.global"

peers:
  orgs: ["my-org"]              # find repos with active sessions in this GitHub org
  repos: ["my-org/other-repo"]  # repos that are always visible
```

The `peers:` key makes `/who`, `/write` and `/finger` reach sessions in other repos. `orgs` finds repos with active sessions; `repos` lists repos that are always visible. Both are optional.

Mail is kept per repo, and `read` drains the mailbox of the repo you run it in. So `/write user` (no tty) delivers to the repo where that user has a session: yours if they have one here, otherwise their most recently used repo among those visible. A user with no session in any visible repo gets it in your repo's mailbox.

Put per-user relay credentials (a token, NKeys seed or credentials file path) in the gitignored `.punt-labs/biff/config.local.yaml`, never in the committed file. See [Installing](docs/INSTALLING.md) for the schema, [relay configuration](docs/INSTALLING.md#relay-configuration) to run your own relay, and [environment-variable relay overrides](docs/INSTALLING.md#environment-variable-relay-overrides-ci-containers-systemd) for the five `BIFF_RELAY_*` variables used in CI and containers.

What the two setup commands change:

- **`biff install`** (once per machine) installs the biff Claude Code plugin for your user account. The plugin provides the MCP server, slash commands and Claude Code hooks. It changes nothing in any repo. If Claude Code is not installed, it reports a CLI-only install and stops.
- **`biff enable`** (once per repo, same as `/biff enable`):
  - writes `.punt-labs/biff/enabled` (the on switch), `.punt-labs/biff/CLAUDE.md` (a guide for agents) and `.github/workflows/biff-notify.yml` (a CI workflow); commit these
  - adds an `@.punt-labs/biff/CLAUDE.md` import line to the repo's `CLAUDE.md`
  - adds biff's permission rules to the repo's `.claude/settings.json`; biff commands that change settings still ask for your approval
  - installs git hooks in this clone's `.git/hooks/` that update your plan on checkout and commit; the hooks are not committed, so each clone runs `biff enable` once

  `biff disable` removes all of it.

Run `biff doctor` to check the result. [Installing](docs/INSTALLING.md) has the full guide.

### Devin

An organization admin can install the Devin plugin from
<https://github.com/punt-labs/biff/tree/main/devin-plugin>, binding its
MCP installation environment to
`MCP_BIFF_RELAY_TOKEN: secret:org:MCP_BIFF_RELAY_TOKEN`. The MCP config maps
that secret placeholder to `BIFF_RELAY_TOKEN`; empty or unexpanded values
are unset rather than sent to the relay. Never put a token value in files.
After installing the plugin, open **Customize > MCPs > From plugins > biff >
Connect** and save the relay token as `MCP_BIFF_RELAY_TOKEN`. Devin fills
plugin MCP placeholders only from values saved on that plugin installation.
The launcher uses installed biff 1.24.0 or newer, otherwise requests
`punt-biff>=1.24.0` through `uvx` (or `uv tool run`), falling back to plain
`biff` only when neither runner exists. Install uv or a current biff first.
It supplies a fresh session ID when absent so startup can register presence.
Explicit `biff mcp --user devin` binds unstamped tool calls to that same session;
stamped calls keep their declared caller and malformed stamps are refused.

The plugin provides a long-lived MCP server, who/read/write/wall skills,
and an always-on five-minute mail polling rule. Its self-contained SessionStart
command is inline in `hooks.json`, requiring no plugin-root variable. It
creates missing local team configuration and Devin identity records in
`DEVIN_PROJECT_DIR` and repositories under `~/repos/`, excluding newly
created files from Git without overwriting existing configuration. It
does not run `biff enable` or change repository enablement policy.

## Status Bar

`biff install-statusline` adds biff to the end of your existing Claude Code status line; it does not replace it. `biff uninstall-statusline` restores the original.

```text
Line 1: your-existing-status | kai:tty1(3)
Line 2: ▶ WALL: release freeze until 5pm
```

`kai:tty1(0)` means no unread messages, **`kai:tty1(3)`** (bold yellow) means three unread, and `kai:tty1(n)` means messages are off. Line 2 shows the latest `/talk` message (bold yellow), an active wall (bold red), or `▶` when there is nothing to show.

## Agents Welcome

Biff treats an agent's Claude Code session like any other session. An agent can set a `/plan` for its current task, `/write` a person when it needs a decision, and appear in `/who` with everyone else.

- **Across machines:** `/who` lists every session's plan, so an agent or a person can see what others are working on before starting a task.
- **On one machine:** `/who` and `/finger` show each session's host and directory. Agents sharing a machine and directory can agree on who does what with `/write` and use separate git worktrees.

See [Agent Workflow](docs/AGENT_WORKFLOW.md) for patterns and examples.

## Vision

Biff assumes the terminal is where you're already working, so that is where your team communication should be. There are no channels to monitor, no threads to catch up on, no emoji reactions to parse. Communication is pull-based: you decide when to engage.

## Documentation

[Installing](docs/INSTALLING.md) |
[Agent Workflow](docs/AGENT_WORKFLOW.md) |
[Claude Setup](docs/CLAUDE_SETUP.md) |
[FAQ](docs/FAQ.md) |
[Troubleshooting](docs/TROUBLESHOOTING.md) |
[Roadmap](docs/ROADMAP.md) |
[Changelog](CHANGELOG.md)

For contributors:
[Contributing](CONTRIBUTING.md) |
[Library API](docs/LIBRARY.md) |
[Design Log](DESIGN.md) |
[Installer Design](DESIGN-INSTALLER.md) |
[Testing Guide](TESTING.md)

## License

MIT
