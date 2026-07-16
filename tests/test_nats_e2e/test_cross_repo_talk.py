"""NATS E2E: talk routes on identity alone, gated only by visibility (biff-e9u).

Talk's NATS subject is the globally-unique ``user:tty`` identity and nothing
else (talk.tex ``subjectOf~k = k``): neither repository nor organization is a
routing coordinate.  A frame reaches the addressed ``@user:tty`` whichever repo
or org either party runs in.  The one gate is visibility — you can only talk to
a session you can see (it must be in ``visible_repos``), enforced at resolution,
never on the subject.

Three properties are proven end to end:

* Same org, different repos complete a full talk (the biff-e9u regression).
* Different orgs, mutually peered so both are visible, complete a full talk —
  org was over-scoping, just like repo.
* A peer you cannot see cannot be talked to: resolution raises and no frame is
  ever published (no silent strand).

The subject-level counterexamples show both the repo-keyed and the org-keyed
subject would strand the cross-org pair, and both would have stranded the
same-org/different-repo pair under repo keying — only the identity subject
delivers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import nats as nats_lib
import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.state import create_state
from biff.testing import RecordingClient, Transcript

pytestmark = pytest.mark.nats

# Same organization, two repositories (sanitized ``owner__repo``): the pair
# biff-e9u mis-routed under repo keying.
_KAI_REPO = "punt-labs__biff"
_ERIC_REPO = "punt-labs__vox"

# Two DIFFERENT organizations, mutually peered so each is in the other's
# ``visible_repos``: the pair org-keyed routing would still mis-route.
_ALPHA_REPO = "org-alpha__biff"
_BETA_REPO = "org-beta__vox"


async def _serve(
    nats_server: str,
    data_dir: Path,
    transcript: Transcript,
    *,
    user: str,
    repo: str,
    tty: str,
    peers: tuple[str, ...],
) -> AsyncIterator[RecordingClient]:
    """Yield a RecordingClient for *user* in *repo* peered with *peers*."""
    config = BiffConfig(user=user, repo_name=repo, relay_url=nats_server, peers=peers)
    state = create_state(config, data_dir, tty=tty, hostname="h", pwd="/w")
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield RecordingClient(client=client, transcript=transcript, user=user)


@pytest.fixture
async def kai_biff(
    nats_server: str, shared_data_dir: Path, transcript: Transcript
) -> AsyncIterator[RecordingClient]:
    """kai in the org's biff repo, peered with the same org's vox repo."""
    async for client in _serve(
        nats_server,
        shared_data_dir / "kai",
        transcript,
        user="kai",
        repo=_KAI_REPO,
        tty="kai11111",
        peers=(_ERIC_REPO,),
    ):
        yield client


@pytest.fixture
async def eric_vox(
    nats_server: str, shared_data_dir: Path, transcript: Transcript
) -> AsyncIterator[RecordingClient]:
    """eric in the org's vox repo, peered with the same org's biff repo."""
    async for client in _serve(
        nats_server,
        shared_data_dir / "eric",
        transcript,
        user="eric",
        repo=_ERIC_REPO,
        tty="eric2222",
        peers=(_KAI_REPO,),
    ):
        yield client


@pytest.fixture
async def kai_alpha(
    nats_server: str, shared_data_dir: Path, transcript: Transcript
) -> AsyncIterator[RecordingClient]:
    """kai in org-alpha's biff repo, peered with org-beta's vox repo."""
    async for client in _serve(
        nats_server,
        shared_data_dir / "kai-alpha",
        transcript,
        user="kai",
        repo=_ALPHA_REPO,
        tty="kaialpha",
        peers=(_BETA_REPO,),
    ):
        yield client


@pytest.fixture
async def eric_beta(
    nats_server: str, shared_data_dir: Path, transcript: Transcript
) -> AsyncIterator[RecordingClient]:
    """eric in org-beta's vox repo, peered with org-alpha's biff repo."""
    async for client in _serve(
        nats_server,
        shared_data_dir / "eric-beta",
        transcript,
        user="eric",
        repo=_BETA_REPO,
        tty="ericbeta",
        peers=(_ALPHA_REPO,),
    ):
        yield client


async def _drive_full_talk(
    inviter: RecordingClient,
    invitee: RecordingClient,
    *,
    invitee_addr: str,
    inviter_addr: str,
) -> None:
    """Drive invite -> accept -> message both ways -> end and assert delivery.

    Every assertion is a frame reaching the addressed identity: the invite,
    the accept's opening line (reply direction), the inviter's reply, and the
    hangup all cross whatever repo/org boundary separates the two sessions.
    """
    await inviter.call("plan", message="ready")
    await invitee.call("plan", message="ready")

    result = await inviter.call("talk", to=invitee_addr, message="cross review?")
    assert "Invite sent" in result
    assert invitee_addr.lstrip("@") in result

    await asyncio.sleep(0.3)
    read = await invitee.call("talk_read")
    assert inviter.user in read
    assert "wants to talk" in read

    result = await invitee.call("talk", to=inviter_addr, message="sure, looking now")
    assert "accepted their invite" in result

    await asyncio.sleep(0.3)
    read = await inviter.call("talk_read")
    assert "sure, looking now" in read

    result = await inviter.call("talk", to=invitee_addr, message="thanks!")
    assert "Sent to" in result
    await asyncio.sleep(0.3)
    read = await invitee.call("talk_read")
    assert "thanks!" in read

    result = await inviter.call("talk_end")
    assert "ended" in result
    await asyncio.sleep(0.3)
    read = await invitee.call("talk_read")
    assert "ended the conversation" in read


class TestSameOrgDifferentRepoTalk:
    """Same org, different repos — the biff-e9u regression guard."""

    @pytest.mark.transcript
    async def test_full_talk_flow(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """kai (biff) and eric (vox) of one org complete a whole talk."""
        kai_biff.transcript.title = "NATS E2E: same-org cross-repo talk (biff-e9u)"
        kai_biff.transcript.description = (
            "One org's biff and vox repos complete a full talk — every frame "
            "routed on identity alone."
        )
        await _drive_full_talk(
            kai_biff,
            eric_vox,
            invitee_addr="@eric:eric2222",
            inviter_addr="@kai:kai11111",
        )

    async def test_invite_withdraw_clears_pending(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """A withdrawn invite clears the invitee's pending marker across repos."""
        await kai_biff.call("plan", message="on biff")
        await eric_vox.call("plan", message="on vox")

        result = await eric_vox.call("talk", to="@kai:kai11111", message="ping?")
        assert "Invite sent" in result

        await asyncio.sleep(0.3)
        read = await kai_biff.call("talk_read")
        assert "eric" in read
        assert "wants to talk" in read

        result = await eric_vox.call("talk_end")
        assert "ended" in result

        await asyncio.sleep(0.3)
        read = await kai_biff.call("talk_read")
        assert "No pending talk activity" in read


class TestCrossOrgVisibleTalk:
    """Different orgs, mutually peered so both are visible — a full talk.

    This is the case org-keyed routing would still strand: the two sessions
    are in different organizations, so an org-scoped subject would put them in
    different namespaces.  Identity routing ignores the org entirely, and the
    visibility peering is the only thing that lets them address each other.
    """

    @pytest.mark.transcript
    async def test_full_talk_flow(
        self, kai_alpha: RecordingClient, eric_beta: RecordingClient
    ) -> None:
        """kai (org-alpha) and eric (org-beta), mutually visible, complete a talk."""
        kai_alpha.transcript.title = "NATS E2E: cross-org visible talk (biff-e9u)"
        kai_alpha.transcript.description = (
            "Two mutually-peered orgs complete a full talk — identity is the "
            "route, visibility is the only gate."
        )
        await _drive_full_talk(
            kai_alpha,
            eric_beta,
            invitee_addr="@eric:ericbeta",
            inviter_addr="@kai:kaialpha",
        )


class TestVisibilityGate:
    """You cannot talk to a session you cannot see — the only gate."""

    async def test_invisible_peer_raises_and_sends_nothing(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """An unseen target raises 'not online' and publishes no frame.

        kai peers with nobody, so eric's repo is not in kai's ``visible_repos``.
        The talk resolves against visible sessions only, raises a clear error,
        and never reaches the publish step — an external subscriber on eric's
        identity subject sees nothing.
        """
        kai_cfg = BiffConfig(user="kai", repo_name=_KAI_REPO, relay_url=nats_server)
        kai_state = create_state(
            kai_cfg, shared_data_dir / "kai", tty="kai11111", hostname="h", pwd="/w"
        )
        eric_cfg = BiffConfig(user="eric", repo_name=_ERIC_REPO, relay_url=nats_server)
        eric_state = create_state(
            eric_cfg, shared_data_dir / "eric", tty="eric2222", hostname="h", pwd="/w"
        )
        kai_mcp = create_server(kai_state)
        eric_mcp = create_server(eric_state)

        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        received: list[bytes] = []

        async def _capture(msg: object) -> None:
            received.append(msg.data)  # type: ignore[attr-defined]

        assert isinstance(eric_state.relay, NatsRelay)
        subject = eric_state.relay.talk_notify_subject(eric_state.session_key)
        sub = await nc.subscribe(subject, cb=_capture)  # pyright: ignore[reportUnknownMemberType]

        try:
            async with (
                Client(FastMCPTransport(kai_mcp)) as kai_raw,
                Client(FastMCPTransport(eric_mcp)) as eric_raw,
            ):
                kai_r = RecordingClient(
                    client=kai_raw, transcript=transcript, user="kai"
                )
                eric_r = RecordingClient(
                    client=eric_raw, transcript=transcript, user="eric"
                )
                await eric_r.call("plan", message="on vox")
                result = await kai_r.call("talk", to="@eric:eric2222", message="hi")
                assert "not online" in result

                await asyncio.sleep(0.3)
                assert not received, "a frame was published to an invisible peer"
        finally:
            await sub.unsubscribe()  # pyright: ignore[reportUnknownMemberType]
            await nc.close()


class TestIdentitySubjectCounterexamples:
    """Repo-keyed and org-keyed subjects both strand; the identity subject does not.

    subjectOf keys on the identity alone.  Keying on the repository OR the
    organization puts a publisher and a cross-repo/cross-org subscriber on
    different subjects, so the frame is undeliverable (ReceiveNotForSubject) —
    the strand identity routing excludes.
    """

    def test_identity_subject_ignores_repo_and_org(self) -> None:
        """Different repos AND different orgs resolve to one identity subject."""
        alpha = NatsRelay(
            url="nats://localhost", repo_name=_ALPHA_REPO, stream_prefix="biff-test"
        )
        beta = NatsRelay(
            url="nats://localhost", repo_name=_BETA_REPO, stream_prefix="biff-test"
        )
        assert alpha.talk_notify_subject("eric:ericbeta") == beta.talk_notify_subject(
            "eric:ericbeta"
        )
        assert alpha.talk_notify_subject("eric:ericbeta") == (
            "biff-test.talk.notify.eric:ericbeta"
        )

    def test_repo_keyed_subject_strands_cross_repo(self) -> None:
        """A repo-keyed subject: same-org peers in different repos never meet."""
        kai_repo_subject = f"biff-test.{_KAI_REPO}.talk.notify.eric:eric2222"
        eric_repo_subject = f"biff-test.{_ERIC_REPO}.talk.notify.eric:eric2222"
        assert kai_repo_subject != eric_repo_subject

    def test_org_keyed_subject_strands_cross_org(self) -> None:
        """An org-keyed subject: cross-org peers land in different namespaces."""
        # org = the ``owner`` before ``__`` in a sanitized repo name.
        alpha_org_subject = "biff-test.org-alpha.talk.notify.eric:ericbeta"
        beta_org_subject = "biff-test.org-beta.talk.notify.eric:ericbeta"
        assert alpha_org_subject != beta_org_subject
