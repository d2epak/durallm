"""Prompt Cache and Prefix-Aware Routing Tracker (Frontier 6).

Preserves prompt cache hit rates (up to 90% cost savings and 50% TTFT reduction)
across multi-turn agent sessions (Hermes, Claude Code) by tracking warm-cache
prefix TTL windows and boosting candidate scores for warm endpoints.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from durallm.protocol.ir import NormalizedRequest

logger = logging.getLogger("durallm.routing.cache")


def compute_prefix_hash(request: NormalizedRequest) -> str:
    """Compute a byte-stable SHA-256 hash of the system instruction, tool schemas, and root turn.

    Tools are sorted deterministically by name so arbitrary tool order variations
    do not invalidate prompt caching.
    """
    hasher = hashlib.sha256()

    # 1. System instruction
    sys_text = request.get_effective_system_instruction() or ""
    hasher.update(sys_text.encode("utf-8"))
    hasher.update(b"\x00")

    # 2. Deterministically sorted tools
    if request.tools:
        sorted_tools = sorted(request.tools, key=lambda t: t.name)
        for t in sorted_tools:
            hasher.update(t.name.encode("utf-8"))
            hasher.update(b"\x01")
            hasher.update(t.description.encode("utf-8"))
            hasher.update(b"\x01")
            hasher.update(json.dumps(t.parameters, sort_keys=True).encode("utf-8"))
            hasher.update(b"\x00")
    else:
        hasher.update(b"no_tools\x00")

    # 3. Root message (initial user instruction)
    if request.messages:
        first_msg = request.messages[0]
        hasher.update(first_msg.role.encode("utf-8"))
        hasher.update(b"\x01")
        hasher.update((first_msg.content or "").encode("utf-8"))
        hasher.update(b"\x00")

    return hasher.hexdigest()


class PromptCacheTracker:
    """Thread-safe tracker of warm prompt cache TTL windows per endpoint."""

    def __init__(
        self,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._warm_entries: Dict[Tuple[str, str], float] = {}  # (endpoint_id, prefix_hash) -> expires_at

    def record_warm_cache(
        self,
        endpoint_id: str,
        prefix_hash: str,
        now: Optional[float] = None,
    ) -> None:
        """Mark an endpoint as having a warm prompt cache for the given prefix."""
        current_time = self._clock() if now is None else now
        expires_at = current_time + self.ttl_seconds
        with self._lock:
            self._warm_entries[(endpoint_id, prefix_hash)] = expires_at
            logger.debug(
                "[llm-circuit-breaker] WARM CACHE RECORDED for %s (prefix=%s, ttl=%.0fs)",
                endpoint_id,
                prefix_hash[:8],
                self.ttl_seconds,
            )

    def is_warm(
        self,
        endpoint_id: str,
        prefix_hash: str,
        now: Optional[float] = None,
    ) -> bool:
        """Check whether an endpoint's prompt cache for this prefix is currently warm."""
        current_time = self._clock() if now is None else now
        with self._lock:
            expires_at = self._warm_entries.get((endpoint_id, prefix_hash))
            if expires_at is None:
                return False
            if expires_at <= current_time:
                del self._warm_entries[(endpoint_id, prefix_hash)]
                return False
            return True

    def reset(self) -> None:
        """Clear all tracked warm-cache entries."""
        with self._lock:
            self._warm_entries.clear()


DEFAULT_PROMPT_CACHE_TRACKER = PromptCacheTracker()
