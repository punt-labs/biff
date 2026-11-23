"""Exercise the report-only design workflow with normal, empty and error output."""

from __future__ import annotations

import asyncio
import os
import textwrap
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/lint.yml"
STEP = "Design ratchet (shadow, report only)"


def test_full_report_includes_non_gating_design_scan() -> None:
    report = (
        (ROOT / "Makefile")
        .read_text()
        .split("\nreport:", 1)[1]
        .split("\nformat:", 1)[0]
    )
    assert '\t-uv run oo-ratchet design "$(SRC)"\n' in report


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("biff.session_id", "kernel"),
        ("biff.models", "wire"),
        ("biff.talk_types", "wire"),
        ("biff.relay", "engine"),
        ("biff.relay_select", "engine"),
        ("biff.nats_relay", "engine"),
        ("biff.talk_state", "engine"),
        ("biff._formatting", "render"),
        ("biff.formatting", "render"),
        ("biff.cli_context", "commands"),
        ("biff.commands.write", "commands"),
        ("biff.server.entrypoints", "server"),
        ("biff.server.tools._presence", "server"),
        ("biff.testing.caller", "server"),
        ("biff.plugins", "plugins"),
        ("biff.plugins.contract", "plugins"),
        ("biff.repo_toggle", "service"),
        ("biff.integration.vox", "service"),
        ("biff.plugins.claude_code._hook_runtime", "claude_code"),
        ("biff.plugins.claude_code.installer", "claude_code"),
        ("biff.__main__", "cli"),
        ("biff.cli_session_store", "cli"),
        ("biff.repl_display", "cli"),
        ("biff.undeclared", None),
    ],
)
def test_design_layer_boundaries(
    module: str,
    expected: str | None,  # None means the module matches no layer.
) -> None:
    layers = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["oo-ratchet"][
        "layers"
    ]
    relative = module.removeprefix("biff.")
    # Longest matching prefix wins, as in oo-ratchet's declared layers.
    matches = [
        (len(prefix.rstrip("*")), name)
        for name, spec in layers.items()
        for prefix in spec["prefixes"]
        if (
            relative.startswith(prefix.removesuffix("*"))
            if prefix.endswith("*")
            else relative == prefix or relative.startswith(f"{prefix}.")
        )
    ]
    actual = max(matches)[1] if matches else None
    assert actual == expected


def shadow_step() -> str:
    """Extract the actual reporting step, excluding YAML comments and later steps."""
    source = "\n".join(
        line
        for line in WORKFLOW.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    return source.split(f"      - name: {STEP}\n", 1)[1].split("\n      - ", 1)[0]


def test_shadow_is_a_pr_report_not_a_gate() -> None:
    source = WORKFLOW.read_text()
    assert source.index("- name: Ratchets (PR") < source.index(f"- name: {STEP}")
    step = shadow_step()
    assert "if: ${{ !cancelled() && github.event_name == 'pull_request' }}" in step
    assert "continue-on-error: true" in step
    assert "DESIGN_BASE: origin/${{ github.base_ref }}" in step
    assert "${{" not in step.split("run: |\n", 1)[1]
    check = next(
        line
        for line in (ROOT / "Makefile").read_text().splitlines()
        if line.startswith("check:")
    )
    assert "design-shadow" not in check
    assert "DESIGN_BASE ?= origin/main" in (ROOT / "Makefile").read_text()


@pytest.mark.parametrize(
    ("status", "output"),
    [(0, "neutral\n"), (0, ""), (2, "FAIL: invalid reference\n")],
)
@pytest.mark.parametrize(
    "base",
    [
        "origin/other-base",
        "origin/`printf${IFS}injected`",
        "origin/$(printf${IFS}injected)",
    ],
)
@pytest.mark.asyncio
async def test_shadow_publishes_success_empty_and_error_reports(
    tmp_path: Path, status: int, output: str, base: str
) -> None:
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" > "$ARGV_LOG"\n'
        'printf "%s" "$SHADOW_OUTPUT"\nexit "$SHADOW_STATUS"\n'
    )
    uv.chmod(0o755)
    summary = tmp_path / "summary.md"
    summary.write_text("Earlier step\n")
    argv_log = tmp_path / "argv.txt"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GITHUB_STEP_SUMMARY": str(summary),
        "DESIGN_BASE": base,
        "ARGV_LOG": str(argv_log),
        "SHADOW_OUTPUT": output,
        "SHADOW_STATUS": str(status),
    }
    script = textwrap.dedent(shadow_step().split("run: |\n", 1)[1])
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-e",
        "-o",
        "pipefail",
        "-c",
        script,
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    assert argv_log.read_text().strip() == (
        f"run oo-ratchet design src/biff/ --shadow-check {base}"
    )
    report = summary.read_text()
    assert report.startswith(f"Earlier step\n### {STEP}")
    assert report == "Earlier step\n" + stdout.decode()
    assert report.endswith("```\n")
    assert output in report


@pytest.mark.asyncio
async def test_bare_make_still_lists_targets() -> None:
    process = await asyncio.create_subprocess_exec(
        "make",
        "-n",
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    assert "oo-ratchet design" not in stdout.decode()
    assert "grep -E" in stdout.decode()
