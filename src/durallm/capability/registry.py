"""Provider & Model Capability Registry."""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

from durallm.capability.profile import Endpoint, ModelProfile

# (provider, alias) -> canonical model. Resolution is exact match, then this map; never substrings.
_BUILTIN_ALIASES = (
    ("groq", "llama-3.3-70b", "llama-3.3-70b-versatile"),
    ("cerebras", "llama-3.3-70b", "llama3.3-70b"),
    ("cerebras", "llama-3.1-8b", "llama3.1-8b"),
)


class CapabilityRegistry:
    """Thread-safe registry for model capability profiles and endpoints."""

    def __init__(self):
        self._lock = threading.RLock()
        self._profiles: Dict[str, ModelProfile] = {}
        self._aliases: Dict[str, str] = {}
        self._endpoints: Dict[str, Endpoint] = {}
        self._seed_builtin_profiles()
        for provider, alias, canonical in _BUILTIN_ALIASES:
            self.register_alias(provider, alias, canonical)

    def _make_key(self, provider: str, model: str) -> str:
        return f"{provider.lower()}:{model.lower()}"

    def register_alias(self, provider: str, alias: str, canonical_model: str) -> None:
        """Map an alternate model name to a registered profile of the same provider."""
        with self._lock:
            self._aliases[self._make_key(provider, alias)] = self._make_key(provider, canonical_model)

    def register_profile(self, profile: ModelProfile) -> None:
        """Register or update a model profile."""
        with self._lock:
            key = self._make_key(profile.provider, profile.model)
            self._profiles[key] = profile

    def get_profile(self, provider: str, model: str) -> ModelProfile:
        """
        Resolve a profile by exact key, then by explicit alias. Unknown models get a
        pessimistic profile whose tool/parallel/structured-output capabilities are
        undeclared (None), so requirements for them exclude the candidate.
        """
        with self._lock:
            key = self._make_key(provider, model)
            if key in self._profiles:
                return self._profiles[key]

            target = self._aliases.get(key)
            if target in self._profiles:
                return self._profiles[target]

            return ModelProfile(
                provider=provider,
                model=model,
                protocol="openai",
                context_window=32768,
                max_output_tokens=4096,
                supports_tools=None,
                supports_parallel_tools=None,
                supports_structured_output=None,
                supports_streaming=True,
            )

    def register_endpoint(self, endpoint: Endpoint) -> None:
        """Register an endpoint and associate its profile."""
        with self._lock:
            if not endpoint.profile:
                endpoint.profile = self.get_profile(endpoint.provider, endpoint.model)
            self._endpoints[endpoint.id] = endpoint

    def get_endpoint(self, endpoint_id: str) -> Optional[Endpoint]:
        with self._lock:
            return self._endpoints.get(endpoint_id)

    def all_endpoints(self) -> List[Endpoint]:
        with self._lock:
            return list(self._endpoints.values())

    def endpoints_for_pool(self, pool: str) -> List[Endpoint]:
        with self._lock:
            return [e for e in self._endpoints.values() if e.pool == pool]

    def _seed_builtin_profiles(self) -> None:
        """Seed known profiles for core providers."""
        profiles = [
            # Cerebras
            ModelProfile("cerebras", "llama3.3-70b", protocol="openai", context_window=65536, max_output_tokens=8192, supports_tools=True, is_free=True),
            ModelProfile("cerebras", "llama-3.3-70b", protocol="openai", context_window=65536, max_output_tokens=8192, supports_tools=True, is_free=True),
            ModelProfile("cerebras", "llama3.1-8b", protocol="openai", context_window=65536, max_output_tokens=4096, supports_tools=True, is_free=True),
            # SambaNova
            ModelProfile("sambanova", "Qwen2.5-Coder-32B-Instruct", protocol="openai", context_window=65536, max_output_tokens=4096, supports_tools=True, is_free=True),
            ModelProfile("sambanova", "Meta-Llama-3.3-70B-Instruct", protocol="openai", context_window=65536, max_output_tokens=4096, supports_tools=True, is_free=True),
            # Groq
            ModelProfile("groq", "llama-3.3-70b-versatile", protocol="openai", context_window=131072, max_output_tokens=8192, supports_tools=True, is_free=True),
            ModelProfile("groq", "qwen/qwen3.6-27b", protocol="openai", context_window=7000, max_output_tokens=950, supports_tools=True, is_free=True),
            ModelProfile("groq", "openai/gpt-oss-120b", protocol="openai", context_window=7500, max_output_tokens=950, supports_tools=True, is_free=True),
            ModelProfile("groq", "openai/gpt-oss-20b", protocol="openai", context_window=7500, max_output_tokens=950, supports_tools=True, is_free=True),
            ModelProfile("groq", "llama-3.1-8b-instant", protocol="openai", context_window=131072, max_output_tokens=4096, supports_tools=True, is_free=True),
            # OpenRouter Free
            ModelProfile("openrouter", "google/gemma-4-31b-it:free", protocol="openai", context_window=262144, max_output_tokens=8192, supports_tools=True, is_free=True),
            ModelProfile("openrouter", "cohere/north-mini-code:free", protocol="openai", context_window=256000, max_output_tokens=8192, supports_tools=True, is_free=True),
            ModelProfile("openrouter", "nvidia/nemotron-3-super-120b-a12b:free", protocol="openai", context_window=262144, max_output_tokens=8192, supports_tools=True, is_free=True),
            ModelProfile("openrouter", "openrouter/free", protocol="openai", context_window=200000, max_output_tokens=8192, supports_tools=True, is_free=True),
            ModelProfile("openrouter", "qwen/qwen-2.5-coder-32b-instruct:free", protocol="openai", context_window=256000, max_output_tokens=8192, supports_tools=True, is_free=True),
            ModelProfile("openrouter", "mistralai/devstral-2512:free", protocol="openai", context_window=262144, max_output_tokens=8192, supports_tools=True, is_free=True),
            ModelProfile("openrouter", "meta-llama/llama-3.3-70b-instruct:free", protocol="openai", context_window=65536, max_output_tokens=4096, supports_tools=True, is_free=True),
            # Mistral
            ModelProfile("mistral", "codestral-latest", protocol="openai", context_window=256000, max_output_tokens=8192, supports_tools=True),
            ModelProfile("mistral", "mistral-small-latest", protocol="openai", context_window=128000, max_output_tokens=4096, supports_tools=True),
            # NVIDIA NIM
            ModelProfile("nvidia", "nvidia/nemotron-3-ultra-550b-a55b", protocol="openai", context_window=131072, max_output_tokens=8192, supports_tools=True, is_free=True),
            ModelProfile("nvidia", "google/gemma-4-31b-it", protocol="openai", context_window=131072, max_output_tokens=8192, supports_tools=True, is_free=True),
            ModelProfile("nvidia", "meta/llama-3.3-70b-instruct", protocol="openai", context_window=131072, max_output_tokens=4096, supports_tools=True, is_free=True),
            ModelProfile("nvidia", "meta/llama-3.2-11b-vision-instruct", protocol="openai", context_window=131072, max_output_tokens=4096, supports_tools=True, supports_vision=True, is_free=True),
            ModelProfile("nvidia", "mistralai/codestral-22b-instruct-v0.1", protocol="openai", context_window=32768, max_output_tokens=4096, supports_tools=True, is_free=True),
            # Google Gemini
            ModelProfile("gemini", "gemini-2.5-flash", protocol="gemini", context_window=1048576, max_output_tokens=8192, supports_tools=True, supports_vision=True, is_free=True),
            ModelProfile("gemini", "gemini-1.5-pro", protocol="gemini", context_window=2097152, max_output_tokens=8192, supports_tools=True, supports_vision=True),
            # Anthropic
            ModelProfile("anthropic", "claude-3-7-sonnet-20250219", protocol="anthropic", context_window=200000, max_output_tokens=8192, supports_tools=True, supports_reasoning=True, supports_vision=True),
            ModelProfile("anthropic", "claude-3-5-haiku-20241022", protocol="anthropic", context_window=200000, max_output_tokens=8192, supports_tools=True),
            # OpenAI
            ModelProfile("openai", "gpt-4o", protocol="openai", context_window=128000, max_output_tokens=4096, supports_tools=True, supports_vision=True),
            ModelProfile("openai", "gpt-4o-mini", protocol="openai", context_window=128000, max_output_tokens=4096, supports_tools=True, supports_vision=True),
        ]
        for p in profiles:
            self.register_profile(p)


DEFAULT_CAPABILITY_REGISTRY = CapabilityRegistry()
