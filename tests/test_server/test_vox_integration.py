"""Unit tests for vox integration -- L0/L1 discovery and speak."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from biff.integration.vox import (
    _UNCHECKED,
    WALL_DEDUP_SECONDS,
    WALL_DEFAULT_VIBES,
    has_vox,
    speak_fire_and_forget,
    vibes_from_text,
    vox_binary,
)

# Opt out of the autouse _silence_vox fixture — these tests
# manage vox mocking themselves.
pytestmark = pytest.mark.vox

VOX_PATH = "/usr/local/bin/vox"
WHICH = "biff.integration.vox.shutil.which"
EXEC = "asyncio.create_subprocess_exec"
PROBE = "biff.integration.vox._probe_once_support"
SUBPROCESS_RUN = "biff.integration.vox.subprocess.run"


def _reset_vox_module_state() -> None:
    """Clear cached binary and probe results between tests."""
    import biff.integration.vox as mod

    mod._vox_binary = _UNCHECKED
    mod._vox_once_supported = _UNCHECKED


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
        _reset_vox_module_state()

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
        _reset_vox_module_state()

    @pytest.mark.asyncio
    async def test_spawns_subprocess(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.pid = 12345

        with (
            patch(WHICH, return_value=VOX_PATH),
            patch(PROBE, return_value=True),
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
                "--once",
                str(WALL_DEDUP_SECONDS),
                "Wall from kai: deploy freeze [alert]",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

    @pytest.mark.asyncio
    async def test_wall_dedup_flag_passed(self) -> None:
        """vox-0e9: --once <WALL_DEDUP_SECONDS> deduplicates fan-out spam.

        Wall broadcasts fan out to N sessions; each spawns ``vox unmute``
        with identical text. Without ``--once``, the user hears the same
        message N times. Biff passes ``--once`` with ``WALL_DEDUP_SECONDS``
        when the installed vox supports the flag; the gate is structural
        (``speak_fire_and_forget`` has a single caller in the wall refresh
        path; talk/write do not go through vox).
        """
        mock_proc = AsyncMock()
        mock_proc.pid = 54321

        with (
            patch(WHICH, return_value=VOX_PATH),
            patch(PROBE, return_value=True),
            patch(EXEC, return_value=mock_proc) as mock_exec,
        ):
            speak_fire_and_forget("deploy freeze")
            await asyncio.sleep(0)

            args = mock_exec.call_args[0]
            assert "--once" in args
            once_idx = args.index("--once")
            assert args[once_idx + 1] == str(WALL_DEDUP_SECONDS)
            assert WALL_DEDUP_SECONDS == 600

    @pytest.mark.asyncio
    async def test_argv_without_once_when_probe_fails(self) -> None:
        """Graceful degradation: old vox (no ``--once``) still plays audio.

        Pre-4.1.1 vox rejects ``--once`` with a non-zero exit. Since
        ``speak_fire_and_forget`` does not inspect returncode, passing the
        flag unconditionally would turn audio off entirely. When the probe
        reports no support, biff drops the flag and argv matches the
        pre-dedup shape: ``[vox, unmute, text]``.
        """
        mock_proc = AsyncMock()
        mock_proc.pid = 7777

        with (
            patch(WHICH, return_value=VOX_PATH),
            patch(PROBE, return_value=False),
            patch(EXEC, return_value=mock_proc) as mock_exec,
        ):
            speak_fire_and_forget("deploy freeze")
            await asyncio.sleep(0)

            mock_exec.assert_called_once_with(
                VOX_PATH,
                "unmute",
                "deploy freeze",
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
            patch(PROBE, return_value=True),
            patch(EXEC, return_value=mock_proc) as mock_exec,
        ):
            speak_fire_and_forget("Hello world")
            await asyncio.sleep(0)

            args = mock_exec.call_args[0]
            assert "--vibe-tags" not in args


class TestProbeOnceSupport:
    """Feature detection for ``vox unmute --once``."""

    def setup_method(self) -> None:
        _reset_vox_module_state()

    def test_supported_when_help_mentions_flag(self) -> None:
        from biff.integration.vox import _probe_once_support

        help_text = (
            "Usage: vox unmute [OPTIONS] [TEXT]\n\n"
            "Options:\n"
            "  --once INTEGER  Deduplicate identical text within N seconds.\n"
            "  --help          Show this message and exit.\n"
        )
        with patch(SUBPROCESS_RUN) as mock_run:
            mock_run.return_value.stdout = help_text
            assert _probe_once_support(VOX_PATH) is True

    def test_unsupported_when_help_omits_flag(self) -> None:
        from biff.integration.vox import _probe_once_support

        help_text = (
            "Usage: vox unmute [OPTIONS] [TEXT]\n\n"
            "Options:\n"
            "  --voice TEXT  Voice name.\n"
            "  --help        Show this message and exit.\n"
        )
        with patch(SUBPROCESS_RUN) as mock_run:
            mock_run.return_value.stdout = help_text
            assert _probe_once_support(VOX_PATH) is False

    def test_cached_across_calls(self) -> None:
        from biff.integration.vox import _probe_once_support

        help_text = "  --once INTEGER  Dedup window\n"
        with patch(SUBPROCESS_RUN) as mock_run:
            mock_run.return_value.stdout = help_text
            _probe_once_support(VOX_PATH)
            _probe_once_support(VOX_PATH)
            mock_run.assert_called_once()

    def test_oserror_degrades_to_unsupported(self) -> None:
        from biff.integration.vox import _probe_once_support

        with patch(SUBPROCESS_RUN, side_effect=OSError("probe exec failed")):
            assert _probe_once_support(VOX_PATH) is False

    def test_timeout_degrades_to_unsupported(self) -> None:
        import subprocess as sp

        from biff.integration.vox import _probe_once_support

        with patch(
            SUBPROCESS_RUN,
            side_effect=sp.TimeoutExpired(cmd="vox unmute --help", timeout=2),
        ):
            assert _probe_once_support(VOX_PATH) is False


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
        _reset_vox_module_state()

    @pytest.mark.asyncio
    async def test_oserror_swallowed(self) -> None:
        with (
            patch(WHICH, return_value=VOX_PATH),
            patch(EXEC, side_effect=OSError("exec failed")),
        ):
            speak_fire_and_forget("should not crash")
            await asyncio.sleep(0)
