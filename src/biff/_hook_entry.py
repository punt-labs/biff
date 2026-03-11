"""Lightweight hook entry point — bypasses full CLI import chain.

The ``biff`` CLI (``__main__.py``) imports the entire application:
nats, pydantic, fastmcp, typer, server, commands — ~4.7s of module
load before a single line of handler code runs.

This module is the entry point for ``biff-hook``, which imports only
``biff.hook`` (typer + stdlib) and dispatches directly to handler
functions.  Hook handlers use lazy imports from ``biff._stdlib``
(stdlib-only) and ``biff.markers`` (stdlib-only), avoiding the full
dependency tree entirely.

Import cost: ~0.2s (typer) vs ~4.7s (full CLI).
"""

# pyright: reportPrivateUsage=false
# This module is an internal companion to biff.hook — cross-module
# access to _-prefixed helpers is intentional, not a violation.

from __future__ import annotations

import sys
from collections.abc import Callable


def main() -> None:
    """Dispatch hook commands via sys.argv — no typer overhead for simple cases."""
    args = sys.argv[1:]
    if len(args) < 2:
        # Fall back to full typer CLI for help, errors, etc.
        from biff.hook import hook_app  # noqa: PLC0415

        hook_app()
        return

    layer, event = args[0], args[1]
    extra = args[2:]

    if layer == "claude-code":
        _dispatch_cc(event)
    elif layer == "git":
        _dispatch_git(event, extra)
    else:
        sys.exit(f"Unknown hook layer: {layer}")


# ── Claude Code dispatch ─────────────────────────────────────────────


def _cc_session_start() -> None:
    from biff.hook import _emit, _hook_context, handle_session_start  # noqa: PLC0415

    result = handle_session_start()
    _emit(_hook_context("SessionStart", result))


def _cc_session_resume() -> None:
    from biff.hook import _emit, _hook_context, handle_session_resume  # noqa: PLC0415

    result = handle_session_resume()
    _emit(_hook_context("SessionStart", result))


def _cc_session_end() -> None:
    from biff.hook import handle_session_end  # noqa: PLC0415

    handle_session_end()


def _cc_stop() -> None:
    from biff.hook import _emit, _hook_context, handle_stop  # noqa: PLC0415

    result = handle_stop()
    if result is not None:
        _emit(_hook_context("Stop", result))


def _cc_pre_tool_use() -> None:
    from biff.hook import _emit, _read_hook_input, handle_pre_tool_use  # noqa: PLC0415

    data = _read_hook_input()
    result = handle_pre_tool_use(data)
    if result is not None:
        _emit(result)


def _cc_post_bash() -> None:
    from biff.hook import (  # noqa: PLC0415
        _emit,
        _post_tool_use_context,
        _read_hook_input,
        check_plan_hint,
        check_wall_hint,
        handle_post_bash,
    )

    data = _read_hook_input()
    result = handle_post_bash(data) or check_plan_hint() or check_wall_hint()
    if result is not None:
        _emit(_post_tool_use_context(result))


def _cc_post_pr() -> None:
    from biff.hook import (  # noqa: PLC0415
        _emit,
        _post_tool_use_context,
        _read_hook_input,
        handle_post_pr,
    )

    data = _read_hook_input()
    result = handle_post_pr(data)
    if result is not None:
        _emit(_post_tool_use_context(result))


_CC_HANDLERS: dict[str, Callable[[], None]] = {
    "session-start": _cc_session_start,
    "session-resume": _cc_session_resume,
    "session-end": _cc_session_end,
    "stop": _cc_stop,
    "pre-tool-use": _cc_pre_tool_use,
    "post-bash": _cc_post_bash,
    "post-pr": _cc_post_pr,
    "pre-compact": lambda: None,  # Stub: full implementation in biff-sgl.
}


def _dispatch_cc(event: str) -> None:
    """Dispatch Claude Code lifecycle hooks."""
    handler = _CC_HANDLERS.get(event)
    if handler is None:
        sys.exit(f"Unknown claude-code event: {event}")

    # Gate: skip if biff not enabled in this repo.
    if event != "pre-compact":
        from biff.hook import _is_biff_enabled  # noqa: PLC0415

        if not _is_biff_enabled():
            return

    handler()


# ── Git dispatch ─────────────────────────────────────────────────────


def _dispatch_git(event: str, extra: list[str]) -> None:
    """Dispatch git lifecycle hooks."""
    from biff.hook import (  # noqa: PLC0415
        _is_biff_enabled,
        _read_pre_push_refs,
        handle_post_checkout,
        handle_post_commit,
        handle_pre_push,
    )

    if not _is_biff_enabled():
        return

    if event == "post-checkout":
        branch_flag = extra[2] if len(extra) >= 3 else ""
        handle_post_checkout(branch_flag)

    elif event == "post-commit":
        handle_post_commit()

    elif event == "pre-push":
        ref_lines = _read_pre_push_refs()
        handle_pre_push(ref_lines)

    else:
        sys.exit(f"Unknown git event: {event}")
