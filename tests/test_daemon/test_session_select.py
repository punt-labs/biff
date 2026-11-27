"""CurrentPointer and SessionSelector: per-repo pointer and name/id lookup."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from biff.daemon.session_pointer import CurrentPointer
from biff.daemon.session_select import SessionSelectionError, SessionSelector
from biff.daemon.session_store import CliSessionRecord, CliSessionStore

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


class TestPointer:
    def test_repo_key_is_16_hex_and_per_repo(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        key = CurrentPointer.repo_key(a)
        assert len(key) == 16
        int(key, 16)
        assert key != CurrentPointer.repo_key(b)

    def test_repo_key_resolves_symlinks(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert CurrentPointer.repo_key(link) == CurrentPointer.repo_key(real)

    def test_roundtrip_and_overwrite(self, tmp_path: Path) -> None:
        ptr = CurrentPointer(tmp_path / "current")
        assert ptr.read(tmp_path) is None
        ptr.write(tmp_path, "cli-0000000000000001")
        ptr.write(tmp_path, "cli-0000000000000002")
        assert ptr.read(tmp_path) == "cli-0000000000000002"

    def test_modes_are_private(self, tmp_path: Path) -> None:
        ptr = CurrentPointer(tmp_path / "current")
        ptr.write(tmp_path, "cli-0000000000000001")
        assert (ptr.root.stat().st_mode & 0o777) == 0o700
        (file,) = ptr.root.iterdir()
        assert (file.stat().st_mode & 0o777) == 0o600

    def test_pointers_for_two_repos_are_independent(self, tmp_path: Path) -> None:
        ptr = CurrentPointer(tmp_path / "current")
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        ptr.write(a, "cli-aaaaaaaaaaaaaaaa")
        assert ptr.read(b) is None


def _selector(tmp_path: Path, **over: object) -> SessionSelector:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    fields: dict[str, object] = {
        "store": CliSessionStore(tmp_path / "sessions"),
        "pointer": CurrentPointer(tmp_path / "current"),
        "user": "kai",
        "repo": "biff",
        "repo_root": root,
        "hostname": "h1",
    }
    fields.update(over)
    return SessionSelector(**fields)  # type: ignore[arg-type]


def _make(
    sel: SessionSelector,
    tty: str = "tty1",
    *,
    user: str = "kai",
    repo: str = "biff",
    hostname: str = "h1",
) -> CliSessionRecord:
    record = CliSessionRecord.new(
        tty=tty,
        user=user,
        repo=repo,
        repo_root="/r",
        hostname=hostname,
        now=T0,
    )
    sel.store.create(record)
    return record


class TestLookup:
    def test_by_name_and_by_id(self, tmp_path: Path) -> None:
        sel = _selector(tmp_path)
        rec = _make(sel, "main")
        assert sel.lookup("main") == rec
        assert sel.lookup(rec.session_id) == rec

    def test_unknown(self, tmp_path: Path) -> None:
        with pytest.raises(SessionSelectionError, match="unknown session: x"):
            _selector(tmp_path).lookup("x")

    def test_other_host_is_unknown(self, tmp_path: Path) -> None:
        sel = _selector(tmp_path)
        _make(sel, "main", hostname="elsewhere")
        with pytest.raises(SessionSelectionError, match="unknown session"):
            sel.lookup("main")

    def test_other_user_is_named(self, tmp_path: Path) -> None:
        sel = _selector(tmp_path)
        _make(sel, "main", user="eric")
        with pytest.raises(SessionSelectionError, match="belongs to eric, not kai"):
            sel.lookup("main")

    def test_ended_name_reused_resolves_to_the_open_session(
        self, tmp_path: Path
    ) -> None:
        sel = _selector(tmp_path)
        old = _make(sel, "main")
        sel.store.end(old.session_id, now=T0)
        new = _make(sel, "main")
        assert sel.lookup("main") == new

    def test_only_an_ended_one_is_returned_as_ended(self, tmp_path: Path) -> None:
        sel = _selector(tmp_path)
        old = _make(sel, "main")
        sel.store.end(old.session_id, now=T0)
        assert sel.lookup("main").state == "ended"


class TestExplicitAndPointed:
    def test_explicit_ended_raises_the_named_error(self, tmp_path: Path) -> None:
        sel = _selector(tmp_path)
        rec = _make(sel, "main")
        sel.store.end(rec.session_id, now=T0)
        with pytest.raises(ValueError, match="biff session new --tty main"):
            sel.explicit("main")

    def test_pointed_none_when_unset(self, tmp_path: Path) -> None:
        assert _selector(tmp_path).pointed() is None

    @pytest.mark.parametrize(
        "over",
        [
            {"user": "eric"},
            {"repo": "other"},
            {"hostname": "h2"},
        ],
    )
    def test_pointed_stale_when_identity_differs(
        self, tmp_path: Path, over: dict[str, str]
    ) -> None:
        owner = _selector(tmp_path)
        rec = _make(owner, "main")
        owner.pointer.write(owner.repo_root, rec.session_id)
        assert _selector(tmp_path, **over).pointed() is None

    def test_pointed_stale_when_ended_or_missing_or_malformed(
        self, tmp_path: Path
    ) -> None:
        sel = _selector(tmp_path)
        rec = _make(sel, "main")
        sel.pointer.write(sel.repo_root, rec.session_id)
        assert sel.pointed() == rec
        sel.store.end(rec.session_id, now=T0)
        assert sel.pointed() is None
        sel.pointer.write(sel.repo_root, "cli-ffffffffffffffff")
        assert sel.pointed() is None
        sel.pointer.write(sel.repo_root, "../../etc/passwd")
        assert sel.pointed() is None


class TestFromResolvedUsesTheCommonRoot:
    def test_worktree_and_main_checkout_share_one_pointer_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from biff.config import ResolvedConfig
        from biff.models import BiffConfig

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        main = tmp_path / "main"
        worktree = tmp_path / "wt"
        main.mkdir()
        worktree.mkdir()
        config = BiffConfig(user="kai", repo_name="biff")

        from_main = SessionSelector.from_resolved(
            ResolvedConfig(
                config=config, data_dir=tmp_path, repo_root=main, repo_common_root=main
            )
        )
        from_worktree = SessionSelector.from_resolved(
            ResolvedConfig(
                config=config,
                data_dir=tmp_path,
                repo_root=worktree,
                repo_common_root=main,
            )
        )

        assert from_worktree.repo_root == from_main.repo_root == main


class TestIdentityIsFrozen:
    def test_other_identity_error_names_the_owner_and_the_way_out(
        self, tmp_path: Path
    ) -> None:
        sel = _selector(tmp_path)
        _make(sel, "main", user="eric")
        with pytest.raises(SessionSelectionError) as info:
            sel.lookup("main")
        text = str(info.value)
        assert "belongs to eric, not kai" in text
        assert "keeps the identity it was created with" in text
        assert "--user eric" in text

    def test_the_owner_can_reach_it_with_user(self, tmp_path: Path) -> None:
        sel = _selector(tmp_path, user="eric")
        rec = _make(sel, "main", user="eric")
        assert sel.explicit("main") == rec
