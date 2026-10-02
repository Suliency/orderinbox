"""Shared fixtures: a temp home with mock Odoo backend and LLM off
(deterministic + fast)."""
import os
import tempfile

import pytest

from orderinbox.config import Settings
from orderinbox.db import Store
from orderinbox.extract import LLMClient
from orderinbox.odoo.mock import MockOdooBackend
from orderinbox.pipeline import Pipeline


@pytest.fixture()
def env():
    tmp = tempfile.mkdtemp(prefix="oi-test-")
    os.environ["ORDERINBOX_HOME"] = tmp
    os.environ["LLM_MODE"] = "off"
    os.environ["ODOO_MODE"] = "mock"
    for k in ("MATCH_THRESHOLD", "LOW_MATCH_THRESHOLD", "PRICE_DEVIATION_TOLERANCE"):
        os.environ.pop(k, None)
    settings = Settings()
    backend = MockOdooBackend()
    llm = LLMClient(settings)
    store = Store(settings.db_path())
    pipeline = Pipeline(settings, backend, llm, store)
    yield settings, backend, llm, store, pipeline
    os.environ.pop("ORDERINBOX_HOME", None)
    os.environ.pop("LLM_MODE", None)
    os.environ.pop("ODOO_MODE", None)
