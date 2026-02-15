# Changelog

## Unreleased

### Added

- **Remote NATS connection support** — token, NKey seed, and credentials file
  authentication via `.biff` config `[relay]` section. TLS via `tls://` URL
  scheme. Automatic reconnection with disconnect/reconnect/error logging.
- **NATS E2E tests** — 9 end-to-end tests with two MCP servers sharing a
  NATS relay, covering presence, messaging, and lifecycle.
- **Hosted NATS E2E tests** — same 9 scenarios run against a real hosted NATS
  account (Synadia Cloud or self-hosted) via `BIFF_TEST_NATS_*` env vars.
  Weekly CI workflow with `workflow_dispatch` for manual triggers.
- **Relay lifecycle** — `close()` method on Relay protocol; server lifespan
  cleans up NATS connections on shutdown.
- **Relay selection** — `create_state()` dispatches NatsRelay vs LocalRelay
  based on `relay_url` in config.
