"""The seed identity tells two relays and two accounts apart and shows no secret."""

from __future__ import annotations

import hashlib
import stat
from typing import TYPE_CHECKING, Final

import pytest

from biff.daemon.harness_records import HarnessRecords
from biff.daemon.seed_key import SeedKey
from biff.daemon.seed_record import SeedRecord
from biff.frame_client import Hello
from biff.relay_seed import RelaySeed

if TYPE_CHECKING:
    from pathlib import Path

_KEY: Final = b"k" * 32
_URL: Final = "nats://relay.invalid:4222"


def _daemon(environ: dict[str, str], key: bytes = _KEY) -> RelaySeed:
    return RelaySeed.for_daemon(environ, key)


def test_the_same_host_with_another_token_is_another_identity() -> None:
    a = _daemon({"BIFF_RELAY_URL": _URL, "BIFF_RELAY_TOKEN": "tok-A"})
    b = _daemon({"BIFF_RELAY_URL": _URL, "BIFF_RELAY_TOKEN": "tok-B"})
    assert a.identity() != b.identity()


def test_the_same_token_is_the_same_identity() -> None:
    a = _daemon({"BIFF_RELAY_URL": _URL, "BIFF_RELAY_TOKEN": "tok-A"})
    b = _daemon({"BIFF_RELAY_URL": _URL, "BIFF_RELAY_TOKEN": "tok-A"})
    assert a.identity() == b.identity()


def test_a_token_is_not_recoverable_from_the_identity_without_the_key() -> None:
    seed = _daemon({"BIFF_RELAY_URL": _URL, "BIFF_RELAY_TOKEN": "tok-A"})
    bare = hashlib.sha256(b"tok-A").hexdigest()
    assert ("tok-A" in seed.identity(), bare in seed.identity()) == (False, False)


def test_another_key_makes_another_identity() -> None:
    environ = {"BIFF_RELAY_URL": _URL, "BIFF_RELAY_TOKEN": "tok-A"}
    assert _daemon(environ).identity() != _daemon(environ, b"j" * 32).identity()


def test_another_credentials_file_is_another_identity(tmp_path: Path) -> None:
    one, two = tmp_path / "one.creds", tmp_path / "two.creds"
    one.write_text("x")
    two.write_text("x")
    a = _daemon({"BIFF_RELAY_URL": _URL, "BIFF_RELAY_USER_CREDENTIALS": str(one)})
    b = _daemon({"BIFF_RELAY_URL": _URL, "BIFF_RELAY_USER_CREDENTIALS": str(two)})
    assert a.identity() != b.identity()


def test_the_whole_url_without_userinfo_is_the_relay() -> None:
    a = RelaySeed.of({"BIFF_RELAY_URL": "wss://relay.invalid/a"})
    b = RelaySeed.of({"BIFF_RELAY_URL": "wss://relay.invalid/b"})
    assert a.identity() != b.identity()


def test_the_case_of_the_host_and_a_trailing_slash_do_not_matter() -> None:
    a = RelaySeed.of({"BIFF_RELAY_URL": "nats://RELAY.invalid:4222/"})
    b = RelaySeed.of({"BIFF_RELAY_URL": "nats://relay.invalid:4222"})
    assert a.identity() == b.identity()


_SECRET_URLS = [
    ("nats://uSeR:pA55/w0rd@relay.invalid:4222", ("uSeR", "pA55", "w0rd")),
    ("nats://s3cr3ttoken/@relay.invalid", ("s3cr3ttoken",)),
    ("nats://relay.invalid:4222/?auth=Qs3cret", ("Qs3cret",)),
    ("nats://relay.invalid:4222/#Fr4gSecret", ("Fr4gSecret",)),
    ("nats://u:p?x@relay.invalid:4222/p?t=Tail5ecret@h", ("u:p", "Tail5ecret")),
]


@pytest.mark.parametrize(("url", "secrets"), _SECRET_URLS)
def test_no_part_of_a_url_secret_reaches_what_is_shown_or_stored(
    url: str, secrets: tuple[str, ...], tmp_path: Path
) -> None:
    seed = RelaySeed.of({"BIFF_RELAY_URL": url})
    SeedRecord(tmp_path / "relay-seed").record(seed)
    records = HarnessRecords(tmp_path / "records", seed)
    records.keep(
        Hello(mac="0" * 64, client="mcp", session_id="s-1", cwd="/w", user="kai")
    )
    shown = " ".join(
        [
            seed.describe(),
            seed.identity(),
            repr(seed),
            (tmp_path / "relay-seed").read_text(),
            *(p.read_text() for p in (tmp_path / "records").glob("*.json")),
        ]
    )
    assert [secret for secret in secrets if secret in shown] == []


def test_a_url_that_cannot_be_read_is_named_unparseable() -> None:
    assert "unparseable" in RelaySeed.of({"BIFF_RELAY_URL": "not a url"}).describe()


def test_the_key_is_private_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "relay-seed.key"
    first = SeedKey(path).load()
    assert (len(first), stat.S_IMODE(path.stat().st_mode)) == (32, 0o600)
    assert SeedKey(path).load() == first


def test_a_key_file_that_is_not_private_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "relay-seed.key"
    SeedKey(path).load()
    path.chmod(0o644)
    with pytest.raises(ValueError, match=r"relay-seed\.key"):
        SeedKey(path).load()
