"""Budget-Aware Context Manager and Structured Semantic Compactor."""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Tuple

from llm_circuit_breaker.errors import ContextOverflowError
from llm_circuit_breaker.protocol.ir import (
    NormalizedRequest,
)

logger = logging.getLogger("llm_circuit_breaker.agent.context")


def estimate_tokens(payload: Any) -> int:
    """Safe, conservative token estimation (~3.8 characters per token)."""
    if isinstance(payload, str):
        text_len = len(payload)
    elif isinstance(payload, NormalizedRequest):
        parts = []
        if payload.system_instruction:
            parts.append(payload.system_instruction)
        for m in payload.messages:
            if m.content:
                parts.append(m.content)
            if m.reasoning_content:
                parts.append(m.reasoning_content)
            for tc in m.tool_calls:
                parts.append(tc.raw_arguments or json.dumps(tc.arguments))
            for tr in m.tool_results:
                parts.append(tr.content)
        text_len = sum(len(p) for p in parts)
    else:
        try:
            text_len = len(json.dumps(payload, ensure_ascii=False))
        except Exception:
            text_len = len(str(payload))
    return max(1, (text_len + 3) // 4)


def extract_structured_tool_summary(raw_content: str, max_chars: int = 500) -> str:
    """
    Extract structured diagnostic information from tool outputs rather than blind text slicing.
    Preserves exit codes, error messages, stack traces, paths, and status keys.
    """
    if len(raw_content) <= max_chars:
        return raw_content

    # 1. Attempt JSON structured extraction
    try:
        data = json.loads(raw_content)
        if isinstance(data, dict):
            extracted = {}
            for k in ["status", "exit_code", "returncode", "error", "errors", "message", "path", "file", "id", "count"]:
                if k in data:
                    extracted[k] = data[k]
            if extracted:
                return (
                    f"[Structured Tool Output Summary (by Circuit Breaker)]:\n"
                    f"{json.dumps(extracted, ensure_ascii=False, indent=2)}\n"
                    f"... (remaining payload truncated to preserve context budget)"
                )
    except Exception:
        pass

    # 2. Text / Log file extraction: hunt for diagnostic lines
    lines = raw_content.splitlines()
    if len(lines) <= 2:
        return (
            "[Historical Tool Output compacted by Circuit Breaker to fit target budget]\n"
            + raw_content[:max_chars // 2]
            + "\n... [truncated] ...\n"
            + raw_content[-(max_chars // 2):]
        )

    error_patterns = re.compile(r"(error|exception|fail|fatal|critical|traceback|exit code|returncode)", re.IGNORECASE)
    diagnostic_lines = [ln.strip() for ln in lines if error_patterns.search(ln)]

    header_lines = lines[:2]
    tail_lines = lines[-2:]

    parts = [
        "[Historical Tool Output compacted by Circuit Breaker to fit target budget]",
        f"--- HEAD ({len(lines)} total lines) ---",
        "\n".join(header_lines),
    ]

    if diagnostic_lines:
        parts.extend([
            "--- EXTRACTED DIAGNOSTICS & ERRORS ---",
            "\n".join(diagnostic_lines[:4]),
        ])

    parts.extend([
        "--- TAIL ---",
        "\n".join(tail_lines),
    ])

    summary = "\n".join(parts)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n... [truncated]"
    return summary


@dataclass
class ContextBudget:
    """Model context window and reserved output token budget."""
    model_context_window: int = 65536
    desired_output_tokens: int = 4096
    safety_margin_tokens: int = 2048

    @property
    def available_input_budget(self) -> int:
        """Remaining tokens available for input prompt history."""
        return max(512, self.model_context_window - self.desired_output_tokens - self.safety_margin_tokens)


class ContextManager:
    """
    Manages request context size, enforcing explicit token budgets and hierarchical compaction.
    Compaction hierarchy:
    1. System instructions (never dropped)
    2. Root user objective (first user prompt, never dropped)
    3. Active constraints (never dropped)
    4. Recent execution turns (latest preserve_tail_turns intact)
    5. Structured tool result summaries (preserving exit codes, errors, paths)
    6. Evict oldest intermediate pairs between root objective and recent tail turns.
    """

    def __init__(self, preserve_tail_turns: int = 6):
        self.preserve_tail_turns = preserve_tail_turns

    def compact(
        self,
        request: NormalizedRequest,
        budget: ContextBudget,
    ) -> Tuple[NormalizedRequest, bool]:
        """
        Compact request to fit strictly within the target model's available input budget.
        Returns (compacted_request, was_compacted).
        Raises ContextOverflowError when the protected content (system instruction, root
        objective, preserved tail turns) still exceeds the budget after every compaction phase.
        """
        current_tokens = estimate_tokens(request)
        target_tokens = budget.available_input_budget

        if current_tokens <= target_tokens:
            return request, False

        logger.info(
            "Request size (%d tokens) exceeds available budget (%d tokens). Initiating hierarchical compaction.",
            current_tokens, target_tokens
        )

        compacted = copy.deepcopy(request)
        messages = compacted.messages
        # OpenAI-compatible inputs commonly retain the system message in
        # ``messages`` *and* project it into ``system_instruction``. Protect
        # every leading system/developer message and the first user objective;
        # treating index 1 as disposable silently lost that objective.
        first_user_idx = next((idx for idx, message in enumerate(messages) if message.role == "user"), None)
        protected_prefix_count = (first_user_idx + 1) if first_user_idx is not None else 1

        if len(messages) <= (self.preserve_tail_turns + protected_prefix_count):
            # Too few messages to drop turns; compact content in place
            for m in messages:
                for tr in m.tool_results:
                    if len(tr.content) > 400:
                        tr.content = extract_structured_tool_summary(tr.content, max_chars=400)
            self._require_fit(compacted, target_tokens)
            return compacted, True

        # Phase 1: Structured semantic compaction of historical tool results in older turns
        cutoff_idx = len(messages) - self.preserve_tail_turns
        for idx in range(1, cutoff_idx):
            m = messages[idx]
            for tr in m.tool_results:
                if len(tr.content) > 400:
                    tr.content = extract_structured_tool_summary(tr.content, max_chars=400)
            if m.content and len(m.content) > 1000 and m.role == "assistant":
                m.content = (
                    m.content[:400]
                    + "\n... [Prior assistant reasoning compacted by Circuit Breaker] ...\n"
                    + m.content[-400:]
                )

        if estimate_tokens(compacted) <= target_tokens:
            return compacted, True

        # Phase 2: Drop only intermediate message turns. The root objective is
        # the first user message, not a hard-coded numeric index.
        start_evict_idx = protected_prefix_count
        while len(compacted.messages) > (self.preserve_tail_turns + protected_prefix_count):
            if estimate_tokens(compacted) <= target_tokens:
                break
            compacted.messages.pop(start_evict_idx)

        self._require_fit(compacted, target_tokens)
        return compacted, True

    @staticmethod
    def _require_fit(request: NormalizedRequest, target_tokens: int) -> None:
        remaining = estimate_tokens(request)
        if remaining > target_tokens:
            raise ContextOverflowError(required_tokens=remaining, available_budget=target_tokens)
