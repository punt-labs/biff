# Contributing to Biff

Thank you for your interest in contributing to biff. This guide covers what you need to get started.

## Getting Started

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) for dependency management
- [NATS server](https://nats.io/) (optional, for NATS integration tests)

### Setup

```bash
git clone https://github.com/punt-labs/biff.git
cd biff
uv sync --all-extras
```

### Verify your setup

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ tests/
uv run pyright
uv run pytest
```

All five must pass with zero errors before any commit.

## Development Workflow

### Branch Discipline

All changes go on feature branches. Never commit directly to `main`.

```bash
git checkout -b feat/short-description main
```

| Prefix | Use |
|--------|-----|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `refactor/` | Code improvements |
| `docs/` | Documentation only |

### Commit Messages

Format: `type(scope): description`

```
feat(who): add idle time display
fix(relay): handle disconnection during fetch
refactor(tools): extract shared formatting
test(integration): add multi-user messaging tests
docs: update command reference
chore: bump fastmcp dependency
```

One logical change per commit. Small commits are preferred over large ones.

### Quality Gates

Every commit must pass:

```bash
uv run ruff check .           # Linting
uv run ruff format --check .  # Formatting
uv run mypy src/ tests/       # Type checking (mypy)
uv run pyright                 # Type checking (pyright)
uv run pytest                  # Tests (tiers 1-2)
```

### Running Tests

```bash
uv run pytest                          # Tiers 1-2 (unit + integration, fast)
uv run pytest -m subprocess            # Tier 3: subprocess/wire protocol
uv run pytest -m nats                  # Tier 3b: local NATS (requires nats-server)
uv run pytest -m sdk                   # Tier 4: SDK tests (requires ANTHROPIC_API_KEY)
```

New features should have tests at tiers 1-2 minimum.

## Code Standards

- **`from __future__ import annotations`** in every Python file.
- **Full type annotations** on every function signature. No `Any`.
- **Double quotes.** Line length 88.
- **Immutable data models.** `@dataclass(frozen=True)` or pydantic with immutability.
- **No duplication.** If you see two copies, extract one abstraction.
- **No backwards-compatibility shims.** When code changes, callers change.

## Submitting Changes

1. Push your branch and open a pull request.
2. Ensure CI passes on all commits.
3. Respond to review feedback. Each fix commit must also pass quality gates.
4. Once approved and green, the PR will be merged.

### What Makes a Good PR

- Clear title and description explaining *why*, not just *what*.
- Small, focused scope. One concern per PR.
- Tests included for new behavior.
- README updated if user-facing behavior changed.
- CHANGELOG entry for notable changes.

## Reporting Bugs

Open an issue at [github.com/punt-labs/biff/issues](https://github.com/punt-labs/biff/issues) with:

- What you expected to happen.
- What actually happened.
- Steps to reproduce.
- Your environment (OS, Python version, biff version).

## Suggesting Features

Open an issue describing the problem you want to solve, not just the solution you have in mind. Context about your use case helps us evaluate the right approach.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
