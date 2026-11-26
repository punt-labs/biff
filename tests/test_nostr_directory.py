"""Public Nostr keys live in ethos's ext store, or in a local YAML file."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from biff.nostr_directory import NostrDirectory


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


def test_a_handle_ethos_refuses_falls_back_to_the_local_file(tmp_path: Path) -> None:
    local = tmp_path / "nostr.yaml"
    directory = NostrDirectory(_FakeEthos(refuse=True), local)
    directory.publish("stranger", {"npub": "npub1s"})
    assert directory.npub("stranger") == "npub1s"


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
