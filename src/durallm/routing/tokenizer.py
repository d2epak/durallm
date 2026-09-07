"""Conservative tokenizer preflight used to explain context routing decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from durallm.capability.profile import ModelProfile


@dataclass(frozen=True)
class TokenizerPreflight:
    tokenizer_id: str
    tokenizer_revision: Optional[str]
    input_tokens: int
    expected_output_tokens: int
    safety_margin_tokens: int
    context_window: int
    fits_without_compaction: bool
    requires_compaction: bool
    compatible: bool
    reason: Optional[str] = None


def preflight_context(
    profile: ModelProfile,
    input_tokens: int,
    expected_output_tokens: int,
    safety_margin_tokens: int,
    allow_compaction: bool,
) -> TokenizerPreflight:
    """Determine whether the target can hold the turn before dispatch.

    The current built-in estimator is conservative and its identity travels in
    the decision record. Provider-specific tokenizers can later replace it
    without changing the routing contract.
    """
    effective_output = min(
        expected_output_tokens if expected_output_tokens > 0 else (profile.max_output_tokens or 4096),
        profile.max_output_tokens or 4096,
    )
    required = max(0, input_tokens) + max(0, effective_output) + max(0, safety_margin_tokens)
    minimum_required = max(0, effective_output) + max(0, safety_margin_tokens)
    fits = required <= profile.context_window
    if fits:
        return TokenizerPreflight(
            tokenizer_id=profile.tokenizer_id,
            tokenizer_revision=profile.tokenizer_revision,
            input_tokens=max(0, input_tokens),
            expected_output_tokens=max(0, effective_output),
            safety_margin_tokens=max(0, safety_margin_tokens),
            context_window=profile.context_window,
            fits_without_compaction=True,
            requires_compaction=False,
            compatible=True,
        )
    if minimum_required > profile.context_window:
        reason = (
            f"Required output plus safety margin ({minimum_required} tokens) exceeds "
            f"context window ({profile.context_window} tokens)"
        )
        compatible = False
    else:
        reason = "Input exceeds context window and requires compaction"
        compatible = allow_compaction
    return TokenizerPreflight(
        tokenizer_id=profile.tokenizer_id,
        tokenizer_revision=profile.tokenizer_revision,
        input_tokens=max(0, input_tokens),
        expected_output_tokens=max(0, expected_output_tokens),
        safety_margin_tokens=max(0, safety_margin_tokens),
        context_window=profile.context_window,
        fits_without_compaction=False,
        requires_compaction=True,
        compatible=compatible,
        reason=reason,
    )
