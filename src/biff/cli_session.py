"""Pseudo-ephemeral CLI sessions for product commands.

Each ``biff <command>`` invocation needs a NATS relay to call relay
methods. This module manages a reusable session: two back-to-back
``biff who`` calls share the same tty/session identity. A 5-minute
TTL on the local session file triggers a fresh session.

Session file: ``~/.biff/cli-sessions/{repo_name}.json``

The NATS session persists until its own KV TTL expires (3 days).
The local file just tracks which tty to reconnect to.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from biff.config import load_config
from biff.models import BiffConfig, UserSession
from biff.nats_relay import NatsRelay
from biff.relay import Relay
from biff.tty import generate_tty, get_hostname, get_pwd

_SESSION_TTL = timedelta(minutes=5)
_SESSION_DIR = Path.home() / ".biff" / "cli-sessions"


@dataclass(frozen=True)
class CliContext:
    """Context for a CLI command: relay connection + identity."""

    relay: Relay
    config: BiffConfig
    session_key: str
    user: str
    tty: str


def _session_path(repo_name: str) -> Path:
    """Path to the local session file for a repo."""
    return _SESSION_DIR / f"{repo_name}.json"


def _load_session(repo_name: str) -> tuple[str, str] | None:
    """Load a valid (non-expired) session from disk.

    Returns ``(user, tty)`` or ``None`` if expired/missing.
    """
    path = _session_path(repo_name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        last_active = datetime.fromisoformat(data["last_active"])
        if last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=UTC)
        if datetime.now(UTC) - last_active > _SESSION_TTL:
            return None
        user: str = data["user"]
        tty: str = data["tty"]
        return user, tty
    except (json.JSONDecodeError, KeyError, ValueError, OSError, TypeError):
        return None


def _save_session(repo_name: str, user: str, tty: str) -> None:
    """Save session to disk with current timestamp."""
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "user": user,
        "tty": tty,
        "tty_name": "cli",
        "last_active": datetime.now(UTC).isoformat(),
    }
    _session_path(repo_name).write_text(json.dumps(data, indent=2) + "\n")


@asynccontextmanager
async def cli_relay() -> AsyncIterator[CliContext]:
    """Provide a NatsRelay + session for a CLI command.

    Reuses an existing session if the local file is fresh (< 5 min).
    Creates a new session otherwise. Updates ``last_active`` on exit.

    Requires NATS — exits with an error if no relay_url is configured.
    """
    resolved = load_config()
    config = resolved.config

    if not config.relay_url:
        msg = "CLI commands require a NATS relay. Configure relay_url in .biff."
        raise ValueError(msg)

    relay = NatsRelay(
        url=config.relay_url,
        auth=config.relay_auth,
        name=f"biff-cli-{config.user}",
        repo_name=config.repo_name,
    )

    # Reuse or create session (invalidate if user identity changed)
    existing = _load_session(config.repo_name)
    if existing is not None and existing[0] == config.user:
        user, tty = existing
    else:
        user = config.user
        tty = generate_tty()

    session_key = f"{user}:{tty}"

    try:
        # Register/heartbeat the session
        await relay.update_session(
            UserSession(
                user=user,
                tty=tty,
                tty_name="cli",
                hostname=get_hostname(),
                pwd=get_pwd(),
            )
        )

        yield CliContext(
            relay=relay,
            config=config,
            session_key=session_key,
            user=user,
            tty=tty,
        )

        # Update last_active on success
        _save_session(config.repo_name, user, tty)
    finally:
        await relay.close()
