"""Opt-in environment flags shared by the V1 and V3 planes."""

from __future__ import annotations

import os


def env_flag(name: str) -> bool:
    """True when an opt-in environment variable is set to 1/true/yes/on."""
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")
