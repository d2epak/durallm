"""Budget-Aware Context Manager and Structured Semantic Compactor."""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Tuple

from durallm.errors import ContextOverflowError
from durallm.protocol.ir import (
    NormalizedRequest,
)

logger = logging.getLogger("durallm.agent.context")


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


DIAGNOSTIC_PATTERNS = re.compile(
    r"("
    r"(?:error|fatal|fail(?:ed|ure)?|exception|critical|traceback|panic)\b"
    r"|(?:exit(?:\s+code)?|returncode)\s*[:=]?\s*\d+"
    r"|(?:AssertionError|TypeError|ValueError|KeyError|IndexError|AttributeError|SyntaxError|NameError)"
    r"|(?:\bFAILED\b|\bFAIL\b|\b=== FAILURES ===\b|\b=== ERRORS ===\b)"
    r"|error\[E\d+\]"
    r"|error TS\d+"
    r"|(?:[\w\.\-/]+\.\w+):(\d+)(?::(\d+))?:\s*(?:fatal )?(?:error|warning)"
    r"|File \"[^\"]+\", line \d+"
    r"|diff --git"
    r"|@@ -[0-9,]+ \+[0-9,]+ @@"
    r")",
    re.IGNORECASE,
)


def extract_diagnostic_summary(raw_content: str, max_chars: int = 600) -> str:
    """
    Extract structured diagnostic information from compiler outputs, test traces,
    and tool outputs rather than blind text slicing.
    Preserves:
    - Compiler error sites (file:line:col: error)
    - Test failure assertions (FAILED test_..., AssertionError)
    - Python/runtime tracebacks
    - Exit codes and execution summaries
    """
    if len(raw_content) <= max_chars:
        return raw_content

    # 1. Attempt JSON structured extraction
    try:
        data = json.loads(raw_content)
        if isinstance(data, dict):
            extracted = {}
            for k in [
                "status", "exit_code", "returncode", "error", "errors", "message",
                "path", "file", "id", "count", "stderr", "stdout",
            ]:
                if k in data:
                    extracted[k] = data[k]
            if extracted:
                formatted = (
                    f"[Structured Tool Output Summary (by Circuit Breaker)]:\n"
                    f"{json.dumps(extracted, ensure_ascii=False, indent=2)}\n"
                    f"... (remaining payload truncated to preserve context budget)"
                )
                if len(formatted) <= max_chars:
                    return formatted
                return formatted[:max_chars] + "\n... [truncated]"
    except Exception:
        pass

    # 2. Text / Log file extraction: hunt for diagnostic lines
    lines = raw_content.splitlines()
    if len(lines) <= 4:
        half = max(50, (max_chars - 60) // 2)
        return (
            "[Historical Tool Output compacted by Circuit Breaker to fit target budget]\n"
            + raw_content[:half]
            + "\n... [truncated] ...\n"
            + raw_content[-half:]
        )

    diagnostic_lines: list[str] = []
    seen = set()
    for ln in lines:
        cleaned = ln.strip()
        if not cleaned:
            continue
        # Skip repetitive progress lines
        if re.match(r"^[\.FEsxX]+\s+\[\s*\d+%\]", cleaned) or re.match(r"^\.+$", cleaned):
            continue
        if DIAGNOSTIC_PATTERNS.search(cleaned):
            if cleaned not in seen:
                seen.add(cleaned)
                diagnostic_lines.append(cleaned)

    header_lines = [ln.strip() for ln in lines[:3] if ln.strip()]
    tail_lines = [ln.strip() for ln in lines[-3:] if ln.strip()]

    parts = [
        "[Historical Tool Output compacted by Circuit Breaker to fit target budget]",
        f"--- HEAD ({len(lines)} total lines) ---",
        "\n".join(header_lines),
    ]

    if diagnostic_lines:
        max_diag = max(2, min(len(diagnostic_lines), 8))
        parts.extend([
            f"--- EXTRACTED DIAGNOSTICS & ERRORS ({len(diagnostic_lines)} findings) ---",
            "\n".join(diagnostic_lines[:max_diag]),
        ])

    parts.extend([
        "--- TAIL ---",
        "\n".join(tail_lines),
    ])

    summary = "\n".join(parts)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n... [truncated]"
    return summary


def extract_structured_tool_summary(raw_content: str, max_chars: int = 500) -> str:
    """Backward-compatible wrapper for extract_diagnostic_summary."""
    return extract_diagnostic_summary(raw_content, max_chars=max_chars)


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
    4. Diagnostic compaction of large compiler/test traces in tool results and messages
    5. Recent execution turns (latest preserve_tail_turns intact)
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
        first_user_idx = next((idx for idx, message in enumerate(messages) if message.role == "user"), None)
        protected_prefix_count = (first_user_idx + 1) if first_user_idx is not None else 1

        # Phase 1: In-place diagnostic compaction of oversized tool results and logs
        for idx, m in enumerate(messages):
            for tr in m.tool_results:
                if len(tr.content) > 400:
                    tr.content = extract_diagnostic_summary(tr.content, max_chars=400)
            # Compact message content if it contains huge logs (e.g. OpenCode compiler/test outputs)
            # Do not drop root objective or final instruction entirely; compact huge diagnostic bodies
            is_root_user = (idx == first_user_idx)
            is_final_msg = (idx == len(messages) - 1)
            if m.content and len(m.content) > 1000:
                if not is_root_user and not is_final_msg:
                    m.content = extract_diagnostic_summary(m.content, max_chars=600)
                elif is_final_msg and not is_root_user:
                    # Final instruction has huge attached log; compact the log while keeping tail instructions
                    m.content = extract_diagnostic_summary(m.content, max_chars=1200)

        if estimate_tokens(compacted) <= target_tokens:
            return compacted, True

        # Phase 2: If message turns exceed tail window, evict intermediate turns
        cutoff_idx = len(messages) - self.preserve_tail_turns
        for idx in range(protected_prefix_count, max(protected_prefix_count, cutoff_idx)):
            m = messages[idx]
            for tr in m.tool_results:
                if len(tr.content) > 300:
                    tr.content = extract_diagnostic_summary(tr.content, max_chars=300)
            if m.content and len(m.content) > 600 and m.role == "assistant":
                m.content = (
                    m.content[:300]
                    + "\n... [Prior assistant reasoning compacted by Circuit Breaker] ...\n"
                    + m.content[-300:]
                )

        if estimate_tokens(compacted) <= target_tokens:
            return compacted, True

        start_evict_idx = protected_prefix_count
        while len(compacted.messages) > (self.preserve_tail_turns + protected_prefix_count):
            if estimate_tokens(compacted) <= target_tokens:
                break
            compacted.messages.pop(start_evict_idx)

        if estimate_tokens(compacted) <= target_tokens:
            return compacted, True

        # Phase 3: Aggressive compaction of tail turns if still over budget
        for idx, m in enumerate(compacted.messages):
            if idx == first_user_idx:
                continue
            for tr in m.tool_results:
                if len(tr.content) > 250:
                    tr.content = extract_diagnostic_summary(tr.content, max_chars=250)
            if m.content and len(m.content) > 500:
                m.content = extract_diagnostic_summary(m.content, max_chars=400)

        self._require_fit(compacted, target_tokens)
        return compacted, True

    @staticmethod
    def _require_fit(request: NormalizedRequest, target_tokens: int) -> None:
        remaining = estimate_tokens(request)
        if remaining > target_tokens:
            raise ContextOverflowError(required_tokens=remaining, available_budget=target_tokens)

