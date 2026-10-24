"""Explicit, fail-closed Ethos identity for one OpenCode MCP process."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from biff.config import EthosIdentity
from biff.session_id import SessionHint

_SESSION_ID = re.compile(r"ses_[a-zA-Z0-9]+\Z")
_PERSONA = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class EthosSessionPin:
    """Raw harness ID and exact participant ID; never a shared default."""

    session_id: str
    agent_id: str

    def __post_init__(self) -> None:
        if not _SESSION_ID.fullmatch(self.session_id):
            raise ValueError("Expected an OpenCode session ID (ses_<alphanumeric>).")
        if self.agent_id != self.session_id:
            raise ValueError("--ethos-agent-id must equal --ethos-session.")

    @property
    def routing_id(self) -> str:
        """Domain-separated routing token, stable across process restarts."""
        return SessionHint.derive_routing_id(
            self.session_id, "biff.opencode.session.v1"
        )

    def unread_path(self, directory: Path) -> Path:
        return directory / f"opencode-{self.routing_id}.json"

    def _read_json(self, arguments: list[str]) -> dict[str, object]:
        env = dict(
            os.environ, ETHOS_SESSION=self.session_id, ETHOS_AGENT_ID=self.agent_id
        )
        try:
            result = subprocess.run(  # noqa: S603
                ["ethos", *arguments],  # noqa: S607
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Pinned Ethos identity read failed.") from exc
        if result.returncode != 0:
            raise ValueError("Pinned Ethos identity command failed.")
        try:
            raw: object = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("Pinned Ethos identity returned invalid JSON.") from exc
        if not isinstance(raw, dict):
            raise ValueError("Pinned Ethos identity must be a JSON object.")
        return cast("dict[str, object]", raw)

    def resolve_identity(self) -> EthosIdentity:
        """Read the exact existing participant, then its authoritative persona."""
        roster = self._read_json(["session", "roster", self.session_id, "--json"])
        participants = roster.get("participants")
        if roster.get("session") != self.session_id or not isinstance(
            participants, list
        ):
            raise ValueError("Ethos roster does not match the pinned session.")
        matches = [
            cast("dict[str, object]", participant)
            for participant in cast("list[object]", participants)
            if isinstance(participant, dict)
            and cast("dict[str, object]", participant).get("agent_id") == self.agent_id
        ]
        if len(matches) != 1:
            raise ValueError("Pinned Ethos participant must exist exactly once.")
        persona = matches[0].get("persona")
        if not isinstance(persona, str) or not _PERSONA.fullmatch(persona):
            raise ValueError("Pinned Ethos participant has no valid persona.")
        identity = self._read_json(["show", persona, "--json"])
        name = identity.get("name")
        if (
            identity.get("handle") != persona
            or identity.get("kind") != "agent"
            or not isinstance(name, str)
            or not name.strip()
        ):
            raise ValueError("Pinned Ethos persona must be a named agent identity.")
        return EthosIdentity(handle=persona, display_name=name, kind="agent")
