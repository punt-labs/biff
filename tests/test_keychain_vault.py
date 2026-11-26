"""The macOS keychain vault, against a simulator of the ``security`` tool."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from biff.key_vault import VaultError, VaultKey
from biff.keychain_vault import MacKeychainVault

SECRET = "ab" * 32


_DUMP_CLOSED = """\
keychain: "/Users/x/Library/Keychains/login.keychain-db"
version: 256
class: "genp"
attributes:
    0x00000007 <blob>="{service}"
    "acct"<blob>="{account}"
    "icmt"<blob>="{comment}"
    "svce"<blob>="{service}"
access: 3 entries
    entry 0:
        authorizations (1): encrypt
        don't-require-password
        description: {service}
        applications: <null>
    entry 1:
        authorizations (6): decrypt derive export_clear export_wrapped mac sign
        don't-require-password
        description: {service}
        applications (0):
    entry 2:
        authorizations (1): change_acl
        don't-require-password
        description: {service}
        applications (0):
"""

_DUMP_OPEN = _DUMP_CLOSED.replace(
    "        applications (0):\n    entry 2:",
    "        applications (1):\n"
    "            0: /usr/bin/security (OK)\n"
    '                requirement: identifier "com.apple.security" and anchor apple\n'
    "    entry 2:",
)


class _Security:
    """A keychain simulator behind the runner interface of ``MacKeychainVault``.

    ``-i`` reads one command per stdin line, as the real tool does.  The
    ``reject`` and ``drop`` switches model a write that is refused with a
    message on stdout and a write that silently changes nothing.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.items: dict[tuple[str, str], tuple[str, str]] = {}
        self.open_acl = False
        self.reject = False
        self.prompt_first = False
        self.drop = False
        self.failure: Exception | None = None
        self.dump_override: str | None = None

    def __call__(self, args: list[str], stdin: str | None = None) -> str:
        self.calls.append((args, stdin))
        if self.failure:
            raise self.failure
        if args == ["-i"] and stdin is not None:
            return self._add(shlex.split(stdin))
        if args[0] == "default-keychain":
            return '    "/Users/x/Library/Keychains/login.keychain-db"\n'
        if args[0] == "dump-keychain":
            return self._dump()
        key = (args[args.index("-s") + 1], args[args.index("-a") + 1])
        if key not in self.items:
            raise subprocess.CalledProcessError(44, args)
        secret, comment = self.items[key]
        return f"{secret}\n" if "-w" in args else f'"icmt"<blob>="{comment}"\n'

    def _add(self, words: list[str]) -> str:
        if self.reject:
            prompt = "password data for new item:\n" if self.prompt_first else ""
            return (
                prompt
                + "security: SecKeychainItemModifyContent: Interaction not allowed.\n"
            )
        if not self.drop:
            key = (words[words.index("-s") + 1], words[words.index("-a") + 1])
            self.items[key] = (
                words[words.index("-w") + 1],
                words[words.index("-j") + 1],
            )
        return ""

    def _dump(self) -> str:
        if self.dump_override is not None:
            return self.dump_override
        template = _DUMP_OPEN if self.open_acl else _DUMP_CLOSED
        return "".join(
            template.format(service=s, account=a, comment=c)
            for (s, a), (_, c) in self.items.items()
        )

    @property
    def argv(self) -> list[str]:
        """Every argument ever passed to the subprocess."""
        return [word for args, _ in self.calls for word in args]


def _mac(run: _Security, keychain: Path | None = None) -> MacKeychainVault:
    return MacKeychainVault(run, keychain)


def test_mac_vault_secret_is_never_in_argv() -> None:
    run = _Security()
    _mac(run).put(VaultKey("biff-owner", "jfreeman"), SECRET)
    assert SECRET not in " ".join(run.argv)


def test_mac_vault_secret_travels_on_stdin() -> None:
    run = _Security()
    _mac(run).put(VaultKey("biff-owner", "jfreeman"), SECRET)
    assert run.calls[0][0] == ["-i"]
    assert SECRET in (run.calls[0][1] or "")


def test_mac_vault_item_trusts_no_application() -> None:
    run = _Security()
    _mac(run).put(VaultKey("biff-owner", "jfreeman"), SECRET)
    assert ' -T "" ' in (run.calls[0][1] or "")


def test_mac_vault_stdin_is_one_command_line() -> None:
    run = _Security()
    _mac(run).put(VaultKey("biff-owner", "jfreeman"), SECRET)
    assert (run.calls[0][1] or "").count("\n") == 1


def test_mac_vault_reports_a_refusal_that_follows_an_interactive_prompt() -> None:
    run = _Security()
    run.reject = True
    run.prompt_first = True
    with pytest.raises(VaultError, match="Interaction not allowed"):
        _mac(run).put(VaultKey("biff-owner", "jfreeman"), SECRET)


def test_mac_vault_refuses_a_secret_that_is_not_hex() -> None:
    with pytest.raises(VaultError, match="hex"):
        _mac(_Security()).put(VaultKey("biff-owner", "jfreeman"), 'x" -T "any')


def test_mac_vault_quotes_a_keychain_path_with_spaces_and_quotes() -> None:
    run = _Security()
    _mac(run, Path('/tmp/a b/"x".keychain')).put(VaultKey("s", "a"), SECRET)
    assert (run.calls[0][1] or "").endswith(' "/tmp/a b/\\"x\\".keychain"\n')


def test_mac_vault_reads_back_what_it_stored() -> None:
    vault = _mac(_Security())
    vault.put(VaultKey("biff-owner", "jfreeman"), SECRET)
    assert vault.get(VaultKey("biff-owner", "jfreeman")) == SECRET


def test_mac_vault_has_does_not_ask_for_the_secret() -> None:
    run = _Security()
    _mac(run).has(VaultKey("biff-owner", "jfreeman"))
    assert "-w" not in run.calls[0][0]


def test_mac_vault_absence_is_none() -> None:
    assert _mac(_Security()).get(VaultKey("biff-owner", "nobody")) is None


def test_a_rejected_write_is_a_vault_error_carrying_the_message() -> None:
    run = _Security()
    run.reject = True
    with pytest.raises(VaultError, match="Interaction not allowed"):
        _mac(run).put(VaultKey("biff-owner", "jim"), SECRET)


def test_a_rejected_update_of_an_existing_item_is_an_error_too() -> None:
    run = _Security()
    vault = _mac(run)
    vault.put(VaultKey("biff-owner", "jim"), SECRET)
    run.reject = True
    with pytest.raises(VaultError, match="Interaction not allowed"):
        vault.put(VaultKey("biff-owner", "jim"), "cd" * 32)


def test_a_write_that_changes_nothing_is_detected_even_when_the_item_exists() -> None:
    run = _Security()
    vault = _mac(run)
    vault.put(VaultKey("biff-owner", "jim"), SECRET)
    run.drop = True
    with pytest.raises(VaultError, match="did not land"):
        vault.put(VaultKey("biff-owner", "jim"), "cd" * 32)


def test_an_updated_secret_is_the_one_read_back() -> None:
    vault = _mac(_Security())
    vault.put(VaultKey("biff-owner", "jim"), SECRET)
    vault.put(VaultKey("biff-owner", "jim"), "cd" * 32)
    assert vault.get(VaultKey("biff-owner", "jim")) == "cd" * 32


def test_get_refuses_an_item_that_trusts_an_application() -> None:
    run = _Security()
    vault = _mac(run)
    vault.put(VaultKey("biff-owner", "jim"), SECRET)
    run.open_acl = True
    with pytest.raises(VaultError, match="Always Allow"):
        vault.get(VaultKey("biff-owner", "jim"))


def test_refusal_tells_the_user_to_delete_and_recreate_the_item() -> None:
    run = _Security()
    vault = _mac(run)
    vault.put(VaultKey("biff-owner", "jim"), SECRET)
    run.open_acl = True
    with pytest.raises(VaultError, match=r"delete.*biff key init"):
        vault.get(VaultKey("biff-owner", "jim"))


def test_get_never_reads_the_secret_from_an_open_item() -> None:
    run = _Security()
    vault = _mac(run)
    vault.put(VaultKey("biff-owner", "jim"), SECRET)
    run.open_acl = True
    run.calls.clear()
    with pytest.raises(VaultError):
        vault.get(VaultKey("biff-owner", "jim"))
    assert not any("-w" in args for args, _ in run.calls)


def test_the_access_list_is_checked_before_every_read() -> None:
    run = _Security()
    vault = _mac(run)
    vault.put(VaultKey("biff-owner", "jim"), SECRET)
    run.calls.clear()
    vault.get(VaultKey("biff-owner", "jim"))
    vault.get(VaultKey("biff-owner", "jim"))
    dumps = [args for args, _ in run.calls if args[0] == "dump-keychain"]
    assert len(dumps) == 2


def test_get_fails_closed_when_the_access_list_cannot_be_read() -> None:
    run = _Security()
    vault = _mac(run)
    vault.put(VaultKey("biff-owner", "jim"), SECRET)
    run.dump_override = "keychain: nothing parseable here\n"
    with pytest.raises(VaultError, match="access list"):
        vault.get(VaultKey("biff-owner", "jim"))


def test_put_over_an_item_that_now_trusts_an_application_is_refused() -> None:
    run = _Security()
    vault = _mac(run)
    vault.put(VaultKey("biff-owner", "jim"), SECRET)
    run.open_acl = True
    with pytest.raises(VaultError, match="Always Allow"):
        vault.put(VaultKey("biff-owner", "jim"), "cd" * 32)


def test_the_acl_dump_is_scoped_to_the_chosen_keychain() -> None:
    run = _Security()
    _mac(run, Path("/tmp/k.keychain-db")).put(VaultKey("s", "a"), SECRET)
    dumps = [args for args, _ in run.calls if args[0] == "dump-keychain"]
    assert dumps == [["dump-keychain", "-a", "/tmp/k.keychain-db"]]


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(1, ["security"]),
        subprocess.TimeoutExpired(["security"], 300),
        OSError("no such file"),
    ],
)
def test_subprocess_failures_become_vault_errors_naming_the_item(
    failure: Exception,
) -> None:
    run = _Security()
    run.failure = failure
    with pytest.raises(VaultError, match=r"biff-owner/jim"):
        _mac(run).get(VaultKey("biff-owner", "jim"))


def test_mac_vault_names_itself() -> None:
    assert _mac(_Security()).label == "the macOS login keychain"
