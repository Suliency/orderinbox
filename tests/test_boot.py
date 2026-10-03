"""First-boot demo seeding must not block startup.

With a local model on CPU each LLM call can take minutes, so seeding runs in
a background thread: the console is reachable immediately and orders appear
as they finish processing.
"""
import threading

from orderinbox.pipeline import Pipeline
from orderinbox.web.app_context import AppContext


def test_demo_seeding_runs_in_background(env, monkeypatch):
    monkeypatch.setenv("ORDERINBOX_DEMO", "1")
    release = threading.Event()
    original = Pipeline.process_message

    def slow_process(self, path):
        release.wait(timeout=10)
        return original(self, path)

    monkeypatch.setattr(Pipeline, "process_message", slow_process)

    ctx = AppContext()  # must return while processing is still blocked

    assert ctx.seed_thread is not None
    assert ctx.seed_thread.is_alive()
    assert ctx.store.counts_by_status() == {}

    release.set()
    ctx.seed_thread.join(timeout=30)
    assert not ctx.seed_thread.is_alive()
    assert ctx.store.counts_by_status() == {"exception": 3, "ready": 3, "rejected": 1}


def test_no_seeding_without_demo_flag(env, monkeypatch):
    monkeypatch.delenv("ORDERINBOX_DEMO", raising=False)
    ctx = AppContext()
    assert ctx.seed_thread is None
    assert ctx.store.counts_by_status() == {}
