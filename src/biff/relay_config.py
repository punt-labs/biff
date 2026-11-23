"""Choose the relay from the config files and locate the local relay's files.

The relay is the local filesystem relay unless a config file names another:

- ``type: local`` (or no ``relay`` section): the local relay, no network.
- ``type: demo``: the shared public NATS relay, with its bundled credentials.
- ``type: nats`` (or just ``url:``): a NATS server at ``relay.url``.

Config layers (``config.yaml``, then ``config.local.yaml``) do not deep-merge
the relay: the highest layer that sets ``type`` or ``url`` decides which relay
it is, and only a layer at or above it can supply its credentials.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from biff._stdlib import biff_data_dir, get_repo_common_root
from biff.models import RelayAuth

DEMO_RELAY_URL = "tls://connect.ngs.global"
DEMO_CREDS_PATH = Path(str(importlib.resources.files("biff.data") / "demo.creds"))

RELAY_TYPES = ("local", "demo", "nats")
_AUTH_KEYS = ("token", "nkeys_seed", "user_credentials")
_RELAY_KEYS = frozenset(
    {"type", "url", "auth", "tls_handshake_first", "allow_env_override", *_AUTH_KEYS}
)
_BOOL_KEYS = ("tls_handshake_first", "allow_env_override")

type ConfigLayer = tuple[str, dict[str, object]]
"""A config file's name (for messages) and its parsed content."""


@dataclass(frozen=True)
class RelaySetting:
    """What the config layers say about the relay; a ``None`` URL is local."""

    url: str | None = None
    auth: RelayAuth | None = None
    tls_handshake_first: bool = False


# -- Reading the relay section ----------------------------------------------


def relay_section(raw: dict[str, object]) -> dict[str, object]:
    """Return the ``relay`` mapping of one config file, or ``{}`` when absent."""
    section: object = raw.get("relay")
    return cast("dict[str, object]", section) if isinstance(section, dict) else {}


def relay_declared(raw: dict[str, object]) -> bool:
    """Check whether one config file chooses a relay (sets ``type`` or ``url``)."""
    return bool(relay_section(raw).keys() & {"type", "url"})


def resolve_relay(layers: Sequence[ConfigLayer]) -> RelaySetting:
    """Resolve the relay from *layers*, lowest precedence first.

    Raises:
        SystemExit: A layer's relay section is malformed or contradicts
            itself, or the layers pair credentials with a relay that cannot
            take them (``local``, ``demo``), or credentials with no relay.
    """
    sections = [(name, _checked_section(name, raw)) for name, raw in layers]
    deciders = [i for i, (_, s) in enumerate(sections) if _chooses(s)]
    if not deciders:
        if any(_layer_auth(s) for _, s in sections):
            raise SystemExit("relay credentials are set but no relay type or url is.")
        return RelaySetting()
    above = sections[deciders[-1] :]
    url = _layer_url(above[0][1])
    kind = _layer_type(above[0][1])
    # Credentials and TLS mode come from the highest layer that sets them,
    # at or above the layer that chose the relay.
    auth = next((a for _, s in reversed(above) if (a := _layer_auth(s))), None)
    tls = False
    for _, s in above:
        tls = bool(s.get("tls_handshake_first", tls))
    if kind == "demo":
        url = DEMO_RELAY_URL
    if kind != "nats" and (auth or tls):
        what = "credentials" if auth else "tls_handshake_first"
        reason = (
            "the public demo relay must never receive your own credentials"
            if kind == "demo"
            else "the local relay takes none"
        )
        raise SystemExit(f"relay type: {kind} cannot use {what}; {reason}.")
    if url == DEMO_RELAY_URL and auth is None:
        auth = RelayAuth(user_credentials=str(DEMO_CREDS_PATH))
    return RelaySetting(url=url, auth=auth, tls_handshake_first=tls)


def _checked_section(name: str, raw: dict[str, object]) -> dict[str, object]:
    """Return one file's relay mapping after rejecting anything malformed."""
    if "relays" in raw:
        raise SystemExit(f"{name}: unknown key 'relays'; did you mean 'relay'?")
    section: object = raw.get("relay")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise SystemExit(
            f"{name}: 'relay' must be a mapping, not {type(section).__name__}."
        )
    mapping = cast("dict[str, object]", section)
    unknown = sorted(str(k) for k in mapping.keys() - _RELAY_KEYS)
    if unknown:
        raise SystemExit(
            f"{name}: unknown key(s) in relay section: {', '.join(unknown)}.\n"
            f"Allowed: {', '.join(sorted(_RELAY_KEYS))}."
        )
    url = mapping.get("url")
    if url is not None and not isinstance(url, str):
        raise SystemExit(f"{name}: relay.url must be a string, not {url!r}.")
    for key in _BOOL_KEYS:
        if key in mapping and not isinstance(mapping[key], bool):
            raise SystemExit(f"{name}: relay.{key} must be true or false.")
    _check_one_file(name, mapping)
    return mapping


def _check_one_file(name: str, section: dict[str, object]) -> None:
    """Reject a relay section whose own keys contradict its ``type``."""
    kind = _layer_type(section)
    if kind == "nats" and not _layer_url(section):
        raise SystemExit(
            f"{name}: relay type: nats requires relay.url "
            "(for example nats://host:4222)."
        )
    if "type" not in section or kind == "nats":
        return
    extras = [
        label
        for label, present in (
            ("url", bool(_layer_url(section))),
            ("auth", _layer_auth(section) is not None),
            ("tls_handshake_first", section.get("tls_handshake_first") is True),
        )
        if present
    ]
    if extras:
        raise SystemExit(
            f"{name}: relay type: {kind} cannot be combined with "
            f"{', '.join(extras)} in the same file."
        )


def _chooses(section: dict[str, object]) -> bool:
    """Check whether the section sets ``type`` or ``url``."""
    return section.get("type") is not None or isinstance(section.get("url"), str)


def _layer_url(section: dict[str, object]) -> str | None:
    url = section.get("url")
    return url if isinstance(url, str) and url else None


def _layer_type(section: dict[str, object]) -> str:
    """Return the relay type, which is ``nats`` when only a URL is given."""
    declared = section.get("type")
    if declared is None:
        return "nats" if _layer_url(section) else "local"
    if declared not in RELAY_TYPES:
        raise SystemExit(
            f"Unknown relay type {declared!r}.\n"
            f"Set type to one of: {', '.join(RELAY_TYPES)}."
        )
    return str(declared)


def _layer_auth(section: dict[str, object]) -> RelayAuth | None:
    """Return one file's relay auth: at most one of token, nkeys_seed, credentials.

    Flat keys and a nested ``auth:`` mapping are both accepted; a flat key
    wins, and the nested mapping may spell the credentials file ``credentials``.
    """
    nested: object = section.get("auth")
    auth = cast("dict[str, object]", nested) if isinstance(nested, dict) else {}
    flat = {"user_credentials": auth.get("credentials"), **auth, **section}
    present = {
        key: value
        for key in _AUTH_KEYS
        if isinstance(value := flat.get(key), str) and value
    }
    if len(present) > 1:
        raise SystemExit(
            f"Conflicting auth in relay section: {', '.join(sorted(present))}\n"
            "Set at most one of 'token', 'nkeys_seed', or 'user_credentials'."
        )
    return RelayAuth(**present) if present else None


# -- Where the local relay keeps its files ----------------------------------


@dataclass(frozen=True)
class RelayLocation:
    """A repo's local relay directory and why it is that directory."""

    path: Path
    reason: str


def local_relay_location(repo_root: Path) -> RelayLocation:
    """Return the local relay directory for the repo at *repo_root*.

    Every clone and worktree of one repo must land on the same directory, and
    two different repos never may.  A repo with a remote is keyed by that
    remote's normalized identity; one without is keyed by its common root.
    """
    url = _remote_url(repo_root)
    if url:
        key, reason = remote_relay_key(url)
    else:
        common = Path(get_repo_common_root(str(repo_root)) or repo_root).resolve()
        key = f"{_slug(common.name)}-{_digest(str(common))}"
        reason = f"no remote: keyed by the repo's path ({common})"
    return RelayLocation(biff_data_dir() / "relay" / key, reason)


def _remote_url(repo_root: Path) -> str | None:
    """Return the URL of ``origin``, or of the only remote when there is one.

    ``None`` when git is unavailable, there is no remote, or several remotes
    exist and none is ``origin``.  The URL is read as configured, before any
    ``insteadOf`` rewriting, so every clone of a remote agrees on it.
    """
    try:
        result = subprocess.run(
            ["git", "config", "--get-regexp", r"^remote\..*\.url$"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            cwd=repo_root,
        )
    except OSError:
        return None
    urls: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, url = line.partition(" ")
        urls[key.removeprefix("remote.").removesuffix(".url")] = url.strip()
    if "origin" in urls:
        return urls["origin"]
    return next(iter(urls.values())) if len(urls) == 1 else None


_PROXY = re.compile(
    r"https://git-manager\.devin\.ai/proxy/(?P<host>github\.com)/(?P<path>.+)"
)
_URL = re.compile(
    r"[a-z][a-z0-9+.-]*://(?:[^/@]*@)?(?P<host>[^/:@]+)(?::\d+)?/(?P<path>.+)", re.I
)
_SCP = re.compile(r"(?:[^@/]+@)?(?P<host>[^:/@]+):(?P<path>[^/].*)")


def remote_relay_key(url: str) -> tuple[str, str]:
    """Return the relay directory name for a git remote URL, and the reason.

    The key is built from the normalized identity (lowercase host, lowercase
    ``owner/repo`` without trailing ``/`` or ``.git``), so ssh and https forms
    of one remote agree and different identities never do.  A remote that is
    not ``host/owner/repo`` (a GitLab subgroup) is keyed by its normalized URL.
    """
    text = url.strip().rstrip("/").removesuffix(".git").rstrip("/").lower()
    match = _PROXY.fullmatch(text) or _URL.fullmatch(text) or _SCP.fullmatch(text)
    if match is None:
        return f"remote-{_digest(text)}", f"remote URL hash ({_digest(text)})"
    host, path = match["host"], match["path"].strip("/")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        where = f"{host}/{path}"
        return f"remote-{_digest(where)}", f"remote URL hash of {where}"
    identity = f"{host}/{path}"
    return (
        f"{_slug('__'.join(parts))}-{_digest(identity)}",
        f"remote slug of {identity}",
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _slug(text: str) -> str:
    """Return *text* lowercased with anything outside ``[a-z0-9_-]`` as ``-``."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-")
    return slug[:60] or "repo"
