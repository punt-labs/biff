"""``biff doctor``'s checks of biffd: unit, process, port, health, token, relay."""

from __future__ import annotations

from importlib.metadata import version as package_version
from typing import TYPE_CHECKING, Final, Protocol, Self, final

from biff.cli_daemon_relay import RelayStatus
from biff.doctor import Diagnostics
from biff.log_hint import LOG_PATH
from biff.plugins.contract import CheckResult
from biff.run_dir import DEFAULT_PORT, RunDir
from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.health import Health, HealthProbe, HealthUnavailableError
from biff.supervision.host import ServiceHost
from biff.supervision.port import PortProbe

if TYPE_CHECKING:
    from collections.abc import Iterator

    from biff.supervision.backend import ServiceBackend

__all__ = ["DaemonChecks", "DaemonDiagnostics", "RelayProbe"]

_HEALTH_SECONDS: Final = 1.0
_START: Final = "run: biff daemon start"
_RESTART: Final = "run: biff daemon restart"


class RelayProbe(Protocol):
    """Reports the relay biffd is connected to; ``RelayStatus`` is the real one."""

    def status(self, port: int) -> str:
        """Return a one-line relay report.

        Raises:
            ConnectionError: The relay cannot be reached; the message says why.
        """
        ...


@final
class DaemonDiagnostics(Diagnostics):
    """Doctor's report with biffd's checks after the others."""

    __slots__ = ()

    def _checks(self) -> Iterator[CheckResult]:
        yield from super()._checks()
        yield from DaemonChecks.for_host().checks()


@final
class DaemonChecks:
    """The seven checks of biffd; each failure names the command that repairs it.

    They are informational: nothing routes through biffd by default yet, so a
    machine without it is not broken.
    """

    __slots__ = ("_backend", "_client_version", "_relay", "_run_dir")

    # None: the host has no service manager (see ServiceHost.backend).
    _backend: ServiceBackend | None
    _run_dir: RunDir
    _relay: RelayProbe
    _client_version: str

    def __new__(
        cls,
        backend: ServiceBackend | None,
        run_dir: RunDir,
        relay: RelayProbe,
        client_version: str,
    ) -> Self:
        """Check biffd as *backend* supervises it, with its files in *run_dir*."""
        self = super().__new__(cls)
        self._backend = backend
        self._run_dir = run_dir
        self._relay = relay
        self._client_version = client_version
        return self

    @classmethod
    def for_host(cls) -> Self:
        """Return the checks for this host and this biff version."""
        return cls(
            ServiceHost.backend(BIFFD_SPEC),
            RunDir.default(),
            RelayStatus(),
            package_version("punt-biff"),
        )

    def checks(self) -> Iterator[CheckResult]:
        """Run the seven checks, yielding each as it finishes."""
        port, port_problem = self._port()
        health = self._health(port)
        yield self._unit()
        yield self._running(health)
        yield port_problem or self._port_owner(port)
        yield self._health_result(health, port)
        yield self._token()
        yield self._version(health)
        yield self._relay_result(port, health)

    @staticmethod
    def _pass(name: str, message: str) -> CheckResult:
        return CheckResult(name, passed=True, message=message, required=False)

    @staticmethod
    def _fail(name: str, message: str) -> CheckResult:
        return CheckResult(name, passed=False, message=message, required=False)

    def _port(self) -> tuple[int, CheckResult | None]:
        """Return the port to check, and a failed result when its record is corrupt."""
        try:
            return self._run_dir.read_port(), None
        except ValueError as exc:
            problem = self._fail("biffd port", f"{exc}; remove it, then {_RESTART}")
            return DEFAULT_PORT, problem

    @staticmethod
    def _health(port: int) -> Health | HealthUnavailableError:
        try:
            return HealthProbe(port).fetch(_HEALTH_SECONDS)
        except HealthUnavailableError as exc:
            return exc

    def _unit(self) -> CheckResult:
        name = "biffd unit"
        if self._backend is None:
            return self._pass(
                name,
                "no service manager here; biffd starts on demand when a client "
                "needs it",
            )
        path = self._backend.config_path()
        if path.exists():
            return self._pass(name, f"installed ({path})")
        return self._fail(name, "not installed (run: biff daemon install)")

    def _running(self, health: Health | HealthUnavailableError) -> CheckResult:
        name = "biffd running"
        up = (
            isinstance(health, Health)
            if self._backend is None
            else self._backend.is_active()
        )
        if up:
            return self._pass(name, "running")
        return self._fail(name, f"not running ({_START})")

    def _port_owner(self, port: int) -> CheckResult:
        name = "biffd port"
        state = PortProbe(port).state()
        if state == "ours":
            return self._pass(name, f"{port} is held by biffd")
        if state == "free":
            return self._fail(
                name, f"{port} is free, so biffd is not listening ({_START})"
            )
        return self._fail(
            name,
            f"{port} is held by another program; see what holds it with: "
            f"lsof -nP -iTCP:{port} -sTCP:LISTEN",
        )

    def _health_result(
        self, health: Health | HealthUnavailableError, port: int
    ) -> CheckResult:
        name = "biffd health"
        if isinstance(health, Health):
            return self._pass(
                name, f"answers on {port} (up {health.uptime_seconds:.0f}s)"
            )
        return self._fail(
            name,
            f"no answer within {_HEALTH_SECONDS:g}s ({health}); {_RESTART}; "
            f"see {LOG_PATH}",
        )

    def _token(self) -> CheckResult:
        name = "biffd token"
        path = self._run_dir.token_path
        try:
            self._run_dir.read_token()
        except FileNotFoundError:
            return self._fail(
                name, f"missing at {path}; biffd creates it at startup ({_RESTART})"
            )
        except (PermissionError, ValueError) as exc:
            return self._fail(name, str(exc))
        return self._pass(name, f"{path} is private (mode 0600)")

    def _version(self, health: Health | HealthUnavailableError) -> CheckResult:
        name = "biffd version"
        if not isinstance(health, Health):
            return self._fail(
                name,
                f"cannot read the daemon's version: it is not answering ({_START})",
            )
        if health.version == self._client_version:
            return self._pass(name, health.version)
        return self._fail(
            name,
            f"daemon {health.version}, client {self._client_version}; {_RESTART}",
        )

    def _relay_result(
        self, port: int, health: Health | HealthUnavailableError
    ) -> CheckResult:
        name = "biffd relay"
        if not isinstance(health, Health):
            return self._fail(
                name, f"cannot check the relay: biffd is not answering ({_START})"
            )
        try:
            return self._pass(name, self._relay.status(port))
        except ConnectionError as exc:
            return self._fail(name, f"relay is down: {exc}; {_RESTART}; see {LOG_PATH}")
