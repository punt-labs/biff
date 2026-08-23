"""Tests for CI workflow deployment.

Unit tests for deploy/remove/check operations on
``.github/workflows/biff-notify.yml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.ci_workflow import (
    _WORKFLOW_NAME,
    _WORKFLOWS_PLACEHOLDER,
    NotifyWorkflow,
    _template_content,
    check_ci_workflow,
    deploy_ci_workflow,
    remove_ci_workflow,
)


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal directory structure (no .git needed for CI workflow)."""
    return tmp_path


def _write_workflow(root: Path, filename: str, name: str | None) -> None:
    """Write a minimal GitHub Actions workflow with an optional top-level name."""
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    header = f"name: {name}\n" if name is not None else ""
    job = "on: [push]\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps: []\n"
    (wf / filename).write_text(header + job)


# A resolvable, non-dead action pin. The prior pin
# (e58605a9b6da7c637471fab8847a5e5a6b8df081) does not exist on GitHub — the
# notify job failed to resolve the action on first trigger in every repo.
_LIVE_SETUP_UV_SHA = "c771a70e6277c0a99b617c7a806ffedaca235ff9"
_DEAD_SETUP_UV_SHA = "e58605a9b6da7c637471fab8847a5e5a6b8df081"

# actions/checkout@v7.0.1, matching the SHA every other workflow in this repo
# already pins (test.yml, lint.yml, docs.yml, release.yml, subprocess-tests.yml,
# hosted-nats.yml) -- verified against actions/checkout's tag refs.
_LIVE_CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"

_PINNED_BIFF_VERSION = "1.15.2"


# ── template action pins ───────────────────────────────────────────


class TestTemplateActionPins:
    """The bundled template must pin only resolvable action SHAs."""

    def test_setup_uv_pinned_to_live_sha(self) -> None:
        content = _template_content()
        assert _LIVE_SETUP_UV_SHA in content
        assert _DEAD_SETUP_UV_SHA not in content

    def test_checkout_pinned_to_live_sha_matching_sibling_workflows(self) -> None:
        content = _template_content()
        assert _LIVE_CHECKOUT_SHA in content

    def test_template_carries_exactly_one_placeholder(self) -> None:
        """The template⇄constant invariant: render()'s str.replace needs a match.

        If the template's placeholder line drifts (reindent, edited comment,
        whitespace), str.replace would silently no-op and deposit a workflow
        watching nothing. Pin the invariant so drift fails at test time.
        """
        assert _template_content().count(_WORKFLOWS_PLACEHOLDER) == 1


class TestTemplateHardening:
    """CI-notify job hardening: pinned checkout ref, no persisted creds, pinned
    package version, and a timeout that survives a cold ``uvx`` install.
    """

    def test_checkout_pins_ref_to_default_branch_explicitly(self) -> None:
        """This job only reads ``.punt-labs/biff`` config to route the
        notification -- it must stay pinned to the default branch, never the
        failing commit. A failing commit can be any branch that made a
        watched workflow fail, and that branch's config.yaml would otherwise
        control where the notification (and the github-actions identity)
        gets sent.
        """
        content = _template_content()
        assert "ref: ${{ github.event.repository.default_branch }}" in content
        assert "ref: ${{ github.event.workflow_run.head_sha }}" not in content

    def test_checkout_does_not_persist_credentials(self) -> None:
        """The very next step downloads and executes third-party code
        (``uvx --from punt-biff``) -- the checkout must not leave a
        ``GITHUB_TOKEN`` in ``.git/config`` for that code to read.
        """
        content = _template_content()
        assert "persist-credentials: false" in content

    def test_biff_package_pinned_to_known_version(self) -> None:
        """``uvx --from punt-biff`` unpinned resolves latest at runtime --
        non-reproducible and a supply-chain risk for a job that runs with a
        live token in scope.
        """
        content = _template_content()
        assert f"punt-biff=={_PINNED_BIFF_VERSION}" in content

    def test_timeout_survives_cold_uv_install(self) -> None:
        """2 minutes is too tight for ``setup-uv`` + a cold ``uvx`` install of
        ``punt-biff`` -- a slow cold start silently drops the failure notice.
        """
        content = _template_content()
        assert "timeout-minutes: 10" in content

    def test_setup_uv_cache_enabled(self) -> None:
        content = _template_content()
        assert "enable-cache: true" in content


class TestRenderPlaceholderGuard:
    """render() fails loud, never silently deposits an unrendered template."""

    def test_missing_placeholder_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drifted = "name: Biff CI Notifications\non:\n  workflow_run:\n    types: [x]\n"
        monkeypatch.setattr("biff.ci_workflow._template_content", lambda: drifted)
        with pytest.raises(ValueError, match="placeholder"):
            NotifyWorkflow(tmp_path).render()


# ── NotifyWorkflow.render (per-repo parameterization) ──────────────


class TestNotifyWorkflowRender:
    """The deposited workflow watches the TARGET repo's own workflow names.

    ``workflow_run`` matches a workflow's ``name:`` field, so a fixed list
    (the old ``["Lint", "Tests", "Docs"]``) watched the wrong set in every
    repo but biff's own. The render reads the repo's ``.github/workflows``.
    """

    def test_lists_repo_workflow_names_sorted_unique(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_workflow(repo, "ci.yml", "Tests")
        _write_workflow(repo, "build.yml", "Build")
        _write_workflow(repo, "docs.yml", "Docs")
        rendered = NotifyWorkflow(repo).render()
        assert 'workflows: ["Build", "Docs", "Tests"]' in rendered

    def test_dedupes_repeated_names(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_workflow(repo, "a.yml", "Lint")
        _write_workflow(repo, "b.yml", "Lint")
        rendered = NotifyWorkflow(repo).render()
        assert 'workflows: ["Lint"]' in rendered

    def test_reads_yaml_extension(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_workflow(repo, "ci.yaml", "Build")
        rendered = NotifyWorkflow(repo).render()
        assert 'workflows: ["Build"]' in rendered

    def test_excludes_notify_workflow_by_filename(self, tmp_path: Path) -> None:
        """The notify workflow file itself must never appear in its own list."""
        repo = _make_repo(tmp_path)
        _write_workflow(repo, "ci.yml", "Tests")
        _write_workflow(repo, _WORKFLOW_NAME, "Some Other Name")
        rendered = NotifyWorkflow(repo).render()
        assert 'workflows: ["Tests"]' in rendered

    def test_excludes_notify_workflow_by_name(self, tmp_path: Path) -> None:
        """A workflow literally named 'Biff CI Notifications' is not self-watched."""
        repo = _make_repo(tmp_path)
        _write_workflow(repo, "ci.yml", "Tests")
        _write_workflow(repo, "old-notify.yml", "Biff CI Notifications")
        rendered = NotifyWorkflow(repo).render()
        # The notify workflow's own name appears once (its `name:` field), never
        # as a quoted item in its own watched list.
        assert 'workflows: ["Tests"]' in rendered
        assert '"Biff CI Notifications"' not in rendered

    def test_empty_when_no_workflows(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        rendered = NotifyWorkflow(repo).render()
        assert "workflows: []" in rendered
        assert "No sibling workflows" in rendered  # explanatory comment

    def test_empty_when_only_self_present(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_workflow(repo, _WORKFLOW_NAME, "Biff CI Notifications")
        rendered = NotifyWorkflow(repo).render()
        assert "workflows: []" in rendered

    def test_skips_files_without_name(self, tmp_path: Path) -> None:
        """A nameless workflow has no watchable name — it is skipped, not fatal."""
        repo = _make_repo(tmp_path)
        _write_workflow(repo, "ci.yml", "Tests")
        _write_workflow(repo, "nameless.yml", None)
        rendered = NotifyWorkflow(repo).render()
        assert 'workflows: ["Tests"]' in rendered

    def test_skips_symlinked_workflow(self, tmp_path: Path) -> None:
        """A symlinked *.yml is not ours — never followed, never parsed.

        Mirrors the write-path guard (ensure_real_dir, is_regular_file): a
        committed ``evil.yml -> /outside`` must not be opened and read during
        enable/check. The read path enforces the same regular-file discipline.
        """
        repo = _make_repo(tmp_path)
        _write_workflow(repo, "ci.yml", "Tests")
        outside = tmp_path / "outside.yml"
        outside.write_text("name: Sneaky\non: [push]\n")
        (repo / ".github" / "workflows" / "evil.yml").symlink_to(outside)
        rendered = NotifyWorkflow(repo).render()
        assert 'workflows: ["Tests"]' in rendered
        assert "Sneaky" not in rendered

    def test_skips_non_yaml_files(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_workflow(repo, "ci.yml", "Tests")
        readme = repo / ".github" / "workflows" / "README.md"
        readme.write_text("name: Not A Workflow")
        rendered = NotifyWorkflow(repo).render()
        assert 'workflows: ["Tests"]' in rendered

    def test_symlinked_workflows_dir_reads_nothing(self, tmp_path: Path) -> None:
        """A symlinked .github/workflows must not be traversed out of the repo.

        deploy runs ensure_real_dir first, but check_ci_workflow renders with no
        such guard — so _watched_names self-guards: a symlinked .github or
        .github/workflows yields an empty list (check reports not-current; the
        next deploy replaces the symlink and renders correctly).
        """
        repo = tmp_path / "repo"
        (repo / ".github").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "ci.yml").write_text("name: Sneaky\non: [push]\n")
        (repo / ".github" / "workflows").symlink_to(outside, target_is_directory=True)

        rendered = NotifyWorkflow(repo).render()
        assert "workflows: []" in rendered
        assert "Sneaky" not in rendered

    def test_symlinked_github_dir_reads_nothing(self, tmp_path: Path) -> None:
        """Same guard one level up: a symlinked .github component."""
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside"
        (outside / "workflows").mkdir(parents=True)
        (outside / "workflows" / "ci.yml").write_text("name: Sneaky\non: [push]\n")
        (repo / ".github").symlink_to(outside, target_is_directory=True)

        rendered = NotifyWorkflow(repo).render()
        assert "workflows: []" in rendered
        assert "Sneaky" not in rendered

    def test_rendered_carries_live_sha(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_workflow(repo, "ci.yml", "Tests")
        rendered = NotifyWorkflow(repo).render()
        assert _LIVE_SETUP_UV_SHA in rendered
        assert _DEAD_SETUP_UV_SHA not in rendered


class TestDeployIdempotencyAcrossNameChanges:
    """A repo whose workflow names change re-renders on the next enable."""

    def test_redeploys_after_workflow_name_change(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_workflow(repo, "ci.yml", "Tests")
        assert deploy_ci_workflow(repo) is True
        assert deploy_ci_workflow(repo) is False  # unchanged names → no-op

        _write_workflow(repo, "ci.yml", "Unit Tests")  # rename the workflow
        assert check_ci_workflow(repo) is False  # deposited file now stale
        assert deploy_ci_workflow(repo) is True  # re-rendered
        target = repo / ".github" / "workflows" / _WORKFLOW_NAME
        assert 'workflows: ["Unit Tests"]' in target.read_text()

    def test_check_current_matches_render(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_workflow(repo, "ci.yml", "Build")
        deploy_ci_workflow(repo)
        assert check_ci_workflow(repo) is True


# ── deploy_ci_workflow ─────────────────────────────────────────────


class TestDeployCiWorkflow:
    """CI workflow deployment — create, update, idempotent."""

    def test_creates_workflow(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        assert deploy_ci_workflow(repo) is True

        target = repo / ".github" / "workflows" / _WORKFLOW_NAME
        assert target.exists()
        assert target.read_text() == NotifyWorkflow(repo).render()

    def test_creates_directories(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        deploy_ci_workflow(repo)
        assert (repo / ".github" / "workflows").is_dir()

    def test_idempotent(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        assert deploy_ci_workflow(repo) is True
        assert deploy_ci_workflow(repo) is False  # No change

    def test_updates_stale_workflow(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        target = repo / ".github" / "workflows" / _WORKFLOW_NAME
        target.parent.mkdir(parents=True)
        target.write_text("old content")

        assert deploy_ci_workflow(repo) is True
        assert target.read_text() == NotifyWorkflow(repo).render()

    def test_unusable_root_raises(self) -> None:
        """A non-directory root is a real failure, not a silent ``False`` no-op."""
        with pytest.raises(ValueError, match="no usable repo root"):
            deploy_ci_workflow(Path("/nonexistent"))

    def test_replaces_symlinked_target(self, tmp_path: Path) -> None:
        """A symlink at the workflow path is replaced, never followed.

        Mirrors ``write_enabled_marker``'s symlink guard: an untrusted checkout
        could commit ``biff-notify.yml`` as a symlink pointing outside the repo;
        enabling must overwrite the link with a real file, not clobber the
        link's target.  Both surfaces (`biff enable`, `/biff enable`) reach this
        path, so the guard closes a symlink-follow write.
        """
        repo = _make_repo(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("do not touch")

        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        link = workflows / _WORKFLOW_NAME
        link.symlink_to(outside)

        assert deploy_ci_workflow(repo) is True
        assert not link.is_symlink()
        assert link.read_text() == NotifyWorkflow(repo).render()
        # The symlink target is untouched.
        assert outside.read_text() == "do not touch"

    def test_symlinked_github_parent_cannot_escape(self, tmp_path: Path) -> None:
        """A committed symlinked ``.github`` must not redirect the write out of repo.

        ``mkdir(parents=True)`` follows a symlinked parent; the parent-dir guard
        replaces the symlinked ``.github`` with a real directory so the workflow
        lands inside the repo and never in the link's target.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / ".github").symlink_to(outside, target_is_directory=True)

        assert deploy_ci_workflow(repo) is True

        assert not (repo / ".github").is_symlink()
        assert (repo / ".github" / "workflows" / _WORKFLOW_NAME).is_file()
        # Nothing escaped into the symlink target.
        assert not (outside / "workflows").exists()

    def test_symlinked_workflows_parent_cannot_escape(self, tmp_path: Path) -> None:
        """Same guard one level deeper: a symlinked ``.github/workflows``."""
        repo = tmp_path / "repo"
        (repo / ".github").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / ".github" / "workflows").symlink_to(outside, target_is_directory=True)

        assert deploy_ci_workflow(repo) is True

        assert not (repo / ".github" / "workflows").is_symlink()
        assert (repo / ".github" / "workflows" / _WORKFLOW_NAME).is_file()
        assert not (outside / _WORKFLOW_NAME).exists()

    def test_symlinked_github_no_fail_open_when_target_resolves(
        self, tmp_path: Path
    ) -> None:
        """Fail-open regression: ``.github`` is a symlink AND the full nested
        path already resolves through it.

        An ``exists()`` short-circuit would see ``.github/workflows`` resolve
        and skip the guard, letting the write follow the link out of the repo.
        The guard must still replace the symlinked ``.github`` and keep the
        write inside.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside"
        (outside / "workflows").mkdir(parents=True)  # nested path pre-exists
        (repo / ".github").symlink_to(outside, target_is_directory=True)
        # repo/.github/workflows resolves (through the link) to an existing dir.
        assert (repo / ".github" / "workflows").is_dir()

        assert deploy_ci_workflow(repo) is True

        assert not (repo / ".github").is_symlink()
        assert (repo / ".github" / "workflows" / _WORKFLOW_NAME).is_file()
        # The workflow never landed in the symlink target.
        assert not (outside / "workflows" / _WORKFLOW_NAME).exists()


# ── remove_ci_workflow ─────────────────────────────────────────────


class TestRemoveCiWorkflow:
    """CI workflow removal."""

    def test_removes_workflow(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        deploy_ci_workflow(repo)
        assert remove_ci_workflow(repo) is True
        assert not (repo / ".github" / "workflows" / _WORKFLOW_NAME).exists()

    def test_no_workflow_returns_false(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        assert remove_ci_workflow(repo) is False

    def test_no_repo_root_returns_false(self) -> None:
        assert remove_ci_workflow(Path("/nonexistent")) is False

    def test_directory_at_workflow_path_left_alone(self, tmp_path: Path) -> None:
        """A directory (not ours) at the workflow path: report False, don't touch."""
        repo = tmp_path / "repo"
        wf = repo / ".github" / "workflows" / _WORKFLOW_NAME
        wf.mkdir(parents=True)  # a directory occupies the workflow path

        assert remove_ci_workflow(repo) is False
        assert wf.is_dir()  # left untouched, not misreported as removed

    def test_symlinked_workflow_removed_without_touching_target(
        self, tmp_path: Path
    ) -> None:
        """A committed symlinked workflow: remove the link, never its target."""
        repo = tmp_path / "repo"
        outside = tmp_path / "secret.txt"
        outside.write_text("keep me")
        wf = repo / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / _WORKFLOW_NAME).symlink_to(outside)

        assert remove_ci_workflow(repo) is True
        assert not (wf / _WORKFLOW_NAME).exists()  # the link is gone
        assert outside.read_text() == "keep me"  # target untouched


# ── check_ci_workflow ──────────────────────────────────────────────


class TestCheckCiWorkflow:
    """CI workflow presence and currency checks."""

    def test_current_returns_true(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        deploy_ci_workflow(repo)
        assert check_ci_workflow(repo) is True

    def test_missing_returns_false(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        assert check_ci_workflow(repo) is False

    def test_stale_returns_false(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        target = repo / ".github" / "workflows" / _WORKFLOW_NAME
        target.parent.mkdir(parents=True)
        target.write_text("old content")
        assert check_ci_workflow(repo) is False

    def test_no_repo_root_returns_false(self) -> None:
        assert check_ci_workflow(Path("/nonexistent")) is False

    def test_symlinked_workflow_not_current(self, tmp_path: Path) -> None:
        """A symlinked workflow path is not ours — not-current, never followed.

        The link's target matches the template, yet check must not follow the
        symlink to read it; it reports the workflow as not deployed/current.
        """
        repo = tmp_path / "repo"
        outside = tmp_path / "tpl.yml"
        outside.write_text(_template_content())
        wf = repo / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / _WORKFLOW_NAME).symlink_to(outside)

        assert check_ci_workflow(repo) is False
