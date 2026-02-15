# Changelog

## Unreleased

### Added

- **Remote NATS connection support** — token, NKey seed, and credentials file
  authentication via `.biff` config `[relay]` section. TLS via `tls://` URL
  scheme. Automatic reconnection with disconnect/reconnect/error logging.
- **NATS E2E tests** — 9 end-to-end tests with two MCP servers sharing a
  NATS relay, covering presence, messaging, and lifecycle.
- **Relay lifecycle** — `close()` method on Relay protocol; server lifespan
  cleans up NATS connections on shutdown.
- **Relay selection** — `create_state()` dispatches NatsRelay vs LocalRelay
  based on `relay_url` in config.
