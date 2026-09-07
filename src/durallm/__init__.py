"""⚡ LLM Circuit Breaker

Self-Hostable Agent Resilience Gateway with Capability-Aware Routing,
Semantic Failover, and Zero-Dependency Autonomous Recovery.
"""

from __future__ import annotations

# V2 Core Exports
from durallm.agent import (
    AgentState,
    ContextBudget,
    ContextManager,
    StateSnapshot,
    ToolCallResult,
    ToolCallValidator,
    ToolValidationReport,
    extract_diagnostic_summary,
)
from durallm.breaker import (
    DEFAULT_BREAKER_REGISTRY,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitBreakerState,
    StateTransitionEvent,
)
from durallm.capability import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityRegistry,
    Endpoint,
    ModelProfile,
)
from durallm.classifier import (
    ClassifiedError,
    FailoverReason,
    FailureCategory,
    FailureClassification,
    classify_api_error,
    classify_failure,
    parse_retry_after,
)
from durallm.config import GatewayConfig
from durallm.continuation import (
    ACP_VERSION,
    Checkpoint,
    ContinuationEvent,
    ContinuationRequest,
    ContinuationStore,
    ContinuationTurn,
    InMemoryContinuationStore,
)
from durallm.discovery import (
    discover_free_models,
    is_model_free,
    supports_tool_calling,
)
from durallm.errors import (
    BreakerOpenError,
    CircuitBreakerError,
    ContextOverflowError,
    ContinuationProtocolError,
    CycleDetectedError,
    DeadlineExceededError,
    GatewayError,
    IndeterminateToolOperationError,
    NoHealthyRouteError,
    ProbeAdmissionDeniedError,
    ToolOperationProtocolError,
    UnsafeToolCallError,
)
from durallm.execution import (
    AttemptLedger,
    Deadline,
    ExecutionPolicy,
    FallbackPolicy,
    GatewayExecutor,
    RetryPolicy,
)
from durallm.health import (
    DEFAULT_HEALTH_STORE,
    EndpointHealthSnapshot,
    HealthTelemetryStore,
)
from durallm.mcp import (
    DEFAULT_MCP_PROXY,
    MCPProxy,
    MCPToolDefinition,
)
from durallm.models import AttemptRecord
from durallm.performance import (
    FastSlidingWindow,
    FastStreamRelay,
    FastTokenEstimator,
    get_accelerated_event_loop,
    is_uvloop_active,
)
from durallm.pools import (
    POOL_MANAGER,
    IsolatedPoolManager,
    RouteDefinition,
)
from durallm.protocol import (
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
from durallm.proxy import (
    CircuitBreakerGatewayHandler,
    create_proxy_app,
    start_proxy_server,
)
from durallm.pruner import (
    estimate_tokens,
    prune_anthropic_request,
    prune_openai_request,
)
from durallm.router import UniversalFailoverRouter
from durallm.routing import (
    CandidateEvaluation,
    CapabilityRouter,
    RequirementVector,
    RoutingDecision,
    RoutingScorer,
)
from durallm.routing.cache import PromptCacheTracker
from durallm.routing.keys import KeyRotationPool
from durallm.storage.cluster import ClusterPersistenceStore
from durallm.streaming import (
    MidStreamFailurePolicy,
    StreamingMetrics,
    StreamingMode,
    interruption_sse,
    synthesize_anthropic_sse,
    synthesize_openai_sse,
)
from durallm.translators import (
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
    "extract_diagnostic_summary",
    "MCPProxy",
    "DEFAULT_MCP_PROXY",
    "MCPToolDefinition",
    # Agent Continuation Protocol
    "ACP_VERSION",
    "Checkpoint",
    "ContinuationEvent",
    "ContinuationRequest",
    "ContinuationStore",
    "ContinuationTurn",
    "InMemoryContinuationStore",
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
    "interruption_sse",
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
    "ContinuationProtocolError",
    "IndeterminateToolOperationError",
    "ToolOperationProtocolError",
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
    # Frontier Additions
    "ClusterPersistenceStore",
    "MCPProxy",
    "KeyRotationPool",
    "PromptCacheTracker",
    "FastStreamRelay",
    "FastTokenEstimator",
    "FastSlidingWindow",
    "get_accelerated_event_loop",
    "is_uvloop_active",
]
