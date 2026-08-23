"""Unit tests for NatsRelay's TLS handshake mode.

Regression coverage for the production incident where ``biff who`` hung
indefinitely against a relay behind a TLS-terminating load balancer.
nats.py's default (opportunistic TLS: read a plaintext INFO line first,
upgrade only if it advertises ``tls_required``) never completes against a
proxy expecting a TLS ClientHello as the first bytes on the wire.

``tls_handshake_first`` is an explicit opt-in, not inferred from the URL
scheme -- a first attempt at this fix keyed off ``tls://`` alone, which
broke the *default* demo relay (``tls://connect.ngs.global``, a native-TLS
nats-server that genuinely needs the opportunistic flow biff-relay's
proxy setup doesn't). Both deployment shapes use ``tls://``; only an
explicit operator signal can tell them apart.

These tests mock the connection, so they run in tiers 1-2 (no ``nats``
marker, no real server).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from biff.nats_relay import NatsRelay


def _fake_nc() -> MagicMock:
    nc = MagicMock()
    nc.is_closed = False
    nc.close = AsyncMock()
    return nc


class TestTlsKwargs:
    """``_tls_kwargs`` reflects the explicit constructor flag, never the URL."""

    def test_flag_set_requests_handshake_first(self) -> None:
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            tls_handshake_first=True,
            repo_name="test",
        )
        assert relay._tls_kwargs() == {"tls_handshake_first": True}

    def test_flag_unset_omits_the_option(self) -> None:
        relay = NatsRelay(url="tls://relay.example.com:4222", repo_name="test")
        assert relay._tls_kwargs() == {}

    def test_flag_set_on_a_plain_nats_url_still_requests_it(self) -> None:
        # The flag is an explicit opt-in -- NatsRelay doesn't second-guess
        # it against the URL scheme. Misconfiguring nats:// + the flag is
        # the operator's error to surface via a failed connect, not
        # something to silently correct here.
        relay = NatsRelay(
            url="nats://localhost:4222", tls_handshake_first=True, repo_name="test"
        )
        assert relay._tls_kwargs() == {"tls_handshake_first": True}


class TestConnectPassesTlsHandshakeFirst:
    """``_open_connection`` must actually thread the option into nats.connect."""

    @pytest.mark.anyio()
    async def test_flag_set_dials_with_handshake_first(self) -> None:
        relay = NatsRelay(
            url="tls://relay.example.com:4222",
            tls_handshake_first=True,
            repo_name="test",
        )
        relay._provision = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        connect = AsyncMock(return_value=_fake_nc())
        with patch("biff.nats_relay.nats.connect", connect):
            await relay._ensure_connected()

        assert connect.await_args is not None
        assert connect.await_args.kwargs["tls_handshake_first"] is True

    @pytest.mark.anyio()
    async def test_default_omits_handshake_first(self) -> None:
        # Covers the demo relay's shape: tls://connect.ngs.global, native
        # TLS, no explicit flag -- must stay on nats.py's opportunistic
        # default or this exact scenario regresses (WRONG_VERSION_NUMBER).
        relay = NatsRelay(url="tls://connect.ngs.global", repo_name="test")
        relay._provision = AsyncMock(  # type: ignore[method-assign]
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        connect = AsyncMock(return_value=_fake_nc())
        with patch("biff.nats_relay.nats.connect", connect):
            await relay._ensure_connected()

        assert connect.await_args is not None
        assert "tls_handshake_first" not in connect.await_args.kwargs
