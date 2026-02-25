# Frequently Asked Questions

## General

### What is biff and who is it for?

Biff is a terminal-native communication tool for software engineers who use Claude Code or other MCP-compatible systems. If you spend your working day in a terminal and resent switching to a browser to talk to your team, biff is for you. It provides ten slash commands covering messaging, presence, broadcast, real-time conversation, session identity, and availability control --- all without leaving your session.

### How is biff different from Slack?

Slack is a workplace chat platform designed around channels, threads, and continuous presence. It assumes you are watching. Biff is a communication tool designed around intent and focus. It assumes you are working.

In Slack, the default state is "available and monitoring." In biff, the default state is "heads down; interrupt me if it matters." Slack is optimized for managers who need visibility. Biff is optimized for engineers who need concentration.

Structurally, biff runs inside your existing development environment as MCP slash commands. There is no separate app, no browser tab, no notification center. Communication happens where your code already lives. And because team configuration lives in a `.biff` file committed to the repo, communication is scoped to the project you are working on right now. Slack shows you every channel from every project simultaneously; biff shows you only the teammates and messages relevant to the code in your terminal.

### How is biff different from Discord?

Discord is a community platform built around persistent voice and text channels. It works well for open-source communities and gaming. Biff is a team tool built around directed messages and explicit intent. Discord is for hanging out. Biff is for getting things done. Biff also runs natively inside your AI coding session --- there is no alt-tab.

### Does biff work without Claude Code?

Biff is built on the Model Context Protocol (MCP), which is an open standard. Any MCP-compatible client can use biff. Claude Code is the primary target today because it has the largest population of engineers living in the terminal. As other MCP clients mature, biff will work there too.

The `biff` CLI also provides standalone access to some features (`biff talk`, `biff who`, `biff write`) without requiring an MCP host.

## Setup

### How do I get started?

Run the one-line installer, restart Claude Code twice, and type `/who`:

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/biff/6ce60b3/install.sh | sh
```

Biff ships with a shared demo relay on Synadia Cloud, so there is no infrastructure to provision. If your repo has a `.biff` file (committed by a teammate), biff picks up the team roster automatically. Your display name is resolved from your GitHub identity. No account creation, no workspace to configure.

See [Installing](INSTALLING.md) for the full guide.

### Do I need to set up a NATS server?

No. Biff ships with a shared demo relay that works out of the box. When you're ready for your own relay (for privacy, performance, or team isolation), see [relay configuration](INSTALLING.md#relay-configuration).

### Why does biff need two restarts?

The first restart lets Claude Code discover the new plugin and run the SessionStart hook. The second restart activates the slash commands. This is a Claude Code limitation --- plugins are loaded at startup and hooks run before commands are available.

## Privacy and Security

### What happens to my messages?

Messages follow a POP mail model: they are held on the NATS relay only until the recipient reads them, then discarded. Biff is not a system of record.

The shared demo relay is hosted on Synadia Cloud. Teams that want data sovereignty can self-host a NATS server and route all traffic through their own infrastructure.

### Are messages encrypted?

Today, messages are encrypted in transit via TLS between your client and the NATS server. End-to-end encryption (NaCl/libsodium) is on the roadmap --- once shipped, the relay will store only ciphertext it cannot read.

### Does biff send data to Anthropic or any third party?

No. Biff communicates only with the NATS relay specified in your `.biff` file. No telemetry, no analytics, no data shared with Anthropic or anyone else.

## Agents

### Can agents use biff?

Yes. Because biff is an MCP server, agents participate alongside humans as natural members of the team. An autonomous coding agent can `/plan` what it is working on, `/write` a human when it needs a decision, and show up in `/who` alongside everyone else. Each agent gets a distinct identity via `/tty`, targetable via `/write @user:tty`.

See [Agent Workflow](AGENT_WORKFLOW.md) for patterns and examples.

### Is biff an agent orchestration framework?

No. Biff is the human+agent communication layer, not a swarm coordinator. For pure agent-to-agent coordination (shared memory, high-throughput task dispatch, swarm orchestration), use dedicated tools like claude-flow or other agent frameworks. Biff serves the place where humans see what their agents are doing, and agents can reach a human when they need one.

### Can I use biff with multiple agents on the same machine?

Yes. Each agent gets its own TTY session and shows up separately in `/who`. Use `/who` to see host and directory per session, and use git worktrees so agents don't edit the same files. See the [Physical Plane](AGENT_WORKFLOW.md#physical-plane-same-machine) section.

## Features

### Will biff support real-time pairing or live conversation?

`/talk` is shipped. It opens a real-time bidirectional conversation between any two biff sessions --- human to human, human to agent, or agent to agent. Uses NATS core pub/sub for instant notification. `biff talk @user` provides a standalone terminal REPL.

`/pair` --- allowing a teammate to send input to your Claude Code session with explicit consent --- is on the roadmap.

### What is biff NOT building?

- **Channels or rooms.** Communication is directed: you message a person or broadcast to the team.
- **Message history or search.** Messages are ephemeral. Biff is not a system of record.
- **Video or voice.** Biff is text. Use dedicated tools for calls.
- **Project management.** No tasks, no boards, no sprints. Use beads, Linear, or GitHub Issues.
- **A mobile app.** Biff is for engineers at their terminals.
- **Artifact sharing.** Use git, GitHub Issues, or PR comments for diffs and files.
