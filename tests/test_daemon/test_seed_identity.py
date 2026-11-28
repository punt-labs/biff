"""The seed identity tells two relays and two accounts apart and shows no secret."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

import pytest

from biff.daemon.harness_records import HarnessRecords
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


def test_two_urls_differing_only_in_path_are_different_identities() -> None:
    a = _daemon({"BIFF_RELAY_URL": "wss://relay.invalid/a"})
    b = _daemon({"BIFF_RELAY_URL": "wss://relay.invalid/b"})
    assert (a.identity() != b.identity(), a.describe() == b.describe()) == (True, True)


def test_a_path_change_is_explained_without_naming_either_path() -> None:
    a = _daemon({"BIFF_RELAY_URL": "wss://relay.invalid/k-SECRET-PATH-7f3a"})
    b = _daemon({"BIFF_RELAY_URL": "wss://relay.invalid/k-OTHER-PATH-91cc"})
    note = RelaySeed.explain_change(a.identity(), b.identity())
    assert ("path differs" in note, "SECRET" in note or "OTHER" in note) == (
        True,
        False,
    )


def test_a_host_change_is_explained_with_both_hosts() -> None:
    a = _daemon({"BIFF_RELAY_URL": "wss://one.invalid:443/p"})
    b = _daemon({"BIFF_RELAY_URL": "wss://two.invalid:443/p"})
    note = RelaySeed.explain_change(a.identity(), b.identity())
    assert ("one.invalid:443" in note, "two.invalid:443" in note) == (True, True)


def test_an_unchanged_identity_has_nothing_to_explain() -> None:
    a = _daemon({"BIFF_RELAY_URL": "wss://one.invalid:443/p"})
    assert RelaySeed.explain_change(a.identity(), a.identity()) == ""


def test_a_secret_in_the_path_is_shown_and_stored_nowhere(tmp_path: Path) -> None:
    secret = "k-SECRET-PATH-7f3a"
    seed = _daemon({"BIFF_RELAY_URL": f"wss://relay.invalid:443/{secret}"})
    SeedRecord(tmp_path / "relay-seed").record(seed)
    HarnessRecords(tmp_path / "records", seed).keep(
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
    assert secret.casefold() not in shown.casefold()


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


def test_two_accounts_in_the_url_userinfo_are_different_identities() -> None:
    a = _daemon({"BIFF_RELAY_URL": "nats://alice:pwA@relay.invalid:4222"})
    b = _daemon({"BIFF_RELAY_URL": "nats://bob:pwB@relay.invalid:4222"})
    assert (a.identity() != b.identity(), a.describe() == b.describe()) == (True, True)


def test_two_hosts_behind_a_userinfo_with_an_at_sign_are_different_identities() -> None:
    a = _daemon({"BIFF_RELAY_URL": "nats://u:p@ss@host-one:4222"})
    b = _daemon({"BIFF_RELAY_URL": "nats://u:p@ss@host-two:4222"})
    assert a.identity() != b.identity()


def test_two_urls_that_cannot_be_displayed_stay_distinct() -> None:
    a = _daemon({"BIFF_RELAY_URL": "nats://one/@relay.invalid"})
    b = _daemon({"BIFF_RELAY_URL": "nats://two/@relay.invalid"})
    assert (a.identity() != b.identity(), "unparseable" in a.describe()) == (True, True)


def test_the_tls_flag_is_part_of_the_identity() -> None:
    base = {"BIFF_RELAY_URL": _URL}
    on = _daemon({**base, "BIFF_RELAY_TLS_HANDSHAKE_FIRST": "true"})
    off = _daemon({**base, "BIFF_RELAY_TLS_HANDSHAKE_FIRST": "false"})
    assert on.identity() != off.identity()


def test_a_control_character_in_the_host_never_reaches_what_is_shown() -> None:
    seed = _daemon({"BIFF_RELAY_URL": "tls://h\x1b[31mX:4222"})
    shown = seed.describe() + seed.identity()
    assert ("\x1b" in shown, "unparseable" in seed.describe()) == (False, True)


def test_a_host_with_odd_characters_is_not_displayed() -> None:
    seed = _daemon({"BIFF_RELAY_URL": "nats://rel ay.invalid:4222"})
    assert "unparseable" in seed.describe()


def test_a_host_change_still_names_both_hosts_and_a_credential_change_does_not() -> (
    None
):
    one = _daemon({"BIFF_RELAY_URL": "nats://a.invalid:4222", "BIFF_RELAY_TOKEN": "x"})
    two = _daemon({"BIFF_RELAY_URL": "nats://b.invalid:4222", "BIFF_RELAY_TOKEN": "x"})
    three = _daemon(
        {"BIFF_RELAY_URL": "nats://a.invalid:4222", "BIFF_RELAY_TOKEN": "y"}
    )
    assert (
        "a.invalid:4222" in RelaySeed.explain_change(one.identity(), two.identity()),
        RelaySeed.explain_change(one.identity(), three.identity()),
    ) == (True, "credentials or path differs")
