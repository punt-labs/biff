# Claude Code Hook Lifecycle

How Claude Code dispatches hooks, which hooks block session progress,
and what that means for startup latency in Python plugins.

**Date**: 2026-03-11
**Source**: Base Z specification (`z-spec/examples/claude-code.tex`,
329K states verified), biff Layer 2 (`docs/claude-code-biff.tex`),
and measured behavior across four Punt Labs plugins.

---

## 1. The Session State Machine

Claude Code sessions move through five phases. Hook events fire at
specific phase transitions. The phase determines which hooks can fire
and whether they can block.

```text
spInactive ──SessionStart──▶ spIdle ──SubmitPrompt──▶ spProcessing
                                ▲                          │
                                │    ┌─────────────────────┘
                                │    │ (tool calls, subagents)
                                │    ▼
                                │  BeginResponse
                                │    │
                                │    ▼
                                │  spResponding ──Stop(allow)──▶ spIdle
                                │                                  │
                                │  Stop(block) ──▶ spProcessing    │
                                │                                  │
                                └──────────────────────────────────┘
                                                                   │
                                                              EndSession
                                                                   │
                                                                   ▼
                                                               spEnded
```

**Key invariant** (proven by model checker): hooks can only fire
during the phase transitions shown above. A tool call hook cannot
fire in `spIdle`. A Stop hook cannot fire in `spProcessing`.

---

## 2. Hook Events: Blocking vs Non-Blocking

A **blocking hook** is a guard condition on a state transition. Claude
Code waits for the hook's return value before proceeding. The return
value determines whether the transition completes.

A **non-blocking hook** is a side effect of a transition. It fires
during the transition but cannot prevent it. Claude Code does not
wait for the return value.

In the Z specification, blocking hooks appear as precondition inputs
on the operation schema (`hookResult? : HookDecision`). Non-blocking
hooks are noted in the narrative but do not appear in the formal
model — they cannot alter the state machine's reachable states.

### Complete hook event table

| Hook Event | Blocking? | Phase Transition | What the return value controls |
|-----------|-----------|------------------|-------------------------------|
| **SessionStart** | No | spInactive → spIdle | Nothing — observational. But `additionalContext` is injected into the session. |
| **UserPromptSubmit** | **Yes** | spIdle → spProcessing | `hdAllow` proceeds; `hdBlock` rejects the prompt. |
| **PreToolUse** | **Yes** | spProcessing (tpPreHook) | `pdAllow` runs the tool; `pdDeny` aborts with reason; `pdAsk` escalates to permission dialog. |
| **PermissionRequest** | **Yes** | spProcessing (tpPermission) | User grants or denies. |
| **PostToolUse** | No | spProcessing (tool complete) | Nothing — observational. `additionalContext` injected. |
| **PostToolUseFailure** | No | spProcessing (tool failed) | Nothing — observational. |
| **SubagentStart** | No | spProcessing (agent spawned) | Nothing — observational. |
| **SubagentStop** | **Yes** | spProcessing (agent finishing) | `hdAllow` stops the agent; `hdBlock` keeps it running. |
| **Stop** | **Yes** | spResponding → spIdle | `hdAllow` finishes response; `hdBlock` re-enters spProcessing (ForceContinue). |
| **PreCompact** | No | spIdle (context compaction) | Nothing — observational. |
| **SessionEnd** | No | spIdle → spEnded | Nothing — observational. |
| **Notification** | No | Any active phase | Nothing — external event. |

---

## 3. What "Blocking" Means for Latency

The critical distinction for startup latency:

**SessionStart is non-blocking in the formal model** — the transition
from spInactive → spIdle always succeeds. No hook can prevent it.

**But Claude Code waits for SessionStart hooks to complete before the
user can interact.** The `additionalContext` they return is injected
into the session context. Until all SessionStart hooks finish, the
session shows "Resuming conversation..." and the user cannot type.

This creates a practical paradox: SessionStart is formally
non-blocking (the state transition is unconditional) but
**operationally synchronous** (Claude Code serializes hook execution
and waits for all hooks to complete before entering spIdle).

The `"async"` flag in `hooks.json` changes this: an async hook is
fire-and-forget. Claude Code does not wait for it. But async
SessionStart hooks cannot inject `additionalContext` — the context
window has already moved past the injection point.

### Sync vs async: who pays what

```text
Sync SessionStart hook:
  Claude Code ──spawn──▶ shell ──spawn──▶ Python ──import──▶ handler
       │                                                        │
       │◀──────────── wait for additionalContext ───────────────┘
       │
       ▼ (blocked until hook returns)

Async SessionStart hook:
  Claude Code ──spawn──▶ shell ──spawn──▶ Python ──import──▶ handler
       │                                                        │
       ▼ (continues immediately)                    (runs in background)
```

**Every sync SessionStart hook adds its full execution time to session
startup.** If four plugins each register sync SessionStart hooks that
spawn Python processes, the total wait is the sum of all four (serial
within each plugin, potentially parallel across plugins — but the
slowest plugin gates the total).

---

## 4. The Hook Call Path

When Claude Code fires a hook, the execution path is:

```text
Claude Code event loop
  │
  ├─ For each registered hook (serial within plugin):
  │    │
  │    ├─ Spawn shell process (hooks/<event>.sh)
  │    │    │
  │    │    ├─ Shell precondition check (config exists? tool enabled?)
  │    │    │    exit 0 if not applicable  ← ~1ms
  │    │    │
  │    │    └─ Invoke CLI handler
  │    │         │
  │    │         ├─ [Go binary]    ~10ms total
  │    │         │
  │    │         └─ [Python CLI]   ~0.3s–4.7s depending on imports
  │    │              │
  │    │              ├─ Python interpreter startup      ~50ms
  │    │              ├─ uv wrapper overhead             ~100ms
  │    │              ├─ __init__.py imports              0–2s (eager vs lazy)
  │    │              ├─ __main__.py + CLI framework      0–1s (typer, commands)
  │    │              ├─ Handler module imports           0–2s (pydantic, nats, etc.)
  │    │              └─ Handler business logic           ~5ms (the actual work)
  │    │
  │    ├─ Read stdout (JSON response)
  │    └─ Inject additionalContext into session
  │
  └─ Session ready (user can interact)
```

**The import cost dominates.** For a typical Python plugin, 95–99% of
hook execution time is importing libraries the handler never calls.

---

## 5. Latency Budget by Hook Event

Not all hook events have the same latency sensitivity. The user
experience impact depends on when the hook fires:

| Hook Event | When User Notices | Latency Budget | Why |
|-----------|-------------------|---------------|-----|
| **SessionStart** | Session open / resume | **< 0.5s per plugin** | User is staring at "Resuming..." — every second is felt |
| **UserPromptSubmit** | After pressing Enter | < 100ms | Delays feel like input lag |
| **PreToolUse** | During Claude's turn | < 100ms | Adds to tool execution time; Claude is already working |
| **PostToolUse** | During Claude's turn | < 200ms | Same as above; slightly more budget for context injection |
| **Stop** | When Claude finishes | < 200ms | User is waiting for control to return |
| **Notification** | Idle / permission prompt | **Unlimited** | Background; user isn't waiting |
| **PreCompact** | Between turns | < 2s | Happens infrequently; user is reading |
| **SessionEnd** | Session closing | **Unlimited** | Session is ending anyway |

**SessionStart has the tightest budget and the highest cost.** It fires
on every session open, runs sync, and spawns Python processes that
import the world. This is why the import tax pattern hits hardest here.

---

## 6. The Import Tax Anti-Pattern

When the CLI handler is a Python process, the call path is:

```text
shell script
  └─ <tool> hook <event>           ← console_scripts entry point
       └─ <package>/__main__.py    ← imports CLI framework + all commands
            └─ <package>/__init__.py  ← imports all submodules
                 └─ heavy dependencies (nats, pydantic, lancedb, onnxruntime, ...)
                      └─ handler function (needs: pathlib, json)
```

Three layers compound:

1. **Eager `__init__.py`** — any submodule import triggers the full
   package. `from biff.hook import handler` loads nats, pydantic, and
   the entire server because `biff/__init__.py` eagerly imports them.

2. **`__main__.py` loads everything** — the CLI framework, all
   commands, the server, configuration. It exists to run the full CLI;
   the hook subcommand is one of many.

3. **Handler imports from heavy modules** — pure-stdlib functions live
   in modules that also import third-party libraries. Importing the
   function means importing the library.

### Measured cost (M2 MacBook Air, installed binaries)

| Plugin | Handler needs | Module imports | Cold start |
|--------|-------------|---------------|-----------|
| biff (old) | pathlib, json | nats, pydantic, fastmcp, typer | 3.7s |
| biff (new) | pathlib, json | (stdlib only via `_stdlib.py`) | 0.29s |
| quarry | sqlite3, subprocess | pydantic_settings, lancedb, onnxruntime | 1.47s |
| lux | subprocess, pathlib | lightweight config (stdlib) | 0.30s |
| vox | (no Python) | (no Python) | 0.00s |

---

## 7. Three Approaches to Hook Implementation

Each has a different latency profile:

### 7a. Pure Shell (vox model)

```text
hooks/<event>.sh → shell commands only → exit
```

**Startup**: ~0.1s.
**Capability**: file checks, jq, subprocess spawns, command deployment.
**Limitation**: no access to Python libraries.
**When to use**: when the handler's work is genuinely shell-native
(config file existence, JSON manipulation, deploying files).

### 7b. Lightweight Python Entry Point (biff model)

```text
hooks/<event>.sh → <tool>-hook <event> → _hook_entry.py → _stdlib.py → handler
```

A separate console script (`<tool>-hook`) bypasses `__main__.py`.
Handlers import from `_stdlib.py` (stdlib only). `__init__.py` is
lazy (PEP 562).

**Startup**: ~0.3s.
**Capability**: full Python stdlib; no third-party libraries on the
hot path.
**Limitation**: stdlib extraction is manually enforced. One bad import
in `_stdlib.py` breaks the invariant silently. Two binaries to
maintain.
**When to use**: when the handler needs Python but not third-party
libraries. This is a mitigation, not a clean architecture.

### 7c. Go Proxy to Warm Python Daemon (mcp-proxy model)

```text
hooks/<event>.sh → go-proxy → warm Python daemon → handler
```

A Go binary (<10ms startup) routes hook calls to an already-running
Python daemon. The Python process started once at session begin and
stays warm. No per-hook import cost.

**Startup**: <10ms (Go binary) + RPC overhead (~5ms).
**Capability**: full Python ecosystem, all libraries available.
**Limitation**: requires a daemon lifecycle (start, health check,
graceful shutdown). More infrastructure.
**When to use**: the long-term target for any plugin that needs
Python on the hook path.

### Decision Matrix

| Handler needs | Shell only? | Import budget | Approach |
|-------------|------------|--------------|----------|
| File existence, jq, deploy | Yes | — | **Shell** (7a) |
| pathlib, json, subprocess | No | < 0.5s | **Lightweight entry point** (7b) |
| pydantic, DB, ML libraries | No | > 0.5s | **Go proxy** (7c) or accept the cost |

---

## 8. Implications for Plugin Design

### Design hooks as if they will run 50 times per session

SessionStart fires on open. PostToolUse fires on every tool call
(dozens per session). Stop fires on every response. Notification fires
on every permission prompt and idle timeout. A 0.5s hook that fires 50
times costs 25 seconds of cumulative session time.

### The Entire lesson: the dispatch language matters

This hook architecture was modeled after Entire, which uses Go.
Go processes start in ~10ms. The "thin shell gate → CLI handler"
pattern is correct when the CLI binary is fast. In Python, the binary
startup (interpreter + imports) dominates — and the correct
architecture is different.

For Python plugins, the options are:

1. **Do it in shell** (if you can)
2. **Minimize imports** (lightweight entry point + stdlib extraction)
3. **Keep Python warm** (daemon + proxy)

Option 3 is the correct long-term answer. Options 1 and 2 are
practical mitigations that work today.

### Hook return values determine sync requirements

A hook that injects `additionalContext` must be sync — Claude Code
needs the text before proceeding. A hook that only produces side
effects (audio, file writes, background sync) should be async.

Review every hook registration: if the handler returns `None` or
only writes to disk, set `"async": true` in `hooks.json`. This
removes it from the critical path entirely.

### The budget is per-plugin serial, cross-plugin parallel

Claude Code runs hooks from the same plugin serially. Hooks from
different plugins may run in parallel. The total startup time is
bounded by the slowest single plugin, not the sum of all plugins.

This means: consolidate hooks within a plugin (fewer shell spawns),
but don't worry about cross-plugin coordination.

---

## 9. Reference

### Z Specification

- `z-spec/examples/claude-code.tex` — Base state machine (18 ops,
  329K states, all invariants verified)
- `docs/claude-code-biff.tex` — Layer 2: biff workflow gates
  (plan + bead + message awareness)
- `z-spec/examples/claude-code-vox.tex` — Layer 2: Stop hook
  decision-block (no infinite loop)
- `z-spec/examples/claude-code-quarry.tex` — Layer 2: knowledge
  capture lifecycle (WebFetch dedup)

### Standards

- `punt-kit/standards/hooks.md` — Hook implementation standard
  (§ 12: Hook Startup Performance)

### ADRs

- `biff/DESIGN.md` DES-028 — Hook Import Tax (three-layer fix,
  measured results, alternatives rejected)

### Measured Data

- `../hooks-latency.md` — Per-hook timing, import profiling,
  cross-product analysis
