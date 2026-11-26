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

`uv run pytest -v` without the extra skips nothing but fails to import
`test_monstr.py`; the extra installs the runner-up candidate.

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

RESULTS_PLACEHOLDER
