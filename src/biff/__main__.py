"""Biff CLI entry point.

Provides ``biff serve``, ``biff version``, ``biff init``, ``biff install``,
``biff doctor``, ``biff uninstall``, and status line management.
"""

from __future__ import annotations

import os
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated

import click
import typer

from biff.config import (
    DEMO_RELAY_URL,
    find_git_root,
    get_github_identity,
    get_os_user,
    load_config,
)
from biff.server.app import create_server
from biff.server.state import create_state

app = typer.Typer(help="Biff: the dog that barked when messages arrived.")


@app.command()
def version() -> None:
    """Print the biff version."""
    print(f"biff {pkg_version('punt-biff')}")


@app.command()
def serve(
    user: Annotated[
        str | None,
        typer.Option(help="Your username. Auto-detected from GitHub CLI."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(help="Data directory. Auto-computed as {prefix}/biff/{repo}."),
    ] = None,
    relay_url: Annotated[
        str | None,
        typer.Option(help="Relay URL override. Empty string forces local relay."),
    ] = None,
    prefix: Annotated[
        Path,
        typer.Option(help="Base path for data directory (default: /tmp)."),
    ] = Path("/tmp"),  # noqa: S108
    transport: Annotated[
        str,
        typer.Option(
            help="Transport: 'stdio' or 'http'.",
            click_type=click.Choice(["stdio", "http"]),
        ),
    ] = "stdio",
    host: Annotated[
        str, typer.Option(help="HTTP host (http transport only).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP port (http transport only).")] = 8419,
) -> None:
    """Start the biff MCP server."""
    from biff.config import RELAY_URL_UNSET
    from biff.statusline import UNREAD_DIR

    resolved = load_config(
        user_override=user,
        data_dir_override=data_dir,
        relay_url_override=relay_url if relay_url is not None else RELAY_URL_UNSET,
        prefix=prefix,
    )
    state = create_state(
        resolved.config,
        resolved.data_dir,
        unread_path=UNREAD_DIR / f"{os.getppid()}.json",
    )
    mcp = create_server(state)

    if transport == "http":
        print(f"Starting biff MCP server on http://{host}:{port}")
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run(transport="stdio")


@app.command("install-statusline")
def install_statusline() -> None:
    """Install biff into Claude Code's status bar."""
    from biff.statusline import install

    result = install()
    print(result.message)
    if not result.installed:
        raise typer.Exit(code=1)


@app.command("uninstall-statusline")
def uninstall_statusline() -> None:
    """Remove biff from Claude Code's status bar."""
    from biff.statusline import uninstall

    result = uninstall()
    print(result.message)
    if not result.uninstalled:
        raise typer.Exit(code=1)


def _toml_basic_string(value: str) -> str:
    """Escape *value* for use as a TOML basic string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _build_biff_toml(members: list[str], relay_url: str) -> str:
    """Build ``.biff`` TOML content from user inputs."""
    lines: list[str] = []
    if members:
        quoted = ", ".join(_toml_basic_string(m) for m in members)
        lines.append("[team]")
        lines.append(f"members = [{quoted}]")
    if relay_url:
        if lines:
            lines.append("")
        lines.append("[relay]")
        lines.append(f"url = {_toml_basic_string(relay_url)}")
    return "\n".join(lines) + "\n" if lines else ""


@app.command()
def init(
    start: Annotated[
        Path | None,
        typer.Option(help="Repo root (default: auto-detect)."),
    ] = None,
) -> None:
    """Initialize biff in the current git repo."""
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run this from inside a repo.")

    biff_file = repo_root / ".biff"
    if biff_file.exists():
        raise SystemExit(
            f"{biff_file} already exists. Edit it directly or remove it first."
        )

    # Resolve identity: GitHub CLI > OS username
    identity = get_github_identity()
    user = (identity.login if identity is not None else None) or get_os_user()
    if user is None:
        raise SystemExit(
            "Could not determine username.\n"
            "Install the gh CLI and authenticate: gh auth login"
        )
    print(f"Identity: {user}")

    # Gather team members
    members_input = typer.prompt(
        "Team members (comma-separated, or empty)",
        default="",
        show_default=False,
    )
    members = [m.strip() for m in members_input.split(",") if m.strip()]

    relay_url = typer.prompt(
        "Relay URL",
        default=DEMO_RELAY_URL,
    )

    # Write .biff (even if empty — signals "biff is configured here")
    biff_file.write_text(_build_biff_toml(members, relay_url))
    print(f"Created {biff_file}")

    if members:
        print(f"  Team: {', '.join(members)}")
    if relay_url:
        print(f"  Relay: {relay_url}")
    if not members and not relay_url:
        print("  (empty — add [team] or [relay] sections as needed)")


@app.command()
def install() -> None:
    """Install biff plugin and register MCP server."""
    from biff.installer import install as do_install

    result = do_install()
    for step in result.steps:
        symbol = "\u2713" if step.passed else "\u2717"
        print(f"  {symbol} {step.name}: {step.message}")
    print()
    print(result.message)
    if not result.installed:
        raise typer.Exit(code=1)


@app.command()
def doctor() -> None:
    """Check biff installation health."""
    from biff.doctor import check_environment

    code = check_environment()
    if code != 0:
        raise typer.Exit(code=code)


@app.command()
def uninstall() -> None:
    """Uninstall biff plugin and MCP server."""
    from biff.installer import uninstall as do_uninstall

    result = do_uninstall()
    for step in result.steps:
        symbol = "\u2713" if step.passed else "\u2717"
        print(f"  {symbol} {step.name}: {step.message}")
    print()
    print(result.message)
    if not result.uninstalled:
        raise typer.Exit(code=1)


@app.command()
def statusline() -> None:
    """Output status bar text (called by Claude Code)."""
    from biff.statusline import run_statusline

    print(run_statusline())


if __name__ == "__main__":
    app()
