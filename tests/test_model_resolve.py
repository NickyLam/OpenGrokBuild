from __future__ import annotations

import json
from pathlib import Path

from grok_proxy.model_resolve import (
    RuntimeModels,
    get_model_capabilities,
    load_models_from_cache,
    load_models_from_cli,
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


def test_cli_subset_keeps_cache_models(monkeypatch, tmp_path: Path):
    """A built-in CLI list must not hide catalog models that exist only in cache.

    grok 1.0.50 prints grok-4.6 and grok-4.5 when the network catalog has not
    been fetched yet. grok-4.7 and grok-4.7-build-fast live only in
    models_cache.json until that fetch succeeds.
    """
    grok_home = tmp_path / "grokhome"
    grok_home.mkdir()
    (grok_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": {
                    "grok-4.7": {"info": {"id": "grok-4.7"}},
                    "grok-4.7-build-fast": {"info": {"id": "grok-4.7-build-fast"}},
                    "grok-4.6": {"info": {"id": "grok-4.6"}},
                    "grok-4.5": {"info": {"id": "grok-4.5"}},
                }
            }
        ),
        encoding="utf-8",
    )
    cli = tmp_path / "grok"
    cli.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'Default model: grok-4.6' '' 'Available models:' "
        "'  * grok-4.6 (default)' '  - grok-4.5'\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    monkeypatch.setenv("GROK_HOME", str(grok_home))

    rt = resolve_runtime_models(grok_bin=str(cli))

    assert rt.source == "cli"
    assert rt.default_model == "grok-4.6"
    assert rt.available == [
        "grok-4.6",
        "grok-4.5",
        "grok-4.7",
        "grok-4.7-build-fast",
    ]


def test_cli_cache_rewrite_is_the_catalog_that_gets_merged(monkeypatch, tmp_path: Path):
    """Read the cache after `grok models` returns. A CLI that rewrites the cache must win."""
    grok_home = tmp_path / "grokhome"
    grok_home.mkdir()
    cache = grok_home / "models_cache.json"
    cache.write_text(
        json.dumps(
            {
                "models": {
                    "grok-4.7": {"info": {"id": "grok-4.7"}},
                    "grok-4.6": {"info": {"id": "grok-4.6"}},
                }
            }
        ),
        encoding="utf-8",
    )
    cli = tmp_path / "grok"
    cli.write_text(
        "#!/bin/sh\n"
        "cat > \"$GROK_HOME/models_cache.json\" <<'EOF'\n"
        '{"models":{"grok-4.6":{"info":{"id":"grok-4.6"}}}}\n'
        "EOF\n"
        "printf '%s\\n' 'Default model: grok-4.6' '  - grok-4.6'\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    monkeypatch.setenv("GROK_HOME", str(grok_home))

    rt = resolve_runtime_models(grok_bin=str(cli))

    assert rt.source == "cli"
    assert rt.default_model == "grok-4.6"
    assert rt.available == ["grok-4.6"]


def test_explicit_model_list_is_not_unioned_with_cache(monkeypatch, tmp_path: Path):
    grok_home = tmp_path / "grokhome"
    grok_home.mkdir()
    (grok_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": {
                    "grok-4.7": {"info": {"id": "grok-4.7"}},
                    "grok-4.5": {"info": {"id": "grok-4.5"}},
                }
            }
        ),
        encoding="utf-8",
    )
    cli = tmp_path / "grok"
    cli.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'Default model: grok-4.6' '  - grok-4.6' '  - grok-4.5'\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    monkeypatch.setenv("GROK_HOME", str(grok_home))

    rt = resolve_runtime_models(
        configured_default="grok-4.5",
        configured_models="grok-4.5",
        grok_bin=str(cli),
    )

    assert rt.source == "config"
    assert rt.default_model == "grok-4.5"
    assert rt.available == ["grok-4.5"]


def test_resolve_runtime_prefers_cli_over_stale_cache(monkeypatch, tmp_path: Path):
    grok_home = tmp_path / "grokhome"
    grok_home.mkdir()
    (grok_home / "models_cache.json").write_text(
        json.dumps({"models": {"grok-4.5": {"info": {"id": "grok-4.5"}}}}),
        encoding="utf-8",
    )
    cli = tmp_path / "grok"
    cli.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'Default model: grok-4.6' '' 'Available models:' "
        "'  * grok-4.6 (default)' '  - grok-4.5'\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    monkeypatch.setenv("GROK_HOME", str(grok_home))

    rt = resolve_runtime_models(grok_bin=str(cli))

    assert rt.source == "cli"
    assert rt.default_model == "grok-4.6"
    assert rt.available == ["grok-4.6", "grok-4.5"]


def test_load_models_from_cli_rejects_nonzero_even_with_model_text(tmp_path: Path):
    cli = tmp_path / "grok"
    cli.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'Default model: grok-9.9' '  - grok-9.9'\n"
        "exit 2\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)

    assert load_models_from_cli(str(cli)) is None


def test_load_models_from_cli_ignores_diagnostic_stderr(tmp_path: Path):
    cli = tmp_path / "grok"
    cli.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'WARN model refresh failed - grok-9.9' >&2\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)

    assert load_models_from_cli(str(cli)) is None


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
