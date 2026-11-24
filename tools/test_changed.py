"""Choose default-tier tests from the staged Git paths."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SHARED = frozenset(
    {
        Path("Makefile"),
        Path("pyproject.toml"),
        Path("tests/conftest.py"),
        Path("tools/test_changed.py"),
        Path("tools/test_quarantine.py"),
        Path("uv.lock"),
    }
)


def _imports(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_bytes())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    return imported


def _importers(root: Path, source: set[Path]) -> set[Path] | None:
    modules = {".".join(path.with_suffix("").parts[1:]) for path in source}
    packages = {
        ".".join(parts[:depth])
        for module in modules
        for parts in [module.split(".")]
        for depth in range(1, len(parts))
    }
    matched: set[str] = set()
    tests: set[Path] = set()
    for path in (root / "tests").rglob("test_*.py"):
        imports = _imports(path)
        matched.update(imports & modules)
        if imports & (modules | packages):
            tests.add(path.relative_to(root))
    return tests if matched == modules else None


def select_tests(root: Path, staged: set[Path]) -> list[Path] | None:
    """Return test paths to run, or None for the whole default tier."""
    if staged & _SHARED or any(
        path.parts[0] in {".githooks", ".github", "tools"} for path in staged
    ):
        return None
    source = {path for path in staged if path.is_relative_to("src/biff")}
    changed_tests = {path for path in staged if path.is_relative_to("tests")}
    tests = {
        path
        for path in changed_tests
        if path.match("test_*.py") and (root / path).is_file()
    }
    if changed_tests - tests or any(
        path.suffix != ".py" or path.name == "__init__.py" for path in source
    ):
        return None
    if source:
        importers = _importers(root, source)
        if importers is None:
            return None
        tests |= importers
    if any(path.parts[0] == "scripts" or path.name == "install.sh" for path in staged):
        return None
    return sorted(tests)


def main() -> int:
    """Run the affected subset with pytest's default marker filter."""
    names = subprocess.check_output(
        ["git", "diff", "--cached", "--no-renames", "--name-only", "-z"]
    )
    selected = select_tests(
        Path.cwd(), {Path(os.fsdecode(name)) for name in names.split(b"\0") if name}
    )
    if selected == []:
        return 0
    command = [sys.executable, "-m", "pytest"]
    code = subprocess.call([*command, *(str(path) for path in selected or [])])
    if selected is not None and code == pytest.ExitCode.NO_TESTS_COLLECTED:
        # Only opt-in cases were affected. The default gate still has to run.
        return subprocess.call(command)
    return code


if __name__ == "__main__":
    sys.exit(main())
