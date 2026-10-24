"""Formal gates fail closed rather than trusting filtered success banners."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from biff.prob_gate import (
    _SESSION_PREDICATES,
    check_output,
    check_session_invariant,
    main,
)


@pytest.mark.parametrize(
    "output",
    [
        "deadlock_counter_example_found(354,358)\n*** DEADLOCK state found ***",
        "\x1b[31m*** COUNTER EXAMPLE found ***\x1b[0m",
        "Counter-example exists",
        "! *** error occurred ***\n! Total Errors: 1, Warnings:0",
        "TIMEOUT",
        "not all open states visited",
    ],
)
def test_violations_fail_even_with_zero_exit_and_success_banner(output: str) -> None:
    assert not check_output(output + "\nprob-session: ALL CHECKS PASSED", 0, None)


def test_exit_status_and_incomplete_proof_are_failures() -> None:
    assert not check_output("No DEADLOCK state found", 1, "No DEADLOCK state found")
    assert not check_output("Starting check", 0, "No DEADLOCK state found")
    assert check_output(
        "no_deadlock_counter_example_found(1,1)\nNo DEADLOCK state found",
        0,
        "No DEADLOCK state found",
    )


def test_full_output_saved_and_failure_stops_later_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = "unfiltered diagnostics\ndeadlock_counter_example_found(354,358)\n"
    results = [
        subprocess.CompletedProcess(["probcli"], 0, "init\n"),
        subprocess.CompletedProcess(["probcli"], 0, "animate\n"),
        subprocess.CompletedProcess(
            ["probcli"], 0, "No counter-example to ASSERTION exists\n"
        ),
        subprocess.CompletedProcess(["probcli"], 0, output),
    ]
    with patch("biff.prob_gate.subprocess.run", side_effect=results) as run:
        assert (
            main(
                [
                    "--probcli",
                    "probcli",
                    "--spec",
                    "session.tex",
                    "--log-dir",
                    str(tmp_path),
                ]
            )
            == 1
        )
    assert run.call_count == 4
    assert "-strict" in run.call_args.args[0]
    assert (tmp_path / "session-deadlock.log").read_text() == output
    captured = capsys.readouterr()
    assert output in captured.out
    assert "deadlock FAILED" in captured.err
    assert not (tmp_path / "session-model-check.log").exists()


def test_missing_checker_fails(tmp_path: Path) -> None:
    with patch("biff.prob_gate.subprocess.run", side_effect=FileNotFoundError()):
        assert (
            main(
                [
                    "--probcli",
                    "missing",
                    "--spec",
                    "session.tex",
                    "--log-dir",
                    str(tmp_path),
                ]
            )
            == 1
        )


def test_make_gate_has_no_output_filtering() -> None:
    makefile = (Path(__file__).parents[1] / "Makefile").read_text()
    target = makefile.split("prob: ##", 1)[1].split("prob-session:", 1)[0]
    assert "|" not in target
    assert "biff.prob_gate" in target


def test_completed_model_check_is_not_a_counterexample() -> None:
    output = "No counter example found. ALL states visited."
    assert check_output(output, 0, output)


def test_session_translation_requires_actual_global_predicates() -> None:
    invariant = "\n  & ".join(_SESSION_PREDICATES)
    translated = f"MACHINE session\nINVARIANT\n{invariant}\nINITIALISATION\nskip"
    assert check_session_invariant(translated)
    assert not check_session_invariant(
        "MACHINE session\nINVARIANT\nabind : POW(ALIAS * (SESSION * KIND))"
        "\nINITIALISATION\n" + invariant
    )
    for predicate in _SESSION_PREDICATES:
        assert not check_session_invariant(translated.replace(predicate, "TRUE"))


def test_session_translation_failure_stops_before_proofs(tmp_path: Path) -> None:
    stale = "\n  & ".join(_SESSION_PREDICATES)
    (tmp_path / "session-model-translation.mch").write_text(
        f"MACHINE session\nINVARIANT\n{stale}\nINITIALISATION\nskip"
    )
    with patch(
        "biff.prob_gate.subprocess.run",
        return_value=subprocess.CompletedProcess(["probcli"], 0, "loaded\n"),
    ) as run:
        assert (
            main(
                [
                    "--probcli",
                    "probcli",
                    "--spec",
                    "session-model.tex",
                    "--log-dir",
                    str(tmp_path),
                ]
            )
            == 1
        )
    run.assert_called_once()
    assert "-ppf" in run.call_args.args[0]
    assert not (tmp_path / "session-model-init.log").exists()
    assert not (tmp_path / "session-model-translation.mch").exists()


def test_session_close_requires_cleanup_and_frames_routing() -> None:
    spec = (Path(__file__).parents[1] / "docs/session-model.tex").read_text()
    close = spec.split(r"\begin{schema}{CloseSession}", 1)[1].split(r"\end{schema}", 1)[
        0
    ]
    assert r"\Delta RouteWorld" in close
    assert r"s? \notin \dom entries" in close
    for variable in (
        "entries",
        "ttyalias",
        "savedalias",
        "abind",
        "routeMode",
        "lostF",
        "misF",
    ):
        assert f"{variable}' = {variable}" in close


def test_all_steps_complete_successfully(tmp_path: Path) -> None:
    outputs = [
        "init",
        "animate",
        "No counter-example to ASSERTION exists",
        "No DEADLOCK state found",
        "No counter example found. ALL states visited.",
    ]
    with patch(
        "biff.prob_gate.subprocess.run",
        side_effect=[
            subprocess.CompletedProcess(["probcli"], 0, output) for output in outputs
        ],
    ) as run:
        assert (
            main(
                [
                    "--probcli",
                    "probcli",
                    "--spec",
                    "session.tex",
                    "--log-dir",
                    str(tmp_path),
                ]
            )
            == 0
        )
    assert run.call_count == 5


@pytest.mark.parametrize("directory", ["timeout", "timed-out", "time-out"])
def test_healthy_paths_are_not_timeout_diagnostics(directory: str) -> None:
    output = (
        f"Writing fuzz AST to /biff/{directory}/docs/session-model.fuzz\n"
        f"Loaded /biff/{directory}/docs/session-model.tex\n"
        "No counter example found. ALL states visited.\n"
    )
    assert check_output(output, 0, "No counter example found. ALL states visited.")


@pytest.mark.parametrize(
    "diagnostic",
    [
        "TIMEOUT",
        "Time-out: operation did not finish",
        "% Timeout occurred",
        "*** TIME OUT ***",
        "Warning: timed-out",
        "% Model checking timed out after 60000 ms",
        "Constraint solver has timed out.",
        "ProB execution timed out: 60 seconds",
    ],
)
def test_actual_timeout_diagnostics_fail(diagnostic: str) -> None:
    assert not check_output(
        diagnostic + "\nNo counter example found. ALL states visited.",
        0,
        "No counter example found. ALL states visited.",
    )


@pytest.mark.parametrize(
    "output", [None, "checker diagnostics", b"checker diagnostics\n", b"raw\xff"]
)
def test_subprocess_timeout_preserves_output_before_explanation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], output: str | bytes | None
) -> None:
    exc = subprocess.TimeoutExpired("probcli", 60, output=output)
    with patch("biff.prob_gate.subprocess.run", side_effect=exc) as run:
        assert (
            main(
                [
                    "--probcli",
                    "probcli",
                    "--spec",
                    "session.tex",
                    "--log-dir",
                    str(tmp_path),
                ]
            )
            == 1
        )
    run.assert_called_once()
    raw = output.encode() if isinstance(output, str) else output or b""
    separator = b"\n" if raw and not raw.endswith(b"\n") else b""
    expected = raw + separator + f"ProB execution timed out: {exc}\n".encode()
    assert (tmp_path / "session-init.log").read_bytes() == expected
    assert capsys.readouterr().err == expected.decode(errors="replace")
    assert not (tmp_path / "session-animate.log").exists()
