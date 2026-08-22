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
if [ -f /etc/nats/nats.conf ]; then
  conf=/etc/nats/base-with-user.conf
else
  conf=/etc/nats/base.conf
fi

exec nats-server -js -sd /data -c "$conf" "$@"
