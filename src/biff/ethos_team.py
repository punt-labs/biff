"""The ethos team role graph, read from team YAML on disk."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Self, cast, final

import yaml

from biff.config import load_yaml

logger = logging.getLogger(__name__)

# Team YAML is repo-controlled text that ends up in an MCP tool response,
# so identities and roles are checked against a conservative grammar at
# the boundary instead of trusting whatever the file holds.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REPORTS_TO = "reports_to"


@final
class EthosTeams:
    """The role graph read from ``.punt-labs/ethos/teams/*.yaml``.

    Maps an identity handle to its role, and a role to the role it
    reports to.  The answer is a role name (``"ceo"``), never a person:
    a role can have several holders.
    """

    __slots__ = ("_reports_to", "_roles")

    _roles: dict[str, str]
    _reports_to: dict[str, str]

    def __new__(cls, roles: Mapping[str, str], reports_to: Mapping[str, str]) -> Self:
        self = super().__new__(cls)
        self._roles = dict(roles)
        self._reports_to = dict(reports_to)
        return self

    @classmethod
    def from_repo(cls, repo_root: Path) -> Self:
        """Read every team file under *repo_root*; bad input is skipped.

        Never raises: a missing directory, unreadable or malformed file,
        non-dict document, or malformed entry contributes nothing.  Files
        are read in sorted order and the first team to mention a handle
        (or a role's edge) wins, so the result is deterministic.
        """
        roles: dict[str, str] = {}
        edges: dict[str, str] = {}
        teams_dir = repo_root / ".punt-labs" / "ethos" / "teams"
        try:
            paths = sorted(teams_dir.glob("*.yaml"))
        except OSError:
            paths = []
        for path in paths:
            try:
                doc = load_yaml(path)
            except yaml.YAMLError:
                logger.warning("Skipping malformed team file %s", path)
                continue
            for handle, role in cls._members(doc.get("members")):
                roles.setdefault(handle, role)
            for src, dst in cls._edges(doc.get("collaborations")):
                edges.setdefault(src, dst)
        return cls(roles, edges)

    def manager_of(self, handle: str) -> str:
        """Return the manager role for *handle*; raise LookupError when none."""
        try:
            return self._reports_to[self._roles[handle]]
        except KeyError:
            msg = f"no manager for {handle!r}"
            raise LookupError(msg) from None

    @staticmethod
    def _entries(raw: object) -> list[dict[str, object]]:
        """Return the dict items of *raw* when it is a list, else ``[]``."""
        if not isinstance(raw, list):
            return []
        items = cast("list[object]", raw)
        return [cast("dict[str, object]", i) for i in items if isinstance(i, dict)]

    @staticmethod
    def _name(raw: object) -> str:
        """Return *raw* when it is a valid name, else ``""``."""
        if isinstance(raw, str) and _NAME_RE.match(raw):
            return raw
        return ""

    @classmethod
    def _members(cls, raw: object) -> list[tuple[str, str]]:
        """Return valid ``(identity, role)`` pairs from a ``members`` list."""
        pairs: list[tuple[str, str]] = []
        for entry in cls._entries(raw):
            handle = cls._name(entry.get("identity"))
            role = cls._name(entry.get("role"))
            if handle and role:
                pairs.append((handle, role))
        return pairs

    @classmethod
    def _edges(cls, raw: object) -> list[tuple[str, str]]:
        """Return valid ``(from, to)`` role pairs of type ``reports_to``."""
        pairs: list[tuple[str, str]] = []
        for entry in cls._entries(raw):
            if entry.get("type") != _REPORTS_TO:
                continue
            src = cls._name(entry.get("from"))
            dst = cls._name(entry.get("to"))
            if src and dst:
                pairs.append((src, dst))
        return pairs
