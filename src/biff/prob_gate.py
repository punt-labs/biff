"""Run ProB gates without filtering output or losing checker failures."""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FAILURE = re.compile(
    r"(?im)(?:^!|\*\*\*.*(?:error|deadlock|counter)|"
    r"^deadlock_counter_example_found|(?<!No )\bcounter[- ]?example (?:found|exists)|"
    r"\b(?:not all states|not all open states)\b)"
)
_TIMEOUT = re.compile(
    r"(?im)^\s*(?:[%!*]+\s*)?"
    r"(?:(?:warning|error)\s*:\s*)?"
    r"(?:(?:model checking|model check|constraint solving|constraint solver|"
    r"operation|execution|solver|ProB(?: execution)?)\s+(?:(?:has|was)\s+)?)?"
    r"(?:timed[ -]?out|time[ -]?out|timeout)(?=\s|:|[.!*]|$)"
)
_MODEL_CHECK_PROOF = "No counter example found. ALL states visited."
_COVERAGE = re.compile(
    r"(?m)^Coverage:[ \t]*\n"
    r"\[STATES \([0-9]+\),([^\n\]]*),TOTAL_TRANSITIONS,[0-9]+,"
    r"[^\n\]]*\][ \t]*$"
)
_SAFETY_COUNTERS = ("deadlocked", "invariant_violated", "invariant_not_checked", "open")
_STEPS = (
    ("init", ["-init"], None),
    ("animate", ["-animate", "50"], None),
    ("assertions", ["-cbc_assertions"], "No counter-example to ASSERTION exists"),
    ("deadlock", ["-cbc_deadlock"], "No DEADLOCK state found"),
    (
        "model-check",
        [
            "-model_check",
            "-coverage",
            "-p",
            "MAX_INITIALISATIONS",
            "100",
            "-p",
            "MAX_OPERATIONS",
            "5000",
        ],
        _MODEL_CHECK_PROOF,
    ),
)
_SESSION_PREDICATES = (
    "smach : SESSION +-> HOST",
    "pkind : PROCESS +-> KIND",
    "ttyalias : SESSION * KIND >+> ALIAS",
    "abind : ALIAS +-> SESSION * KIND",
    "dom(psess) = processes",
    "dom(sbranch) = sessions",
    "dom(entries) <: sessions",
    "dom(ttyalias) = entries",
    "routeMode = rmIdentity => lostF = zfalse",
    "routeMode = rmIdentity => misF = zfalse",
)


def check_session_invariant(translation: str) -> bool:
    """Reject a session translation checking only underlying relation types."""
    _, invariant_marker, rest = translation.partition("\nINVARIANT\n")
    invariant, init_marker, _ = rest.partition("\nINITIALISATION\n")
    normalized = " ".join(invariant.replace("(", " ( ").replace(")", " ) ").split())
    normalized = normalized.replace(" ( ", "(").replace(" )", ")")
    return bool(invariant_marker and init_marker) and all(
        predicate in normalized for predicate in _SESSION_PREDICATES
    )


def check_coverage(output: str) -> bool:
    """Require one state summary with explicit zero safety counters."""
    summaries = _COVERAGE.findall(output)
    if len(summaries) != 1:
        return False
    fields = [field.partition(":") for field in summaries[0].split(",")]
    return all(
        [field for field in fields if field[0] == counter] == [(counter, ":", "0")]
        for counter in _SAFETY_COUNTERS
    )


def check_output(output: str, returncode: int, proof: str | None) -> bool:
    """Require successful execution, no violations, and a completed proof."""
    plain = _ANSI.sub("", output)
    return (
        returncode == 0
        and _FAILURE.search(plain) is None
        and _TIMEOUT.search(plain) is None
        and (proof is None or proof in plain)
        and (proof != _MODEL_CHECK_PROOF or check_coverage(plain))
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Validate scope and independent operation and wall time limits."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probcli", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--setsize", type=int, default=2)
    parser.add_argument("--maxint", type=int, default=4)
    parser.add_argument(
        "--timeout", type=int, default=60000, help="ProB per-operation timeout in ms"
    )
    parser.add_argument(
        "--wall-timeout",
        type=float,
        default=None,
        help="Optional wall timeout in seconds for each checker subprocess",
    )
    parser.add_argument("--log-dir", type=Path, default=Path(".tmp/prob"))
    args = parser.parse_args(argv)
    if args.setsize <= 0:
        parser.error("--setsize must be positive")
    if args.maxint < 0:
        parser.error("--maxint must be nonnegative")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.wall_timeout is not None and (
        not math.isfinite(args.wall_timeout) or args.wall_timeout <= 0
    ):
        parser.error("--wall-timeout must be finite and positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    flags = [
        "-strict",
        "-p",
        "DEFAULT_SETSIZE",
        str(args.setsize),
        "-p",
        "MAXINT",
        str(args.maxint),
        "-p",
        "TIME_OUT",
        str(args.timeout),
    ]
    translation_path = args.log_dir / "session-model-translation.mch"
    steps: tuple[tuple[str, list[str], str | None], ...] = _STEPS
    if Path(args.spec).stem == "session-model":
        translation_path.unlink(missing_ok=True)
        steps = (("translation", ["-ppf", str(translation_path)], None), *steps)
    for name, operation, proof in steps:
        print(f"--- {name} ---", flush=True)  # noqa: T201
        log_path = args.log_dir / f"{Path(args.spec).stem}-{name}.log"
        try:
            result = subprocess.run(  # noqa: S603
                [args.probcli, args.spec, *operation, *flags],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=args.wall_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # TimeoutExpired.output can be bytes even with text=True. Retain
            # the original bytes in the log, decoding only for console output.
            output = exc.output or b""
            raw = output.encode() if isinstance(output, str) else output
            separator = b"\n" if raw and not raw.endswith(b"\n") else b""
            explanation = f"ProB execution timed out: {exc}\n".encode()
            combined = raw + separator + explanation
            log_path.write_bytes(combined)
            print(  # noqa: T201
                combined.decode(errors="replace"), file=sys.stderr, end="", flush=True
            )
            return 1
        except OSError as exc:
            log_path.write_text(f"ProB execution failed: {exc}\n")
            print(log_path.read_text(), file=sys.stderr, end="")  # noqa: T201
            return 1
        log_path.write_text(result.stdout)
        print(result.stdout, end="", flush=True)  # noqa: T201
        if not check_output(result.stdout, result.returncode, proof):
            print(  # noqa: T201
                f"ProB {name} FAILED (exit {result.returncode}); "
                f"full output: {log_path}",
                file=sys.stderr,
            )
            return 1
        if name == "translation" and (
            not translation_path.is_file()
            or not check_session_invariant(translation_path.read_text())
        ):
            print(  # noqa: T201
                "ProB translation FAILED: missing full session invariant; "
                f"inspect {translation_path}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
