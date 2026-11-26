# Roadmap

Planned work that has not shipped. Nothing on this page works yet. The
issue tracker is the source of truth for scope and status; shipped features
are described in the [README](../README.md) and recorded in the
[CHANGELOG](../CHANGELOG.md).

| Area | Planned work |
|------|--------------|
| **Security** | Per-person and per-agent keys, signed messages, end-to-end encrypted DMs and talk, per-sender grants. Specified in [trust-model.md](design/trust-model.md) (DES-094), delivered in four waves |
| **Daemon** | `biffd`, a per-user daemon that holds the relay connection and private keys. Specified in [biffd.md](design/biffd.md) (DES-095) |
| **Real-time** | `/pair` for session sharing with explicit consent |
| **Hosted relay** | Managed service with admin controls, audit logs and team isolation |

When an item ships, remove it from this table, document it in the README,
and add a CHANGELOG entry.
