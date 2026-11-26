# Nostr trial build (PL-1661)

A spike, not shipped code. It proves in Python the two Nostr pieces the
biff trust model (`docs/design/trust-model.md`, DES-094) depends on, before
production code (PL-1656) commits to them:

1. NIP-OA owner attestation, against every vector Buzz publishes.
2. NIP-17 private DMs (NIP-59 gift wrap, NIP-44 v2 encryption), against the
   official NIP-44 vectors and an independent second implementation.

Buzz is checked out at commit `298543f` (`git clone https://github.com/block/buzz
.tmp/buzz`, then `git checkout 298543f`). Nothing here is added to biff's own
dependency set; this directory has its own `pyproject.toml`.

## Run

```bash
cd spikes/nostr-trial
uv run --extra candidates pytest -v
```

Without `--extra candidates`, collecting `test_monstr.py` fails with
`ModuleNotFoundError`; the extra installs the runner-up candidate. The
directory pins Python 3.13 in `.python-version` (see the coincurve finding).

## Files

| File | What it is |
|------|------------|
| `nip_oa.py` | Minimal NIP-OA: conditions grammar, preimage, tag verify, event id and signature check. coincurve backend. |
| `test_nip_oa.py` | Buzz's NIP-OA valid vector, signed event, six invalid vectors, and the condition-grammar cases from `buzz-sdk/src/nip_oa.rs`. |
| `nip44_ref.py` | NIP-44 v2 in about 80 lines: coincurve for ECDH, `cryptography` for ChaCha20, stdlib for HKDF, HMAC and base64. |
| `nip44.vectors.json` | Official vectors from `github.com/paulmillr/nip44`, vendored unchanged apart from JSON whitespace. |
| `test_nip44.py` | All official NIP-44 v2 vectors against `nip44_ref`; encrypt/decrypt vectors against nostr-sdk; nostr-sdk and `nip44_ref` against each other. |
| `test_nip17.py` | Gift-wrap round trips in both directions between nostr-sdk and plain Python. |
| `test_monstr.py` | The runner-up library against the same NIP-44 vectors. |

## Library findings

Data gathered 2026-11-26. Licence and Python support come from the installed
wheel metadata. Maintenance signals come from the GitHub repositories. PyPI's
JSON API was not reachable from this session, so PyPI upload dates are not
recorded; the repository's last push stands in for them.

| Library (PyPI) | Version | Licence | Repo last push | Python 3.13 | Native code | NIP-44 v2 | NIP-59 / NIP-17 |
|----------------|---------|---------|----------------|-------------|-------------|-----------|-----------------|
| `nostr-sdk` | 0.45.2 | MIT | 2026-11-14 (`nostrdevkit/nostr-sdk-ffi`, wraps `rust-nostr/nostr`, pushed 2026-11-26, 685 stars) | Yes: one `cp39-abi3` wheel per platform, so 3.13 and 3.14 both work | Yes: a Rust shared library (`libnostr_sdk_ffi`) through UniFFI | Yes | Yes: `nip59_make_seal`, `nip59_make_gift_wrap`, `nip17_make_private_msg`, `UnwrappedGift.from_gift_wrap` |
| `monstr` | 0.1.9 | MIT | 2024-07-16 | Pure Python wheel; no classifiers. Needs `secp256k1` (native) and `pycryptodome` (native) | Indirect | Yes (`NIP44Encrypt`) | NIP-59 `GiftWrap` class, async signer only; no NIP-17 helper |
| `pynostr` | 0.7.0 | MIT | 2025-08-07 | Pure Python; classifiers stop at 3.11 | Indirect: `coincurve`, `cryptography` | No (NIP-04 only) | No |
| `nostr` (python-nostr) | 0.0.2 | MIT | 2024-08-14 | Pure Python; needs `secp256k1` | Indirect | No (NIP-04 only) | No |
| `nostr-tools` (bigbrotr) | 1.4.1 | MIT | 2025-12-01 (3 stars) | Yes, classifier lists 3.13 | Indirect: `secp256k1` | No | No |
| `nostr-protocol` | 0.35.0 | MIT | (older name of `nostr-sdk`) | No: wheels for cp39 to cp312 only | Yes | n/a | n/a, superseded by `nostr-sdk` |

Python 3.14 (dry-run resolution with wheels only, and a real `uv run` on
3.14.0): `nostr-sdk` installs from its `abi3` wheel. `coincurve` 21.0.0 has no
cp314 wheel on any platform; `uv` fell back to the sdist and the build failed
with `Expected exactly one LICENSE file in cffi distribution`. `secp256k1`
0.14.0 keeps its 3.13 gaps (no Linux aarch64, no Windows wheel). The
pure-Python packages (`monstr`, `pynostr`, `nostr`, `nostr-tools`) install on
3.14 but pull `secp256k1` or `coincurve` underneath.

Raw Schnorr verification: `nostr-sdk` 0.45.2 has no standalone BIP-340 verify.
It exposes `Keys.sign_schnorr` and `Event.verify()` (event id plus signature),
and nothing that checks a signature over an arbitrary 32-byte message against
an x-only public key. NIP-OA's owner signature is exactly that, so
PL-1656 needs a separate backend for it.

Checked by reading each package's modules for `nip44`, `chacha`, `gift` and
`seal`: only `monstr` and `nostr-sdk` contain NIP-44 or NIP-59 code. No package
named `nip44` exists on PyPI.

| BIP-340 backend (PyPI) | Version | Licence | Repo last push | Native wheels for 3.13 | Notes |
|------------------------|---------|---------|----------------|------------------------|-------|
| `coincurve` | 21.0.0 | MIT OR Apache-2.0 | 2026-08-12 (latest release 2025-03-08) | macOS arm64 and x86_64, Linux x86_64 and aarch64, Windows: all resolve | Bundles libsecp256k1 (CFFI). Has `sign_schnorr`, `PublicKeyXOnly.verify`, `multiply` for ECDH. **No cp314 wheel on any platform**: on Python 3.14 uv builds from source and the build failed here (`Expected exactly one LICENSE file in cffi distribution`). |
| `secp256k1` (`secp256k1-py`) | 0.14.0 | MIT | 2026-02-12 | macOS arm64 and Linux x86_64 only. **No Linux aarch64 and no Windows wheel** | Bundles libsecp256k1 (CFFI). Used by `monstr`, `nostr`, `nostr-tools`. |

## Interoperability with Buzz

Buzz publishes no NIP-17 or NIP-44 cryptographic fixtures. What it pins is the
wire shape of a gift wrap (`crates/buzz-test-client/tests/e2e_nostr_interop.rs`,
`NOSTR.md`): a kind `1059` event signed by an ephemeral key, so its author is
not the sender; addressed by a `p` tag to the recipient; delivered by
`#p`-filtered subscriptions; never indexed for search. Buzz does not implement
NIP-04 or NIP-44 outside gift wraps. `test_nip17.py` asserts that shape, and
decrypts a wrap with an independent implementation of NIP-44 that passes the
official vectors.

The NIP-OA vectors are Buzz's own (`docs/nips/NIP-OA.md`): one valid vector
with preimage, SHA-256, signature, tag and tag bytes; one signed event; six
invalid vectors; plus the condition-grammar cases in
`crates/buzz-sdk/src/nip_oa.rs`.

## Recommendation

1. **NIP-17, NIP-44 v2 and NIP-59: `nostr-sdk`.** It is the only maintained
   package that has all three (its repository was pushed 12 days before this
   run and wraps `rust-nostr`, pushed the day of this run). It passes the official NIP-44 payloads through its public
   API, and its gift wraps unwrap with an independent implementation and the
   reverse. The cost is a native Rust library inside the wheel. The wheel is
   `abi3`, so it installs on Python 3.13 and 3.14 on macOS (arm64, x86_64),
   Linux (x86_64, aarch64) and Windows without a compiler.
2. **BIP-340 backend: `coincurve`.** It runs all 53 NIP-OA vectors. Pin
   Python to 3.13 for now: coincurve 21.0.0 ships no cp314 wheel, and on
   Python 3.14 `uv` builds it from source, which failed in this spike.
   Production code must either cap `requires-python` below 3.14 until
   coincurve ships 3.14 wheels. `nostr-sdk` cannot replace it: it has no raw
   Schnorr verify (see above), which NIP-OA needs.
3. **Rejected: `secp256k1` (`secp256k1-py`).** Its 0.14.0 wheels cover macOS
   arm64 and Linux x86_64 only; Linux aarch64 and Windows resolve to no wheel.
4. **Rejected: `pynostr`, `python-nostr`, `nostr-tools`.** None has NIP-44 or
   NIP-59. `monstr` has both and passes the vectors, but it was last pushed in
   July 2024 and offers no NIP-17 helper.
5. **Fallback if the Rust binary is unacceptable:** `nip44_ref.py` plus
   `coincurve` and `cryptography`. It is about 80 lines and passes every
   official NIP-44 v2 vector (padding, conversation key, message keys,
   encrypt and decrypt, long messages, all invalid cases). The gift-wrap
   layering is another 60 lines (see `test_nip17.py`, the Python-built wrap).

## Results

Run on 2026-11-26, Python 3.13.1, macOS arm64, 1-minute load average 11 at
the start of the run (the limit for running tests was 12):

```text
$ uv run --extra candidates pytest -v
collected 236 items
... 236 passed in 0.47s
```

| File | Tests | Result |
|------|-------|--------|
| `test_nip_oa.py` | 53 | All pass: published valid vector, signed event, six invalid vectors, grammar and time-clause cases |
| `test_nip44.py` | 160 | All pass: every official v2 vector against `nip44_ref`; payloads and round trips against nostr-sdk |
| `test_nip17.py` | 3 | All pass: nostr-sdk round trip; independent Python unwrap of an nostr-sdk wrap; nostr-sdk unwrap of a Python-built wrap |
| `test_monstr.py` | 20 | All pass |

What the first runs got wrong, kept here because production code will hit the
same things:

- `nip44_ref.padded_length` failed five official `calc_padded_len` vectors
  (400, 515, 700, 800, 900) on the first full run. The spec's chunk size is
  `next_power / 8` where `next_power` is the next power of two at or above the
  length, and the threshold tests `next_power`, not the length. The HKDF
  expand step also needs three 32-byte blocks (76 bytes), not two.
- `NostrSigner` in nostr-sdk 0.45.2 is a protocol, not a class with a
  `keys()` constructor; a `Keys` instance is passed directly.
- Buzz's NIP-OA signature is not reproduced byte for byte by signing the same
  preimage again, because BIP-340 signing mixes in aux randomness. The test
  asserts the published signature verifies and that a freshly signed tag
  verifies, which is what the spec requires.
- Buzz has no NIP-17 or NIP-44 test vectors. Interoperability with Buzz for
  DMs rests on the wire shape (kind 1059, ephemeral author, `p` tag) and on
  the official NIP-44 vectors, not on a Buzz fixture.
