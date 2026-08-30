# Requirements: biffd, Message Authenticity, and Multi-Host Support

**Status:** Draft for review — not yet a design
**Date:** 2026-08-29
**Provenance:** Operator rulings and research session of 2026-08-29
(opencode capability research, Block Buzz trust-model research).
**Relationship to DESIGN.md:** This document states *what* the work must
achieve. Design missions produce the *how* as DES entries and Z
specifications. Where this document names a mechanism (Schnorr
signatures, `session.prompt`), that mechanism is an operator ruling or a
verified host capability — a constraint, not a design choice left open.

---

## 1. Overview

Biff today is a single-host product. Its architecture is shaped by one
Claude Code limitation: MCP has no push mechanism, so biff's largest
subsystem is a three-mechanism polling workaround (DESIGN.md DES-036,
"The Constraint"). Its trust model is shaped by transport authentication
alone: NATS credentials say a connection is legitimate, but nothing
proves *who authored a message*.

Three forces now converge on one restructuring:

1. **Architecture.** The MCP server process owns too much: relay
   connection, inbox state, notification logic, and the Claude Code
   display pipeline are one process. Vox already proved the better
   shape — a long-lived daemon owning state and engine, with thin
   per-host surfaces.
2. **Trust.** Remote prompting — a teammate's message steering your
   session directly — is the product vision (prfaq), but it is only
   safe when authorship is cryptographically provable. Block's Buzz
   demonstrates the model: identity is a keypair, every message is
   signed, verification replaces address-trust.
3. **A second host.** opencode natively provides what biff has waited
   for Claude Code to ship (DESIGN.md "Future: Channels", biff-5esx):
   a plugin can push a prompt into a running session
   (`session.prompt()`), show real notifications (`tui.showToast()`),
   and inject context without a model turn (`noReply: true`). On
   opencode, biff works as designed instead of as worked-around.

The work is one program in five modules. Each module has independent
value and its own acceptance bar; later modules depend on earlier ones.

| Module | Name | Depends on |
|--------|------|------------|
| M1 | biffd daemon extraction | — |
| M2 | Message authenticity | M1 |
| M3 | Permissions and delivery policy | M2 |
| M4 | Claude Code adapter | M1 (M3 for trust display) |
| M5 | opencode adapter | M1, M3 |

Requirement keywords MUST, SHOULD, and MAY follow RFC 2119. Every
requirement carries an ID for traceability into design docs, beads, and
mission contracts.

---

## 2. M1 — biffd: daemon extraction

The end state mirrors vox: one daemon owns the engine; every host
surface is a thin client.

### Requirements

- **R-D.1** A single long-lived per-user daemon (`biffd`) MUST own: the
  NATS relay connection and reconnect lifecycle, inbox and unread
  state, presence and `.plan` state, talk session state, and
  notification fan-out to connected clients.
- **R-D.2** All host surfaces — the Claude Code MCP server, the `biff`
  CLI, the opencode plugin, and hook scripts — MUST be thin clients of
  biffd over a local IPC boundary. No surface may open its own relay
  connection.
- **R-D.3** biffd MUST support multiple concurrent clients (several
  Claude Code sessions, an opencode session, and CLI invocations at
  once) with per-session identity, matching today's multi-session
  semantics (DESIGN.md session-model).
- **R-D.4** Host adapters MUST differ only in *delivery* — how a
  notification reaches the human and the model. Message semantics,
  trust classification, inbox behavior, and POP read-once semantics
  MUST be identical across hosts and MUST live in biffd, not in
  adapters.
- **R-D.5** biffd MUST expose a notification subscription to clients
  (push over the local IPC), so that an adapter with a push-capable
  host never polls biffd.
- **R-D.6** The existing connection-lifecycle Z specifications
  (`nats-relay.tex`, `session-model.tex`) MUST be extended or
  re-homed to model biffd's lifecycle before or with the
  implementation, per the repo's formal-methods policy. The
  daemon/client split introduces new states (client attached/detached,
  daemon absent) that MUST be modeled.
- **R-D.7** Daemon absence MUST degrade cleanly: a client invoked with
  no running daemon MUST either start it or fail with an actionable
  message — never hang, never silently no-op.
- **R-D.8** The migration MUST be incremental: the existing
  single-process MCP server keeps working until the Claude Code
  adapter (M4) reaches parity. No flag-day cutover.

### Acceptance

M1 is done when the Claude Code plugin and CLI run entirely through
biffd with behavior parity (all existing tier 1–4 tests pass against
the daemon-backed path), and a second concurrent client can attach
without disturbing the first.

---

## 3. M2 — Message authenticity (Buzz-style)

Adopt the authenticity portion of Block's Buzz trust model: identity is
a keypair, every message is signed, verification replaces address-trust.
The permissions portion is deliberately NOT Buzz's — see M3.

### Requirements

- **R-A.1** Every biff participant — human or agent — MUST be
  identified by a keypair. Signature verification against a public key,
  not any sender-claimed handle or transport credential, MUST be the
  sole basis for authorship claims.
- **R-A.2** Every biff message (write, wall, talk, steering request)
  MUST be signed by its author's key at the payload level. The
  signature MUST travel end-to-end: the relay is untrusted
  infrastructure and MUST NOT be able to forge, alter, or replay a
  message undetected. Signed payloads MUST include replay protection
  (timestamp and nonce within the signed body).
- **R-A.3** Signing MUST use secp256k1/Schnorr in the Nostr event
  style (operator ruling, per Buzz). Whether payloads are literal
  Nostr events carried over NATS, or a Nostr-shaped envelope of our
  own, is a design decision (see SP-3) — but the signature scheme is
  fixed.
- **R-A.4** Agent keys MUST be distinct from human keys. An agent MUST
  NOT hold or use its owner's private key. Authorship is always the
  agent's own key.
- **R-A.5** A human owner MUST be able to issue a signed, narrowly
  scoped delegation naming an agent's public key. Verifiers MUST be
  able to establish two separate facts about a message: which key
  authored it, and which owner (if any) authorized that key —
  "authorization does not erase authorship."
- **R-A.6** Revocation MUST be surgical: revoking an agent key MUST NOT
  invalidate or rotate the owner's human identity. Verifiers MUST
  reject messages signed by a revoked key from the revocation point
  forward; handling of messages signed before revocation is a design
  decision that MUST be made explicitly and logged.
- **R-A.7** The ethos registry MUST be the system of record for
  participant public keys and delegation records. Key material
  handling (generation, storage, rotation) MUST be specified in the
  design with `djb` as evaluator; the threat model MUST be reviewed
  by `bcs` (per biff's worker table).
- **R-A.8** Unsigned messages MUST remain deliverable (classified
  `unverified`, see M3) during and after migration. Authenticity is
  additive; it does not break plain messaging.
- **R-A.9** Beadle-email remains on GPG (operator ruling: the email
  client ecosystem is GPG-native). Biff and beadle therefore run two
  signing stacks by design. Cross-stack unification is a non-goal of
  this program (§8); the door is left open for a future decision.
- **R-A.10** Biff MUST support message forwarding with preserved
  provenance: a forward wraps the original signed payload *intact*
  inside a new payload signed by the forwarder. A recipient MUST be
  able to verify both facts independently — who authored the inner
  message and who forwarded it — from the message alone. Forwards of
  forwards MUST verify as a chain, with a design-chosen maximum depth.
  Tampering with the inner payload MUST invalidate the whole envelope.
  (The Nostr repost pattern is a proven prior art for nested signed
  events; SP-3 evaluates it on correctness and interop grounds, and
  whatever envelope design is chosen meets these requirements in
  full.)
- **R-A.11** Forwarded provenance MUST distinguish two origin classes,
  and verifiers and UIs MUST NOT conflate them:
  - **originator-signed** — the inner payload carries the original
    author's own signature; the recipient has cryptographic proof of
    authorship.
  - **attested** — the forwarder relays content whose origin was not
    signed (e.g. an operator's in-session utterance quoted by their
    agent); the forwarder's signature plus its delegation chain attest
    the claim "owner said this," but only the forwarder is proven.

  An agent forwarding its owner's unsigned words MUST mark the payload
  attested; a surface through which the originator can sign at capture
  time MAY upgrade it to originator-signed.

### Acceptance

M2 is done when every message sent by an upgraded client carries a
verifiable signature, biffd classifies inbound messages by
verification outcome, forged and replayed payloads are rejected in
tests, key/delegation resolution works from the ethos registry, and a
forwarded message verifies both signatures — including a test where a
tampered inner payload invalidates the envelope and a test
distinguishing attested from originator-signed origins.

---

## 4. M3 — Permissions and delivery policy (beadle-style)

Authenticity (M2) answers "who wrote this, provably?" This module
answers "what may this message do to my session?" The model is
beadle-email's four trust levels, driven by M2's verification outcome.

### Requirements

- **R-P.1** Every inbound message MUST carry exactly one trust level,
  computed by biffd from signature verification and delegation-chain
  resolution:

  | Level | Meaning |
  |-------|---------|
  | `trusted` | Valid signature; key resolves to an org identity or carries a valid delegation chain to an owner the recipient trusts |
  | `verified` | Valid signature; key is known but outside the recipient's trust set |
  | `untrusted` | Signature present but verification failed |
  | `unverified` | No signature |

- **R-P.2** Trust level MUST determine delivery privilege. The default
  policy:

  | Level | Human channel | Model channel |
  |-------|---------------|---------------|
  | `trusted` | notify | full prompt injection — message content enters the model's prompt (operator ruling: trusted prompt injection is a feature, not a risk to design away) |
  | `verified` | notify | fixed-format wake line only ("message from @X — call read_messages"); content stays behind the pull |
  | `unverified` | notify | wake line only |
  | `untrusted` | warning | nothing injected; flagged on next read |

- **R-P.3** The delivery policy MUST be recipient-configurable per
  level (a user MAY tighten `trusted` to wake-line-only, or widen
  `verified`), but the *defaults* above MUST ship as stated, and no
  configuration may grant injection to `untrusted` or `unverified`.
- **R-P.4** A failed signature (`untrusted`) MUST be presented as a
  distinct, alarming state — visually and in tool output — not blended
  with `unverified`. A bad signature is a stronger signal than no
  signature.
- **R-P.5** Only the *wake/delivery* decision is push. Message reading,
  reply, and history MUST remain pull (tool calls), preserving biff's
  "purposeful, not chatty" model. For `trusted` full injection, the
  injected prompt MUST identify the author handle and trust level
  inline so the model and the transcript both show provenance.
- **R-P.6** The trust classification and delivery policy MUST live in
  biffd and be host-independent (R-D.4). Adapters enforce nothing; they
  render biffd's decision with whatever delivery mechanics the host
  offers.
- **R-P.7** The policy table and trust-level state machine MUST be
  modeled in a Z specification with zero-GAP audit coverage before the
  injection path ships (this is a security-critical closed
  classification — exactly the class the invariant-completeness
  reviewer exists for).
- **R-P.8** Forwarded messages (R-A.10) MUST take the delivery
  privilege of the *lowest* trust level anywhere in the chain — inner
  author, every intermediate forwarder, and origin class. An attested
  origin (R-A.11) caps the chain at the forwarder's own level, since
  only the forwarder is proven. A failed signature anywhere in the
  chain classifies the whole message `untrusted`. Provenance MUST be
  displayed through the chain (e.g. `@jim via @claude`), with the
  origin class visible.

### Acceptance

M3 is done when biffd classifies and routes messages per the table,
configuration overrides work within R-P.3 bounds, and the Z spec for
the classification passes check, model-check, and audit with zero GAP.

---

## 5. M4 — Claude Code adapter

Biff on Claude Code keeps working, now as a thin biffd client. Claude
Code remains delivery-degraded until it ships a Channels-class API.

### Requirements

- **R-CC.1** The Claude Code MCP server MUST become a biffd client with
  full behavior parity: dynamic tool descriptions, `tools/list_changed`
  firing, status-line unread file, two-layer polling, and the display
  pipeline (DES-036 and related) all preserved.
- **R-CC.2** Trust levels (M3) MUST surface in Claude Code even though
  full injection cannot: the wake line arrives via the existing
  description-mutation channel; `trusted` content still requires the
  model to pull via `read_messages`. The degraded-delivery difference
  versus opencode MUST be documented in the user-facing docs.
- **R-CC.3** When Claude Code ships a stable push/Channels API, the
  adapter MUST be able to adopt it as a delivery-mechanics change only
  — no biffd or policy changes (this is the payoff of R-D.4/R-P.6;
  biff-5esx remains the tracking bead).
- **R-CC.4** The existing plugin release channels (marketplace +
  PyPI) MUST continue to ship together per the current release
  process.

### Acceptance

M4 is done when the shipped Claude Code plugin runs against biffd with
all existing tests green and no user-visible regression.

---

## 6. M5 — opencode adapter

The first push-capable host. Biff on opencode is the reference
implementation of biff's intended notification model.

### Requirements

- **R-OC.1** Biff MUST ship an opencode plugin (JS/TS, distributed as
  an npm package) that connects to biffd and implements delivery:
  `tui.showToast()` for the human channel; `session.prompt()` for the
  model channel per the M3 policy (full content for `trusted`, wake
  line for `verified`/`unverified`).
- **R-OC.2** The plugin MUST NOT poll. Delivery MUST be event-driven
  end-to-end: relay → biffd → plugin subscription → host call.
- **R-OC.3** The plugin MUST track session activity via opencode events
  and apply an explicit busy-session policy (queue vs. inject-now)
  chosen in design after SP-1 resolves `session.prompt` queueing
  semantics.
- **R-OC.4** The biff tool surface (read, write, **forward**, wall,
  talk, plan, finger, who, tty, mesg, poll-status) MUST be available
  as native opencode tools backed by biffd, with argument and output
  parity with the MCP tools wherever the host allows. `forward` is a
  new verb introduced by this program (R-A.10/R-A.11) and MUST ship on
  every host surface — MCP tool, CLI, and opencode tool — in the same
  release, taking a message reference (from read/talk) or inline
  content (attested origin) plus a recipient.
- **R-OC.5** Slash-command equivalents of the plugin's commands MUST be
  provided in opencode's command format, deposited by `biff enable`
  (or `biff install`) when it detects an opencode host — mirroring how
  enablement deposits Claude Code surfaces today.
- **R-OC.6** Session-start context (the agent guide content that
  Claude Code receives via hooks and `@`-imports) MUST reach opencode
  sessions. `session.prompt` with `noReply: true` is the candidate
  mechanism, gated on SP-2.
- **R-OC.7** The npm package becomes a third release channel. It MUST
  version-lock to the biffd/PyPI release the same way the marketplace
  plugin does today ("both channels ship together" becomes "all three
  ship together").
- **R-OC.8** CI MUST cover the opencode adapter (Bun/TypeScript
  toolchain), including at least one end-to-end test where a signed
  message produces a `session.prompt` delivery in a real opencode
  session.

### Acceptance

M5 is done when a teammate's signed `trusted` message lands in a live
opencode session as an injected prompt within seconds, with toast, with
zero polling anywhere on the path — and the same message reaching a
Claude Code session shows the M4 degraded path, both from one biffd.

---

## 7. Spikes (resolve before design closes)

Source-level investigations against opencode; its docs do not answer
these.

- **SP-1** `session.prompt()` semantics against a busy session: queue,
  interleave, or error? Determines R-OC.3.
- **SP-2** `noReply: true` context injection: token cost, visibility to
  the model, persistence across compaction. Determines R-OC.6 and
  informs the org-wide ethos-on-opencode question.
- **SP-3** Payload format: literal Nostr events over NATS vs.
  Nostr-shaped envelope. Evaluate interop value (future Buzz/Nostr
  federation) against implementation cost. Determines R-A.3's design.
- **SP-4** opencode plugin lifecycle: restart behavior, crash
  isolation, and whether a plugin survives across sessions — affects
  where the biffd client connection lives.

---

## 8. Non-goals

- **Migrating beadle-email off GPG.** Ruled out for this program
  (R-A.9); email clients are GPG-native. Revisit only with new
  evidence.
- **Replacing NATS with Nostr relays.** Transport is out of scope;
  only payload signing changes (SP-3 bounds the question).
- **Channels, threads, or chat-app semantics.** Biff's communication
  model ("purposeful, not chatty") is unchanged.
- **Implementing Claude Code push.** M4 tracks readiness for it
  (R-CC.3); building it is Anthropic's side of the fence.
- **Porting other punt-labs plugins to opencode.** This document
  covers biff; the org-wide opencode program is tracked separately.

---

## 9. Sequencing and delivery

1. **Spikes** (SP-1..SP-4) — small, parallel, source-level. Gate the
   design missions.
2. **M1 biffd** — extraction with Claude Code parity. M4's parity
   requirements (R-CC.1) are M1's acceptance criteria: M1 does not
   close until the full existing test suite passes against the
   daemon-backed path.
3. **M2 authenticity** — keys, signing, ethos registry, delegation.
4. **M3 permissions** — classification, policy, Z spec, config.
5. **M5 opencode adapter** — delivery, tools, commands, npm channel.

Each module is its own mission pipeline (`standard` or `formal` per the
repo's pipeline table — M2/M3 are `formal` candidates given the
protocol and state-machine content). Each lands through normal PRs with
CHANGELOG, README, and DES entries per the documentation discipline.
This document is decomposed into beads at design kickoff; requirement
IDs appear in bead descriptions and mission contracts for traceability.
