"""Confidence-based escalation (Sections 3 & 10) and the local-first /
cloud-escalation strategy.

Section 10's policy, verbatim:

    confidence > .95   -> accept automatically
    .80 - .95          -> second local model checks
    .60 - .80          -> cloud model adjudicates
    <  .60             -> human review

Section 3 makes it explicit that *local* handles the volume and *cloud* is
reserved for the genuinely ambiguous cases — we do not pay cloud prices for
the 95–99% of routine work a strong local model covers.

The tier is a pure function of a confidence number plus (optionally) whether
the task is high-value / high-risk, which can push a borderline value up one
tier. All thresholds are config-driven so a deployment can tighten or loosen
the policy without a code change.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Tier(str, Enum):
    AUTO = "auto"          # accept automatically
    VERIFY = "verify"      # second local model checks (producer/verifier)
    CLOUD = "cloud"        # cloud model adjudicates
    HUMAN = "human"        # human review


@dataclass
class EscalationThresholds:
    """The four cut-points from Section 10. Defaults are the proposal's values.

    They are expressed as *lower bounds*: a confidence at or above the value
    reaches that tier.
    """
    auto: float = 0.95      # >= this -> AUTO
    verify: float = 0.80    # >= this -> VERIFY
    cloud: float = 0.60     # >= this -> CLOUD ; below -> HUMAN

    def validate(self) -> None:
        if not (self.auto >= self.verify >= self.cloud):
            raise ValueError("thresholds must be ordered auto >= verify >= cloud")


@dataclass
class EscalationDecision:
    tier: Tier
    confidence: float
    reason: str
    #: whether a high-value flag pushed this one tier stricter than the raw
    #: confidence would suggest
    promoted: bool = False


class EscalationPolicy:
    def __init__(self, thresholds: EscalationThresholds | None = None):
        self.thresholds = thresholds or EscalationThresholds()
        self.thresholds.validate()

    def tier_for(self, confidence: float, high_value: bool = False) -> EscalationDecision:
        """Map a confidence number to a tier.

        `high_value` (a large quote, a margin-below-threshold deal, a
        conditional surcharge, ...) nudges the decision one tier stricter so
        consequential work gets a second look even at the same confidence.
        """
        t = self.thresholds
        c = max(0.0, min(1.0, float(confidence or 0.0)))

        base = (Tier.AUTO if c >= t.auto
                else Tier.VERIFY if c >= t.verify
                else Tier.CLOUD if c >= t.cloud
                else Tier.HUMAN)

        order = [Tier.AUTO, Tier.VERIFY, Tier.CLOUD, Tier.HUMAN]
        promoted = False
        tier = base
        if high_value and base is not Tier.HUMAN:
            tier = order[order.index(base) + 1]
            promoted = True

        if tier is Tier.AUTO:
            reason = f"confidence {c:.2f} >= {t.auto:.2f} -> auto"
        elif tier is Tier.VERIFY:
            reason = f"confidence {c:.2f} in [{t.verify:.2f},{t.auto:.2f}) -> second local check"
        elif tier is Tier.CLOUD:
            reason = f"confidence {c:.2f} in [{t.cloud:.2f},{t.verify:.2f}) -> cloud adjudication"
        else:
            reason = f"confidence {c:.2f} < {t.cloud:.2f} -> human review"
        if promoted:
            reason += " (promoted: high-value)"
        return EscalationDecision(tier=tier, confidence=c, reason=reason, promoted=promoted)
