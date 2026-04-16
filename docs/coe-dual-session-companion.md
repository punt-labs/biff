# COE — dual-session companion registration (biff-dzqc)

**Date:** 2026-04-15
**Scope:** biff v1.8.0 → v1.9.0
**Severity:** P1 — shipped feature (dual-session) did not work end-to-end
**Related:** biff-dzqc (this bead), biff-2mhb (resume-session registration race), biff-uw6i (dual-session UX), biff-l9cl (pre-existing hosted test drift)

## 1. Summary

The v1.8.0 dual-session feature registered companion sessions in a way that
left a KV row with an empty `tty_name` any time anything failed between two
sequential writes. Users observed `claude:eaffcbe5` (raw TTY hex) in `/who`
output instead of `claude:ttyN`. The fix inverts session registration from a
two-write pattern to a claim-then-write-once pattern via a new
`register_session()` helper, used by both primary and companion startup paths.
A tier-3b regression test against real NATS would have caught the original
bug; it did not exist in v1.8.0.

## 2. Timeline

| Date (local) | Event |
|---|---|
| 2026-04-08 (PR #208) | v1.8.0 shipped dual-session (DES-039). Two-write pattern introduced in `_register_companion` and `_active_lifespan`. No assertion on resulting KV state. |
| 2026-04-14 (PR #210) | v1.8.1 shipped — fixed ethos roster parser. Dual-session now activated for the first time in practice. |
| 2026-04-15 13:46 | CEO noticed `claude:eaffcbe5` in `/who` — raw hex instead of `ttyN`. |
| 2026-04-15 13:53 | Filed biff-2mhb (resumed-session companion miss) — later refined. |
| 2026-04-15 14:14 | Filed biff-dzqc with proof-required mandate. COE pipeline instantiated. |
| 2026-04-15 14:30 | Stage 1 investigate — evidence collected, structural hypothesis formed. |
| 2026-04-15 15:10 | Stage 2 root-cause — three in-isolation reproductions; two-write pattern confirmed as structural defect; specific runtime trigger in PID 983529 unreproducible from outside daemon but falsified each candidate. |
| 2026-04-15 15:40 | Stage 3 fix — `register_session` helper, inverted both paths, 7 regression tests, version bump to 1.9.0. |
| 2026-04-15 15:56 | Stage 4 verify — `make check` green, tier-3b test passed, 4 code-review findings flagged (2 critical, 2 important). |
| 2026-04-15 16:40 | Fix-up round — stale reservation release, marker ordering, docstrings, guard softening. All clean. |
| 2026-04-15 ~17:00 | Stage 5 document + PR. |

## 3. Root cause

### 3.1 Structural defect

Both `_register_companion` (src/biff/server/app.py pre-fix L670-678) and the
parallel primary path in `_active_lifespan` (pre-fix L734-770) used a
two-write pattern:

1. Write the session row to KV with `tty_name=""` (default).
2. Call `claim_tty_name()` — NATS CAS on the names bucket.
3. Write the session row again with `tty_name` set.

If anything failed between steps 1 and 3, the KV row remained with
`tty_name=""` and nothing ever fixed it. Heartbeats preserve `tty_name` via
`model_copy(update={"last_active": ...})` (nats_relay.py:844) — so a row
initialised with empty `tty_name` stays empty indefinitely.

`cli_session.cli_session()` (cli_session.py:190-212) already used the correct
pattern: claim first, build the full `UserSession` including `tty_name`, write
once.

### 3.2 Why the specific runtime failure was unreproducible

Stage 2 ran three separate reproductions:

- LocalRelay — in-memory dict, no failure mode between writes.
- Fresh NATS (`connect.ngs.global` with throwaway users) — no mid-write error.
- Pre-reserved `tty1..tty5` — `claim_tty_name` re-fetches on collision and
  finds `tty6`; no exhaust.

None reproduced the empty-`tty_name` outcome. Candidate triggers (FastMCP
lifespan swallowing startup exception; concurrent process race on name
reservation; unhandled NATS RequestError from `kv.put`) remained plausible but
unproven. The fix does not require knowing which fired — a one-write flow
with the name already present cannot leave a row with empty `tty_name`.

### 3.3 Why it shipped

v1.8.0's PR #208 had zero tests that asserted post-startup KV state. Existing
test coverage:

- `tests/test_server/` — tool behavior on already-registered sessions. Assumes
  registration worked.
- `tests/test_integration/` — MCP tool dispatch and protocol. Does not inspect
  KV.
- `tests/test_subprocess/` — boots real `biff serve` but only asserts `/who`
  output formatting. The fallback branch `_format_who_name` returns
  `s.tty_name or s.tty[:8]` (formatting.py:115-118) — when `tty_name` is
  empty, output renders as `user:hexfrag`, which looked plausible to the
  human reviewer and did not trigger a failed assertion.
- Hosted NATS tier-3c existed but had no dual-session-specific test.

The v1.8.0 bug was structural — it was present from the merge — but nothing
ran the dual-session lifespan against a relay whose I/O could fail between the
two writes.

## 4. Fix

### 4.1 `register_session` helper

New function in src/biff/server/app.py:

```python
async def register_session(
    relay: Relay,
    user: str,
    tty_hex: str,
    *,
    display_name: str,
    kind: str,
    hostname: str,
    pwd: str,
    repo: str,
    preferred_name: str | None = None,
) -> tuple[UserSession, str]:
    # Release stale reservation if previous process crashed.
    session_key = build_session_key(user, tty_hex)
    existing = await relay.get_session(session_key)
    tty_name = await claim_tty_name(relay, user, session_key, preferred=preferred_name)
    if existing is not None and existing.tty_name and existing.tty_name != tty_name:
        with suppress(Exception):
            await relay.release_tty_name(existing.user, existing.tty_name)
    session = UserSession(
        user=user, tty=tty_hex, tty_name=tty_name,
        display_name=display_name, kind=kind,
        hostname=hostname, pwd=pwd, repo=repo,
        last_active=datetime.now(UTC),
    )
    await relay.update_session(session)
    return session, tty_name
```

Claim first, write once. Stale reservation released before overwrite to
prevent leaks across crash-restart cycles.

### 4.2 Call sites inverted

Both `_register_companion` and `_active_lifespan` primary path now call
`register_session` once. Active-marker writes moved to AFTER
`register_session` returns — preserving the invariant "marker exists iff KV
row exists".

### 4.3 Silent failure → audible

`write_active_session(...)` previously wrapped in `with suppress(OSError)`.
Replaced with `_write_marker()` that catches OSError and calls
`logger.warning()`. Marker-write failures now show up in logs.

### 4.4 Fallback guards

`update_companion_session` previously constructed a fresh `UserSession`
without `tty_name` if the row was missing. Now logs a warning and returns
`None` — graceful degradation for the "row reaped, tool called before
restart" case.

### 4.5 Regression tests

Seven new tests:

- Tier 1 (`test_lifespan_registration.py`): register_session happy path,
  companion tty_name write, atomic under claim failure, stale-reservation
  release. LocalRelay-based; asserts the invariant.
- Tier 1 (`test_active_markers.py`): marker exists post-success, marker NOT
  written on register failure.
- Tier 3b (`test_dual_session_lifespan.py`): two concurrent lifespans against
  real NATS register 4 distinct sessions with non-empty `tty_name` on all
  four. **This is the actual regression guard** — it can fail the two-write
  pattern in a way LocalRelay cannot.

## 5. Prevention

### 5.1 Post-startup state assertions are mandatory

Every lifespan startup path must have at least one test that asserts the
resulting KV state matches documented invariants. No exceptions. This would
have caught v1.8.0 at PR time.

### 5.2 Tier-3b is non-negotiable for relay changes

Tier-1 tests with LocalRelay cannot exercise mid-write failure modes. Any
change to registration, heartbeat, or relay I/O must include a hosted NATS
assertion. The release checklist already says "hosted NATS tests pass locally
if relay code changed" — v1.8.0 changed relay code and did not have a
dual-session hosted test.

### 5.3 Invariant: `/who` output must not silently fall back to hex

`_format_who_name` returns `s.tty_name or s.tty[:8]`. Plausible output masks
bugs. Consider adding a debug mode or metric that logs when the fallback
branch fires — so a broken registration surfaces as a log, not as a
"looks-fine" UI artifact.

### 5.4 Claim-then-write is the pattern

Any new session-registration code path — for a new identity kind, a new
process type, a future multi-agent scenario — uses `register_session()`.
Do not reintroduce two-write patterns. The helper is the contract.

## 6. What changed in this PR

- `src/biff/server/app.py` — `register_session` helper, `_write_marker`,
  inverted both registration paths, marker after KV write.
- `src/biff/server/tools/_session.py` — guard-and-return-None in companion
  update fallback; tty_name backfill in `get_or_create_session`.
- `tests/test_server/test_lifespan_registration.py` (new) — 5 tier-1 tests.
- `tests/test_server/test_active_markers.py` (new) — 2 tier-1 tests.
- `tests/test_nats_e2e/test_dual_session_lifespan.py` (new) — 1 tier-3b test.
- `CHANGELOG.md`, `pyproject.toml`, `.claude-plugin/plugin.json`,
  `uv.lock` — version 1.8.1 → 1.9.0.

## 7. Related open items

- **biff-l9cl** — pre-existing `test_hosted_e2e.py` failures (plan text vs
  boolean P column). Not caused by this PR. P3.
- **biff-2mhb** — resumed-session companion miss. Separate race — the
  MCP server starts before the SessionStart hook finishes populating the
  ethos roster. Structural fix is lazy companion registration on heartbeat
  tick; does not overlap with this PR. P1.
- **biff-uw6i** — dual-session UX (status bar + read_messages sectioning).
  Blocked on registration working; now unblocked. P1.
