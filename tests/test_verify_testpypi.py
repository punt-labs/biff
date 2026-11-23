"""The release verifier checks TestPyPI's files before anything is installed."""

from __future__ import annotations

import hashlib
import http.client
import json
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Self

import pytest

from tools.verify_testpypi import (
    _HTTPS_ONLY,
    ReleaseVerifier,
    RetryPolicy,
    ServedFile,
    VerificationError,
    main,
)

_WHEEL = b"wheel bytes"
_SDIST = b"sdist bytes"
_META = "https://test.pypi.org/pypi/punt-biff/9.9.9/json"
_WHEEL_URL = "https://files.example/punt_biff-9.9.9-py3-none-any.whl"
_POLL = RetryPolicy(3, 30.0)
_RETRY = RetryPolicy(3, 30.0)

type Response = bytes | BaseException


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _metadata(
    wheel: bytes = _WHEEL, sdist: bytes = _SDIST, *, with_sdist: bool = True
) -> bytes:
    urls = [
        {
            "filename": "punt_biff-9.9.9-py3-none-any.whl",
            "url": _WHEEL_URL,
            "digests": {"sha256": _sha(wheel)},
        }
    ]
    if with_sdist:
        urls.append(
            {
                "filename": "punt_biff-9.9.9.tar.gz",
                "url": "https://files.example/punt_biff-9.9.9.tar.gz",
                "digests": {"sha256": _sha(sdist)},
            }
        )
    return json.dumps({"urls": urls}).encode()


class _Clock:
    """A fake monotonic clock; sleeping and slow fetches advance it."""

    __slots__ = ("_now", "_oversleep", "_waits")

    _now: float
    _oversleep: float
    _waits: list[float]

    def __new__(cls, oversleep: float = 0.0) -> Self:
        """``oversleep`` models a runner that wakes later than asked."""
        self = super().__new__(cls)
        self._now = 0.0
        self._oversleep = oversleep
        self._waits = []
        return self

    @property
    def waits(self) -> list[float]:
        return self._waits

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def sleep(self, seconds: float) -> None:
        self._waits.append(seconds)
        self.advance(seconds + self._oversleep)

    def time(self) -> float:
        return self._now


class _Net:
    """Scripted responses per URL; the last response repeats once a script runs out.

    Each fetch takes ``cost`` seconds on ``clock``, so a test can model a slow
    request crossing the deadline.
    """

    __slots__ = ("_calls", "_clock", "_cost", "_scripts")

    _scripts: dict[str, list[Response]]
    _calls: list[str]
    _clock: _Clock | None  # None: fetches take no time
    _cost: float

    def __new__(
        cls,
        scripts: dict[str, list[Response]],
        clock: _Clock | None = None,  # None: fetches take no time
        cost: float = 0.0,
    ) -> Self:
        self = super().__new__(cls)
        self._scripts = scripts
        self._calls = []
        self._clock = clock
        self._cost = cost
        return self

    @property
    def calls(self) -> list[str]:
        return self._calls

    def fetch(self, url: str, timeout: float) -> bytes:
        assert timeout > 0
        self._calls.append(url)
        if self._clock is not None:
            self._clock.advance(self._cost)
        script = self._scripts[url]
        response = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(response, BaseException):
            raise response
        return response


# One list for both retry tests, so the metadata and download paths cannot drift.
_TRANSIENT_CASES = pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("dns"),
        TimeoutError("slow"),
        ConnectionResetError("reset"),
        http.client.IncompleteRead(b"partial"),
    ],
    ids=["url-error", "timeout", "reset", "incomplete-read"],
)


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    built = tmp_path / "dist"
    built.mkdir()
    (built / "punt_biff-9.9.9-py3-none-any.whl").write_bytes(_WHEEL)
    (built / "punt_biff-9.9.9.tar.gz").write_bytes(_SDIST)
    return built


def _verifier(
    dist: Path, net: _Net, clock: _Clock, budget: float = 1200.0
) -> ReleaseVerifier:
    return ReleaseVerifier(
        "punt-biff",
        "9.9.9",
        dist,
        fetch=net.fetch,
        sleep=clock.sleep,
        clock=clock.time,
        budget=budget,
    )


def _run(dist: Path, net: _Net, clock: _Clock, **kwargs: float) -> Path:
    return _verifier(dist, net, clock, **kwargs).run(
        dist.parent / "verified", _POLL, _RETRY
    )


def test_matching_files_write_the_verified_wheel(dist: Path) -> None:
    net = _Net({_META: [_metadata()], _WHEEL_URL: [_WHEEL]})
    clock = _Clock()
    wheel = _run(dist, net, clock)
    assert wheel.read_bytes() == _WHEEL
    assert wheel.parent == dist.parent / "verified"
    assert clock.waits == []


def test_waits_until_testpypi_lists_every_file(dist: Path) -> None:
    net = _Net(
        {_META: [_metadata(with_sdist=False), _metadata()], _WHEEL_URL: [_WHEEL]}
    )
    clock = _Clock()
    _run(dist, net, clock)
    assert net.calls.count(_META) == 2
    assert clock.waits == [30.0]


@_TRANSIENT_CASES
def test_transient_metadata_errors_are_retried(
    dist: Path, error: BaseException
) -> None:
    net = _Net({_META: [error, _metadata()], _WHEEL_URL: [_WHEEL]})
    _run(dist, net, _Clock())
    assert net.calls.count(_META) == 2


@_TRANSIENT_CASES
def test_transient_download_errors_are_retried(
    dist: Path, error: BaseException
) -> None:
    net = _Net({_META: [_metadata()], _WHEEL_URL: [error, _WHEEL]})
    wheel = _run(dist, net, _Clock())
    assert wheel.read_bytes() == _WHEEL
    assert net.calls.count(_WHEEL_URL) == 2


def test_never_listed_fails_after_every_attempt(dist: Path) -> None:
    not_found = urllib.error.HTTPError(
        _META, 404, "Not Found", http.client.HTTPMessage(), None
    )
    net = _Net({_META: [not_found]})
    clock = _Clock()
    with pytest.raises(VerificationError, match="never listed"):
        _run(dist, net, clock)
    assert net.calls.count(_META) == _POLL.attempts
    assert not (dist.parent / "verified").exists()


def test_download_that_keeps_failing_stops_the_release(dist: Path) -> None:
    net = _Net({_META: [_metadata()], _WHEEL_URL: [ConnectionResetError("reset")]})
    with pytest.raises(VerificationError, match="could not download"):
        _run(dist, net, _Clock())
    assert net.calls.count(_WHEEL_URL) == _RETRY.attempts
    assert not (dist.parent / "verified").exists()


def test_listed_digest_mismatch_downloads_nothing(dist: Path) -> None:
    net = _Net(
        {_META: [_metadata(wheel=b"someone else's wheel")], _WHEEL_URL: [_WHEEL]}
    )
    with pytest.raises(VerificationError, match="is not this run's build"):
        _run(dist, net, _Clock())
    assert _WHEEL_URL not in net.calls


def test_downloaded_bytes_must_match(dist: Path) -> None:
    net = _Net({_META: [_metadata()], _WHEEL_URL: [b"swapped after listing"]})
    with pytest.raises(VerificationError, match="does not match"):
        _run(dist, net, _Clock())
    assert not (dist.parent / "verified").exists()


def test_budget_bounds_all_retries(dist: Path) -> None:
    net = _Net({_META: [urllib.error.URLError("down")]})
    clock = _Clock()
    with pytest.raises(VerificationError, match="budget spent"):
        _run(dist, net, clock, budget=45.0)
    assert clock.waits == [30.0]


def test_a_sleep_that_overshoots_the_deadline_stops_the_next_poll(
    dist: Path,
) -> None:
    clock = _Clock(oversleep=20.0)
    net = _Net({_META: [urllib.error.URLError("down"), _metadata()]})
    with pytest.raises(VerificationError, match="budget spent"):
        _run(dist, net, clock, budget=45.0)
    assert net.calls == [_META]
    assert clock.waits == [30.0]


def test_a_fetch_that_crosses_the_deadline_stops_before_the_next(
    dist: Path,
) -> None:
    clock = _Clock()
    net = _Net({_META: [_metadata()], _WHEEL_URL: [_WHEEL]}, clock=clock, cost=50.0)
    with pytest.raises(VerificationError, match="budget spent"):
        _run(dist, net, clock, budget=45.0)
    assert net.calls == [_META]
    assert clock.waits == []


def test_dist_without_a_wheel_fails(tmp_path: Path) -> None:
    built = tmp_path / "dist"
    built.mkdir()
    (built / "punt_biff-9.9.9.tar.gz").write_bytes(_SDIST)
    with pytest.raises(VerificationError, match="no wheel"):
        _run(built, _Net({}), _Clock())


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"no_urls": []}', b'{"urls": [{"filename": "x"}]}'],
    ids=["not-json", "no-urls", "missing-fields"],
)
def test_unreadable_metadata_fails_closed(body: bytes) -> None:
    with pytest.raises(VerificationError, match="unreadable"):
        ServedFile.listing(body)


@pytest.mark.parametrize(
    "url", ["http://test.pypi.org/x", "file:///etc/passwd", "ftp://example/x"]
)
def test_http_get_refuses_anything_but_https(url: str) -> None:
    with pytest.raises(VerificationError, match="non-https"):
        ReleaseVerifier.http_get(url, 1.0)


@pytest.mark.parametrize("url", ["http://example.invalid/x", "file:///etc/hosts"])
def test_opener_cannot_fetch_non_https_even_by_redirect(url: str) -> None:
    with pytest.raises(urllib.error.URLError, match="unknown url type"):
        _HTTPS_ONLY.open(url, timeout=1.0)


def _fake_http(net: _Net) -> Callable[[str, float], bytes]:
    return net.fetch


def test_main_reports_failure_as_a_ci_error(
    dist: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    net = _Net(
        {"https://test.pypi.org/pypi/punt-biff/9.9.9/json": [_metadata(wheel=b"other")]}
    )
    monkeypatch.setattr(ReleaseVerifier, "http_get", staticmethod(_fake_http(net)))
    code = main(["punt-biff", "9.9.9", str(dist), str(dist.parent / "verified")])
    assert code == 1
    assert (
        "::error::TestPyPI punt_biff-9.9.9-py3-none-any.whl is not this run's build"
        in (capsys.readouterr().out)
    )


def test_main_succeeds_and_writes_the_wheel(
    dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    net = _Net({_META: [_metadata()], _WHEEL_URL: [_WHEEL]})
    monkeypatch.setattr(ReleaseVerifier, "http_get", staticmethod(_fake_http(net)))
    dest = dist.parent / "verified"
    assert main(["punt-biff", "9.9.9", str(dist), str(dest)]) == 0
    assert (dest / "punt_biff-9.9.9-py3-none-any.whl").read_bytes() == _WHEEL
