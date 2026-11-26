"""The plugin surface must not reach outside ``plugin/``.

The marketplace installs ``plugin/`` alone, via Claude Code's ``git-subdir``
source: a blobless partial clone plus ``sparse-checkout set --cone plugin``.
Cone mode excludes whole *directories*, so any runtime path in the surface
that points at a sibling directory of ``plugin/`` -- ``assets/``, ``scripts/``,
``src/`` -- resolves on a developer's full checkout and is simply **absent**
on an installed plugin. A hook that references one fails silently: it exits
non-zero into a hook runner that ignores it, or globs to nothing.

These tests are the machine-checkable form of that invariant (DES-055). They
walk every text file in the surface, collect the paths it addresses through
``${CLAUDE_PLUGIN_ROOT}`` and through ``session-start.sh``'s script-relative
``$PLUGIN_ROOT``, and require each one to exist inside ``plugin/``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_PLUGIN = Path(__file__).resolve().parent.parent / "plugin"

# Text extensions cover the whole surface: hooks (.sh), wiring and manifest
# (.json/.jsonc), commands and agents (.md).
_TEXT_SUFFIXES = frozenset({".sh", ".json", ".jsonc", ".md"})

# `${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh` -> `hooks/session-start.sh`.
_PLUGIN_ROOT_VAR = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\"'\s]+)")

# session-start.sh resolves its own root script-relatively (`dirname $0/..`)
# rather than from `git rev-parse`, which would break the moment the surface
# sits in a subdirectory. Its references look like "$PLUGIN_ROOT/commands/"*.md
# -- the capture stops at the closing quote, leaving a directory prefix.
_SCRIPT_ROOT_VAR = re.compile(r"\$PLUGIN_ROOT/([^\"'\s]*)")

# An assignment to any *PLUGIN_ROOT* variable, capturing the right-hand side:
# `PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"` -> the `"$(cd ...)"` part.
_PLUGIN_ROOT_ASSIGN = re.compile(r"^\s*\w*PLUGIN_ROOT\w*=(.*)$", re.MULTILINE)


def _surface_files() -> list[Path]:
    """Every text file shipped in the plugin surface."""
    return sorted(
        path
        for path in _PLUGIN.rglob("*")
        if path.is_file() and path.suffix in _TEXT_SUFFIXES
    )


def _referenced_paths(pattern: re.Pattern[str]) -> list[tuple[Path, str]]:
    """Collect ``(source file, referenced path)`` pairs matching *pattern*."""
    found: list[tuple[Path, str]] = []
    for path in _surface_files():
        found.extend(
            (path, match.group(1))
            for match in pattern.finditer(path.read_text(encoding="utf-8"))
        )
    return found


def _assert_in_surface(source: Path, target: str, spelling: str) -> None:
    """Require *target* to name a path that exists inside ``plugin/``.

    Existence alone is not enough. ``${CLAUDE_PLUGIN_ROOT}/../src/foo`` exists
    on a developer's full checkout and is absent on an installed plugin, so a
    bare ``exists()`` check would pass for precisely the escape these tests
    are here to catch. Containment is therefore asserted first, against the
    *resolved* path, and only then existence.
    """
    candidate = _PLUGIN / target.rstrip("/")
    where = f"{source.relative_to(_PLUGIN.parent)} references {spelling}"
    resolved = candidate.resolve()
    assert resolved.is_relative_to(_PLUGIN.resolve()), (
        f"{where}, which escapes the plugin surface (resolves to {resolved})"
    )
    assert candidate.exists(), f"{where}, which is not in the surface"


class TestSurfaceIsSelfContained:
    """Every path the surface addresses at runtime lives inside ``plugin/``."""

    def test_plugin_root_references_resolve(self) -> None:
        references = _referenced_paths(_PLUGIN_ROOT_VAR)
        # A regex that silently matches nothing would make this whole test
        # vacuous, so require the known hooks.json wiring to be present.
        assert len(references) >= 9, (
            f"expected the hooks.json dispatchers, found {len(references)}"
        )
        for source, target in references:
            _assert_in_surface(source, target, f"${{CLAUDE_PLUGIN_ROOT}}/{target}")

    def test_script_relative_references_resolve(self) -> None:
        references = _referenced_paths(_SCRIPT_ROOT_VAR)
        assert references, "expected session-start.sh's $PLUGIN_ROOT references"
        for source, target in references:
            _assert_in_surface(source, target, f"$PLUGIN_ROOT/{target}")

    def test_plugin_root_is_derived_script_relatively(self) -> None:
        """The plugin root must come from the script's own location.

        The hooks *do* call ``git rev-parse --show-toplevel``, ten of them, to
        find the **consumer's** repo -- that is where the enablement marker
        lives, and it is correct. What must never happen is a plugin root
        derived from git: on an installed plugin the checkout is not the
        user's repo, and since DES-055 it is not even a repo root.

        So this is a whitelist, not a blacklist. Every assignment to a
        ``*PLUGIN_ROOT*`` variable must derive from ``dirname``/``BASH_SOURCE``,
        which rules out a git-derived value however it is laundered --
        including the two-step form ``ROOT=$(git rev-parse --show-toplevel);
        PLUGIN_ROOT="$ROOT/plugin"``, which any pattern hunting for
        ``rev-parse`` on the assignment line would miss.
        """
        assignments: list[tuple[Path, str]] = []
        for script in sorted(_PLUGIN.glob("hooks/*.sh")):
            body = script.read_text(encoding="utf-8")
            assignments.extend(
                (script, match.group(1)) for match in _PLUGIN_ROOT_ASSIGN.finditer(body)
            )

        assert assignments, "expected session-start.sh to assign a PLUGIN_ROOT"
        for script, rhs in assignments:
            assert "dirname" in rhs or "BASH_SOURCE" in rhs, (
                f"{script.name} assigns a plugin root that is not script-relative: "
                f"{rhs.strip()}"
            )


class TestChannelModeIsPluginDefinition:
    """The plugin turns channel mode on; nothing else does (DES-087).

    biff 1.24.2 exits 2 on an unknown option, so the mode travels as an
    environment variable (an older binary ignores it) and the command line
    stays exactly ``biff mcp``.
    """

    @staticmethod
    def _servers() -> dict[str, dict[str, Any]]:
        manifest = _PLUGIN / ".claude-plugin" / "plugin.json"
        servers: dict[str, dict[str, Any]] = json.loads(
            manifest.read_text(encoding="utf-8")
        )["mcpServers"]
        return servers

    def test_tty_server_sets_the_variable(self) -> None:
        assert self._servers()["tty"]["env"] == {"BIFF_CHANNEL_MODE": "1"}

    def test_no_other_server_sets_it(self) -> None:
        others = [s for name, s in self._servers().items() if name != "tty"]

        assert all("BIFF_CHANNEL_MODE" not in s.get("env", {}) for s in others)

    def test_command_line_carries_no_new_argument(self) -> None:
        tty = self._servers()["tty"]

        assert (tty["command"], tty["args"]) == ("biff", ["mcp"])

    def test_the_name_matches_the_build(self) -> None:
        """``biff-dev`` with its ``-dev`` commands on main; ``biff`` without them.

        scripts/release-plugin.sh renames the plugin and strips the ``-dev``
        commands together on the release commit, and restore-dev-plugin.sh
        undoes both, so a ``biff`` name with dev commands (or ``biff-dev``
        without them) is a broken build either way.
        """
        manifest = _PLUGIN / ".claude-plugin" / "plugin.json"
        name = json.loads(manifest.read_text(encoding="utf-8"))["name"]
        has_dev_commands = any((_PLUGIN / "commands").glob("*-dev.md"))

        assert (name, has_dev_commands) in {("biff-dev", True), ("biff", False)}
