"""Structural and isolated bootstrap checks for the Devin plugin surface."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

from biff.session_routing import ID_RE

_SURFACE = Path(__file__).resolve().parent.parent / "devin-plugin"
_CONFIG = ".punt-labs/biff/config.local.yaml"
_IDENTITY = ".punt-labs/ethos/identities/devin.yaml"
_EXPECTED_CONFIG = {
    "team": {"members": ["jmf-pobox", "github-actions"]},
    "peers": {"orgs": ["punt-labs"], "repos": ["local-scripts"]},
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    return path


def _hook() -> str:
    events = json.loads((_SURFACE / "hooks.json").read_text())
    return cast("str", events["SessionStart"][0]["hooks"][0]["command"])


def _run_hook(
    home: Path, project: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", _hook()],
        cwd=home,
        env={
            **os.environ,
            "HOME": str(home),
            "DEVIN_PROJECT_DIR": str(project) if project else "",
            "CLAUDE_PLUGIN_ROOT": "",
            "BIFF_RELAY_TOKEN": "test-sensitive-relay-value",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _server() -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads((_SURFACE / ".mcp.json").read_text())["mcpServers"]["biff"],
    )


def _launch(home: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/bin/sh", *cast("list[str]", _server()["args"])],
        env={**os.environ, "HOME": str(home), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_manifest_and_server() -> None:
    """The installed surface launches the named stdio server without secrets."""
    manifest = json.loads((_SURFACE / ".devin-plugin/plugin.json").read_text())
    assert manifest["name"] == "biff"
    assert manifest["author"]["name"] == "Punt Labs"
    server = _server()
    assert server["type"] == "stdio"
    assert server["command"] == "sh"
    args = cast("list[str]", server["args"])
    assert args[0] == "-c"
    assert "exec biff mcp --user devin" in args[1]
    assert server["env"] == {
        "BIFF_MCP_TRANSPORT": "inprocess",
        "BIFF_RELAY_TOKEN": "${MCP_BIFF_RELAY_TOKEN}",
        "BIFF_RELAY_URL": "tls://biff-relay.punt-labs.com:4222",
        "BIFF_RELAY_TLS_HANDSHAKE_FIRST": "true",
    }
    assert "TOKEN" not in json.dumps(manifest).upper()


def test_hook_wiring_and_surface_paths() -> None:
    """Hook commands are self-contained without plugin-root path expansion."""
    events = json.loads((_SURFACE / "hooks.json").read_text())
    hook = events["SessionStart"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert hook["timeout"] == 30
    assert _hook().startswith("#!/bin/sh\n")
    for path in _SURFACE.rglob("*"):
        if not path.is_file():
            continue
        assert "CLAUDE_PLUGIN_ROOT" not in path.read_text()


@pytest.mark.parametrize("name", ["who", "read", "write", "wall"])
def test_skills(name: str) -> None:
    """Each skill declares its name and the shared MCP invocation interface."""
    text = (_SURFACE / "skills" / name / "SKILL.md").read_text()
    metadata = yaml.safe_load(text.split("---", 2)[1])
    assert metadata["name"] == name
    assert isinstance(metadata["description"], str) and metadata["description"]
    assert "mcp_call_tool" in text
    assert "server `biff`" in text


def test_poll_rule() -> None:
    """Mail polling remains active independently of skill invocation."""
    text = (_SURFACE / "rules/biff-mail-poll.md").read_text()
    assert yaml.safe_load(text.split("---", 2)[1])["trigger"] == "always_on"
    assert "five minutes" in text
    assert "read_messages" in text


def test_bootstrap_and_deduplication(tmp_path: Path) -> None:
    """Every discovered repo gets private local files, without leaking the token."""
    repos = [_repo(tmp_path / "repos" / name) for name in ("one", "two", "punt-labs")]
    result = _run_hook(tmp_path, repos[0])
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    for repo in repos:
        assert yaml.safe_load((repo / _CONFIG).read_text()) == _EXPECTED_CONFIG
        assert yaml.safe_load((repo / _IDENTITY).read_text()) == {
            "name": "Devin",
            "handle": "devin",
            "kind": "agent",
            "email": "158243242+devin-ai-integration[bot]@users.noreply.github.com",
            "github": "devin-ai-integration[bot]",
        }
        exclude = repo / _git(repo, "rev-parse", "--git-path", "info/exclude")
        before = exclude.read_text()
        for relative in (_CONFIG, _IDENTITY):
            assert before.splitlines().count("/" + relative) == 1
            assert _git(repo, "check-ignore", relative) == relative
        assert _run_hook(tmp_path, repos[0]).returncode == 0
        assert exclude.read_text() == before
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"test-sensitive-relay-value" not in path.read_bytes()


def test_preserves_existing_files(tmp_path: Path) -> None:
    """Existing user configuration and identity records are never overwritten."""
    repo = _repo(tmp_path / "project")
    for relative in (_CONFIG, _IDENTITY):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("existing: user-owned\n")
    assert _run_hook(tmp_path, repo).returncode == 0
    for relative in (_CONFIG, _IDENTITY):
        assert (repo / relative).read_text() == "existing: user-owned\n"
    assert _git(repo, "ls-files", "--others", "--exclude-standard").splitlines() == [
        _CONFIG,
        _IDENTITY,
    ]


def test_already_ignored_files(tmp_path: Path) -> None:
    """Repository ignore rules need no redundant local exclusions."""
    repo = _repo(tmp_path / "project")
    (repo / ".gitignore").write_text(".punt-labs/\n")
    exclude = repo / _git(repo, "rev-parse", "--git-path", "info/exclude")
    before = exclude.read_bytes()
    assert _run_hook(tmp_path, repo).returncode == 0
    assert exclude.read_bytes() == before
    assert (repo / _CONFIG).exists()


@pytest.mark.parametrize("relative", [_IDENTITY, _CONFIG])
def test_negation_gets_private_nested_ignore(tmp_path: Path, relative: str) -> None:
    """A repository negation cannot expose newly vended bootstrap files."""
    repo = _repo(tmp_path / "project")
    parent = Path(relative).parent
    (repo / ".gitignore").write_text(f"!{parent}/**\n")
    result = _run_hook(tmp_path, repo)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert (repo / relative).is_file()
    ignore = parent / ".gitignore"
    assert (repo / ignore).read_text() == f"/{Path(relative).name}\n/.gitignore\n"
    assert _git(repo, "check-ignore", relative, str(ignore)).splitlines() == [
        relative,
        str(ignore),
    ]
    assert _git(repo, "status", "--porcelain", "--", relative, str(ignore)) == ""
    before = (repo / ignore).read_bytes()
    assert _run_hook(tmp_path, repo).returncode == 0
    assert (repo / ignore).read_bytes() == before


@pytest.mark.parametrize("relative", [_IDENTITY, _CONFIG])
@pytest.mark.parametrize("existing", ["untracked", "tracked", "deleted", "symlink"])
def test_negation_with_existing_ignore_removes_created_file(
    tmp_path: Path, relative: str, existing: str
) -> None:
    """Existing ignore policy is never overwritten to conceal bootstrap output."""
    repo = _repo(tmp_path / "project")
    parent = Path(relative).parent
    (repo / ".gitignore").write_text(f"!{parent}/**\n")
    ignore = repo / parent / ".gitignore"
    ignore.parent.mkdir(parents=True)
    content = "# user-owned ignore policy\n"
    outside = tmp_path / "outside-ignore"
    if existing == "symlink":
        outside.write_text(content)
        ignore.symlink_to(outside)
    else:
        ignore.write_text(content)
        if existing in {"tracked", "deleted"}:
            _git(repo, "add", "--", str(parent / ".gitignore"))
        if existing == "deleted":
            ignore.unlink()
    result = _run_hook(tmp_path, repo)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert not (repo / relative).exists()
    assert _git(repo, "status", "--porcelain", "--", relative) == ""
    if existing == "deleted":
        assert not ignore.exists()
    else:
        assert ignore.read_text() == content
    if existing == "symlink":
        assert ignore.is_symlink()
        assert outside.read_text() == content


@pytest.mark.parametrize("relative", [_IDENTITY, _CONFIG])
def test_negation_preserves_preexisting_file(tmp_path: Path, relative: str) -> None:
    """Fail-safe removal applies only to files created during this invocation."""
    repo = _repo(tmp_path / "project")
    parent = Path(relative).parent
    (repo / ".gitignore").write_text(f"!{parent}/**\n")
    target = repo / relative
    target.parent.mkdir(parents=True)
    target.write_text("user-owned\n")
    assert _run_hook(tmp_path, repo).returncode == 0
    assert target.read_text() == "user-owned\n"
    assert not (target.parent / ".gitignore").exists()


def test_outside_repositories(tmp_path: Path) -> None:
    """A session with no repositories is a silent successful no-op."""
    result = _run_hook(tmp_path, tmp_path)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_write_failure_is_nonfatal(tmp_path: Path) -> None:
    """An unwritable destination does not prevent session startup."""
    repo = _repo(tmp_path / "project")
    (repo / ".punt-labs").write_text("not a directory")
    result = _run_hook(tmp_path, repo)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    "parent",
    [
        ".punt-labs",
        ".punt-labs/biff",
        ".punt-labs/ethos",
        ".punt-labs/ethos/identities",
    ],
)
def test_symlinked_parents_are_skipped(tmp_path: Path, parent: str) -> None:
    """Repository-controlled parent symlinks cannot redirect bootstrap writes."""
    repo = _repo(tmp_path / "project")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = repo / parent
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    result = _run_hook(tmp_path, repo)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("choice", ["project", "preferred", "fallback", "empty"])
def test_launcher_repo_selection(tmp_path: Path, choice: str) -> None:
    """The launcher honors priority and execs the CLI inside the selected repo."""
    project = tmp_path / "project"
    preferred = tmp_path / "repos/punt-labs"
    fallback = _repo(tmp_path / "repos/another repo") if choice != "empty" else None
    if choice in {"project", "preferred"}:
        _repo(preferred)
    if choice == "project":
        _repo(project)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "biff"
    binary.write_text(
        '#!/bin/sh\nif [ "$1" = version ]; then echo "biff 1.24.0"; exit; fi\n'
        'pwd\nprintf "%s\\n" "$@"\n'
    )
    binary.chmod(0o755)
    result = _launch(
        tmp_path,
        {
            "DEVIN_PROJECT_DIR": str(project),
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        },
    )
    if choice == "empty":
        assert result.returncode == 1
        assert "no git repository found" in result.stderr
    else:
        expected = {"project": project, "preferred": preferred, "fallback": fallback}
        assert result.returncode == 0
        assert result.stdout.splitlines() == [
            str(expected[choice]),
            "mcp",
            "--user",
            "devin",
        ]


@pytest.mark.parametrize(
    ("version", "runner", "prefix"),
    [
        ("1.24.0", "uvx", []),
        ("2.0.0", "uvx", []),
        ("1.23.0", "uvx", ["--from", "punt-biff>=1.24.0", "biff"]),
        ("1.19.0", "uv", ["tool", "run", "--from", "punt-biff>=1.24.0", "biff"]),
        ("unavailable", "", []),
    ],
)
@pytest.mark.parametrize("token", ["", "${MCP_BIFF_RELAY_TOKEN}", "test-only-input"])
@pytest.mark.parametrize("session", ["", "existing-session"])
def test_launcher_environment_and_version(
    tmp_path: Path,
    version: str,
    runner: str,
    prefix: list[str],
    token: str,
    session: str,
) -> None:
    """Launchers select compatible binaries, sanitize auth, and supply presence IDs."""
    repo = _repo(tmp_path / "repos/project")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    awk = shutil.which("awk")
    date = shutil.which("date")
    assert awk is not None and date is not None
    (bin_dir / "awk").symlink_to(awk)
    (bin_dir / "date").symlink_to(date)
    body = (
        '#!/bin/sh\nprintf "%s\\n" "$CLAUDE_CODE_SESSION_ID" '
        '"${BIFF_RELAY_TOKEN+bound}" "$@"\n'
    )
    binary = bin_dir / "biff"
    binary.write_text(
        '#!/bin/sh\nif [ "$1" = version ]; then '
        f'printf "biff %s\\n" "{version}"; exit; fi\n' + body
    )
    binary.chmod(0o755)
    if runner:
        executable = bin_dir / runner
        executable.write_text(body)
        executable.chmod(0o755)
    result = _launch(
        tmp_path,
        {
            "DEVIN_PROJECT_DIR": str(repo),
            "PATH": str(bin_dir),
            "BIFF_RELAY_TOKEN": token,
            "CLAUDE_CODE_SESSION_ID": session,
        },
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert ID_RE.fullmatch(lines[0])
    if session:
        assert lines[0] == session
    assert lines[1] == ("bound" if token == "test-only-input" else "")
    assert lines[2:] == [*prefix, "mcp", "--user", "devin"]
    assert result.stderr == ""


def test_the_devin_server_runs_in_process_because_it_carries_relay_settings() -> None:
    """Devin hands biff its relay in the environment, which biffd cannot carry."""
    manifest = json.loads((_SURFACE / ".mcp.json").read_text())

    assert manifest["mcpServers"]["biff"]["env"]["BIFF_MCP_TRANSPORT"] == "inprocess"
