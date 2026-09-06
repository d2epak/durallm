"""⚡ LLM Circuit Breaker

Self-Hostable Agent Resilience Gateway with Capability-Aware Routing,
Semantic Failover, and Zero-Dependency Autonomous Recovery.
"""

from __future__ import annotations

# V2 Core Exports
from llm_circuit_breaker.agent import (
    AgentState,
    ContextBudget,
    ContextManager,
    StateSnapshot,
    ToolCallResult,
    ToolCallValidator,
    ToolValidationReport,
)
from llm_circuit_breaker.breaker import (
    DEFAULT_BREAKER_REGISTRY,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitBreakerState,
    StateTransitionEvent,
)
from llm_circuit_breaker.capability import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityRegistry,
    Endpoint,
    ModelProfile,
)
from llm_circuit_breaker.classifier import (
    ClassifiedError,
    FailoverReason,
    FailureCategory,
    FailureClassification,
    classify_api_error,
    classify_failure,
    parse_retry_after,
)
from llm_circuit_breaker.config import GatewayConfig
from llm_circuit_breaker.discovery import (
    discover_free_models,
    is_model_free,
    supports_tool_calling,
)
from llm_circuit_breaker.errors import (
    BreakerOpenError,
    CircuitBreakerError,
    ContextOverflowError,
    CycleDetectedError,
    DeadlineExceededError,
    GatewayError,
    NoHealthyRouteError,
    ProbeAdmissionDeniedError,
    UnsafeToolCallError,
)
from llm_circuit_breaker.execution import (
    AttemptLedger,
    Deadline,
    ExecutionPolicy,
    FallbackPolicy,
    GatewayExecutor,
    RetryPolicy,
)
from llm_circuit_breaker.health import (
    DEFAULT_HEALTH_STORE,
    EndpointHealthSnapshot,
    HealthTelemetryStore,
)
from llm_circuit_breaker.models import AttemptRecord
from llm_circuit_breaker.pools import (
    POOL_MANAGER,
    IsolatedPoolManager,
    RouteDefinition,
)
from llm_circuit_breaker.protocol import (
    NormalizedMessage,
    NormalizedRequest,
    NormalizedResponse,
    NormalizedToolCall,
    NormalizedToolDefinition,
    NormalizedToolResult,
    anthropic_request_to_ir,
    gemini_response_to_ir,
    ir_to_anthropic_request,
    ir_to_anthropic_response,
    ir_to_gemini_request,
    ir_to_openai_request,
    ir_to_openai_response,
    openai_request_to_ir,
    openai_response_to_ir,
)
from llm_circuit_breaker.proxy import (
    CircuitBreakerGatewayHandler,
    create_proxy_app,
    start_proxy_server,
)
from llm_circuit_breaker.pruner import (
    estimate_tokens,
    prune_anthropic_request,
    prune_openai_request,
)
from llm_circuit_breaker.router import UniversalFailoverRouter
from llm_circuit_breaker.routing import (
    CandidateEvaluation,
    CapabilityRouter,
    RequirementVector,
    RoutingDecision,
    RoutingScorer,
)
from llm_circuit_breaker.streaming import (
    MidStreamFailurePolicy,
    StreamingMetrics,
    StreamingMode,
    synthesize_anthropic_sse,
    synthesize_openai_sse,
)
from llm_circuit_breaker.translators import (
    anthropic_to_openai_request,
    clean_gemini_schema,
    convert_gemini_to_openai_response,
    convert_openai_to_gemini_payload,
    openai_to_anthropic_response,
    repair_json_string,
)

__version__ = "0.2.0"

__all__ = [
    # Breaker & Registry
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerState",
    "CircuitBreakerRegistry",
    "DEFAULT_BREAKER_REGISTRY",
    "StateTransitionEvent",
    # Capability & Routing
    "ModelProfile",
    "Endpoint",
    "CapabilityRegistry",
    "DEFAULT_CAPABILITY_REGISTRY",
    "CapabilityRouter",
    "RequirementVector",
    "RoutingDecision",
    "CandidateEvaluation",
    "RoutingScorer",
    # Execution & Deadlines
    "GatewayExecutor",
    "ExecutionPolicy",
    "RetryPolicy",
    "FallbackPolicy",
    "Deadline",
    "AttemptLedger",
    "AttemptRecord",
    # Agent & Tool Safety
    "AgentState",
    "StateSnapshot",
    "ToolCallValidator",
    "ToolCallResult",
    "ToolValidationReport",
    "ContextManager",
    "ContextBudget",
    # Protocol IR
    "NormalizedRequest",
    "NormalizedResponse",
    "NormalizedMessage",
    "NormalizedToolCall",
    "NormalizedToolDefinition",
    "NormalizedToolResult",
    "anthropic_request_to_ir",
    "ir_to_anthropic_request",
    "ir_to_anthropic_response",
    "openai_request_to_ir",
    "ir_to_openai_request",
    "openai_response_to_ir",
    "ir_to_openai_response",
    "ir_to_gemini_request",
    "gemini_response_to_ir",
    # Streaming & Health
    "StreamingMode",
    "MidStreamFailurePolicy",
    "StreamingMetrics",
    "synthesize_anthropic_sse",
    "synthesize_openai_sse",
    "HealthTelemetryStore",
    "EndpointHealthSnapshot",
    "DEFAULT_HEALTH_STORE",
    # Classifier & Taxonomy
    "FailureCategory",
    "FailoverReason",
    "FailureClassification",
    "classify_failure",
    "parse_retry_after",
    "classify_api_error",
    "ClassifiedError",
    # Errors
    "GatewayError",
    "CircuitBreakerError",
    "BreakerOpenError",
    "ProbeAdmissionDeniedError",
    "DeadlineExceededError",
    "NoHealthyRouteError",
    "UnsafeToolCallError",
    "ContextOverflowError",
    "CycleDetectedError",
    # Configuration
    "GatewayConfig",
    # V1 Compatibility
    "UniversalFailoverRouter",
    "POOL_MANAGER",
    "IsolatedPoolManager",
    "RouteDefinition",
    "prune_anthropic_request",
    "prune_openai_request",
    "estimate_tokens",
    "anthropic_to_openai_request",
    "openai_to_anthropic_response",
    "clean_gemini_schema",
    "convert_openai_to_gemini_payload",
    "convert_gemini_to_openai_response",
    "repair_json_string",
    "discover_free_models",
    "is_model_free",
    "supports_tool_calling",
    "CircuitBreakerGatewayHandler",
    "start_proxy_server",
    "create_proxy_app",
]
