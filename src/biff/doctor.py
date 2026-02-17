"""Environment diagnostics for biff.

Each check function returns a :class:`CheckResult`.  The
:func:`check_environment` aggregator runs all checks, prints
results, and returns an exit code.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from biff.config import (
    DEMO_RELAY_URL,
    demo_creds_path,
    extract_biff_fields,
    find_git_root,
    load_biff_file,
)
from biff.installer import COMMANDS_DIR, PLUGINS_DIR
from biff.models import RelayAuth
from biff.statusline import STASH_PATH


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    passed: bool
    message: str
    required: bool = True


# Individual checks ----------------------------------------------------------


def _check_gh_cli() -> CheckResult:
    """Check ``gh`` CLI is installed and authenticated."""
    gh = shutil.which("gh")
    if not gh:
        return CheckResult(
            "gh CLI",
            False,
            "not found (install: brew install gh)",
        )
    result = subprocess.run(
        [gh, "auth", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return CheckResult("gh CLI", False, "not authenticated (run: gh auth login)")
    return CheckResult("gh CLI", True, "authenticated")


def _check_mcp_server() -> CheckResult:
    """Check biff MCP server is registered."""
    claude = shutil.which("claude")
    if not claude:
        return CheckResult("MCP server", False, "claude CLI not found")
    result = subprocess.run(
        [claude, "mcp", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return CheckResult(
            "MCP server",
            False,
            "could not list MCP servers",
        )
    if "biff" in result.stdout:
        return CheckResult("MCP server", True, "registered")
    return CheckResult("MCP server", False, "not registered (run: biff install)")


def _check_plugin_installed(plugins_dir: Path | None = None) -> CheckResult:
    """Check plugin slash commands are installed."""
    target = plugins_dir or PLUGINS_DIR
    commands_dir = target / "commands"
    if not commands_dir.exists():
        return CheckResult(
            "Plugin commands",
            False,
            "not installed (run: biff install)",
        )
    commands = list(commands_dir.glob("*.md"))
    return CheckResult("Plugin commands", True, f"{len(commands)} commands installed")


def _check_user_commands(commands_dir: Path | None = None) -> CheckResult:
    """Check top-level user commands are deployed (informational)."""
    from biff.installer import plugin_source

    target = commands_dir or COMMANDS_DIR
    source = plugin_source() / "commands"
    expected = {f.name for f in source.glob("*.md")}
    missing = sorted(name for name in expected if not (target / name).exists())
    if not missing:
        return CheckResult(
            "User commands",
            True,
            f"{len(expected)} commands in {target}",
            required=False,
        )
    return CheckResult(
        "User commands",
        False,
        f"missing: {', '.join(missing)} (run: biff install)",
        required=False,
    )


def _resolve_relay_config() -> tuple[str, RelayAuth | None]:
    """Resolve relay URL and auth without requiring user identity."""
    repo_root = find_git_root()
    relay_url = DEMO_RELAY_URL
    relay_auth: RelayAuth | None = None

    if repo_root is not None:
        raw = load_biff_file(repo_root)
        _, url, auth = extract_biff_fields(raw)
        if url:
            relay_url = url
        if auth:
            relay_auth = auth

    # Auto-load demo credentials for demo relay
    if relay_url == DEMO_RELAY_URL and relay_auth is None:
        relay_auth = RelayAuth(user_credentials=str(demo_creds_path()))

    return relay_url, relay_auth


async def _test_nats_connection(url: str, auth: RelayAuth | None) -> bool:
    """Attempt a NATS connection with a short timeout."""
    import nats

    kwargs = auth.as_nats_kwargs() if auth else {}
    try:
        nc = await nats.connect(  # pyright: ignore[reportUnknownMemberType]
            url,
            connect_timeout=3,
            **kwargs,
        )
        await nc.close()
    except Exception:  # noqa: BLE001
        return False
    return True


def _check_relay() -> CheckResult:
    """Check NATS relay is reachable."""
    relay_url, relay_auth = _resolve_relay_config()

    try:
        reachable = asyncio.run(_test_nats_connection(relay_url, relay_auth))
    except Exception:  # noqa: BLE001
        return CheckResult("NATS relay", False, f"connection error ({relay_url})")

    if reachable:
        return CheckResult("NATS relay", True, f"reachable ({relay_url})")
    return CheckResult("NATS relay", False, f"unreachable ({relay_url})")


def _check_biff_file() -> CheckResult:
    """Check ``.biff`` file exists (informational)."""
    repo_root = find_git_root()
    if repo_root is None:
        return CheckResult(
            ".biff file",
            False,
            "not in a git repo (run 'biff init' inside a project)",
            required=False,
        )
    biff_file = repo_root / ".biff"
    if biff_file.exists():
        return CheckResult(".biff file", True, str(biff_file), required=False)
    return CheckResult(
        ".biff file",
        False,
        f"not found (run 'biff init' in {repo_root})",
        required=False,
    )


def _check_statusline() -> CheckResult:
    """Check status line is configured (informational)."""
    if STASH_PATH.exists():
        return CheckResult("Status line", True, "configured", required=False)
    return CheckResult(
        "Status line",
        False,
        "not configured (optional: run 'biff install-statusline')",
        required=False,
    )


# Aggregator -----------------------------------------------------------------


def _print_check(check: CheckResult) -> None:
    """Print a single check result with the appropriate symbol."""
    if check.passed:
        symbol = "\u2713"  # ✓
    elif check.required:
        symbol = "\u2717"  # ✗
    else:
        symbol = "\u25cb"  # ○
    print(f"  {symbol} {check.name}: {check.message}")


def check_environment(plugins_dir: Path | None = None) -> int:
    """Run all diagnostics. Returns 0 if all required pass, 1 otherwise."""
    from importlib.metadata import version

    print(f"biff-mcp {version('biff-mcp')}")
    print()

    checks = [
        _check_gh_cli(),
        _check_mcp_server(),
        _check_plugin_installed(plugins_dir),
        _check_user_commands(),
        _check_relay(),
        _check_biff_file(),
        _check_statusline(),
    ]

    for check in checks:
        _print_check(check)

    required_failures = [c for c in checks if c.required and not c.passed]
    if required_failures:
        count = len(required_failures)
        print(f"\n{count} required check(s) failed.")
        return 1
    print("\nAll required checks passed.")
    return 0
