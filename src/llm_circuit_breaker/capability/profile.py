"""Model, Deployment, Endpoint, and Capability Profiles."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class CapabilityVerificationStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    VERIFIED = "VERIFIED"
    UNAVAILABLE = "UNAVAILABLE"
    DEGRADED = "DEGRADED"


@dataclass
class PricingProfile:
    """Explicit pricing structure per 1M tokens."""
    input_price_per_1m: float = 0.0
    output_price_per_1m: float = 0.0
    cached_input_price_per_1m: float = 0.0
    reasoning_price_per_1m: float = 0.0
    is_free: bool = False
    currency: str = "USD"


@dataclass
class PrivacyProfile:
    """Data handling and compliance classification."""
    data_retention: str = "zero_retention"  # "zero_retention", "30_day", "training"
    region: Optional[str] = None
    compliance: List[str] = field(default_factory=list)  # ["HIPAA", "SOC2", "GDPR"]
    allows_external_traffic: bool = True


@dataclass
class QuotaBucket:
    """Upstream rate limit and quota bucket tracking."""
    bucket_id: str
    rpm_limit: Optional[int] = None
    tpm_limit: Optional[int] = None
    rpd_limit: Optional[int] = None
    current_rpm_used: int = 0
    current_tpm_used: int = 0
    reset_at: float = field(default_factory=time.time)


@dataclass
class ModelProfile:
    """Declared and verified capabilities of an LLM."""
    provider: str
    model: str
    protocol: str = "openai"  # "openai", "anthropic", "gemini"
    context_window: int = 65536
    max_output_tokens: int = 4096
    # None means "undeclared": the registry returns it for unknown models, and a
    # requirement for that capability then excludes the candidate (pessimistic).
    supports_tools: Optional[bool] = True
    supports_parallel_tools: Optional[bool] = True
    supports_structured_output: Optional[bool] = True
    supports_vision: bool = False
    supports_reasoning: bool = False
    supports_streaming: bool = True
    supports_json_mode: bool = True
    supports_system_prompt: bool = True
    supports_multipart: bool = False
    is_free: bool = False
    input_price_per_1m: float = 0.0
    output_price_per_1m: float = 0.0
    pricing: Optional[PricingProfile] = None
    privacy: PrivacyProfile = field(default_factory=PrivacyProfile)
    verification_status: CapabilityVerificationStatus = CapabilityVerificationStatus.UNKNOWN
    last_verified_at: Optional[float] = None
    verification_method: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.pricing is None:
            self.pricing = PricingProfile(
                input_price_per_1m=self.input_price_per_1m,
                output_price_per_1m=self.output_price_per_1m,
                is_free=self.is_free,
            )
        else:
            if self.is_free:
                self.pricing.is_free = True
            if self.input_price_per_1m > 0:
                self.pricing.input_price_per_1m = self.input_price_per_1m


@dataclass
class Endpoint:
    """Target provider endpoint definition."""
    id: str
    provider: str
    model: str
    base_url: str
    protocol: str = "openai"
    deployment: Optional[str] = None
    quota_bucket_id: Optional[str] = None
    env_key: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    weight: float = 1.0
    priority: int = 1
    profile: Optional[ModelProfile] = None
    pool: str = "general_agent"
    is_discovered: bool = False

    @property
    def resource_key(self) -> str:
        """Composite identity: provider x deployment x model x quota bucket."""
        dep = self.deployment or "default"
        quota = self.quota_bucket_id or "default"
        return f"{self.provider}:{dep}:{self.model}:{quota}"
