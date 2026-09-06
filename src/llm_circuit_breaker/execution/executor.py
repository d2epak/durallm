"""Core Request Execution Engine for Semantic Failover."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_circuit_breaker.agent.context import ContextBudget, ContextManager, estimate_tokens
from llm_circuit_breaker.agent.failover_plan import FailoverPlan
from llm_circuit_breaker.agent.idempotency import (
    DEFAULT_TOOL_LEDGER,
    ToolExecutionLedger,
)
from llm_circuit_breaker.agent.tool_validation import ToolCallValidator
from llm_circuit_breaker.validation.response import ResponseValidator
from llm_circuit_breaker.breaker.circuit_breaker import CircuitBreaker
from llm_circuit_breaker.breaker.registry import (
    DEFAULT_BREAKER_REGISTRY,
    CircuitBreakerRegistry,
)
from llm_circuit_breaker.capability.profile import Endpoint
from llm_circuit_breaker.capability.registry import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityRegistry,
)
from llm_circuit_breaker.classifier import classify_failure
from llm_circuit_breaker.errors import (
    NonRecoverableFailureError,
    BreakerOpenError,
    ContextOverflowError,
    DeadlineExceededError,
    NoHealthyRouteError,
    ProbeAdmissionDeniedError,
)
from llm_circuit_breaker.execution.deadline import Deadline
from llm_circuit_breaker.execution.ledger import AttemptLedger
from llm_circuit_breaker.execution.policy import ExecutionPolicy
from llm_circuit_breaker.health.telemetry import (
    DEFAULT_HEALTH_STORE,
    HealthTelemetryStore,
)
from llm_circuit_breaker.observability.logger import DEFAULT_STRUCTURED_LOGGER, StructuredJsonLogger
from llm_circuit_breaker.models import (
    AttemptRecord,
    FailureCategory,
    FailureClassification,
    FailoverReason,
)
from llm_circuit_breaker.protocol.ir import (
    NormalizedRequest,
    NormalizedResponse,
)
from llm_circuit_breaker.providers.adapters import (
    DEFAULT_ADAPTER_REGISTRY,
    ProviderAdapterRegistry,
)
from llm_circuit_breaker.routing.decision import RoutingDecision
from llm_circuit_breaker.routing.requirements import RequirementVector
from llm_circuit_breaker.routing.router import CapabilityRouter

logger = logging.getLogger("llm_circuit_breaker.execution")


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
        sleeper: Callable[[float], None] = time.sleep,
        events: Optional[StructuredJsonLogger] = None,
    ):
        self.capability_registry = capability_registry or DEFAULT_CAPABILITY_REGISTRY
        self.breaker_registry = breaker_registry or DEFAULT_BREAKER_REGISTRY
        self.adapter_registry = adapter_registry or DEFAULT_ADAPTER_REGISTRY
        self.health_store = health_store or DEFAULT_HEALTH_STORE
        self.policy = policy or ExecutionPolicy()
        self.context_manager = context_manager or ContextManager()
        self.tool_validator = tool_validator or ToolCallValidator(strict=True)
        self.tool_ledger = tool_ledger or DEFAULT_TOOL_LEDGER
        self.response_validator = response_validator or ResponseValidator(tool_validator=self.tool_validator)
        self._sleep = sleeper
        self.events = events or DEFAULT_STRUCTURED_LOGGER
        # The router must read the same telemetry this executor writes, or scoring never sees it.
        self.router = router or CapabilityRouter(
            capability_registry=self.capability_registry,
            breaker_registry=self.breaker_registry,
            health_store=self.health_store,
        )

    def execute(
        self,
        request: NormalizedRequest,
        pool: str = "general_agent",
        strategy: Optional[str] = None,
        deadline_ms: float = 60000.0,
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

        # Build requirement vector from request (on top of any caller-supplied constraints)
        req_vector = replace(
            requirements or RequirementVector(),
            require_tools=bool(request.tools) or bool(requirements and requirements.require_tools),
            task_class="coding" if pool == "coding" else "general",
            estimated_input_tokens=estimate_tokens(request),
            expected_output_tokens=request.max_output_tokens or 0,
        )

        excluded_endpoints: List[str] = []
        last_decision: Optional[RoutingDecision] = None
        attempt_idx = 0
        last_failure_reason: Optional[str] = None
        last_endpoint: Optional[Endpoint] = None
        fallback_marked = False  # True once the retry loop has already counted the pending hop
        # Spec §15: after a size rejection, shrink the assumed window and retry the same candidate once.
        window_shrink: Dict[str, float] = {}

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
            last_decision = decision

            if not endpoint:
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

            # 2. Circuit Breaker Admission
            breaker = self.breaker_registry.get_or_create(endpoint.resource_key)
            try:
                breaker.acquire_permission()
            except (BreakerOpenError, ProbeAdmissionDeniedError) as b_err:
                logger.warning("Breaker admission denied for %s: %s", endpoint.id, b_err)
                excluded_endpoints.append(endpoint.id)
                continue

            # 3. Context Adaptation (Budget Sizing)
            profile = endpoint.profile or self.capability_registry.get_profile(endpoint.provider, endpoint.model)
            shrink = window_shrink.get(endpoint.id, 1.0)
            budget = ContextBudget(
                model_context_window=int(profile.context_window * shrink),
                desired_output_tokens=request.max_output_tokens or 4096,
                safety_margin_tokens=2048,
            )
            try:
                adapted_request, was_compacted = self.context_manager.compact(request, budget)
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

            last_endpoint = endpoint

            # 4. Prepare and Execute Request
            adapter = self.adapter_registry.get_adapter(endpoint.provider, protocol=endpoint.protocol)
            key_val = keys.get(endpoint.env_key, "") if endpoint.env_key else ""
            prepared = adapter.prepare_request(endpoint, adapted_request, api_key=key_val)

            attempt_timeout_sec = deadline.per_attempt_timeout_seconds()
            attempt_rec = AttemptRecord(
                request_id=request.request_id,
                endpoint_id=endpoint.id,
                provider=endpoint.provider,
                model=endpoint.model,
                attempt_index=attempt_idx,
                fallback_index=ledger.fallback_count,
                compacted=was_compacted,
            )

            # Execute attempt
            exec_result = adapter.execute(prepared, timeout_seconds=attempt_timeout_sec)

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

                        # Find schema
                        tool_schema = next((t.parameters for t in request.tools if t.name == tc.name), None)
                        val_report = self.tool_validator.validate_tool_call(
                            tool_name=tc.name,
                            arguments=tc.arguments,
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
                            self.tool_ledger.mark_validated(tc_id)

                    if validation_passed:
                        # Success!
                        breaker.record_success(exec_result.duration_ms)
                        self.health_store.record_success(endpoint.id, exec_result.duration_ms)
                        attempt_rec.finish(success=True, status_code=200)
                        ledger.record_attempt(attempt_rec)
                        self._emit_attempt(attempt_rec)

                        # Estimate cost
                        in_tokens = estimate_tokens(adapted_request)
                        out_tokens = estimate_tokens(norm_response.content or "")
                        cost = ((in_tokens / 1_000_000.0) * profile.input_price_per_1m) + ((out_tokens / 1_000_000.0) * profile.output_price_per_1m)
                        ledger.add_cost(cost)

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
                except Exception as norm_err:
                    logger.warning("Failed to normalize response from %s: %s", endpoint.id, norm_err)
                    classified = classify_failure(norm_err, status_code=502)
            else:
                # Classify error
                raw_msg = exec_result.body.decode("utf-8", errors="ignore")
                classified = classify_failure(raw_msg, status_code=exec_result.status_code, headers=exec_result.headers)

            # Record failure in breaker & health store
            breaker.record_failure(exec_result.duration_ms, failure_classification=classified)
            self.health_store.record_failure(
                endpoint_id=endpoint.id,
                latency_ms=exec_result.duration_ms,
                error_message=classified.message[:160],
                cooldown_seconds=classified.retry_after_seconds or (60.0 if classified.reason == FailoverReason.rate_limit else None),
            )

            attempt_rec.finish(success=False, status_code=exec_result.status_code, failure=classified)
            ledger.record_attempt(attempt_rec)
            self._emit_attempt(attempt_rec)

            last_failure_reason = classified.reason.value

            # Retry / fallback decision is driven by the classification first, budget second.
            size_rejected = classified.reason in (FailoverReason.payload_too_large, FailoverReason.context_overflow)
            if size_rejected and endpoint.id not in window_shrink and ledger.can_attempt_endpoint(endpoint.id):
                # Spec §15: compact harder and retry this candidate before falling back; no backoff needed.
                window_shrink[endpoint.id] = 0.5
                logger.info("Size rejection from %s; compacting and retrying the same candidate", endpoint.id)
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
                # Non-retryable, or retries on this endpoint exhausted: exclude and step fallback
                excluded_endpoints.append(endpoint.id)
                ledger.mark_fallback()
                fallback_marked = True
            else:
                # Retrying the same endpoint: back off (Retry-After takes precedence) within the deadline.
                backoff_s = self.policy.retry.compute_backoff_seconds(
                    ledger.attempts_on(endpoint.id), retry_after=classified.retry_after_seconds
                )
                if backoff_s >= deadline.remaining_ms() / 1000.0:
                    # Waiting would blow the deadline; abandon this endpoint and fall back now.
                    excluded_endpoints.append(endpoint.id)
                    ledger.mark_fallback()
                    fallback_marked = True
                else:
                    self._sleep(backoff_s)

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
