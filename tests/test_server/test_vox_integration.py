"""Unit tests for vox integration -- L0/L1 discovery and speak."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from biff.integration.vox import (
    _UNCHECKED,
    WALL_DEFAULT_VIBES,
    has_vox,
    speak_fire_and_forget,
    vibes_from_text,
    vox_binary,
)

VOX_PATH = "/usr/local/bin/vox"
WHICH = "biff.integration.vox.shutil.which"
EXEC = "asyncio.create_subprocess_exec"


class TestHasVox:
    """L0 sentinel check."""

    def test_present(self, tmp_path: Path) -> None:
        vox_dir = tmp_path / ".vox"
        vox_dir.mkdir()
        (vox_dir / "config.md").write_text("---\nvoice: lily\n---\n")
        assert has_vox(tmp_path) is True

    def test_absent(self, tmp_path: Path) -> None:
        assert has_vox(tmp_path) is False

    def test_dir_exists_but_no_config(self, tmp_path: Path) -> None:
        (tmp_path / ".vox").mkdir()
        assert has_vox(tmp_path) is False


class TestVoxBinary:
    """L1 binary discovery."""

    def setup_method(self) -> None:
        import biff.integration.vox as mod

        mod._vox_binary = _UNCHECKED

    def test_found(self) -> None:
        with patch(WHICH, return_value=VOX_PATH):
            assert vox_binary() == VOX_PATH

    def test_not_found(self) -> None:
        with patch(WHICH, return_value=None):
            assert vox_binary() is None

    def test_cached(self) -> None:
        with patch(WHICH, return_value=VOX_PATH) as mock:
            vox_binary()
            vox_binary()
            mock.assert_called_once()


class TestSpeakFireAndForget:
    """Fire-and-forget subprocess dispatch."""

    def setup_method(self) -> None:
        import biff.integration.vox as mod

        mod._vox_binary = _UNCHECKED

    @pytest.mark.asyncio
    async def test_spawns_subprocess(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.pid = 12345

        with (
            patch(WHICH, return_value=VOX_PATH),
            patch(EXEC, return_value=mock_proc) as mock_exec,
        ):
            speak_fire_and_forget(
                "Wall from kai: deploy freeze",
                vibe_tags="[alert]",
            )
            await asyncio.sleep(0)

            mock_exec.assert_called_once_with(
                VOX_PATH,
                "unmute",
                "Wall from kai: deploy freeze [alert]",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

    @pytest.mark.asyncio
    async def test_no_vox_binary_is_noop(self) -> None:
        with (
            patch(WHICH, return_value=None),
            patch(EXEC) as mock_exec,
        ):
            speak_fire_and_forget("should not speak")
            await asyncio.sleep(0)
            mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_vibe_tags(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.pid = 99

        with (
            patch(WHICH, return_value=VOX_PATH),
            patch(EXEC, return_value=mock_proc) as mock_exec,
        ):
            speak_fire_and_forget("Hello world")
            await asyncio.sleep(0)

            args = mock_exec.call_args[0]
            assert "--vibe-tags" not in args


class TestVibesFromText:
    """Emoticon-to-vibe extraction."""

    def test_default_no_emoticons(self) -> None:
        assert vibes_from_text("deploy freeze") == WALL_DEFAULT_VIBES

    def test_smiley(self) -> None:
        assert "warm" in vibes_from_text("good morning :)")

    def test_wink(self) -> None:
        assert "playful" in vibes_from_text("just kidding ;-)")

    def test_sad(self) -> None:
        assert "sad" in vibes_from_text("build is broken :(")

    def test_excited(self) -> None:
        assert "excited" in vibes_from_text("shipped! :D")

    def test_urgent_bangs(self) -> None:
        assert "urgent" in vibes_from_text("DO NOT PUSH!!")

    def test_first_match_wins(self) -> None:
        result = vibes_from_text("wow :D and also :(")
        assert "excited" in result


class TestSpeakOSError:
    """OSError handling in speak_fire_and_forget."""

    def setup_method(self) -> None:
        import biff.integration.vox as mod

        mod._vox_binary = _UNCHECKED

    @pytest.mark.asyncio
    async def test_oserror_swallowed(self) -> None:
        with (
            patch(WHICH, return_value=VOX_PATH),
            patch(EXEC, side_effect=OSError("exec failed")),
        ):
            speak_fire_and_forget("should not crash")
            await asyncio.sleep(0)
