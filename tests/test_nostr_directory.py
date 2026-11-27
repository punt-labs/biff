"""Public Nostr keys live in ethos's ext store, or in a local YAML file."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from biff import nostr_directory
from biff.models import UserSession
from biff.nostr_directory import LOOKUP_FAILED, EthosExt, NostrDirectory, SessionKeys


class _FakeEthos:
    """An ethos ext store in memory; ``refuse`` makes every write fail."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.data: dict[str, dict[str, str]] = {}
        self.refuse = refuse

    def get(self, handle: str) -> dict[str, str]:
        return dict(self.data.get(handle, {}))

    def set(self, handle: str, key: str, value: str) -> None:
        if self.refuse:
            msg = "no such identity"
            raise OSError(msg)
        self.data.setdefault(handle, {})[key] = value


def test_publish_goes_through_ethos(tmp_path: Path) -> None:
    ethos = _FakeEthos()
    NostrDirectory(ethos, tmp_path / "local.yaml").publish("jf", {"npub": "npub1a"})
    assert ethos.data == {"jf": {"npub": "npub1a"}}


def test_publish_with_ethos_writes_no_local_file(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    NostrDirectory(_FakeEthos(), local).publish("jf", {"npub": "npub1a"})
    assert not local.exists()


def test_publish_merges_with_existing_fields(tmp_path: Path) -> None:
    directory = NostrDirectory(_FakeEthos(), tmp_path / "local.yaml")
    directory.publish("jf", {"npub": "npub1a"})
    directory.publish("jf", {"auth_sig": "ff"})
    assert directory.fields("jf") == {"npub": "npub1a", "auth_sig": "ff"}


def test_without_ethos_publish_uses_the_local_file(tmp_path: Path) -> None:
    local = tmp_path / "biff" / "nostr.yaml"
    NostrDirectory(None, local).publish("jf", {"npub": "npub1a"})
    assert yaml.safe_load(local.read_text()) == {"jf": {"nostr": {"npub": "npub1a"}}}


def test_without_ethos_fields_read_back(tmp_path: Path) -> None:
    directory = NostrDirectory(None, tmp_path / "nostr.yaml")
    directory.publish("claude", {"npub": "npub1x"})
    directory.publish("claude", {"owner_npub": "npub1o"})
    assert directory.fields("claude") == {"npub": "npub1x", "owner_npub": "npub1o"}


def test_ethos_refusal_is_an_error_naming_what_failed(tmp_path: Path) -> None:
    directory = NostrDirectory(_FakeEthos(refuse=True), tmp_path / "nostr.yaml")
    with pytest.raises(OSError, match="published nowhere"):
        directory.publish("stranger", {"npub": "npub1s"})


def test_ethos_refusal_writes_nothing_to_the_local_file(tmp_path: Path) -> None:
    local = tmp_path / "nostr.yaml"
    with pytest.raises(OSError, match="published nowhere"):
        NostrDirectory(_FakeEthos(refuse=True), local).publish("s", {"npub": "n"})
    assert not local.exists()


def test_failed_republish_does_not_leave_a_stale_npub_winning(tmp_path: Path) -> None:
    ethos = _FakeEthos()
    directory = NostrDirectory(ethos, tmp_path / "nostr.yaml")
    directory.publish("jim", {"npub": "npub1OLD"})
    ethos.refuse = True
    with pytest.raises(OSError, match="published nowhere"):
        directory.publish("jim", {"npub": "npub1NEW"})
    assert directory.npub("jim") == "npub1OLD"


def test_ethos_reads_ignore_a_local_file_from_before_ethos(tmp_path: Path) -> None:
    local = tmp_path / "nostr.yaml"
    NostrDirectory(None, local).publish("jim", {"npub": "npub1OLD", "auth_sig": "aa"})
    directory = NostrDirectory(_FakeEthos(), local)
    directory.publish("jim", {"npub": "npub1NEW"})
    assert directory.fields("jim") == {"npub": "npub1NEW"}


def test_ethos_with_nothing_set_does_not_fall_back_to_the_local_file(
    tmp_path: Path,
) -> None:
    local = tmp_path / "nostr.yaml"
    NostrDirectory(None, local).publish("jim", {"npub": "npub1OLD"})
    assert NostrDirectory(_FakeEthos(), local).npub("jim") is None


def test_unknown_handle_has_no_npub(tmp_path: Path) -> None:
    assert NostrDirectory(_FakeEthos(), tmp_path / "n.yaml").npub("nobody") is None


def test_handle_that_escapes_the_directory_is_refused(tmp_path: Path) -> None:
    directory = NostrDirectory(None, tmp_path / "nostr.yaml")
    with pytest.raises(ValueError, match="handle"):
        directory.publish("../evil", {"k": "v"})


def test_malformed_local_file_reads_as_no_fields(tmp_path: Path) -> None:
    local = tmp_path / "nostr.yaml"
    local.write_text("- not\n- a mapping\n")
    assert NostrDirectory(None, local).fields("jf") == {}


def test_publish_never_writes_into_the_repository_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    identities = repo / ".punt-labs" / "ethos" / "identities"
    identities.mkdir(parents=True)
    (identities / "jf.yaml").write_text("handle: jf\n")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "no-ethos"))
    before = sorted(p.relative_to(repo) for p in repo.rglob("*"))
    NostrDirectory.for_repo(repo).publish("jf", {"npub": "npub1a"})
    assert sorted(p.relative_to(repo) for p in repo.rglob("*")) == before


@pytest.mark.parametrize("content", ["bob: [unclosed\n", "- a\n- list\n", "\x00\xff"])
def test_publish_never_overwrites_an_unreadable_local_file(
    tmp_path: Path, content: str
) -> None:
    local = tmp_path / "nostr.yaml"
    local.write_text(content, encoding="latin-1")
    before = local.read_bytes()
    with pytest.raises(OSError, match=str(local)):
        NostrDirectory(None, local).publish("carol", {"npub": "npub1carol"})
    assert local.read_bytes() == before


def test_publish_error_says_how_to_fix_the_file(tmp_path: Path) -> None:
    local = tmp_path / "nostr.yaml"
    local.write_text("bob: [unclosed\n")
    with pytest.raises(OSError, match="fix or move"):
        NostrDirectory(None, local).publish("carol", {"npub": "npub1carol"})


def test_fields_degrade_to_empty_on_an_unreadable_local_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    local = tmp_path / "nostr.yaml"
    local.write_text("bob: [unclosed\n")
    assert NostrDirectory(None, local).fields("bob") == {}
    assert str(local) in caplog.text


_NOT_FOUND = 'echo \'ethos: namespace "nostr" not found for "jim"\' >&2; exit 1'


def _script(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


def test_ethos_not_found_namespace_means_unset(tmp_path: Path) -> None:
    ethos = _script(tmp_path, "ethos", _NOT_FOUND)
    assert EthosExt(ethos, tmp_path).get("jim") == {}


def test_ethos_other_failure_is_an_error_with_exit_code_and_stderr(
    tmp_path: Path,
) -> None:
    body = "echo 'ethos: identity store corrupt' >&2; exit 3"
    ethos = _script(tmp_path, "ethos", body)
    with pytest.raises(OSError, match=r"exit 3.*identity store corrupt"):
        EthosExt(ethos, tmp_path).get("jim")


def test_ethos_timeout_is_an_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nostr_directory, "_ETHOS_TIMEOUT_SECONDS", 1)
    ethos = _script(tmp_path, "ethos", "exec sleep 30")
    with pytest.raises(OSError, match="timed out"):
        EthosExt(ethos, tmp_path).get("jim")


def test_ethos_json_is_read(tmp_path: Path) -> None:
    ethos = _script(tmp_path, "ethos", """echo '{"npub": "npub1x"}'""")
    assert EthosExt(ethos, tmp_path).get("jim") == {"npub": "npub1x"}


async def test_session_npubs_marks_a_failed_lookup(tmp_path: Path) -> None:
    ethos = _script(tmp_path, "ethos", "echo boom >&2; exit 3")
    directory = NostrDirectory(EthosExt(ethos, tmp_path), tmp_path / "n.yaml")
    found = await SessionKeys(directory).lookup([UserSession(user="jim", tty="a")])
    assert found == {"jim": LOOKUP_FAILED}


async def test_session_npubs_survives_an_ethos_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nostr_directory, "_ETHOS_TIMEOUT_SECONDS", 1)
    ethos = _script(tmp_path, "ethos", "exec sleep 30")
    directory = NostrDirectory(EthosExt(ethos, tmp_path), tmp_path / "n.yaml")
    found = await SessionKeys(directory).lookup([UserSession(user="jim", tty="a")])
    assert found == {"jim": LOOKUP_FAILED}


async def test_session_npubs_leaves_an_unset_user_out(tmp_path: Path) -> None:
    ethos = _script(tmp_path, "ethos", _NOT_FOUND)
    directory = NostrDirectory(EthosExt(ethos, tmp_path), tmp_path / "n.yaml")
    assert await SessionKeys(directory).lookup([UserSession(user="jim", tty="a")]) == {}


async def test_session_npubs_skips_a_user_that_is_not_a_handle(tmp_path: Path) -> None:
    directory = NostrDirectory(None, tmp_path / "n.yaml")
    sessions = [UserSession(user="Not A Handle!", tty="a")]
    assert await SessionKeys(directory).lookup(sessions) == {}
