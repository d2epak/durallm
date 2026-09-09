"""Universal Failover Router with Multi-Agent Pool Isolation and Fast Failover."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

from durallm._env import ALLOW_LOCAL_UPSTREAM_ENV
from durallm.classifier import (
    FailoverReason,
    calculate_seconds_until_utc_midnight,
    classify_api_error,
    parse_input_tpm_limit,
    parse_output_cap_from_error,
)
from durallm.errors import CircuitBreakerGatewayError
from durallm.pools import AUTO_DISCOVER_ENV, POOL_MANAGER, RouteDefinition, env_flag
from durallm.pruner import prune_openai_request
from durallm.security.defense import MAX_PAYLOAD_BYTES, enforce_payload_limit, validate_upstream_url
from durallm.translators import (
    convert_gemini_to_openai_response,
    convert_openai_to_gemini_payload,
)

logger = logging.getLogger("durallm.router")
DEFAULT_TIMEOUT = int(os.environ.get("GATEWAY_TIMEOUT", "120"))


def calculate_adaptive_timeout(provider: str, payload: Dict[str, Any], default_timeout: float = 120.0) -> float:
    """Calculate adaptive transport timeout based on provider compute tier and estimated payload tokens."""
    p = (provider or "").lower()
    base = 45.0 if p in ("nvidia", "sambanova", "cerebras") else 35.0
    messages = payload.get("messages", [])
    char_count = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
    approx_tokens = char_count // 4
    if approx_tokens > 16000:
        base += 25.0
    elif approx_tokens > 8000:
        base += 15.0
    return min(default_timeout, base)


def resolve_secret(key_name: str) -> Optional[str]:
    """Resolve an API key or secret by key name across env and pool configuration."""
    val = os.environ.get(key_name)
    if val and val.strip():
        return val.strip()
    keys = POOL_MANAGER.keys
    if key_name in keys and keys[key_name].strip():
        return keys[key_name].strip()
    from durallm.pools import load_all_env_keys
    fresh = load_all_env_keys([key_name])
    return fresh.get(key_name)


def execute_upstream_request(
    route: RouteDefinition,
    openai_payload: Dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT
) -> Tuple[int, Dict[str, str], bytes]:
    """Execute completion request against upstream API endpoint."""
    api_key = route.env_key and POOL_MANAGER.keys.get(route.env_key)

    if route.api_format == "gemini":
        gemini_payload = convert_openai_to_gemini_payload(openai_payload)
        gemini_payload["generationConfig"] = {
            "maxOutputTokens": min(openai_payload.get("max_tokens", route.max_output_tokens), route.max_output_tokens, 8192),
            "temperature": openai_payload.get("temperature", 0.7),
        }
        data = json.dumps(gemini_payload, ensure_ascii=False).encode("utf-8")
        url = f"{route.base_url.rstrip('/')}/models/{route.model}:generateContent"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["x-goog-api-key"] = api_key
    else:
        url = f"{route.base_url.rstrip('/')}/chat/completions"
        payload_copy = dict(openai_payload)
        payload_copy["model"] = route.model
        if "max_tokens" in payload_copy:
            payload_copy["max_tokens"] = min(payload_copy["max_tokens"], route.max_output_tokens, 8192)
        if "thinking" in payload_copy and route.provider not in ("anthropic", "openrouter"):
            payload_copy.pop("thinking", None)

        data = json.dumps(payload_copy, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if route.headers:
            headers.update(route.headers)

    try:
        validate_upstream_url(url, allow_localhost=env_flag(ALLOW_LOCAL_UPSTREAM_ENV))
        enforce_payload_limit(len(data))
    except CircuitBreakerGatewayError as exc:
        logger.error("Refusing upstream call for route %s: %s", route.id, exc)
        return 599, {}, f"transport_error:blocked: {exc}".encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read(MAX_PAYLOAD_BYTES + 1)
            enforce_payload_limit(len(resp_body))
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, resp_headers, resp_body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if hasattr(exc, "headers") else {}
        return exc.code, resp_headers, body
    except Exception as exc:
        return 599, {}, str(exc).encode("utf-8")


class UniversalFailoverRouter:
    """Manages multi-provider LLM failover with isolated pools, cooldowns, and self-healing."""

    def __init__(
        self,
        configured_fallbacks: Optional[List[Dict[str, Any]]] = None,
        auto_discover_free: Optional[bool] = None,
        max_discovered_free: int = 5,
        default_pool: str = "general_agent",
        allowed_providers: Optional[Set[str]] = None,
    ):
        self.default_pool = default_pool
        self.pool_manager = POOL_MANAGER

        # Resolve allowed providers from parameter or env
        env_allowed = os.getenv("LLM_ALLOWED_PROVIDERS")
        if allowed_providers is not None:
            self.allowed_providers: Optional[Set[str]] = {p.lower().strip() for p in allowed_providers}
        elif env_allowed:
            self.allowed_providers = {p.lower().strip() for p in env_allowed.split(",") if p.strip()}
        else:
            self.allowed_providers = None

        if self.allowed_providers is not None:
            self.pool_manager.allowed_providers = self.allowed_providers

        self.fallback_chain: List[Dict[str, Any]] = []
        for item in (configured_fallbacks or []):
            p = (item.get("provider") or "").lower().strip()
            if self.allowed_providers is not None and p not in self.allowed_providers:
                continue
            self.fallback_chain.append(item)

        self.provider_cooldowns: Dict[str, float] = {}
        self.deprecated_models: Set[str] = set()
        self.auth_failed_providers: Set[str] = set()
        self.fallback_index: int = -1

        # Also register configured fallbacks into pool manager if provided
        if self.fallback_chain:
            converted_routes: List[RouteDefinition] = []
            for item in self.fallback_chain:
                route = RouteDefinition(
                    id=item.get("id") or f"{item.get('provider')}-{item.get('model')}",
                    provider=item.get("provider", "custom"),
                    model=item.get("model", "default"),
                    pool=item.get("pool", default_pool),
                    base_url=item.get("base_url", "https://api.openai.com/v1"),
                    api_format=item.get("api_format", "openai"),
                    env_key=item.get("key_env") or item.get("env_key"),
                    context_length=item.get("context_length", 65536),
                    headers=item.get("headers", {}),
                )
                converted_routes.append(route)

            if default_pool == "coding":
                self.pool_manager.coding_routes = converted_routes + self.pool_manager.coding_routes
            else:
                self.pool_manager.agent_routes = converted_routes + self.pool_manager.agent_routes

        # Network discovery is opt-in: explicit argument, else LLM_BREAKER_AUTO_DISCOVER=1.
        if auto_discover_free is None:
            auto_discover_free = env_flag(AUTO_DISCOVER_ENV)
        if auto_discover_free:
            from durallm.discovery import register_discovered_models_to_pools
            register_discovered_models_to_pools(limit_per_pool=max_discovered_free)

    @property
    def active_provider(self) -> Dict[str, Any]:
        """Return currently active provider definition."""
        if self.fallback_chain:
            idx = max(0, self.fallback_index) % len(self.fallback_chain)
            return self.fallback_chain[idx]
        route = self.pool_manager.select_route(self.default_pool)
        if not route:
            return {}
        return {
            "provider": route.provider,
            "model": route.model,
            "base_url": route.base_url,
            "key_env": route.env_key,
            "context_length": route.context_length,
        }

    def mark_cooldown(self, provider: str, seconds: float = 60.0, pool: Optional[str] = None, route_id: Optional[str] = None) -> None:
        """Place a route on cooldown to avoid turn-thrashing.

        If route_id is provided, the cooldown targets that specific route.
        Otherwise falls back to using provider as a route_id key.
        """
        self.provider_cooldowns[provider.lower()] = time.monotonic() + seconds
        target_pool = pool or self.default_pool
        cd_key = route_id or provider
        self.pool_manager.mark_cooldown(target_pool, cd_key, seconds)
        logger.info("Route/provider %s placed on cooldown for %.1fs", cd_key, seconds)

    def mark_deprecated(self, model: str, pool: Optional[str] = None) -> None:
        """Permanently skip model for this session."""
        self.deprecated_models.add(model)
        target_pool = pool or self.default_pool
        self.pool_manager.mark_deprecated(target_pool, model)
        logger.warning("Blacklisted deprecated model: %s", model)

    def record_success(self, provider: str, model: str) -> None:
        """Record a successful execution, clearing cooldowns for this provider."""
        p = provider.lower()
        if p in self.provider_cooldowns:
            del self.provider_cooldowns[p]
        if hasattr(self, "auth_failed_providers") and p in self.auth_failed_providers:
            self.auth_failed_providers.remove(p)
        self.pool_manager.clear_cooldown(p)
        logger.info("Recorded success for %s/%s; cleared cooldowns.", provider, model)

    def mark_auth_failed(self, provider: str) -> None:
        """Mark provider as permanently failed authentication for this session."""
        p = provider.lower()
        if not hasattr(self, "auth_failed_providers") or self.auth_failed_providers is None:
            self.auth_failed_providers = set()
        self.auth_failed_providers.add(p)
        logger.warning("Provider %s marked authentication failed", provider)

    def get_next_available_route(self, reason: Optional[FailoverReason] = None, pool: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Select next healthy route in fallback chain or pool."""
        if self.fallback_chain:
            max_attempts = len(self.fallback_chain) * 2
            attempts = 0
            while attempts < max_attempts:
                self.fallback_index = (self.fallback_index + 1) % len(self.fallback_chain)
                candidate = self.fallback_chain[self.fallback_index]
                attempts += 1

                p = candidate.get("provider", "").lower()
                m = candidate.get("model", "")

                if self.allowed_providers is not None and p not in self.allowed_providers:
                    continue
                if hasattr(self, "auth_failed_providers") and p in self.auth_failed_providers:
                    continue
                if m in self.deprecated_models:
                    continue
                cooldown = self.provider_cooldowns.get(p, 0)
                if time.monotonic() < cooldown:
                    continue

                return candidate

        target_pool = pool or self.default_pool
        route = self.pool_manager.select_route(target_pool)
        if not route:
            return None
        return {
            "provider": route.provider,
            "model": route.model,
            "base_url": route.base_url,
            "key_env": route.env_key,
            "context_length": route.context_length,
        }

    def dispatch(
        self,
        pool: str,
        openai_payload: Dict[str, Any],
        requested_model: str = "default",
        max_attempts: int = 8
    ) -> Tuple[int, Dict[str, Any], Optional[RouteDefinition]]:
        """
        Execute request with automatic failover across isolated pool candidates.
        Returns (status_code, parsed_openai_response, successful_route).
        """
        attempts = 0
        while attempts < max_attempts:
            route = self.pool_manager.select_route(pool, requested_model)
            if not route:
                logger.error("Pool '%s' has exhausted all available healthy routes.", pool)
                break

            attempts += 1
            logger.info(
                "[%s] Upstream attempt %d/%d -> %s (%s)",
                pool.upper(), attempts, max_attempts, route.provider, route.model
            )

            effective_context = route.context_length
            pruned_payload = prune_openai_request(openai_payload, effective_context)
            adaptive_timeout = calculate_adaptive_timeout(route.provider, pruned_payload, default_timeout=float(DEFAULT_TIMEOUT))
            status, headers, body = execute_upstream_request(route, pruned_payload, timeout=int(adaptive_timeout))

            if status == 200:
                try:
                    raw_json = json.loads(body.decode("utf-8"))
                    if route.api_format == "gemini":
                        parsed = convert_gemini_to_openai_response(raw_json, route.model)
                    else:
                        parsed = raw_json
                    return 200, parsed, route
                except Exception as e:
                    logger.warning("Provider %s returned invalid JSON: %s", route.provider, e)
                    status = 502

            classified = classify_api_error(body.decode("utf-8", errors="ignore"), status_code=status, pool=pool, route_id=route.id, provider=route.provider)
            logger.warning(
                "[%s] Upstream error from %s (%d): %s (classified as: %s)",
                pool.upper(), route.provider, status, classified.message[:160], classified.reason
            )

            # Auto-clamp output cap if provider reported one
            if classified.reason == FailoverReason.output_cap_exceeded:
                raw_body = body.decode("utf-8", errors="ignore")
                cap = parse_output_cap_from_error(raw_body)
                current_max = openai_payload.get("max_tokens")
                if cap and cap > 0 and (current_max is None or current_max > cap):
                    clamped_tokens = max(1, cap - 64) if cap > 64 else cap
                    logger.info(
                        "Auto-clamping output tokens to %d for %s (reported cap: %d) and retrying...",
                        clamped_tokens, route.provider, cap
                    )
                    openai_payload["max_tokens"] = clamped_tokens
                    continue

            # In-flight compaction if input TPM or size rejection reported
            if classified.reason in (FailoverReason.payload_too_large, FailoverReason.context_overflow):
                raw_body = body.decode("utf-8", errors="ignore")
                in_limit = parse_input_tpm_limit(raw_body)
                if in_limit and in_limit > 0:
                    target_tokens = max(1000, int(in_limit * 0.85))
                    logger.info(
                        "Input TPM / token limit %d detected from %s; pruning payload to %d tokens and retrying immediately...",
                        in_limit, route.provider, target_tokens
                    )
                    openai_payload = prune_openai_request(openai_payload, target_tokens)
                    continue

            if classified.reason == FailoverReason.billing:
                msg_lower = (classified.message or "").lower()
                if "free-models-per-day" in msg_lower or "free model requests" in msg_lower or route.provider.lower() == "openrouter":
                    reset_sec = calculate_seconds_until_utc_midnight()
                    self.pool_manager.mark_provider_quota_exhausted(pool, route.provider, reset_sec)
                else:
                    self.pool_manager.mark_quota_exhausted(pool, route.id, 86400)
                continue

            if classified.reason in (FailoverReason.rate_limit, FailoverReason.upstream_rate_limit):
                retry_after = headers.get("retry-after")
                try:
                    cd = min(120, max(30, int(float(retry_after or "60"))))
                except Exception:
                    cd = 60
                self.pool_manager.mark_cooldown(pool, route.id, cd)
                continue

            if classified.reason == FailoverReason.model_not_found or status == 404:
                self.pool_manager.mark_deprecated(pool, route.model)
                continue

            if classified.reason in (FailoverReason.overloaded, FailoverReason.server_error, FailoverReason.timeout, FailoverReason.connection_refused):
                self.pool_manager.mark_cooldown(pool, route.id, 30.0)  # Tier 1 transient probe cooldown
                continue

        return 503, {"error": {"message": f"All providers in pool '{pool}' are temporarily unavailable."}}, None
