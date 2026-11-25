"""Channel events reach a live interactive Claude Code session.

Channels are stdio-only and interactive-only: the Agent SDK and raw stdio never
receive them, so this drives a real ``claude`` in tmux and reads its screen.  A
second biff user writes to it through the CLI; the event must show in the pane
and ``biff.log`` must record it.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator

import pytest

from tests.test_claude_channels.launches import LAUNCHES, select_launches
from tests.test_claude_channels.rig import SENDER, ChannelRig, channel_rig

pytestmark = pytest.mark.claude_channels


def _requested_launches() -> list[object]:
    """Return the launches to run, never raising: collection must not abort.

    A blank or unknown ``BIFF_CHANNEL_LAUNCHES`` becomes one case that fails when
    the tier runs, so the default suite, which deselects this tier, still collects.
    """
    try:
        names = select_launches(os.environ.get("BIFF_CHANNEL_LAUNCHES"))
    except ValueError as exc:
        return [pytest.param(str(exc), id="invalid-selection")]
    return list(names)


@pytest.fixture(scope="module", params=_requested_launches())
def rig(request: pytest.FixtureRequest) -> Iterator[ChannelRig]:
    """A Claude Code session started with one launch, past its handshake."""
    if request.param not in LAUNCHES:
        pytest.fail(str(request.param), pytrace=False)
    for tool in ("tmux", "claude"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    nats_url: str = request.getfixturevalue("nats_server")
    with channel_rig(LAUNCHES[request.param], nats_url) as rig:
        rig.start_receiver()
        yield rig


def _logged(rig: ChannelRig, kind: str) -> bool:
    """Whether biff.log records the event: queued for the client, not displayed."""
    return re.search(rf"channel event \({kind}\) queued", rig.log_text()) is not None


class TestChannelDelivery:
    """Each kind of arrival shows up in the receiving session's pane.

    The log line says an event was queued for the client; only the pane shows it
    displayed.
    """

    def test_mail_event_shows_in_the_pane(self, rig: ChannelRig) -> None:
        rig.send_mail("channel mail probe")

        pane = rig.wait_for_pane(r"Call read_messages: \d+ unread")
        assert "call read_messages now" not in pane, "the nudge hook was not silenced"
        assert _logged(rig, "mail"), rig.log_text()

    def test_wall_event_shows_in_the_pane(self, rig: ChannelRig) -> None:
        rig.post_wall("channel wall probe")

        rig.wait_for_pane(rf"Call wall: new wall from {SENDER}")
        assert _logged(rig, "wall"), rig.log_text()

    def test_talk_invite_event_shows_in_the_pane(self, rig: ChannelRig) -> None:
        rig.invite_to_talk("channel talk probe")

        rig.wait_for_pane(rf"Call talk_read: talk invite from {SENDER}")
        assert _logged(rig, "talk_invite"), rig.log_text()
