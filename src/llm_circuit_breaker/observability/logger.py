"""Structured JSON Event Logging and Credential Redaction."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Optional

# Anchored at the end of the key so `input_tokens` / `max_tokens` are not treated as secrets.
SENSITIVE_KEY_PATTERN = re.compile(
    r"(api[_-]?key|secret[_-]?key|authorization|bearer|secret|token|password|cookie)$",
    re.IGNORECASE,
)

# Secret-looking values inside free text (error messages, URLs) are masked in place.
SECRET_VALUE_PATTERN = re.compile(r"(sk-[a-zA-Z0-9_-]{20,}|gsk_[a-zA-Z0-9_-]{20,}|AIza[a-zA-Z0-9_-]{30,})")
BEARER_PATTERN = re.compile(r"(bearer\s+)\S+", re.IGNORECASE)

_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}


def redact_sensitive_data(obj: Any, max_prompt_preview_chars: int = 120) -> Any:
    """Recursively redacts secrets and truncates raw prompts."""
    if isinstance(obj, dict):
        redacted = {}
        for k, v in obj.items():
            if SENSITIVE_KEY_PATTERN.search(str(k)):
                redacted[k] = "[REDACTED]"
            elif k in ("prompt", "messages", "content") and isinstance(v, str):
                if len(v) > max_prompt_preview_chars:
                    redacted[k] = f"{v[:max_prompt_preview_chars]}... [TRUNCATED_OBSERVABILITY_PREVIEW]"
                else:
                    redacted[k] = v
            else:
                redacted[k] = redact_sensitive_data(v, max_prompt_preview_chars)
        return redacted
    elif isinstance(obj, list):
        return [redact_sensitive_data(item, max_prompt_preview_chars) for item in obj]
    elif isinstance(obj, str):
        masked = SECRET_VALUE_PATTERN.sub("[REDACTED_API_KEY]", obj)
        return BEARER_PATTERN.sub(r"\1[REDACTED_API_KEY]", masked)
    return obj


class StructuredJsonLogger:
    """Emits one redacted JSON object per event.

    With `stream=None` (the default) events go through the stdlib logger named `name`, so the
    host application decides where they end up. Pass a stream to write lines directly.
    """

    def __init__(self, name: str = "llm_circuit_breaker.events", stream=None):
        self.name = name
        self.stream = stream
        self._logger = logging.getLogger(name)

    def log_event(
        self,
        level: str,
        event: str,
        request_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = {
            "timestamp": time.time(),
            "logger": self.name,
            "level": level.upper(),
            "event": event,
            "request_id": request_id,
            "data": redact_sensitive_data(metadata or {}),
        }
        line = json.dumps(payload, ensure_ascii=False, default=str)
        if self.stream is None:
            self._logger.log(_LEVELS.get(payload["level"], logging.INFO), line)
            return
        self.stream.write(line + "\n")
        self.stream.flush()

    def info(self, event: str, request_id: Optional[str] = None, **kwargs) -> None:
        self.log_event("INFO", event, request_id, kwargs)

    def warning(self, event: str, request_id: Optional[str] = None, **kwargs) -> None:
        self.log_event("WARNING", event, request_id, kwargs)

    def error(self, event: str, request_id: Optional[str] = None, **kwargs) -> None:
        self.log_event("ERROR", event, request_id, kwargs)


DEFAULT_STRUCTURED_LOGGER = StructuredJsonLogger()
