"""A real interactive Claude Code in tmux, and a second biff user to write to it."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import nats

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugin"
SENDER = "bob"

_PANE_COLUMNS = 220
_PANE_ROWS = 60
_POLL_SECONDS = 0.5
_STARTUP_SECONDS = 60.0
_DELIVERY_SECONDS = 45.0

# Prompts Claude Code puts up in a directory it has not seen before, and for the
# development-channels flag, each with the keys that choose "yes".  The trust
# prompt highlights "No, exit", so it needs a Down first.
_PROMPTS: dict[str, tuple[str, ...]] = {
    "Yes, I trust this folder": ("Down", "Enter"),
    "I am using this for local development": ("Enter",),
    "Teach auto mode about your environment": ("Down", "Enter"),
}
_SESSION_READY = re.compile(r"Session ready: (\w+) \(tty\d+\)")
_SPACES = re.compile(r"\s+")
_ISOLATION = ("--setting-sources", "project,local")
_FORWARDED = ("PATH", "BIFF_USER", "PUNT_TELEMETRY")
# Ambient variables that would steer the rig: Claude Code's own marker (a nested
# claude refuses to start), and git's repository and config selection.
_AMBIENT_PREFIXES = ("BIFF_", "CLAUDECODE", "CLAUDE_CODE_", "GIT_")


def find_strays(biff_home: Path, since: float, markers: tuple[str, ...]) -> list[Path]:
    """Return files under *biff_home* written since *since* that name a marker.

    A marker is something only the rig knows (its temp root, its receiver's name);
    other sessions on the machine write under the same directory but never name
    these, so a hit is the rig's session reaching the operator's real biff home.
    """
    if not biff_home.is_dir():
        return []
    strays: list[Path] = []
    for path in biff_home.rglob("*"):
        try:
            if not path.is_file() or path.stat().st_mtime < since:
                continue
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if any(marker in text or marker in path.name for marker in markers):
            strays.append(path)
    return strays


@contextmanager
def channel_rig(launch: Launch, nats_url: str) -> Generator[ChannelRig]:
    """Yield a rig under one private temp root that is removed however it ends.

    The root is under ``/tmp``, outside the checkout: Claude Code imports every
    ``CLAUDE.md`` above its working directory, and the repo's own ``.tmp`` sits
    under this repository.  The root is made first, so a rig that fails part way
    through construction still has its directories removed.
    """
    root = Path(tempfile.mkdtemp(prefix="biff-channels-", dir="/tmp"))
    rig: ChannelRig | None = None
    try:
        rig = ChannelRig(root, launch, nats_url)
        yield rig
    finally:
        if rig is not None:
            rig.close()
        shutil.rmtree(root, ignore_errors=True)
        strays = rig.strays() if rig is not None else []
        if strays:
            note = f"the rig wrote to the real biff home: {strays}"
            failure = sys.exc_info()[1]
            if failure is None:
                raise AssertionError(note)
            # The scan runs however setup ended; the original failure stays the
            # error and carries the strays as a note.
            failure.add_note(note)


@dataclass(frozen=True, slots=True)
class Launch:
    """One way of starting the receiving Claude Code, as its argument list."""

    name: str
    channel_flag: str
    uses_plugin_dir: bool
    extra: tuple[str, ...] = ()
    is_isolated: bool = True

    def argv(self, plugin_dir: Path) -> list[str]:
        """Return the ``claude`` argument list that loads the plugin at *plugin_dir*.

        Project and local settings only: the operator's own installed plugins would
        otherwise start a second biff server in the same session.
        """
        argv = ["claude", *(_ISOLATION if self.is_isolated else ()), *self.extra]
        if self.uses_plugin_dir:
            argv += ["--plugin-dir", str(plugin_dir)]
        argv += ["--dangerously-load-development-channels", self.channel_flag]
        return argv


class PaneTimeoutError(AssertionError):
    """A needle never appeared in the pane; the message carries the pane."""


class ChannelRig:
    """A throwaway biff repo with a receiving Claude Code and a sending CLI.

    Everything biff writes goes under a private ``HOME``: a shim named ``biff``
    first on ``PATH`` sets it, so Claude Code keeps the operator's real home (and
    login) while the plugin's ``biff mcp``, its hooks and the sending CLI all
    share one relay directory and one ``biff.log`` that the operator never sees.
    """

    def __init__(self, root: Path, launch: Launch, nats_url: str) -> None:
        """Lay out the repo, the private home and the ``biff`` shim under *root*.

        Both users reach each other through the NATS server at *nats_url*; talk
        has no local relay, so the invite can only arrive over NATS.
        """
        self._nats_url = nats_url
        self._root = root
        self._real_biff_home = Path.home() / ".punt-labs" / "biff"
        self._started = time.time()
        # One name per rig: a name shared through the NATS server would carry the
        # last rig's unread mail into this one.
        self.receiver = f"alice{uuid.uuid4().hex[:6]}"
        self._launch = launch
        self._home = root / "home"
        self._repo = root / "repo"
        self._bin = root / "bin"
        self._plugin = root / "plugin"
        self._socket = f"biff-channels-{uuid.uuid4().hex[:8]}"
        self._session = "recv"
        for path in (self._home, self._repo, self._bin):
            path.mkdir(parents=True)
        self._write_shims()
        self._copy_plugin()
        self._init_repo()

    @property
    def log(self) -> Path:
        """The ``biff.log`` the receiving session's server writes."""
        return self._home / ".punt-labs" / "biff" / "logs" / "biff.log"

    @property
    def command(self) -> str:
        """The ``claude`` command line the receiver was started with."""
        return " ".join(self._launch.argv(self._plugin))

    def strays(self) -> list[Path]:
        """Return files the rig's session wrote into the operator's real biff home."""
        return find_strays(
            self._real_biff_home, self._started, (str(self._root), self.receiver)
        )

    def start_receiver(self) -> None:
        """Start Claude Code in a detached tmux session and wait for the handshake."""
        env = self._env(self.receiver)
        flags = [arg for key in _FORWARDED for arg in ("-e", f"{key}={env[key]}")]
        self._tmux(
            "new-session",
            "-d",
            "-s",
            self._session,
            "-x",
            str(_PANE_COLUMNS),
            "-y",
            str(_PANE_ROWS),
            "-c",
            str(self._repo),
            *flags,
            *self._launch.argv(self._plugin),
        )
        self._tmux("set-option", "-t", self._session, "remain-on-exit", "on")
        self._answer_prompts_until(self._handshake_logged, _STARTUP_SECONDS)

    def send_mail(self, text: str) -> None:
        """Send *text* from the second user to the receiver."""
        self._biff("write", self.receiver, text)

    def post_wall(self, text: str) -> None:
        """Post a wall from the second user."""
        self._biff("wall", text)

    def invite_to_talk(self, text: str) -> None:
        """Publish a talk invite from the second user to the receiver's session.

        No CLI verb sends an invite (``biff talk`` carries lines over the inbox),
        so this publishes the frame the REPL and the MCP ``talk`` tool publish.
        """
        asyncio.run(self._publish_invite(text))

    async def _publish_invite(self, text: str) -> None:
        key = self._receiver_key()
        frame = {
            "type": "invite",
            "from": SENDER,
            "from_tty": "tty1",
            "body": text,
            "from_key": f"{SENDER}:{uuid.uuid4().hex[:16]}",
            "to_key": key,
        }
        nc = await nats.connect(self._nats_url)  # pyright: ignore[reportUnknownMemberType]
        try:
            await nc.publish(f"biff.talk.notify.{key}", json.dumps(frame).encode())
            await nc.flush()
        finally:
            await nc.close()

    def wait_for_pane(self, pattern: str) -> str:
        """Return the pane once *pattern* matches it; raise with the pane on timeout."""
        wanted = re.compile(pattern)
        deadline = time.monotonic() + _DELIVERY_SECONDS
        while time.monotonic() < deadline:
            pane = self.pane()
            if wanted.search(_SPACES.sub(" ", pane)):
                return pane
            self._answer_prompts()
            time.sleep(_POLL_SECONDS)
        msg = (
            f"{pattern!r} never appeared in the pane of {self._launch.name} "
            f"({self.command}) within {_DELIVERY_SECONDS:.0f}s\n"
            f"--- pane ---\n{self.pane()}\n--- end pane ---\n"
            f"--- biff.log tail ---\n{self.log_text()[-2000:]}\n--- end log ---"
        )
        raise PaneTimeoutError(msg)

    def _receiver_key(self) -> str:
        """Return the receiver's session key, ``alice:<session id>``."""
        ready = _SESSION_READY.search(self.log_text())
        if ready is None:
            msg = f"no 'Session ready' line in biff.log:\n{self.log_text()}"
            raise PaneTimeoutError(msg)
        return f"{self.receiver}:{ready.group(1)}"

    def pane(self) -> str:
        """Return the receiver's pane with its scrollback, wrapped lines joined."""
        return self._tmux(
            "capture-pane", "-p", "-J", "-S", "-300", "-t", "recv:"
        ).stdout

    def log_text(self) -> str:
        """Return ``biff.log``, or an empty string before the server wrote it."""
        return self.log.read_text() if self.log.exists() else ""

    def close(self) -> None:
        """Kill the tmux server this rig started."""
        subprocess.run(  # noqa: S603
            ["tmux", "-L", self._socket, "kill-server"],  # noqa: S607
            check=False,
            capture_output=True,
        )

    @property
    def _shim(self) -> str:
        return str(self._bin / "biff")

    def _write_shims(self) -> None:
        """Write ``biff`` and ``biff-hook`` shims that run with the private home.

        Claude Code's hooks call ``biff-hook`` from ``PATH``; without its own shim
        that is the operator's installed one, with the operator's real ``HOME``.
        """
        for name in ("biff", "biff-hook"):
            target = Path(sys.executable).parent / name
            shim = self._bin / name
            shim.write_text(
                f'#!/bin/sh\nexport HOME="{self._home}"\nexec "{target}" "$@"\n'
            )
            shim.chmod(0o755)

    def _copy_plugin(self) -> None:
        """Copy ``plugin/``, pointing its server command at the checkout's ``biff``.

        Claude Code starts a plugin's server with its own idea of ``PATH``, so the
        shim cannot be relied on to be found; everything else in the manifest, the
        environment included, is the shipped one.
        """
        shutil.copytree(PLUGIN_DIR, self._plugin)
        # The nudge hook injects "You have N unread biff messages" and would tell
        # the model about mail even when no channel event arrives; silence it so
        # only the channel can make the mail show.
        nudge = self._plugin / "hooks" / "unread-nudge.sh"
        nudge.write_text("#!/bin/sh\nexit 0\n")
        manifest = self._plugin / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text())
        data["mcpServers"]["tty"]["command"] = self._shim
        manifest.write_text(json.dumps(data, indent=2) + "\n")

    def _init_repo(self) -> None:
        env = self._env(SENDER)
        hooks = self._repo / ".git" / "hooks"
        steps = (
            ["git", "init", "-q", "-b", "main"],
            # A local hooks path, so ``biff enable`` never reads or writes a global one.
            ["git", "config", "--local", "core.hooksPath", str(hooks)],
            ["git", "commit", "-q", "--allow-empty", "-m", "init"],
            [self._shim, "--user", SENDER, "enable"],
        )
        for argv in steps:
            subprocess.run(  # noqa: S603
                argv, cwd=self._repo, env=env, check=True, capture_output=True
            )
        config = self._repo / ".punt-labs" / "biff" / "config.local.yaml"
        config.write_text(f"relay:\n  url: {self._nats_url}\n")

    def _env(self, user: str) -> dict[str, str]:
        """Build the environment for *user* from nothing the operator's shell steers.

        Ambient ``BIFF_*`` (a relay URL would override the rig's), Claude Code's own
        markers and ``GIT_*`` are dropped, and git reads no global or system config.
        """
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(_AMBIENT_PREFIXES)
        }
        env["PATH"] = f"{self._bin}{os.pathsep}{env['PATH']}"
        env["BIFF_USER"] = user
        env["PUNT_TELEMETRY"] = "off"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "rig"
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "rig@example.invalid"
        return env

    def _biff(self, *args: str) -> None:
        subprocess.run(  # noqa: S603
            [self._shim, "--user", SENDER, *args],
            cwd=self._repo,
            env=self._env(SENDER),
            check=True,
            capture_output=True,
            timeout=60,
        )

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["tmux", "-L", self._socket, *args],  # noqa: S607
            # The first call starts the tmux server, whose environment every pane
            # inherits, so it gets the cleaned one, not the operator's shell's.
            env=self._env(self.receiver),
            check=True,
            capture_output=True,
            text=True,
        )

    def _handshake_logged(self) -> bool:
        return "MCP handshake" in self.log_text()

    def _answer_prompts_until(self, done: Callable[[], bool], seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if done():
                return
            self._answer_prompts()
            time.sleep(_POLL_SECONDS)
        msg = (
            f"{self._launch.name}: no MCP handshake in {seconds:.0f}s\n"
            f"--- pane ---\n{self.pane()}\n--- end pane ---"
        )
        raise PaneTimeoutError(msg)

    def _answer_prompts(self) -> None:
        pane = self._tmux("capture-pane", "-p", "-J", "-t", "recv:").stdout
        for prompt, keys in _PROMPTS.items():
            if prompt in pane:
                self._tmux("send-keys", "-t", "recv:", *keys)
                time.sleep(1)
