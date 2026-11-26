# Trust Model: Identity, Keys and What a Message May Do

Biff decides two things about every message, in code and never by asking the
model. First, who wrote it, proven by a signature. Second, what that author
may make the recipient's agent do, read from a signed address book.

The design copies Block's Buzz where it fits: keys, owner attestation and
message format. Biff adds its own permission ladder, the floor and the agent
model it already has. Biff owns this design. Ethos stores public keys and
does not interpret them.

## 1. Status of enforcement

Until enforcement ships (section 4.3), three protections are advisory: the
floor tag, the signed address book and the owner attestation. An agent that
can read its own key can sign any floor tag, and a process that can read
the address-book key can rewrite the address book. Each section below says
what it guarantees now and what waits for enforcement.

Until wave (c) (section 16), biff shows no trust labels, warnings or refusal
text on messages. Message text appears as it did before the channel work,
and `tests/_trust_phrases.py` keeps failing any tool output that carries
such a phrase. Wave (c) replaces that test with tests of the graded
presentation (section 12).

## 2. Terms

| Term | Meaning |
| --- | --- |
| Nostr | A protocol of signed JSON events. Each event carries its author's public key and a signature over its contents. |
| NIP | A numbered Nostr specification, such as NIP-01 (the event format). |
| npub | A Nostr public key written in its bech32 text form. The key is a secp256k1 point; signatures are BIP-340 Schnorr. |
| keyring | The operating system's secret store: macOS Keychain, or Secret Service on Linux. |
| ext | An ethos extension file on an identity (`<handle>.ext/<namespace>.yaml`). Ethos stores it without reading it. |
| owner | The person whose key signs an agent's attestation. |
| NIP-OA | Owner attestation: the owner signs a statement that an agent's public key acts for the owner. The agent stays the author of what it signs. |
| address book | The owner's signed list of senders and the level each is granted (section 7). |
| floor | The lowest level of any biff message or beadle mail a session has read. The session stamps it on everything it sends (section 8). |
| biffd | Biff's per-user daemon (`docs/design/biffd.md`). It holds keys and signs for its clients. |
| head | An event that no later event has cited yet (section 10). |
| Lamport number | A counter: one more than the highest counter the author has seen. |
| auth callout | A NATS server feature that asks an external service to approve each connection and set its permissions. |

## 3. Identity and keys

Each person and each agent is one Nostr key. There is no separate root
key. A person's key signs their messages, their agents' attestations and
their address book.

### 3.1 Where keys live

| Key | Private half | Public half |
| --- | --- | --- |
| Person (owner) | OS keyring, with an access rule that needs the owner present for each signature, for example a Touch ID prompt | Identity's `ext/nostr` namespace |
| Agent | Held by biffd; agents receive a token, not the key (section 4.2) | Agent identity's `ext/nostr` namespace |

The public key and, for an agent, its attestation and expiry go in the
identity's `ext/nostr` namespace. Biff writes that namespace and beadle
reads it. Ethos shows it in its merged identity view, so `ethos whoami`
prints the npub with no ethos code change.

Private keys never enter ext, any tracked file or an environment variable.
Ext values can reach model context, so a key there would be readable by the
model.

### 3.2 Without a keyring

On headless Linux there is no keyring. The private key is a
passphrase-encrypted file, and biff prompts for the passphrase on use
(Q2). A plain file is not a fallback.

### 3.3 Without ethos

When ethos is absent, biff keeps the same namespaces in a local YAML file,
as its existing fallback chain does for identity (DES-076). Behavior is
identical; only the store differs.

## 4. Who may read which key

### 4.1 The split

The rule for keys has two parts, because the two kinds of key face
different threats.

- **The owner key is never readable by an agent, from wave (a).** Signing
  the address book or an attestation needs the owner present. An agent
  that is prompt-injected cannot grant itself trust or mint an attestation.
- **An agent's own key is log-only until enforcement.** Agents keep working
  as they do today. Every key access is instrumented and logged. Enforcement
  waits until everything else is ready, so it breaks neither biff nor
  development.

### 4.2 Custody

biffd holds the private keys of the agents it serves and signs for its
clients. A client, such as an MCP server or the CLI, asks biffd to sign and
presents a token. Enforcement of custody lives in biffd, so it ships with
biffd. Key and attestation work in wave (a) can start before biffd exists.

### 4.3 What is advisory until enforcement

Until an agent cannot read its own key:

- the floor tag can be forged by an agent with shell access, because it is
  signed with that agent's key;
- the attestation limits a stolen agent key only through its expiry;
- the address book is protected only by the owner key, which is already
  out of reach.

Enforcement ships behind a switch and runs log-only during development.

## 5. Agents act for their owner: NIP-OA attestation

Biff creates the agent's key. The owner signs the agent's public key. The
attestation and its expiry go in the agent identity's `ext/nostr`
namespace.

NIP-OA's own time clause constrains the event's `created_at`, which the
agent sets, so an agent can ignore it. Biff therefore adds an expiry that
the receiver checks against its own clock.

| Where the agent runs | Default expiry | Configurable |
| --- | --- | --- |
| Developer machine | 24 hours | yes |
| Agent image | 7 days | yes |

An expired attestation is ignored. The message is then judged by its
author alone, which is a stranger unless the owner's address book names
the agent.

Renewal needs the owner present, because it needs a signature from the
owner key. An agent image receives a pre-signed attestation through its
read-only secret mount.

The agent stays the author. A message from `claude`, attested by
`jfreeman`, is displayed as "@claude, authorized by @jfreeman" and never
merges into `jfreeman`'s own messages.

## 6. The two checks

Both checks run in biffd and in beadle, never in the model.

1. **Is the signature valid, and is the attestation valid?** The event
   signature must verify. If the event carries a NIP-OA tag, the owner's
   signature on it must verify and its expiry must not have passed on the
   receiver's clock.
2. **What is the recipient's relationship with the author, or with its
   owner?** The owner is the attesting owner when the attestation is valid
   and unexpired, and otherwise the author.

### 6.1 Relationships

| Relationship | Who it is |
| --- | --- |
| self | The recipient, or any agent whose valid attestation names the recipient as owner |
| contact | A key in the recipient's address book |
| team | A key admitted to the same team relay as the recipient (Q1) |
| known | A key published in some ethos identity but not in the recipient's address book |
| stranger | Any other key, including a fresh key that nothing links to a person |

A valid signature says only who wrote the message. The relationship, the
address book and the floor decide what it may do.

## 7. The address book and the ladder

### 7.1 The ladder

Each sender gets exactly one level. The same level covers biff messages,
talk and beadle mail.

| Level | Meaning |
| --- | --- |
| `---` | Ignored. The agent never reads it. |
| `r--` | The agent reads the body as data. |
| `rw-` | The agent may reply in its own words. It asks the user before it edits files, runs commands, or sends file contents or command output. |
| `rwx` | The agent acts on it, as it acts on a subagent's prompt, within the host's normal permission prompts. |

The `notify y|n` setting stays separate. It controls whether the recipient
accepts messages at all.

### 7.2 Who gets what

| Sender | Level | What the agent does |
| --- | --- | --- |
| You and your own agents | `rwx`, subject to Q4 | Acts on it within normal permission prompts |
| Contact you added | As you set it; a new contact starts at `rw-` | Depends on the level |
| Teammate | `rw-` | Reads, searches and replies; asks you before changing anything |
| Known key, not a contact | `---` | Ignores it |
| Stranger, or unsigned | `---` | Ignores it |
| Signature stripped, where the sender normally signs | `---`, with a warning label from wave (c) | Ignores it |
| Bad or revoked signature | Nothing delivered | Biff logs an error |

A bad or revoked signature never reaches the model and never interrupts
the user. Replayed and expired messages are dropped without a log entry
above debug level.

### 7.3 The address book itself

The address book is the `ext/trust` namespace on the owner's identity. The
owner key signs it. Biff owns and writes it (`biff grant`); beadle reads
it. The 64-key ext cap allows about 60 contacts.

The address book also lists revoked keys (section 14).

A book whose signature fails is ignored, and every sender falls back to the
defaults in section 7.2. An agent that edits the file can only lower trust.
The owner key is out of an agent's reach, so the agent cannot re-sign.

### 7.4 Computing the level

```text
event arrives
  |
  +-- signature bad or revoked ----> drop; log error
  +-- unsigned or stripped --------> ---
  |
  v  signed
owner = attesting owner if the NIP-OA tag is valid and unexpired, else the author
  |
grant = address book entry for owner, or the default for its relationship
  |
level = min(grant, floor tag)
  |
  +-- ---  ignored; never read
  +-- r--  body as data
  +-- rw-  reply; ask before acting
  +-- rwx  act on it
```

## 8. The floor

An own agent receives `rwx`. If that agent read a stranger's mail and then
messaged a sibling, the stranger's words would arrive at `rwx`. The floor
prevents that path.

- Each agent session tracks the lowest level of any biff message or beadle
  mail it has read.
- The session stamps that level as a signed tag on everything it sends.
- The recipient takes the minimum of the tag and the grant.
- Biff keeps the floor in the session roster's ext map (DES-007), so it
  ends with the session.
- Biff and beadle compute the floor themselves, so it needs no tool hooks
  and works on every host.
- No biff tool, CLI flag or hook input accepts a floor value, so a model
  that passes on a stranger's request through biff's tools cannot raise it.

The floor covers biff messages and beadle mail only. Web pages, issue
text, repository files and other content stay outside it. The harness's
permission prompts bound that content, as they do today.

### 8.1 The floor gap (Q4, open)

Poisoned repository or web content is the main way injected instructions
reach coding agents. Agent A reads a poisoned README and messages agent B.
The floor does not see the README, so B receives the message at `rwx`.

Recommendation, which stands until the operator changes it: own-agent
messages default to `rw-` until the sender's session floor is clean. A
floor is clean when the session has read no biff message or beadle mail
below `self`. This needs no hooks and works on every host.

The recommendation narrows the gap and does not close it. Biff cannot know
that the session read a poisoned README. Closing it needs one harness
hook: a signal that the session read outside content. That hook is the
later upgrade.

## 9. Wire format

Transport stays NATS. Messages are Nostr events (NIP-01 JSON) carried over
NATS, not over WebSocket relays. Biff runs no buzz-relay and speaks only
Buzz's protocol.

| Message | Format |
| --- | --- |
| Channel messages: write, wall | NIP-29 `kind:9` events; an `h` tag names the room |
| Direct messages and talk | NIP-17: a rumor sealed to the recipient and gift-wrapped, encrypted end to end |

A forwarded channel message carries the original signed event, so the
original author's signature still verifies (section 13).

Event expiry uses an `expiration` tag (NIP-40) of at most 30 days, so a
stored message cannot outlive its usefulness. Unsigned messages stay
deliverable and are treated as `---` (section 7.2).

## 10. Ordering and delivery

Ordering lives in the signed event, not in the server. JetStream's stream
sequence is server metadata that no signature covers. A relay operator
could reorder events, drop them or show different readers different
histories. Each event therefore carries its own causal history.

### 10.1 Fields every event carries

- **Heads.** Tags cite the full ids of every head the author has seen.
  The 8-character prefixes of NIP-29 are not used.
- **Author chain.** Each event cites the author's own previous event and a
  per-author counter, as Secure Scuttlebutt does and as Matrix does with
  `prev_events`.
- **Lamport number.** Each event carries the highest Lamport number its
  author has seen, plus one.

Every reader sorts by (Lamport number, event id) and gets the same total
order.

### 10.2 What readers detect

- A gap: an event cites an id the reader never received.
- An author fork: two events from one author with the same counter.
- A relay showing different histories: heads disagree when readers
  compare them.

### 10.3 JetStream's role

JetStream provides delivery, storage and a fast path for honest writers.
It is not the authority on order.

- One stream per room, with limits retention. Biff's inbox uses
  `WORK_QUEUE` retention, which deletes a message once it is read. Facts
  must be kept, so rooms do not use it.
- `Nats-Expected-Last-Subject-Sequence` on each `entity.attribute` subject
  rejects, at write time, a write based on a stale value.
- `Nats-Msg-Id` is the Nostr event id, so JetStream drops duplicates
  inside its dedup window. Outside that window, the reader keeps its own
  seen-id check.
- KV provides the current-value view.
- `created_at` is the author's claim. It is used only to check expiry,
  never to order.

### 10.4 Concurrent writes

An honest writer publishes with `Nats-Expected-Last-Subject-Sequence`, so a
second write based on the same stale value is rejected at write time. The
writer then re-reads. If the new value came from its own author, it retries;
if another author changed the attribute, it does not overwrite, and sends the
conflict to the owner's needs-you queue as a decision. The relay enforces this only for writers that
send the header, and it is not covered by any signature. So readers do not
rely on it: when two signed writes cite the same predecessor and both reach
them, every reader resolves them the same way, and the higher (Lamport number,
event id) wins. Where losing a write matters, the conflict goes to the owner's
needs-you queue as a decision.

## 11. Admission and exposure

### 11.1 Admission

NIP-42 (a client proves it holds a key) and NIP-AA (an agent is admitted
through its owner) map to the NATS auth callout, available from NATS 2.10.
The callout verifies a Nostr-signed challenge and returns per-npub publish
and subscribe permissions. NATS user keys (nkeys) are ed25519 and Nostr
keys are secp256k1, so the two do not map directly.

Team membership (Q1) is relay membership, as NIP-43 defines it in Buzz.
No organization key held by an admin is involved.

### 11.2 What the server operator sees

NIP-17 content stays end-to-end encrypted. The sender is not hidden from
the server operator, who sees the authenticated connection that
published and the subject that names the recipient. Biff does not
protect that metadata.

## 12. Presentation

Labels arrive with wave (c). Until then biff shows no trust labels,
warnings or refusal text (DES-093), and `tests/_trust_phrases.py` stays.

From wave (c):

- Message bodies travel inside Claude Code channel events, at the level
  the recipient's grant allows. The wake-line-then-pull behavior goes away
  once every harness plugin supports push.
- A message shows its author, its owner when attested, and its level, for
  example "@claude, authorized by @jfreeman, rwx".
- The wave replaces `tests/_trust_phrases.py` with tests of this
  presentation.

## 13. Forwarding

A forwarded channel message carries the original signed event inside the
forwarder's own signed event. A reader verifies both independently.
Tampering with the inner event fails its signature.

The level of a forward is the lower of the forwarder's level and the
original author's level. If the forwarder relays words nobody signed, only
the forwarder is proven, and the level is the forwarder's own.

NIP-17 direct messages are deniable by design, so a third party cannot
verify a forwarded one. Verifiable forwarding of DMs is not supported
(Q3).

## 14. Revocation

Revoking an agent key leaves the owner's key untouched. The owner adds
the agent's key to the revoked list in the signed address book, which
needs the owner present.

A verifier rejects a message from a revoked key from the moment it learns
of the revocation, whatever the message's `created_at`, because a thief
can backdate. The attestation expiry (section 5) bounds how long a
revocation can be withheld: 24 hours on a developer machine, 7 days in an
agent image.

If the owner key itself is lost or stolen, the owner creates a new key,
re-signs the address book and re-attests each agent. Contacts re-pin the
new key by hand.

## 15. One person across tools

One person is one identity, whichever key proved a message.

- The `ext/nostr` and `ext/beadle` namespaces hold the person's npub and
  GPG key id.
- Each key signs the other: the npub signs a statement naming the GPG
  fingerprint, and the GPG key signs a statement naming the npub.
  Both signatures are required. One direction alone lets a person claim
  someone else's key.
- Agent mail carries its NIP-OA attestation in a header.

A GPG-signed mail and a biff event from the same person therefore resolve
to the same identity, address-book entry and level. Beadle stays on GPG
because email clients are GPG-native. GnuPG has no BIP-340 support, so one
key cannot serve both stacks.

## 16. Rollout

Four waves, each shippable alone. No ethos release is needed: biff writes
the ext namespaces, and ethos's merged view shows them. Wave (b) can start
alongside wave (a) against fixtures.

| Wave | Repos | User-visible change | Proven by |
| --- | --- | --- | --- |
| (a) Keys and attestation | biff | `ethos whoami` prints your npub and your agents' attestations; `biff who` gets a key column | Buzz's NIP-OA test vectors pass; private keys are never in the repository tree or in ext; the YAML fallback works with ethos absent; the owner key is not readable by an agent |
| (b) Buzz protocol | biff | Channel messages are signed NIP-29 events and DMs are NIP-17; the NATS callout admits each key; delivery and display are unchanged | Forged, redirected, replayed, stripped and tampered events are each classified correctly; unauthorized subscribe and publish are rejected; a capture of the relay holds no DM plaintext |
| (c) Grants, floor, graded delivery | biff | A message from your own agent arrives as an instruction | A Z specification of classification and delivery passes with zero audit gaps; then an own-agent message is acted on, a stranger's identical message is not, and an agent that read stranger mail sends at `---` |
| (d) beadle | beadle | `check_trust` shows "verified, team acme" | Tests that fail on today's code: attached-key mail is not verified, and a stored `0x` matches nothing |

Wave (d) ships in two steps. Its signature-checking fixes follow wave (a).
Its move to the shared address book follows wave (c), using
`biff grant --import-beadle` so per-mailbox grants and denies carry over.

Wave (b) records the verification result of each message in biff's log.
It shows nothing to the user, because labels wait for wave (c).

## 17. Live beadle bugs

Three beadle bugs exist independent of this design. Reviewers found them
by reading the code; no test has run against them yet.

1. **Attached-key self-signing.** Beadle imports a public key attached to
   the message itself (`internal/pgp/verify.go:57-70`). Any sender can sign
   with a fresh key and show as verified. When a contact has no key on
   file, that mail passes the daemon's gate for running a signed recipe.
2. **Short key-id suffix match.** The daemon compares key ids with
   `strings.HasSuffix` in `verifyTrust` (`internal/daemon/handler.go:343` in
   the beadle checkout of 2026-11-26). An empty stored id accepts any key, and a stored `0x`
   normalizes to empty and matches every key.
3. **Unchecked command-line paths.** The `beadle-email` read and send
   commands (`cmd/beadle-email/email_cmd.go`) enforce no read or write
   permission. Only the MCP server and the daemon do.

## 18. What this design does not protect

- Anything an agent does once it is prompt-injected, because it runs as its
  owner. The owner key's isolation and the address book limit what the agent
  can grant, not what it can do.
- Poisoned web, issue or repository content reaching an own agent, beyond
  the Q4 recommendation.
- A relay that withholds a revocation, beyond the expiry bound in section 14.
- A permission prompt the user approves.
- Metadata and traffic analysis (section 11.2).
- A compromised operating system or harness.
- Signatures prove who wrote a message, never that obeying it is safe.

## 19. Questions

| # | Question | Answer |
| --- | --- | --- |
| Q1 | Team trust: an org key held by an admin, or relay membership (NIP-43)? | Ruled 2026-11-26: relay membership, to match Buzz |
| Q2 | Where does the key live with no keyring? | Ruled 2026-11-26: a passphrase-encrypted file, prompted on use |
| Q3 | Should forwarded DMs be verifiable? | Ruled 2026-11-26: not for now. Verifiable forwarding needs the inner message signed, a departure from NIP-17 |
| Q4 | How is the floor gap closed? | Open. Recommendation: own-agent messages default to `rw-` until the sender's session floor is clean; a harness hook is the later upgrade |

## 20. Requirements traceability

`docs/requirements-biffd-multihost.md` no longer carries the `R-A` and
`R-P` requirement groups. This table maps each requirement to its section
here.

| Requirement | Subject | Section |
| --- | --- | --- |
| R-A.1 | A keypair per participant; signatures decide authorship | 3, 6 |
| R-A.2 | Signed end to end; relay cannot forge, alter or replay | 9, 10 |
| R-A.3 | secp256k1 Schnorr in Nostr events; literal events over NATS | 2, 9 |
| R-A.4 | Agent keys distinct from owner keys | 3, 4 |
| R-A.5 | Owner attestation; authorization does not erase authorship | 5 |
| R-A.6 | Surgical revocation | 14 |
| R-A.7 | System of record for keys | 3 (public keys in ext; ethos stores and does not interpret them) |
| R-A.8 | Unsigned messages stay deliverable | 7.2 |
| R-A.9 | Beadle stays on GPG | 15 |
| R-A.10 | Forwarding keeps the inner signed message | 13 (channel messages; DMs are not verifiable, Q3) |
| R-A.11 | Origin classes for forwards | 13 (the two cases fold into the forward's level) |
| R-P.1 | Four trust levels | Dropped: replaced by relationships (6.1) and the ladder (7.1) |
| R-P.2 | Trust level decides delivery privilege | 7.1, 7.2, 7.4 |
| R-P.3 | Recipient-configurable policy; no grant to unsigned senders | 7.3 |
| R-P.4 | A failed signature is an alarm | Dropped: a bad signature is a logged error, with no alarm, because biff shows no labels now (7.2) |
| R-P.5 | Only wake is push; injected prompt names author and level | 12 |
| R-P.6 | Classification lives in biffd | 4.2, 6 |
| R-P.7 | Z specification with zero audit gaps | 16 (wave c) |
| R-P.8 | A forward takes the lowest level in its chain | 13 |

## 21. Sources

- [block/buzz](https://github.com/block/buzz), read at commit 298543f:
  NIP-OA (owner attestation), NIP-AA (agent authentication), SECURITY.md,
  the local credential provider and NOSTR.md.
- [Block engineering: Buzz](https://engineering.block.xyz/blog/buzz):
  "authorization does not erase authorship".
- Ethos PR #667: an earlier design, reviewed for threats (13 findings)
  and key handling (14 findings). Its floor, signed address book,
  confirmation for `rw-` replies and 30-day expiry are used here.
- DES-094 in `DESIGN.md`: the decision this document implements.
