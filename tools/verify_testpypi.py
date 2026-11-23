"""Verify that TestPyPI serves this run's build, then download the verified wheel.

The release job runs this before anything from TestPyPI is installed: a wheel can
carry ``.pth`` files that run at every Python start, so the bytes must match the
locally built ``dist/`` files first. Standard library only, because it runs on a
bare runner before any dependency is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self

type Fetch = Callable[[str, float], bytes]
type Sleep = Callable[[float], None]
type Clock = Callable[[], float]

# Every way a fetch can fail transiently: URLError, socket timeouts and resets
# are OSError subclasses; a response cut short raises http.client.IncompleteRead.
_TRANSIENT: Final = (OSError, http.client.HTTPException)
_METADATA_TIMEOUT: Final = 30.0
_DOWNLOAD_TIMEOUT: Final = 60.0
# An opener with only an HTTPS handler: http://, ftp:// and file:// cannot be
# fetched at all, even through a redirect. UnknownHandler turns any other
# scheme into URLError("unknown url type") instead of a silent None.
_HTTPS_ONLY: Final = urllib.request.OpenerDirector()
for _handler in (
    urllib.request.UnknownHandler(),
    urllib.request.HTTPSHandler(),
    urllib.request.HTTPRedirectHandler(),
    urllib.request.HTTPDefaultErrorHandler(),
    urllib.request.HTTPErrorProcessor(),
):
    _HTTPS_ONLY.add_handler(_handler)


class VerificationError(Exception):
    """TestPyPI does not serve this run's build, or could not be reached in time."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times to try a fetch and how long to wait between tries."""

    attempts: int
    wait: float


@dataclass(frozen=True, slots=True)
class ServedFile:
    """One file TestPyPI lists for a version: where it is and its sha256."""

    url: str
    sha256: str

    @classmethod
    def listing(cls, body: bytes) -> dict[str, ServedFile]:
        """Parse a TestPyPI version JSON document into its served files.

        Args:
            body: The ``/pypi/<package>/<version>/json`` response.

        Returns:
            File name to served file.

        Raises:
            VerificationError: When the document lacks the expected fields.
        """
        try:
            entries = json.loads(body)["urls"]
            return {
                str(e["filename"]): cls(str(e["url"]), str(e["digests"]["sha256"]))
                for e in entries
            }
        except (ValueError, KeyError, TypeError) as err:
            raise VerificationError(f"unreadable TestPyPI metadata: {err!r}") from err


class ReleaseVerifier:
    """Checks one package version on TestPyPI against the locally built files."""

    __slots__ = (
        "_clock",
        "_deadline",
        "_dist",
        "_fetch",
        "_package",
        "_sleep",
        "_version",
    )

    _package: str
    _version: str
    _dist: Path
    _fetch: Fetch
    _sleep: Sleep
    _clock: Clock
    _deadline: float

    def __new__(
        cls,
        package: str,
        version: str,
        dist: Path,
        *,
        fetch: Fetch,
        sleep: Sleep,
        clock: Clock,
        budget: float,
    ) -> Self:
        """Bind the package, its built files and the I/O used to reach TestPyPI.

        Args:
            package: The distribution name on TestPyPI.
            version: The version this run built.
            dist: Directory holding this run's wheel and sdist.
            fetch: Returns the body of a URL, given a socket timeout.
            sleep: Waits between attempts.
            clock: Monotonic seconds, for the overall deadline.
            budget: Seconds allowed for all fetching and waiting.

        Returns:
            The verifier.
        """
        self = super().__new__(cls)
        self._package = package
        self._version = version
        self._dist = dist
        self._fetch = fetch
        self._sleep = sleep
        self._clock = clock
        self._deadline = clock() + budget
        return self

    @staticmethod
    def http_get(url: str, timeout: float) -> bytes:
        """Return the body at ``url``; ``timeout`` bounds each socket wait.

        Args:
            url: The URL to read; only ``https://`` is accepted.
            timeout: Seconds allowed for each socket operation.

        Returns:
            The response body.

        Raises:
            VerificationError: When ``url`` is not an ``https://`` URL.
        """
        if not url.startswith("https://"):
            raise VerificationError(f"refusing non-https URL {url!r}")
        with _HTTPS_ONLY.open(url, timeout=timeout) as resp:
            body: bytes = resp.read()
        return body

    @staticmethod
    def say(text: str) -> None:
        """Write one line of progress or a CI annotation to stdout.

        Args:
            text: The line, without its newline.
        """
        sys.stdout.write(f"{text}\n")

    def built(self) -> dict[str, str]:
        """Map each locally built file name to its sha256.

        Returns:
            File name to hex digest, for every file in ``dist``.

        Raises:
            VerificationError: When ``dist`` holds no wheel.
        """
        built = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self._dist.iterdir())
        }
        if not any(name.endswith(".whl") for name in built):
            raise VerificationError(f"no wheel in {self._dist}")
        return built

    def served(self, built: dict[str, str], policy: RetryPolicy) -> dict[str, str]:
        """Poll TestPyPI until it lists every built file, and return its URLs.

        Args:
            built: The local file names and digests from :meth:`built`.
            policy: Attempts and wait between polls.

        Returns:
            File name to download URL, for the built files.

        Raises:
            VerificationError: When a file is never listed, or a listed digest
                differs from this run's build.
        """
        url = f"https://test.pypi.org/pypi/{self._package}/{self._version}/json"
        for attempt in range(1, policy.attempts + 1):
            try:
                listed = ServedFile.listing(self._fetch(url, _METADATA_TIMEOUT))
                if set(built) <= set(listed):
                    return self._matching_urls(built, listed)
                self.say(f"Attempt {attempt}: TestPyPI lists {sorted(listed)}; waiting")
            except _TRANSIENT as err:
                self.say(f"Attempt {attempt}: {err}; waiting")
            self._wait(policy.wait)
        raise VerificationError(
            f"TestPyPI never listed {sorted(built)} for {self._version}"
        )

    def download(self, name: str, url: str, digest: str, policy: RetryPolicy) -> bytes:
        """Download one served file and check its bytes against this run's build.

        Args:
            name: The file name, for messages.
            url: Where TestPyPI serves it.
            digest: The sha256 of this run's file.
            policy: Attempts and wait between tries.

        Returns:
            The verified bytes.

        Raises:
            VerificationError: When every attempt fails, or the bytes differ.
        """
        for attempt in range(1, policy.attempts + 1):
            try:
                data = self._fetch(url, _DOWNLOAD_TIMEOUT)
            except _TRANSIENT as err:
                self.say(f"Download attempt {attempt}: {err}; waiting")
                self._wait(policy.wait)
                continue
            if hashlib.sha256(data).hexdigest() != digest:
                raise VerificationError(
                    f"downloaded {name} does not match this run's build"
                )
            return data
        raise VerificationError(f"could not download {name} from TestPyPI")

    def run(self, dest: Path, poll: RetryPolicy, retry: RetryPolicy) -> Path:
        """Verify every built file on TestPyPI and write the verified wheel to ``dest``.

        Args:
            dest: Directory that receives the verified wheel; created here.
            poll: Retry policy for the metadata poll.
            retry: Retry policy for the wheel download.

        Returns:
            Path of the verified wheel.
        """
        built = self.built()
        urls = self.served(built, poll)
        wheel = next(name for name in built if name.endswith(".whl"))
        data = self.download(wheel, urls[wheel], built[wheel], retry)
        dest.mkdir()
        target = dest / wheel
        target.write_bytes(data)
        self.say(f"Verified {sorted(built)}; downloaded {wheel}")
        return target

    @staticmethod
    def _matching_urls(
        built: dict[str, str], listed: dict[str, ServedFile]
    ) -> dict[str, str]:
        for name, digest in built.items():
            if listed[name].sha256 != digest:
                raise VerificationError(
                    f"TestPyPI {name} is not this run's build; bump the version"
                )
        return {name: listed[name].url for name in built}

    def _wait(self, seconds: float) -> None:
        if self._clock() + seconds > self._deadline:
            raise VerificationError(
                f"gave up on {self._package} {self._version}: release budget spent"
            )
        self._sleep(seconds)


def main(argv: list[str] | None = None) -> int:  # None: use sys.argv
    """Run the verifier from the command line.

    Args:
        argv: Arguments after the program name; ``None`` reads ``sys.argv``.

    Returns:
        0 when TestPyPI serves this run's build and the wheel is written, else 1.
    """
    parser = argparse.ArgumentParser(
        description="Verify TestPyPI serves this run's build; download its wheel."
    )
    parser.add_argument("package")
    parser.add_argument("version")
    parser.add_argument("dist", type=Path)
    parser.add_argument("dest", type=Path)
    parser.add_argument("--budget", type=float, default=1200.0)
    parser.add_argument("--poll-wait", type=float, default=30.0)
    args = parser.parse_args(argv)
    verifier = ReleaseVerifier(
        args.package,
        args.version,
        args.dist,
        fetch=ReleaseVerifier.http_get,
        sleep=time.sleep,
        clock=time.monotonic,
        budget=args.budget,
    )
    try:
        verifier.run(args.dest, RetryPolicy(10, args.poll_wait), RetryPolicy(5, 30.0))
    except VerificationError as err:
        ReleaseVerifier.say(f"::error::{err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
