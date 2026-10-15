from __future__ import annotations

import asyncio
import os
import time

import pytest
from fastapi.testclient import TestClient

from grok_proxy.backends.base import BackendEvent
from grok_proxy.backends.fake import FakeBackend
from grok_proxy.config import Settings
from grok_proxy.main import create_app
from grok_proxy.model_resolve import RuntimeModelRegistry, RuntimeModels


def _models(model: str, *, source: str = "cli") -> RuntimeModels:
    return RuntimeModels(default_model=model, available=[model], source=source)


@pytest.mark.asyncio
async def test_registry_explicit_refresh_updates_snapshot(monkeypatch):
    calls = 0

    def fake_resolve(**_kwargs):
        nonlocal calls
        calls += 1
        return _models("grok-4.6")

    monkeypatch.setattr("grok_proxy.model_resolve.resolve_runtime_models", fake_resolve)
    registry = RuntimeModelRegistry(
        initial=_models("grok-4.5", source="cache"),
        configured_default="auto",
        refresh_interval_sec=1800,
    )

    before = registry.snapshot()
    after = await registry.get(refresh=True)

    assert before.runtime.default_model == "grok-4.5"
    assert after.runtime.default_model == "grok-4.6"
    assert after.runtime.available == ["grok-4.6"]
    assert after.runtime.source == "cli"
    assert after.last_error is None
    assert after.last_success_at is not None
    assert calls == 1


@pytest.mark.asyncio
async def test_registry_get_refreshes_expired_snapshot(monkeypatch):
    calls = 0

    def fake_resolve(**_kwargs):
        nonlocal calls
        calls += 1
        return _models("grok-4.6")

    monkeypatch.setattr("grok_proxy.model_resolve.resolve_runtime_models", fake_resolve)
    registry = RuntimeModelRegistry(
        initial=_models("grok-4.5", source="cache"),
        configured_default="auto",
        refresh_interval_sec=0,
    )

    snapshot = await registry.get()

    assert snapshot.runtime.default_model == "grok-4.6"
    assert calls == 1


@pytest.mark.asyncio
async def test_registry_coalesces_concurrent_refreshes(monkeypatch):
    calls = 0

    def fake_resolve(**_kwargs):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return _models("grok-4.6")

    monkeypatch.setattr("grok_proxy.model_resolve.resolve_runtime_models", fake_resolve)
    registry = RuntimeModelRegistry(
        initial=_models("grok-4.5", source="cache"),
        configured_default="auto",
    )

    first, second = await asyncio.gather(
        registry.refresh(force=True),
        registry.refresh(force=True),
    )

    assert calls == 1
    assert first.runtime.default_model == "grok-4.6"
    assert second.runtime.default_model == "grok-4.6"


@pytest.mark.asyncio
async def test_registry_failure_retains_last_success_and_backs_off(monkeypatch):
    results = iter([_models("grok-4.6"), _models("grok-4.5", source="fallback")])
    calls = 0

    def fake_resolve(**_kwargs):
        nonlocal calls
        calls += 1
        return next(results)

    monkeypatch.setattr("grok_proxy.model_resolve.resolve_runtime_models", fake_resolve)
    registry = RuntimeModelRegistry(
        initial=_models("grok-4.5", source="cache"),
        configured_default="auto",
        refresh_interval_sec=0,
    )

    refreshed = await registry.refresh(force=True)
    failed = await registry.refresh(force=True)
    backed_off = await registry.get()

    assert refreshed.runtime.default_model == "grok-4.6"
    assert failed.runtime.default_model == "grok-4.6"
    assert failed.runtime.source == "cli"
    assert failed.last_error is not None
    assert backed_off.runtime.default_model == "grok-4.6"
    assert backed_off.last_error == failed.last_error
    assert calls == 2


@pytest.mark.asyncio
async def test_registry_refreshes_when_models_cache_changes(monkeypatch, tmp_path):
    """A newer models_cache.json must invalidate the snapshot inside the TTL.

    WorkBuddy calls GET /v1/models without refresh=true. The 2026-10-09
    snapshot omitted grok-4.7 because the cache landed after the last success
    and the 30 minute TTL had not elapsed.
    """
    grok_home = tmp_path / "grokhome"
    grok_home.mkdir()
    cache = grok_home / "models_cache.json"
    cache.write_text('{"models":{}}', encoding="utf-8")
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    calls = 0

    def fake_resolve(**_kwargs):
        nonlocal calls
        calls += 1
        return _models("grok-4.7")

    monkeypatch.setattr("grok_proxy.model_resolve.resolve_runtime_models", fake_resolve)
    registry = RuntimeModelRegistry(
        initial=_models("grok-4.6", source="cli"),
        configured_default="auto",
        refresh_interval_sec=1800,
    )

    first = await registry.get()
    cache.write_text('{"models":{"grok-4.7":{}}}', encoding="utf-8")
    os.utime(cache, (cache.stat().st_atime, cache.stat().st_mtime + 5))
    second = await registry.get()

    assert first.runtime.available == ["grok-4.6"]
    assert calls == 1
    assert second.runtime.default_model == "grok-4.7"
    assert second.runtime.available == ["grok-4.7"]


@pytest.mark.asyncio
async def test_registry_refreshes_when_grok_binary_changes(monkeypatch, tmp_path):
    grok_home = tmp_path / "grokhome"
    grok_home.mkdir()
    (grok_home / "models_cache.json").write_text("{}", encoding="utf-8")
    grok_bin = tmp_path / "grok"
    grok_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    grok_bin.chmod(0o755)
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    calls = 0

    def fake_resolve(**_kwargs):
        nonlocal calls
        calls += 1
        return _models("grok-4.7")

    monkeypatch.setattr("grok_proxy.model_resolve.resolve_runtime_models", fake_resolve)
    registry = RuntimeModelRegistry(
        initial=_models("grok-4.6", source="cli"),
        configured_default="auto",
        grok_bin=str(grok_bin),
        refresh_interval_sec=1800,
    )

    await registry.get()
    grok_bin.write_text("#!/bin/sh\nexit 0\n# updated\n", encoding="utf-8")
    os.utime(grok_bin, (grok_bin.stat().st_atime, grok_bin.stat().st_mtime + 5))
    second = await registry.get()

    assert calls == 1
    assert second.runtime.default_model == "grok-4.7"


@pytest.mark.asyncio
async def test_failed_refresh_keeps_catalog_stamp_dirty(monkeypatch, tmp_path):
    grok_home = tmp_path / "grokhome"
    grok_home.mkdir()
    cache = grok_home / "models_cache.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    calls = 0

    def fake_resolve(**_kwargs):
        nonlocal calls
        calls += 1
        return _models("grok-4.5", source="fallback")

    monkeypatch.setattr("grok_proxy.model_resolve.resolve_runtime_models", fake_resolve)
    registry = RuntimeModelRegistry(
        initial=_models("grok-4.6", source="cli"),
        configured_default="auto",
        refresh_interval_sec=1800,
    )

    await registry.get()
    cache.write_text('{"models":{"grok-4.7":{}}}', encoding="utf-8")
    os.utime(cache, (cache.stat().st_atime, cache.stat().st_mtime + 5))
    failed = await registry.get()
    backed_off = await registry.get()

    assert calls == 1
    assert failed.runtime.default_model == "grok-4.6"
    assert backed_off.runtime.default_model == "grok-4.6"

    registry._retry_after_monotonic = 0.0
    await registry.get()

    assert calls == 2


def test_http_response_uses_refreshed_default_model(tmp_path, monkeypatch):
    results = iter([_models("grok-4.5", source="cache"), _models("grok-4.6")])

    def fake_resolve(**_kwargs):
        return next(results)

    # Startup resolves through bootstrap's binding. The registry refresh
    # resolves through model_resolve. Patch both so the test does not call
    # the live `grok models` command.
    monkeypatch.setattr("grok_proxy.model_resolve.resolve_runtime_models", fake_resolve)
    monkeypatch.setattr("grok_proxy.bootstrap.resolve_runtime_models", fake_resolve)
    settings = Settings(
        api_key="test-secret-key",
        default_cwd=str(tmp_path),
        default_model="auto",
        models="",
        model_refresh_interval_sec=0,
        database_path=str(tmp_path / "chat.db"),
        always_approve=True,
    )
    fake = FakeBackend(
        script=[BackendEvent(type="end", data={"text": "ok", "session_id": "s"})]
    )
    app = create_app(settings, bootstrap=False, backend=fake, database_path=tmp_path / "chat.db")

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-secret-key"},
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "cwd": str(tmp_path),
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["model"] == "grok-4.6"
    assert fake.started[-1].model == "grok-4.6"


def test_http_app_keeps_supplied_registry_when_settings_are_materialized(tmp_path, monkeypatch):
    calls = 0

    def fake_resolve(**_kwargs):
        nonlocal calls
        calls += 1
        return _models("grok-4.6")

    monkeypatch.setattr("grok_proxy.model_resolve.resolve_runtime_models", fake_resolve)
    initial = _models("grok-4.6")
    registry = RuntimeModelRegistry(
        initial=initial,
        configured_default="auto",
        configured_models="",
        refresh_interval_sec=0,
    )
    settings = Settings(
        api_key="test-secret-key",
        default_cwd=str(tmp_path),
        # These values simulate the model ids materialized by bootstrap before
        # an application instance is handed to uvicorn.
        default_model="grok-4.6",
        models="grok-4.6",
        model_refresh_interval_sec=0,
        database_path=str(tmp_path / "chat.db"),
        always_approve=True,
    )
    app = create_app(
        settings,
        bootstrap=False,
        runtime_models=initial,
        model_registry=registry,
        backend=FakeBackend(),
        database_path=tmp_path / "chat.db",
    )

    with TestClient(app) as client:
        response = client.get(
            "/v1/models?refresh=true",
            headers={"Authorization": "Bearer test-secret-key"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["id"] == "grok-4.6"
    assert calls == 1
