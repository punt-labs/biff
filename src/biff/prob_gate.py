"""Run ProB gates without filtering output or losing checker failures."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FAILURE = re.compile(
    r"(?im)(?:^!|\*\*\*.*(?:error|deadlock|counter)|"
    r"^deadlock_counter_example_found|(?<!No )\bcounter[- ]?example (?:found|exists)|"
    r"\b(?:time[d ]?out|timeout|not all states|not all open states)\b)"
)
_STEPS = (
    ("init", ["-init"], None),
    ("animate", ["-animate", "50"], None),
    ("assertions", ["-cbc_assertions"], "No counter-example to ASSERTION exists"),
    ("deadlock", ["-cbc_deadlock"], "No DEADLOCK state found"),
    (
        "model-check",
        [
            "-model_check",
            "-p",
            "MAX_INITIALISATIONS",
            "100",
            "-p",
            "MAX_OPERATIONS",
            "5000",
        ],
        "No counter example found. ALL states visited.",
    ),
)


def check_output(output: str, returncode: int, proof: str | None) -> bool:
    """Require successful execution, no violations, and a completed proof."""
    plain = _ANSI.sub("", output)
    return (
        returncode == 0
        and _FAILURE.search(plain) is None
        and (proof is None or proof in plain)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probcli", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--setsize", type=int, default=2)
    parser.add_argument("--maxint", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=60000)
    parser.add_argument("--log-dir", type=Path, default=Path(".tmp/prob"))
    args = parser.parse_args(argv)
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
    for name, operation, proof in _STEPS:
        print(f"--- {name} ---", flush=True)  # noqa: T201
        log_path = args.log_dir / f"{Path(args.spec).stem}-{name}.log"
        try:
            result = subprocess.run(  # noqa: S603
                [args.probcli, args.spec, *operation, *flags],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=max(1, args.timeout / 1000) + 30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
