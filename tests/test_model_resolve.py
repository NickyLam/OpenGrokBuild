from __future__ import annotations

import json
from pathlib import Path

from grok_proxy.model_resolve import (
    RuntimeModels,
    get_model_capabilities,
    load_models_from_cache,
    resolve_request_model,
    resolve_runtime_models,
)


def test_load_models_from_cache(tmp_path: Path):
    cache = tmp_path / "models_cache.json"
    cache.write_text(
        json.dumps(
            {
                "models": {
                    "grok-4.5": {"info": {"id": "grok-4.5", "hidden": False}},
                    "hidden-x": {"info": {"id": "hidden-x", "hidden": True}},
                }
            }
        ),
        encoding="utf-8",
    )
    rt = load_models_from_cache(cache)
    assert rt is not None
    assert rt.default_model == "grok-4.5"
    assert "grok-4.5" in rt.available
    assert "hidden-x" not in rt.available


def test_resolve_request_aliases():
    rt = RuntimeModels(default_model="grok-4.5", available=["grok-4.5"], source="test")
    assert resolve_request_model("grok-build", rt) == "grok-4.5"
    assert resolve_request_model("auto", rt) == "grok-4.5"
    assert resolve_request_model(None, rt) == "grok-4.5"
    assert resolve_request_model("grok-4.5", rt) == "grok-4.5"


def test_resolve_runtime_treats_grok_build_as_auto(monkeypatch, tmp_path: Path):
    cache = tmp_path / "models_cache.json"
    cache.write_text(
        json.dumps({"models": {"grok-4.5": {"info": {"id": "grok-4.5"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    # Put cache at GROK_HOME/models_cache.json — load uses _grok_home()/models_cache.json
    rt = resolve_runtime_models(
        configured_default="grok-build",
        configured_models="grok-build",
        grok_bin="grok-that-does-not-exist-xyz",
    )
    assert rt.default_model == "grok-4.5"
    assert "grok-4.5" in rt.available


def test_get_model_capabilities_from_cache(tmp_path: Path, monkeypatch):
    cache = tmp_path / "models_cache.json"
    cache.write_text(
        json.dumps(
            {
                "models": {
                    "grok-4.5": {
                        "info": {
                            "id": "grok-4.5",
                            "name": "Grok 4.5",
                            "context_window": 500_000,
                            "max_completion_tokens": None,
                            "supports_reasoning_effort": True,
                            "reasoning_effort": "high",
                            "reasoning_efforts": [
                                {"id": "high", "value": "high", "default": True},
                                {"id": "medium", "value": "medium"},
                                {"id": "low", "value": "low"},
                            ],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    caps = get_model_capabilities("grok-4.5", cache_path=cache)
    assert caps.context_window == 500_000
    assert caps.supports_images is True
    assert caps.supports_reasoning is True
    assert caps.default_reasoning_effort == "high"
    assert list(caps.reasoning_efforts) == ["high", "medium", "low"]
    assert caps.max_output_tokens > 0
    assert caps.source == "cache"


def test_get_model_capabilities_known_fallback():
    caps = get_model_capabilities("grok-4.5", cache_path=Path("/nonexistent/models_cache.json"))
    assert caps.context_window == 500_000
    assert caps.supports_images is True
    assert caps.supports_reasoning is True


def test_get_model_capabilities_grok_46_known():
    caps = get_model_capabilities("grok-4.6", cache_path=Path("/nonexistent/models_cache.json"))
    assert caps.display_name == "Grok 4.6"
    assert caps.context_window == 500_000
    assert caps.default_reasoning_effort == "high"
    assert "xhigh" not in caps.reasoning_efforts
    assert caps.source == "known"


def test_get_model_capabilities_does_not_default_xhigh(tmp_path: Path):
    cache = tmp_path / "models_cache.json"
    cache.write_text(
        json.dumps(
            {
                "models": {
                    "grok-4.6": {
                        "info": {
                            "id": "grok-4.6",
                            "name": "Grok 4.6",
                            "context_window": 500_000,
                            "supports_reasoning_effort": True,
                            "reasoning_effort": "xhigh",
                            "reasoning_efforts": [
                                {"id": "xhigh", "value": "xhigh", "default": True},
                                {"id": "high", "value": "high"},
                                {"id": "medium", "value": "medium"},
                                {"id": "low", "value": "low"},
                            ],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    caps = get_model_capabilities("grok-4.6", cache_path=cache)
    assert caps.default_reasoning_effort == "high"
    assert "xhigh" in caps.reasoning_efforts
