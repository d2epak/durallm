"""Core Request Execution Engine for Semantic Failover."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from durallm.agent.context import ContextBudget, ContextManager, estimate_tokens
from durallm.agent.failover_plan import FailoverPlan
from durallm.agent.idempotency import (
    DEFAULT_TOOL_LEDGER,
    ToolExecutionLedger,
)
from durallm.agent.tool_validation import ToolCallValidator
from durallm.breaker.registry import (
    DEFAULT_BREAKER_REGISTRY,
    CircuitBreakerRegistry,
)
from durallm.capability.profile import Endpoint
from durallm.capability.registry import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityRegistry,
)
from durallm.classifier import (
    calculate_seconds_until_utc_midnight,
    classify_failure,
    parse_input_tpm_limit,
    parse_output_cap_from_error,
)
from durallm.errors import (
    BreakerOpenError,
    ConfigurationError,
    ContextOverflowError,
    IndeterminateToolOperationError,
    NoHealthyRouteError,
    NonRecoverableFailureError,
    ProbeAdmissionDeniedError,
)
from durallm.execution.deadline import Deadline
from durallm.execution.ledger import AttemptLedger
from durallm.execution.policy import ExecutionPolicy
from durallm.health.telemetry import (
    HealthTelemetryStore,
)
from durallm.models import (
    AttemptRecord,
    FailoverReason,
    FailureCategory,
    FailureClassification,
)
from durallm.observability.logger import DEFAULT_STRUCTURED_LOGGER, StructuredJsonLogger
from durallm.protocol.ir import (
    NormalizedRequest,
    NormalizedResponse,
)
from durallm.providers.adapters import (
    DEFAULT_ADAPTER_REGISTRY,
    ProviderAdapterRegistry,
)
from durallm.providers.base import (
    ProviderByteStream,
    ProviderStreamError,
    RequestCancellation,
)
from durallm.routing.budget import (
    BudgetReservationStore,
)
from durallm.routing.cache import compute_prefix_hash
from durallm.routing.decision import RoutingDecision
from durallm.routing.keys import KeyRotationPool
from durallm.routing.requirements import RequirementVector
from durallm.routing.resources import ResourceLaneStore
from durallm.routing.router import CapabilityRouter
from durallm.storage.contracts import AttemptStore
from durallm.validation.response import ResponseValidator

logger = logging.getLogger("durallm.execution")


@dataclass
class NativeStreamHandle:
    """One provider-native stream after its first downstream-visible bytes.

    This object has a deliberately narrow safety contract: its provider may
    not be replaced once ``first_chunk`` exists.  Failure after that point is
    terminal for this client stream and is reported to the caller so it can
    emit an explicit continuation/interruption event.
    """

    stream: ProviderByteStream
    stream_iterator: Iterator[bytes]
    first_chunk: bytes
    endpoint: Endpoint
    decision: RoutingDecision
    attempt: AttemptRecord
    ledger: AttemptLedger
    breaker: object
    executor: "GatewayExecutor"
    cancellation: RequestCancellation
    _finished: bool = False

    def _finish_success(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.attempt.finish(success=True, status_code=200)
        self.breaker.record_success(self.attempt.latency_ms)
        self.executor.health_store.record_success(self.endpoint.id, self.attempt.latency_ms)
        self.executor._record_lane_outcome(self.endpoint)
        self.ledger.record_attempt(self.attempt)
        self.executor._emit_attempt(self.attempt)
        self.executor._finish_durable_attempt(self.attempt, "stream_succeeded")

    def _finish_failure(self, error: BaseException) -> None:
        if self._finished:
            return
        self._finished = True
        phase = error.phase if isinstance(error, ProviderStreamError) else "connection"
        status = 597 if phase in {"idle", "total", "first_byte"} else 598
        classified = classify_failure(str(error), status_code=status)
        self.attempt.finish(success=False, status_code=status, failure=classified)
        self.breaker.record_failure(self.attempt.latency_ms, failure_classification=classified)
        self.executor.health_store.record_failure(
            endpoint_id=self.endpoint.id,
            latency_ms=self.attempt.latency_ms,
            error_message=classified.message[:160],
            cooldown_seconds=30.0,
        )
        self.executor._record_lane_outcome(self.endpoint, classified)
        self.ledger.record_attempt(self.attempt)
        self.executor._emit_attempt(self.attempt)
        self.executor._finish_durable_attempt(self.attempt, "stream_interrupted", str(error))

    def iter_bytes(self) -> Iterator[bytes]:
        try:
            yield self.first_chunk
            for chunk in self.stream_iterator:
                if chunk:
                    yield chunk
        except BaseException as exc:
            self._finish_failure(exc)
            raise
        else:
            self._finish_success()
        finally:
            self.stream.close()

    def cancel(self, reason: str = "downstream client disconnected") -> None:
        self.cancellation.cancel(reason)
        self.stream.close()


class GatewayExecutor:
    """
    Unified execution engine implementing semantic failover:
    failure -> classify -> recompute candidates -> adapt request/state -> execute -> validate -> continue
    """

    def __init__(
        self,
        capability_registry: Optional[CapabilityRegistry] = None,
        breaker_registry: Optional[CircuitBreakerRegistry] = None,
        adapter_registry: Optional[ProviderAdapterRegistry] = None,
        health_store: Optional[HealthTelemetryStore] = None,
        policy: Optional[ExecutionPolicy] = None,
        context_manager: Optional[ContextManager] = None,
        tool_validator: Optional[ToolCallValidator] = None,
        tool_ledger: Optional[ToolExecutionLedger] = None,
        response_validator: Optional[ResponseValidator] = None,
        router: Optional[CapabilityRouter] = None,
        attempt_store: Optional[AttemptStore] = None,
        budget_store: Optional[BudgetReservationStore] = None,
        lane_store: Optional[ResourceLaneStore] = None,
        key_pool: Optional[KeyRotationPool] = None,
        sleeper: Callable[[float], None] = time.sleep,
        events: Optional[StructuredJsonLogger] = None,
        pool_manager: Optional[Any] = None,
    ):
        self.capability_registry = capability_registry or DEFAULT_CAPABILITY_REGISTRY
        self.breaker_registry = breaker_registry or DEFAULT_BREAKER_REGISTRY
        self.adapter_registry = adapter_registry or DEFAULT_ADAPTER_REGISTRY
        self.health_store = health_store if health_store is not None else HealthTelemetryStore()
        self.pool_manager = pool_manager
        self.policy = policy or ExecutionPolicy()
        self.context_manager = context_manager or ContextManager()
        self.tool_validator = tool_validator or ToolCallValidator(strict=True)
        self.tool_ledger = tool_ledger or DEFAULT_TOOL_LEDGER
        self.response_validator = response_validator or ResponseValidator(tool_validator=self.tool_validator)
        self.attempt_store = attempt_store
        self.budget_store = budget_store if budget_store is not None else BudgetReservationStore()
        self.key_pool = key_pool if key_pool is not None else KeyRotationPool()
        self._sleep = sleeper
        self.events = events or DEFAULT_STRUCTURED_LOGGER
        self.failover_listeners: List[Callable[[Dict[str, Any]], None]] = []
        # The router must read the same telemetry this executor writes, or scoring never sees it.
        self.router = router or CapabilityRouter(
            capability_registry=self.capability_registry,
            breaker_registry=self.breaker_registry,
            health_store=self.health_store,
            lane_store=lane_store,
        )

    def on_failover(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        """Register a callback listener for failover telemetry events."""
        self.failover_listeners.append(listener)

    @staticmethod
    def _attempt_payload(attempt: AttemptRecord, status: str, detail: str = "") -> Dict[str, object]:
        """Serialize an attempt without leaking provider request bodies or credentials."""
        return {
            "request_id": attempt.request_id,
            "operation_id": attempt.operation_id,
            "endpoint_id": attempt.endpoint_id,
            "provider": attempt.provider,
            "model": attempt.model,
            "attempt_index": attempt.attempt_index,
            "fallback_index": attempt.fallback_index,
            "compacted": attempt.compacted,
            "status": status,
            "status_code": attempt.status_code,
            "success": attempt.success,
            "latency_ms": attempt.latency_ms,
            "failure_reason": attempt.failure.reason.value if attempt.failure else None,
            "detail": detail[:500],
        }

    def _prepare_durable_attempt(self, attempt: AttemptRecord) -> None:
        if self.attempt_store is not None:
            self.attempt_store.prepare_attempt(
                attempt.attempt_id,
                attempt.request_id,
                self._attempt_payload(attempt, "prepared"),
            )

    def _finish_durable_attempt(self, attempt: AttemptRecord, status: str, detail: str = "") -> None:
        if self.attempt_store is not None:
            self.attempt_store.finish_attempt(
                attempt.attempt_id,
                status,
                self._attempt_payload(attempt, status, detail),
            )

    def _record_lane_outcome(
        self,
        endpoint: Endpoint,
        failure: Optional[FailureClassification] = None,
    ) -> None:
        """Keep a rate-limited credential/deployment lane out of new traffic."""
        if not endpoint.lane_key:
            return
        if failure is None:
            self.router.lane_store.set_available(endpoint.lane_key)
            return
        if failure.reason in (FailoverReason.rate_limit, FailoverReason.upstream_rate_limit):
            self.router.lane_store.set_unavailable(
                endpoint.lane_key,
                reason=failure.reason.value,
                # Omitted Retry-After is not permission to black-hole a lane.
                retry_after_seconds=failure.retry_after_seconds or 60.0,
            )

    @staticmethod
    def _streaming_prepared_request(prepared: object) -> object:
        """Set the native provider streaming flag without mutating the retry body."""
        try:
            payload = json.loads(prepared.body_bytes.decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("Provider request cannot be converted into a native stream") from exc
        if not isinstance(payload, dict):
            raise ConfigurationError("Provider request must be a JSON object to use native streaming")
        payload["stream"] = True
        headers = dict(prepared.headers)
        headers["Accept"] = "text/event-stream"
        return replace(prepared, body_bytes=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)

    def open_native_stream(
        self,
        request: NormalizedRequest,
        pool: str = "general_agent",
        strategy: Optional[str] = None,
        api_keys: Optional[Dict[str, str]] = None,
        client_protocol: str = "openai",
        deadline_ms: float = 300000.0,
    ) -> NativeStreamHandle:
        """Open a raw provider-native stream, failing over only before bytes escape.

        Native streaming deliberately excludes tool-bearing turns.  Tool calls
        need whole-response validation and durable submission handling, so the
        proxy keeps those requests in atomic-buffered mode until a transaction
        aware streamed tool protocol is available.
        """
        if request.tools:
            raise ConfigurationError("Native streaming is unavailable for tool-bearing turns; use atomic_buffered mode")

        deadline = Deadline(total_timeout_ms=deadline_ms)
        ledger = AttemptLedger(self.policy)
        keys = dict(api_keys or {})
        prefix_hash = compute_prefix_hash(request)
        req_vector = RequirementVector(
            require_tools=False,
            task_class="coding" if pool == "coding" else "general",
            estimated_input_tokens=estimate_tokens(request),
            expected_output_tokens=request.max_output_tokens or 4096,
            prefix_hash=prefix_hash,
        )
        excluded_endpoints: List[str] = []
        last_failure_reason: Optional[str] = None
        attempt_idx = 0
        rate_limit_waits = 0
        max_rate_limit_waits = 3

        while not deadline.is_expired() and ledger.total_attempts < self.policy.max_total_attempts:
            attempt_idx += 1
            deadline.check()
            endpoint, decision = self.router.select_candidate(
                requirements=req_vector,
                pool=pool,
                strategy=strategy,
                request_id=request.request_id,
                excluded_endpoints=excluded_endpoints,
                fallback_reason=last_failure_reason,
            )
            if endpoint is None:
                temp_rate_limited_eps = []
                delays = []
                for att in ledger.attempts:
                    if att.endpoint_id in excluded_endpoints and att.failure:
                        if (
                            att.failure.reason in (FailoverReason.rate_limit, FailoverReason.upstream_rate_limit)
                            and not att.failure.is_permanent
                            and (att.failure.retry_after_seconds is None or att.failure.retry_after_seconds < 300.0)
                        ):
                            if att.endpoint_id not in temp_rate_limited_eps:
                                temp_rate_limited_eps.append(att.endpoint_id)
                                if att.failure.retry_after_seconds:
                                    delays.append(att.failure.retry_after_seconds)

                rem_sec = deadline.remaining_ms() / 1000.0
                if temp_rate_limited_eps and rate_limit_waits < max_rate_limit_waits and rem_sec > 10.0:
                    rate_limit_waits += 1
                    target_delay = min(delays) if delays else 8.0
                    wait_s = max(2.0, min(target_delay, 15.0, rem_sec - 5.0))
                    self.events.info(
                        "stream_rate_limit_rollover_wait",
                        pool=pool,
                        endpoints=temp_rate_limited_eps,
                        wait_seconds=wait_s,
                        attempt=rate_limit_waits,
                    )
                    self._sleep(wait_s)
                    for ep_id in temp_rate_limited_eps:
                        if ep_id in excluded_endpoints:
                            excluded_endpoints.remove(ep_id)
                        for ep_obj in self.capability_registry.endpoints_for_pool(pool):
                            if ep_obj.id == ep_id and ep_obj.lane_key:
                                self.router.lane_store.set_available(ep_obj.lane_key)
                    ledger.reset_for_rate_limit_retry(temp_rate_limited_eps)
                    last_failure_reason = "rate_limit_rollover_retry"
                    continue
                break
            # Raw pass-through is safe only when the client and upstream use
            # the same event protocol. Cross-protocol calls remain atomic.
            if endpoint.protocol != client_protocol:
                excluded_endpoints.append(endpoint.id)
                last_failure_reason = "stream_protocol_mismatch"
                continue
            profile = endpoint.profile or self.capability_registry.get_profile(endpoint.provider, endpoint.model)
            if not profile.supports_streaming:
                excluded_endpoints.append(endpoint.id)
                last_failure_reason = "streaming_not_supported"
                continue
            try:
                ledger.validate_next_candidate(endpoint.id)
                breaker = self.breaker_registry.get_or_create(endpoint.resource_key)
                breaker.acquire_permission()
            except (BreakerOpenError, ProbeAdmissionDeniedError):
                excluded_endpoints.append(endpoint.id)
                last_failure_reason = "breaker_admission_denied"
                continue
            except Exception:
                excluded_endpoints.append(endpoint.id)
                last_failure_reason = "stream_route_rejected"
                continue

            max_supported_out = profile.max_output_tokens or 4096
            desired_output = min(request.max_output_tokens or max_supported_out, max_supported_out)
            safety_margin = 2048
            est_in = estimate_tokens(request)
            if profile.context_window - safety_margin - desired_output < est_in and desired_output > 1024:
                desired_output = max(1024, min(desired_output, profile.context_window - safety_margin - est_in))

            budget = ContextBudget(
                model_context_window=profile.context_window,
                desired_output_tokens=desired_output,
                safety_margin_tokens=safety_margin,
            )
            try:
                adapted_request, was_compacted = self.context_manager.compact(request, budget)
                adapted_request = replace(adapted_request, max_output_tokens=desired_output)
            except ContextOverflowError:
                excluded_endpoints.append(endpoint.id)
                last_failure_reason = "context_overflow"
                continue

            adapter = self.adapter_registry.get_adapter(endpoint.provider, protocol=endpoint.protocol)
            if not hasattr(adapter, "open_stream"):
                excluded_endpoints.append(endpoint.id)
                last_failure_reason = "adapter_has_no_native_stream"
                continue
            key_val, key_id = self.key_pool.get_active_key(endpoint, keys)
            if key_val is None:
                key_val = keys.get(endpoint.env_key, "") if endpoint.env_key else ""
            prepared = self._streaming_prepared_request(adapter.prepare_request(endpoint, adapted_request, api_key=key_val))
            attempt = AttemptRecord(
                request_id=request.request_id,
                endpoint_id=endpoint.id,
                provider=endpoint.provider,
                model=endpoint.model,
                attempt_index=attempt_idx,
                fallback_index=ledger.fallback_count,
                compacted=was_compacted,
            )
            self._prepare_durable_attempt(attempt)
            cancellation = RequestCancellation()
            result = adapter.open_stream(prepared, deadline.transport_timeouts(streaming=True), cancellation)
            if result.status_code == 200 and result.stream is not None:
                stream_iterator = result.stream.iter_bytes()
                try:
                    first_chunk = next(stream_iterator)
                except StopIteration:
                    first_chunk = b""
                    first_error: Optional[BaseException] = ProviderStreamError("first_byte", "provider closed an empty stream")
                except BaseException as exc:
                    first_chunk = b""
                    first_error = exc
                else:
                    first_error = None
                if first_chunk:
                    attempt.ttft_ms = max(result.duration_ms, (time.monotonic() - attempt.start_time_monotonic) * 1000.0)
                    return NativeStreamHandle(
                        stream=result.stream,
                        stream_iterator=stream_iterator,
                        first_chunk=first_chunk,
                        endpoint=endpoint,
                        decision=decision,
                        attempt=attempt,
                        ledger=ledger,
                        breaker=breaker,
                        executor=self,
                        cancellation=cancellation,
                    )
                result.stream.close()
                failure_source: object = first_error or "provider returned an empty stream"
                failure_status = 597 if isinstance(first_error, ProviderStreamError) else 502
            else:
                failure_source = result.body.decode("utf-8", errors="ignore")
                failure_status = result.status_code

            classified = classify_failure(failure_source, status_code=failure_status, headers=result.headers)
            attempt.finish(success=False, status_code=failure_status, failure=classified)
            breaker.record_failure(attempt.latency_ms, failure_classification=classified)

            cd_seconds: Optional[float] = None
            if classified.reason in (FailoverReason.rate_limit, FailoverReason.upstream_rate_limit):
                cd_seconds = min(120.0, max(30.0, float(classified.retry_after_seconds or 60.0)))
            elif classified.reason in (FailoverReason.server_error, FailoverReason.timeout, FailoverReason.connection_refused, FailoverReason.overloaded):
                cd_seconds = 30.0

            quota_seconds: Optional[float] = None
            if classified.reason == FailoverReason.billing:
                quota_seconds = float(classified.retry_after_seconds or 86400.0)

            self.health_store.record_failure(
                endpoint_id=endpoint.id,
                latency_ms=attempt.latency_ms,
                error_message=classified.message[:160],
                cooldown_seconds=None,
                quota_exhausted_seconds=quota_seconds,
                is_permanent=classified.is_permanent,
            )

            if classified.reason == FailoverReason.billing:
                msg_lower = (classified.message or "").lower()
                is_account_wide = "free-models-per-day" in msg_lower or "free model requests" in msg_lower or endpoint.provider.lower() == "openrouter"
                retry_sec = quota_seconds or (calculate_seconds_until_utc_midnight() if is_account_wide else 86400.0)
                pm = getattr(self, "pool_manager", None)
                if pm:
                    route_id = endpoint.id.split(":")[-1]
                    pm.mark_quota_exhausted(pool, route_id, seconds=retry_sec)
                    if is_account_wide:
                        pm.mark_provider_quota_exhausted(pool, endpoint.provider, seconds=retry_sec)
                if is_account_wide:
                    for ep_item in self.capability_registry.endpoints_for_pool(pool):
                        if ep_item.provider.lower() == endpoint.provider.lower() and ep_item.id not in excluded_endpoints:
                            excluded_endpoints.append(ep_item.id)

            if classified.is_permanent or classified.reason == FailoverReason.model_not_found:
                self.router.mark_dead(endpoint.id, provider=endpoint.provider, model=endpoint.model, reason=classified.message[:160])
                pm = getattr(self, "pool_manager", None)
                if pm:
                    pm.mark_deprecated(pool, endpoint.model)
                if endpoint.id not in excluded_endpoints:
                    excluded_endpoints.append(endpoint.id)

            self._record_lane_outcome(endpoint, classified)
            ledger.record_attempt(attempt)
            self._emit_attempt(attempt)
            self._finish_durable_attempt(attempt, "stream_open_failed", classified.message)
            last_failure_reason = classified.reason.value

            if not classified.should_fallback:
                raise NonRecoverableFailureError(
                    f"{endpoint.id} failed before native stream visibility: {classified.message[:160]}",
                    classification=classified,
                    endpoint_id=endpoint.id,
                )
            if classified.retryable and ledger.can_attempt_endpoint(endpoint.id):
                backoff_s = self.policy.retry.compute_backoff_seconds(
                    ledger.attempts_on(endpoint.id), retry_after=classified.retry_after_seconds
                )
                if backoff_s < deadline.remaining_ms() / 1000.0:
                    self._sleep(backoff_s)
                    self.router.lane_store.set_available(endpoint.lane_key)
                    continue

            # Route has failed over: activate cooldown horizons
            if cd_seconds:
                self.health_store.record_failure(
                    endpoint_id=endpoint.id,
                    latency_ms=0.0,
                    cooldown_seconds=cd_seconds,
                )
                pm = getattr(self, "pool_manager", None)
                if pm:
                    pm.mark_cooldown(pool, endpoint.provider, cd_seconds)

            excluded_endpoints.append(endpoint.id)
            ledger.mark_fallback()

        raise NoHealthyRouteError(f"No native streaming route is available in pool '{pool}'", pool=pool)

    def execute(
        self,
        request: NormalizedRequest,
        pool: str = "general_agent",
        strategy: Optional[str] = None,
        deadline_ms: float = 300000.0,
        api_keys: Optional[Dict[str, str]] = None,
        requirements: Optional[RequirementVector] = None,
    ) -> Tuple[NormalizedResponse, RoutingDecision, AttemptLedger]:
        """
        Execute request with bounded retries, semantic failover, and cycle protection.
        `requirements` carries caller constraints (cost ceiling, latency budget, provider filters);
        tool requirement, task class and token estimates are always derived from the request.
        """
        deadline = Deadline(total_timeout_ms=deadline_ms)
        ledger = AttemptLedger(self.policy)
        keys = dict(api_keys or {})
        prefix_hash = compute_prefix_hash(request)

        # Build requirement vector from request (on top of any caller-supplied constraints)
        req_vector = replace(
            requirements or RequirementVector(),
            require_tools=bool(request.tools) or bool(requirements and requirements.require_tools),
            task_class="coding" if pool == "coding" else "general",
            estimated_input_tokens=estimate_tokens(request),
            expected_output_tokens=request.max_output_tokens or 0,
            prefix_hash=prefix_hash,
        )

        excluded_endpoints: List[str] = []
        attempt_idx = 0
        last_failure_reason: Optional[str] = None
        last_endpoint: Optional[Endpoint] = None
        fallback_marked = False  # True once the retry loop has already counted the pending hop
        # Spec §15: after a size rejection, shrink the assumed window and retry the same candidate once.
        window_shrink: Dict[str, float] = {}
        output_cap_retried: Set[str] = set()
        rate_limit_waits = 0
        max_rate_limit_waits = 3

        while not deadline.is_expired() and ledger.total_attempts < self.policy.max_total_attempts:
            attempt_idx += 1
            deadline.check()

            # 1. Candidate Selection
            endpoint, decision = self.router.select_candidate(
                requirements=req_vector,
                pool=pool,
                strategy=strategy,
                request_id=request.request_id,
                excluded_endpoints=excluded_endpoints,
                fallback_reason=last_failure_reason,
            )

            if not endpoint:
                temp_rate_limited_eps = []
                delays = []
                for att in ledger.attempts:
                    if att.endpoint_id in excluded_endpoints and att.failure:
                        if (
                            att.failure.reason in (FailoverReason.rate_limit, FailoverReason.upstream_rate_limit)
                            and not att.failure.is_permanent
                            and (att.failure.retry_after_seconds is None or att.failure.retry_after_seconds < 300.0)
                        ):
                            if att.endpoint_id not in temp_rate_limited_eps:
                                temp_rate_limited_eps.append(att.endpoint_id)
                                if att.failure.retry_after_seconds:
                                    delays.append(att.failure.retry_after_seconds)

                rem_sec = deadline.remaining_ms() / 1000.0
                if temp_rate_limited_eps and rate_limit_waits < max_rate_limit_waits and rem_sec > 10.0:
                    rate_limit_waits += 1
                    target_delay = min(delays) if delays else 8.0
                    wait_s = max(2.0, min(target_delay, 15.0, rem_sec - 5.0))
                    logger.info(
                        "All candidates exhausted in pool '%s', but %d candidate(s) are on temporary rate limit cooldown. "
                        "Waiting %.1fs for rate limit rollover (remaining deadline: %.1fs)...",
                        pool, len(temp_rate_limited_eps), wait_s, rem_sec,
                    )
                    self.events.info(
                        "rate_limit_rollover_wait",
                        pool=pool,
                        endpoints=temp_rate_limited_eps,
                        wait_seconds=wait_s,
                        attempt=rate_limit_waits,
                    )
                    self._sleep(wait_s)

                    for ep_id in temp_rate_limited_eps:
                        if ep_id in excluded_endpoints:
                            excluded_endpoints.remove(ep_id)
                        for ep_obj in self.capability_registry.endpoints_for_pool(pool):
                            if ep_obj.id == ep_id and ep_obj.lane_key:
                                self.router.lane_store.set_available(ep_obj.lane_key)

                    ledger.reset_for_rate_limit_retry(temp_rate_limited_eps)
                    last_failure_reason = "rate_limit_rollover_retry"
                    continue

                logger.error("No candidate matches requirements in pool '%s'", pool)
                self.events.error(
                    "request_exhausted", request_id=request.request_id, pool=pool,
                    attempts=ledger.total_attempts, last_reason=last_failure_reason,
                    detail="no healthy candidate left" if excluded_endpoints else "no candidate matches requirements",
                )
                raise NoHealthyRouteError(f"No healthy candidate available in pool '{pool}'", pool=pool)

            # Check cycle & budget protection
            try:
                ledger.validate_next_candidate(endpoint.id)
            except Exception as e:
                logger.warning("Ledger validation rejected candidate %s: %s", endpoint.id, e)
                excluded_endpoints.append(endpoint.id)
                continue

            # 2. Context Adaptation (Budget Sizing)
            profile = endpoint.profile or self.capability_registry.get_profile(endpoint.provider, endpoint.model)
            shrink = window_shrink.get(endpoint.id, 1.0)
            target_context = int(profile.context_window * shrink)

            max_supported_out = profile.max_output_tokens or 4096
            desired_output = min(request.max_output_tokens or max_supported_out, max_supported_out)
            safety_margin = 2048 if target_context > 8000 else min(512, int(target_context * 0.1))
            est_in = estimate_tokens(request)
            if target_context - safety_margin - desired_output < est_in and desired_output > 1024:
                desired_output = max(1024, min(desired_output, target_context - safety_margin - est_in))

            budget = ContextBudget(
                model_context_window=target_context,
                desired_output_tokens=desired_output,
                safety_margin_tokens=safety_margin,
            )
            try:
                adapted_request, was_compacted = self.context_manager.compact(request, budget)
                adapted_request = replace(adapted_request, max_output_tokens=desired_output)
            except ContextOverflowError as c_err:
                logger.warning("Protected context does not fit %s: %s", endpoint.id, c_err)
                excluded_endpoints.append(endpoint.id)
                continue
            if shrink < 1.0 and not was_compacted:
                # The provider rejected the size but nothing is compactable: do not resend the same payload.
                logger.warning("Nothing to compact for %s after size rejection; falling back", endpoint.id)
                excluded_endpoints.append(endpoint.id)
                continue

            # Record observable FailoverPlan if switching endpoints
            if last_endpoint and last_endpoint.id != endpoint.id:
                if not fallback_marked:
                    # Switch forced by breaker/admission rather than retry exhaustion; still a hop.
                    ledger.mark_fallback()
                fallback_marked = False
                fplan = FailoverPlan(
                    request_id=request.request_id,
                    source_endpoint=last_endpoint.id,
                    target_endpoint=endpoint.id,
                    failover_reason=last_failure_reason or "endpoint_failover",
                    context_tokens_before=estimate_tokens(request),
                    context_tokens_after=estimate_tokens(adapted_request),
                    context_compaction_applied=was_compacted,
                    remaining_deadline_ms=deadline.remaining_ms(),
                )
                ledger.record_failover_plan(fplan)
                logger.info("Semantic FailoverPlan created: %s -> %s (reason: %s)", last_endpoint.id, endpoint.id, fplan.failover_reason)
                self.events.warning(
                    "failover_triggered",
                    request_id=request.request_id,
                    source_endpoint=last_endpoint.id,
                    target_endpoint=endpoint.id,
                    reason=fplan.failover_reason,
                )
                event_data = {
                    "request_id": request.request_id,
                    "source_endpoint": last_endpoint.id,
                    "target_endpoint": endpoint.id,
                    "reason": fplan.failover_reason,
                    "timestamp": time.time(),
                }
                for listener in list(self.failover_listeners):
                    try:
                        listener(event_data)
                    except Exception as listener_err:
                        logger.warning("Error in failover listener: %s", listener_err)

            last_endpoint = endpoint

            # 3. Prepare and Execute Request
            adapter = self.adapter_registry.get_adapter(endpoint.provider, protocol=endpoint.protocol)
            key_val, key_id = self.key_pool.get_active_key(endpoint, keys)
            if key_val is None:
                key_val = keys.get(endpoint.env_key, "") if endpoint.env_key else ""
            prepared = adapter.prepare_request(endpoint, adapted_request, api_key=key_val)

            attempt_timeout_sec = deadline.per_attempt_timeout_seconds(
                endpoint.provider,
                estimated_input_tokens=req_vector.estimated_input_tokens,
            )
            attempt_rec = AttemptRecord(
                request_id=request.request_id,
                endpoint_id=endpoint.id,
                provider=endpoint.provider,
                model=endpoint.model,
                attempt_index=attempt_idx,
                fallback_index=ledger.fallback_count,
                compacted=was_compacted,
            )
            # Reserve before dispatch using the maximum expected turn cost.
            # A failed/ambiguous upstream attempt settles this conservative
            # amount; only a known successful response can settle lower.
            budget_reservation_id: Optional[str] = None
            reserved_amount_usd = 0.0
            if req_vector.budget_limit_usd is not None:
                budget_reservation_id = "%s:%s" % (request.request_id, attempt_rec.attempt_id)
                reserved_amount_usd = req_vector.estimated_cost_usd(profile)
                reservation = self.budget_store.reserve(
                    reservation_id=budget_reservation_id,
                    scope=req_vector.budget_scope or request.request_id,
                    amount_usd=reserved_amount_usd,
                    limit_usd=req_vector.budget_limit_usd,
                )
                if reservation is None:
                    logger.info("Budget reservation rejected for %s", endpoint.id)
                    excluded_endpoints.append(endpoint.id)
                    last_failure_reason = "budget_reservation_denied"
                    continue

            # Breaker admission happens after all local no-dispatch checks so
            # a rejected budget cannot consume a half-open probe permit.
            breaker = self.breaker_registry.get_or_create(endpoint.resource_key)
            try:
                breaker.acquire_permission()
            except (BreakerOpenError, ProbeAdmissionDeniedError) as b_err:
                logger.warning("Breaker admission denied for %s: %s", endpoint.id, b_err)
                if budget_reservation_id is not None:
                    self.budget_store.release(budget_reservation_id)
                excluded_endpoints.append(endpoint.id)
                continue
            # Persist dispatch intent before an adapter can send bytes to an
            # upstream provider. A later process can distinguish a request it
            # never issued from one whose outcome is merely unknown.
            self._prepare_durable_attempt(attempt_rec)

            # Execute attempt
            try:
                exec_result = adapter.execute(prepared, timeout_seconds=attempt_timeout_sec)
            except Exception as exec_err:
                classified = classify_failure(exec_err, status_code=502)
                attempt_rec.finish(success=False, status_code=502, failure=classified)
                breaker.record_failure(attempt_rec.latency_ms, failure_classification=classified)
                cd_seconds = 30.0  # Tier 1 transient cloud probe cooldown
                self.health_store.record_failure(
                    endpoint_id=endpoint.id,
                    latency_ms=attempt_rec.latency_ms,
                    error_message=classified.message[:160],
                    cooldown_seconds=cd_seconds,
                )
                try:
                    from durallm.pools import POOL_MANAGER
                    POOL_MANAGER.mark_cooldown(pool, endpoint.provider, cd_seconds)
                except Exception:
                    pass
                self._record_lane_outcome(endpoint, classified)
                ledger.record_attempt(attempt_rec)
                self._emit_attempt(attempt_rec)
                if budget_reservation_id is not None:
                    # The adapter may have sent bytes before reporting a
                    # transport error, so release would permit an overspend.
                    self.budget_store.settle(budget_reservation_id, reserved_amount_usd)
                self._finish_durable_attempt(attempt_rec, "transport_error", str(exec_err))
                raise

            if exec_result.status_code == 200:
                try:
                    norm_response = adapter.normalize_response(endpoint, exec_result)

                    # 5. Body sanity (spec §51: HTTP 200 is not semantic success), then tool validation
                    sanity_failure = self.response_validator.check_sanity(norm_response)
                    validation_passed = sanity_failure is None
                    for tc_index, tc in enumerate(norm_response.tool_calls if validation_passed else []):
                        # Preserve the provider's tool-call id for the client (ADR 0005); only mint one if absent.
                        if not tc.id:
                            tc.id = f"call_{request.request_id[:8]}_{attempt_idx}_{tc_index}"
                        # Ledger key is scoped per request and attempt so retries never collide.
                        tc_id = f"{request.request_id}:att{attempt_idx}:{tc.id}"
                        # Register in tool ledger
                        self.tool_ledger.register_tool_call(
                            tool_call_id=tc_id,
                            logical_operation_id=request.request_id,
                            tool_name=tc.name,
                            arguments=tc.arguments,
                        )

                        # Check idempotency receipt
                        has_receipt, cached_receipt = self.tool_ledger.check_idempotency(
                            logical_operation_id=request.request_id,
                            tool_name=tc.name,
                            arguments=tc.arguments,
                        )
                        # Clients commit receipts against this key (tool ids alone are not unique across attempts).
                        tc.metadata["ledger_call_id"] = tc_id
                        if has_receipt:
                            logger.info("Idempotent tool call detected for '%s'; using cached execution receipt", tc.name)
                            self.tool_ledger.mark_replayed(tc_id, cached_receipt)
                            tc.metadata["replayed"] = True
                            tc.metadata["execution_receipt"] = cached_receipt
                        elif self.tool_ledger.has_indeterminate_operation(
                            logical_operation_id=request.request_id,
                            tool_name=tc.name,
                            arguments=tc.arguments,
                        ):
                            # A submitted side effect without a durable receipt
                            # is not a retry candidate. Returning it to a tool
                            # runner would silently permit duplicate execution.
                            self.tool_ledger.mark_indeterminate(
                                tc_id,
                                "A prior submission has no durable acknowledgement",
                            )
                            tc.metadata["indeterminate"] = True
                            raise IndeterminateToolOperationError(request.request_id, tc.name)

                        # Find schema
                        tool_schema = next((t.parameters for t in request.tools if t.name == tc.name), None)
                        # Providers retain malformed tool JSON in ``raw_arguments``
                        # but expose an empty mapping after parsing.  Validate the
                        # source bytes so the syntactic-repair policy can act.
                        validation_input = tc.raw_arguments if tc.raw_arguments and not tc.arguments else tc.arguments
                        val_report = self.tool_validator.validate_tool_call(
                            tool_name=tc.name,
                            arguments=validation_input,
                            schema=tool_schema,
                            known_tools=[t.name for t in request.tools],
                        )
                        # Feeds the scorer's tool-reliability term; without this it was never observed.
                        self.health_store.record_tool_outcome(endpoint.id, val_report.is_executable)
                        if not val_report.is_executable:
                            logger.warning("Tool validation rejected tool call '%s': %s", tc.name, val_report.error_message)
                            self.tool_ledger.mark_failed(tc_id, val_report.error_message)
                            validation_passed = False
                            break
                        else:
                            tc.arguments = val_report.validated_arguments
                            # The native OpenAI encoder prefers ``raw_arguments``. Once a deterministic
                            # syntactic repair is accepted, return the validated JSON—not the malformed
                            # pre-repair source—to the client/tool runner. Retain the original for audit.
                            if val_report.normalizations_applied:
                                tc.metadata["raw_arguments_before_normalization"] = tc.raw_arguments
                                tc.raw_arguments = json.dumps(tc.arguments, ensure_ascii=False)
                            self.tool_ledger.mark_validated(tc_id)

                    if validation_passed:
                        # Success!
                        breaker.record_success(exec_result.duration_ms)
                        self.health_store.record_success(endpoint.id, exec_result.duration_ms)
                        self._record_lane_outcome(endpoint)
                        if prefix_hash:
                            self.router.cache_tracker.record_warm_cache(endpoint.id, prefix_hash)
                        attempt_rec.finish(success=True, status_code=200)
                        ledger.record_attempt(attempt_rec)
                        self._emit_attempt(attempt_rec)

                        # Estimate cost
                        in_tokens = estimate_tokens(adapted_request)
                        out_tokens = estimate_tokens(norm_response.content or "")
                        cost = ((in_tokens / 1_000_000.0) * profile.input_price_per_1m) + ((out_tokens / 1_000_000.0) * profile.output_price_per_1m)
                        ledger.add_cost(cost)
                        if budget_reservation_id is not None:
                            self.budget_store.settle(budget_reservation_id, cost)
                        self._finish_durable_attempt(attempt_rec, "succeeded")

                        return norm_response, decision, ledger
                    else:
                        # Semantic failure: empty/oversized body, or a malformed tool call
                        if sanity_failure is not None:
                            logger.warning("Response sanity check rejected %s: %s", endpoint.id, sanity_failure.error_message)
                            reason = (
                                FailoverReason.empty_completion
                                if sanity_failure.rejection_reason == "empty_response"
                                else FailoverReason.output_cap_exceeded
                            )
                            message = sanity_failure.error_message
                        else:
                            reason = FailoverReason.malformed_tool_call
                            message = "Model generated invalid tool arguments"
                        classified = FailureClassification(
                            category=FailureCategory.SEMANTIC_AGENT_FAILURE,
                            reason=reason,
                            should_fallback=True,
                            retryable=False,
                            poisons_health=False,
                            status_code=200,
                            message=message,
                        )
                except IndeterminateToolOperationError:
                    attempt_rec.finish(success=False, status_code=409)
                    if budget_reservation_id is not None:
                        self.budget_store.settle(budget_reservation_id, reserved_amount_usd)
                    ledger.record_attempt(attempt_rec)
                    self._emit_attempt(attempt_rec)
                    self._finish_durable_attempt(attempt_rec, "blocked_indeterminate_operation")
                    raise
                except Exception as norm_err:
                    body_preview = exec_result.body[:200] if exec_result.body else b""
                    logger.warning("Failed to normalize response from %s: %s (body preview: %r)", endpoint.id, norm_err, body_preview)
                    classified = classify_failure(norm_err, status_code=502)
            else:
                # Classify error
                raw_msg = exec_result.body.decode("utf-8", errors="ignore")
                classified = classify_failure(raw_msg, status_code=exec_result.status_code, headers=exec_result.headers)

            # Intra-provider multi-key rotation: if 429 and an alternative key exists, rotate and retry locally
            if (
                classified.reason == FailoverReason.rate_limit
                and key_id
                and self.key_pool.has_alternative_key(endpoint, keys, key_id)
            ):
                self.key_pool.record_rate_limit(key_id, cooldown_seconds=classified.retry_after_seconds)
                self.events.info(
                    "key_rotated_on_rate_limit",
                    endpoint=endpoint.id,
                    old_key=key_id,
                    retry_after=classified.retry_after_seconds,
                )
                attempt_rec.finish(success=False, status_code=exec_result.status_code, failure=classified)
                ledger.record_attempt(attempt_rec)
                self._emit_attempt(attempt_rec)
                if budget_reservation_id is not None:
                    self.budget_store.settle(budget_reservation_id, reserved_amount_usd)
                self._finish_durable_attempt(attempt_rec, "key_rotated_429", classified.message)
                continue

            # Cooldown horizons: Tier 1 (30s transient probe), Tier 2 (60s/Retry-After RPM/TPM), Tier 3 (24h Quota)
            cd_seconds: Optional[float] = None
            if classified.reason in (FailoverReason.rate_limit, FailoverReason.upstream_rate_limit):
                cd_seconds = min(120.0, max(30.0, float(classified.retry_after_seconds or 60.0)))
            elif classified.reason in (FailoverReason.server_error, FailoverReason.timeout, FailoverReason.connection_refused, FailoverReason.overloaded):
                cd_seconds = 30.0  # Tier 1 transient cloud probe

            quota_seconds: Optional[float] = None
            if classified.reason == FailoverReason.billing:
                quota_seconds = float(classified.retry_after_seconds or 86400.0)

            # Record failure in breaker & health store
            breaker.record_failure(exec_result.duration_ms, failure_classification=classified)
            self.health_store.record_failure(
                endpoint_id=endpoint.id,
                latency_ms=exec_result.duration_ms,
                status_code=exec_result.status_code,
                error_message=classified.message[:160],
                cooldown_seconds=None,
                quota_exhausted_seconds=quota_seconds,
                is_permanent=classified.is_permanent,
            )

            if classified.reason == FailoverReason.billing:
                msg_lower = (classified.message or "").lower()
                is_account_wide = "free-models-per-day" in msg_lower or "free model requests" in msg_lower or endpoint.provider.lower() == "openrouter"
                retry_sec = quota_seconds or (calculate_seconds_until_utc_midnight() if is_account_wide else 86400.0)
                pm = getattr(self, "pool_manager", None)
                if pm:
                    route_id = endpoint.id.split(":")[-1]
                    pm.mark_quota_exhausted(pool, route_id, seconds=retry_sec)
                    if is_account_wide:
                        pm.mark_provider_quota_exhausted(pool, endpoint.provider, seconds=retry_sec)
                if hasattr(self.attempt_store, "record_quota_lockout"):
                    self.attempt_store.record_quota_lockout(
                        provider_id=endpoint.provider,
                        pool=pool,
                        route_id=endpoint.id.split(":")[-1],
                        expires_at=time.time() + retry_sec,
                        reason=classified.message[:160],
                    )
                    if is_account_wide:
                        self.attempt_store.record_quota_lockout(
                            provider_id=endpoint.provider,
                            pool=pool,
                            route_id="*",
                            expires_at=time.time() + retry_sec,
                            reason=classified.message[:160],
                        )
                if is_account_wide:
                    for ep_item in self.capability_registry.endpoints_for_pool(pool):
                        if ep_item.provider.lower() == endpoint.provider.lower() and ep_item.id not in excluded_endpoints:
                            excluded_endpoints.append(ep_item.id)

            if classified.is_permanent or classified.reason == FailoverReason.model_not_found:
                self.router.mark_dead(endpoint.id, provider=endpoint.provider, model=endpoint.model, reason=classified.message[:160])
                pm = getattr(self, "pool_manager", None)
                if pm:
                    pm.mark_deprecated(pool, endpoint.model)
                if endpoint.id not in excluded_endpoints:
                    excluded_endpoints.append(endpoint.id)

            self._record_lane_outcome(endpoint, classified)

            attempt_rec.finish(success=False, status_code=exec_result.status_code, failure=classified)
            ledger.record_attempt(attempt_rec)
            self._emit_attempt(attempt_rec)
            if budget_reservation_id is not None:
                self.budget_store.settle(budget_reservation_id, reserved_amount_usd)
            self._finish_durable_attempt(attempt_rec, "failed", classified.message)

            last_failure_reason = classified.reason.value

            # In-flight output cap clamping: if model rejected max_tokens/output cap, clamp and retry immediately
            if (
                classified.reason == FailoverReason.output_cap_exceeded
                and endpoint.id not in output_cap_retried
                and ledger.can_attempt_endpoint(endpoint.id)
            ):
                cap = parse_output_cap_from_error(classified.message)
                current_max = request.max_output_tokens or profile.max_output_tokens or 4096
                if cap and cap > 0:
                    clamped = max(1, cap - 64) if cap > 64 else cap
                else:
                    clamped = min(current_max, 1024)
                output_cap_retried.add(endpoint.id)
                request = replace(request, max_output_tokens=clamped)
                logger.info(
                    "Output cap exceeded on %s; clamped max_output_tokens from %d to %d and retrying candidate immediately",
                    endpoint.id, current_max, clamped,
                )
                continue

            # Retry / fallback decision is driven by the classification first, budget second.
            size_rejected = classified.reason in (FailoverReason.payload_too_large, FailoverReason.context_overflow)
            if size_rejected and endpoint.id not in window_shrink and ledger.can_attempt_endpoint(endpoint.id):
                # Extract explicit input TPM or payload token limit from error message/details
                token_limit = None
                if classified.details and isinstance(classified.details, dict) and "token_limit" in classified.details:
                    token_limit = classified.details["token_limit"]
                if not token_limit:
                    token_limit = parse_input_tpm_limit(classified.message)

                if token_limit and token_limit > 0:
                    target_tokens = max(1000, int(token_limit * 0.85))
                    shrink_ratio = max(0.1, min(0.95, target_tokens / profile.context_window))
                    window_shrink[endpoint.id] = shrink_ratio
                    logger.info(
                        "Input TPM / token limit %d detected from %s; shrinking window to %d tokens (ratio %.2f) and retrying immediately",
                        token_limit, endpoint.id, target_tokens, shrink_ratio,
                    )
                else:
                    window_shrink[endpoint.id] = 0.5
                    logger.info("Size rejection from %s; compacting and retrying the same candidate", endpoint.id)
                continue
            elif not classified.retryable or not ledger.can_attempt_endpoint(endpoint.id):
                if not classified.should_fallback:
                    self.events.error(
                        "request_failed_non_recoverable", request_id=request.request_id, pool=pool,
                        endpoint_id=endpoint.id, reason=classified.reason.value, attempts=ledger.total_attempts,
                    )
                    raise NonRecoverableFailureError(
                        f"{endpoint.id} failed with {classified.reason.value} and fallback is not permitted: {classified.message[:160]}",
                        classification=classified,
                        endpoint_id=endpoint.id,
                    )
                # Non-retryable, or retries on this endpoint exhausted: activate cooldown, exclude and step fallback
                if cd_seconds:
                    self.health_store.record_failure(
                        endpoint_id=endpoint.id,
                        latency_ms=0.0,
                        cooldown_seconds=cd_seconds,
                    )
                    pm = getattr(self, "pool_manager", None)
                    if pm:
                        pm.mark_cooldown(pool, endpoint.provider, cd_seconds)
                excluded_endpoints.append(endpoint.id)
                ledger.mark_fallback()
                fallback_marked = True
            else:
                # Retrying the same endpoint: back off (Retry-After takes precedence) within the deadline.
                backoff_s = self.policy.retry.compute_backoff_seconds(
                    ledger.attempts_on(endpoint.id), retry_after=classified.retry_after_seconds
                )
                if backoff_s >= deadline.remaining_ms() / 1000.0:
                    # Waiting would blow the deadline; abandon this endpoint, activate cooldown and fall back now.
                    if cd_seconds:
                        self.health_store.record_failure(
                            endpoint_id=endpoint.id,
                            latency_ms=0.0,
                            cooldown_seconds=cd_seconds,
                        )
                        pm = getattr(self, "pool_manager", None)
                        if pm:
                            pm.mark_cooldown(pool, endpoint.provider, cd_seconds)
                    excluded_endpoints.append(endpoint.id)
                    ledger.mark_fallback()
                    fallback_marked = True
                else:
                    self._sleep(backoff_s)
                    # The wait is complete for this request. New requests still
                    # observe lane cooldowns unless their own retry wait passes.
                    if endpoint.lane_key:
                        self.router.lane_store.set_available(endpoint.lane_key)

        self.events.error(
            "request_exhausted", request_id=request.request_id, pool=pool,
            attempts=ledger.total_attempts, last_reason=last_failure_reason,
        )
        raise NoHealthyRouteError(f"All fallback attempts exhausted for pool '{pool}'", pool=pool)

    def _emit_attempt(self, rec: AttemptRecord) -> None:
        data = dict(
            endpoint_id=rec.endpoint_id, provider=rec.provider, model=rec.model,
            attempt_index=rec.attempt_index, fallback_index=rec.fallback_index,
            status_code=rec.status_code, latency_ms=round(rec.latency_ms, 1), compacted=rec.compacted,
        )
        if rec.success:
            self.events.info("upstream_attempt_succeeded", request_id=rec.request_id, **data)
        else:
            self.events.warning(
                "upstream_attempt_failed", request_id=rec.request_id,
                reason=rec.failure.reason.value if rec.failure else None,
                retryable=rec.failure.retryable if rec.failure else None,
                message=(rec.failure.message or "")[:160] if rec.failure else None,
                **data,
            )
