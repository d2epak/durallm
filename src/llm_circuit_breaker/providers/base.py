"""Base Provider Adapter Protocol and Interfaces."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, Optional, Protocol

from llm_circuit_breaker.capability.profile import Endpoint
from llm_circuit_breaker.protocol.ir import NormalizedRequest, NormalizedResponse


@dataclass
class PreparedRequest:
    """Pre-processed native HTTP request ready for execution."""
    url: str
    headers: Dict[str, str]
    body_bytes: bytes
    method: str = "POST"


@dataclass(frozen=True)
class TransportTimeouts:
    """Independent, monotonic budgets for one upstream streaming attempt.

    All values are milliseconds.  A transport implementation must honour the
    total budget in addition to the phase budgets; phase deadlines are not a
    substitute for a request deadline.
    """

    connect_timeout_ms: float = 5000.0
    tls_timeout_ms: float = 5000.0
    first_byte_timeout_ms: float = 15000.0
    idle_timeout_ms: float = 10000.0
    total_timeout_ms: float = 60000.0

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")

    @staticmethod
    def _seconds(value_ms: float) -> float:
        # ``socket`` rejects zero, while callers may have just a few
        # milliseconds left on their request deadline.
        return max(0.001, value_ms / 1000.0)

    @property
    def connect_timeout_seconds(self) -> float:
        return self._seconds(self.connect_timeout_ms)

    @property
    def tls_timeout_seconds(self) -> float:
        return self._seconds(self.tls_timeout_ms)

    @property
    def first_byte_timeout_seconds(self) -> float:
        return self._seconds(self.first_byte_timeout_ms)

    @property
    def idle_timeout_seconds(self) -> float:
        return self._seconds(self.idle_timeout_ms)


class RequestCancellation:
    """Thread-safe cancellation that closes an in-flight upstream transport.

    A proxy writer invokes this when its downstream client disconnects.  The
    callback API intentionally stays synchronous and tiny so it can also be
    used by the zero-dependency HTTP server.
    """

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._reason = ""
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def add_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._cancelled.is_set():
                callback()
                return
            self._callbacks.append(callback)

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self._cancelled.is_set():
                return
            self._reason = reason
            self._cancelled.set()
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # Cancellation is best effort. A closed connection is the
                # expected case and must not mask the original disconnect.
                pass


class ProviderStreamError(Exception):
    """A stream failed after connection, annotated with its transport phase."""

    def __init__(self, phase: str, message: str):
        super().__init__(message)
        self.phase = phase


class ProviderByteStream(Protocol):
    """A bounded, cancellable sequence of raw provider-native bytes."""

    def iter_bytes(self) -> Iterator[bytes]:
        ...

    def close(self) -> None:
        ...


@dataclass
class ProviderStreamResult:
    """Headers plus an unbuffered provider stream, or a bounded open failure."""

    status_code: int
    headers: Dict[str, str]
    duration_ms: float = 0.0
    stream: Optional[ProviderByteStream] = None
    body: bytes = b""
    transport_error: Optional[str] = None


# Pseudo status codes for failures that never produced an HTTP response.
TRANSPORT_STATUS = {"tls": 596, "timeout": 597, "connection": 598, "unknown": 599}


@dataclass
class ProviderExecutionResult:
    """Result of an HTTP invocation to an upstream provider."""
    status_code: int
    headers: Dict[str, str]
    body: bytes
    duration_ms: float = 0.0
    # Set when no HTTP response was received: "tls", "timeout", "connection" or "unknown".
    transport_error: Optional[str] = None


class ProviderAdapter(Protocol):
    """Narrow provider abstraction isolating provider-specific network protocols."""

    provider_id: str

    def prepare_request(
        self,
        endpoint: Endpoint,
        request: NormalizedRequest,
        api_key: Optional[str] = None,
    ) -> PreparedRequest:
        """Translate NormalizedRequest into provider-specific HTTP headers and body."""
        ...

    def execute(
        self,
        prepared: PreparedRequest,
        timeout_seconds: float,
    ) -> ProviderExecutionResult:
        """Execute request with per-attempt timeout."""
        ...

    def open_stream(
        self,
        prepared: PreparedRequest,
        timeouts: TransportTimeouts,
        cancellation: Optional[RequestCancellation] = None,
    ) -> ProviderStreamResult:
        """Open a provider-native stream without buffering its response body."""
        ...

    def normalize_response(
        self,
        endpoint: Endpoint,
        result: ProviderExecutionResult,
    ) -> NormalizedResponse:
        """Translate raw provider HTTP response into NormalizedResponse IR."""
        ...
