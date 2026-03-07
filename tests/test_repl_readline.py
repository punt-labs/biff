"""Tests for REPL readline support (biff.repl_readline)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from biff.repl_readline import setup


class TestSetup:
    def test_no_crash_without_readline(self) -> None:
        """setup() is safe when readline is not available."""
        with patch.dict("sys.modules", {"readline": None}):
            # Force re-import failure
            import importlib

            import biff.repl_readline

            importlib.reload(biff.repl_readline)
            # Should not raise
            biff.repl_readline.setup(["who", "write"])

    def test_sets_completer(self) -> None:
        """setup() configures a tab completer."""
        mock_rl = MagicMock()
        with patch.dict("sys.modules", {"readline": mock_rl}):
            import importlib

            import biff.repl_readline

            importlib.reload(biff.repl_readline)
            biff.repl_readline.setup(["who", "write", "wall"])
            mock_rl.set_completer.assert_called_once()

    def test_completer_matches(self) -> None:
        """The completer returns matching command names."""
        import readline

        setup(["who", "write", "wall", "read"])
        completer = readline.get_completer()
        assert completer is not None

        # "w" should match wall, who, write (sorted by startswith)
        w_results = [completer("w", i) for i in range(4)]
        assert set(w_results[:3]) == {"wall", "who", "write"}
        assert w_results[3] is None

        # "r" should match read
        assert completer("r", 0) == "read"
        assert completer("r", 1) is None

        # "z" should match nothing
        assert completer("z", 0) is None

    def test_loads_history_file(self, tmp_path: Path) -> None:
        """setup() loads history from file when it exists."""
        import readline

        history_file = tmp_path / "test_history"
        history_file.write_text("who\nfinger @kai\n")

        with patch("biff.repl_readline._HISTORY_PATH", history_file):
            setup(["who"])

        # readline should have loaded the history
        count = readline.get_current_history_length()
        assert count >= 2

    def test_missing_history_is_fine(self, tmp_path: Path) -> None:
        """setup() handles missing history file gracefully."""
        missing = tmp_path / "nonexistent"
        with patch("biff.repl_readline._HISTORY_PATH", missing):
            setup(["who"])  # Should not raise
