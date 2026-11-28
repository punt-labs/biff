"""``biff doctor``'s checks of biffd: unit, process, port, health, token, relay."""

from __future__ import annotations

import subprocess
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import TYPE_CHECKING, Final, Self, final

from biff.doctor import Diagnostics
from biff.log_hint import LOG_PATH
from biff.plugins.contract import CheckResult
from biff.run_dir import RunDir
from biff.supervision.biffd import BIFFD_SPEC
from biff.supervision.health import (
    Health,
    HealthProbe,
    HealthProbes,
    HealthUnavailableError,
)
from biff.supervision.host import Detection, ServiceHost
from biff.supervision.port import PortProbe

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from biff.supervision.spec import ServiceSpec

__all__ = ["DaemonChecks", "DaemonDiagnostics"]

_HEALTH_SECONDS: Final = 1.0
_START: Final = "run: biff daemon start"
_RESTART: Final = "run: biff daemon restart"
_UNKNOWN_VERSION: Final = "unknown"


@final
class DaemonDiagnostics(Diagnostics):
    """Doctor's report with biffd's checks after the others."""

    __slots__ = ()

    def _checks(self) -> Iterator[CheckResult]:
        yield from super()._checks()
        yield from DaemonChecks.for_host().checks()


@final
class DaemonChecks:
    """The checks of biffd; each failure names the command that repairs it.

    They are advisory while biffd is not installed, since nothing routes through
    it by default yet.  Once its unit is installed, a version mismatch and a port
    held by another program fail ``biff doctor``: the machine asked for a daemon
    and does not have the right one.
    """

    __slots__ = ("_client_version", "_host", "_run_dir", "_spec")

    _host: Detection
    _run_dir: RunDir
    _client_version: str
    _spec: ServiceSpec

    def __new__(
        cls,
        host: Detection,
        run_dir: RunDir,
        client_version: str,
        *,
        spec: ServiceSpec = BIFFD_SPEC,
    ) -> Self:
        """Check biffd as *host*'s manager supervises it, with files in *run_dir*."""
        self = super().__new__(cls)
        self._host = host
        self._run_dir = run_dir
        self._client_version = client_version
        self._spec = spec
        return self

    @classmethod
    def for_host(cls) -> Self:
        """Return the checks for this host and this biff version."""
        try:
            client = package_version("punt-biff")
        except PackageNotFoundError:
            client = _UNKNOWN_VERSION
        return cls(ServiceHost.detect(BIFFD_SPEC), RunDir.default(), client)

    def checks(self) -> Iterator[CheckResult]:
        """Run the checks, yielding each as it finishes; none raises."""
        installed = self._is_installed()
        port, port_problem = self._port()
        health = self._health(port)
        if not installed:
            yield CheckResult(
                "biffd checks",
                passed=True,
                message=(
                    "advisory: biffd is not installed, so these lines never fail "
                    "biff doctor (run: biff daemon install)"
                ),
                required=False,
            )
        yield self._guard("biffd unit", self._unit)
        yield self._guard("biffd running", lambda: self._running(health))
        yield port_problem or self._guard(
            "biffd port", lambda: self._port_owner(port, installed=installed)
        )
        yield self._guard("biffd health", lambda: self._health_result(health, port))
        yield self._guard("biffd token", self._token)
        yield self._guard(
            "biffd version", lambda: self._version(health, installed=installed)
        )
        yield self._guard("biffd relay", lambda: self._relay_result(health))

    def _is_installed(self) -> bool:
        backend = self._host.backend
        try:
            return backend is not None and backend.config_path().exists()
        except OSError:
            return False

    def _guard(self, name: str, check: Callable[[], CheckResult]) -> CheckResult:
        """Run *check*; a failing supervisor or file is a failed line, not a crash."""
        try:
            return check()
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return self._fail(name, f"could not check ({exc})")

    @staticmethod
    def _pass(name: str, message: str, *, required: bool = False) -> CheckResult:
        return CheckResult(name, passed=True, message=message, required=required)

    @staticmethod
    def _fail(name: str, message: str, *, required: bool = False) -> CheckResult:
        return CheckResult(name, passed=False, message=message, required=required)

    def _port(self) -> tuple[int, CheckResult | None]:
        """Return the port to check, and a failed result when its record is unreadable.

        The recorded port is tried first, then the unit's, so a stale record left
        by a crash does not hide a healthy daemon.
        """
        try:
            recorded = self._run_dir.read_port()
        except (OSError, ValueError) as exc:
            problem = self._fail("biffd port", f"{exc}; remove it, then {_RESTART}")
            return self._spec.health_port, problem
        live = HealthProbes(recorded, self._spec.health_port).live_port(_HEALTH_SECONDS)
        return (recorded if live is None else live), None

    @staticmethod
    def _health(port: int) -> Health | HealthUnavailableError:
        try:
            return HealthProbe(port).fetch(_HEALTH_SECONDS)
        except HealthUnavailableError as exc:
            return exc

    def _unit(self) -> CheckResult:
        name = "biffd unit"
        backend = self._host.backend
        if backend is None:
            if self._host.status == "unreachable":
                return self._fail(
                    name,
                    f"the service manager does not answer ({self._host.reason}); "
                    "biffd is not supervised here (start it with: biff daemon serve)",
                )
            return self._pass(
                name,
                "no service manager here; biffd is not supervised (start it with: "
                "biff daemon serve)",
            )
        path = backend.config_path()
        if path.exists():
            return self._pass(name, f"installed ({path})")
        return self._fail(name, "not installed (run: biff daemon install)")

    def _running(self, health: Health | HealthUnavailableError) -> CheckResult:
        name = "biffd running"
        backend = self._host.backend
        up = isinstance(health, Health) if backend is None else backend.is_active()
        if up:
            return self._pass(name, "running")
        return self._fail(name, f"not running ({_START})")

    def _port_owner(self, port: int, *, installed: bool) -> CheckResult:
        name = "biffd port"
        state = PortProbe(port).state()
        if state == "ours":
            return self._pass(name, f"{port} is held by biffd", required=installed)
        if state == "free":
            return self._fail(
                name, f"{port} is free, so biffd is not listening ({_START})"
            )
        lsof = f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
        if state == "unresponsive":
            return self._fail(
                name,
                f"{port} is held but does not answer /health; {_RESTART}, or see "
                f"what holds it with: {lsof}",
            )
        return self._fail(
            name,
            f"{port} is held by another program; see what holds it with: {lsof}",
            required=installed,
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

    def _version(
        self, health: Health | HealthUnavailableError, *, installed: bool
    ) -> CheckResult:
        name = "biffd version"
        if not isinstance(health, Health):
            return self._fail(
                name,
                f"cannot read the daemon's version: it is not answering ({_START})",
            )
        comparable = installed and self._client_version != _UNKNOWN_VERSION
        if health.version == self._client_version:
            return self._pass(name, health.version, required=comparable)
        return self._fail(
            name,
            f"daemon {health.version}, client {self._client_version}; {_RESTART}",
            required=comparable,
        )

    def _relay_result(self, health: Health | HealthUnavailableError) -> CheckResult:
        """Say the relay is not reported yet; asking would register a session.

        biffd has no method that returns its relay connections without opening a
        session, and a session opened by doctor would take part in mail routing.
        """
        name = "biffd relay"
        if not isinstance(health, Health):
            return self._fail(
                name, f"cannot check the relay: biffd is not answering ({_START})"
            )
        seeded = (
            f"; started with {health.relay_seed}, which every client is served by"
            if health.relay_seed and health.relay_seed != "none"
            else ""
        )
        changed = (
            f"; WARNING {health.relay_seed_note}: sessions opened under the others "
            "are kept dormant"
            if health.relay_seed_note
            else ""
        )
        return self._pass(
            name,
            "connections not reported by biffd yet: no daemon method returns them "
            f"without registering a session{seeded}{changed}",
        )
