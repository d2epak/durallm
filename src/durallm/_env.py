"""Opt-in environment flags shared by the V1 and V3 planes."""

from __future__ import annotations

import os

# Loopback / RFC1918 upstreams (Ollama, LM Studio, a LAN proxy) are blocked unless this is set.
ALLOW_LOCAL_UPSTREAM_ENV = "DURALLM_ALLOW_LOCAL_UPSTREAM"


def env_flag(name: str) -> bool:
    """True when an opt-in environment variable is set to 1/true/yes/on."""
    val = os.getenv(name)
    if val is None and name.startswith("DURALLM_"):
        legacy_name = "LLM_BREAKER_" + name[8:]
        val = os.getenv(legacy_name)
    return (val or "").strip().lower() in ("1", "true", "yes", "on")
