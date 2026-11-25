"""Channel events reach a live interactive Claude Code session.

Channels are stdio-only and interactive-only: the Agent SDK and raw stdio never
receive them, so this drives a real ``claude`` in tmux and reads its screen.  A
second biff user writes to it through the CLI; the event must show in the pane
and ``biff.log`` must record that the server wrote it.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_claude_channels.rig import SENDER, ChannelRig, Launch

pytestmark = pytest.mark.claude_channels

# Every launch tried; ``BIFF_CHANNEL_LAUNCHES`` names which of them to run.
LAUNCHES = {
    launch.name: launch
    for launch in (
        Launch("inline", "plugin:biff-dev@inline", True),
        Launch("marketplace", "plugin:biff-dev@punt-labs", True),
        Launch("bare-plugin", "plugin:biff-dev", True),
        Launch("server-tty", "server:tty", True),
        Launch("server-plugin-tty", "server:plugin:biff-dev:tty", True),
    )
}
_DEFAULT_LAUNCHES = "inline"


def _selected() -> list[str]:
    names = os.environ.get("BIFF_CHANNEL_LAUNCHES", _DEFAULT_LAUNCHES)
    return [name.strip() for name in names.split(",") if name.strip()]


@pytest.fixture(scope="module", params=_selected())
def rig(request: pytest.FixtureRequest, nats_server: str) -> Iterator[ChannelRig]:
    """A Claude Code session started with one launch, past its handshake."""
    for tool in ("tmux", "claude"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not on PATH")
    # Outside the checkout: Claude Code imports every CLAUDE.md above its working
    # directory, and the repo's own ``.tmp`` sits under this repository.
    root = Path(tempfile.mkdtemp(prefix="biff-channels-", dir="/tmp"))
    rig = ChannelRig(root, LAUNCHES[request.param], nats_server)
    try:
        rig.start_receiver()
        yield rig
    finally:
        rig.close()
        shutil.rmtree(root, ignore_errors=True)


def _logged(rig: ChannelRig, kind: str) -> bool:
    return re.search(rf"channel event \({kind}\) sent", rig.log_text()) is not None


class TestChannelDelivery:
    """Each kind of arrival shows up in the receiving session's pane."""

    def test_mail_event_shows_in_the_pane(self, rig: ChannelRig) -> None:
        rig.send_mail("channel mail probe")

        pane = rig.wait_for_pane(r"Call read_messages: \d+ unread biff messages?")
        assert "call read_messages now" not in pane, "the nudge hook was not silenced"
        assert _logged(rig, "mail"), rig.log_text()

    def test_wall_event_shows_in_the_pane(self, rig: ChannelRig) -> None:
        rig.post_wall("channel wall probe")

        rig.wait_for_pane(rf"Call wall: .*{SENDER}")
        assert _logged(rig, "wall"), rig.log_text()

    def test_talk_invite_event_shows_in_the_pane(self, rig: ChannelRig) -> None:
        rig.invite_to_talk("channel talk probe")

        rig.wait_for_pane(rf"Ask the user: .*{SENDER}")
        assert _logged(rig, "talk_invite"), rig.log_text()


def test_launch_names_are_the_ones_selected() -> None:
    assert set(_selected()) <= set(LAUNCHES), sorted(LAUNCHES)
