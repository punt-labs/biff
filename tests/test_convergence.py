"""Convergence contract: install is machine scope, enable owns the repo.

Installing or upgrading biff and running its setup verbs must leave a repo
correct with zero manual follow-up and zero regressions.  Five guarantees,
proven together and standalone, against a real ``git init`` repo and a tmp HOME:

1. ``install`` writes nothing repo-level (no hooks, no guide, no repo file).
2. ``enable`` is idempotent and never reverts a committed Dependabot bump of
   the ``setup-uv`` pin, yet still repairs an older or unversioned pin.
3. ``enable`` converges the whole repo artifact set; ``disable`` removes it and
   leaves the guide dormant.
4. The deployed notify workflow floats ``punt-biff`` (no ``==`` pin).
5. ``install`` and ``enable`` write disjoint sets of files.

Each test fails against the code that preceded the matching fix: install
writing ``.git/hooks`` or the user-scope guide (1, 5), ``enable`` rewriting the
setup-uv line unconditionally (2), ``enable`` not depositing the guide/import
(3), and the template pinning ``punt-biff==X`` (4).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from biff.__main__ import app
from biff.ci_workflow import check_ci_workflow
from biff.enablement import RepoEnablement
from biff.git_hooks import GIT_HOOKS, resolve_hooks_dir

if TYPE_CHECKING:
    from collections.abc import Callable

runner = CliRunner()

_WORKFLOW = Path(".github") / "workflows" / "biff-notify.yml"
_MARKER = Path(".punt-labs") / "biff" / "enabled"
_GUIDE = Path(".punt-labs") / "biff" / "CLAUDE.md"
_IMPORT_LINE = "@.punt-labs/biff/CLAUDE.md"
_CLI_LOG_DIR = (".punt-labs", "biff", "logs")

_NEWER_LINE = (
    "      - uses: astral-sh/setup-uv@1234567890abcdef1234567890abcdef12345678"
    " # v10.99.0"
)
_OLDER_LINE = (
    "      - uses: astral-sh/setup-uv@abcdef0123456789abcdef0123456789abcdef01 # v9.0.0"
)
_UNVERSIONED_LINE = (
    "      - uses: astral-sh/setup-uv@abcdef0123456789abcdef0123456789abcdef01"
)


class _Tree:
    """A byte-for-byte snapshot of every file under a root, for write-set diffs."""

    __slots__ = ("_files", "_root")

    _root: Path
    _files: dict[Path, bytes]

    def __new__(cls, root: Path) -> _Tree:
        self = super().__new__(cls)
        self._root = root
        self._files = {
            p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
        }
        return self

    def written_since(self, before: _Tree) -> set[Path]:
        """Files created or modified relative to *before* (same root)."""
        return {
            path
            for path, data in self._files.items()
            if before._files.get(path) != data  # pyright: ignore[reportPrivateUsage]
        }


def _git_init(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "init", "-q"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return repo


def _setup_uv_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if "astral-sh/setup-uv@" in ln]
    assert len(lines) == 1
    return lines[0]


def _set_pin(repo: Path, line: str) -> Path:
    target = repo / _WORKFLOW
    text = target.read_text()
    target.write_text(text.replace(_setup_uv_line(text), line))
    return target


def _run_install(
    monkeypatch: pytest.MonkeyPatch, home: Path, repo: Path, *, claude: bool
) -> None:
    """Run ``biff install`` from inside *repo* with a tmp HOME."""

    def which(_cmd: str) -> str | None:
        return "/usr/bin/claude" if claude else None

    def plugin_ok(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)
    # The stubs live only for the invoke, so the real ``git`` calls the
    # assertions make afterwards (hooks-dir resolution) keep working.
    with monkeypatch.context() as stub:
        stub.setattr("shutil.which", which)
        stub.setattr("subprocess.run", plugin_ok)
        result = runner.invoke(app, ["install"])
    assert result.exit_code == 0, result.output


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    return h


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = _git_init(tmp_path / "repo")
    (r / "CLAUDE.md").write_text("# Project rules\n")
    wf = r / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "name: Tests\non: [push]\njobs:\n  x:\n    runs-on: ubuntu-latest\n"
        "    steps: []\n"
    )
    return r


class TestInstallWritesNothingRepoLevel:
    """Guarantee 1: install is machine scope only."""

    @pytest.mark.parametrize("claude", [True, False], ids=["claude", "cli-only"])
    def test_install_leaves_home_and_repo_byte_identical(
        self,
        monkeypatch: pytest.MonkeyPatch,
        home: Path,
        repo: Path,
        claude: bool,
    ) -> None:
        home_before = _Tree(home)
        repo_before = _Tree(repo)

        _run_install(monkeypatch, home, repo, claude=claude)

        # The CLI keeps a machine-scope log under HOME; that is not a guide,
        # hook, or repo artifact.  Anything else under HOME is a violation.
        home_writes = _Tree(home).written_since(home_before)
        assert {p for p in home_writes if p.parts[:3] != _CLI_LOG_DIR} == set()
        assert _Tree(repo).written_since(repo_before) == set()

    @pytest.mark.parametrize("claude", [True, False], ids=["claude", "cli-only"])
    def test_install_writes_no_hooks_guide_or_user_claude_md(
        self,
        monkeypatch: pytest.MonkeyPatch,
        home: Path,
        repo: Path,
        claude: bool,
    ) -> None:
        _run_install(monkeypatch, home, repo, claude=claude)

        hooks_dir = resolve_hooks_dir(repo)
        assert hooks_dir is not None
        for name in GIT_HOOKS:
            assert not (hooks_dir / name).exists()
        assert not (home / ".claude" / "CLAUDE.md").exists()
        assert not (home / ".punt-labs" / "biff" / "CLAUDE.md").exists()
        assert not (repo / _MARKER).exists()
        assert not (repo / _WORKFLOW).exists()
        assert not (repo / _GUIDE).exists()
        assert (repo / "CLAUDE.md").read_text() == "# Project rules\n"


class TestEnableNonRevertingAcrossDependabot:
    """Guarantee 2: re-enable never reverts a bump, always repairs a bad pin."""

    def test_newer_pin_survives_reenable(self, repo: Path) -> None:
        RepoEnablement(repo).enable()
        target = _set_pin(repo, _NEWER_LINE)
        committed = target.read_text()

        assert check_ci_workflow(repo) is True
        change = RepoEnablement(repo).enable()

        assert change.ci_workflow_changed is False
        assert target.read_text() == committed
        assert _setup_uv_line(target.read_text()) == _NEWER_LINE
        assert check_ci_workflow(repo) is True

    @pytest.mark.parametrize(
        "bad_line", [_OLDER_LINE, _UNVERSIONED_LINE], ids=["older", "unversioned"]
    )
    def test_older_or_unversioned_pin_is_repaired(
        self, repo: Path, bad_line: str
    ) -> None:
        RepoEnablement(repo).enable()
        template_line = _setup_uv_line((repo / _WORKFLOW).read_text())
        target = _set_pin(repo, bad_line)

        assert check_ci_workflow(repo) is False
        change = RepoEnablement(repo).enable()

        assert change.ci_workflow_changed is True
        assert _setup_uv_line(target.read_text()) == template_line
        assert check_ci_workflow(repo) is True


class TestEnableConvergesRepoArtifacts:
    """Guarantee 3: enable owns every repo-level write; disable undoes them."""

    def _assert_enabled(self, repo: Path) -> None:
        hooks_dir = resolve_hooks_dir(repo)
        assert hooks_dir is not None
        assert (repo / _MARKER).is_file()
        assert (repo / _WORKFLOW).is_file()
        assert (repo / _GUIDE).is_file()
        for name in GIT_HOOKS:
            assert (hooks_dir / name).is_file()
        lines = (repo / "CLAUDE.md").read_text().splitlines()
        assert lines.count(_IMPORT_LINE) == 1

    def test_enable_writes_full_artifact_set(self, repo: Path) -> None:
        RepoEnablement(repo).enable()
        self._assert_enabled(repo)
        assert check_ci_workflow(repo) is True

    def test_reenable_is_a_noop(self, repo: Path) -> None:
        RepoEnablement(repo).enable()
        before = _Tree(repo)

        change = RepoEnablement(repo).enable()

        assert change.ci_workflow_changed is False
        assert change.git_hooks_changed == ()
        assert _Tree(repo).written_since(before) == set()
        self._assert_enabled(repo)

    def test_disable_removes_artifacts_and_leaves_guide_dormant(
        self, repo: Path
    ) -> None:
        RepoEnablement(repo).enable()
        RepoEnablement(repo).disable()

        hooks_dir = resolve_hooks_dir(repo)
        assert hooks_dir is not None
        assert not (repo / _MARKER).exists()
        assert not (repo / _WORKFLOW).exists()
        for name in GIT_HOOKS:
            assert not (hooks_dir / name).exists()
        assert _IMPORT_LINE not in (repo / "CLAUDE.md").read_text().splitlines()
        assert (repo / _GUIDE).is_file()


class TestNotifyWorkflowFloatsBiff:
    """Guarantee 4: a release reaches consumer CI with no per-repo re-enable."""

    def test_deployed_workflow_has_no_punt_biff_pin(self, repo: Path) -> None:
        RepoEnablement(repo).enable()
        text = (repo / _WORKFLOW).read_text()

        assert "uvx --from punt-biff " in text
        assert re.search(r"punt-biff\s*(==|>=|<=|~=|<|>)", text) is None
        assert "==" not in "\n".join(
            ln for ln in text.splitlines() if "punt-biff" in ln
        )

    def test_reenable_never_introduces_a_pin(self, repo: Path) -> None:
        RepoEnablement(repo).enable()
        RepoEnablement(repo).enable()
        assert "punt-biff==" not in (repo / _WORKFLOW).read_text()


class TestInstallAndEnableAreDisjoint:
    """Guarantee 5: no artifact has two owners."""

    def test_write_sets_do_not_overlap(
        self, monkeypatch: pytest.MonkeyPatch, home: Path, repo: Path
    ) -> None:
        def measure(action: Callable[[], None]) -> tuple[set[Path], set[Path]]:
            home_before, repo_before = _Tree(home), _Tree(repo)
            action()
            return (
                _Tree(home).written_since(home_before),
                _Tree(repo).written_since(repo_before),
            )

        install_home, install_repo = measure(
            lambda: _run_install(monkeypatch, home, repo, claude=True)
        )

        def run_enable() -> None:
            RepoEnablement(repo).enable()

        enable_home, enable_repo = measure(run_enable)

        install_writes = {("home", p) for p in install_home} | {
            ("repo", p) for p in install_repo
        }
        enable_writes = {("home", p) for p in enable_home} | {
            ("repo", p) for p in enable_repo
        }

        assert enable_writes, "enable must write the repo artifact set"
        assert install_writes.isdisjoint(enable_writes)
        # enable is repo-scoped: it never reaches into HOME.
        assert enable_home == set()
