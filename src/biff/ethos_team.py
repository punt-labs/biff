"""The ethos team role graph, read from team YAML on disk."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self, cast, final

import yaml

from biff.config import get_repo_slug, load_yaml

logger = logging.getLogger(__name__)

# Team YAML is repo-controlled text that ends up in an MCP tool response,
# so identities and roles are checked against a conservative grammar at
# the boundary instead of trusting whatever the file holds.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REPORTS_TO = "reports_to"


@dataclass(frozen=True, slots=True)
class RoleEntry:
    """One role a handle holds in one team, and who it reports to there.

    ``manager_role`` and ``manager_person`` are empty when the team names no
    manager, or no member holds the manager role.
    """

    role: str
    team: str
    manager_role: str = ""
    manager_person: str = ""


@final
class EthosTeams:
    """The role graphs read from ``.punt-labs/ethos/teams/*.yaml``.

    One graph per team: it maps an identity handle to its role, and a role
    to the role it reports to.  Roles are shared names across teams, so a
    handle is only ever resolved through the team it is a member of.  The
    answer to :meth:`manager_of` is a role name (``"ceo"``), never a person:
    a role can have several holders.  :meth:`roles_of` also names the
    first member holding the manager role.
    """

    __slots__ = ("_teams",)

    _teams: tuple[tuple[dict[str, str], dict[str, str], str], ...]

    def __new__(
        cls, teams: Iterable[tuple[Mapping[str, str], Mapping[str, str], str]]
    ) -> Self:
        self = super().__new__(cls)
        self._teams = tuple(
            (dict(roles), dict(edges), name) for roles, edges, name in teams
        )
        return self

    @classmethod
    def from_repo(cls, repo_root: Path, repo_slug: str) -> Self:
        """Read the team files under *repo_root* that select *repo_slug*.

        Mirrors ethos: a team applies to a repository only when its
        ``repositories`` list names the ``owner/repo`` slug exactly, so a
        team with no list is never selected.  Never raises: a missing
        directory, unreadable or malformed file, non-dict document, or
        malformed entry contributes nothing.  Files are read in sorted
        order and the first selected team that resolves a handle wins, so
        the result is deterministic.
        """
        teams: list[tuple[dict[str, str], dict[str, str], str]] = []
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
            if not cls._selects(doc.get("repositories"), repo_slug):
                continue
            roles: dict[str, str] = {}
            for handle, role in cls._members(doc.get("members")):
                roles.setdefault(handle, role)
            edges: dict[str, str] = {}
            for src, dst in cls._edges(doc.get("collaborations")):
                edges.setdefault(src, dst)
            teams.append((roles, edges, cls._name(doc.get("name")) or path.stem))
        return cls(teams)

    @classmethod
    def for_repo_root(cls, repo_root: Path | None) -> Self:
        """Return the teams of the repository at *repo_root*; none when unknown."""
        if repo_root is None:
            return cls(())
        return cls.from_repo(repo_root, get_repo_slug(repo_root) or "")

    def manager_of(self, handle: str) -> str:
        """Return the manager role for *handle*; raise LookupError when none."""
        for roles, edges, _ in self._teams:
            manager = edges.get(roles.get(handle, ""))
            if manager:
                return manager
        msg = f"no manager for {handle!r}"
        raise LookupError(msg)

    def roles_of(self, handle: str) -> tuple[RoleEntry, ...]:
        """Return one entry per selected team *handle* is a member of.

        Empty when no team names the handle.  Order follows the sorted team
        files, so the result is deterministic.
        """
        entries: list[RoleEntry] = []
        for roles, edges, team in self._teams:
            role = roles.get(handle)
            if not role:
                continue
            manager_role = edges.get(role, "")
            person = next(
                (h for h, r in roles.items() if manager_role and r == manager_role),
                "",
            )
            entries.append(RoleEntry(role, team, manager_role, person))
        return tuple(entries)

    @staticmethod
    def _selects(raw: object, repo_slug: str) -> bool:
        """Return whether the ``repositories`` list *raw* names *repo_slug*."""
        if not isinstance(raw, list):
            return False
        return repo_slug in cast("list[object]", raw)

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
