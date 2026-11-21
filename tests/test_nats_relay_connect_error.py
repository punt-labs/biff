"""Unit tests for connect-failure secret hygiene in ``_open_connection``.

Every call site that expands ``RelayAuth.as_nats_kwargs()`` into
``nats.connect()`` must raise a redacted ``RelayConnectError`` whose
``__context__`` is genuinely ``None`` -- not merely display-suppressed via
``from None`` -- and must clear the auth kwargs dict in a ``finally`` block
regardless of outcome (docs/relay-env-overrides.md Sec 5 item 6).

These tests mock the connection, so they run in tiers 1-2 (no ``nats``
marker, no real server).
"""

from __future__ import annotations

import asyncio
import logging
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nats.errors import Error as NatsError

from biff.models import RelayAuth, RelayConnectError
from biff.nats_relay import NatsRelay


def _fake_nc() -> MagicMock:
    nc = MagicMock()
    nc.is_closed = False
    nc.close = AsyncMock()
    return nc


class TestConnectFailureRaisesRelayConnectError:
    @pytest.mark.anyio()
    async def test_rejected_handshake_closes_transport(self) -> None:
        closed = asyncio.Event()
        peers: list[asyncio.StreamWriter] = []

        async def reject(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            peers.append(writer)
            writer.write(b'INFO {"server_id":"test","auth_required":true}\r\n')
            await writer.drain()
            await reader.readline()
            writer.write(b"-ERR 'Authorization Violation'\r\n")
            await writer.drain()
            await reader.read()
            closed.set()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(reject, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            relay = NatsRelay(url=f"nats://127.0.0.1:{port}", repo_name="test")
            try:
                with pytest.raises(RelayConnectError):
                    await relay._dial()
                await asyncio.wait_for(closed.wait(), timeout=0.5)
            finally:
                for peer in peers:
                    peer.close()
                    await peer.wait_closed()

    @pytest.mark.anyio()
    async def test_initial_auth_rejection_is_terminal_and_redacted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = NatsRelay(url="nats://localhost:4222", repo_name="test")
        rejected = AsyncMock(side_effect=NatsError("Authorization Violation private"))
        with (
            patch("nats.aio.client.Client._select_next_server", AsyncMock()),
            patch("nats.aio.client.Client._process_connect_init", rejected),
            pytest.raises(RelayConnectError),
        ):
            await asyncio.wait_for(relay._ensure_connected(), timeout=0.5)
        assert rejected.await_count == 1
        assert "private" not in caplog.text

    @pytest.mark.anyio()
    async def test_initial_unreachable_dial_does_not_retry_forever(self) -> None:
        relay = NatsRelay(url="nats://localhost:4222", repo_name="test")
        refused = AsyncMock(side_effect=ConnectionRefusedError("private-token"))
        with (
            patch("nats.aio.transport.TcpTransport.connect", refused),
            pytest.raises(RelayConnectError),
        ):
            await asyncio.wait_for(relay._ensure_connected(), timeout=0.5)

    @pytest.mark.anyio()
    async def test_refused_retries_log_only_concise_exception_types(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = NatsRelay(url="nats://localhost:4222", repo_name="test")
        refused = AsyncMock(
            side_effect=ConnectionRefusedError("private-login private-session")
        )
        caplog.set_level(logging.DEBUG, logger="biff.nats_relay")
        with (
            patch("nats.aio.transport.TcpTransport.connect", refused),
            patch("biff.nats_relay._RECONNECT_TIME_WAIT", 0),
            pytest.raises(RelayConnectError),
        ):
            await relay._ensure_connected()

        assert refused.await_count == 2
        retries = [r for r in caplog.records if "NATS error" in r.getMessage()]
        assert len(retries) == refused.await_count
        assert all(
            r.getMessage()
            == "NATS error (transient, auto-recovering): ConnectionRefusedError"
            for r in retries
        )
        assert [r.levelno for r in retries] == [
            logging.INFO,
            logging.DEBUG,
        ]
        assert [
            r.getMessage() for r in caplog.records if "outcome=failed" in r.message
        ] == ["NATS connection outcome=failed failures=2 error=NoServersError"]
        assert all(r.exc_info is None for r in caplog.records)
        assert "private" not in caplog.text
        assert "Traceback" not in caplog.text

    @pytest.mark.anyio()
    async def test_connect_failure_raises_redacted_error(self) -> None:
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with (
            patch("nats.aio.client.Client.connect", connect),
            pytest.raises(RelayConnectError, match=re.escape("relay.example.com:4222")),
        ):
            await relay._ensure_connected()

    @pytest.mark.anyio()
    async def test_redacted_error_message_never_contains_the_token(self) -> None:
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with patch("nats.aio.client.Client.connect", connect):
            try:
                await relay._ensure_connected()
            except RelayConnectError as exc:
                assert "s3cret-token" not in str(exc)
            else:
                pytest.fail("expected RelayConnectError")

    @pytest.mark.anyio()
    async def test_redacted_error_message_never_contains_userinfo(self) -> None:
        """A credential-bearing URL must not appear verbatim in the message.

        BIFF_RELAY_URL can in principle carry ``user:pass@`` even though
        docs/relay-env-overrides.md Sec 1 tells operators never to embed it
        there -- the error message must not trust that operators comply.
        """
        relay = NatsRelay(
            url="tls://opuser:opsecret@relay.example.com:4222",
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with patch("nats.aio.client.Client.connect", connect):
            try:
                await relay._ensure_connected()
            except RelayConnectError as exc:
                assert "opsecret" not in str(exc)
                assert "relay.example.com:4222" in str(exc)
            else:
                pytest.fail("expected RelayConnectError")

    @pytest.mark.anyio()
    async def test_raw_original_exception_type_is_not_propagated(self) -> None:
        """Callers see RelayConnectError, never the raw TimeoutError."""
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with patch("nats.aio.client.Client.connect", connect):
            with pytest.raises(RelayConnectError) as exc_info:
                await relay._ensure_connected()
            assert type(exc_info.value) is RelayConnectError

    @pytest.mark.anyio()
    async def test_relay_connect_error_is_a_connection_error(self) -> None:
        """Subclassing ConnectionError keeps existing OSError-catching callers intact.

        talk/REPL paths along ``biff/__main__.py`` catch
        ``(NatsError, TimeoutError, OSError)`` around calls that indirectly
        redial through ``_ensure_connected()``. RelayConnectError must stay
        catchable by that tuple, or a best-effort loop would crash instead
        of absorbing the failure.
        """
        relay = NatsRelay(url="tls://relay.example.com:4222", repo_name="test")
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with patch("nats.aio.client.Client.connect", connect):
            try:
                await relay._ensure_connected()
            except (TimeoutError, OSError) as exc:
                assert isinstance(exc, RelayConnectError)
            else:
                pytest.fail("expected RelayConnectError via the OSError branch")

    @pytest.mark.anyio()
    async def test_connect_failure_error_has_no_chained_context(self) -> None:
        """RelayConnectError's __context__ is genuinely None, not merely hidden.

        This is structural isolation, not display suppression: the redacted
        error is raised only after the try/except/finally handling the raw
        nats.connect() failure has fully completed, so there is no
        "currently handled exception" left for Python to implicitly chain
        onto. Raising from inside the except clause, even with ``from None``,
        would leave __context__ populated and
        walkable by anything that ignores __suppress_context__ (a
        debugger, an APM integration, traceback.format_exception(chain=True)
        called explicitly), exposing the original traceback frame.
        """
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with (
            patch("nats.aio.client.Client.connect", connect),
            pytest.raises(RelayConnectError) as exc_info,
        ):
            await relay._ensure_connected()
        assert exc_info.value.__context__ is None


class TestAuthKwargsClearedOnConnectFailure:
    """The finally block empties auth_kwargs so no frame retains the secret."""

    @pytest.mark.anyio()
    async def test_auth_kwargs_cleared_after_failure(self) -> None:
        """Asserts on the actual dict _auth_kwargs() returns, not a mock's copy.

        ``connect.await_args.kwargs`` is a *separate* dict built by the
        mock's own ``**`` expansion at call time -- clearing our
        ``auth_kwargs`` local cannot and does not touch it. Holding a
        reference to the real dict object and asserting *that* object is
        empty is the only assertion that actually proves the ``finally``
        block ran.
        """
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        held_auth_kwargs = {"token": "s3cret-token"}
        relay._auth_kwargs = MagicMock(  # type: ignore[method-assign]
            return_value=held_auth_kwargs
        )
        connect = AsyncMock(side_effect=TimeoutError("nats: timeout"))
        with (
            patch("nats.aio.client.Client.connect", connect),
            pytest.raises(RelayConnectError),
        ):
            await relay._ensure_connected()

        assert connect.await_args is not None
        assert connect.await_args.kwargs["token"] == "s3cret-token"
        assert held_auth_kwargs == {}

    @pytest.mark.anyio()
    async def test_auth_kwargs_cleared_even_on_success(self) -> None:
        """Clear the original authentication kwargs even when connecting succeeds.

        Inspect the original dict rather than the mock's captured keyword copy,
        which cannot prove that the caller cleared its own credentials.
        """
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            auth=RelayAuth(token="s3cret-token"),
            repo_name="test",
        )
        relay._provision = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        held_auth_kwargs = {"token": "s3cret-token"}
        relay._auth_kwargs = MagicMock(  # type: ignore[method-assign]
            return_value=held_auth_kwargs
        )
        nc = _fake_nc()
        connect = nc.connect = AsyncMock()
        with patch("biff.nats_relay.NatsClient", return_value=nc):
            await relay._ensure_connected()

        assert connect.await_args is not None
        assert connect.await_args.kwargs["token"] == "s3cret-token"
        assert held_auth_kwargs == {}
