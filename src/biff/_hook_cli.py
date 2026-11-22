"""Retain privacy-safe diagnostics for hooks dispatched through the full CLI."""

from __future__ import annotations

import logging

import click
import typer
from typer.core import TyperGroup

from biff._hook_foreground import ForegroundHook
from biff.hook import hook_app

logger = logging.getLogger(__name__)


class HookAwareCli(TyperGroup):
    """Build the CLI and retain unexpected hook failures without changing exits."""

    @classmethod
    def create_app(cls) -> typer.Typer:
        """Create the root CLI with its hook diagnostics boundary."""
        app = typer.Typer(
            help="Biff: the dog that barked when messages arrived.", cls=cls
        )
        app.add_typer(hook_app, name="hook")
        return app

    def invoke(self, ctx: click.Context) -> object:
        """Invoke a command, recording unexpected hook faults before re-raising."""
        try:
            return super().invoke(ctx)
        except (click.ClickException, click.exceptions.Exit, click.Abort):
            raise
        except Exception as exc:
            if ctx.invoked_subcommand == "hook":
                logger.error(
                    "hook_cli_failed error=%s at %s",
                    type(exc).__name__,
                    ForegroundHook.failure_location(exc),
                )
            raise
