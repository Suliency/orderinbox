"""The model gateway: provider abstraction, routing, escalation, verification.

This package is the "Strata Local LLM Integration" from the proposal — the
reasoning/model-serving layer sits *behind* a gateway so the workflow never
hard-codes a backend, and so local inference handles the volume while cloud
handles the ambiguous tail.
"""
from .escalation import EscalationDecision, EscalationPolicy, EscalationThresholds, Tier
from .gateway import ModelGateway
from .provenance import FieldProvenance, Source, provenance
from .providers import (
    AnthropicProvider,
    GeminiProvider,
    LLMError,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderSpec,
    STRENGTH_FAST,
    STRENGTH_MEDIUM,
    STRENGTH_STRONG,
    build_providers,
    parse_json_loose,
)
from .router import ModelRouter, RouteDecision, RouteRequest
from .verifier import FieldDiff, Verdict, compare

__all__ = [
    "ModelGateway",
    "ModelRouter", "RouteRequest", "RouteDecision",
    "EscalationPolicy", "EscalationThresholds", "EscalationDecision", "Tier",
    "ProviderSpec", "build_providers", "parse_json_loose", "LLMError",
    "OpenAICompatibleProvider", "OllamaProvider", "AnthropicProvider", "GeminiProvider",
    "STRENGTH_FAST", "STRENGTH_MEDIUM", "STRENGTH_STRONG",
    "FieldProvenance", "Source", "provenance",
    "Verdict", "FieldDiff", "compare",
]
