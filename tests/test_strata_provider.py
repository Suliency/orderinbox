"""StrataProvider — the wire protocol of the Strata local model server
(https://github.com/Niko1221/Strata): OpenAI-compatible chat completions plus
a /health endpoint, per-request thinking levels, and a vision flag.

httpx is monkeypatched, so no server is needed.
"""
import os
import tempfile

import pytest

from orderinbox.ai import providers as P
from orderinbox.ai.providers import ProviderSpec, StrataProvider


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _strata(**kw):
    spec = ProviderSpec("strata", "local", P.STRENGTH_STRONG, label="strata")
    args = dict(base_url="http://strata:8080/v1", model="strata")
    args.update(kw)
    return StrataProvider(spec, **args)


def _settings(**env):
    from orderinbox.config import Settings
    keys = ("ORDERINBOX_HOME", "LLM_MODE", "STRATA_URL", "STRATA_MODEL", "STRATA_REASONING_EFFORT",
            "STRATA_REASONING_BUDGET", "OLLAMA_URL", "VLLM_URL")
    saved = {k: os.environ.pop(k, None) for k in keys}
    os.environ["ORDERINBOX_HOME"] = tempfile.mkdtemp(prefix="oi-strata-")
    os.environ.update(env)
    try:
        return Settings()
    finally:
        for k in keys:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


# --------------------------------------------------------------------------
# health / availability
# --------------------------------------------------------------------------

def test_health_is_read_from_server_root(monkeypatch):
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return FakeResponse(200, {"status": "ok", "service": "strata", "images": False})

    monkeypatch.setattr(P.httpx, "get", fake_get)
    assert _strata().available() is True
    assert seen == ["http://strata:8080/health"]


def test_unhealthy_server_is_rechecked_after_retry_window(monkeypatch):
    """Strata opens its port only after the model loads (minutes). A provider
    that saw it down at boot must pick it up later without an app restart."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(P.time, "monotonic", lambda: clock["t"])
    state = {"up": False}

    def fake_get(url, **kw):
        if not state["up"]:
            raise P.httpx.ConnectError("refused")
        return FakeResponse(200, {"status": "ok"})

    monkeypatch.setattr(P.httpx, "get", fake_get)
    p = _strata()
    assert p.available() is False
    state["up"] = True
    assert p.available() is False          # still inside the retry window
    clock["t"] += StrataProvider.RECHECK_SECONDS + 1
    assert p.available() is True


def test_health_reports_vision(monkeypatch):
    monkeypatch.setattr(P.httpx, "get",
                        lambda url, **kw: FakeResponse(200, {"status": "ok", "images": True}))
    p = _strata()
    assert p.spec.vision is False
    assert p.available()
    assert p.spec.vision is True


def test_api_key_sent_on_health(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, **kw):
        seen.update(headers or {})
        return FakeResponse(200, {"status": "ok"})

    monkeypatch.setattr(P.httpx, "get", fake_get)
    _strata(api_key="s3cret").available()
    assert seen.get("Authorization") == "Bearer s3cret"


# --------------------------------------------------------------------------
# chat completions
# --------------------------------------------------------------------------

def test_complete_sends_reasoning_controls(monkeypatch):
    captured = {}

    def fake_post(url, json=None, **kw):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse(200, {"choices": [{"message": {
            "content": '{"ok": true}', "reasoning_content": "thinking..."}}]})

    monkeypatch.setattr(P.httpx, "post", fake_post)
    p = _strata(reasoning_effort="low", reasoning_budget_tokens=512)
    assert p.extract_json("sys", "user") == {"ok": True}
    assert captured["url"] == "http://strata:8080/v1/chat/completions"
    body = captured["payload"]
    assert body["model"] == "strata"
    assert body["reasoning_effort"] == "low"
    assert body["reasoning_budget_tokens"] == 512
    assert body["response_format"] == {"type": "json_object"}


def test_complete_omits_unset_reasoning_controls(monkeypatch):
    captured = {}

    def fake_post(url, json=None, **kw):
        captured["payload"] = json
        return FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr(P.httpx, "post", fake_post)
    _strata(reasoning_effort="", reasoning_budget_tokens=0).complete("s", "u")
    assert "reasoning_effort" not in captured["payload"]
    assert "reasoning_budget_tokens" not in captured["payload"]


def test_invalid_reasoning_effort_rejected():
    with pytest.raises(ValueError):
        _strata(reasoning_effort="extreme")


def test_http_error_raises_llm_error(monkeypatch):
    monkeypatch.setattr(P.httpx, "post",
                        lambda url, **kw: FakeResponse(503, text="engine restarting"))
    with pytest.raises(P.LLMError, match="strata HTTP 503"):
        _strata().complete("s", "u")


# --------------------------------------------------------------------------
# wiring from Settings
# --------------------------------------------------------------------------

def test_build_providers_puts_strata_ahead_of_ollama():
    s = _settings(LLM_MODE="auto", STRATA_URL="http://strata:8080/v1",
                  OLLAMA_URL="http://ollama:11434")
    built = P.build_providers(s)
    assert [p.spec.family for p in built][:2] == ["strata", "ollama"]
    strata = built[0]
    assert isinstance(strata, StrataProvider)
    assert strata.model == "strata"               # default served-model name
    assert strata.reasoning_effort == "low"       # default thinking level
    assert strata.spec.strength == P.STRENGTH_STRONG


def test_build_providers_without_strata_url_has_no_strata():
    s = _settings(LLM_MODE="auto", OLLAMA_URL="http://ollama:11434")
    assert "strata" not in [p.spec.family for p in P.build_providers(s)]


def test_settings_reasoning_overrides():
    s = _settings(STRATA_URL="http://x/v1", STRATA_REASONING_EFFORT="none",
                  STRATA_REASONING_BUDGET="256")
    strata = [p for p in P.build_providers(s) if p.spec.family == "strata"][0]
    assert strata.reasoning_effort == "none"
    assert strata.reasoning_budget_tokens == 256


def test_gateway_summary_reflects_vision_from_health(monkeypatch):
    from orderinbox.ai import ModelGateway
    monkeypatch.setattr(P.httpx, "get",
                        lambda url, **kw: FakeResponse(200, {"status": "ok", "images": True}))
    gw = ModelGateway(_settings(), providers=[_strata()])
    assert gw.providers_summary()[0]["vision"] is True


def test_failed_call_marks_strata_down_for_fallback(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(P.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(P.httpx, "get", lambda url, **kw: FakeResponse(200, {"status": "ok"}))

    def boom(url, **kw):
        raise P.httpx.ConnectError("engine died")

    monkeypatch.setattr(P.httpx, "post", boom)
    p = _strata()
    assert p.available()
    with pytest.raises(P.httpx.ConnectError):
        p.complete("s", "u")
    assert p.available() is False                 # router now skips it
    clock["t"] += StrataProvider.RECHECK_SECONDS + 1
    assert p.available() is True                  # back after the re-check


@pytest.mark.parametrize("mode", ["cloud", "off"])
def test_llm_mode_cloud_or_off_excludes_strata(mode):
    s = _settings(LLM_MODE=mode, STRATA_URL="http://strata:8080/v1")
    assert "strata" not in [p.spec.family for p in P.build_providers(s)]


def test_bad_reasoning_effort_falls_back_to_low():
    s = _settings(STRATA_URL="http://x/v1", STRATA_REASONING_EFFORT="off")
    strata = [p for p in P.build_providers(s) if p.spec.family == "strata"][0]
    assert strata.reasoning_effort == "low"
