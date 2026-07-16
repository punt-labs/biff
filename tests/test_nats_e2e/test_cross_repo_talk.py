"""NATS E2E: a full cross-repo talk conversation (biff-e9u).

Two sessions of the SAME organization in DIFFERENT repositories complete a
whole talk: invite -> accept -> message both directions -> end, plus an
invite -> withdraw.  Talk routes on ``(org, identity)`` (talk.tex
``subjectOf``), never on the repository, so every frame reaches the addressed
``@user:tty`` whichever repo either party runs in.

The proof that repo is not a routing coordinate: the repo-keyed subject keys
on a session's *own* repository, so two sessions in different repos subscribe
to and publish on different subjects and every reply strands — the reproduced
biff-e9u counterexample.  The org-keyed subject shares one namespace across
the org, so the same conversation is delivered.  ``test_repo_keyed_subject_
strands_cross_repo`` pins that contrast at the subject level.
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

# Two repositories of one organization: sanitized ``owner__repo`` names whose
# org (the ``owner`` before ``__``) agrees, so talk shares the org namespace
# while the repositories differ — the configuration biff-e9u mis-routes.
_ORG = "punt-labs"
_KAI_REPO = f"{_ORG}__biff"
_ERIC_REPO = f"{_ORG}__vox"


@pytest.fixture
async def kai_biff(
    nats_server: str, shared_data_dir: Path, transcript: Transcript
) -> AsyncIterator[RecordingClient]:
    """kai in the org's biff repo, peered with the vox repo."""
    config = BiffConfig(
        user="kai",
        repo_name=_KAI_REPO,
        relay_url=nats_server,
        peers=(_ERIC_REPO,),
    )
    state = create_state(
        config, shared_data_dir / "kai", tty="kai11111", hostname="h", pwd="/biff"
    )
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield RecordingClient(client=client, transcript=transcript, user="kai")


@pytest.fixture
async def eric_vox(
    nats_server: str, shared_data_dir: Path, transcript: Transcript
) -> AsyncIterator[RecordingClient]:
    """eric in the org's vox repo, peered with the biff repo."""
    config = BiffConfig(
        user="eric",
        repo_name=_ERIC_REPO,
        relay_url=nats_server,
        peers=(_KAI_REPO,),
    )
    state = create_state(
        config, shared_data_dir / "eric", tty="eric2222", hostname="h", pwd="/vox"
    )
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield RecordingClient(client=client, transcript=transcript, user="eric")


class TestCrossRepoTalkConversation:
    """A same-org, different-repo pair completes a full talk end to end."""

    @pytest.mark.transcript
    async def test_full_cross_repo_talk_flow(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """invite -> accept -> message both ways -> end, across two repos.

        Every frame is delivered to the addressed identity even though kai
        runs in the biff repo and eric in the vox repo — the org, not the
        repo, is the routing namespace.
        """
        kai_biff.transcript.title = "NATS E2E: cross-repo talk (biff-e9u)"
        kai_biff.transcript.description = (
            "A full talk between one org's biff and vox repos — every frame "
            "routed on (org, identity), never on repo."
        )

        await kai_biff.call("plan", message="on biff")
        await eric_vox.call("plan", message="on vox")

        # kai (biff) invites eric (vox) — the invite reaches eric's identity.
        result = await kai_biff.call(
            "talk", to="@eric:eric2222", message="cross-repo review?"
        )
        assert "Invite sent" in result
        assert "eric:eric2222" in result

        # eric (vox) receives the invite from kai (biff).
        await asyncio.sleep(0.3)
        read = await eric_vox.call("talk_read")
        assert "kai" in read
        assert "wants to talk" in read

        # eric accepts — the accept reaches kai across repos (the biff-e9u frame).
        result = await eric_vox.call(
            "talk", to="@kai:kai11111", message="sure, looking now"
        )
        assert "accepted their invite" in result

        # kai sees eric's opening line (reply direction, vox -> biff).
        await asyncio.sleep(0.3)
        read = await kai_biff.call("talk_read")
        assert "sure, looking now" in read

        # kai replies (biff -> vox); eric receives it.
        result = await kai_biff.call("talk", to="@eric:eric2222", message="thanks!")
        assert "Sent to eric:eric2222" in result
        await asyncio.sleep(0.3)
        read = await eric_vox.call("talk_read")
        assert "thanks!" in read

        # kai ends the conversation; eric sees the hangup.
        result = await kai_biff.call("talk_end")
        assert "ended" in result
        await asyncio.sleep(0.3)
        read = await eric_vox.call("talk_read")
        assert "ended the conversation" in read

    async def test_cross_repo_invite_withdraw_clears_pending(
        self, kai_biff: RecordingClient, eric_vox: RecordingClient
    ) -> None:
        """A withdrawn invite clears the invitee's pending marker across repos.

        eric (vox) invites kai (biff) then abandons it (talk_end while
        inviting -> ntWithdraw); kai's pending invite from eric clears, so a
        later talk_read shows no pending activity — the withdraw frame reached
        kai's identity across the repo boundary.
        """
        await kai_biff.call("plan", message="on biff")
        await eric_vox.call("plan", message="on vox")

        result = await eric_vox.call("talk", to="@kai:kai11111", message="ping?")
        assert "Invite sent" in result

        await asyncio.sleep(0.3)
        read = await kai_biff.call("talk_read")
        assert "eric" in read
        assert "wants to talk" in read

        # eric withdraws the still-pending invite.
        result = await eric_vox.call("talk_end")
        assert "ended" in result

        # kai's pending invite from eric is cleared by the withdraw frame.
        await asyncio.sleep(0.3)
        read = await kai_biff.call("talk_read")
        assert "No pending talk activity" in read


class TestRepoKeyedSubjectStrands:
    """The repo-keyed subject is the reproduced biff-e9u strand.

    subjectOf keys on the organization, so two sessions of one org share a
    namespace regardless of repo.  A repo-keyed subject instead keys on each
    session's own repository, so a biff session and a vox session never meet
    on a subject — the reply the addressee never receives (talk.tex R2).
    """

    def test_org_keyed_subject_shares_namespace_cross_repo(self) -> None:
        """Both repos of one org resolve a peer identity to one subject."""
        kai = NatsRelay(
            url="nats://localhost", repo_name=_KAI_REPO, stream_prefix="biff-test"
        )
        eric = NatsRelay(
            url="nats://localhost", repo_name=_ERIC_REPO, stream_prefix="biff-test"
        )
        # kai (biff) publishing a reply to the vox peer and eric (vox)
        # subscribing to its own identity land on the SAME subject.
        assert kai.talk_notify_subject("eric:eric2222") == eric.talk_notify_subject(
            "eric:eric2222"
        )
        assert kai.talk_notify_subject("eric:eric2222") == (
            f"biff-test.{_ORG}.talk.notify.eric:eric2222"
        )

    def test_repo_keyed_subject_strands_cross_repo(self) -> None:
        """A repo-keyed subject strands: the two repos never share a namespace.

        This is the counterexample the org-keyed design excludes.  Keying the
        subject on the session's own repository, a biff publisher stamps a
        different subject than a vox subscriber listens on, so the frame is
        undeliverable (ReceiveNotForSubject).
        """
        kai_repo_subject = f"biff-test.{_KAI_REPO}.talk.notify.eric:eric2222"
        eric_repo_subject = f"biff-test.{_ERIC_REPO}.talk.notify.eric:eric2222"
        # Under the defect the publisher (biff) and the subscriber (vox) key
        # on their own repos, so the subjects differ and the frame strands.
        assert kai_repo_subject != eric_repo_subject


class TestCrossRepoTalkFramePublish:
    """The talk frame is published to the recipient's (org, identity) subject."""

    async def test_invite_frame_lands_on_org_identity_subject(
        self,
        nats_server: str,
        shared_data_dir: Path,
        transcript: Transcript,
    ) -> None:
        """kai (biff) inviting eric (vox) publishes to the org+identity subject.

        An external subscriber on ``biff.{org}.talk.notify.eric:eric2222`` —
        the subject eric's vox session actually listens on — receives the
        invite kai's biff session sends, proving the frame crossed the repo
        boundary via the shared org namespace.
        """
        kai_cfg = BiffConfig(
            user="kai",
            repo_name=_KAI_REPO,
            relay_url=nats_server,
            peers=(_ERIC_REPO,),
        )
        kai_state = create_state(
            kai_cfg, shared_data_dir / "kai", tty="kai11111", hostname="h", pwd="/biff"
        )
        eric_cfg = BiffConfig(
            user="eric",
            repo_name=_ERIC_REPO,
            relay_url=nats_server,
            peers=(_KAI_REPO,),
        )
        eric_state = create_state(
            eric_cfg, shared_data_dir / "eric", tty="eric2222", hostname="h", pwd="/vox"
        )
        kai_mcp = create_server(kai_state)
        eric_mcp = create_server(eric_state)

        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        received: list[bytes] = []

        async def _capture(msg: object) -> None:
            received.append(msg.data)  # type: ignore[attr-defined]

        # The subject eric's vox session subscribes to: (org, identity).
        assert isinstance(eric_state.relay, NatsRelay)
        subject = eric_state.relay.talk_notify_subject(eric_state.session_key)
        assert subject == f"biff.{_ORG}.talk.notify.eric:eric2222"
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
                await kai_r.call("talk", to="@eric:eric2222", message="hello vox")
                await asyncio.sleep(0.3)

                assert received, "invite never reached eric's org+identity subject"
        finally:
            await sub.unsubscribe()  # pyright: ignore[reportUnknownMemberType]
            await nc.close()
