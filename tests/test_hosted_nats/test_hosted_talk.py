"""Hosted NATS: talk routes on identity alone, verified on the real relay (biff-e9u).

The ``-m hosted`` suite exercises presence and messaging against a real hosted
NATS server; this module extends it to *talk*, the one subsystem whose routing
changed under biff-e9u and whose delivery-soundness invariant (talk.tex
invariant 12, R1/R3) had never been checked against real infrastructure.

Talk's NATS subject is the globally-unique ``user:tty`` identity and nothing
else (talk.tex ``subjectOf~k = k``): neither repository nor organization is a
routing coordinate.  These tests drive the relay directly — publishing each
frame to ``subjectOf(peer)`` and asserting it reaches the addressed
``@user:tty`` — rather than standing up two FastMCP servers in one event loop,
which deadlocks the session-scoped asyncio loop the hosted suite runs on.

Four properties are proven end to end on the real relay:

* Same org, different repos complete a full talk (the biff-e9u regression).
* Different orgs, mutually visible, complete a full talk — org over-scoped
  exactly as repo did.
* A withdraw frame (ntWithdraw) routes on identity too, cross-org.
* A frame published to a *foreign* subject never reaches us (ReceiveNotForSubject),
  while one on our own subject does — the receiving half of the routing argument.

Run:
    BIFF_TEST_NATS_URL=tls://connect.ngs.global \\
        BIFF_TEST_NATS_CREDS=src/biff/data/demo.creds \\
        uv run pytest -m hosted -v
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from biff.config import DEMO_RELAY_URL
from biff.nats_relay import NatsRelay
from biff.talk_types import TalkNotification

if TYPE_CHECKING:
    from nats.aio.msg import Msg
    from nats.aio.subscription import Subscription

pytestmark = [pytest.mark.hosted, pytest.mark.asyncio(loop_scope="session")]

_STREAM_PREFIX = "biff-dev"

# Same organization (punt-labs), two repositories — the pair biff-e9u mis-routed
# under repo keying.  The org/repo lives only in these identities' provenance;
# it never enters the subject.
_KAI_BIFF = "htkai:htkaibiff"
_ERIC_VOX = "hteric:htericvox"

# Two DIFFERENT organizations, mutually visible — the pair org-keyed routing
# would still strand.
_KAI_ALPHA = "htkai:htkaialpha"
_ERIC_BETA = "hteric:htericbeta"


@dataclass(slots=True)
class _Endpoint:
    """One side of a wire-level talk exchange on the hosted relay.

    Binds a relay connection to a session identity, subscribes to that
    identity's own subject (``subjectOf~k = k``), and captures every frame the
    broker delivers there so a test can assert exactly what reached the
    addressed ``@user:tty``.
    """

    _relay: NatsRelay
    _key: str
    _received: list[TalkNotification] = field(default_factory=list[TalkNotification])
    _sub: Subscription | None = None

    @property
    def key(self) -> str:
        """This endpoint's ``user:tty`` identity."""
        return self._key

    @property
    def subject(self) -> str:
        """The identity subject this endpoint subscribes to and is addressed on."""
        return self._relay.talk_notify_subject(self._key)

    @property
    def received(self) -> tuple[TalkNotification, ...]:
        """Every frame the broker has delivered to this identity's subject."""
        return tuple(self._received)

    async def subscribe(self) -> None:
        """Begin capturing frames delivered to this identity's own subject.

        Flushes after subscribing so the SUB is registered on the server before
        any publish.  Talk rides core NATS (no stream, no retention): a frame
        published before its subscriber is known is dropped, so without the
        flush the whole exchange would race and the first frame could be lost.
        """
        nc = await self._relay.get_nc()

        async def _capture(msg: Msg) -> None:
            payload: dict[str, object] = json.loads(msg.data)
            self._received.append(TalkNotification.from_payload(payload))

        self._sub = await nc.subscribe(  # pyright: ignore[reportUnknownMemberType]
            self.subject, cb=_capture
        )
        await nc.flush()

    async def send(self, peer: _Endpoint, *, ntype: str, body: str = "") -> None:
        """Publish one talk frame to *peer* on ``subjectOf(peer)`` — identity only.

        Mirrors the production ``TalkState._publish`` payload so the wire test
        exercises the same frame shape the REPL and MCP front-ends emit.
        """
        nc = await self._relay.get_nc()
        user, _, tty = self._key.partition(":")
        payload = json.dumps(
            {
                "type": ntype,
                "from": user,
                "from_tty": tty,
                "body": body,
                "from_key": self._key,
                "to_key": peer._key,
            }
        ).encode()
        await nc.publish(peer.subject, payload)

    async def await_frame(
        self, ntype: str, *, timeout: float = 5.0
    ) -> TalkNotification:
        """Return the first captured frame of type *ntype*, or fail on timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for frame in self._received:
                if frame.ntype == ntype:
                    return frame
            await asyncio.sleep(0.05)
        got = [f.ntype for f in self._received]
        msg = f"no {ntype!r} frame reached {self._key} within {timeout}s; got {got}"
        raise AssertionError(msg)

    async def close(self) -> None:
        """Drop the subscription so it cannot capture a later test's frames."""
        if self._sub is not None:
            await self._sub.unsubscribe()  # pyright: ignore[reportUnknownMemberType]
            self._sub = None


async def _drive_full_talk(inviter: _Endpoint, invitee: _Endpoint) -> None:
    """Invite -> accept -> message both ways -> end, asserting delivery each step.

    Every assertion is a frame reaching the addressed identity: the invite, the
    accept (reply direction), a reply each way, and the hangup all cross whatever
    repo/org boundary separates the two sessions, carried by a subject that names
    neither.
    """
    # Both sides compute one subject per identity — no repo, no org in the route.
    assert invitee.subject == f"{_STREAM_PREFIX}.talk.notify.{invitee.key}"
    assert inviter.subject == f"{_STREAM_PREFIX}.talk.notify.{inviter.key}"

    await inviter.send(invitee, ntype="invite", body="cross review?")
    invite = await invitee.await_frame("invite")
    assert invite.nfrom_key == inviter.key
    assert invite.nbody == "cross review?"

    await invitee.send(inviter, ntype="accept", body="sure, looking now")
    accept = await inviter.await_frame("accept")
    assert accept.nfrom_key == invitee.key

    await inviter.send(invitee, ntype="message", body="thanks!")
    to_invitee = await invitee.await_frame("message")
    assert to_invitee.nbody == "thanks!"
    assert to_invitee.nfrom_key == inviter.key

    await invitee.send(inviter, ntype="message", body="you're welcome")
    to_inviter = await inviter.await_frame("message")
    assert to_inviter.nbody == "you're welcome"
    assert to_inviter.nfrom_key == invitee.key

    await inviter.send(invitee, ntype="end")
    end = await invitee.await_frame("end")
    assert end.nfrom_key == inviter.key


class TestHostedIdentitySubject:
    """subjectOf keys on the identity alone — the wire-level statement of R1."""

    async def test_subject_ignores_repo_and_org(self) -> None:
        """Relays that believe they are in different orgs/repos agree on the subject.

        ``talk_notify_subject`` is a pure function of the stream prefix and the
        peer identity; neither the relay's repository nor its organization
        enters.  Two relays configured for different organizations and
        repositories therefore compute one subject for a given ``@user:tty`` —
        the concrete form of ``subjectOf~k = k`` that makes cross-org delivery
        possible.  No connection is opened; only the pure routing function runs.
        """
        alpha = NatsRelay(
            url=DEMO_RELAY_URL,
            repo_name="org-alpha__biff",
            stream_prefix=_STREAM_PREFIX,
        )
        beta = NatsRelay(
            url=DEMO_RELAY_URL,
            repo_name="org-beta__vox",
            stream_prefix=_STREAM_PREFIX,
        )
        assert alpha.talk_notify_subject(_ERIC_BETA) == beta.talk_notify_subject(
            _ERIC_BETA
        )
        assert alpha.talk_notify_subject(_ERIC_BETA) == (
            f"{_STREAM_PREFIX}.talk.notify.{_ERIC_BETA}"
        )


class TestHostedSameOrgTalk:
    """Same org, different repos complete a full talk — the biff-e9u guard."""

    @pytest.mark.transcript
    async def test_full_talk_flow(
        self, kai_relay: NatsRelay, eric_relay: NatsRelay
    ) -> None:
        """kai (biff) and eric (vox) of one org exchange every frame on the relay."""
        kai = _Endpoint(kai_relay, _KAI_BIFF)
        eric = _Endpoint(eric_relay, _ERIC_VOX)
        await kai.subscribe()
        await eric.subscribe()
        try:
            await _drive_full_talk(kai, eric)
        finally:
            await kai.close()
            await eric.close()


class TestHostedCrossOrgTalk:
    """Different orgs, mutually visible, complete a full talk — org was over-scoped.

    org-keyed routing would strand this pair: two sessions in different
    organizations would land in different namespaces.  Identity routing ignores
    the org entirely, so the same subject reaches both on the real relay.
    """

    @pytest.mark.transcript
    async def test_full_talk_flow(
        self, kai_relay: NatsRelay, eric_relay: NatsRelay
    ) -> None:
        """kai (org-alpha) and eric (org-beta), mutually visible, complete a talk."""
        kai = _Endpoint(kai_relay, _KAI_ALPHA)
        eric = _Endpoint(eric_relay, _ERIC_BETA)
        await kai.subscribe()
        await eric.subscribe()
        try:
            # Both relays compute the identical subject for the peer identity.
            assert eric_relay.talk_notify_subject(_KAI_ALPHA) == kai.subject
            assert kai_relay.talk_notify_subject(_ERIC_BETA) == eric.subject
            await _drive_full_talk(kai, eric)
        finally:
            await kai.close()
            await eric.close()

    async def test_withdraw_routes_cross_org(
        self, kai_relay: NatsRelay, eric_relay: NatsRelay
    ) -> None:
        """A withdraw frame (ntWithdraw) reaches the cross-org peer on identity.

        Withdrawal sits on the availability side of the consent boundary
        (talk.tex threat model): it must reach the invited session to cancel a
        pending invite.  Like every reply, it routes on the peer's identity —
        no org, no repo — so a cross-org withdraw is delivered on the real relay.
        """
        kai = _Endpoint(kai_relay, _KAI_ALPHA)
        eric = _Endpoint(eric_relay, _ERIC_BETA)
        await kai.subscribe()
        await eric.subscribe()
        try:
            await kai.send(eric, ntype="invite", body="ping?")
            invite = await eric.await_frame("invite")
            assert invite.nfrom_key == kai.key

            await kai.send(eric, ntype="withdraw")
            withdraw = await eric.await_frame("withdraw")
            assert withdraw.nfrom_key == kai.key
        finally:
            await kai.close()
            await eric.close()


class TestHostedForeignSubjectDropped:
    """A frame on a foreign subject never reaches us — ReceiveNotForSubject."""

    async def test_foreign_subject_not_delivered(
        self, kai_relay: NatsRelay, eric_relay: NatsRelay
    ) -> None:
        """We subscribe to our own subject; a frame published elsewhere is lost.

        The receiving half of the routing argument: a frame may carry a correct
        ``to_key`` and still never arrive if its publisher addressed the wrong
        subject — exactly what a repo- or org-keyed reply to a cross-boundary
        peer does.  A correctly-subjected frame is delivered in the same test to
        prove the subscription is live, so the foreign frame's absence is a drop,
        not a dead subscriber.
        """
        kai = _Endpoint(kai_relay, _KAI_ALPHA)
        eric = _Endpoint(eric_relay, _ERIC_BETA)
        await kai.subscribe()
        try:
            # eric publishes a well-formed frame to a DIFFERENT identity's subject.
            foreign_key = "nobody:htelsewhere"
            foreign_subject = eric_relay.talk_notify_subject(foreign_key)
            assert foreign_subject != kai.subject
            nc = await eric_relay.get_nc()
            await nc.publish(
                foreign_subject,
                json.dumps({"type": "message", "to_key": foreign_key}).encode(),
            )

            # And a correctly-subjected frame to kai, to prove the sub is live.
            await eric.send(kai, ntype="message", body="on your subject")
            got = await kai.await_frame("message")
            assert got.nbody == "on your subject"

            # The foreign-subject frame never reached kai's subscription.
            assert all(f.nto != foreign_key for f in kai.received)
        finally:
            await kai.close()
