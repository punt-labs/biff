# Biff documentation

Each concern has one owner document. Others point to it rather than restate it.
Decisions are recorded as ADRs in [`DESIGN.md`](../DESIGN.md) (runtime) and
[`design/installer.md`](design/installer.md) (installation).

## Owners

| Concern | Owner | ADR |
|---|---|---|
| Message trust: keys, owner attestation, the rwx address book, the floor, the wire format over NATS | [design/trust-model.md](design/trust-model.md) | DES-094 |
| biffd, the per-user daemon, and its thin clients | [design/biffd.md](design/biffd.md) | DES-095 |
| Channel delivery to Claude Code | [design/design-channels.md](design/design-channels.md) | DES-087 |
| Installation: install, enable, doctor, uninstall | [design/installer.md](design/installer.md) | INS-001 to INS-011 |
| Package layers and the plugin contract | [design/package-structure.md](design/package-structure.md) | DES-088 |
| Product vision, phasing and what biff does not build | [`prfaq.tex`](../prfaq.tex) | — |
| Testing tiers and markers | [`TESTING.md`](../TESTING.md) | — |
| What reviewers should and should not raise | [`REVIEW.md`](../REVIEW.md) | — |

## Layout

| Folder | Holds |
|---|---|
| [design/](design/) | Current design documents, one per concern |
| [specs/](specs/) | Z specifications (`*.tex`) and their fuzz and ProB outputs |
| [guides/](guides/) | Installing, setup, troubleshooting, the library API, the FAQ and running a relay |
| [coe/](coe/) | Correction-of-error analyses of past incidents |
| [api-reference/](api-reference/) | Generated API reference |

Type-check and model-check a spec with `make fuzz SPEC=docs/specs/<name>.tex`
and `make prob SPEC=docs/specs/<name>.tex`.

Planned work lives in Linear (team PL, label `repo:biff`), not in this tree.
