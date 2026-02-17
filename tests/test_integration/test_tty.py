"""Tests for the /tty session naming tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from biff.testing import RecordingClient


class TestTtyNaming:
    """Naming a session and verifying it appears in /who and /finger."""

    @pytest.mark.transcript
    async def test_name_visible_in_who(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai names session; eric sees it in /who."""
        kai.transcript.title = "TTY: named session visible in /who"
        kai.transcript.description = "kai names a session, eric sees name in /who."

        await kai.call("tty", name="auth-work")
        result = await eric.call("who")

        assert "@kai" in result
        assert "auth-work" in result

    @pytest.mark.transcript
    async def test_name_visible_in_finger(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai names session; eric sees it in /finger."""
        kai.transcript.title = "TTY: named session visible in /finger"
        kai.transcript.description = "kai names a session, eric sees name in /finger."

        await kai.call("tty", name="pr-review")
        result = await eric.call("finger", user="@kai")

        assert "pr-review" in result

    @pytest.mark.transcript
    async def test_unnamed_shows_hex(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Unnamed session shows the raw tty identifier."""
        kai.transcript.title = "TTY: unnamed session shows hex ID"
        kai.transcript.description = "kai has no tty name, /who shows hex ID."

        await kai.call("plan", message="working")
        result = await eric.call("who")

        assert "@kai" in result
        # Fixture tty is "tty1" — should appear as-is when no tty_name set
        assert "tty1" in result

    @pytest.mark.transcript
    async def test_rename_session(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """kai renames session; new name visible."""
        kai.transcript.title = "TTY: rename session"
        kai.transcript.description = "kai renames session, eric sees updated name."

        await kai.call("tty", name="first-name")
        result = await eric.call("who")
        assert "first-name" in result

        await kai.call("tty", name="second-name")
        result = await eric.call("who")
        assert "second-name" in result
        assert "first-name" not in result

    async def test_tty_returns_confirmation(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """Tool returns confirmation message."""
        result = await kai.call("tty", name="my-session")
        assert result == "TTY: my-session"


class TestTtyNameResolution:
    """Addressing @user:tty_name resolves to the correct session."""

    @pytest.mark.transcript
    async def test_finger_by_tty_name(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """eric can /finger @kai:auth-work using the tty_name."""
        kai.transcript.title = "TTY: finger by tty_name"
        kai.transcript.description = "kai names session, eric fingers by name."

        await kai.call("tty", name="auth-work")
        result = await eric.call("finger", user="@kai:auth-work")

        assert "Login: kai" in result
        assert "auth-work" in result

    @pytest.mark.transcript
    async def test_write_by_tty_name(
        self, kai: RecordingClient, eric: RecordingClient
    ) -> None:
        """eric can /write @kai:auth-work using the tty_name."""
        kai.transcript.title = "TTY: write by tty_name"
        kai.transcript.description = "kai names session, eric writes to name."

        await kai.call("tty", name="auth-work")
        await eric.call("plan", message="working")
        result = await eric.call("write", to="@kai:auth-work", message="hey")

        assert "Message sent" in result

        # kai receives the message
        inbox = await kai.call("read_messages")
        assert "hey" in inbox
