# PRD: Integration Testing with Demo Transcript Capture

**Status:** Approved (hive-mind consensus)
**Date:** 2026-02-14
**Origin:** User observation: "design an integration testing approach, include capturing transcripts that can be presented as a 'demo' output to the user."

## Problem Statement

Biff's current test suite (102 tests) calls tool functions directly via `_get_tool_fn()`, bypassing the MCP protocol layer entirely. This means:

1. **No protocol coverage.** Tool listing, tool calling via MCP messages, content block serialization, and lifespan management are untested.
2. **No demo artifacts.** There is no way to show a prospective user what a biff session looks like — the tool descriptions, the call/response flow, the multi-tool workflows — without running a live server.
3. **No regression net for protocol changes.** FastMCP upgrades could break the server without any test catching it.

## Root Cause Analysis

The unit tests were the right foundation for the server scaffold, but they test the *implementation* (Python functions) rather than the *interface* (MCP protocol). Integration tests that drive the server through `FastMCPTransport` + `Client` would test what clients actually experience.

The transcript capture need comes from biff's adoption challenge: as a CLI tool with no GUI, biff needs a way to show its value before installation. Captured transcripts of real MCP sessions serve as both test artifacts and marketing collateral.

## Proposed Solution

### Two-layer integration test architecture

1. **MCP protocol tests** — Use `FastMCPTransport` to drive the biff server through the real MCP protocol. Assert on tool listing, tool call results, multi-step workflows, and error handling.

2. **Transcript capture** — A pytest fixture/context manager that records every tool call and response during a test into a structured transcript. Tests decorated with a marker (e.g., `@pytest.mark.transcript`) automatically capture their session.

3. **Demo output renderer** — A function that takes a captured transcript and renders it as formatted text (terminal-style output showing the "conversation" between a user and biff). This output can be written to a file, printed during test runs, or included in documentation.

## User Stories

### P0 — Must Have

| ID | Story | Acceptance Criteria |
|----|-------|---------------------|
| US-1 | As a developer, I can run integration tests that exercise biff tools through the MCP protocol | Tests use `Client` + `FastMCPTransport`, call tools by name, assert on `CallToolResult` content |
| US-2 | As a developer, integration tests capture transcripts of tool calls and responses | A transcript fixture records `(tool_name, arguments, result_text)` tuples for each call |
| US-3 | As a developer, I can render a captured transcript as readable demo output | A renderer produces formatted text showing each step as `/command` + response |

### P1 — Important

| ID | Story | Acceptance Criteria |
|----|-------|---------------------|
| US-4 | As a developer, multi-tool workflow tests demonstrate realistic biff sessions | At least one test shows: set plan, check who, finger a user — a complete session |
| US-5 | As a developer, transcripts are saved to a known location for CI/docs use | Transcripts written to `tests/transcripts/` as `.txt` files when marker is present |

### P2 — Nice to Have

| ID | Story | Acceptance Criteria |
|----|-------|---------------------|
| US-6 | As a developer, I can generate all demo transcripts with a single pytest command | `pytest -m transcript` runs only transcript-generating tests |

## Design Considerations

- **Transcript format.** Terminal-style, showing `/command` and response blocks. Not JSON — this is for humans.
- **Test isolation.** Each integration test gets its own `ServerState` with `tmp_path`, same as unit tests.
- **Async tests.** `FastMCPTransport` requires async. Use `pytest-asyncio` with `auto` mode or explicit `@pytest.mark.asyncio`.

## Technical Considerations

- **FastMCPTransport** provides zero-overhead in-memory MCP protocol testing. No HTTP, no subprocess.
- **Client.call_tool()** returns `CallToolResult` with `content: list[ContentBlock]`. Text results are in `content[0].text`.
- **Client.list_tools()** returns `list[mcp.types.Tool]` with `name`, `description`, `inputSchema`.
- **Lifespan** is managed automatically by `FastMCPTransport` — it enters the server lifespan context.
- **pytest-asyncio** is needed as a new dev dependency.

## Domain Considerations (CLI/Developer Experience)

- Demo transcripts should look like what a user actually sees in their terminal session.
- Use the `/command` vocabulary from the PR/FAQ (e.g., `/plan`, `/who`, `/finger`, `/biff on`).
- Show the AI-mediated aspect: the transcript should read as natural tool use, not raw API calls.

## Success Metrics

1. Integration tests cover all 4 tools through the MCP protocol.
2. At least one multi-step workflow transcript is generated.
3. Demo output is human-readable and could be included in a README.
4. All quality gates pass (ruff, mypy, pytest).

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| pytest-asyncio compatibility | Low | Medium | Well-established library, widely used with FastMCP |
| FastMCPTransport API changes | Low | Medium | Pin fastmcp version; tests will catch breakage |
| Transcript format bikeshedding | Medium | Low | Start simple (plain text), iterate later |

## Out of Scope

- HTTP transport integration tests (FastMCPTransport covers the protocol; HTTP is a transport detail)
- Performance/load testing
- CI pipeline changes (transcripts are generated locally)
- Interactive demo mode (transcripts are static artifacts)
