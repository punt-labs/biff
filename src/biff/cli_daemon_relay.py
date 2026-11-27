"""Ask biffd, over its protocol, which relay it is connected to."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import final

from biff.client.connection import ClientIdentity, DaemonClient, DaemonError
from biff.run_dir import RunDir

__all__ = ["RelayStatus"]


@final
class RelayStatus:
    """Runs the daemon's ``status`` command as a throwaway ``cli`` client."""

    def status(self, port: int) -> str:
        """Return the relay line biffd reports for the current directory.

        Raises:
            ConnectionError: biffd cannot be reached, refuses the client, or its
                relay is down; the message says which.
        """
        try:
            return asyncio.run(self._ask(port))
        except (OSError, ValueError, DaemonError) as exc:
            raise ConnectionError(str(exc)) from exc

    @staticmethod
    async def _ask(port: int) -> str:
        identity = ClientIdentity(
            client="cli", session_id=f"doctor-{os.getpid()}", cwd=str(Path.cwd())
        )
        client = await DaemonClient.connect(
            run_dir=RunDir.default(), identity=identity, port=port
        )
        try:
            reply = await client.request("status")
        finally:
            await client.close()
        if not isinstance(reply, dict) or reply.get("error") is not False:
            msg = "biffd's status command reported an error"
            raise ConnectionError(msg)
        data = reply.get("data")
        relay = data.get("relay") if isinstance(data, dict) else None
        if not isinstance(relay, str):
            msg = "biffd's status reply names no relay"
            raise ConnectionError(msg)
        return f"relay: {relay}"
