# Self-Hosted Relay

Biff talks to a NATS relay for presence (`/who`, `/finger`), messaging
(`/write`, `/wall`, `/talk`), and plan data. Biff ships with a shared demo
relay (`tls://connect.ngs.global`) so you can start immediately, but that
relay shares its namespace with every biff user. Repos are still isolated
(see [DES-007](../DESIGN.md#des-007-nats-namespace-scoping)), but you're
sharing connection capacity with everyone else on it.

`ghcr.io/punt-labs/biff-relay` is a Docker image that runs your own
`nats-server` with JetStream enabled, giving you full isolation with no
dependency on Synadia Cloud. It does not create biff's streams — your biff
client creates `biff-inbox`, `biff-sessions`, and `biff-wtmp` on first
connection (see
[DES-016](../DESIGN.md#des-016-shared-nats-streams--encryption-aware-design)).
The image's job is to run a correctly configured, durable `nats-server`.

The Docker image, `docker-compose.yml`, and Kubernetes manifests
referenced below live in [`docker/`](../docker/) in this repo.

**Does the image actually work?** Check
[`tests/test_relay_image/`](../tests/test_relay_image/) rather than
manually running `docker build`/`docker run`/`wget` yourself — it builds
the image fresh from this checkout's `docker/` directory and runs it as a
real container, asserting presence, message write/read, JetStream
persistence across a `docker stop`/`start` cycle, and `entrypoint.sh`'s
auth-refusal guard actually exiting 1. It's the same tier CI runs on every
push/PR (`relay-image` job in `.github/workflows/subprocess-tests.yml`);
run it locally with `uv run pytest -m nats_docker -v` (requires Docker).

This guide covers three ways to run the relay, because "self-hosted"
means different things depending on who's running it:

| You are... | Section |
|---|---|
| One developer, running biff across your own repos on one machine | [Individual](#individual-one-developer-one-machine) |
| A small team sharing an always-on relay over a network | [Small team](#small-team-shared-always-on-relay) |
| An organization piloting biff before deciding on the paid hosted tier | [Enterprise proof-of-value](#enterprise-proof-of-value) |

If none of these fit, or you'd rather not run infrastructure at all, see
[When to stop self-hosting](#when-to-stop-self-hosting) at the bottom.

---

## Individual: one developer, one machine

You work across several of your own repos and want a private relay with no
setup beyond one command.

### Install and run

```bash
docker run -d --name biff-relay -p 127.0.0.1:4222:4222 ghcr.io/punt-labs/biff-relay:X.Y.Z
```

Replace `X.Y.Z` with the version of biff you're running (`biff --version`) —
the image is tagged to match every `punt-biff` release.

No config file, no auth setup. The port is bound to `127.0.0.1`, so only
processes on this machine can reach it — that's what makes running with
no auth safe.

### Configure biff to use it

In each repo's `.punt-labs/biff/config.yaml`:

```yaml
relay:
  url: "nats://localhost:4222"
```

### Persistence

You didn't pass a `-v` flag, so Docker allocated an anonymous volume for
the container's `/data` directory, where JetStream stores your sessions,
messages, and wtmp log.

- `docker stop biff-relay` / `docker start biff-relay` — data survives.
  This is the normal restart path (reboots, Docker Desktop restarts).
- `docker rm biff-relay` — the volume itself isn't deleted, but nothing
  keeps track of it either. It becomes a dangling volume, and the next
  `docker run` in the [Install and run](#install-and-run) command above
  allocates a *new* anonymous volume, not the old one. In practice, your
  data is inaccessible unless you go find the orphaned volume by ID
  (`docker volume ls -f dangling=true`) and mount it explicitly.

If you want data to survive `docker rm` and come back automatically on
the next run, use the named-volume command from the
[team section](#persistence-2) below. Same image, one extra flag.

### Auth

None, by default. This is the same trust boundary as running `nats-server`
directly on your laptop: nothing else on your machine is untrusted, and
the `127.0.0.1` bind above means nothing off your machine can reach it
either. If you later want to reach this relay from another machine, don't
just republish the port — follow the [Small team](#auth) setup, which
adds a token before opening the port to the network.

### Upgrade

```bash
docker stop biff-relay && docker rm biff-relay
docker run -d --name biff-relay -p 127.0.0.1:4222:4222 ghcr.io/punt-labs/biff-relay:X.Y.Z
```

The new container gets a fresh anonymous volume, so this orphans the old
one, same as a plain `docker rm` above. To upgrade without losing state,
switch to a named volume first (team section).

### Teardown

```bash
docker rm -fv biff-relay
```

The `-v` flag removes the anonymous volume along with the container —
without it, `docker rm -f` leaves the volume dangling on disk (see
[Persistence](#persistence) above). Switch
`.punt-labs/biff/config.yaml`'s `relay.url` back to the demo relay, or to
another relay, and you're done.

---

## Small team: shared, always-on relay

Multiple people connect to one relay over a network. Losing the shared
volume here loses every team member's sessions, messages, and plan history
at once, not just one person's, so persistence and a baseline auth setup
are both required at this tier.

### Install and run

Unlike the individual tier, this relay is reachable over the network, so
set up a token before starting the container — there's no bare, no-auth
`docker run` command for this tier, on purpose.

Create `nats.conf` with a random token:

```text
authorization {
  token: "REPLACE_WITH_A_LONG_RANDOM_TOKEN"
}
```

Then `docker-compose.yml`:

```yaml
services:
  biff-relay:
    image: ghcr.io/punt-labs/biff-relay:X.Y.Z  # pin the version, never :latest
    container_name: biff-relay
    restart: unless-stopped
    ports:
      - "4222:4222"
    volumes:
      - biff-relay-data:/data
      - ./nats.conf:/etc/nats/nats.conf:ro
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:8222/healthz"]
      interval: 10s
      timeout: 3s
      retries: 3

volumes:
  biff-relay-data:
```

```bash
docker compose up -d
```

If you'd rather use a bare `docker run`, mount the same `nats.conf` at
the same path -- `entrypoint.sh` detects it and layers it in itself, so
don't pass your own `-c` flag: appending one here would replace
`entrypoint.sh`'s own config selection outright, including the loopback
monitor bind (see [DES-059](../DESIGN.md#des-059-self-hosted-relay-docker-image)).
`entrypoint.sh` also refuses to start if the mounted `nats.conf` doesn't
define `authorization`/`accounts`/`nkeys`, or still has the placeholder
token below unedited — a file existing at that path isn't the same thing
as auth being configured, and 4222 is published on all interfaces at this
tier:

```bash
docker run -d --name biff-relay \
  -p 4222:4222 \
  -v biff-relay-data:/data \
  -v "$(pwd)/nats.conf:/etc/nats/nats.conf:ro" \
  ghcr.io/punt-labs/biff-relay:X.Y.Z
```

### Configure biff to use it

Commit the URL in `.punt-labs/biff/config.yaml`, which is shared team
config:

```yaml
relay:
  url: "nats://your-relay-host:4222"
```

Put the token in `.punt-labs/biff/config.local.yaml` instead. This file
is per-user and gitignored, so it never reaches the shared repo:

```yaml
relay:
  auth:
    token: "REPLACE_WITH_A_LONG_RANDOM_TOKEN"
```

`nkeys_seed` and `credentials` (a path to a credentials file) are the
alternatives under `auth:` if you're using NATS nkeys or a credentials
file instead of a plain token — same rule either way: the secret goes in
`config.local.yaml`, never in `config.yaml`.

### Persistence

The named volume (`biff-relay-data`) survives `docker rm`, container
recreation, host reboot, and image upgrades — anything short of `docker
volume rm biff-relay-data` or losing the underlying disk. Back it up with
your normal host backup strategy if the team's history matters beyond
convenience; JetStream state isn't replicated anywhere by this image on
its own.

### Auth

A team relay is reachable over a network, so the trust model changes from
"trust everything on my laptop" to "trust everyone with the token." Set a
token before pointing this relay at anything beyond localhost.

`nats.conf`, mounted read-only at `/etc/nats/nats.conf` (referenced in the
compose file above):

```text
authorization {
  token: "REPLACE_WITH_A_LONG_RANDOM_TOKEN"
}
```

Restart the container after adding this file. `entrypoint.sh` picks up
`/etc/nats/nats.conf` if present and passes `base-with-user.conf` (baked
into the image, which `include`s your mounted `nats.conf`) to
`nats-server -c` -- don't pass your own `-c` flag (see the bare `docker
run` alternative above), and don't leave the file empty or with the
placeholder token unedited: `entrypoint.sh` checks for an actual
`authorization`/`accounts`/`nkeys` block and refuses to start otherwise.

### Health check

The container's `HEALTHCHECK` and `docker compose ps` both read from NATS's
own monitoring endpoint, `http://127.0.0.1:8222/healthz` -- the literal
IPv4 address, not `localhost`: this image's musl libc resolves `localhost`
to `::1` first, and nats-server's monitor only binds the IPv4 loopback
address, so `localhost` fails here even though the port is genuinely up.
`docker inspect biff-relay --format '{{.State.Health.Status}}'` shows
`healthy`/`unhealthy` directly.

### Upgrade without losing data

State lives entirely in the named volume, so an upgrade is a container
swap:

```bash
# edit docker-compose.yml's image: tag to the new version, then:
docker compose pull
docker compose up -d
```

The new container mounts the same volume and the same JetStream file
store; biff's client reconnects and finds its streams already there.

- Pin an exact version in `docker-compose.yml`. Never `:latest` — an
  uncontrolled pull can jump multiple NATS versions on the next `docker
  compose up`.
- Read the [nats-server release notes](https://github.com/nats-io/nats-server/releases)
  before crossing a major version (2.10 → 2.11) for JetStream file-format
  changes. Minor and patch upgrades within a major version are
  forward-compatible with existing data.

Downgrading the image tag isn't supported — JetStream's on-disk format
isn't guaranteed backward-compatible. If you need to roll back, restore the
volume from a pre-upgrade backup along with the image tag.

### Teardown

```bash
docker compose down          # stops and removes the container, keeps the volume
docker volume rm biff-relay-data   # only once you're sure -- this deletes team history
```

---

## Enterprise proof-of-value

You're evaluating biff at a scale beyond one small team, deciding whether
to keep self-hosting or move to the paid hosted tier
([biff-bv5](https://github.com/punt-labs/biff/issues?q=biff-bv5)). This
tier needs a real TLS/auth setup, observability, and, since this is a
pilot rather than a production commitment, a clean way to walk away.

A single Docker image and `docker-compose.yml` aren't enough here. An org
piloting biff "at scale" is usually testing on infrastructure that
resembles what it would actually run, and for most orgs at this size
that's Kubernetes. See
[DES-059](../DESIGN.md#des-059-self-hosted-relay-docker-image)
for the full reasoning. This section ships a second artifact — a minimal
Kubernetes manifest set — on top of the same `ghcr.io/punt-labs/biff-relay`
image the other two tiers use.

### Install and run

Create the two Secrets the `Deployment` mounts, copying from
[`nats.conf.enterprise.example`](../docker/nats.conf.enterprise.example)
rather than hand-transcribing the block below, then apply the manifests
(`Deployment`, `PersistentVolumeClaim`, `Service`, optional
`prometheus-nats-exporter` sidecar) against your own cluster. `nats.conf`
holds the authorization token/nkeys, so it's a Secret, not a ConfigMap —
ConfigMaps aren't Kubernetes secret objects (broader default RBAC read
access, plaintext in `kubectl get cm -o yaml`, casual inclusion in
backups):

```bash
kubectl create secret generic biff-relay-nats-conf \
  --from-file=nats.conf=nats.conf.enterprise.example
kubectl create secret tls biff-relay-tls --cert=tls.crt --key=tls.key
kubectl apply -f k8s/biff-relay-pvc.yaml
kubectl apply -f k8s/biff-relay-deployment.yaml
kubectl apply -f k8s/biff-relay-service.yaml
```

Swap the PVC's `storageClassName` for whatever storage class your cluster
provides, and swap the `Service` type (`ClusterIP`, `LoadBalancer`, or an
`Ingress` in front) for however your org exposes internal services.

### Configure biff to use it

```yaml
relay:
  url: "tls://biff-relay.your-namespace.svc.cluster.local:4222"
```

Or the external DNS name / LoadBalancer IP if biff clients run outside the
cluster.

### Persistence

The `PersistentVolumeClaim` is the only persistence mechanism at this
tier — there's no single Docker host to hold a named volume. Your
cluster's storage class and its reclaim policy (`Retain` vs. `Delete`)
govern what happens to the underlying volume on teardown. Check that
policy before assuming a namespace delete keeps or discards your data.

### Auth: TLS and credentials required

Enterprise POV traffic usually crosses network boundaries you don't fully
control — cluster networking, possibly a shared cluster with other
tenants. The manifest set's `nats.conf` template includes both:

```text
tls {
  cert_file: "/etc/nats/certs/tls.crt"
  key_file:  "/etc/nats/certs/tls.key"
}
authorization {
  token: "REPLACE_WITH_A_LONG_RANDOM_TOKEN"
  # or a full users/permissions block for per-caller scoping
}
```

Mount your cluster's TLS secret (`kubectl create secret tls ...` or a
cert-manager-issued secret) at `/etc/nats/certs`. This isn't optional at
this tier, unlike the team tier where a token is the minimum bar.

### Health check

`readinessProbe` and `livenessProbe` in the manifest hit the same
`/healthz` endpoint the Docker `HEALTHCHECK` uses, but as `exec` probes,
not `httpGet`. The monitoring port binds to `127.0.0.1` inside the
container (see [DES-059](../DESIGN.md#des-059-self-hosted-relay-docker-image)),
so a `httpGet` probe — which connects from the kubelet, outside the
container's network namespace — can't reach it. `exec` runs the request
inside that namespace instead:

```yaml
readinessProbe:
  exec:
    command: ["wget", "-q", "--spider", "http://127.0.0.1:8222/healthz"]
  initialDelaySeconds: 5
  periodSeconds: 10
livenessProbe:
  exec:
    command: ["wget", "-q", "--spider", "http://127.0.0.1:8222/healthz"]
  initialDelaySeconds: 10
  periodSeconds: 15
```

### Observability

`biff-relay-deployment.yaml` includes an optional `prometheus-nats-exporter`
sidecar, commented out by default -- uncomment it to scrape the same
`:8222` monitoring port. Point your existing Prometheus at the sidecar's
metrics port — it's upstream NATS's own exporter, no biff-specific format
to learn.

### Upgrade

Same principle as the team tier (state lives outside the container, in
the PVC), but the mechanism is a rolling update:

```bash
kubectl set image deployment/biff-relay biff-relay=ghcr.io/punt-labs/biff-relay:X.Y.Z
kubectl rollout status deployment/biff-relay
```

Same version-pinning and release-notes rules from the team tier apply.

### Teardown

```bash
kubectl delete -f k8s/biff-relay-deployment.yaml
kubectl delete -f k8s/biff-relay-service.yaml
kubectl delete -f k8s/biff-relay-pvc.yaml   # deletes the PVC; underlying volume fate depends on your storage class's reclaim policy
```

No other biff state depends on this relay having existed. Point
`.punt-labs/biff/config.yaml`'s `relay.url` back at the demo relay, or
delete the file, and the pilot is over.

### If the POV succeeds

Two paths from here:

- **Move to the paid hosted tier**
  ([biff-bv5](https://github.com/punt-labs/biff/issues?q=biff-bv5)) —
  managed relay, team admin controls, audit logs, end-to-end encryption,
  billing. Per-repo credential provisioning via GitHub identity
  ([biff-3pn](https://github.com/punt-labs/biff/issues?q=biff-3pn)) is
  specific to this path. Self-hosted teams, including this POV tier,
  always own their own NATS auth material, the same way you'd own your
  own Postgres or Redis credentials.
- **Keep self-hosting in production** — adopt the upstream
  [`nats-io/k8s`](https://github.com/nats-io/k8s) Helm chart directly
  (clustering, mTLS between NATS nodes, operator-managed accounts)
  instead of this guide's minimal manifests. This project doesn't
  maintain a competing Helm chart; see
  [DES-059](../DESIGN.md#des-059-self-hosted-relay-docker-image)'s
  Rejected section for why.

---

## When to stop self-hosting

All three tiers above assume you want to run `nats-server` yourself. If
that stops being worth it — team growth, compliance requirements, or you'd
simply rather not operate infrastructure — the paid hosted tier (biff-bv5)
is a drop-in replacement: same `relay.url` field in
`.punt-labs/biff/config.yaml`, pointed at a managed endpoint instead of
your own container or cluster. Only who runs the relay changes.
