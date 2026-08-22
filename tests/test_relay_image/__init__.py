"""Tier 3d: tests against the real ghcr.io/punt-labs/biff-relay Docker image.

Builds the image from this checkout's ``docker/`` directory (never a
published tag) and runs it as an actual container, so this tier exercises
whatever a PR changed in ``docker/`` -- ``entrypoint.sh``'s config-file
selection, the loopback monitor bind, and the auth-refusal guard PR #376
added. Neither of the other two NATS tiers reaches the image itself:
``tests/test_nats_e2e/`` spawns a bare ``nats-server`` binary with no Docker
and no auth, and ``tests/test_hosted_nats/`` points at Synadia Cloud's
managed relay. Requires Docker; gated behind the ``relay_image`` marker so
the default ``uv run pytest`` does not run it.
"""

from __future__ import annotations
