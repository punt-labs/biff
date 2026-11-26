"""A macOS keychain item that trusts no application, driven through ``security``."""

from __future__ import annotations

import re
import secrets
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import final

from biff.key_vault import VaultError, VaultKey

_HEX_RE = re.compile(r"[0-9a-f]{1,128}")
_KEYCHAIN_NOT_FOUND = 44
_SECURITY_TIMEOUT_SECONDS = 300
_RELEASING_RIGHTS = ("decrypt", "export_clear")


class ItemNotFoundError(VaultError):
    """The keychain holds no item of the name asked for."""


@final
class MacKeychainVault:
    """A macOS keychain item that trusts no application.

    ``-T ""`` leaves the item's access list empty, so every read, by any
    process including biff itself, makes macOS ask the person at the screen
    to allow it.  A process that cannot click the dialog cannot read the key.
    The secret goes to ``security -i`` on its standard input, never on a
    command line, where any process could read it from the process table.

    One click on **Always Allow** defeats this: it adds ``/usr/bin/security``
    to the item's access list, and from then on any process of the user can
    read the key with no dialog.  So the vault reads the access list, which
    ``security dump-keychain -a`` prints without releasing any secret, before
    every read and refuses an item that trusts an application.
    """

    __slots__ = ("_keychain", "_run")

    _run: Callable[[list[str], str | None], str]
    _keychain: tuple[str, ...]

    def __new__(
        cls,
        run: Callable[[list[str], str | None], str] | None = None,
        keychain: Path | None = None,
    ) -> MacKeychainVault:
        """Use *run* to invoke ``security``, in *keychain* when given.

        The default keychain is the user's login keychain.  A recording
        stand-in for *run* serves unit tests; a throwaway *keychain* serves
        the manual presence test.
        """
        self = super().__new__(cls)
        self._run = run or MacKeychainVault.run_security
        self._keychain = (str(keychain),) if keychain else ()
        return self

    @property
    def label(self) -> str:
        """Name this vault for a message to the user."""
        if self._keychain:
            return f"the macOS keychain {self._keychain[0]}"
        return "the macOS login keychain"

    def get(self, key: VaultKey) -> str | None:
        """Read the secret; macOS asks the user to allow it.

        Refuses, without asking for the secret, an item whose access list
        trusts an application.
        """
        if self._attributes(key) is None:
            return None
        self._require_closed_access_list(key)
        out = self._call(
            [
                "find-generic-password",
                "-s",
                key.service,
                "-a",
                key.account,
                "-w",
                *self._keychain,
            ],
            None,
            key.where,
        )
        return out.strip()

    def has(self, key: VaultKey) -> bool:
        """Return whether the item exists; attributes are read, not the secret."""
        return self._attributes(key) is not None

    def put(self, key: VaultKey, secret: str) -> None:
        """Create or replace the item with an empty access list.

        Succeeds only when ``security`` reported no error and the item now
        carries the comment this write set, which proves the write landed,
        not merely that an item of that name exists.
        """
        where = key.where
        if not _HEX_RE.fullmatch(secret):
            msg = "keychain secret must be hex, so it needs no shell quoting"
            raise VaultError(msg)
        comment = f"biff:{secrets.token_hex(8)}"
        line = (
            f"add-generic-password -U -s {key.service} -a {key.account} -j {comment} "
            f'-T "" -w {secret}'
            + "".join(f" {self._quote(path)}" for path in self._keychain)
            + "\n"
        )
        output = self._call(["-i"], where=where, stdin=line)
        refused = [ln.strip() for ln in output.splitlines() if "security:" in ln]
        if refused:
            msg = f"keychain write of {where} was refused: {refused[0]}"
            raise VaultError(msg)
        attributes = self._attributes(key)
        if attributes is None or f'"icmt"<blob>="{comment}"' not in attributes:
            msg = f"keychain write of {where} did not land: not the item just written"
            raise VaultError(msg)
        self._require_closed_access_list(key)

    @staticmethod
    def _quote(word: str) -> str:
        """Double-quote *word* for one line of ``security -i`` input."""
        return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _call(self, args: list[str], stdin: str | None = None, where: str = "") -> str:
        """Run ``security``, turning every failure into a ``VaultError``.

        An item that is not in the keychain is the ``ItemNotFoundError`` subclass.
        """
        try:
            return self._run(args, stdin)
        except subprocess.CalledProcessError as exc:
            msg = f"keychain access for {where} failed (exit {exc.returncode})"
            if exc.returncode == _KEYCHAIN_NOT_FOUND:
                raise ItemNotFoundError(msg) from exc
            raise VaultError(msg) from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            msg = f"keychain access for {where} failed: {exc}"
            raise VaultError(msg) from exc

    def _attributes(self, key: VaultKey) -> str | None:
        """Return the item's attribute text, or ``None`` when it does not exist."""
        args = [
            "find-generic-password",
            "-s",
            key.service,
            "-a",
            key.account,
            *self._keychain,
        ]
        try:
            return self._call(args, None, key.where)
        except ItemNotFoundError:
            return None

    def _require_closed_access_list(self, key: VaultKey) -> None:
        """Raise unless the item's access list trusts no application."""
        where = key.where
        keychain = self._keychain or (self._default_keychain(where),)
        dump = self._call(["dump-keychain", "-a", *keychain], None, where)
        entries = self._releasing_entries(dump, key)
        if not entries:
            msg = f"cannot read the access list of {where}; refusing to release it"
            raise VaultError(msg)
        if any("applications (0):" not in entry for entry in entries):
            msg = (
                f"the keychain item {where} now trusts an application, usually after "
                "clicking Always Allow, so any program of yours can read it with no "
                "prompt; delete it with `security delete-generic-password "
                f"-s {key.service} -a {key.account}`, run `biff key init` again and "
                "re-attest your agents; click Allow, never Always Allow"
            )
            raise VaultError(msg)

    def _default_keychain(self, where: str) -> str:
        out = self._call(["default-keychain"], None, where)
        return out.strip().strip('"')

    @staticmethod
    def _releasing_entries(dump: str, key: VaultKey) -> list[str]:
        """Return the access entries that can release the item's secret."""
        blocks = re.split(r"(?m)^keychain: ", dump)
        for block in blocks:
            if (
                f'"svce"<blob>="{key.service}"' not in block
                or f'"acct"<blob>="{key.account}"' not in block
            ):
                continue
            entries = re.split(r"(?m)^    entry \d+:\n", block)[1:]
            return [
                entry
                for entry in entries
                if any(
                    right in MacKeychainVault._authorizations(entry)
                    for right in _RELEASING_RIGHTS
                )
            ]
        return []

    @staticmethod
    def _authorizations(entry: str) -> str:
        """Return the rights an access entry grants, as one string."""
        match = re.search(r"authorizations \(\d+\): (.*)", entry)
        return match[1] if match else ""

    @staticmethod
    def run_security(
        args: list[str],
        stdin: str | None = None,
        timeout: float | None = _SECURITY_TIMEOUT_SECONDS,
    ) -> str:
        """Run macOS ``security`` with *args*, feeding it *stdin*; return its output."""
        done = subprocess.run(
            ["/usr/bin/security", *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
        return done.stdout
