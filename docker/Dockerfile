# Self-hosted biff relay image (biff-syr / DES-059).
#
# A thin wrapper around upstream nats-server: no biff-specific patch, no
# stream provisioning, no auth baked in. Pinned to an exact patch version
# by tag AND digest, never :latest -- an uncontrolled pull on
# `docker compose up` could jump multiple NATS minor versions in one
# upgrade, or a mutated tag could silently swap the image contents. See
# DES-059's Upgrade path section before bumping this tag across a major
# version (2.10 -> 2.11).
#
# 2.10.29 or later is required: versions >= 2.2.0 and < 2.10.27 are
# affected by GHSA-fhg8-qxh5-7q3w (unrestricted JetStream system APIs --
# account purge, server remove, stream move -- reachable by any client
# that can publish on `$JS.>`).
FROM nats:2.10.29-alpine3.22@sha256:b83efabe3e7def1e0a4a31ec6e078999bb17c80363f881df35edc70fcb6bb927

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Two config files baked in at build time -- entrypoint.sh picks between
# them, never writes either. See base.conf for why the loopback monitor
# bind needs a config file at all rather than nats-server's `-m` flag.
COPY base.conf base-with-user.conf /etc/nats/

# JetStream's file store (`-sd /data` in entrypoint.sh). Docker allocates
# an anonymous volume for any declared VOLUME, so even a bare `docker run`
# with no `-v` flag persists data across `docker stop`/`docker start`.
VOLUME /data

# Client port only. Monitoring (8222) binds to 127.0.0.1 inside the
# container in entrypoint.sh and is never published -- /varz, /connz, and
# /jsz have no auth of their own and would leak client IPs, subjects, and
# JetStream topology to anything that could reach the container on that
# port. Do not add `EXPOSE 8222` here.
EXPOSE 4222

# Reads the same loopback-bound /healthz endpoint the entrypoint enables.
# Runs inside the container, where 127.0.0.1 reaches the loopback bind.
# Uses the literal IPv4 address, not `localhost` -- this image's musl libc
# resolves `localhost` to ::1 first, and nats-server's monitor only binds
# the IPv4 loopback address, so `localhost` fails here even though the
# port is genuinely up (verified by actually running the container).
#
# `-q --spider` only -- this base image's wget is BusyBox wget, which has
# no `--no-verbose`/`--tries` (the GNU wget flags); passing them makes
# BusyBox wget exit non-zero on every run and the container never reports
# healthy (verified: `wget --help` inside this exact base image).
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD wget -q --spider http://127.0.0.1:8222/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
