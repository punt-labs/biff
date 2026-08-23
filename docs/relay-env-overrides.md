# Design: environment-variable relay overrides for headless invocations

Status: **proposed** — not yet implemented, not yet ratified.
Ticket: biff-xvv. Mission: m-2026-08-23-005.

## Why a standalone doc, not a `DESIGN.md` ADR

`DESIGN.md` records *settled* decisions (DES-NNN entries) after implementation.
This document is a proposal awaiting review — writing it as a DES entry would
imply a decision that hasn't been made. Once this design is ratified and
implemented, the accepted shape gets a DES-NNN entry in `DESIGN.md` that
points back here for the rejected-alternatives detail, per the existing
convention (e.g. DES-016's amendment chain).

## Problem

`src/biff/config.py`'s relay resolution reads only two files:
`.punt-labs/biff/config.yaml` (committed, shared) and
`.punt-labs/biff/config.local.yaml` (gitignored, personal — never present in
a CI checkout). `.github/workflows/biff-notify.yml` runs

```bash
uvx --from punt-biff biff --user github-actions wall "..." --duration 2h
```

with no relay override of any kind. `--relay-url` exists only on `serve` and
`mcp` (`src/biff/__main__.py:1286`, `:1318`) — the `wall` command, and every
other CLI command, has zero flags for this. Even where `--relay-url` exists,
`_load_base_config` (`config.py:1158-1162`) deliberately clears
`relay_auth` and `relay_tls_handshake_first` on override, so it cannot point
at a token-authed relay anyway — see PR #383's postmortem below.

Net effect: CI always falls back to the bundled demo relay
(`DEMO_RELAY_URL = "tls://connect.ngs.global"`), silently, with no way to
reach the private relay a team's Claude Code plugin sessions actually use.

## The PR #383 precedent — read before touching this chain

PR #383 (`abcb68d`, merged this session) fixed a real production hang: NATS's
opportunistic-TLS negotiation deadlocks against a TLS-terminating proxy. The
first attempted fix inferred TLS handshake mode from the `tls://` URL scheme
— caught in review before shipping, because the demo relay's native-TLS
`nats-server` *also* uses `tls://` and needs the opposite (opportunistic)
behavior. Both deployment shapes are indistinguishable from the URL alone;
the shipped fix is an explicit `relay.tls_handshake_first: true` opt-in, not
inference. The `--relay-url` override was also hardened at the same time to
clear `relay_auth` and `relay_tls_handshake_first` together, because leaving
either set would silently misapply a property of the *old* relay to
whatever the override points at.

This design inherits that lesson directly: **every place a relay URL can
change, its auth and its TLS mode must change atomically with it, or not at
all.** Section 3 below applies this as a hard rule, not a suggestion.

## 0. Repo-scoping — env vars are ambient, biff's security model is not

**Round 2 amendment.** Round 1 review (djb) found this design's biggest gap:
environment variables are process-global, but every other credential surface
in biff is repo/team-scoped (`CLAUDE.md`: "Team-scoped. `/wall` broadcasts to
your team, not the world."). A shell that exports `BIFF_RELAY_TOKEN` once —
which the now-retracted §6(b) text below used to recommend — carries that
export into *every* `biff` invocation in that shell, in every repo, for the
rest of the session. Combined with §2's "exactly one auth var wholesale
replaces the file-resolved `RelayAuth`" rule, that means: operator works in
repo B, exports Team B's token to debug something; later in the same shell
runs `biff wall` in repo A, whose `config.yaml` declares Team A's
`nkeys_seed`/`user_credentials` pointing at Team A's private relay. The env
var silently wins, and Team B's token gets transmitted to Team A's relay —
either failing loudly after the fact, or succeeding and leaking a message to
the wrong team's channel.

**Round 3 amendment — the primary fix for human usage is direnv, not a new
config.yaml gate.** This org already solves "repo-scoped environment
variables that don't leak across `cd`" — every repo has a self-contained,
`direnv`-managed `.envrc` (workspace `CLAUDE.md`, "Repo Environment
(`.envrc`)": "Every repo has an identical, self-contained `.envrc`... No
cross-directory inheritance," with machine-specific overrides in
`.envrc.local`, gitignored). `direnv` loads a repo's environment on `cd` in
and unloads it on `cd` out. That is structurally different from, and safer
than, a bare shell `export`: a bare `export BIFF_RELAY_TOKEN=...` in a raw
shell (interactive session or `~/.zshrc`) is ambient for the rest of the
shell's life across every repo, which is exactly the cross-repo leak djb
flagged above — but `BIFF_RELAY_TOKEN=...` set in *repo B's*
`.envrc.local` is live only while the operator is `cd`'d into repo B; `cd`
into repo A and direnv unloads it before any `biff wall` in repo A ever
reads the environment. The leak scenario in the paragraph above cannot occur
through `.envrc.local`, because there is no shell state left over to carry
Team B's token into repo A — direnv, not biff, is the boundary.

**Human-usage guidance (supersedes the "no env vars for humans" framing
below).** The correct pattern for a human who needs `BIFF_RELAY_*` set for a
specific repo is: add it to that repo's `.envrc.local` (gitignored,
machine-specific — the existing per-repo override file, same mechanism the
org already uses for secrets sourced from the platform keychain). This
requires no new logic in biff; it reuses infrastructure that already exists
and is already the org's standard answer to this exact problem class. It is
**not** equivalent to the retracted §6(b) suggestion — §6(b) proposed a bare
shell export with no repo boundary at all.

**Defense-in-depth: the `config.yaml` opt-in gate, kept but demoted to
secondary.** `_apply_env_relay_overrides` (§1) still checks one precondition
before reading any `BIFF_RELAY_*` variable: the file-resolved
`_ConfigFields.relay_url` is `None` **or** `config.yaml` sets
`relay.allow_env_override: true` explicitly. This remains worth keeping —
`.envrc.local` scoping protects a well-behaved operator's shell, but it does
not protect against a `BIFF_RELAY_*` value set some other way (a systemd
unit's `Environment=`, a container's base image, a CI runner misconfigured
to inherit a parent job's env) reaching a repo whose `config.yaml` already
commits to a specific relay. The gate is redundant for the `.envrc.local`
case and load-bearing for those other cases, so it stays as a second layer,
not the primary fix:

- **CI's case is unaffected.** `biff-notify.yml`'s sparse checkout of
  `.punt-labs/biff` has no `relay:` section in `config.yaml` at all today
  (there is no committed relay config for CI to conflict with) — so the
  "file-resolved `relay_url` is `None`" branch of the precondition is exactly
  the CI path, and env overrides apply with zero new config needed.
- **A repo that already has its own committed `relay.url`/`relay_auth`
  cannot be silently overridden by an ambient env var.** If Team A's
  `config.yaml` declares a relay, `_apply_env_relay_overrides` no-ops unless
  that same `config.yaml` explicitly adds `relay.allow_env_override: true` —
  a decision Team A makes and commits, not one an unrelated operator's shell
  history (or `.envrc.local`, or systemd unit) makes for them.
- This is a new field on the existing `relay:` mapping in `_ConfigFields`
  (`config.py`'s `_extract_relay`), defaulting to `False` when absent, mirrors
  the strictness of the existing auth-conflict `SystemExit` (§2) — silence
  means "no," not "maybe."

**Retracted: the §6(b) "a human exports `BIFF_RELAY_TOKEN` once per shell
session" suggestion.** That text (previously in the rejected-alternatives
comparison) is not just unendorsed, it is actively wrong: interactive human
use of `BIFF_RELAY_*` via a bare, non-direnv shell export is the exact
ambient-credential footgun this section exists to close. §6(b)'s surviving
rejection reasons (shell-history exposure) still hold, but the design must
not suggest a bare exported env var as a workable human workflow anywhere
else in this document. **Documented warning:** do not
`export BIFF_RELAY_TOKEN=...` in an interactive shell session or in a raw
shell profile such as `~/.zshrc` — either one reintroduces the cross-repo
leak described above, because neither is unloaded when the operator `cd`s
into a different repo. Use the repo's `.envrc.local` instead, per the
guidance above. `BIFF_RELAY_*` variables set this way (or via CI/container/
systemd `env:`) are a deployment-context signal for a single relay identity
scoped to one repo or one process's entire lifetime — never a value that
should outlive a `cd` into another repo.

## 1. Env var names and precedence

Five new environment variables, read only by the config-resolution layer,
never by presentation code:

| Variable | Maps to | Notes |
|---|---|---|
| `BIFF_RELAY_URL` | `relay.url` | e.g. `tls://relay.example.com:4222` |
| `BIFF_RELAY_TOKEN` | `relay.auth.token` | mutually exclusive with the next two |
| `BIFF_RELAY_NKEYS_SEED` | `relay.auth.nkeys_seed` | path to a `.nk` file, must exist at run time |
| `BIFF_RELAY_USER_CREDENTIALS` | `relay.auth.user_credentials` | path to a `.creds` file |
| `BIFF_RELAY_TLS_HANDSHAKE_FIRST` | `relay.tls_handshake_first` | `"1"`/`"true"`/`"yes"` (case-insensitive) → `True`; anything else, including unset → unchanged |

**Precedence, lowest to highest** (unchanged fields fall through to the next
layer down — this is a *field-level* override, exactly like
`config.local.yaml`'s deep merge over `config.yaml`, not a whole-document
replacement):

```text
1. config.yaml            (committed, shared)
2. config.local.yaml       (gitignored, personal — merged over 1)
3. BIFF_RELAY_* env vars   (this proposal — merged over 1+2)
4. --relay-url CLI flag    (serve/mcp only, existing RELAY_URL_UNSET pattern)
```

CLI `--relay-url` stays the final word, unchanged from today, because it is
the most locally explicit signal available (an operator typing a flag at the
moment of invocation) and because `serve`/`mcp` already document and test
that behavior. Env vars sit *below* the CLI flag and *above* both config
files, because they represent the deployment context (a CI runner, a
container, a systemd unit) choosing which relay to speak to — a context that
should win over whatever the checked-out repo happens to say, without
requiring a code change to say so.

### Where this slots into the existing code

`_load_base_config` (`config.py:1123`) is the single choke point every entry
point (`load_mcp_config`, `load_cli_config`) already funnels through. Add one
step immediately after `_resolve_config_fields` returns and before
`_apply_demo_relay_default`:

```python
cf = _resolve_config_fields(repo_root)
cf = _apply_env_relay_overrides(cf)          # NEW
relay_url_resolved, relay_auth = _apply_demo_relay_default(
    cf.relay_url, cf.relay_auth
)
...
if relay_url_override is not RELAY_URL_UNSET:   # existing --relay-url path, untouched
    ...
```

Placing it *before* `_apply_demo_relay_default` means: if `BIFF_RELAY_URL` is
set, the demo-relay fallback never fires (correct — env said "use this
relay"); if it isn't set, the fallback behaves exactly as it does today.
Placing it *after* `_resolve_config_fields` means env vars see the fully
merged `config.yaml` + `config.local.yaml` result and override on top of it,
not underneath it.

`_apply_env_relay_overrides` is a pure function `_ConfigFields ->
_ConfigFields`, symmetric with `_apply_demo_relay_default` and
`_extract_relay` in shape — no new class needed, `_ConfigFields` already
exists as the intermediate container these fields flow through.

### `doctor.py` also resolves relay config independently

`doctor.py:_resolve_relay_config()` (`doctor.py:120-147`) is a **second,
independent** relay-config reader — PR #383 had to thread
`tls_handshake_first` through it by hand because it doesn't call
`config.py`'s pipeline. Any implementation of this proposal must call
`_apply_env_relay_overrides` from both `_load_base_config` and
`doctor.py:_resolve_relay_config`, or (better) make `doctor.py` call into
`config.py`'s resolution instead of duplicating it. Forgetting the second
writer is exactly the failure class flagged before: a source of truth with
an un-audited second reader/writer goes stale silently. `biff doctor` must
report the same relay `wall` actually connects to, including under env
overrides — a CI runbook step (`biff doctor`) that says "reachable" against
the demo relay while `wall` silently uses the env-overridden production
relay (or vice versa) is worse than no diagnostic at all.

## 2. Mutual exclusivity with `RelayAuth`'s other auth modes

`RelayAuth` (`models.py:174-199`) already enforces "at most one of `token`,
`nkeys_seed`, `user_credentials`" at config-parse time
(`_extract_relay`, `config.py:904-910`, raises `SystemExit` on conflict). The
env-var layer must enforce the identical invariant, independently, at the
same strictness — and, per §0, only after confirming the repo either has no
committed relay config or has explicitly opted in via
`relay.allow_env_override: true`:

- If **more than one** of `BIFF_RELAY_TOKEN`, `BIFF_RELAY_NKEYS_SEED`,
  `BIFF_RELAY_USER_CREDENTIALS` is set (non-empty) simultaneously,
  `_apply_env_relay_overrides` raises `SystemExit` with the same message
  shape as `_extract_relay`'s conflict error, naming which env vars
  conflict — never which *values* they hold.
- If **exactly one** is set, it becomes the new `relay_auth` **wholesale** —
  it replaces the file-resolved `RelayAuth`, it is never merged field-by-field
  with it. A `RelayAuth(token="…")` from a file and a
  `BIFF_RELAY_NKEYS_SEED` from the environment must never combine into a
  `RelayAuth(token="…", nkeys_seed="…")` — that object violates `RelayAuth`'s
  own single-field invariant and `RelayAuth.as_nats_kwargs()` would silently
  pick just one of the two, hiding the conflict instead of failing on it.
- If **none** is set, `relay_auth` falls through unchanged from
  `config.yaml`/`config.local.yaml` resolution — this is the case that lets a
  team keep `relay.url` committed in `config.yaml` while CI supplies only the
  token via `BIFF_RELAY_TOKEN`.

### The URL-changes-clear-auth rule (direct #383 lineage)

If `BIFF_RELAY_URL` is set **and** none of the three auth env vars is set,
`_apply_env_relay_overrides` clears `relay_auth` and
`relay_tls_handshake_first` to their empty defaults before returning — the
exact same rule `_load_base_config` already applies to the CLI `--relay-url`
override (`config.py:1153-1157`), for the exact same reason: auth and TLS
mode are properties of the relay being replaced, not portable to whatever
`BIFF_RELAY_URL` now points at. Concretely: a repo whose `config.yaml` has no
`relay:` section resolves to the demo relay's URL *and* its bundled demo
credentials (`_apply_demo_relay_default`); if CI sets only `BIFF_RELAY_URL`
to a private relay without also setting an auth env var, silently carrying
the demo creds forward would mean CI attempts to authenticate to the private
relay with democreds — an even more confusing failure than "no auth
configured." Clearing forces the operator to set the *pair*
(`BIFF_RELAY_URL` + one of the three auth vars) together, which is also
exactly the pairing `biff-notify.yml`'s new `env:` block will use (§7).

If `BIFF_RELAY_URL` is **not** set, none of this fires — env-only auth
override (e.g. rotating just the token while `config.yaml`'s URL stays
put) leaves `relay_tls_handshake_first` and the URL exactly as the files
resolved them.

## 3. Does `tls_handshake_first` need its own env var, or can it be inferred?

**Own env var — `BIFF_RELAY_TLS_HANDSHAKE_FIRST` — never inferred.** This is
not a new judgment call; it is the direct, restated conclusion of PR #383.
The scheme-inference approach was tried, was wrong, and was caught in review
specifically because `tls://` is worn by both the demo relay (opportunistic
TLS, must **not** set the flag) and a TLS-terminating proxy (must set it).
Nothing about moving the *source* of the URL from a YAML file to an
environment variable changes that ambiguity — the string `tls://…` still
carries no information about which side of the proxy question it's on.
Re-deriving it from the URL a second time, even "just for the env-var path,"
reopens the exact bug #383 closed. One explicit boolean, one meaning,
independent of where the URL came from.

## 4. How does `wall` (and every other CLI command) pick this up?

**No new flags on `wall`, and no per-command work at all.** Every CLI
command already funnels through `cli_session()` →
`load_cli_config(user_override=...)` (`cli_session.py:186`) with
`relay_url_override` left at its default, `RELAY_URL_UNSET`
(`config.py:1339`) — which means `_load_base_config` always runs its normal
file+env resolution chain for every subcommand today; `wall` never opts out
of it. Because `_apply_env_relay_overrides` lives inside `_load_base_config`
itself (§1), it is automatically live for `wall`, `write`, `read`, `plan`,
`tty`, and every future command, with zero changes to `__main__.py`'s
per-command signatures.

This is deliberate, not incidental: env vars are a *global* deployment-context
signal ("this process is running as CI / in this container / under this
systemd unit"), not a per-invocation choice like `--relay-url` is for
`serve`/`mcp`. A flag would need to be threaded onto every one of the ~15
`@app.command()` functions in `__main__.py` and would still need someone to
remember to pass it on every invocation; an env var set once in the
workflow's `env:` block (or a container's environment) applies to every
command automatically, which is exactly the "same relay a team's Claude Code
plugin sessions actually use" requirement — the plugin's MCP server session
and a CI `wall` invocation should require identical configuration effort,
not "flags for MCP, something else for CLI, nothing for `wall`."

## 5. Security handling

The constraint is explicit and non-negotiable per the mission brief: this
session had a real secret-leak incident with this exact relay's token.
Concrete requirements:

1. **Read via `os.environ.get`, never argv.** Environment variables never
   appear in `ps aux` output the way CLI arguments do, and GitHub Actions
   masks any log line containing the literal value of a step's `env:` entry
   sourced from `secrets.*` — this is why §7 puts the token in `env:`, not
   in the `uvx ... wall "..."` command string.
2. **`_apply_env_relay_overrides` logs only which env vars fired, never their
   values.** Pattern: `logger.info("relay override: BIFF_RELAY_URL set")`,
   `logger.info("relay override: BIFF_RELAY_TOKEN set")` — the *name*, never
   `%s` on the secret itself. The existing `SystemExit` message for the
   auth-conflict case must follow the same rule: name the conflicting env
   vars, never their values (mirrors `_extract_relay`'s existing message,
   which already only names keys, `config.py:905-910`).
3. **`RelayAuth`'s default `dataclass` repr is a live leak, not a hypothetical
   one, and predates this proposal.** `RelayAuth` (`models.py:174`) is a
   plain `@dataclass(frozen=True)` with no `repr=False` on any field — any
   accidental `logger.debug("config: %r", config)`, any uncaught exception
   whose traceback reprs a `BiffConfig`/`RelayAuth` instance, any future
   `--verbose` dump of the resolved config prints the token in clear text.
   Today this risk is latent because the token can only originate from a
   file already excluded from CI by sparse-checkout; once `BIFF_RELAY_TOKEN`
   makes CI-carried secrets flow through this same object, the risk becomes
   live. **This proposal requires `RelayAuth`'s three fields marked
   `field(repr=False)`** (or an explicit `__repr__` that prints `token=***`)
   as a prerequisite, not a follow-up — implementing the env-var override
   without fixing the repr ships a secret-carrying object with a
   secret-printing `repr()` into a code path CI now touches directly.
4. **Never write the token to disk.** No implementation path may write
   `BIFF_RELAY_TOKEN`'s value into `config.local.yaml` or any cache file —
   this is exactly why rejected-alternative (a) in §6 loses to this design.
5. **`biff doctor`'s existing relay-reachability output must not echo
   auth values.** `doctor.py:208` already reports only `relay_url` in its
   success/failure message — confirm this holds after wiring `doctor.py`
   into the shared resolution (§1); it must keep reporting the URL only,
   never the resolved `RelayAuth`.
6. **`repr=False` does not close the leak by itself — round 2 amendment.**
   Round 1 review (djb) confirmed the gap: `RelayAuth.as_nats_kwargs()`
   (`models.py:191-199`) converts the token into a plain `dict[str, str]`
   immediately before every real `nats.connect()` call
   (`nats_relay.py:801-811`, `doctor.py:166-178`). That plain dict is a local
   variable named `kwargs` (or `self._auth_kwargs()`'s unpacked result) that
   survives for the duration of the connect attempt. If anything inside that
   window raises — and both call sites have a nearby broad exception handler
   (`nats_relay.py:823 except Exception as exc`, `doctor.py:185
   except Exception:  # noqa: BLE001`) — the raw token sits in that frame's
   locals regardless of `RelayAuth.__repr__`. A pytest failure capturing
   locals, `--debug`, or any future structured error report that walks the
   traceback prints the token in clear text.

   Requirement: every call site that expands `**self._auth_kwargs()` (or
   equivalent) into `nats.connect(...)` must wrap the connect attempt in a
   narrow `try/except` that raises a **new**, redacted exception with
   `from None`, and must clear the plain dict in a `finally` block —
   not a bare re-raise, and not `except Exception as exc:` followed by
   `raise` or `logger.exception(...)` on `exc` itself. Concretely:

   ```python
   auth_kwargs = self._auth_kwargs()
   try:
       nc = await nats.connect(url, **auth_kwargs, **tls_kwargs)
   except Exception as exc:  # noqa: BLE001 — boundary: never let raw auth
                              # kwargs escape via this frame's traceback
       raise RelayConnectError(f"failed to connect to relay: {url}") from None
   finally:
       auth_kwargs.clear()
   ```

   **`from None` alone is not sufficient — round 2 evaluator correction
   (djb).** `from None` sets `__suppress_context__`, which only changes how
   `traceback` and most loggers *display* an exception chain by default. It
   does not clear `__context__`: the original exception, its traceback, and
   the frame locals holding the raw `auth_kwargs` dict remain reachable to
   anything that walks `__context__` directly — a custom error reporter, a
   debugger, an APM/Sentry-style integration that ignores
   `__suppress_context__` by design, or `traceback.format_exception(chain=True)`
   called explicitly. Verified empirically: `err.__context__.__traceback__
   .tb_frame.f_locals["auth_kwargs"]` still yields the live dict with the raw
   token after a `raise ... from None`.

   The `finally: auth_kwargs.clear()` closes this structurally rather than
   relying on a display flag: clearing the dict in place empties it in every
   frame that references the same object, including the original exception's
   traceback frame, so there is no raw token left to reach regardless of how
   the exception is later inspected. This is a fourth security mechanism, in
   addition to (not instead of) the `repr=False` fix in item 3 above —
   `repr=False` prevents an *accidental* print/log of the `RelayAuth` object;
   `finally: auth_kwargs.clear()` plus `from None` prevents an *exceptional*
   traceback from exposing the post-`as_nats_kwargs()` plain dict, which
   `repr=False` cannot reach because it is no longer a `RelayAuth` instance by
   that point.

## 6. Rejected alternatives

### (a) CI-runtime patching of `config.local.yaml` from a secret

Have the workflow write a `.punt-labs/biff/config.local.yaml` from
`${{ secrets.BIFF_RELAY_TOKEN }}` before invoking `biff`, e.g. a `run:` step
that shells out `yq` or a heredoc to materialize the file.

**Rejected because:**

- `biff-notify.yml`'s checkout is `sparse-checkout: .punt-labs/biff` — the
  *directory* exists in the runner's working copy, so this is at least
  mechanically possible, but it means the workflow becomes responsible for
  YAML-authoring logic that duplicates `write_yaml_config`
  (`config.py:802-818`) — a second, workflow-local implementation of a
  concern `config.py` already owns, with all the drift risk that implies
  (this is the same "two writers of one truth" failure class flagged for
  `doctor.py` in §1).
- **The secret lands on disk**, even if the step is careful to avoid an `ls`
  or `cat` afterward. Any GitHub Actions runner is a shared/reused VM image
  for `ubuntu-latest`; any workflow debugging step (`actions/upload-artifact`
  aimed too broadly, a crash dump, a coredump-on-panic) that captures the
  working directory now captures the token in plaintext. An environment
  variable sourced from `secrets.*` never touches the filesystem at all —
  GitHub Actions injects it directly into the process environment of the
  `run:` step.
- Cleanup is fallible: a `run:` step that fails *before* its own cleanup
  step runs leaves the plaintext file on the runner's disk for the remainder
  of the job (subsequent steps, artifact uploads) — an env var has no
  equivalent "forgot to delete it" failure mode.
- It does not compose across the eventual non-CI headless cases (a
  systemd unit, a Docker container running `biff wall` on a schedule) as
  cleanly — those need config-file *management* infrastructure (mounting,
  writing, permissions) that env vars just don't require.

### (b) Extend `--relay-url` with a paired `--relay-token` flag

Add `--relay-token` (and by extension `--relay-nkeys-seed`,
`--relay-user-credentials`, `--relay-tls-handshake-first`) alongside the
existing `--relay-url` on `serve`/`mcp`, and additionally add the same set
of flags to `wall` (and every other CLI command CI might invoke).

**Rejected because:**

- **Command-line arguments are not secret-safe the way `env:` is.**
  GitHub Actions' log-masking for `secrets.*` values works reliably when a
  secret is referenced via `env:` and read from the environment; a secret
  interpolated directly into a `run:` command string
  (`biff wall ... --relay-token ${{ secrets.BIFF_RELAY_TOKEN }}`) is a
  documented GitHub Actions anti-pattern — masking is substring-based and
  brittle against shell quoting/escaping, and the fully-expanded command is
  independently visible via `ps aux` (or `/proc/<pid>/cmdline`) to any other
  process on the same runner for the argument's lifetime, which an
  environment variable read once via `os.environ.get` is not (env vars are
  visible via `/proc/<pid>/environ` too, but GitHub-hosted runners are
  single-job, single-tenant for the duration of the job, and this is the
  same exposure surface `secrets.*` already accepts as its baseline threat
  model for `env:` — CLI args add a *second*, needless surface on top).
- **Every command needs the flag, defeating the point of §4.** Unlike env
  vars, which apply globally once set in the job's `env:` block, a
  `--relay-token` flag only helps the one invocation it's attached to. `wall`
  today has zero relay flags; adopting this alternative means adding four new
  flags to `wall` *and* every other CLI command CI might someday invoke, in
  perpetuity, as new commands are added — the exact per-command threading
  cost §4 explicitly avoids.
- **It reopens the `--relay-url`-clears-auth footgun for every future
  caller.** `--relay-url` already has the PR #383-hardened behavior of
  wiping `relay_auth`/`relay_tls_handshake_first` on override (§1). Making
  `--relay-token` a sibling flag means every future consumer of `--relay-url`
  must remember to pass the matching auth flag in the same breath, or
  silently regress to unauthenticated/wrong-TLS-mode connections — precisely
  the bug class #383 fixed, reintroduced at the CLI surface instead of the
  config-file surface. An env var pair (`BIFF_RELAY_URL` +
  `BIFF_RELAY_TOKEN`) set once in a workflow's `env:` block is set together
  by construction; two independent flags on N commands are not.
- Shell history: interactive use of a `--relay-token` flag leaves the token
  in `~/.bash_history`/`~/.zsh_history` for any human operator who types it
  directly rather than through a workflow's `env:` block.
  **This is not a point in favor of exported env vars either** — see §0,
  which retracts the idea that a human `export`ing `BIFF_RELAY_TOKEN` once
  per shell session is a safe alternative. Both a typed `--relay-token` flag
  and an exported env var are footguns for interactive human use; the
  difference is that `--relay-token` additionally fails on the CLI-args
  exposure surface described above. Env vars remain the right mechanism, but
  strictly for non-interactive, single-identity processes (CI runners,
  containers, systemd units) as scoped by §0 — never for a human's
  multi-repo interactive shell.

## 7. `biff-notify.yml` diff

**Round 2 amendment — `workflow_run` + fork-PR secret-safety analysis
(previously missing, explicitly requested in the mission brief).** Round 1
review (djb) verified the existing `if:` gate at `biff-notify.yml:16-18`
(`github.event.workflow_run.conclusion == 'failure' &&
github.event.workflow_run.event == 'push'`) is, today, sufficient: it
excludes `workflow_run` completions whose triggering run originated from a
`pull_request` event — including fork PRs — which is GitHub's documented
mitigation for the classic "`workflow_run` + secrets + untrusted fork" attack
class (a fork PR can influence the *source* run's code, but the notify job
that reads `secrets.*` never fires for that source event, only for `push`
completions on branches in this repository). Also verified: the diff below
reads `WORKFLOW_NAME`/`BRANCH`/`RUN_URL` via `env:` + `"${VAR}"` shell
expansion rather than direct `${{ }}` interpolation into `run:`, so there is
no script-injection route through those values either.

**What changes here is the stakes, not the mechanism.** Before this
proposal, `event == 'push'` was purely a notification-scope choice — get it
wrong, and CI notifications fire for the wrong runs. After this proposal,
`secrets.BIFF_RELAY_TOKEN` lives in the same step's `env:` block, so
`event == 'push'` is now **also** the boundary that keeps that secret away
from any workflow run traceable to a fork PR. A future change that widens
this filter — e.g. "also notify on `pull_request`-triggered failures, so
contributors see CI status sooner" — would, without realizing it, reopen
fork-triggered access to the CI relay token. That must not happen silently;
the gate needs a comment saying so, both in this diff and in the merged
workflow file.

```diff
--- a/.github/workflows/biff-notify.yml
+++ b/.github/workflows/biff-notify.yml
@@ -14,7 +14,11 @@ jobs:
   notify:
+    # event == 'push' is a secret-safety boundary, not just a notification
+    # filter: this step's env: block carries secrets.BIFF_RELAY_TOKEN.
+    # Widening this to include pull_request-sourced workflow_run completions
+    # (e.g. to notify on fork-PR CI failures) reopens fork access to that
+    # token unless paired with an explicit
+    # `github.event.workflow_run.head_repository.full_name ==
+    # github.repository` check, per GitHub's workflow_run-with-secrets
+    # guidance. Do not relax this filter without adding that check.
     if: >-
       github.event.workflow_run.conclusion == 'failure'
       && github.event.workflow_run.event == 'push'
@@ -22,10 +26,14 @@ jobs:
       - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4
         with:
           sparse-checkout: .punt-labs/biff
       - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0
       - env:
           WORKFLOW_NAME: ${{ github.event.workflow_run.name }}
           BRANCH: ${{ github.event.workflow_run.head_branch }}
           RUN_URL: ${{ github.event.workflow_run.html_url }}
+          BIFF_RELAY_URL: ${{ vars.BIFF_RELAY_URL }}
+          BIFF_RELAY_TOKEN: ${{ secrets.BIFF_RELAY_TOKEN }}
+          BIFF_RELAY_TLS_HANDSHAKE_FIRST: ${{ vars.BIFF_RELAY_TLS_HANDSHAKE_FIRST }}
         run: >-
           uvx --from punt-biff biff --user github-actions wall
           "CI failed: ${WORKFLOW_NAME} on ${BRANCH} — ${RUN_URL}"
           --duration 2h
```

Notes on this diff:

- `BIFF_RELAY_URL` and `BIFF_RELAY_TLS_HANDSHAKE_FIRST` come from repository
  **variables** (`vars.*`), not secrets — a relay URL and a boolean TLS mode
  are not sensitive, and using `vars.*` avoids the (small, but nonzero) risk
  of over-classifying non-secret config as a secret and losing visibility
  into what's configured (`secrets.*` values render as `***` even in the
  Settings UI's own list; `vars.*` don't).
- `BIFF_RELAY_TOKEN` comes from `secrets.BIFF_RELAY_TOKEN`, which must be
  configured as a repository or org secret pointing at a credential scoped
  to whatever the team's actual relay grants CI (least privilege — a
  CI-scoped token, not the same token a human's `config.local.yaml` carries,
  so rotating or revoking CI's access never touches a human's session).
- If `vars.BIFF_RELAY_URL` is unset (repo hasn't opted in yet), the `env:`
  entry evaluates to an empty string, `_apply_env_relay_overrides` treats an
  empty `BIFF_RELAY_URL` as "not set" (consistent with how `config.py`
  already treats absent-vs-empty elsewhere, e.g. `_has_orgs_key`), and
  behavior is byte-for-byte identical to today: the workflow degrades to the
  demo relay, not an error. No repo is forced onto this mechanism to keep
  `biff-notify.yml` working.
- No changes to `on.workflow_run.workflows`, `permissions`, or the *logic* of
  the `if:` gate — this proposal is scoped to the relay-connection step plus
  the one guardrail comment on the gate documented above. The comment is not
  optional decoration: it is the record of why `event == 'push'` may not be
  relaxed without an accompanying `head_repository.full_name == repository`
  check, now that a real secret sits downstream of it.

## Open questions for the reviewer (djb)

1. Does the CI-scoped relay token need its own NATS user/permission set on
   the relay side (write-only to the `wall` subject, no read access to
   others' inboxes), independent of this config-resolution design? This doc
   assumes the token's *scope* is a relay-side authorization concern outside
   `biff`'s config layer, but it's worth confirming that assumption doesn't
   quietly become "CI has the same relay privileges as every human."
2. Should `_apply_env_relay_overrides` reject a `BIFF_RELAY_NKEYS_SEED` or
   `BIFF_RELAY_USER_CREDENTIALS` path that doesn't exist at resolution time
   (fail fast, before attempting to connect), the same way `RelayAuth` today
   defers that check to `nats.connect()`? CI failures from a missing
   credentials file would otherwise surface as an opaque NATS connection
   error rather than a clear config error.
