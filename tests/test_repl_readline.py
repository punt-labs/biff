"""Tests for REPL readline support (biff.repl_readline)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from biff.repl_readline import setup


class TestSetup:
    @pytest.fixture(autouse=True)
    def _no_atexit_history_write(self) -> Iterator[None]:
        """Keep ``setup()``'s exit hook out of the developer's real history.

        ``setup()`` registers an ``atexit`` handler that re-reads the module
        global ``_HISTORY_PATH`` when the process ends -- after any ``patch``
        in a test has been undone -- so an unpatched ``atexit`` lets the suite
        overwrite ``~/.punt-labs/biff/repl_history`` with whatever the tests
        pushed into readline.
        """
        with patch("atexit.register"):
            yield

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

        # The fixture is written by readline itself rather than hand-rolled:
        # libedit (macOS, and the uv-managed CPython builds this repo runs on)
        # only recognizes a history file carrying its own `_HiStOrY_V2_`
        # header, so a plain-text file loads zero entries there while passing
        # under GNU readline.
        history_file = tmp_path / "test_history"
        readline.clear_history()
        readline.add_history("who")
        readline.add_history("finger @kai")
        readline.write_history_file(str(history_file))
        readline.clear_history()

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
