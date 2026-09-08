"""Bridge from the V1 pool tables to the V3 GatewayExecutor for the HTTP proxy.

The proxy's configuration surface (default pools, `--discover`, `LLM_ALLOWED_PROVIDERS`,
`api_keys` loaded by IsolatedPoolManager) is still expressed as RouteDefinitions. This module
mirrors those routes into the executor's capability registry so every HTTP request is served
by GatewayExecutor: classification, breakers, backoff, compaction, ledger and validation.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional, Tuple

from durallm.capability.profile import Endpoint, ModelProfile
from durallm.continuation import (
    ContinuationEvent,
    ContinuationRequest,
    ContinuationStore,
    InMemoryContinuationStore,
)
from durallm.errors import (
    CircuitBreakerGatewayError,
    ConfigurationError,
    ContextOverflowError,
    ContinuationProtocolError,
    DeadlineExceededError,
    IndeterminateToolOperationError,
    NoHealthyRouteError,
    NonRecoverableFailureError,
    ToolOperationProtocolError,
)
from durallm.execution.executor import GatewayExecutor, NativeStreamHandle
from durallm.execution.ledger import AttemptLedger
from durallm.pools import POOL_MANAGER, IsolatedPoolManager, RouteDefinition
from durallm.protocol.ir import NormalizedRequest, NormalizedResponse
from durallm.routing.decision import RoutingDecision


def endpoint_from_route(route: RouteDefinition, pool: str, priority: int) -> Endpoint:
    """One Endpoint per (pool, route); ids are pool-qualified because discovered routes reuse ids across pools."""
    return Endpoint(
        id=f"{pool}:{route.id}",
        provider=route.provider,
        model=route.model,
        base_url=route.base_url,
        protocol=route.api_format,
        env_key=route.env_key,
        env_keys=list(route.env_keys),
        headers=dict(route.headers),
        priority=priority,
        pool=pool,
        is_discovered=route.is_discovered,
        profile=ModelProfile(
            route.provider, route.model, protocol=route.api_format,
            context_window=route.context_length, max_output_tokens=route.max_output_tokens, supports_tools=True,
        ),
    )


def http_error_for(exc: CircuitBreakerGatewayError) -> Tuple[int, str]:
    """Map an executor failure to (HTTP status, error type) for the client."""
    if isinstance(exc, NoHealthyRouteError):
        return 503, "no_healthy_route"
    if isinstance(exc, DeadlineExceededError):
        return 504, "deadline_exceeded"
    if isinstance(exc, ContextOverflowError):
        return 413, "context_overflow"
    if isinstance(exc, NonRecoverableFailureError):
        code = getattr(exc.classification, "status_code", None)
        return (code if isinstance(code, int) and 400 <= code < 500 else 502), "upstream_failure"
    if isinstance(exc, ConfigurationError):
        return 500, "configuration_error"
    if isinstance(exc, ContinuationProtocolError):
        return exc.status_code, "continuation_protocol_error"
    if isinstance(exc, IndeterminateToolOperationError):
        return 409, "indeterminate_tool_operation"
    if isinstance(exc, ToolOperationProtocolError):
        return exc.status_code, "tool_operation_protocol_error"
    return 502, "gateway_error"


class ProxyGateway:
    """Serves proxy requests from GatewayExecutor, fed by the pool manager's routes and keys."""

    def __init__(
        self,
        pool_manager: Optional[IsolatedPoolManager] = None,
        executor: Optional[GatewayExecutor] = None,
        continuation_store: Optional[ContinuationStore] = None,
        persistence_store: Optional[Any] = None,
    ):
        self.pool_manager = pool_manager or POOL_MANAGER
        self.executor = executor or GatewayExecutor(pool_manager=self.pool_manager)
        if not getattr(self.executor, "pool_manager", None):
            self.executor.pool_manager = self.pool_manager
        self._lock = threading.Lock()
        self._synced: set = set()
        # Process-local by default. A durable store is intentionally injected by
        # the next persistence milestone; ACP headers never imply crash recovery.
        self.continuation_store = continuation_store or InMemoryContinuationStore()
        self.persistence_store = persistence_store or getattr(self.executor, "attempt_store", None)
        if self.persistence_store and hasattr(self.persistence_store, "get_active_quota_lockouts"):
            try:
                active_lockouts = self.persistence_store.get_active_quota_lockouts()
                now_epoch = time.time()
                for item in active_lockouts:
                    pool_name = str(item["pool"]).lower()
                    route_id = str(item["route_id"]).lower()
                    provider_id = str(item.get("provider_id", "")).lower()
                    exp_at = float(item["expires_at"])
                    if exp_at > now_epoch:
                        rem_sec = max(0.0, exp_at - now_epoch)
                        if route_id == "*" or not route_id:
                            if provider_id:
                                self.pool_manager.account_lockouts[provider_id] = exp_at
                                self.pool_manager.mark_provider_quota_exhausted(pool_name, provider_id, seconds=rem_sec)
                        else:
                            self.pool_manager.exhausted_quotas[(pool_name, route_id)] = exp_at
                            self.executor.health_store.record_failure(
                                endpoint_id=f"{pool_name}:{route_id}",
                                latency_ms=0.0,
                                quota_exhausted_seconds=rem_sec,
                                error_message=f"Persisted daily quota lockout: {item.get('reason', '')}",
                            )
            except Exception as e:
                logging.getLogger("durallm.gateway").warning("Could not sync persisted quota lockouts: %s", e)

    def sync_endpoints(self) -> None:
        """Register every usable pool route once; called per request so `--discover` additions are picked up."""
        pm = self.pool_manager
        with self._lock:
            for pool, routes in (("coding", pm.coding_routes), ("general_agent", pm.agent_routes)):
                for position, route in enumerate(routes, 1):
                    key = f"{pool}:{route.id}"
                    if key in self._synced or not self._usable(route):
                        continue
                    self.executor.capability_registry.register_endpoint(endpoint_from_route(route, pool, position))
                    self._synced.add(key)

    def _usable(self, route: RouteDefinition) -> bool:
        pm = self.pool_manager
        if pm.allowed_providers is not None and route.provider.lower() not in pm.allowed_providers:
            return False
        # A route whose key is absent is skipped, not registered; a later key refresh makes it eligible.
        return not route.env_key or bool(pm.keys.get(route.env_key, "").strip())

    def complete(self, request: NormalizedRequest, pool: str) -> Tuple[NormalizedResponse, RoutingDecision, AttemptLedger]:
        self.sync_endpoints()
        return self.executor.execute(request, pool=pool, strategy="priority", api_keys=self.pool_manager.keys)

    def open_native_stream(
        self,
        request: NormalizedRequest,
        pool: str,
        client_protocol: str,
    ) -> NativeStreamHandle:
        """Open an opt-in raw stream; pre-visible failures may still fail over."""
        self.sync_endpoints()
        return self.executor.open_native_stream(
            request,
            pool=pool,
            strategy="priority",
            api_keys=self.pool_manager.keys,
            client_protocol=client_protocol,
        )

    def complete_turn(
        self,
        request: NormalizedRequest,
        pool: str,
        continuation: Optional[ContinuationRequest] = None,
    ) -> Tuple[NormalizedResponse, RoutingDecision, AttemptLedger, Optional[ContinuationEvent]]:
        """Execute a request and emit an ACP event only for an opted-in client."""
        if continuation is None:
            response, decision, ledger = self.complete(request, pool)
            return response, decision, ledger, None

        turn = self.continuation_store.begin_turn(continuation)
        try:
            response, decision, ledger = self.complete(request, pool)
        except Exception as exc:
            self.continuation_store.interrupt_turn(turn, detail=str(exc))
            raise
        endpoint = decision.selected_endpoint.id if decision.selected_endpoint else "unknown"
        event = self.continuation_store.complete_turn(turn, request, response, endpoint)
        return response, decision, ledger, event

    def acknowledge_continuation(
        self, session_id: str, turn_id: str, epoch: int, checkpoint_digest: str
    ) -> ContinuationEvent:
        """Record the client acknowledgement required before its next ACP turn."""
        return self.continuation_store.acknowledge(session_id, turn_id, epoch, checkpoint_digest)
