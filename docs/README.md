# Biff Architecture Documentation

This directory contains architecture analysis, technical specifications, and implementation plans for biff.

## Quick Start

**New to biff architecture?** Start here:

1. [phase1-recommendation.md](./phase1-recommendation.md) — TL;DR of architecture decisions (6 min read)
2. [architecture-analysis.md](./architecture-analysis.md) — Full analysis of all approaches (30 min read)
3. [phase1a-spec.md](./phase1a-spec.md) — Concrete implementation spec for Phase 1A (20 min read)

## Documents

### [phase1-recommendation.md](./phase1-recommendation.md)

**Executive summary** of Phase 1 architecture decisions.

**Key sections:**
- Core decisions (Message RX: Hybrid, Talk: Tool-Call Loop)
- What we're NOT doing (and why)
- 4-week implementation plan
- Risk mitigation
- Exit criteria

**Read this if:** You need to understand the architecture decisions quickly.

### [architecture-analysis.md](./architecture-analysis.md)

**Comprehensive analysis** of all architecture approaches considered.

**Key sections:**
- Confirmed constraints (MCP/Claude Code limitations)
- Message RX approaches: Hook+File (A), Pure Tool (B), Hybrid (C) ⭐
- Talk approaches: tmux Split-Pane (A), Tool-Call Loop (B) ⭐
- Technical feasibility ratings (1-5)
- Implementation complexity (LOC estimates)
- Risk assessment matrix
- Performance benchmarks

**Read this if:** You need to understand why we chose specific approaches, or you're evaluating alternatives.

### [phase1a-spec.md](./phase1a-spec.md)

**Technical specification** for Phase 1A: Core Infrastructure.

**Key sections:**
- Module structure (models, storage, relay)
- Data models with full code examples
- Storage layer implementation (inbox, notifications, sessions)
- Local relay design
- Test strategy
- Exit criteria

**Read this if:** You're implementing Phase 1A or need detailed API specifications.

## Architecture Decision Process

```
Problem: How to implement /mesg and /talk in MCP?
  ↓
Constraints: No server push, no PTY, hook bugs
  ↓
Approaches: Evaluate 6 approaches (A, B, C × 2)
  ↓
Analysis: Feasibility, complexity, risk, performance
  ↓
Decision: Message RX Hybrid (C), Talk Tool-Call Loop (B)
  ↓
Specification: Phase 1A technical spec
  ↓
Implementation: Week 1 (Phase 1A), Week 2 (Phase 1B), ...
```

## Key Decisions

| Decision | Approach | Rationale |
|----------|----------|-----------|
| **Message RX** | Hybrid (Hook + Tool) | Proactive when hooks work, reliable fallback when they fail |
| **Talk** | Tool-Call Loop (Phase 1) | Zero dependencies, works everywhere, proven tool call path |
| **Storage** | JSONL + JSON files | Simple, robust, no database required |
| **Relay** | File-based (Phase 1) | Same-machine communication, easy to test |

## Phase 1 Timeline

| Week | Phase | Deliverables | LOC |
|------|-------|--------------|-----|
| 1 | Phase 1A | Models, storage, relay | ~600 |
| 2 | Phase 1B | `/mesg` tools + hook | ~700 |
| 3 | Phase 1C | `/talk` tools | ~500 |
| 4 | Phase 1D | `/plan`, `/finger`, `/who` | ~450 |
| **Total** | **Phase 1** | **All commands** | **~2,250** |

## Questions & Answers

### Why not use MCP notifications?

**Answer**: Claude Code silently drops `notifications/message` notifications. This is confirmed behavior as of 2026-02-13. We use a hook + tool hybrid approach instead.

### Why not use tmux split-pane for talk in Phase 1?

**Answer**: tmux approach is higher risk and more complex (~500 LOC vs ~250 LOC). We're de-risking by starting with tool-call loop, then adding tmux in Phase 2 once core patterns are proven.

### Why file-based storage instead of SQLite?

**Answer**: Simplicity. JSONL and JSON files are:
- Easy to inspect and debug
- Zero-dependency (no database setup)
- Portable (copy ~/.biff to new machine)
- Sufficient for Phase 1 scale (< 1000 messages)

We can migrate to SQLite in Phase 2 if needed.

### What if hooks are unreliable?

**Answer**: The hybrid approach provides graceful degradation. If hooks fail to deliver notifications, users can still explicitly call `/mesg check`. We'll measure hook success rate in Phase 1B and adjust accordingly.

### What's the latency for /talk?

**Answer**: Tool-call loop approach has 1-2 second latency per message (Claude mediation + tool call + relay). This is acceptable for Phase 1. Phase 2 will add real-time tmux-based talk with 150-300ms latency.

## Next Steps

1. **Validate constraints** (1-2 days):
   - Test hook reliability (measure success rate)
   - Confirm MCP notification behavior
   - Benchmark tool call latency

2. **Begin Phase 1A** (1 week):
   - Implement models.py
   - Implement storage layer
   - Implement local relay
   - 100% test coverage

3. **Phase 1B** (1 week):
   - Implement `/mesg` tools
   - Implement UserPromptSubmit hook
   - Validate hook reliability in production

## Related Documents

- [/Users/jfreeman/Coding/biff/README.md](../README.md) — Project README
- [/Users/jfreeman/Coding/biff/CLAUDE.md](../CLAUDE.md) — Development workflow
- [/Users/jfreeman/Coding/biff/AGENTS.md](../AGENTS.md) — Agent instructions

## Beads

- [biff-6k7] Spike: MCP notification rendering and subprocess handoff (IN_PROGRESS)
- [biff-4rg] Phase 1: Core MCP server (BLOCKED by biff-6k7)
- [biff-iok] Phase 2: Network relay and team commands (BLOCKED by biff-4rg)

---

**Last Updated**: 2026-02-13
**Status**: Phase 1 architecture finalized, ready for implementation
