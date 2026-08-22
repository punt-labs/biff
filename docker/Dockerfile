# Self-hosted biff relay image (biff-syr / DES-059).
#
# A thin wrapper around upstream nats-server: no biff-specific patch, no
# stream provisioning, no auth baked in. Pinned to an exact patch version,
# never :latest -- an uncontrolled pull on `docker compose up` could jump
# multiple NATS minor versions in one upgrade. See DES-059's Upgrade path
# section before bumping this tag across a major version (2.10 -> 2.11).
FROM nats:2.10.24-alpine3.21

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
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:8222/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
