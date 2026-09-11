"""Multi-Agent Isolated Routing Pools & Cooldown Management.

Provides separate, isolated provider fallback pools:
- Pool 'coding': Specialised for Claude Code (1M context, code generation, strict JSON tool calls).
- Pool 'general_agent': Specialised for Hermes Agent and OpenClaw (fast inference, reasoning, multi-turn chat).

Independent cooldown and quota tracking ensures that token exhaustion or 429s
in one pool never block or degrade the other.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from durallm._env import env_flag  # noqa: F401  (re-exported for router)

logger = logging.getLogger("durallm.pools")

SCAN_DOTFILES_ENV = "DURALLM_SCAN_DOTFILES"
AUTO_DISCOVER_ENV = "DURALLM_AUTO_DISCOVER"


def load_all_env_keys(target_keys: Optional[List[str]] = None, scan_dotfiles: Optional[bool] = None) -> Dict[str, str]:
    """Read API keys from the process environment.

    Shell dotfiles (~/.zshrc, ~/.claude/.env, ~/.hermes/.env, ...) are only
    read when ``scan_dotfiles`` is True or ``DURALLM_SCAN_DOTFILES`` is set.
    """
    if scan_dotfiles is None:
        scan_dotfiles = env_flag(SCAN_DOTFILES_ENV)
    keys: Dict[str, str] = {}
    if target_keys is None:
        target_keys = [
            "NVIDIA_API_KEY",
            "GROQ_API_KEY",
            "OPENROUTER_API_KEY",
            "SAMBANOVA_API_KEY",
            "CEREBRAS_API_KEY",
            "GEMINI_API_KEY",
        ]
    for k in target_keys:
        if os.getenv(k):
            keys[k] = os.getenv(k, "").strip()

    if not scan_dotfiles:
        return keys

    for candidate in [
        Path.home() / ".claude" / ".env",
        Path.home() / ".hermes" / ".env",
        Path.home() / ".hermes" / "profiles" / "cos" / ".env",
        Path.home() / ".zshrc",
    ]:
        if candidate.exists():
            try:
                with open(candidate, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("export "):
                            line = line[7:].strip()
                        if "=" in line and not line.startswith("#"):
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("\"'")
                            if k in target_keys and k not in keys and v:
                                keys[k] = v
            except Exception as e:
                logger.debug("Could not read %s: %s", candidate, e)

    return keys


@dataclass
class RouteDefinition:
    id: str
    provider: str
    model: str
    pool: str  # 'coding' or 'general_agent'
    base_url: str
    api_format: str  # 'openai'
    env_key: Optional[str]
    env_keys: List[str] = field(default_factory=list)
    context_length: int = 65536
    max_output_tokens: int = 4096
    headers: Dict[str, str] = field(default_factory=dict)
    is_discovered: bool = False


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Default Coding Pool (Claude Code, Cursor, Aider) - 5-Tier Cloud-Only Routing Topology
DEFAULT_CODING_ROUTES: List[RouteDefinition] = [
    # Tier 1 - Groq Ultra-Fast Coding Nodes
    RouteDefinition(
        id="groq-qwen36-coding",
        provider="groq",
        model="qwen/qwen3.6-27b",
        pool="coding",
        base_url="https://api.groq.com/openai/v1",
        api_format="openai",
        env_key="GROQ_API_KEY",
        context_length=7000,
        max_output_tokens=950,
        headers={"User-Agent": _BROWSER_UA},
    ),
    RouteDefinition(
        id="groq-gpt-oss-coding",
        provider="groq",
        model="openai/gpt-oss-120b",
        pool="coding",
        base_url="https://api.groq.com/openai/v1",
        api_format="openai",
        env_key="GROQ_API_KEY",
        context_length=7500,
        max_output_tokens=950,
        headers={"User-Agent": _BROWSER_UA},
    ),
    RouteDefinition(
        id="groq-gpt-oss-20b-coding",
        provider="groq",
        model="openai/gpt-oss-20b",
        pool="coding",
        base_url="https://api.groq.com/openai/v1",
        api_format="openai",
        env_key="GROQ_API_KEY",
        context_length=7500,
        max_output_tokens=950,
        headers={"User-Agent": _BROWSER_UA},
    ),
    RouteDefinition(
        id="groq-llama33-coding",
        provider="groq",
        model="llama-3.3-70b-versatile",
        pool="coding",
        base_url="https://api.groq.com/openai/v1",
        api_format="openai",
        env_key="GROQ_API_KEY",
        context_length=131072,
        max_output_tokens=8192,
        headers={"User-Agent": _BROWSER_UA},
    ),
    # Tier 2 - SambaNova High-Speed Inference
    RouteDefinition(
        id="sambanova-qwen25-coding",
        provider="sambanova",
        model="Qwen2.5-Coder-32B-Instruct",
        pool="coding",
        base_url="https://api.sambanova.ai/v1",
        api_format="openai",
        env_key="SAMBANOVA_API_KEY",
        context_length=65536,
        max_output_tokens=4096,
        headers={"User-Agent": _BROWSER_UA},
    ),
    RouteDefinition(
        id="sambanova-llama33-coding",
        provider="sambanova",
        model="Meta-Llama-3.3-70B-Instruct",
        pool="coding",
        base_url="https://api.sambanova.ai/v1",
        api_format="openai",
        env_key="SAMBANOVA_API_KEY",
        context_length=65536,
        max_output_tokens=4096,
        headers={"User-Agent": _BROWSER_UA},
    ),
    # Tier 3 - Cerebras Wafer-Scale Cloud
    RouteDefinition(
        id="cerebras-llama33-coding",
        provider="cerebras",
        model="llama-3.3-70b",
        pool="coding",
        base_url="https://api.cerebras.ai/v1",
        api_format="openai",
        env_key="CEREBRAS_API_KEY",
        context_length=65536,
        max_output_tokens=4096,
        headers={"User-Agent": _BROWSER_UA},
    ),
    # Tier 4 - NVIDIA NIM Enterprise Cloud
    RouteDefinition(
        id="nvidia-llama33-coding",
        provider="nvidia",
        model="meta/llama-3.3-70b-instruct",
        pool="coding",
        base_url="https://integrate.api.nvidia.com/v1",
        api_format="openai",
        env_key="NVIDIA_API_KEY",
        context_length=131072,
        max_output_tokens=4096,
        headers={"User-Agent": _BROWSER_UA},
    ),
    RouteDefinition(
        id="nvidia-gemma4-coding",
        provider="nvidia",
        model="google/gemma-4-31b-it",
        pool="coding",
        base_url="https://integrate.api.nvidia.com/v1",
        api_format="openai",
        env_key="NVIDIA_API_KEY",
        context_length=131072,
        max_output_tokens=8192,
        headers={"User-Agent": _BROWSER_UA},
    ),
    RouteDefinition(
        id="nvidia-nemotron-coding",
        provider="nvidia",
        model="nvidia/nemotron-3-ultra-550b-a55b",
        pool="coding",
        base_url="https://integrate.api.nvidia.com/v1",
        api_format="openai",
        env_key="NVIDIA_API_KEY",
        context_length=131072,
        max_output_tokens=8192,
        headers={"User-Agent": _BROWSER_UA},
    ),
    # Tier 5 - OpenRouter Free Community Fallback
    RouteDefinition(
        id="openrouter-qwen25-coding",
        provider="openrouter",
        model="qwen/qwen-2.5-coder-32b-instruct:free",
        pool="coding",
        base_url="https://openrouter.ai/api/v1",
        api_format="openai",
        env_key="OPENROUTER_API_KEY",
        context_length=32768,
        max_output_tokens=4096,
        headers={"User-Agent": _BROWSER_UA},
    ),
    RouteDefinition(
        id="openrouter-gemma4-coding",
        provider="openrouter",
        model="google/gemma-4-31b-it:free",
        pool="coding",
        base_url="https://openrouter.ai/api/v1",
        api_format="openai",
        env_key="OPENROUTER_API_KEY",
        context_length=262144,
        max_output_tokens=8192,
        headers={"User-Agent": _BROWSER_UA},
    ),
    RouteDefinition(
        id="openrouter-north-mini-coding",
        provider="openrouter",
        model="cohere/north-mini-code:free",
        pool="coding",
        base_url="https://openrouter.ai/api/v1",
        api_format="openai",
        env_key="OPENROUTER_API_KEY",
        context_length=256000,
        max_output_tokens=8192,
        headers={"User-Agent": _BROWSER_UA},
    ),
    RouteDefinition(
        id="openrouter-nemotron-coding",
        provider="openrouter",
        model="nvidia/nemotron-3-super-120b-a12b:free",
        pool="coding",
        base_url="https://openrouter.ai/api/v1",
        api_format="openai",
        env_key="OPENROUTER_API_KEY",
        context_length=262144,
        max_output_tokens=8192,
        headers={"User-Agent": _BROWSER_UA},
    ),
    RouteDefinition(
        id="openrouter-free-coding",
        provider="openrouter",
        model="openrouter/free",
        pool="coding",
        base_url="https://openrouter.ai/api/v1",
        api_format="openai",
        env_key="OPENROUTER_API_KEY",
        context_length=200000,
        max_output_tokens=8192,
        headers={"User-Agent": _BROWSER_UA},
    ),
]

# Default General Agent Pool (Hermes Agent, OpenClaw) - Core Providers across 5 Cloud Tiers
DEFAULT_AGENT_ROUTES: List[RouteDefinition] = [
    # 1. NVIDIA NIM Llama 3.2 Vision (Fast inference, robust multi-turn context)
    RouteDefinition(
        id="nvidia-llama32-agent",
        provider="nvidia",
        model="meta/llama-3.2-11b-vision-instruct",
        pool="general_agent",
        base_url="https://integrate.api.nvidia.com/v1",
        api_format="openai",
        env_key="NVIDIA_API_KEY",
        context_length=131072,
        max_output_tokens=4096,
        headers={"User-Agent": _BROWSER_UA},
    ),
    # 2. Groq Llama 3.1 8B Instant (128k context, ultra-fast agent responses)
    RouteDefinition(
        id="groq-llama31-agent",
        provider="groq",
        model="llama-3.1-8b-instant",
        pool="general_agent",
        base_url="https://api.groq.com/openai/v1",
        api_format="openai",
        env_key="GROQ_API_KEY",
        context_length=131072,
        max_output_tokens=4096,
        headers={"User-Agent": _BROWSER_UA},
    ),
    # 3. Cerebras Llama 3.1 8B (Fast wafer-scale agent responses)
    RouteDefinition(
        id="cerebras-llama31-agent",
        provider="cerebras",
        model="llama3.1-8b",
        pool="general_agent",
        base_url="https://api.cerebras.ai/v1",
        api_format="openai",
        env_key="CEREBRAS_API_KEY",
        context_length=65536,
        max_output_tokens=4096,
        headers={"User-Agent": _BROWSER_UA},
    ),
    # 4. SambaNova Meta Llama 3.3 70B Instruct (High-speed large model inference)
    RouteDefinition(
        id="sambanova-llama33-agent",
        provider="sambanova",
        model="Meta-Llama-3.3-70B-Instruct",
        pool="general_agent",
        base_url="https://api.sambanova.ai/v1",
        api_format="openai",
        env_key="SAMBANOVA_API_KEY",
        context_length=65536,
        max_output_tokens=4096,
        headers={"User-Agent": _BROWSER_UA},
    ),
    # 5. OpenRouter Nemotron Free (Zero-credit resilient agent route)
    RouteDefinition(
        id="openrouter-nemotron-agent",
        provider="openrouter",
        model="nvidia/nemotron-3-ultra-550b-a55b:free",
        pool="general_agent",
        base_url="https://openrouter.ai/api/v1",
        api_format="openai",
        env_key="OPENROUTER_API_KEY",
        context_length=131072,
        max_output_tokens=4096,
        headers={"User-Agent": _BROWSER_UA},
    ),
]


class IsolatedPoolManager:
    """Thread-safe manager for dual-pool routing and independent cooldowns."""

    def __init__(self, allowed_providers: Optional[Set[str]] = None):
        self._lock = threading.RLock()
        self.allowed_providers: Optional[Set[str]] = {p.lower() for p in allowed_providers} if allowed_providers else None
        self.keys = load_all_env_keys()

        self.coding_routes: List[RouteDefinition] = list(DEFAULT_CODING_ROUTES)
        self.agent_routes: List[RouteDefinition] = list(DEFAULT_AGENT_ROUTES)

        self.coding_index = 0
        self.agent_index = 0

        # Cooldowns map: (pool, route_id) -> expiration_monotonic
        # Keyed per-route so a 429 on one model does not block sibling models
        # from the same provider (e.g. different OpenRouter free models).
        self.cooldowns: Dict[tuple[str, str], float] = {}

        # Deprecated / blacklisted models per pool: set of (pool, model_id)
        self.deprecated: Set[tuple[str, str]] = set()

        # Quota exhausted models until reset: map (pool, route_id) -> reset_epoch
        self.exhausted_quotas: Dict[tuple[str, str], float] = {}

        # Account-wide daily provider quota lockouts (e.g. OpenRouter free-models-per-day): provider -> reset_epoch
        self.account_lockouts: Dict[str, float] = {}

        # Sticky primary routing: prefer last successful route per pool if it
        # succeeded recently (within STICKY_AFFINITY_SECONDS).  Falls back to
        # round-robin when the affinity window expires or the route is in cooldown.
        self.last_success: Dict[str, str] = {}       # pool -> route_id
        self.last_success_at: Dict[str, float] = {}  # pool -> monotonic timestamp
        self.STICKY_AFFINITY_SECONDS: float = 120.0

    def refresh_keys(self) -> None:
        with self._lock:
            self.keys = load_all_env_keys()

    def mark_cooldown(self, pool: str, route_id: str, seconds: float = 60.0) -> None:
        """Place a specific route on cooldown for a SPECIFIC pool only.

        Keyed by route_id (not provider) so sibling routes from the same
        provider remain available when only one model is rate-limited.
        """
        with self._lock:
            key = (pool.lower(), route_id.lower())
            self.cooldowns[key] = time.monotonic() + seconds
            logger.info("[🛡️ COOLDOWN] Pool '%s' placed route '%s' on cooldown for %.1fs", pool, route_id, seconds)

    def mark_quota_exhausted(self, pool: str, route_id: str, seconds: float = 86400.0) -> None:
        """Mark a route as having exhausted daily/monthly quota."""
        with self._lock:
            key = (pool.lower(), route_id.lower())
            self.exhausted_quotas[key] = time.time() + seconds
            logger.warning("[🛑 QUOTA EXHAUSTED] Pool '%s' route '%s' locked out for %.1fh", pool, route_id, seconds / 3600)

    def mark_provider_quota_exhausted(self, pool: str, provider: str, seconds: float = 86400.0) -> None:
        """Mark all routes for a given provider in a pool and the entire provider account as having exhausted quota."""
        with self._lock:
            p = provider.lower()
            self.account_lockouts[p] = time.time() + seconds
            routes = self.coding_routes if pool == "coding" else self.agent_routes
            for r in routes:
                if r.provider.lower() == p:
                    key = (pool.lower(), r.id.lower())
                    self.exhausted_quotas[key] = time.time() + seconds
            logger.warning("[🛑 PROVIDER QUOTA EXHAUSTED] Pool '%s' provider '%s' (all routes) locked out for %.1fh", pool, provider, seconds / 3600)

    def clear_cooldown(self, route_id: str, pool: Optional[str] = None) -> None:
        """Clear active cooldown for a route (or all routes matching the id across pools)."""
        with self._lock:
            rid = route_id.lower()
            keys_to_del = [k for k in self.cooldowns if k[1] == rid and (pool is None or k[0] == pool.lower())]
            for k in keys_to_del:
                del self.cooldowns[k]

    def is_route_in_cooldown(self, pool: str, route_id: str) -> bool:
        """Check whether a specific route is currently under short-term cooldown."""
        with self._lock:
            key = (pool.lower(), route_id.lower())
            return time.monotonic() < self.cooldowns.get(key, 0.0)

    def is_provider_in_cooldown(self, pool: str, provider: str) -> bool:
        """Check whether ANY route from this provider is currently under cooldown.

        Backward-compatible convenience method. Iterates per-route cooldown entries.
        """
        with self._lock:
            now = time.monotonic()
            routes = self.coding_routes if pool == "coding" else self.agent_routes
            for r in routes:
                if r.provider.lower() == provider.lower():
                    if now < self.cooldowns.get((pool.lower(), r.id.lower()), 0.0):
                        return True
            return False

    def is_route_quota_exhausted(self, pool: str, route_id: str, provider: Optional[str] = None) -> bool:
        """Check whether a route or its provider account is currently locked out by daily quota."""
        with self._lock:
            now = time.time()
            if provider and now < self.account_lockouts.get(provider.lower(), 0.0):
                return True
            key = (pool.lower(), route_id.lower())
            return now < self.exhausted_quotas.get(key, 0.0)

    def is_provider_quota_exhausted(self, provider: str) -> bool:
        """Check whether an entire provider account is under daily quota lockout."""
        with self._lock:
            return time.time() < self.account_lockouts.get(provider.lower(), 0.0)

    def auto_expire_cooldowns(self, pool: Optional[str] = None) -> None:
        """Purge expired short-term cooldowns, long-term route quotas, and provider account lockouts."""
        with self._lock:
            now_mono = time.monotonic()
            now_epoch = time.time()

            # 1. Purge expired short-term cloud provider cooldowns (e.g. 30s probe, 60s TPM)
            expired_cds = [
                k for k, exp in self.cooldowns.items()
                if (pool is None or k[0] == pool.lower()) and now_mono >= exp
            ]
            for k in expired_cds:
                del self.cooldowns[k]
                logger.info("[🔄 RESTORED] Pool '%s' cloud provider '%s' cooldown expired. Route reactivated.", k[0], k[1])

            # 2. Purge expired daily route quota lockouts (24h)
            expired_quotas = [
                k for k, exp in self.exhausted_quotas.items()
                if (pool is None or k[0] == pool.lower()) and now_epoch >= exp
            ]
            for k in expired_quotas:
                del self.exhausted_quotas[k]
                logger.info("[🔄 QUOTA RESET] Pool '%s' route '%s' daily lockout expired. Route reactivated.", k[0], k[1])

            # 3. Purge expired provider account lockouts
            expired_accounts = [
                p for p, exp in self.account_lockouts.items()
                if now_epoch >= exp
            ]
            for p in expired_accounts:
                del self.account_lockouts[p]
                logger.info("[🔄 RESTORED] Provider account '%s' daily quota lockout expired. Routes reactivated.", p)

    def mark_deprecated(self, pool: str, model: str) -> None:
        """Permanently skip model in this pool for current process lifecycle."""
        with self._lock:
            self.deprecated.add((pool.lower(), model.lower()))
            logger.warning("[⚠️ DEPRECATED] Blacklisted model '%s' in pool '%s'", model, pool)

    def get_candidate_routes(self, pool: str) -> List[RouteDefinition]:
        with self._lock:
            self.auto_expire_cooldowns(pool)
            routes = self.coding_routes if pool == "coding" else self.agent_routes
            now_mono = time.monotonic()
            valid: List[RouteDefinition] = []

            for r in routes:
                if self.allowed_providers is not None and r.provider.lower() not in self.allowed_providers:
                    continue

                # If route requires an API key, only activate if the user exported a valid, non-empty key!
                # If key is missing, simply skip this provider without failing the fallback!
                if r.env_key:
                    key_val = self.keys.get(r.env_key, "").strip()
                    if not key_val:
                        continue

                if (pool.lower(), r.model.lower()) in self.deprecated:
                    continue

                if self.is_route_quota_exhausted(pool, r.id, r.provider):
                    continue

                cooldown_exp = self.cooldowns.get((pool.lower(), r.id.lower()), 0)
                if now_mono < cooldown_exp:
                    continue

                valid.append(r)
            return valid

    def record_route_success(self, pool: str, route_id: str) -> None:
        """Record a successful response from a route, establishing sticky affinity."""
        with self._lock:
            p = pool.lower()
            self.last_success[p] = route_id.lower()
            self.last_success_at[p] = time.monotonic()

    def select_route(self, pool: str, requested_model: Optional[str] = None) -> Optional[RouteDefinition]:
        """Select next viable route using sticky-primary-with-decay affinity.

        1. If the last successful route is still healthy and succeeded within
           STICKY_AFFINITY_SECONDS (120s), prefer it (eliminates provider churn).
        2. Otherwise fall back to round-robin across all candidates.
        """
        with self._lock:
            candidates = self.get_candidate_routes(pool)
            if not candidates:
                # If all candidates are on cooldown, clear oldest cooldown as safety relief
                pool_cooldowns = {k: v for k, v in self.cooldowns.items() if k[0] == pool.lower()}
                if pool_cooldowns:
                    oldest_key = min(pool_cooldowns.keys(), key=lambda k: pool_cooldowns[k])
                    del self.cooldowns[oldest_key]
                    candidates = self.get_candidate_routes(pool)

            if not candidates:
                return None

            # 1. Sticky primary: try last successful route if it's fresh and still a candidate
            p = pool.lower()
            last_id = self.last_success.get(p)
            last_at = self.last_success_at.get(p, 0.0)
            if last_id and (time.monotonic() - last_at) < self.STICKY_AFFINITY_SECONDS:
                sticky = next((r for r in candidates if r.id.lower() == last_id), None)
                if sticky:
                    return sticky

            # 2. Round-robin fallback
            idx = (self.coding_index if pool == "coding" else self.agent_index) % len(candidates)
            selected = candidates[idx]

            if pool == "coding":
                self.coding_index = (self.coding_index + 1) % len(candidates)
            else:
                self.agent_index = (self.agent_index + 1) % len(candidates)

            return selected

    def add_discovered_route(self, pool: str, route: RouteDefinition) -> None:
        """Dynamically append a newly discovered free model to the pool."""
        with self._lock:
            target_list = self.coding_routes if pool == "coding" else self.agent_routes
            existing_ids = {r.id for r in target_list}
            if route.id not in existing_ids:
                target_list.append(route)
                logger.info("[✨ DISCOVERED] Added model '%s' to pool '%s'", route.model, pool)

    def load_from_quirks_ledger(self, ledger_path: Optional[Path] = None) -> None:
        """Load and synchronize pool routes and deprecations from quirks ledger."""
        import json
        p = ledger_path or (Path(__file__).resolve().parent.parent.parent / "docs" / "providers" / "provider_quirks_ledger.json")
        if not p.exists():
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            providers = data.get("providers", {})
            for prov_name, prov_info in providers.items():
                for model_id, model_info in prov_info.get("models", {}).items():
                    status = model_info.get("status", "active")
                    pool_name = model_info.get("pool", "coding")
                    if status == "deprecated":
                        self.mark_deprecated(pool_name, model_id)
                    elif status == "active":
                        route = RouteDefinition(
                            id=f"{prov_name}-ledger-{model_id.replace('/', '-')}",
                            provider=prov_name,
                            model=model_id,
                            pool=pool_name,
                            base_url=prov_info.get("base_url", ""),
                            api_format="openai",
                            env_key=prov_info.get("auth_env", ""),
                            context_length=model_info.get("context_window", 65536),
                            max_output_tokens=model_info.get("max_output_tokens", 4096),
                            is_discovered=True,
                        )
                        self.add_discovered_route(pool_name, route)
        except Exception as e:
            logger.warning("Could not sync pool routes from quirks ledger: %s", e)


POOL_MANAGER = IsolatedPoolManager()

