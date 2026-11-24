"""Tests for identity resolution (GitHub, OS user, ethos identity files)."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from biff.identity import (
    EthosIdentity,
    GitHubIdentity,
    _ethos_submodule_declared,
    _known_agent_github_logins,
    _read_identity_yaml,
    _SubmoduleDeclaration,
    get_github_identity,
    get_os_user,
    owner_name,
    resolve_agent_identity_from_disk,
)

from ._identity_fixtures import write_gitmodules_ethos

_IDENTITY_LOGGER = "biff.identity"

# chmod-based permission-denial tests don't work when running as root
# (root bypasses the permission bits entirely).
_SKIP_AS_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="permission checks are meaningless as root"
)

# -- get_github_identity (GitHub CLI) --


class TestGetGithubIdentity:
    def test_returns_login_and_name(self) -> None:
        with patch("biff.identity.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "jmf-pobox\tJ Freeman\n"
            result = get_github_identity()
            assert result == GitHubIdentity(login="jmf-pobox", display_name="J Freeman")

    def test_returns_empty_display_name_when_null(self) -> None:
        with patch("biff.identity.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "jmf-pobox\t\n"
            result = get_github_identity()
            assert result is not None
            assert result.login == "jmf-pobox"
            assert result.display_name == ""

    def test_handles_login_only_output(self) -> None:
        with patch("biff.identity.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "jmf-pobox\n"
            result = get_github_identity()
            assert result is not None
            assert result.login == "jmf-pobox"
            assert result.display_name == ""

    def test_returns_none_on_failure(self) -> None:
        with patch("biff.identity.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert get_github_identity() is None

    def test_failure_logs_at_debug_without_stderr(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ``gh api user`` failure (expired token, rate limit, network
        error) must leave a trail -- at debug, not warning, since the same
        code path fires on every machine without ``gh`` configured at all.
        """
        with (
            patch("biff.identity.subprocess.run") as mock_run,
            caplog.at_level(logging.DEBUG, logger=_IDENTITY_LOGGER),
        ):
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = "HTTP 401: private-login: Bad credentials"
            assert get_github_identity() is None
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any(
            "gh api user failed (exit 1)" in r.getMessage() for r in debug_records
        )
        assert "private-login" not in caplog.text
        assert "Bad credentials" not in caplog.text

    def test_returns_none_when_empty(self) -> None:
        with patch("biff.identity.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "  \n"
            assert get_github_identity() is None

    def test_returns_none_when_gh_missing(self) -> None:
        with patch("biff.identity.subprocess.run", side_effect=FileNotFoundError):
            assert get_github_identity() is None


# -- get_os_user --


class TestGetOsUser:
    def test_returns_username(self) -> None:
        with patch("biff.identity.getpass.getuser", return_value="kai"):
            assert get_os_user() == "kai"

    def test_returns_none_on_error(self) -> None:
        with patch("biff.identity.getpass.getuser", side_effect=OSError):
            assert get_os_user() is None


# -- owner_name --


def _init_repo(path: Path, name: str | None) -> Path:
    """Create a git repo at *path*, optionally with a configured user.name."""
    path.mkdir()
    args = [["init", "-q"]]
    if name is not None:
        args.append(["config", "user.name", name])
    for cmd in args:
        subprocess.run(  # noqa: S603
            ["git", "-C", str(path), *cmd],  # noqa: S607
            check=True,
            capture_output=True,
        )
    return path


class TestOwnerName:
    def test_returns_configured_name(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo", "J Freeman")
        assert owner_name(repo) == "J Freeman"

    def test_biff_owner_wins_over_git_user_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A hosted agent's git user.name is the bot; BIFF_OWNER names its human.
        repo = _init_repo(tmp_path / "repo", "Devin AI")
        monkeypatch.setenv("BIFF_OWNER", " jmf-pobox ")
        assert owner_name(repo) == "jmf-pobox"

    def test_blank_biff_owner_falls_back_to_git_user_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _init_repo(tmp_path / "repo", "J Freeman")
        monkeypatch.setenv("BIFF_OWNER", "  ")
        assert owner_name(repo) == "J Freeman"

    def test_strips_one_surrounding_quote_pair(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo", '"jmf-pobox"')
        assert owner_name(repo) == "jmf-pobox"

    def test_strips_only_one_quote_pair(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo", "\"'x'\"")
        assert owner_name(repo) == "'x'"

    def test_unset_returns_empty_and_logs_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _init_repo(tmp_path / "repo", None)
        with caplog.at_level(logging.DEBUG, logger=_IDENTITY_LOGGER):
            assert owner_name(repo) == ""
        assert any(
            r.levelno == logging.DEBUG and "user.name is unset" in r.getMessage()
            for r in caplog.records
        )

    def test_missing_git_returns_empty_and_logs_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch("biff.identity.subprocess.run", side_effect=FileNotFoundError("git")),
            caplog.at_level(logging.DEBUG, logger=_IDENTITY_LOGGER),
        ):
            assert owner_name(tmp_path) == ""
        assert any(
            r.levelno == logging.DEBUG and "could not be read" in r.getMessage()
            for r in caplog.records
        )

    def test_timeout_returns_empty_and_logs_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        timeout = subprocess.TimeoutExpired(cmd="git", timeout=5)
        with (
            patch("biff.identity.subprocess.run", side_effect=timeout),
            caplog.at_level(logging.DEBUG, logger=_IDENTITY_LOGGER),
        ):
            assert owner_name(tmp_path) == ""
        assert any(
            r.levelno == logging.DEBUG and "could not be read" in r.getMessage()
            for r in caplog.records
        )

    def test_generic_subprocess_error_returns_empty_and_logs_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        err = subprocess.SubprocessError("boom")
        with (
            patch("biff.identity.subprocess.run", side_effect=err),
            caplog.at_level(logging.DEBUG, logger=_IDENTITY_LOGGER),
        ):
            assert owner_name(tmp_path) == ""
        assert any(
            r.levelno == logging.DEBUG and "could not be read" in r.getMessage()
            for r in caplog.records
        )

    def test_non_filenotfound_oserror_returns_empty_and_logs_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        err = PermissionError("git not executable")
        with (
            patch("biff.identity.subprocess.run", side_effect=err),
            caplog.at_level(logging.DEBUG, logger=_IDENTITY_LOGGER),
        ):
            assert owner_name(tmp_path) == ""
        assert any(
            r.levelno == logging.DEBUG and "could not be read" in r.getMessage()
            for r in caplog.records
        )

    def test_none_repo_root_runs_in_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _init_repo(tmp_path / "repo", "cwd-user")
        monkeypatch.chdir(repo)
        assert owner_name(None) == "cwd-user"

    def test_none_repo_root_omits_dash_c(self) -> None:
        with patch("biff.identity.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "x\n"
            owner_name(None)
        assert mock_run.call_args.args[0] == ["git", "config", "user.name"]

    def test_repo_root_passes_dash_c(self, tmp_path: Path) -> None:
        with patch("biff.identity.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "x\n"
            owner_name(tmp_path)
        assert mock_run.call_args.args[0] == [
            "git",
            "-C",
            str(tmp_path),
            "config",
            "user.name",
        ]


# -- resolve_agent_identity_from_disk --


def _write_ethos_yaml(repo_root: Path, body: str) -> Path:
    """Write ``.punt-labs/ethos.yaml`` under *repo_root* and return its path."""
    ethos_dir = repo_root / ".punt-labs"
    ethos_dir.mkdir(parents=True, exist_ok=True)
    path = ethos_dir / "ethos.yaml"
    path.write_text(body)
    return path


def _write_identity_yaml(repo_root: Path, handle: str, body: str) -> Path:
    """Write ``.punt-labs/ethos/identities/{handle}.yaml`` and return its path."""
    identities = repo_root / ".punt-labs" / "ethos" / "identities"
    identities.mkdir(parents=True, exist_ok=True)
    path = identities / f"{handle}.yaml"
    path.write_text(body)
    return path


class TestReadIdentityYaml:
    """Every failure mode of ``_read_identity_yaml`` must be observable in
    logs -- this is the function ``_known_agent_github_logins`` depends on
    for the bot-identity cross-check; a silent failure here
    reopens that exact hole.
    """

    def test_missing_file_logs_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_IDENTITY_LOGGER):
            assert _read_identity_yaml(tmp_path / "gone.yaml") is None
        assert caplog.records == []

    @_SKIP_AS_ROOT
    def test_permission_denied_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "claude.yaml"
        path.write_text("kind: agent\ngithub: claude-puntlabs\n")
        path.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
                result = _read_identity_yaml(path)
        finally:
            path.chmod(0o644)
        assert result is None
        assert any(
            "Failed to read identity YAML" in r.getMessage() for r in caplog.records
        )

    def test_invalid_utf8_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "claude.yaml"
        path.write_bytes(b"kind: agent\ngithub: \xff\xfe not valid utf-8\n")
        with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
            result = _read_identity_yaml(path)
        assert result is None
        assert any("not valid UTF-8" in r.getMessage() for r in caplog.records)

    def test_malformed_yaml_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "claude.yaml"
        path.write_text("kind: [unclosed\n")
        with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
            result = _read_identity_yaml(path)
        assert result is None
        assert any(
            "Failed to parse identity YAML" in r.getMessage() for r in caplog.records
        )

    def test_wrong_shape_yaml_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A YAML file that parses to a list (not a mapping) is malformed
        for identity purposes even though it's syntactically valid YAML.
        """
        path = tmp_path / "claude.yaml"
        path.write_text("- kind: agent\n- github: claude-puntlabs\n")
        with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
            result = _read_identity_yaml(path)
        assert result is None
        assert any(
            "did not parse to a mapping" in r.getMessage() for r in caplog.records
        )


# -- _ethos_submodule_declared --


class TestEthosSubmoduleDeclared:
    """Distinguish an absent .gitmodules file from an unreadable one.

    An ambiguous declaration must fail closed rather than authorize an agent
    login through an incomplete identity registry.
    """

    def test_no_gitmodules_file_is_absent(self, tmp_path: Path) -> None:
        assert _ethos_submodule_declared(tmp_path) is _SubmoduleDeclaration.ABSENT

    def test_gitmodules_without_ethos_entry_is_absent(self, tmp_path: Path) -> None:
        (tmp_path / ".gitmodules").write_text(
            '[submodule "vendor/other"]\n'
            "\tpath = vendor/other\n"
            "\turl = git@example.com:x/other.git\n"
        )
        assert _ethos_submodule_declared(tmp_path) is _SubmoduleDeclaration.ABSENT

    def test_gitmodules_with_ethos_entry_is_declared(self, tmp_path: Path) -> None:
        write_gitmodules_ethos(tmp_path)
        assert _ethos_submodule_declared(tmp_path) is _SubmoduleDeclaration.DECLARED

    def test_gitmodules_with_crlf_line_endings_is_declared(
        self, tmp_path: Path
    ) -> None:
        r"""A ``.gitmodules`` checked out with CRLF line endings (Windows,
        or ``core.autocrlf=true``) must still match. The declaration
        regex's trailing ``[ \\t]*$`` does not, on its own, consume a
        ``\\r`` sitting immediately before the ``\\n`` -- a plausible
        latent false negative on any repo checked out with CRLF.
        """
        (tmp_path / ".gitmodules").write_bytes(
            b'[submodule ".punt-labs/ethos"]\r\n'
            b"\tpath = .punt-labs/ethos\r\n"
            b"\turl = git@github.com:punt-labs/team.git\r\n"
        )
        assert _ethos_submodule_declared(tmp_path) is _SubmoduleDeclaration.DECLARED

    @_SKIP_AS_ROOT
    def test_unreadable_gitmodules_is_unverifiable_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ``.gitmodules`` that correctly declares the ethos submodule
        but happens to be unreadable must fail closed (UNVERIFIABLE), not
        be folded into ABSENT -- an unreadable file that in fact declares
        the submodule is exactly the bot-impersonation vulnerability
        DES-053 exists to close, reopened silently.
        """
        write_gitmodules_ethos(tmp_path)
        path = tmp_path / ".gitmodules"
        path.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
                result = _ethos_submodule_declared(tmp_path)
        finally:
            path.chmod(0o644)
        assert result is _SubmoduleDeclaration.UNVERIFIABLE
        assert any("could not be read" in r.getMessage() for r in caplog.records)

    def test_invalid_utf8_gitmodules_is_unverifiable_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handle undecodable .gitmodules content as an ambiguous declaration.

        UnicodeDecodeError is not an OSError subclass, so both need handling.
        """
        (tmp_path / ".gitmodules").write_bytes(b"\xff\xfe not valid utf-8\n")
        with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
            result = _ethos_submodule_declared(tmp_path)
        assert result is _SubmoduleDeclaration.UNVERIFIABLE
        assert any("not valid UTF-8" in r.getMessage() for r in caplog.records)


# -- _known_agent_github_logins --


class TestKnownAgentGithubLogins:
    def test_missing_identities_dir_and_no_gitmodules_entry_is_complete_and_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No ethos integration at all is the common, valid case -- no warning.

        Distinct from ``test_declared_but_uninitialized_submodule_is_
        incomplete_with_warning`` below: both leave
        ``.punt-labs/ethos/identities/`` missing, but only THIS repo has
        no ``.gitmodules`` entry declaring the submodule, so the missing
        directory is trustworthy here.
        """
        with caplog.at_level(logging.DEBUG, logger=_IDENTITY_LOGGER):
            scan = _known_agent_github_logins(tmp_path)
        assert scan.logins == frozenset()
        assert scan.complete is True
        assert not any(r.levelno == logging.WARNING for r in caplog.records)

    def test_declared_but_uninitialized_submodule_is_incomplete_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Reproduces this worktree's exact live state.

        ``.gitmodules`` declares the ``.punt-labs/ethos`` submodule, but
        it was never checked out -- a plain ``git clone`` or
        ``git worktree add`` (neither initializes submodules) leaves
        ``.punt-labs/ethos/identities/`` missing, identical on disk to
        the "no ethos integration" case above. That absence is
        unverifiable here, not "confirmed no bots": the scan must report
        itself incomplete rather than silently trusting an empty result.
        """
        write_gitmodules_ethos(tmp_path)
        with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
            scan = _known_agent_github_logins(tmp_path)
        assert scan.logins == frozenset()
        assert scan.complete is False
        assert any("uninitialized submodule" in r.getMessage() for r in caplog.records)

    @_SKIP_AS_ROOT
    def test_unreadable_identities_dir_is_incomplete_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        identities = tmp_path / ".punt-labs" / "ethos" / "identities"
        identities.mkdir(parents=True)
        identities.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
                scan = _known_agent_github_logins(tmp_path)
        finally:
            identities.chmod(0o755)
        assert scan.complete is False
        assert any("not readable" in r.getMessage() for r in caplog.records)

    def test_agent_entry_with_null_github_field_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_identity_yaml(tmp_path, "claude", "kind: agent\ngithub:\n")  # null
        with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
            scan = _known_agent_github_logins(tmp_path)
        assert scan.logins == frozenset()
        assert scan.complete is False
        assert any("malformed 'github' field" in r.getMessage() for r in caplog.records)

    def test_agent_entry_with_wrong_type_github_field_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_identity_yaml(tmp_path, "claude", "kind: agent\ngithub: 42\n")
        with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
            scan = _known_agent_github_logins(tmp_path)
        assert scan.logins == frozenset()
        assert scan.complete is False
        assert any("malformed 'github' field" in r.getMessage() for r in caplog.records)

    def test_agent_entry_with_empty_github_field_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_identity_yaml(tmp_path, "claude", "kind: agent\ngithub: ''\n")
        with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
            scan = _known_agent_github_logins(tmp_path)
        assert scan.complete is False
        assert any("malformed 'github' field" in r.getMessage() for r in caplog.records)

    def test_agent_entry_missing_github_field_is_ordinary_and_complete(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Most agent identities have no ``github`` field at all -- that's
        normal (only bot accounts registered against a GitHub login need
        one) and must not be treated as a scan failure.
        """
        _write_identity_yaml(tmp_path, "claude", "kind: agent\nname: Claude\n")
        with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
            scan = _known_agent_github_logins(tmp_path)
        assert scan.logins == frozenset()
        assert scan.complete is True
        assert caplog.records == []

    def test_aggregate_summary_logged_when_any_file_fails(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_identity_yaml(tmp_path, "good", "kind: agent\ngithub: good-bot\n")
        _write_identity_yaml(tmp_path, "bad", "kind: [unclosed\n")
        with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
            scan = _known_agent_github_logins(tmp_path)
        assert scan.logins == frozenset({"good-bot"})
        assert scan.complete is False
        assert any(
            "Scanned 2 identity files, 1 incomplete or unreadable" in r.getMessage()
            for r in caplog.records
        )

    def test_github_field_normalized_to_casefolded_stripped_login(
        self, tmp_path: Path
    ) -> None:
        """GitHub logins are case-insensitive -- ``scan.logins`` must store
        the casefolded, whitespace-stripped form so a membership check
        against a resolved (also-normalized) login isn't defeated by an
        identity YAML whose ``github`` field differs only by case or a
        trailing space from ``gh api user``'s canonical ``.login``.
        """
        _write_identity_yaml(
            tmp_path, "claude", "kind: agent\ngithub: ' Claude-PuntLabs '\n"
        )
        scan = _known_agent_github_logins(tmp_path)
        assert scan.logins == frozenset({"claude-puntlabs"})

    @_SKIP_AS_ROOT
    def test_corrupted_bot_identity_file_not_silently_trusted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Reject unverifiable agent identities and log the reason."""
        path = _write_identity_yaml(
            tmp_path, "claude", "kind: agent\ngithub: claude-puntlabs\n"
        )
        path.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger=_IDENTITY_LOGGER):
                scan = _known_agent_github_logins(tmp_path)
        finally:
            path.chmod(0o644)
        assert "claude-puntlabs" not in scan.logins
        assert scan.complete is False, (
            "an incomplete scan must say so -- an empty logins set here is "
            "indistinguishable from 'no bots registered' unless paired "
            "with complete=False"
        )
        assert any(
            "Failed to read identity YAML" in r.getMessage() for r in caplog.records
        )


class TestResolveAgentIdentityFromDisk:
    def test_happy_path(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(
            tmp_path,
            "claude",
            "handle: claude\nname: Claude Agento\nkind: agent\n",
        )
        result = resolve_agent_identity_from_disk(tmp_path)
        assert result == EthosIdentity(
            handle="claude", display_name="Claude Agento", kind="agent"
        )

    def test_legacy_config_yaml_location(self, tmp_path: Path) -> None:
        """Legacy fallback: ``.punt-labs/ethos/config.yaml`` is read when ``ethos.yaml`` is absent."""  # noqa: E501
        legacy = tmp_path / ".punt-labs" / "ethos"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("agent: claude\n")
        _write_identity_yaml(
            tmp_path,
            "claude",
            "handle: claude\nname: Claude Agento\nkind: agent\n",
        )
        result = resolve_agent_identity_from_disk(tmp_path)
        assert result is not None
        assert result.handle == "claude"

    def test_missing_ethos_yaml(self, tmp_path: Path) -> None:
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_missing_identity_yaml(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_malformed_ethos_yaml(self, tmp_path: Path) -> None:
        # Unclosed bracket forces yaml.YAMLError.
        _write_ethos_yaml(tmp_path, "agent: [unclosed\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_malformed_identity_yaml(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(tmp_path, "claude", "kind: [agent\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_missing_agent_field(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "team: engineering\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_empty_agent_field(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: ''\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_whitespace_agent_field(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: '   '\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_non_string_agent_field(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: 42\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_kind_human_rejected(self, tmp_path: Path) -> None:
        """``kind: human`` must NOT be elevated into the agent slot."""
        _write_ethos_yaml(tmp_path, "agent: jfreeman\n")
        _write_identity_yaml(
            tmp_path,
            "jfreeman",
            "handle: jfreeman\nname: Jim Freeman\nkind: human\n",
        )
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_kind_missing_rejected(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(tmp_path, "claude", "handle: claude\nname: Claude\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    @pytest.mark.parametrize(
        "bad_handle",
        [
            "../../etc/passwd",
            "../foo",
            "foo/bar",
            ".hidden",
            "Claude",  # uppercase
            "claude!",  # special char
            "a" * 65,  # too long
            "-leading-hyphen",
            "_leading-underscore",
        ],
    )
    def test_path_traversal_and_invalid_handles_rejected(
        self, tmp_path: Path, bad_handle: str
    ) -> None:
        """The handle regex blocks every path-traversal payload before any FS read."""
        _write_ethos_yaml(tmp_path, f"agent: {bad_handle!r}\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_handle_outside_identities_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A handle that passes the regex but resolves outside identities/ is rejected.

        The regex blocks ``/``, ``..``, etc. -- to exercise the
        ``is_relative_to`` guard we replace ``Path.resolve`` so the
        resolved identity path falls outside the identities directory.
        """
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(
            tmp_path, "claude", "handle: claude\nname: Claude\nkind: agent\n"
        )

        identities_root = (tmp_path / ".punt-labs" / "ethos" / "identities").resolve()
        outside = tmp_path / "outside.yaml"
        outside.write_text("handle: claude\nname: Claude\nkind: agent\n")

        original_resolve = Path.resolve

        def fake_resolve(self: Path, *args: object, **kwargs: object) -> Path:
            if self.name == "claude.yaml" and self.parent == identities_root:
                return outside
            return original_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "resolve", fake_resolve)
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_handle_falls_back_to_agent_field(self, tmp_path: Path) -> None:
        """When the identity YAML omits ``handle``, fall back to the agent field."""
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(tmp_path, "claude", "name: Claude Agento\nkind: agent\n")
        result = resolve_agent_identity_from_disk(tmp_path)
        assert result is not None
        assert result.handle == "claude"

    def test_display_name_falls_back_to_handle(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(tmp_path, "claude", "handle: claude\nkind: agent\n")
        result = resolve_agent_identity_from_disk(tmp_path)
        assert result is not None
        assert result.display_name == "claude"

    def test_yaml_handle_mismatch_uses_validated_agent(self, tmp_path: Path) -> None:
        """A malicious identity YAML declaring a different handle must not impersonate.

        The resolved handle must always equal the validated ``agent``
        string derived from the filename, not the YAML's ``handle``
        field.
        """
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(
            tmp_path,
            "claude",
            "handle: evil-impersonator\nname: Claude Agento\nkind: agent\n",
        )
        result = resolve_agent_identity_from_disk(tmp_path)
        assert result is not None
        assert result.handle == "claude"
        assert result.handle != "evil-impersonator"
