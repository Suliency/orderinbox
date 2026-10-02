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

    # --- LLM ------------------------------------------------------------
    llm_mode: str = field(default_factory=lambda: os.environ.get("LLM_MODE", "auto"))  # auto | ollama | cloud | off
    ollama_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b"))
    cloud_base_url: str = field(default_factory=lambda: os.environ.get("LLM_CLOUD_BASE_URL", ""))
    cloud_api_key: str = field(default_factory=lambda: os.environ.get("LLM_CLOUD_API_KEY", ""))
    cloud_model: str = field(default_factory=lambda: os.environ.get("LLM_CLOUD_MODEL", "gpt-4o-mini"))
    llm_temperature: float = field(default_factory=lambda: _float(os.environ.get("LLM_TEMPERATURE"), 0.1))
    llm_timeout: float = field(default_factory=lambda: _float(os.environ.get("LLM_TIMEOUT"), 600.0))

    # --- matching / rules thresholds -------------------------------------
    match_threshold: float = field(default_factory=lambda: _float(os.environ.get("MATCH_THRESHOLD", "80.0")))
    low_match_threshold: float = field(default_factory=lambda: _float(os.environ.get("LOW_MATCH_THRESHOLD", "60.0")))
    price_deviation_tolerance: float = field(default_factory=lambda: _float(os.environ.get("PRICE_DEVIATION_TOLERANCE", "0.15")))
    confidence_auto_ready: float = field(default_factory=lambda: _float(os.environ.get("CONFIDENCE_AUTO_READY", "0.85")))
    allowed_currencies: str = field(default_factory=lambda: os.environ.get("ALLOWED_CURRENCIES", "USD,CAD,EUR"))

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
