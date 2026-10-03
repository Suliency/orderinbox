"""ModelRouter — pick *which* model handles a task.

From the integration proposal (Section 2):

    result = model_router.run(
        task="quote_normalization",
        data=quote,
        confidentiality="high",
        complexity="medium"
    )

and (Section 3, "Local-First, Cloud-Escalation Strategy"):

    ALL REQUESTS -> local model first; escalate to cloud only for genuinely
    ambiguous work.

The router is the *only* place that knows which provider to use. The workflow
never addresses a backend directly, so Strata can be swapped for vLLM, OpenAI,
Anthropic or Gemini (or a second DGX Spark) by changing configuration alone.

Routing rules, in priority order:
  1. confidentiality == "high"  -> require a local provider (data stays on-prem)
  2. complexity == "high"       -> require a strong-tier model
  3. local-first                 -> among the eligible providers, local wins;
                                    cloud is the fallback
  4. otherwise                   -> first available eligible provider

Every decision records a human-readable reason so the audit trail (Section
21/22) shows *why* a value was produced the way it was.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .providers import STRENGTH_STRONG

log = logging.getLogger("orderinbox.ai.router")


@dataclass
class RouteRequest:
    """A single model-call request, described by policy-relevant attributes.

    `data` is carried for logging/audit but the router routes on the three
    policy fields, not on the payload.
    """
    task: str                          # e.g. "quote_normalization", "order_extraction"
    data: Any = None
    confidentiality: str = "normal"    # "low" | "normal" | "high"
    complexity: str = "medium"         # "low" | "medium" | "high"


@dataclass
class RouteDecision:
    """Why the router chose the provider it chose — stored in the audit log."""
    provider_name: str = ""
    family: str = ""
    kind: str = ""
    reason: str = ""
    escalated_to_cloud: bool = False


class ModelRouter:
    def __init__(self, providers: list):
        # keep the caller's order (build_providers returns local-first)
        self.providers = list(providers)

    # ------------------------------------------------------------------
    def available(self) -> bool:
        return any(self._usable(p) for p in self.providers)

    @staticmethod
    def _usable(p) -> bool:
        try:
            return bool(p.available())
        except Exception:
            return False

    # ------------------------------------------------------------------
    def pick(self, req: RouteRequest) -> tuple[Optional[Any], RouteDecision]:
        high_secret = req.confidentiality == "high"
        needs_strong = req.complexity == "high"

        eligible = [p for p in self.providers if self._usable(p)]
        if not eligible:
            return None, RouteDecision(reason="no provider available")

        def qualifies(p) -> bool:
            if high_secret and not p.spec.is_local:
                return False
            if needs_strong and p.spec.strength < STRENGTH_STRONG:
                return False
            return True

        strict = [p for p in eligible if qualifies(p)]
        used_strict = bool(strict)
        pool = strict if strict else eligible

        # local-first: partition into local / cloud, local wins
        local = [p for p in pool if p.spec.is_local]
        cloud = [p for p in pool if not p.spec.is_local]
        chosen = local[0] if local else (cloud[0] if cloud else None)
        if chosen is None:
            return None, RouteDecision(reason="no eligible provider")

        reasons = []
        if high_secret:
            reasons.append("high confidentiality -> local-only")
        if needs_strong:
            reasons.append("high complexity -> strong model")
        if not used_strict:
            reasons.append("no provider met all constraints; relaxed to any available")
        if high_secret and not chosen.spec.is_local:
            reasons.append("WARNING: confidential task ran on a cloud provider (no local available)")
        if chosen.spec.is_local:
            reasons.append("local-first")
        else:
            reasons.append("escalated to cloud")

        decision = RouteDecision(
            provider_name=chosen.spec.label or chosen.spec.family,
            family=chosen.spec.family,
            kind=chosen.spec.kind,
            reason="; ".join(reasons) or "default",
            escalated_to_cloud=(not chosen.spec.is_local),
        )
        log.info("route %s -> %s (%s)", req.task, decision.provider_name, decision.reason)
        return chosen, decision

    # ------------------------------------------------------------------
    def run(self, req: RouteRequest, system: str, user: str) -> tuple[Any, RouteDecision]:
        provider, decision = self.pick(req)
        if provider is None:
            return None, decision
        return provider, decision

    def complete(self, req: RouteRequest, system: str, user: str) -> tuple[str, RouteDecision]:
        """Route and call the provider; returns (text, decision)."""
        provider, decision = self.pick(req)
        if provider is None:
            raise RuntimeError(f"no LLM provider available for task={req.task}")
        log.info("LLM call via %s (%s)", decision.provider_name, req.task)
        return provider.complete(system, user), decision

    def extract_json(self, req: RouteRequest, system: str, user: str) -> tuple[dict, RouteDecision]:
        provider, decision = self.pick(req)
        if provider is None:
            raise RuntimeError(f"no LLM provider available for task={req.task}")
        return provider.extract_json(system, user), decision
