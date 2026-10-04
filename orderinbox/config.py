"""Configuration. Everything is environment-driven so the same build works as a
Docker appliance, on a customer's server, or locally in demo mode.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # --- home / storage -------------------------------------------------
    home: Path = field(default_factory=lambda: Path(os.environ.get("ORDERINBOX_HOME", str(Path.home() / ".orderinbox"))))
    data_dir: Path = field(init=False)
    spool_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.home = Path(self.home).expanduser()
        self.data_dir = self.home / "data"
        self.spool_dir = self.home / "spool"
        for d in (self.data_dir, self.spool_dir):
            d.mkdir(parents=True, exist_ok=True)

    # --- odoo -----------------------------------------------------------
    odoo_mode: str = field(default_factory=lambda: os.environ.get("ODOO_MODE", "mock"))  # mock | live
    odoo_url: str = field(default_factory=lambda: os.environ.get("ODOO_URL", ""))
    odoo_db: str = field(default_factory=lambda: os.environ.get("ODOO_DB", ""))
    odoo_username: str = field(default_factory=lambda: os.environ.get("ODOO_USERNAME", ""))
    odoo_password: str = field(default_factory=lambda: os.environ.get("ODOO_PASSWORD", ""))
    # Mock-mode seed catalog (JSON). Empty = built-in demo catalog.
    seed_file: str = field(default_factory=lambda: os.environ.get("SEED_FILE", ""))

    # --- inbox ----------------------------------------------------------
    mail_provider: str = field(default_factory=lambda: os.environ.get("MAIL_PROVIDER", "imap"))  # imap | gmail | none
    imap_host: str = field(default_factory=lambda: os.environ.get("IMAP_HOST", "imap.gmail.com"))
    imap_port: int = field(default_factory=lambda: int(os.environ.get("IMAP_PORT", "993")))
    imap_username: str = field(default_factory=lambda: os.environ.get("IMAP_USERNAME", ""))
    imap_password: str = field(default_factory=lambda: os.environ.get("IMAP_PASSWORD", ""))
    imap_folder: str = field(default_factory=lambda: os.environ.get("IMAP_FOLDER", "INBOX"))
    inbox_lookback_days: int = field(default_factory=lambda: int(os.environ.get("INBOX_LOOKBACK_DAYS", "30")))
    inbox_poll_seconds: int = field(default_factory=lambda: int(os.environ.get("INBOX_POLL_SECONDS", "120")))

    # --- LLM (model gateway) --------------------------------------------
    # LLM_MODE gates which *class* of provider is enabled:
    #   auto   = local providers first, cloud fallback if configured
    #   ollama = local Ollama only
    #   cloud  = cloud / remote providers only
    #   off    = deterministic parsing only (no model calls)
    llm_mode: str = field(default_factory=lambda: os.environ.get("LLM_MODE", "auto"))  # auto | ollama | cloud | off
    # Local backends. Ollama stays the zero-config default; Strata and vLLM
    # are OpenAI-compatible servers (a DGX Spark running the main model, or a
    # second Spark running the verifier/vision/embedding models).
    ollama_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b"))
    strata_url: str = field(default_factory=lambda: os.environ.get("STRATA_URL", ""))
    # Strata (github.com/Niko1221/Strata) accepts any model name; "strata" is
    # what its docs use. Vision is also detected from its /health endpoint.
    strata_model: str = field(default_factory=lambda: os.environ.get("STRATA_MODEL", "strata"))
    strata_api_key: str = field(default_factory=lambda: os.environ.get("STRATA_API_KEY", ""))
    strata_vision: bool = field(default_factory=lambda: _bool(os.environ.get("STRATA_VISION")))
    # Thinking level per request: none | low | medium | high ("" = the model's
    # own default, high). Extraction wants short thinking; "low" keeps quality.
    strata_reasoning_effort: str = field(default_factory=lambda: os.environ.get("STRATA_REASONING_EFFORT", "low").strip().lower())
    # Optional hard cap on thinking tokens (0 = no cap).
    strata_reasoning_budget: int = field(default_factory=lambda: int(_float(os.environ.get("STRATA_REASONING_BUDGET"), 0)))
    vllm_url: str = field(default_factory=lambda: os.environ.get("VLLM_URL", ""))
    vllm_model: str = field(default_factory=lambda: os.environ.get("VLLM_MODEL", ""))
    vllm_api_key: str = field(default_factory=lambda: os.environ.get("VLLM_API_KEY", ""))
    vllm_vision: bool = field(default_factory=lambda: _bool(os.environ.get("VLLM_VISION")))
    # Cloud backends (any OpenAI-compatible endpoint, plus Anthropic / Gemini).
    cloud_base_url: str = field(default_factory=lambda: os.environ.get("LLM_CLOUD_BASE_URL", ""))
    cloud_api_key: str = field(default_factory=lambda: os.environ.get("LLM_CLOUD_API_KEY", ""))
    cloud_model: str = field(default_factory=lambda: os.environ.get("LLM_CLOUD_MODEL", "gpt-4o-mini"))
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"))
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-1.5-pro"))
    llm_temperature: float = field(default_factory=lambda: _float(os.environ.get("LLM_TEMPERATURE"), 0.1))
    llm_timeout: float = field(default_factory=lambda: _float(os.environ.get("LLM_TIMEOUT"), 600.0))
    # Confidence-tier escalation cut-points (proposal Section 10).
    escalate_auto: float = field(default_factory=lambda: _float(os.environ.get("ESCALATE_AUTO", "0.95")))
    escalate_verify: float = field(default_factory=lambda: _float(os.environ.get("ESCALATE_VERIFY", "0.80")))
    escalate_cloud: float = field(default_factory=lambda: _float(os.environ.get("ESCALATE_CLOUD", "0.60")))
    # When the producer and verifier disagree, or confidence lands in the
    # cloud band, optionally force a cloud pass before a human sees it.
    use_producer_verifier: bool = field(default_factory=lambda: _bool(os.environ.get("USE_PRODUCER_VERIFIER"), True))
    use_cloud_escalation: bool = field(default_factory=lambda: _bool(os.environ.get("USE_CLOUD_ESCALATION"), True))

    # --- matching / rules thresholds -------------------------------------
    match_threshold: float = field(default_factory=lambda: _float(os.environ.get("MATCH_THRESHOLD", "80.0")))
    low_match_threshold: float = field(default_factory=lambda: _float(os.environ.get("LOW_MATCH_THRESHOLD", "60.0")))
    price_deviation_tolerance: float = field(default_factory=lambda: _float(os.environ.get("PRICE_DEVIATION_TOLERANCE", "0.15")))
    confidence_auto_ready: float = field(default_factory=lambda: _float(os.environ.get("CONFIDENCE_AUTO_READY", "0.85")))
    allowed_currencies: str = field(default_factory=lambda: os.environ.get("ALLOWED_CURRENCIES", "USD,CAD,EUR"))
    # Freight (RateScout) domain
    freight_margin_threshold_pct: float = field(default_factory=lambda: _float(os.environ.get("FREIGHT_MARGIN_THRESHOLD", "10.0")))

    # --- web ------------------------------------------------------------
    web_host: str = field(default_factory=lambda: os.environ.get("WEB_HOST", "0.0.0.0"))
    web_port: int = field(default_factory=lambda: int(os.environ.get("WEB_PORT", "8501")))
    web_password: str = field(default_factory=lambda: os.environ.get("WEB_PASSWORD", "orderinbox"))
    # Token for the /api/* bridge (used by the Odoo Apps Store module to
    # pull orders and to trigger approvals). Empty = API disabled.
    api_token: str = field(default_factory=lambda: os.environ.get("ORDERINBOX_API_TOKEN", ""))

    # --- misc -----------------------------------------------------------
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))

    def db_path(self) -> Path:
        return self.data_dir / "orderinbox.db"

    def odoo_backend(self) -> str:
        return "mock" if self.odoo_mode == "mock" else "live"

    def currencies(self) -> list[str]:
        return [c.strip().upper() for c in self.allowed_currencies.split(",") if c.strip()]


def get_settings() -> Settings:
    return Settings()
