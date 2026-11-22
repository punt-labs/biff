"""Unit coverage for the Typer hook failure boundary."""

from __future__ import annotations

import logging
from unittest.mock import patch

import click
import pytest

from biff.plugins.claude_code._hook_cli import HookAwareCli


def test_unexpected_hook_fault(caplog: pytest.LogCaptureFixture) -> None:
    """Hook faults retain one safe ERROR and propagate the original exception."""
    group = HookAwareCli()
    ctx = click.Context(group)
    ctx.invoked_subcommand = "hook"
    error = RuntimeError("secret-payload")
    with (
        patch("typer.core.TyperGroup.invoke", side_effect=error),
        pytest.raises(RuntimeError) as raised,
    ):
        group.invoke(ctx)
    assert raised.value is error
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert record.getMessage().startswith(
        "hook_cli_failed error=RuntimeError at "
        "biff.plugins.claude_code._hook_cli.invoke:"
    )
    assert record.getMessage().rsplit(":", 1)[1].isdigit()
    assert record.exc_info is None
    assert record.stack_info is None
    assert "secret-payload" not in caplog.text


@pytest.mark.parametrize(
    "error", [click.BadParameter("private"), click.exceptions.Exit(2), click.Abort()]
)
def test_expected_cli_failures_silent(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """Usage errors and intentional exits do not become hook fault records."""
    group = HookAwareCli()
    ctx = click.Context(group)
    ctx.invoked_subcommand = "hook"
    with (
        patch("typer.core.TyperGroup.invoke", side_effect=error),
        pytest.raises(type(error)),
    ):
        group.invoke(ctx)
    assert not caplog.records


def test_non_hook_fault_silent(caplog: pytest.LogCaptureFixture) -> None:
    """This boundary does not change diagnostics for unrelated commands."""
    group = HookAwareCli()
    ctx = click.Context(group)
    ctx.invoked_subcommand = "who"
    with (
        patch("typer.core.TyperGroup.invoke", side_effect=RuntimeError("private")),
        pytest.raises(RuntimeError),
    ):
        group.invoke(ctx)
    assert not caplog.records


def test_success_silent(caplog: pytest.LogCaptureFixture) -> None:
    """Successful invocations retain their return value without diagnostics."""
    group = HookAwareCli()
    ctx = click.Context(group)
    ctx.invoked_subcommand = "hook"
    with patch("typer.core.TyperGroup.invoke", return_value="result"):
        assert group.invoke(ctx) == "result"
    assert not caplog.records
