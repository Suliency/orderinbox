"""ModelGateway — the single object the workflow talks to for all model work.

This is the practical "integration" the proposal asks for: the workflow never
addresses a backend directly. It asks the gateway for a model call, and the
gateway decides — via the ModelRouter — which provider runs it (local-first),
and — via the EscalationPolicy / ProducerVerifier — how much trust the result
gets.

    result = gateway.complete(RouteRequest(task="quote_normalization",
                                           confidentiality="high",
                                           complexity="medium"),
                              system, user)

    decision = gateway.escalate(confidence=0.74)   # -> CLOUD
    verdict  = gateway.verify(extractor_json, validator_json,
                              ["origin_port", "base_freight"],
                              money_fields={"base_freight"})

Everything is observable: each call and each escalation returns a small
decision record that the pipeline folds into its audit log.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .escalation import EscalationDecision, EscalationPolicy, EscalationThresholds, Tier
from .providers import build_providers
from .router import ModelRouter, RouteDecision, RouteRequest
from .verifier import Verdict, compare

log = logging.getLogger("orderinbox.ai.gateway")


class ModelGateway:
    def __init__(self, settings,
                 escalation: EscalationPolicy | None = None,
                 providers: list | None = None):
        self.settings = settings
        self.providers = providers if providers is not None else build_providers(settings)
        self.router = ModelRouter(self.providers)
        self.escalation = escalation or EscalationPolicy(EscalationThresholds())

    # ------------------------------------------------------------------
    def available(self) -> bool:
        return self.router.available()

    def providers_summary(self) -> list[dict]:
        # availability first: a health check may update the spec (Strata
        # reports vision on /health)
        usable = [self._usable(p) for p in self.providers]
        return [
            {"name": p.spec.label or p.spec.family, "family": p.spec.family,
             "kind": p.spec.kind, "strength": p.spec.strength,
             "vision": p.spec.vision, "available": ok}
            for p, ok in zip(self.providers, usable)
        ]

    @staticmethod
    def _usable(p) -> bool:
        try:
            return bool(p.available())
        except Exception:
            return False

    # ------------------------------------------------------------------
    def route(self, req: RouteRequest) -> tuple[Optional[Any], RouteDecision]:
        return self.router.pick(req)

    def complete(self, req: RouteRequest, system: str, user: str) -> tuple[str, RouteDecision]:
        provider, decision = self.router.pick(req)
        if provider is None:
            raise RuntimeError(f"no LLM provider available for task={req.task}")
        log.info("gateway call via %s (%s)", decision.provider_name, req.task)
        return provider.complete(system, user), decision

    def extract_json(self, req: RouteRequest, system: str,
                     user: str) -> tuple[dict, RouteDecision]:
        provider, decision = self.router.pick(req)
        if provider is None:
            raise RuntimeError(f"no LLM provider available for task={req.task}")
        return provider.extract_json(system, user), decision

    # ------------------------------------------------------------------
    def escalate(self, confidence: float, high_value: bool = False) -> EscalationDecision:
        return self.escalation.tier_for(confidence, high_value=high_value)

    def verify(self, a: dict, b: dict, fields: list[str],
               money_fields: set[str] | None = None) -> Verdict:
        return compare(a, b, fields, money_fields)

    # ------------------------------------------------------------------
    def extract_with_escalation(self, req: RouteRequest, system: str, user: str,
                                *, fields: list[str],
                                money_fields: set[str] | None = None,
                                high_value: bool = False) -> dict:
        """Full Section 3 + 10 + 11 loop in one call.

        1. Run the task on the routed (local-first) model  -> result A.
        2. Read A's stated confidence.
        3. EscalationPolicy maps that to a tier:
             AUTO   -> return A.
             VERIFY -> run a second (local) model for B; agree -> return A,
                      disagree -> escalate one tier.
             CLOUD  -> run the task on a cloud model -> return its result.
             HUMAN  -> return A flagged for human review.

        The returned dict is the model result plus an `_escalation` metadata
        block the pipeline can fold into its audit log.
        """
        provider, decision = self.router.pick(req)
        if provider is None:
            raise RuntimeError(f"no LLM provider available for task={req.task}")
        result = provider.extract_json(system, user)

        confidence = float(result.get("confidence", 0.0) or 0.0)
        verdict = self.escalate(confidence, high_value=high_value)
        meta = {
            "provider": decision.provider_name,
            "route": decision.reason,
            "confidence": confidence,
            "tier": verdict.tier.value,
            "escalation": verdict.reason,
        }

        if verdict.tier is Tier.VERIFY:
            # Section 11: an *independent* second model re-derives the fields.
            # We deliberately pick a different provider (Model B) so the check
            # is not the same sampler agreeing with itself.
            checker = self._pick_different(provider)
            if checker is not None:
                try:
                    check = checker.extract_json(system, user)
                    v = self.verify(result, check, fields, money_fields)
                    meta["verifier"] = checker.spec.label or checker.spec.family
                    if v.agree:
                        meta["tier"] = "auto"
                        meta["escalation"] = "producer/verifier agreed -> auto"
                    else:
                        meta["escalation"] = ("producer/verifier disagreed on "
                                              f"{v.diff_fields} -> cloud")
                        return self._cloud_adjudicate(req, system, user, meta)
                except Exception as exc:
                    meta["verifier_error"] = str(exc)
            # no second model available -> stay on the base tier

        if verdict.tier is Tier.CLOUD or (
                verdict.tier is Tier.VERIFY and meta.get("tier") == "cloud"):
            return self._cloud_adjudicate(req, system, user, meta)

        if verdict.tier is Tier.HUMAN:
            meta["needs_human"] = True
        result["_escalation"] = meta
        return result

    def _pick_different(self, provider):
        """Return an available provider different from `provider` (Model B).

        Prefers a different family/model; falls back to any other usable
        instance. Returns None when there is only one usable provider.
        """
        cands = [p for p in self.providers if self._usable(p) and p is not provider]
        if not cands:
            return None
        sig = (provider.spec.family, getattr(provider, "model", ""))
        diff = [p for p in cands
                if (p.spec.family, getattr(p, "model", "")) != sig]
        return (diff or cands)[0]

    def _cloud_adjudicate(self, req: RouteRequest, system: str, user: str,
                          meta: dict) -> dict:
        # force a cloud provider when one exists
        cloud = [p for p in self.providers
                 if not p.spec.is_local and self._usable(p)]
        if cloud:
            provider = cloud[0]
            meta["provider"] = provider.spec.label or provider.spec.family
            meta["route"] = "cloud adjudication"
            try:
                result = provider.extract_json(system, user)
                meta["tier"] = "cloud"
                meta["needs_human"] = False
            except Exception as exc:
                meta["cloud_error"] = str(exc)
                meta["needs_human"] = True
                result = dict(meta)  # caller handles; keep a record
        else:
            # no cloud configured -> a borderline case becomes a human review
            meta["needs_human"] = True
            meta["note"] = "no cloud provider configured; routed to human review"
            result = {}
        result["_escalation"] = meta
        return result
