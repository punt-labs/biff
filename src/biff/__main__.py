"""Biff CLI entry point.

Provides ``biff serve``, ``biff version``, ``biff enable``, ``biff disable``,
``biff install``, ``biff doctor``, ``biff uninstall``, ``biff hook``, and
status line management.
"""

from __future__ import annotations

from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated

import click
import typer

from biff.config import (
    DEMO_RELAY_URL,
    build_biff_toml,
    ensure_gitignore,
    find_git_root,
    get_github_identity,
    get_os_user,
    is_enabled,
    load_config,
    write_biff_local,
)
from biff.hook import hook_app
from biff.server.app import create_server
from biff.server.state import create_state

app = typer.Typer(help="Biff: the dog that barked when messages arrived.")
app.add_typer(hook_app, name="hook")


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
    from biff.session_key import find_session_key
    from biff.statusline import UNREAD_DIR

    resolved = load_config(
        user_override=user,
        data_dir_override=data_dir,
        relay_url_override=relay_url if relay_url is not None else RELAY_URL_UNSET,
        prefix=prefix,
    )
    dormant = not is_enabled(resolved.repo_root)
    state = create_state(
        resolved.config,
        resolved.data_dir,
        unread_path=UNREAD_DIR / f"{find_session_key()}.json",
        dormant=dormant,
        repo_root=resolved.repo_root,
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


@app.command()
def enable(
    start: Annotated[
        Path | None,
        typer.Option(help="Repo root (default: auto-detect)."),
    ] = None,
) -> None:
    """Enable biff in the current git repo.

    If no ``.biff`` team config exists, runs the interactive init flow
    (identity resolution, team members, relay URL) to create one.
    Writes ``.biff.local`` with ``enabled = true`` and ensures it is
    gitignored.  Idempotent — safe to run multiple times.
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run this from inside a repo.")

    biff_file = repo_root / ".biff"
    if not biff_file.exists():
        # Interactive init flow — create .biff
        identity = get_github_identity()
        user = (identity.login if identity is not None else None) or get_os_user()
        if user is None:
            raise SystemExit(
                "Could not determine username.\n"
                "Install the gh CLI and authenticate: gh auth login"
            )
        print(f"Identity: {user}")

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

        biff_file.write_text(build_biff_toml(members, relay_url))
        print(f"Created {biff_file}")
        if members:
            print(f"  Team: {', '.join(members)}")
        if relay_url:
            print(f"  Relay: {relay_url}")

    write_biff_local(repo_root, enabled=True)
    ensure_gitignore(repo_root)
    print("biff enabled. Restart Claude Code for changes to take effect.")


@app.command()
def disable(
    start: Annotated[
        Path | None,
        typer.Option(help="Repo root (default: auto-detect)."),
    ] = None,
) -> None:
    """Disable biff in the current git repo.

    Writes ``.biff.local`` with ``enabled = false``.  Idempotent.
    """
    repo_root = find_git_root(start)
    if repo_root is None:
        raise SystemExit("Not in a git repository. Run this from inside a repo.")

    write_biff_local(repo_root, enabled=False)
    ensure_gitignore(repo_root)
    print("biff disabled. Restart Claude Code for changes to take effect.")


@app.command()
def install() -> None:
    """Install biff via the punt-labs marketplace."""
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
    """Uninstall biff plugin and clean up artifacts."""
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
