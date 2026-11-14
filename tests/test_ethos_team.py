"""EthosTeams resolves a handle's manager role from team YAML on disk."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.ethos_team import EthosTeams, RoleEntry

REPO_ROOT = Path(__file__).resolve().parent.parent

SLUG = "punt-labs/biff"

TEAM = """\
name: t
repositories:
  - punt-labs/biff
members:
  - identity: jfreeman
    role: ceo
  - identity: claude
    role: coo
  - identity: adb
    role: infra-engineer
collaborations:
  - from: coo
    to: ceo
    type: reports_to
  - from: infra-engineer
    to: coo
    type: reports_to
"""


def _write(root: Path, name: str, text: str) -> None:
    teams = root / ".punt-labs" / "ethos" / "teams"
    teams.mkdir(parents=True, exist_ok=True)
    (teams / name).write_text(text)


class TestManagerOf:
    def test_manager_found_through_role(self, tmp_path: Path) -> None:
        _write(tmp_path, "t.yaml", TEAM)
        teams = EthosTeams.from_repo(tmp_path, SLUG)
        assert teams.manager_of("claude") == "ceo"

    def test_manager_of_specialist_is_coo(self, tmp_path: Path) -> None:
        _write(tmp_path, "t.yaml", TEAM)
        assert EthosTeams.from_repo(tmp_path, SLUG).manager_of("adb") == "coo"

    def test_identity_not_in_any_team(self, tmp_path: Path) -> None:
        _write(tmp_path, "t.yaml", TEAM)
        with pytest.raises(LookupError):
            EthosTeams.from_repo(tmp_path, SLUG).manager_of("nobody")

    def test_role_without_reports_to_edge(self, tmp_path: Path) -> None:
        _write(tmp_path, "t.yaml", TEAM)
        with pytest.raises(LookupError):
            EthosTeams.from_repo(tmp_path, SLUG).manager_of("jfreeman")

    def test_edge_of_other_type_is_ignored(self, tmp_path: Path) -> None:
        _write(tmp_path, "t.yaml", TEAM.replace("reports_to", "collaborates"))
        with pytest.raises(LookupError):
            EthosTeams.from_repo(tmp_path, SLUG).manager_of("claude")

    def test_first_sorted_file_wins(self, tmp_path: Path) -> None:
        _write(tmp_path, "b.yaml", TEAM.replace("to: ceo", "to: cto"))
        _write(tmp_path, "a.yaml", TEAM)
        assert EthosTeams.from_repo(tmp_path, SLUG).manager_of("claude") == "ceo"

    def test_constructor_takes_the_graph(self) -> None:
        teams = EthosTeams([({"claude": "coo"}, {"coo": "ceo"}, "t")])
        assert teams.manager_of("claude") == "ceo"


UNSCOPED = TEAM.replace("repositories:\n  - punt-labs/biff\n", "")


class TestTeamScoping:
    def test_shared_role_uses_the_handles_own_team(self, tmp_path: Path) -> None:
        # a.yaml sorts first and also has a coo, but kai is only in b.yaml.
        _write(tmp_path, "a.yaml", TEAM.replace("to: ceo", "to: cto"))
        _write(
            tmp_path,
            "b.yaml",
            TEAM.replace("identity: claude", "identity: kai"),
        )
        assert EthosTeams.from_repo(tmp_path, SLUG).manager_of("kai") == "ceo"

    def test_team_for_another_repo_is_not_read(self, tmp_path: Path) -> None:
        _write(tmp_path, "t.yaml", TEAM.replace("punt-labs/biff", "punt-labs/other"))
        with pytest.raises(LookupError):
            EthosTeams.from_repo(tmp_path, SLUG).manager_of("claude")

    def test_team_without_repositories_is_not_read(self, tmp_path: Path) -> None:
        _write(tmp_path, "t.yaml", UNSCOPED)
        with pytest.raises(LookupError):
            EthosTeams.from_repo(tmp_path, SLUG).manager_of("claude")

    def test_unselected_team_does_not_shadow(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.yaml", UNSCOPED.replace("to: ceo", "to: cto"))
        _write(tmp_path, "b.yaml", TEAM)
        assert EthosTeams.from_repo(tmp_path, SLUG).manager_of("claude") == "ceo"


class TestMalformedInput:
    def test_missing_teams_dir(self, tmp_path: Path) -> None:
        with pytest.raises(LookupError):
            EthosTeams.from_repo(tmp_path, SLUG).manager_of("claude")

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "- just\n- a list\n",
            "just a string\n",
            "key: [unclosed\n",
            "members: nope\ncollaborations: nope\n",
            "members: [1, {identity: claude}, {role: coo}]\n",
            (
                "members:\n  - {identity: claude, role: coo}\n"
                "collaborations: {from: coo, to: ceo}\n"
            ),
            (
                "members:\n  - {identity: claude, role: coo}\n"
                "collaborations:\n  - {from: coo, type: reports_to}\n"
                "  - {to: ceo, type: reports_to}\n  - 7\n"
            ),
        ],
    )
    def test_bad_file_yields_empty_graph(self, tmp_path: Path, text: str) -> None:
        _write(tmp_path, "t.yaml", text)
        teams = EthosTeams.from_repo(tmp_path, SLUG)
        with pytest.raises(LookupError):
            teams.manager_of("claude")

    def test_bad_file_does_not_hide_good_file(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.yaml", "key: [unclosed\n")
        _write(tmp_path, "b.yaml", TEAM)
        assert EthosTeams.from_repo(tmp_path, SLUG).manager_of("claude") == "ceo"

    def test_non_utf8_file_is_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.yaml", "")
        (tmp_path / ".punt-labs/ethos/teams/a.yaml").write_bytes(b"\xff\xfe\x00bad")
        with pytest.raises(LookupError):
            EthosTeams.from_repo(tmp_path, SLUG).manager_of("claude")


class TestHostileRole:
    def test_role_outside_grammar_is_dropped(self, tmp_path: Path) -> None:
        text = TEAM.replace("to: ceo", 'to: "ce\\x1b[31mo"')
        _write(tmp_path, "t.yaml", text)
        with pytest.raises(LookupError):
            EthosTeams.from_repo(tmp_path, SLUG).manager_of("claude")


class TestRolesOf:
    def test_role_team_and_manager(self, tmp_path: Path) -> None:
        _write(tmp_path, "t.yaml", TEAM)
        assert EthosTeams.from_repo(tmp_path, SLUG).roles_of("adb") == (
            RoleEntry("infra-engineer", "t", "coo", "claude"),
        )

    def test_one_entry_per_team(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.yaml", TEAM.replace("name: t", "name: eng"))
        _write(
            tmp_path,
            "b.yaml",
            TEAM.replace("name: t", "name: gstack")
            .replace("identity: claude", "identity: boss")
            .replace("role: infra-engineer", "role: reviewer")
            .replace("from: infra-engineer", "from: reviewer"),
        )
        roles = EthosTeams.from_repo(tmp_path, SLUG).roles_of("adb")
        assert [(r.role, r.team, r.manager_role) for r in roles] == [
            ("infra-engineer", "eng", "coo"),
            ("reviewer", "gstack", "coo"),
        ]
        assert [r.manager_person for r in roles] == ["claude", "boss"]

    def test_role_without_manager(self, tmp_path: Path) -> None:
        _write(tmp_path, "t.yaml", TEAM)
        assert EthosTeams.from_repo(tmp_path, SLUG).roles_of("jfreeman") == (
            RoleEntry("ceo", "t"),
        )

    def test_unknown_handle_and_no_teams_yield_nothing(self, tmp_path: Path) -> None:
        assert EthosTeams.from_repo(tmp_path, SLUG).roles_of("claude") == ()
        _write(tmp_path, "t.yaml", TEAM)
        assert EthosTeams.from_repo(tmp_path, SLUG).roles_of("nobody") == ()

    def test_team_name_outside_grammar_falls_back_to_file_stem(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "eng.yaml", TEAM.replace("name: t", 'name: "x y"'))
        (entry,) = EthosTeams.from_repo(tmp_path, SLUG).roles_of("claude")
        assert entry.team == "eng"

    def test_for_repo_root_none_is_empty(self) -> None:
        assert EthosTeams.for_repo_root(None).roles_of("claude") == ()


def test_real_team_data_resolves_adb_to_coo() -> None:
    assert EthosTeams.from_repo(REPO_ROOT, SLUG).manager_of("adb") == "coo"
