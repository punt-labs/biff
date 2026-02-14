# SPARC Plan: Integration Testing with Demo Transcript Capture

## S — Specification

### Problem Statement

Biff's 102 unit tests call tool functions directly, bypassing the MCP protocol. No test verifies what an MCP client actually experiences. No mechanism exists to capture and present tool interactions as demo output.

### Success Criteria

1. Integration tests exercise all 4 tools (`biff`, `finger`, `who`, `plan`) through `Client` + `FastMCPTransport`
2. A transcript capture mechanism records tool calls and responses during tests
3. A renderer produces human-readable demo output from captured transcripts
4. Multi-tool workflow tests demonstrate realistic biff sessions
5. All quality gates pass (ruff, mypy, pyright, pytest)

### Functional Requirements

| Req | Description | Priority |
|-----|-------------|----------|
| FR-1 | Async pytest fixtures create `Client` connected to biff server via `FastMCPTransport` | P0 |
| FR-2 | Integration tests call tools by name and assert on `CallToolResult` content | P0 |
| FR-3 | Transcript dataclass captures `(tool_name, arguments, result_text)` per call | P0 |
| FR-4 | Transcript renderer produces formatted demo output | P0 |
| FR-5 | Multi-tool workflow tests capture complete session transcripts | P1 |
| FR-6 | `pytest -m transcript` runs only transcript-generating tests | P1 |

## P — Pseudocode

### Transcript data model

```
@dataclass(frozen=True)
class TranscriptEntry:
    tool: str
    arguments: dict[str, object]
    result: str

@dataclass
class Transcript:
    title: str
    entries: list[TranscriptEntry]

    def add(tool, arguments, result):
        entries.append(TranscriptEntry(tool, arguments, result))

    def render() -> str:
        lines = [f"# {title}", ""]
        for entry in entries:
            lines.append(format_command(entry))
            lines.append(entry.result)
            lines.append("")
        return "\n".join(lines)
```

### Client wrapper for transcript capture

```
class RecordingClient:
    def __init__(client: Client, transcript: Transcript):
        self.client = client
        self.transcript = transcript

    async def call(tool_name, **kwargs) -> str:
        result = await client.call_tool(tool_name, kwargs)
        text = result.content[0].text
        transcript.add(tool_name, kwargs, text)
        return text
```

### Integration test fixture

```
@pytest.fixture
async def biff_client(state):
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client

@pytest.fixture
def transcript():
    return Transcript(title="")
```

### Demo output format

```
# Setting your plan and checking presence

> /plan "refactoring the auth layer"
Plan updated: refactoring the auth layer

> /who
@kai — refactoring the auth layer

> /finger @kai
@kai — accepting messages
  Last active: 2026-02-14T15:00:00+00:00
  Plan: refactoring the auth layer
```

## A — Architecture

### File/Module Layout

```
tests/
    test_integration/
        __init__.py
        conftest.py          # async fixtures: biff_client, recording_client, transcript
        test_protocol.py     # MCP protocol tests (list_tools, call each tool)
        test_workflows.py    # Multi-tool workflow tests with transcript capture
    transcripts/             # Generated demo output (gitignored)

src/biff/
    testing/
        __init__.py
        transcript.py        # Transcript, TranscriptEntry, RecordingClient
```

### Component Interactions

```
test_workflows.py
    |
    v
RecordingClient  -->  Client  -->  FastMCPTransport  -->  FastMCP server
    |                                                          |
    v                                                          v
Transcript                                               biff/finger/who/plan tools
    |
    v
render() --> .txt file in tests/transcripts/
```

### Key Design Decisions

- **Transcript types in `src/biff/testing/`** — not in tests/, because they could be used by downstream consumers (e.g., docs generation, CI artifacts)
- **RecordingClient wraps Client** — composition over inheritance; Client's API is large and we only need `call_tool`
- **Plain text output** — not JSON, not markdown. Terminal-style for maximum readability.

## R — Refinement

### Edge Cases

| Case | Handling |
|------|----------|
| Tool returns error | Record error text in transcript, mark entry as error |
| Empty tool result | Record "(no output)" |
| Multi-line result | Preserve formatting in transcript |
| Non-text content blocks | Skip non-text blocks, record text blocks only |

### Testing Strategy

- **Protocol tests**: Assert tool count, tool names, tool descriptions, individual call results
- **Workflow tests**: Multi-step scenarios that capture transcripts
- **Transcript tests**: Verify render output format

### Configuration

- `pytest -m transcript` — run only transcript-generating tests
- Transcripts written to `tests/transcripts/` (add to `.gitignore` initially, can be checked in later)

## C — Completion

### Task Breakdown

| # | Task | Priority | Depends On | Est. LOC |
|---|------|----------|------------|----------|
| 1 | Transcript data model + renderer | P0 | — | 80 |
| 2 | Integration test fixtures (conftest) | P0 | 1 | 50 |
| 3 | Protocol tests (list_tools, individual tool calls) | P0 | 2 | 100 |
| 4 | Workflow tests with transcript capture | P1 | 2, 3 | 80 |
| 5 | Transcript file output + pytest marker | P1 | 4 | 30 |

### Definition of Done

- [ ] All integration tests pass
- [ ] At least one demo transcript is generated and human-readable
- [ ] Quality gates: ruff, mypy, pyright, pytest all clean
- [ ] Existing 102 tests still pass
