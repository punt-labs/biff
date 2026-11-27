"""How a failed WebSocket handshake reaches biffd's log."""

from __future__ import annotations

import logging
from typing import ClassVar, final, override

__all__ = ["HandshakeLog"]


@final
class HandshakeLog(logging.Filter):
    """Turns websockets' ERROR-with-traceback for a failed handshake into one line.

    A port probe (a connect, then close, with no request) fails the handshake the
    same way a broken client does, and websockets logs both at ERROR with a
    traceback.  A probe is routine, so it becomes one DEBUG line; a client that
    sent something that is not HTTP is a WARNING with its reason and no traceback.
    """

    LOGGER_NAME: ClassVar[str] = "websockets.server"
    _MESSAGE: ClassVar[str] = "opening handshake failed"

    @classmethod
    def install(cls) -> None:
        """Attach the filter to websockets' server logger, once."""
        server = logging.getLogger(cls.LOGGER_NAME)
        if not any(isinstance(existing, cls) for existing in server.filters):
            server.addFilter(cls())

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite a handshake failure's level and drop its traceback; keep the rest."""
        if record.getMessage() != self._MESSAGE or record.exc_info is None:
            return True
        error = record.exc_info[1]
        cause = None if error is None else error.__cause__
        record.exc_info = None
        record.exc_text = None
        if isinstance(cause, EOFError | ConnectionResetError):
            record.levelno, record.levelname = logging.DEBUG, "DEBUG"
            record.msg = "port probe: connection closed before a request"
            record.args = ()
        else:
            record.levelno, record.levelname = logging.WARNING, "WARNING"
            record.msg = "opening handshake failed: %s (%s)"
            record.args = (error, cause)
        return True
