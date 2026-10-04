"""Model gateway — provider abstraction.

From the integration proposal:

    Do not hard-code RateScout around Strata.

    LLMProvider
      ├── StrataProvider     (local — the Strata server, github.com/Niko1221/Strata)
      ├── VLLMProvider       (local — any OpenAI-compatible inference server)
      ├── OpenAIProvider     (cloud — also covers OpenRouter/DeepSeek/Together)
      ├── AnthropicProvider  (cloud — Messages API)
      └── GeminiProvider     (cloud — generative language API)

    Section 20 adds: a provider abstraction so any inference layer can be
    swapped without touching the workflow.

Each provider speaks one wire protocol and reports enough metadata for the
ModelRouter to route on privacy (kind) and capability (strength / vision).
Nothing here does business logic — it turns (system, user) into a text or a
JSON object. The router and the escalation policy own the *decisions*.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Optional

import httpx

log = logging.getLogger("orderinbox.ai.providers")


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# capability model
# --------------------------------------------------------------------------

#: Relative capability tiers used for complexity routing and for the
#: producer/verifier split (Model A = fast tier, Model B = strong tier).
STRENGTH_FAST = 1
STRENGTH_MEDIUM = 2
STRENGTH_STRONG = 3


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of a provider — what it is and where it runs."""
    family: str            # strata | vllm | ollama | openai | anthropic | gemini
    kind: str              # "local" | "cloud"
    strength: int = STRENGTH_MEDIUM
    vision: bool = False   # can it read scanned / image-based documents?
    label: str = ""

    @property
    def is_local(self) -> bool:
        return self.kind == "local"


# --------------------------------------------------------------------------
# wire protocols
# --------------------------------------------------------------------------

def parse_json_loose(raw: str) -> dict[str, Any]:
    """Parse JSON that an LLM may have wrapped in fences or commentary."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise LLMError(f"could not parse LLM JSON: {raw[:200]}")


class OpenAICompatibleProvider:
    """Any endpoint that speaks the OpenAI chat-completions protocol.

    This covers vLLM, Strata (its local AI API is OpenAI-compatible), Ollama's
    OpenAI shim, OpenRouter, DeepSeek, Groq, LM Studio, etc. One class, many
    deployments — exactly the "don't hard-code around one backend" rule.
    """

    def __init__(self, spec: ProviderSpec, base_url: str, model: str,
                 api_key: str = "", temperature: float = 0.1,
                 timeout: float = 300.0):
        self.spec = spec
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self._healthy: Optional[bool] = None

    def available(self) -> bool:
        if not self.base_url:
            return False
        if not self.spec.is_local:
            return bool(self.api_key)
        # Local server: a reachability check (cached) keeps a configured-but-
        # down server from blocking the local-first router's cloud fallback.
        if self._healthy is None:
            self._healthy = self.ping()
        return self._healthy

    def ping(self) -> bool:
        """Lenient liveness probe: any HTTP response means the server is up."""
        try:
            httpx.get(self.base_url, timeout=5)
            return True
        except Exception:
            return False

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _payload(self, system: str, user: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
        }

    def complete(self, system: str, user: str) -> str:
        url = f"{self.base_url}/chat/completions"
        r = httpx.post(url, json=self._payload(system, user), headers=self._headers(),
                       timeout=self.timeout)
        if r.status_code != 200:
            raise LLMError(f"{self.spec.family} HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"]

    def extract_json(self, system: str, user: str) -> dict[str, Any]:
        return parse_json_loose(self.complete(system, user))


class StrataProvider(OpenAICompatibleProvider):
    """The Strata local model server (https://github.com/Niko1221/Strata).

    Strata runs Qwen3.8-Flash-Next (a 125B mixture-of-experts model) on one or
    two consumer GPUs and serves it over an OpenAI-compatible API, so the
    request itself is the parent's. What is Strata-specific:

    - readiness comes from ``GET /health`` at the server root. The port only
      opens once the model is loaded (minutes; hours on a first download), so
      a server seen down is re-checked every RECHECK_SECONDS instead of being
      written off until the app restarts.
    - ``/health`` says whether the image encoder is loaded; the spec's vision
      flag follows it, so scanned quotes can be routed to Strata.
    - thinking is on by default (level "high") and is the slow part of an
      answer. ``reasoning_effort`` and the optional hard
      ``reasoning_budget_tokens`` keep extraction calls short.
    """

    RECHECK_SECONDS = 30.0
    REASONING_EFFORTS = ("", "none", "low", "medium", "high")

    def __init__(self, spec: ProviderSpec, base_url: str, model: str = "strata",
                 api_key: str = "", temperature: float = 0.1, timeout: float = 600.0,
                 reasoning_effort: str = "low", reasoning_budget_tokens: int = 0):
        if reasoning_effort not in self.REASONING_EFFORTS:
            raise ValueError(f"reasoning_effort must be one of {self.REASONING_EFFORTS[1:]}, "
                             f"got {reasoning_effort!r}")
        super().__init__(spec, base_url, model or "strata", api_key=api_key,
                         temperature=temperature, timeout=timeout)
        self.reasoning_effort = reasoning_effort
        self.reasoning_budget_tokens = max(0, int(reasoning_budget_tokens or 0))
        self._checked_at = 0.0

    @property
    def root_url(self) -> str:
        return re.sub(r"/v1$", "", self.base_url)

    def available(self) -> bool:
        if not self.base_url:
            return False
        stale = (self._healthy is False
                 and time.monotonic() - self._checked_at >= self.RECHECK_SECONDS)
        if self._healthy is None or stale:
            self._healthy = self.ping()
            self._checked_at = time.monotonic()
        return self._healthy

    def ping(self) -> bool:
        try:
            r = httpx.get(f"{self.root_url}/health", headers=self._headers(), timeout=5)
        except Exception:
            return False
        if r.status_code != 200:
            # a refusal (403 Host check, 401 key) is a config error, not a
            # model still loading: say why, or the app just looks model-less
            log.warning("Strata at %s answered /health with %s: %s",
                        self.root_url, r.status_code, (r.text or "")[:300])
            return False
        try:
            images = bool(r.json().get("images"))
        except Exception:
            images = False
        if images and not self.spec.vision:
            self.spec = replace(self.spec, vision=True)
        return True

    def complete(self, system: str, user: str) -> str:
        # A failed call marks the server down until the next health re-check,
        # so a crashed / reloading Strata hands the next requests to the
        # fallback model instead of timing out on each one.
        try:
            return super().complete(system, user)
        except (httpx.HTTPError, LLMError):
            self._healthy = False
            self._checked_at = time.monotonic()
            raise

    def _payload(self, system: str, user: str) -> dict[str, Any]:
        payload = super()._payload(system, user)
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if self.reasoning_budget_tokens:
            payload["reasoning_budget_tokens"] = self.reasoning_budget_tokens
        return payload


class OllamaProvider:
    """Local Ollama (native /api/chat). Kept as the zero-dependency local
    default so the two-minute demo keeps working with a small model."""

    def __init__(self, spec: ProviderSpec, url: str, model: str,
                 temperature: float = 0.1, timeout: float = 300.0):
        self.spec = spec
        self.url = (url or "").rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self._healthy: Optional[bool] = None

    def available(self) -> bool:
        """Reachability check (cached). A configured-but-down Ollama must not
        block the local-first router from falling back to a cloud provider."""
        if not self.url:
            return False
        if self._healthy is None:
            self._healthy = self.ping()
        return self._healthy

    def ping(self) -> bool:
        try:
            r = httpx.get(f"{self.url}/api/tags", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def complete(self, system: str, user: str) -> str:
        r = httpx.post(
            f"{self.url}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "options": {"temperature": self.temperature},
                "format": "json",
            },
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise LLMError(f"ollama HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["message"]["content"]

    def extract_json(self, system: str, user: str) -> dict[str, Any]:
        return parse_json_loose(self.complete(system, user))


class AnthropicProvider:
    """Cloud Anthropic (Messages API)."""

    def __init__(self, spec: ProviderSpec, api_key: str, model: str = "claude-3-5-sonnet-latest",
                 base_url: str = "https://api.anthropic.com/v1",
                 temperature: float = 0.1, timeout: float = 300.0):
        self.spec = spec
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        self.temperature = temperature
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str) -> str:
        r = httpx.post(
            f"{self.base_url}/messages",
            json={
                "model": self.model,
                "max_tokens": 4096,
                "temperature": self.temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise LLMError(f"anthropic HTTP {r.status_code}: {r.text[:200]}")
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    def extract_json(self, system: str, user: str) -> dict[str, Any]:
        return parse_json_loose(self.complete(system, user))


class GeminiProvider:
    """Cloud Google Gemini (generative language API)."""

    def __init__(self, spec: ProviderSpec, api_key: str, model: str = "gemini-1.5-pro",
                 base_url: str = "https://generativelanguage.googleapis.com/v1beta",
                 temperature: float = 0.1, timeout: float = 300.0):
        self.spec = spec
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.temperature = temperature
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str) -> str:
        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"
        r = httpx.post(
            url,
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": self.temperature,
                    "responseMimeType": "application/json",
                },
            },
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise LLMError(f"gemini HTTP {r.status_code}: {r.text[:200]}")
        cand = r.json().get("candidates", [{}])[0]
        parts = cand.get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    def extract_json(self, system: str, user: str) -> dict[str, Any]:
        return parse_json_loose(self.complete(system, user))


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------

def build_providers(settings) -> list:
    """Build the concrete provider set from Settings.

    Local-first: local providers (Strata / Ollama / vLLM) are returned before
    cloud ones (OpenAI / Anthropic / Gemini). The router relies on this order
    but never assumes it.
    """
    s = settings
    out: list = []

    # --- local -----------------------------------------------------------
    # Strata is the main local reasoning model (Qwen3.8-Flash-Next, 125B MoE)
    # and comes first, so local-first routing gives it the volume; the small
    # Ollama model then serves as fallback and as the second (verifier) model.
    if s.strata_url and s.llm_mode not in ("cloud", "off"):
        effort = s.strata_reasoning_effort
        if effort not in StrataProvider.REASONING_EFFORTS:
            log.warning("STRATA_REASONING_EFFORT=%r is not one of none|low|medium|high; "
                        "using 'low'", effort)
            effort = "low"
        out.append(StrataProvider(
            ProviderSpec("strata", "local", STRENGTH_STRONG, vision=bool(s.strata_vision),
                         label=f"strata:{s.strata_model or 'strata'}"),
            s.strata_url, s.strata_model,
            api_key=s.strata_api_key, temperature=s.llm_temperature, timeout=s.llm_timeout,
            reasoning_effort=effort,
            reasoning_budget_tokens=s.strata_reasoning_budget))
    # Ollama remains the zero-config local default.
    if s.llm_mode in ("auto", "ollama") and s.ollama_url:
        out.append(OllamaProvider(
            ProviderSpec("ollama", "local", STRENGTH_FAST, vision=False,
                         label=f"ollama:{s.ollama_model}"),
            s.ollama_url, s.ollama_model,
            temperature=s.llm_temperature, timeout=s.llm_timeout))
    # vLLM local server.
    if s.vllm_url:
        out.append(OpenAICompatibleProvider(
            ProviderSpec("vllm", "local", STRENGTH_STRONG, vision=bool(s.vllm_vision),
                         label=f"vllm:{s.vllm_model}"),
            s.vllm_url, s.vllm_model,
            api_key=s.vllm_api_key, temperature=s.llm_temperature, timeout=s.llm_timeout))

    # --- cloud ------------------------------------------------------------
    if s.llm_mode in ("auto", "cloud") and s.cloud_base_url and s.cloud_api_key:
        out.append(OpenAICompatibleProvider(
            ProviderSpec("openai", "cloud", STRENGTH_STRONG, vision=False,
                         label=f"openai:{s.cloud_model}"),
            s.cloud_base_url, s.cloud_model, api_key=s.cloud_api_key,
            temperature=s.llm_temperature, timeout=s.llm_timeout))
    if s.anthropic_api_key:
        out.append(AnthropicProvider(
            ProviderSpec("anthropic", "cloud", STRENGTH_STRONG, label="anthropic"),
            s.anthropic_api_key, s.anthropic_model))
    if s.gemini_api_key:
        out.append(GeminiProvider(
            ProviderSpec("gemini", "cloud", STRENGTH_STRONG, label="gemini"),
            s.gemini_api_key, s.gemini_model))

    return out
