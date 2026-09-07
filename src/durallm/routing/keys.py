"""Multi-Key Rotation and Per-Key Quota Isolation per Endpoint."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from durallm.capability.profile import Endpoint

logger = logging.getLogger("durallm.routing.keys")


class KeyRotationPool:
    """Thread-safe multi-key rotation and rate-limit mitigation per endpoint.

    Supports:
    1. Comma-separated keys in a single env_key (e.g. GROQ_API_KEY="key1,key2")
    2. Explicit list of env_keys in Endpoint.env_keys
    3. Weighted round-robin / least-recently-used selection
    4. Independent cooldown tracking per API key on HTTP 429
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._key_cooldowns: Dict[str, float] = {}
        self._rr_indices: Dict[str, int] = {}

    def get_candidate_keys(
        self, endpoint: Endpoint, keys: Dict[str, str]
    ) -> List[Tuple[str, str]]:
        """Return list of (key_value, key_identifier) configured for this endpoint."""
        candidates: List[Tuple[str, str]] = []

        # 1. Inspect endpoint.env_keys if specified
        if getattr(endpoint, "env_keys", None):
            for env_k in endpoint.env_keys:
                val = keys.get(env_k, "")
                if val:
                    # Could itself be comma-separated
                    for idx, part in enumerate(val.split(",")):
                        clean = part.strip()
                        if clean:
                            candidates.append((clean, f"{env_k}:{idx}"))

        # 2. Inspect endpoint.env_key if candidates are still empty
        if not candidates and endpoint.env_key:
            val = keys.get(endpoint.env_key, "")
            if val:
                parts = [p.strip() for p in val.split(",") if p.strip()]
                for idx, part in enumerate(parts):
                    candidates.append((part, f"{endpoint.env_key}:{idx}"))

        return candidates

    def get_active_key(
        self,
        endpoint: Endpoint,
        keys: Dict[str, str],
        now: Optional[float] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Select the next available key for an endpoint.

        Returns (key_value, key_identifier) or (None, None).
        """
        current_time = self._clock() if now is None else now
        with self._lock:
            candidates = self.get_candidate_keys(endpoint, keys)
            if not candidates:
                return None, None
            if len(candidates) == 1:
                val, kid = candidates[0]
                expires = self._key_cooldowns.get(kid, 0.0)
                if expires > current_time:
                    return None, kid
                return val, kid

            # Multi-key rotation: filter to available keys
            available = [
                (val, kid)
                for val, kid in candidates
                if self._key_cooldowns.get(kid, 0.0) <= current_time
            ]
            if not available:
                return None, None

            # Round-robin selection among available keys
            ep_id = endpoint.id
            curr_idx = self._rr_indices.get(ep_id, 0)
            selected_val, selected_kid = available[curr_idx % len(available)]
            self._rr_indices[ep_id] = (curr_idx + 1) % len(available)
            return selected_val, selected_kid

    def record_rate_limit(
        self,
        key_identifier: str,
        cooldown_seconds: Optional[float] = None,
        now: Optional[float] = None,
    ) -> None:
        """Mark a specific key in cooldown after HTTP 429."""
        current_time = self._clock() if now is None else now
        duration = max(1.0, cooldown_seconds if cooldown_seconds is not None else 60.0)
        with self._lock:
            self._key_cooldowns[key_identifier] = current_time + duration
            logger.warning(
                "[durallm] KEY IN COOLDOWN: %s for %.1fs (rate-limited)",
                key_identifier,
                duration,
            )

    def has_alternative_key(
        self,
        endpoint: Endpoint,
        keys: Dict[str, str],
        current_key_id: Optional[str],
        now: Optional[float] = None,
    ) -> bool:
        """Check if endpoint has another candidate key that is not in cooldown."""
        current_time = self._clock() if now is None else now
        with self._lock:
            candidates = self.get_candidate_keys(endpoint, keys)
            if len(candidates) <= 1:
                return False
            for val, kid in candidates:
                if kid != current_key_id and self._key_cooldowns.get(kid, 0.0) <= current_time:
                    return True
            return False

    def reset(self) -> None:
        """Reset all cooldowns and round-robin indices."""
        with self._lock:
            self._key_cooldowns.clear()
            self._rr_indices.clear()


DEFAULT_KEY_ROTATION_POOL = KeyRotationPool()
