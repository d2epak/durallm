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
                                + "\n... [Output compacted by DuraLLM to fit context window] ...\n"
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
    safety_margin_tokens: int = 2048,
    min_preserve_tail: int = 2,
) -> Dict[str, Any]:
    """
    Prune an OpenAI Chat Completion request to fit strictly within max_context_tokens.
    Preserves:
    - System message (index 0)
    - Initial user objective (first user turn)
    - Most recent active turn(s)
    Compacts:
    - Historical role=='tool' payloads across all turns
    - Adaptively reduces tail window from 6 to min_preserve_tail (default: 2) if needed
    """
    from durallm.agent.context import extract_diagnostic_summary

    current_tokens = estimate_tokens(request)
    target_tokens = max(512, max_context_tokens - safety_margin_tokens)

    if current_tokens <= target_tokens:
        return request

    req = copy.deepcopy(request)
    messages: List[Dict[str, Any]] = req.get("messages", [])
    if not messages:
        return req

    if len(messages) <= 2:
        for msg in messages:
            if msg.get("role") in ("tool", "user"):
                c = msg.get("content")
                if isinstance(c, str) and len(c) > 2000:
                    msg["content"] = extract_diagnostic_summary(c, max_chars=1500)
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("type") == "tool_result":
                            tc = part.get("content", "")
                            if isinstance(tc, str) and len(tc) > 1000:
                                part["content"] = extract_diagnostic_summary(tc, max_chars=800)
        return req

    preserve_tail = 6
    start_idx = 1 if messages and messages[0].get("role") == "system" else 0
    start_idx += 1  # preserve root user prompt

    # Step 1: In-place diagnostic compaction of oversized tool results across ALL messages
    tail_cutoff = max(start_idx, len(messages) - 2)
    for idx, msg in enumerate(messages[start_idx:], start=start_idx):
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str):
                if idx < tail_cutoff and len(content) > 400:
                    msg["content"] = extract_diagnostic_summary(content, max_chars=300)
                elif idx >= tail_cutoff and len(content) > 1200:
                    # Compact huge tool outputs in tail (e.g. multi-thousand token file reads)
                    msg["content"] = extract_diagnostic_summary(content, max_chars=800)

    if estimate_tokens(req) <= target_tokens:
        return req

    # Step 2: Evict intermediate message turns between root objective and tail
    while len(messages) > (start_idx + preserve_tail) and estimate_tokens(req) > target_tokens:
        messages.pop(start_idx)

    if estimate_tokens(req) <= target_tokens:
        req["messages"] = messages
        return req

    # Step 3: If still exceeding budget, adaptively shrink preserve_tail down to min_preserve_tail
    while preserve_tail > min_preserve_tail and estimate_tokens(req) > target_tokens:
        preserve_tail -= 2
        while len(messages) > (start_idx + preserve_tail) and estimate_tokens(req) > target_tokens:
            messages.pop(start_idx)

    # Step 4: Compact assistant reasoning content if still exceeding budget
    if estimate_tokens(req) > target_tokens:
        for idx in range(start_idx, len(messages) - 1):
            m = messages[idx]
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) and len(m["content"]) > 400:
                m["content"] = extract_diagnostic_summary(m["content"], max_chars=300)

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
    max_input_tokens: int = 3800,
) -> Dict[str, Any]:
    """Prune and compact an OpenAI-format chat completion payload specifically for Groq's 6,000 TPM limit."""
    from durallm.agent.context import extract_diagnostic_summary

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
            if len(condensed) > 1800:
                condensed = condensed[:1800] + "\n... [System prompt formatting rules compacted for Groq execution] ..."
            messages[0]["content"] = condensed
            if estimate_tokens(req) <= max_input_tokens:
                return req

    # Step 3: Compact all tool results (both role="tool" and type="tool_result" parts)
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 600:
                msg["content"] = extract_diagnostic_summary(content, max_chars=500)
        elif role == "user":
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        t_content = part.get("content", "")
                        if isinstance(t_content, str) and len(t_content) > 600:
                            part["content"] = extract_diagnostic_summary(t_content, max_chars=500)
                        elif isinstance(t_content, list):
                            for sub in t_content:
                                if isinstance(sub, dict) and "text" in sub and len(sub["text"]) > 600:
                                    sub["text"] = extract_diagnostic_summary(sub["text"], max_chars=500)
            elif isinstance(content, str) and len(content) > 2000:
                msg["content"] = extract_diagnostic_summary(content, max_chars=1500)

    if estimate_tokens(req) <= max_input_tokens:
        return req

    # Step 4: Run sliding-window message pruning with adaptive tail down to 2 turns
    req = prune_openai_request(req, max_context_tokens=max_input_tokens, safety_margin_tokens=150, min_preserve_tail=2)
    if estimate_tokens(req) <= max_input_tokens:
        return req

    # Step 5: Aggressive compaction of user/tool messages if still over ceiling
    for msg in req.get("messages", []):
        if msg.get("role") in ("tool", "user"):
            content = msg.get("content")
            if isinstance(content, str) and len(content) > 1000:
                msg["content"] = extract_diagnostic_summary(content, max_chars=800)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text" and len(part.get("text", "")) > 1000:
                            part["text"] = extract_diagnostic_summary(part["text"], max_chars=800)
                        elif part.get("type") == "tool_result":
                            tc = part.get("content", "")
                            if isinstance(tc, str) and len(tc) > 400:
                                part["content"] = extract_diagnostic_summary(tc, max_chars=300)

    # Step 6: If still oversized, evict down to minimum tail (1 user turn + 1 assistant turn)
    msgs = req.get("messages", [])
    start_evict = 1 if msgs and msgs[0].get("role") == "system" else 0
    start_evict += 1  # keep root user
    while len(msgs) > (start_evict + 2) and estimate_tokens(req) > max_input_tokens:
        msgs.pop(start_evict)

    return req

