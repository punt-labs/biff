"""Plugin and MCP server installation for biff.

Handles copying plugin files (slash commands) into the Claude Code plugin
directory, registering the MCP server, and managing the plugin registry
and settings.  Uninstall reverses all operations.
"""

from __future__ import annotations

import importlib.resources
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from biff.relay import atomic_write
from biff.statusline import read_settings, write_settings

# Well-known paths ----------------------------------------------------------

PLUGINS_DIR = Path.home() / ".claude" / "plugins" / "biff"
COMMANDS_DIR = Path.home() / ".claude" / "commands"
REGISTRY_PATH = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
PLUGIN_KEY = "biff@local"


# Result types --------------------------------------------------------------


@dataclass(frozen=True)
class StepResult:
    """Outcome of a single install/uninstall step."""

    name: str
    passed: bool
    message: str


@dataclass(frozen=True)
class InstallResult:
    """Outcome of a full install attempt."""

    installed: bool
    message: str
    steps: list[StepResult] = field(default_factory=list[StepResult])


@dataclass(frozen=True)
class UninstallResult:
    """Outcome of a full uninstall attempt."""

    uninstalled: bool
    message: str
    steps: list[StepResult] = field(default_factory=list[StepResult])


# Plugin source --------------------------------------------------------------


def plugin_source() -> Path:
    """Resolve the bundled plugin directory from package data."""
    return Path(str(importlib.resources.files("biff.plugins").joinpath("biff")))


# MCP server -----------------------------------------------------------------


def _resolve_biff_command() -> list[str]:
    """Build the biff serve command for MCP registration."""
    which = shutil.which("biff")
    if which:
        return [which, "serve", "--transport", "stdio"]
    return [sys.executable, "-m", "biff", "serve", "--transport", "stdio"]


def _install_mcp_server() -> StepResult:
    """Register biff MCP server via ``claude mcp add``."""
    claude = shutil.which("claude")
    if not claude:
        return StepResult("MCP server", False, "claude CLI not found on PATH")

    cmd = _resolve_biff_command()
    result = subprocess.run(
        [claude, "mcp", "add", "--scope", "user", "biff", "--", *cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return StepResult("MCP server", True, "registered")
    stderr = result.stderr.strip()
    if "already exists" in stderr.lower():
        return StepResult("MCP server", True, "already registered")
    return StepResult("MCP server", False, f"claude mcp add failed: {stderr}")


def _uninstall_mcp_server() -> StepResult:
    """Remove biff MCP server via ``claude mcp remove``."""
    claude = shutil.which("claude")
    if not claude:
        return StepResult(
            "MCP server", True, "claude CLI not found (nothing to remove)"
        )

    result = subprocess.run(
        [claude, "mcp", "remove", "biff"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return StepResult("MCP server", True, "removed")
    stderr = result.stderr.strip()
    if "not found" in stderr.lower() or "does not exist" in stderr.lower():
        return StepResult("MCP server", True, "not registered (nothing to remove)")
    return StepResult("MCP server", False, f"claude mcp remove failed: {stderr}")


# Plugin files ---------------------------------------------------------------


def _install_plugin_files(target: Path | None = None) -> StepResult:
    """Copy plugin files from package data to the Claude plugins directory."""
    target = target or PLUGINS_DIR
    source = plugin_source()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        return StepResult("Plugin files", True, f"installed to {target}")
    except OSError as exc:
        return StepResult("Plugin files", False, f"copy failed: {exc}")


def _uninstall_plugin_files(target: Path | None = None) -> StepResult:
    """Remove plugin files from the Claude plugins directory."""
    target = target or PLUGINS_DIR
    if not target.exists():
        return StepResult("Plugin files", True, "not installed (nothing to remove)")
    try:
        shutil.rmtree(target)
        return StepResult("Plugin files", True, "removed")
    except OSError as exc:
        return StepResult("Plugin files", False, f"removal failed: {exc}")


# User commands --------------------------------------------------------------


def _install_user_commands(commands_dir: Path | None = None) -> StepResult:
    """Copy command files to ``~/.claude/commands/`` for top-level access."""
    commands_dir = commands_dir or COMMANDS_DIR
    source = plugin_source() / "commands"
    try:
        commands_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for md_file in sorted(source.glob("*.md")):
            shutil.copy2(md_file, commands_dir / md_file.name)
            count += 1
        return StepResult("User commands", True, f"deployed {count} commands")
    except OSError as exc:
        return StepResult("User commands", False, f"copy failed: {exc}")


def _uninstall_user_commands(commands_dir: Path | None = None) -> StepResult:
    """Remove biff command files from ``~/.claude/commands/``."""
    commands_dir = commands_dir or COMMANDS_DIR
    source = plugin_source() / "commands"
    bundled_names = {f.name for f in source.glob("*.md")}
    removed = 0
    for name in sorted(bundled_names):
        target = commands_dir / name
        if target.exists():
            target.unlink()
            removed += 1
    return StepResult("User commands", True, f"removed {removed} commands")


# Plugin registry ------------------------------------------------------------


def _read_registry(path: Path | None = None) -> dict[str, object]:
    """Read ``installed_plugins.json``, returning empty structure if absent."""
    path = path or REGISTRY_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return {}


def _register_plugin(
    registry_path: Path | None = None,
    plugins_dir: Path | None = None,
) -> StepResult:
    """Add ``biff@local`` to ``installed_plugins.json``."""
    registry_path = registry_path or REGISTRY_PATH
    plugins_dir = plugins_dir or PLUGINS_DIR

    registry = _read_registry(registry_path)
    plugins = registry.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        registry["plugins"] = plugins

    now = datetime.now(UTC).isoformat()
    entry = {
        "scope": "user",
        "installPath": str(plugins_dir),
        "version": "local",
        "installedAt": now,
        "lastUpdated": now,
    }
    plugins[PLUGIN_KEY] = [entry]

    try:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(registry_path, json.dumps(registry, indent=2) + "\n")
        return StepResult("Plugin registry", True, f"registered {PLUGIN_KEY}")
    except OSError as exc:
        return StepResult("Plugin registry", False, f"write failed: {exc}")


def _unregister_plugin(registry_path: Path | None = None) -> StepResult:
    """Remove ``biff@local`` from ``installed_plugins.json``."""
    registry_path = registry_path or REGISTRY_PATH
    if not registry_path.exists():
        return StepResult("Plugin registry", True, "no registry file")

    registry = _read_registry(registry_path)
    plugins = registry.get("plugins")
    if not isinstance(plugins, dict) or PLUGIN_KEY not in plugins:
        return StepResult("Plugin registry", True, f"{PLUGIN_KEY} not registered")

    del plugins[PLUGIN_KEY]
    try:
        atomic_write(registry_path, json.dumps(registry, indent=2) + "\n")
        return StepResult("Plugin registry", True, f"unregistered {PLUGIN_KEY}")
    except OSError as exc:
        return StepResult("Plugin registry", False, f"write failed: {exc}")


# Plugin enable/disable in settings ------------------------------------------


def _enable_plugin(settings_path: Path | None = None) -> StepResult:
    """Enable ``biff@local`` in ``settings.json``."""
    settings_path = settings_path or SETTINGS_PATH
    settings = read_settings(settings_path)
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict):
        enabled = {}
        settings["enabledPlugins"] = enabled

    enabled[PLUGIN_KEY] = True
    try:
        write_settings(settings_path, settings)
        return StepResult("Plugin enabled", True, f"enabled {PLUGIN_KEY}")
    except OSError as exc:
        return StepResult("Plugin enabled", False, f"write failed: {exc}")


def _disable_plugin(settings_path: Path | None = None) -> StepResult:
    """Disable ``biff@local`` in ``settings.json``."""
    settings_path = settings_path or SETTINGS_PATH
    if not settings_path.exists():
        return StepResult("Plugin enabled", True, "no settings file")

    settings = read_settings(settings_path)
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict) or PLUGIN_KEY not in enabled:
        return StepResult("Plugin enabled", True, f"{PLUGIN_KEY} not enabled")

    del enabled[PLUGIN_KEY]
    try:
        write_settings(settings_path, settings)
        return StepResult("Plugin enabled", True, f"disabled {PLUGIN_KEY}")
    except OSError as exc:
        return StepResult("Plugin enabled", False, f"write failed: {exc}")


# Public API -----------------------------------------------------------------


def install(
    plugins_dir: Path | None = None,
    settings_path: Path | None = None,
    registry_path: Path | None = None,
    commands_dir: Path | None = None,
) -> InstallResult:
    """Install biff plugin and register MCP server.

    Steps:
    1. Register MCP server via ``claude mcp add``
    2. Copy plugin files to ``~/.claude/plugins/biff/``
    3. Copy user commands to ``~/.claude/commands/``
    4. Register in ``installed_plugins.json``
    5. Enable in ``settings.json``

    Idempotent: safe to run multiple times.
    """
    steps = [
        _install_mcp_server(),
        _install_plugin_files(plugins_dir),
        _install_user_commands(commands_dir),
        _register_plugin(registry_path, plugins_dir),
        _enable_plugin(settings_path),
    ]

    if any(not s.passed for s in steps):
        return InstallResult(
            installed=False,
            message="Installation incomplete (see details above).",
            steps=steps,
        )
    return InstallResult(
        installed=True,
        message="Installed. Restart Claude Code to activate.",
        steps=steps,
    )


def uninstall(
    plugins_dir: Path | None = None,
    settings_path: Path | None = None,
    registry_path: Path | None = None,
    commands_dir: Path | None = None,
) -> UninstallResult:
    """Uninstall biff plugin, MCP server, and status line.

    Steps:
    1. Disable plugin in ``settings.json``
    2. Remove from ``installed_plugins.json``
    3. Remove plugin files
    4. Remove user commands
    5. Remove MCP server
    6. Call ``uninstall-statusline``

    Does NOT remove the ``.biff`` file.
    """
    from biff.statusline import uninstall as uninstall_statusline

    steps = [
        _disable_plugin(settings_path),
        _unregister_plugin(registry_path),
        _uninstall_plugin_files(plugins_dir),
        _uninstall_user_commands(commands_dir),
        _uninstall_mcp_server(),
    ]

    # Status line — best effort, don't fail uninstall if not installed
    sl_result = uninstall_statusline()
    steps.append(
        StepResult(
            "Status line",
            True,
            sl_result.message,
        )
    )

    if any(not s.passed for s in steps):
        return UninstallResult(
            uninstalled=False,
            message="Uninstall incomplete (see details above).",
            steps=steps,
        )
    return UninstallResult(
        uninstalled=True,
        message="Uninstalled.",
        steps=steps,
    )
