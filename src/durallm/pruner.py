"""Dynamic Sliding-Window Context Pruner & History Compactor.

Prevents fatal HTTP 413 / context_length_exceeded errors when failing over
from a 1M context provider (e.g. Gemini) to a 32k/64k model (e.g. Cerebras,
Groq, or OpenRouter free models).
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger("durallm.pruner")

FREE_TIER_TPM_LIMIT = 12000


def estimate_tokens(payload: Any) -> int:
    """Rough but safe token estimation (approx 3.8 chars per token)."""
    if isinstance(payload, str):
        text_len = len(payload)
    else:
        try:
            text_len = len(json.dumps(payload, ensure_ascii=False))
        except Exception:
            text_len = len(str(payload))
    return max(1, (text_len + 3) // 4)


def prune_anthropic_request(
    request: Dict[str, Any],
    max_context_tokens: int,
    safety_margin_tokens: int = 2048
) -> Dict[str, Any]:
    """
    Prune an Anthropic Messages request to fit within max_context_tokens.
    Preserves:
    - System message
    - First user message (the initial goal/instructions)
    - The latest 6 message blocks (immediate execution context)
    Compacts:
    - Intermediate older tool_result outputs (file reads, terminal logs)
    - Very old intermediate assistant messages
    """
    current_tokens = estimate_tokens(request)
    target_tokens = max(512, max_context_tokens - safety_margin_tokens)

    if current_tokens <= target_tokens:
        return request

    logger.warning(
        "Request size (%d tokens) exceeds target model context (%d tokens). Initiating pruning.",
        current_tokens,
        target_tokens
    )

    req = copy.deepcopy(request)
    messages: List[Dict[str, Any]] = req.get("messages", [])
    if len(messages) <= 4:
        return req

    # Step 1: Compact historical tool_results in older turns
    preserve_tail = 6
    if len(messages) > preserve_tail + 1:
        older_messages = messages[1:-preserve_tail]
        for msg in older_messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        res_content = block.get("content", "")
                        if isinstance(res_content, str) and len(res_content) > 600:
                            block["content"] = (
                                res_content[:250]
                                + "\n... [Output compacted by Circuit Breaker to fit context window] ...\n"
                                + res_content[-250:]
                            )

    if estimate_tokens(req) <= target_tokens:
        return req

    # Step 2: Drop oldest intermediate pairs if still exceeding target
    while len(messages) > (preserve_tail + 2) and estimate_tokens(req) > target_tokens:
        messages.pop(1)

    req["messages"] = messages
    return req


def prune_openai_request(
    request: Dict[str, Any],
    max_context_tokens: int,
    safety_margin_tokens: int = 2048
) -> Dict[str, Any]:
    """
    Prune an OpenAI Chat Completion request to fit within max_context_tokens.
    Preserves:
    - System message
    - Initial user prompt
    - Latest 6 turns
    Compacts:
    - Historical role=='tool' payloads
    """
    current_tokens = estimate_tokens(request)
    target_tokens = max(512, max_context_tokens - safety_margin_tokens)

    if current_tokens <= target_tokens:
        return request

    req = copy.deepcopy(request)
    messages: List[Dict[str, Any]] = req.get("messages", [])
    if len(messages) <= 4:
        return req

    preserve_tail = 6
    start_idx = 1 if messages and messages[0].get("role") == "system" else 0
    start_idx += 1  # preserve root user prompt

    if len(messages) > (start_idx + preserve_tail):
        for msg in messages[start_idx:-preserve_tail]:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 600:
                    msg["content"] = (
                        content[:250]
                        + "\n... [Historical tool output compacted by Circuit Breaker] ...\n"
                        + content[-250:]
                    )

    if estimate_tokens(req) <= target_tokens:
        return req

    while len(messages) > (start_idx + preserve_tail) and estimate_tokens(req) > target_tokens:
        messages.pop(start_idx)

    req["messages"] = messages
    return req


def compact_tool_definitions(tools: List[Dict[str, Any]], max_desc_chars: int = 150, max_param_chars: int = 60) -> List[Dict[str, Any]]:
    """Compact verbose markdown documentation and examples from tool schemas without breaking function calling."""
    compacted = []
    for t in tools:
        if not isinstance(t, dict):
            compacted.append(t)
            continue
        t_copy = copy.deepcopy(t)
        fn = t_copy.get("function")
        if isinstance(fn, dict):
            if "description" in fn and isinstance(fn["description"], str) and len(fn["description"]) > max_desc_chars:
                desc = fn["description"]
                first_sent = desc.split(". ")[0] + "."
                fn["description"] = first_sent if len(first_sent) <= max_desc_chars else desc[:max_desc_chars]
            params = fn.get("parameters")
            if isinstance(params, dict) and "properties" in params and isinstance(params["properties"], dict):
                for p_name, p_val in params["properties"].items():
                    if isinstance(p_val, dict) and "description" in p_val and isinstance(p_val["description"], str):
                        if len(p_val["description"]) > max_param_chars:
                            p_val["description"] = p_val["description"][:max_param_chars]
        compacted.append(t_copy)
    return compacted


def prune_for_free_tpm(
    request: Dict[str, Any],
    tpm_limit: int = FREE_TIER_TPM_LIMIT,
    safety_margin_tokens: int = 2048,
) -> Dict[str, Any]:
    """Prune request payload to fit within free-tier TPM ceilings (default: 12,000 tokens)."""
    current_tokens = estimate_tokens(request)
    if current_tokens > tpm_limit:
        logger.warning(
            "Request payload size (%d tokens) exceeds free-tier TPM ceiling (%d tokens). Pruning payload.",
            current_tokens,
            tpm_limit,
        )
        return prune_openai_request(request, max_context_tokens=tpm_limit, safety_margin_tokens=safety_margin_tokens)
    return request


def prune_for_groq_tpm(
    payload: Dict[str, Any],
    max_input_tokens: int = 5200,
) -> Dict[str, Any]:
    """Prune and compact an OpenAI-format chat completion payload specifically for Groq's 7,000 ITPM / 8,000 TPM limit."""
    req = copy.deepcopy(payload)
    current_tokens = estimate_tokens(req)
    if current_tokens <= max_input_tokens:
        return req

    logger.info(
        "Request size (%d tokens) exceeds Groq free-tier safe input ceiling (%d tokens). Initiating Groq TPM compaction.",
        current_tokens,
        max_input_tokens,
    )

    # Step 1: Compact tool descriptions if tools are present
    if "tools" in req and isinstance(req["tools"], list) and req["tools"]:
        req["tools"] = compact_tool_definitions(req["tools"])
        if estimate_tokens(req) <= max_input_tokens:
            return req

    # Step 2: Compact system prompt if present and oversized
    messages: List[Dict[str, Any]] = req.get("messages", [])
    if messages and messages[0].get("role") == "system":
        sys_content = messages[0].get("content", "")
        if isinstance(sys_content, str) and len(sys_content) > 1500:
            lines = [line.strip() for line in sys_content.splitlines() if line.strip()]
            condensed = "\n".join(lines)
            if len(condensed) > 2000:
                condensed = condensed[:2000] + "\n... [System prompt formatting rules compacted for Groq execution] ..."
            messages[0]["content"] = condensed
            if estimate_tokens(req) <= max_input_tokens:
                return req

    # Step 3: Run standard sliding-window message pruning
    req = prune_openai_request(req, max_context_tokens=max_input_tokens, safety_margin_tokens=250)
    if estimate_tokens(req) <= max_input_tokens:
        return req

    # Step 4: If single-turn/root message is still oversized, compact user prompt content
    for msg in req.get("messages", []):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and len(content) > 3000:
                from durallm.agent.context import extract_diagnostic_summary
                msg["content"] = extract_diagnostic_summary(content, max_chars=2500)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_val = part.get("text", "")
                        if len(text_val) > 3000:
                            from durallm.agent.context import extract_diagnostic_summary
                            part["text"] = extract_diagnostic_summary(text_val, max_chars=2500)

    return req

