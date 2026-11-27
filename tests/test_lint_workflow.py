"""The post-merge ratchet step of lint.yml, run for real with stub commands.

The step looks up the pushed commit's pull requests, lets ``tools/layers_allowance``
decide, and runs every ratchet.  A lookup that fails must warn, keep the layers
check strict and still reach ``make``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "lint.yml"
MERGE = "2b11dc72075cdbc6858381c3424d2b07c3b6a673"
HEAD = "fa6f7b9889384d8360d48cb8f898e5a8c5b580e3"
PULLS = (
    '[{"merged_at": "2026-11-26T23:05:00Z", "merge_commit_sha": "' + MERGE + '", '
    '"labels": [{"name": "layers-policy-change"}]}]'
)
ALLOW = "--allow-config-change"


def _step_script() -> str:
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["lint"]["steps"]
    step = next(s for s in steps if s.get("if") == "github.event_name == 'push'")
    return str(step["run"])


def _answers_only_for(sha: str, pulls: str) -> str:
    """A ``gh`` stub that returns *pulls* for *sha*'s endpoint, else fails."""
    endpoint = f"repos/punt-labs/biff/commits/{sha}/pulls"
    return (
        'case "$*" in\n'
        f"  *\"{endpoint}\"*) echo '{pulls}' ;;\n"
        '  *) echo "unexpected gh arguments: $*" >&2; exit 1 ;;\n'
        "esac"
    )


TARGETS = ["check-oo", "check-coupling", "check-suppressions", "check-layers"]


def _make_arguments(stdout: str) -> list[str]:
    """Return the arguments the stub ``make`` received, one per ``ARG:`` line."""
    return [
        line.removeprefix("ARG:")
        for line in stdout.splitlines()
        if line.startswith("ARG:")
    ]


def _make_variables(stdout: str) -> dict[str, str]:
    """Return the ``NAME=value`` arguments make received, keyed by name."""
    variables: dict[str, str] = {}
    for arg in _make_arguments(stdout):
        name, separator, value = arg.partition("=")
        if separator:
            variables[name] = value
    return variables


def _variables_with_flag(stdout: str) -> list[str]:
    """Return the names of every make variable that carries the allowance flag."""
    return [name for name, value in _make_variables(stdout).items() if ALLOW in value]


def _make_targets(stdout: str) -> list[str]:
    """Return the targets make was asked to build."""
    return [arg for arg in _make_arguments(stdout) if "=" not in arg]


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _run_step(
    tmp_path: Path, *, sha: str, gh_body: str
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "gh", gh_body)
    _stub(bin_dir, "uv", f'shift 2; exec "{sys.executable}" "$@"')
    _stub(bin_dir, "make", "printf 'ARG:%s\\n' \"$@\"")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "BEFORE_SHA": "609d79d5bd1246d4af45623d1550a406714fc349",
        "GITHUB_REPOSITORY": "punt-labs/biff",
        "GITHUB_SHA": sha,
    }
    return subprocess.run(  # noqa: S603
        ["/bin/bash", "-e", "-c", _step_script()],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_labelled_merge_allows_the_layers_widening(tmp_path: Path) -> None:
    result = _run_step(tmp_path, sha=MERGE, gh_body=_answers_only_for(MERGE, PULLS))
    assert result.returncode == 0, result.stderr
    assert "::warning::" not in result.stdout
    assert _variables_with_flag(result.stdout) == ["LAYERS_BASE"]
    assert _make_targets(result.stdout) == TARGETS


def test_the_prs_head_commit_does_not_allow_it(tmp_path: Path) -> None:
    """The lookup succeeds and finds the labelled PR, whose merge commit differs."""
    result = _run_step(tmp_path, sha=HEAD, gh_body=_answers_only_for(HEAD, PULLS))
    assert result.returncode == 0, result.stderr
    assert "::warning::" not in result.stdout
    assert _variables_with_flag(result.stdout) == []
    assert _make_targets(result.stdout) == TARGETS


def test_the_step_asks_about_the_pushed_commit(tmp_path: Path) -> None:
    """A stub that answers only for another commit makes the lookup fail."""
    result = _run_step(tmp_path, sha=MERGE, gh_body=_answers_only_for(HEAD, PULLS))
    assert result.returncode == 0, result.stderr
    assert "::warning::" in result.stdout
    assert _variables_with_flag(result.stdout) == []


def test_a_failed_lookup_warns_stays_strict_and_still_runs_every_ratchet(
    tmp_path: Path,
) -> None:
    result = _run_step(tmp_path, sha=MERGE, gh_body="exit 1")
    assert result.returncode == 0, result.stderr
    assert "::warning::" in result.stdout
    assert _variables_with_flag(result.stdout) == []
    assert _make_targets(result.stdout) == TARGETS


def test_unreadable_pr_data_warns_and_stays_strict(tmp_path: Path) -> None:
    result = _run_step(tmp_path, sha=MERGE, gh_body="echo 'not json'")
    assert result.returncode == 0, result.stderr
    assert "::warning::" in result.stdout
    assert _variables_with_flag(result.stdout) == []
    assert _make_targets(result.stdout) == TARGETS
