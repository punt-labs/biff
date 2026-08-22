#!/bin/sh
# Entrypoint for ghcr.io/punt-labs/biff-relay (DES-059).
#
# Runs a correctly configured, durable nats-server. Nothing here
# provisions biff's streams (biff-inbox, biff-sessions, biff-wtmp) --
# per DES-016, biff's own NatsRelay client creates those idempotently on
# first connection, and a second writer here would race it. This
# entrypoint's only job is process invocation; it never writes to
# nats.conf (the operator's own file, bind-mounted read-only) and never
# writes any file of its own -- it only chooses between two config files
# baked into the image at build time (base.conf, base-with-user.conf).
#
# JetStream is on, with the file store at the declared VOLUME. Monitoring
# binds to loopback only -- see base.conf for why that needs a config
# file rather than the `-m` CLI flag.
set -eu

# A mounted nats.conf is only trustworthy if it actually configures auth --
# a file existing (empty, TLS-only, or the unedited example) is not the
# same thing as auth being configured, and 4222 is published on all
# interfaces in the team/enterprise compose and k8s manifests. Refuse to
# start rather than silently run an unauthenticated, network-reachable
# relay.
if [ -f /etc/nats/nats.conf ]; then
  if ! grep -qE '^\s*(authorization|accounts|nkeys)\b' /etc/nats/nats.conf; then
    echo "entrypoint: /etc/nats/nats.conf does not define authorization," \
      "accounts, or nkeys -- refusing to start an unauthenticated," \
      "network-reachable relay. See docs/self-hosted-relay.md." >&2
    exit 1
  fi
  if grep -q 'REPLACE_WITH_A_LONG_RANDOM_TOKEN' /etc/nats/nats.conf; then
    echo "entrypoint: /etc/nats/nats.conf still has the placeholder token" \
      "from nats.conf.team.example -- replace it with a long random" \
      "value before starting. See docs/self-hosted-relay.md." >&2
    exit 1
  fi
  conf=/etc/nats/base-with-user.conf
else
  conf=/etc/nats/base.conf
fi

# No forwarded args: nats-server CLI flags win over the config file, so a
# trailing operator-supplied `-m`/`--http_port`/`-c`/`--config` could
# rebind or replace the baked config and break the loopback monitor-bind
# invariant this image and DES-059 depend on. This entrypoint's contract
# is "run the baked config," not "run an arbitrary nats-server command
# line" -- an operator who needs different nats-server behavior mounts a
# different nats.conf, not extra CLI args.
if [ "$#" -gt 0 ]; then
  echo "entrypoint: extra arguments are not supported --" \
    "configure via a mounted nats.conf instead." >&2
  exit 1
fi

exec nats-server -js -sd /data -c "$conf"
