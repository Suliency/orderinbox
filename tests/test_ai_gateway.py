"""Model gateway: provider abstraction, router (local-first routing),
confidence-tier escalation, and the producer/verifier.

These tests use in-memory fake providers so no network or model is required —
they verify the *decision logic* (which the proposal cares most about) rather
than the wire protocols.
"""
import pytest

from orderinbox.ai import (
    EscalationPolicy,
    EscalationThresholds,
    ModelGateway,
    ProviderSpec,
    RouteRequest,
    Tier,
    compare,
    provenance,
)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeProvider:
    def __init__(self, spec, *, available=True, payload=None, text="{}"):
        self.spec = spec
        self._available = available
        self.payload = dict(payload or {})
        self.text = text
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def complete(self, system, user) -> str:
        self.calls += 1
        return self.text

    def extract_json(self, system, user) -> dict:
        self.calls += 1
        return dict(self.payload)


LOCAL_FAST = ProviderSpec("vllm", "local", 1, label="local-fast")
LOCAL_STRONG = ProviderSpec("strata", "local", 3, label="local-strong")
CLOUD = ProviderSpec("openai", "cloud", 3, label="cloud")


def _settings():
    from orderinbox.config import Settings
    import os
    os.environ["ORDERINBOX_HOME"] = "/tmp/oi-ai-test"
    os.environ["LLM_MODE"] = "off"
    return Settings()


# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------

def test_router_prefers_local():
    gw = ModelGateway(_settings(), providers=[FakeProvider(CLOUD),
                                              FakeProvider(LOCAL_FAST)])
    provider, decision = gw.router.pick(RouteRequest(task="t"))
    assert provider.spec.family == "vllm"
    assert decision.escalated_to_cloud is False


def test_router_high_confidentiality_requires_local():
    gw = ModelGateway(_settings(), providers=[FakeProvider(CLOUD),
                                              FakeProvider(LOCAL_STRONG)])
    provider, decision = gw.router.pick(
        RouteRequest(task="t", confidentiality="high"))
    assert provider.spec.is_local
    assert "high confidentiality" in decision.reason


def test_router_high_complexity_needs_strong():
    # only a fast local + a strong cloud: complexity=high must skip the fast one
    gw = ModelGateway(_settings(), providers=[FakeProvider(LOCAL_FAST),
                                              FakeProvider(CLOUD)])
    provider, decision = gw.router.pick(RouteRequest(task="t", complexity="high"))
    assert provider.spec.strength == 3
    assert provider is not None


def test_router_falls_back_to_cloud_when_local_down():
    gw = ModelGateway(_settings(), providers=[FakeProvider(LOCAL_FAST, available=False),
                                              FakeProvider(CLOUD)])
    provider, decision = gw.router.pick(RouteRequest(task="t"))
    assert provider.spec.family == "openai"
    assert decision.escalated_to_cloud is True


def test_router_no_provider():
    gw = ModelGateway(_settings(), providers=[])
    provider, decision = gw.router.pick(RouteRequest(task="t"))
    assert provider is None
    assert decision.reason == "no provider available"


# --------------------------------------------------------------------------
# escalation
# --------------------------------------------------------------------------

def test_escalation_tiers():
    policy = EscalationPolicy()
    assert policy.tier_for(0.97).tier is Tier.AUTO
    assert policy.tier_for(0.85).tier is Tier.VERIFY
    assert policy.tier_for(0.70).tier is Tier.CLOUD
    assert policy.tier_for(0.40).tier is Tier.HUMAN


def test_escalation_boundaries_use_configured_thresholds():
    policy = EscalationPolicy(EscalationThresholds(auto=0.9, verify=0.7, cloud=0.5))
    assert policy.tier_for(0.9).tier is Tier.AUTO
    assert policy.tier_for(0.8).tier is Tier.VERIFY
    assert policy.tier_for(0.6).tier is Tier.CLOUD
    assert policy.tier_for(0.4).tier is Tier.HUMAN


def test_escalation_high_value_promotes():
    policy = EscalationPolicy()
    # a borderline-verify value on a high-value deal is pushed to cloud
    assert policy.tier_for(0.85, high_value=True).tier is Tier.CLOUD
    # an auto value on a high-value deal is pushed to verify
    assert policy.tier_for(0.99, high_value=True).tier is Tier.VERIFY
    assert policy.tier_for(0.99, high_value=True).promoted is True


def test_escalation_thresholds_must_be_ordered():
    with pytest.raises(ValueError):
        EscalationPolicy(EscalationThresholds(auto=0.5, verify=0.9, cloud=0.7))


# --------------------------------------------------------------------------
# verifier (producer/verifier comparison)
# --------------------------------------------------------------------------

def test_verifier_agree_on_identical():
    a = {"po": "123", "customer": "Acme"}
    b = {"po": "123", "customer": "Acme"}
    assert compare(a, b, ["po", "customer"]).agree


def test_verifier_disagree_on_field():
    a = {"po": "123", "amount": 100.0}
    b = {"po": "123", "amount": 200.0}
    v = compare(a, b, ["po", "amount"], money_fields={"amount"})
    assert not v.agree
    assert v.diff_fields == ["amount"]


def test_verifier_money_tolerance():
    a = {"amount": 1925.00}
    b = {"amount": 1925.005}
    assert compare(a, b, ["amount"], money_fields={"amount"}).agree


def test_verifier_missing_field_is_a_diff():
    a = {"po": "123"}
    b = {"po": "123", "customer": "Acme"}
    v = compare(a, b, ["customer"])
    assert not v.agree
    assert v.diffs[0].kind == "missing_a"


# --------------------------------------------------------------------------
# gateway end-to-end escalation loop
# --------------------------------------------------------------------------

def test_gateway_auto_tier_returns_first_result():
    fast = FakeProvider(LOCAL_FAST, payload={"confidence": 0.98, "x": 1})
    gw = ModelGateway(_settings(), providers=[fast])
    out = gw.extract_with_escalation(RouteRequest(task="t"), "s", "u",
                                     fields=["x"])
    assert out["x"] == 1
    assert out["_escalation"]["tier"] == "auto"
    assert fast.calls == 1


def test_gateway_verify_uses_different_model_and_agrees():
    fast = FakeProvider(LOCAL_FAST, payload={"confidence": 0.85, "x": 1})
    strong = FakeProvider(LOCAL_STRONG, payload={"confidence": 0.85, "x": 1})
    gw = ModelGateway(_settings(), providers=[fast, strong])
    out = gw.extract_with_escalation(RouteRequest(task="t"), "s", "u",
                                     fields=["x"])
    # router picked fast first; verifier must have been the *other* model
    assert fast.calls == 1
    assert strong.calls == 1
    assert out["_escalation"]["tier"] == "auto"
    assert out["_escalation"].get("verifier") == "local-strong"


def test_gateway_verify_disagreement_routes_to_cloud():
    fast = FakeProvider(LOCAL_FAST, payload={"confidence": 0.85, "x": 1})
    strong = FakeProvider(LOCAL_STRONG, payload={"confidence": 0.85, "x": 2})
    cloud = FakeProvider(CLOUD, payload={"confidence": 0.95, "x": 2})
    gw = ModelGateway(_settings(), providers=[fast, strong, cloud])
    out = gw.extract_with_escalation(RouteRequest(task="t"), "s", "u",
                                     fields=["x"])
    # disagreement -> cloud adjudication; cloud's value wins
    assert out["x"] == 2
    assert out["_escalation"]["tier"] == "cloud"
    assert cloud.calls == 1


def test_gateway_cloud_tier_calls_cloud():
    fast = FakeProvider(LOCAL_FAST, payload={"confidence": 0.7, "x": 1})
    cloud = FakeProvider(CLOUD, payload={"confidence": 0.9, "x": 9})
    gw = ModelGateway(_settings(), providers=[fast, cloud])
    out = gw.extract_with_escalation(RouteRequest(task="t"), "s", "u",
                                     fields=["x"])
    assert out["x"] == 9
    assert out["_escalation"]["tier"] == "cloud"


def test_gateway_human_tier_flags_review():
    fast = FakeProvider(LOCAL_FAST, payload={"confidence": 0.3, "x": 1})
    gw = ModelGateway(_settings(), providers=[fast])
    out = gw.extract_with_escalation(RouteRequest(task="t"), "s", "u",
                                     fields=["x"])
    assert out["_escalation"]["tier"] == "human"
    assert out["_escalation"]["needs_human"] is True


def test_gateway_available_false_when_none():
    gw = ModelGateway(_settings(), providers=[])
    assert gw.available() is False


def test_gateway_providers_summary():
    gw = ModelGateway(_settings(), providers=[FakeProvider(LOCAL_STRONG)])
    summary = gw.providers_summary()
    assert summary[0]["name"] == "local-strong"
    assert summary[0]["kind"] == "local"
    assert summary[0]["available"] is True


# --------------------------------------------------------------------------
# provenance (Section 10: every extracted field carries confidence + source)
# --------------------------------------------------------------------------

def test_provenance_carries_source_and_clamps_confidence():
    p = provenance(2150, 1.4, file="agent_quote_234.pdf", page=2,
                   text="O/F USD 2,150 / 40HC")
    assert p.value == 2150
    assert p.confidence == 1.0            # clamped into [0, 1]
    assert p.source.file == "agent_quote_234.pdf"
    assert p.source.page == 2
    assert p.source.text == "O/F USD 2,150 / 40HC"


def test_provenance_negative_clamps_to_zero():
    p = provenance("x", -0.5)
    assert p.confidence == 0.0
