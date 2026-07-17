"""Hosted NATS: talk routes on identity alone, verified on the real relay (biff-e9u).

The ``-m hosted`` suite exercises presence and messaging against a real hosted
NATS server; this module extends it to *talk*, the one subsystem whose routing
changed under biff-e9u and whose delivery-soundness invariant (talk.tex
invariant 12, R1/R3) had never been checked against real infrastructure.

Talk's NATS subject is the globally-unique ``user:tty`` identity and nothing
else (talk.tex ``subjectOf~k = k``): neither repository nor organization is a
routing coordinate.  These tests drive the relay directly rather than standing
up two FastMCP servers in one event loop, which deadlocks the session-scoped
asyncio loop the hosted suite runs on.

The delivery tests bind the two endpoints to relays configured for *genuinely
different* repositories and organizations, and — mirroring production
``TalkState._publish`` — publish each frame on the subject the SENDER's own
relay computes for the peer identity.  A relay that keyed that subject on its
own repository or organization would compute a subject the peer never
subscribes to, so the frame would be lost.  Because the subject is identity-only
the frame instead reaches the addressed ``@user:tty`` across every boundary,
and reverting the routing to repo- or org-keying makes these tests go red.

Four properties are proven end to end on the real relay:

* Same org, different repos complete a full talk (the biff-e9u regression).
* Different orgs, mutually visible, complete a full talk — org over-scoped
  exactly as repo did.
* A withdraw frame (ntWithdraw) routes on identity too, cross-org.
* A frame correctly addressed to us but published on a *foreign* subject never
  reaches us (ReceiveNotForSubject), while a correctly-subjected one does — the
  receiving half of the routing argument.

Every session identity carries a per-run salt so concurrent runs against the
shared hosted account cannot collide on the identity-only subject and
cross-deliver.

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
from uuid import uuid4

import pytest
import pytest_asyncio

from biff.config import DEMO_RELAY_URL
from biff.nats_relay import NatsRelay
from biff.talk_types import TalkNotification

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nats.aio.msg import Msg
    from nats.aio.subscription import Subscription

    from biff.models import RelayAuth

pytestmark = [pytest.mark.hosted, pytest.mark.asyncio(loop_scope="session")]

_STREAM_PREFIX = "biff-dev"

# One salt per run.  Subjects are identity-only on core NATS, so two runs
# against the shared hosted account that reused a fixed identity would subscribe
# to the same subject and cross-deliver each other's frames.  The salt makes
# every identity — and therefore every subject — unique per run, while staying
# deterministic within a run (computed once at import).
_SALT = uuid4().hex[:8]

# Same organization (org-alpha), two repositories — the pair biff-e9u mis-routed
# under repo keying.  The org/repo lives only in each relay's configuration; it
# never enters the identity-only subject.
_KAI_BIFF = f"htkai{_SALT}:htkaibiff{_SALT}"
_ERIC_VOX = f"hteric{_SALT}:htericvox{_SALT}"

# Two DIFFERENT organizations, mutually visible — the pair org-keyed routing
# would still strand.
_KAI_ALPHA = f"htkai{_SALT}:htkaialpha{_SALT}"
_ERIC_BETA = f"hteric{_SALT}:htericbeta{_SALT}"

# An identity nobody in these tests subscribes to — the foreign subject.
_FOREIGN = f"nobody{_SALT}:htelsewhere{_SALT}"

# Relay repository names, in ``org__repo`` form.  The org is the segment before
# ``__``; two repos of org-alpha exercise the same-org/cross-repo case, and a
# repo of org-beta exercises the cross-org case.
_ORG_ALPHA_BIFF = "org-alpha__biff"
_ORG_ALPHA_VOX = "org-alpha__vox"
_ORG_BETA_VOX = "org-beta__vox"


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def alpha_biff_relay(
    hosted_nats_url: str, hosted_nats_auth: RelayAuth | None
) -> AsyncIterator[NatsRelay]:
    """Relay for org-alpha's biff repo — kai's side in every talk flow."""
    relay = NatsRelay(
        url=hosted_nats_url,
        auth=hosted_nats_auth,
        name="biff-test-alpha-biff",
        repo_name=_ORG_ALPHA_BIFF,
        stream_prefix=_STREAM_PREFIX,
    )
    yield relay
    await relay.close()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def alpha_vox_relay(
    hosted_nats_url: str, hosted_nats_auth: RelayAuth | None
) -> AsyncIterator[NatsRelay]:
    """Relay for org-alpha's vox repo — eric's side in the same-org flow.

    A different repository of the *same* org as :func:`alpha_biff_relay`, so a
    talk between the two crosses a repository boundary but not an org boundary —
    the exact pair biff-e9u stranded under repo keying.
    """
    relay = NatsRelay(
        url=hosted_nats_url,
        auth=hosted_nats_auth,
        name="biff-test-alpha-vox",
        repo_name=_ORG_ALPHA_VOX,
        stream_prefix=_STREAM_PREFIX,
    )
    yield relay
    await relay.close()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def beta_vox_relay(
    hosted_nats_url: str, hosted_nats_auth: RelayAuth | None
) -> AsyncIterator[NatsRelay]:
    """Relay for org-beta's vox repo — eric's side in the cross-org flow.

    A different org from :func:`alpha_biff_relay`, so a talk between the two
    crosses an organization boundary that org-keyed routing would strand.
    """
    relay = NatsRelay(
        url=hosted_nats_url,
        auth=hosted_nats_auth,
        name="biff-test-beta-vox",
        repo_name=_ORG_BETA_VOX,
        stream_prefix=_STREAM_PREFIX,
    )
    yield relay
    await relay.close()


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
        """The subject this endpoint subscribes to, computed by its own relay."""
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
        """Publish one talk frame to *peer*, addressed exactly as production is.

        Mirrors ``TalkState._publish``: the subject is computed by the SENDER's
        own relay from the peer's identity — ``self._relay.talk_notify_subject``
        — never by the peer's relay.  A sender whose relay keyed the subject on
        its own repository or organization would therefore publish to a subject
        the peer never subscribes to, and the frame would be lost.  Because the
        subject is identity-only the frame reaches the peer across every repo and
        org boundary; that is exactly what the delivery tests prove.
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
                "to_key": peer.key,
            }
        ).encode()
        subject = self._relay.talk_notify_subject(peer.key)
        await nc.publish(subject, payload)

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
    repo/org boundary separates the two sessions, carried by a subject the
    sender's relay computes from the peer identity and nothing else.  There is no
    subject-format assertion here — the delivery itself is the proof, so reverting
    the routing to repo- or org-keying fails at the first ``await_frame`` rather
    than short-circuiting on a string comparison.
    """
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
            repo_name=_ORG_ALPHA_BIFF,
            stream_prefix=_STREAM_PREFIX,
        )
        beta = NatsRelay(
            url=DEMO_RELAY_URL,
            repo_name=_ORG_BETA_VOX,
            stream_prefix=_STREAM_PREFIX,
        )
        assert alpha.talk_notify_subject(_ERIC_BETA) == beta.talk_notify_subject(
            _ERIC_BETA
        )
        assert alpha.talk_notify_subject(_ERIC_BETA) == (
            f"{_STREAM_PREFIX}.talk.notify.{_ERIC_BETA}"
        )


class TestHostedSameOrgTalk:
    """Same org, different repos complete a full talk — the biff-e9u guard.

    kai runs in org-alpha's biff repo and eric in org-alpha's vox repo: a talk
    between them crosses a repository boundary within one org.  Because kai's
    relay addresses eric by identity, not by its own repo, the invite reaches
    eric — the regression repo-keyed routing caused.  Reverting
    ``talk_notify_subject`` to repo keying makes this go red.
    """

    @pytest.mark.transcript
    async def test_full_talk_flow(
        self, alpha_biff_relay: NatsRelay, alpha_vox_relay: NatsRelay
    ) -> None:
        """kai (org-alpha/biff) and eric (org-alpha/vox) exchange every frame."""
        kai = _Endpoint(alpha_biff_relay, _KAI_BIFF)
        eric = _Endpoint(alpha_vox_relay, _ERIC_VOX)
        await kai.subscribe()
        await eric.subscribe()
        try:
            await _drive_full_talk(kai, eric)
        finally:
            await kai.close()
            await eric.close()


class TestHostedCrossOrgTalk:
    """Different orgs, mutually visible, complete a full talk — org was over-scoped.

    org-keyed routing would strand this pair: kai in org-alpha and eric in
    org-beta would land in different namespaces.  Identity routing ignores the
    org entirely, so the sender's subject reaches the peer on the real relay.
    Reverting ``talk_notify_subject`` to either repo- or org-keying makes this
    go red.
    """

    @pytest.mark.transcript
    async def test_full_talk_flow(
        self, alpha_biff_relay: NatsRelay, beta_vox_relay: NatsRelay
    ) -> None:
        """kai (org-alpha) and eric (org-beta), mutually visible, complete a talk."""
        kai = _Endpoint(alpha_biff_relay, _KAI_ALPHA)
        eric = _Endpoint(beta_vox_relay, _ERIC_BETA)
        await kai.subscribe()
        await eric.subscribe()
        try:
            await _drive_full_talk(kai, eric)
        finally:
            await kai.close()
            await eric.close()

    async def test_withdraw_routes_cross_org(
        self, alpha_biff_relay: NatsRelay, beta_vox_relay: NatsRelay
    ) -> None:
        """A withdraw frame (ntWithdraw) reaches the cross-org peer on identity.

        Withdrawal sits on the availability side of the consent boundary
        (talk.tex threat model): it must reach the invited session to cancel a
        pending invite.  Like every reply, it routes on the peer's identity —
        no org, no repo — so a cross-org withdraw is delivered on the real relay.
        """
        kai = _Endpoint(alpha_biff_relay, _KAI_ALPHA)
        eric = _Endpoint(beta_vox_relay, _ERIC_BETA)
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
        self, alpha_biff_relay: NatsRelay, beta_vox_relay: NatsRelay
    ) -> None:
        """A frame correctly addressed to us but on a foreign subject is lost.

        The receiving half of the routing argument, isolated: a frame may carry
        our *correct* ``to_key`` and still never arrive if its publisher
        addressed the wrong subject — exactly what a repo- or org-keyed reply to
        a cross-boundary peer does.  We assert the drop is by subject, not by
        address, by giving the foreign frame our own ``to_key`` and a
        distinguishing body: it is dropped because kai never subscribed to its
        subject, not because the address was wrong.  A correctly-subjected frame
        with a different body is delivered in the same test to prove the
        subscription is live, so the foreign frame's absence is a drop, not a
        dead subscriber.
        """
        kai = _Endpoint(alpha_biff_relay, _KAI_ALPHA)
        eric = _Endpoint(beta_vox_relay, _ERIC_BETA)
        await kai.subscribe()
        try:
            # A frame addressed to kai (correct to_key) but published on a
            # DIFFERENT identity's subject — right address, wrong subject.
            foreign_subject = beta_vox_relay.talk_notify_subject(_FOREIGN)
            assert foreign_subject != kai.subject
            nc = await beta_vox_relay.get_nc()
            await nc.publish(
                foreign_subject,
                json.dumps(
                    {
                        "type": "message",
                        "from_key": eric.key,
                        "to_key": kai.key,  # correct address, wrong subject
                        "body": "wrong subject",
                    }
                ).encode(),
            )

            # A correctly-subjected frame to kai, to prove the sub is live.
            await eric.send(kai, ntype="message", body="right subject")
            got = await kai.await_frame("message")
            assert got.nbody == "right subject"

            # The foreign-subject frame never reached kai's subscription — the
            # drop was by subject, since the address was correct.
            assert all(f.nbody != "wrong subject" for f in kai.received)
        finally:
            await kai.close()
