from __future__ import annotations

import json
from pathlib import Path

from grok_proxy.bootstrap import bootstrap_settings
from grok_proxy.config import Settings, clear_settings_cache
from grok_proxy.credentials import (
    API_KEY_PREFIX,
    OPENCODE_PROVIDER_ID,
    PI_AGENT_PROVIDER_ID,
    build_connection_info,
    ensure_api_key,
    format_startup_banner,
    generate_api_key,
    install_opencode_model,
    install_pi_agent_model,
    install_workbuddy_model,
    load_persisted_api_key,
    mask_api_key,
    write_client_config_files,
)


def test_generate_api_key_prefix():
    key = generate_api_key()
    assert key.startswith(API_KEY_PREFIX)
    assert len(key) > len(API_KEY_PREFIX) + 16


def test_mask_api_key():
    key = "sk-gp-abcdefghijklmnopqrstuvwxyz1234"
    masked = mask_api_key(key)
    assert masked != key
    assert masked.startswith("sk-gp-")
    assert masked.endswith(key[-4:])
    assert len(masked) < len(key)
    assert mask_api_key("short") == "[hidden]"


def test_banner_masks_key_by_default():
    key = generate_api_key()
    info = build_connection_info(
        api_key=key, host="127.0.0.1", port=8787, model_id="grok-4.5", source="generated"
    )
    banner = format_startup_banner(info)
    assert key not in banner  # plaintext never printed by default
    assert mask_api_key(key) in banner
    assert "GROK_PROXY_BANNER_SHOW_KEY" in banner

    full = format_startup_banner(info, show_full_key=True)
    assert key in full


def test_ensure_generates_and_persists(tmp_path: Path):
    key1, src1 = ensure_api_key("", state_dir=tmp_path, persist=True)
    assert src1 == "generated"
    assert key1.startswith(API_KEY_PREFIX)
    assert (tmp_path / "api_key").read_text().strip() == key1

    key2, src2 = ensure_api_key("", state_dir=tmp_path, persist=True)
    assert src2 == "file"
    assert key2 == key1


def test_env_key_wins(tmp_path: Path):
    key, src = ensure_api_key("my-fixed-key", state_dir=tmp_path, persist=True)
    assert key == "my-fixed-key"
    assert src == "env"


def test_write_client_config_and_workbuddy_shape(tmp_path: Path):
    info = build_connection_info(
        api_key="sk-gp-test",
        host="127.0.0.1",
        port=8787,
        model_id="grok-4.5",
        source="generated",
    )
    written = write_client_config_files(info, state_dir=tmp_path)
    client = json.loads(written["client_config"].read_text())
    assert client["base_url"] == "http://127.0.0.1:8787/v1"
    assert client["api_key"] == "sk-gp-test"
    assert client["model_id"] == "grok-4.5"
    assert client["supports_images"] is True
    assert client["context_window"] >= 128_000
    assert client["pi_agent"]["contextWindow"] == client["context_window"]
    assert "image" in client["pi_agent"]["input"]
    assert client["opencode"]["limit"]["context"] == client["context_window"]
    assert "image" in client["opencode"]["modalities"]["input"]

    wb = json.loads(written["workbuddy_model"].read_text())
    assert wb["vendor"] == "Custom"
    assert wb["url"] == "http://127.0.0.1:8787/v1"
    assert wb["apiKey"] == "sk-gp-test"
    assert wb["id"] == "grok-4.5"
    assert wb["supportsImages"] is True
    assert wb["supportsReasoning"] is True
    assert wb["maxInputTokens"] == client["context_window"]
    assert "reasoning" in wb
    assert isinstance(wb["reasoning"]["supportedEfforts"], list)
    assert wb["reasoning"]["supportedEfforts"]


def test_install_workbuddy_upsert(tmp_path: Path):
    models = tmp_path / "models.json"
    models.write_text(
        json.dumps(
            [
                {
                    "id": "other",
                    "name": "other",
                    "vendor": "Custom",
                    "url": "http://x/v1",
                    "apiKey": "k",
                }
            ]
        ),
        encoding="utf-8",
    )
    info = build_connection_info(
        api_key="sk-gp-abc",
        host="127.0.0.1",
        port=8787,
        model_id="grok-build",
        source="generated",
    )
    install_workbuddy_model(info, models_path=models)
    data = json.loads(models.read_text())
    assert len(data) == 2
    entry = next(e for e in data if e["id"] == "grok-build")
    assert entry["url"] == "http://127.0.0.1:8787/v1"
    assert entry["apiKey"] == "sk-gp-abc"

    # update in place
    info2 = build_connection_info(
        api_key="sk-gp-new",
        host="127.0.0.1",
        port=8787,
        model_id="grok-build",
        source="generated",
    )
    install_workbuddy_model(info2, models_path=models)
    data2 = json.loads(models.read_text())
    assert len(data2) == 2
    entry2 = next(e for e in data2 if e["id"] == "grok-build")
    assert entry2["apiKey"] == "sk-gp-new"


def test_install_workbuddy_all_discovered_models(tmp_path: Path):
    models = tmp_path / "models.json"
    models.write_text(
        json.dumps(
            [
                {
                    "id": "other",
                    "name": "other",
                    "vendor": "Custom",
                    "url": "http://x/v1",
                    "apiKey": "k",
                },
                {
                    "id": "grok-4.5",
                    "name": "grok-4.5",
                    "vendor": "Custom",
                    "url": "http://127.0.0.1:8787/v1",
                    "apiKey": "old",
                },
            ]
        ),
        encoding="utf-8",
    )
    info = build_connection_info(
        api_key="sk-gp-abc",
        host="127.0.0.1",
        port=8787,
        model_id="grok-4.6",
        source="generated",
    )
    install_workbuddy_model(
        info,
        models_path=models,
        model_ids=["grok-4.6", "grok-4.5"],
        remove_ids=["grok-build", "grok-build-plan"],
    )
    data = json.loads(models.read_text())
    ids = [e["id"] for e in data]
    assert "other" in ids
    assert "grok-4.6" in ids
    assert "grok-4.5" in ids
    assert len(data) == 3
    forty_five = next(e for e in data if e["id"] == "grok-4.5")
    forty_six = next(e for e in data if e["id"] == "grok-4.6")
    assert forty_five["apiKey"] == "sk-gp-abc"
    assert forty_six["apiKey"] == "sk-gp-abc"
    assert forty_five["url"] == forty_six["url"] == "http://127.0.0.1:8787/v1"


def test_install_opencode_merges_without_wiping(tmp_path: Path):
    cfg = tmp_path / "opencode.json"
    cfg.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "model": "anthropic/claude-sonnet-4",
                "provider": {
                    "anthropic": {
                        "npm": "@ai-sdk/anthropic",
                        "name": "Anthropic",
                        "models": {"claude-sonnet-4": {"name": "Claude"}},
                    },
                    "grok-proxy": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "Grok Proxy (local)",
                        "options": {
                            "baseURL": "http://old:1/v1",
                            "apiKey": "old-key",
                        },
                        "models": {
                            "keep-me": {"name": "Keep Me"},
                            "grok-4.5": {"name": "Stale", "extra": "preserve-me"},
                        },
                    },
                },
                "customTop": True,
            }
        ),
        encoding="utf-8",
    )
    info = build_connection_info(
        api_key="sk-gp-oc",
        host="127.0.0.1",
        port=8787,
        model_id="grok-4.5",
        source="generated",
    )
    path = install_opencode_model(info, config_path=cfg)
    assert path == cfg
    data = json.loads(cfg.read_text())

    # Unrelated top-level + providers preserved
    assert data["model"] == "anthropic/claude-sonnet-4"
    assert data["customTop"] is True
    assert "anthropic" in data["provider"]
    assert data["provider"]["anthropic"]["models"]["claude-sonnet-4"]["name"] == "Claude"

    # Our provider updated; sibling model kept; same model merged
    gp = data["provider"][OPENCODE_PROVIDER_ID]
    assert gp["options"]["baseURL"] == "http://127.0.0.1:8787/v1"
    assert gp["options"]["apiKey"] == "sk-gp-oc"
    assert gp["models"]["keep-me"]["name"] == "Keep Me"
    assert gp["models"]["grok-4.5"]["name"].startswith("Grok")
    assert gp["models"]["grok-4.5"]["extra"] == "preserve-me"
    assert "limit" in gp["models"]["grok-4.5"]
    assert "modalities" in gp["models"]["grok-4.5"]

    # Upsert again does not duplicate
    install_opencode_model(info, config_path=cfg)
    data2 = json.loads(cfg.read_text())
    assert set(data2["provider"][OPENCODE_PROVIDER_ID]["models"]) == {"keep-me", "grok-4.5"}


def test_install_opencode_all_discovered_models(tmp_path: Path):
    cfg = tmp_path / "opencode.json"
    info = build_connection_info(
        api_key="sk-gp-oc",
        host="127.0.0.1",
        port=8787,
        model_id="grok-4.6",
        source="generated",
    )
    install_opencode_model(info, config_path=cfg, model_ids=["grok-4.6", "grok-4.5"])
    data = json.loads(cfg.read_text())
    models = data["provider"][OPENCODE_PROVIDER_ID]["models"]
    assert set(models) == {"grok-4.6", "grok-4.5"}
    assert "limit" in models["grok-4.6"]
    assert "limit" in models["grok-4.5"]


def test_install_opencode_creates_file_when_missing(tmp_path: Path):
    cfg = tmp_path / "nested" / "opencode.json"
    info = build_connection_info(
        api_key="sk-gp-new",
        host="127.0.0.1",
        port=8787,
        model_id="grok-4.5",
        source="generated",
    )
    path = install_opencode_model(info, config_path=cfg, create_dirs=True)
    assert path == cfg
    data = json.loads(cfg.read_text())
    assert OPENCODE_PROVIDER_ID in data["provider"]
    assert "grok-4.5" in data["provider"][OPENCODE_PROVIDER_ID]["models"]
    # No forced default model selection
    assert "model" not in data


def test_install_pi_agent_merges_without_wiping(tmp_path: Path):
    models = tmp_path / "models.json"
    models.write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {
                        "baseUrl": "https://api.openai.com/v1",
                        "api": "openai-completions",
                        "apiKey": "sk-other",
                        "models": [{"id": "gpt-4o", "name": "GPT-4o"}],
                    },
                    "grok-proxy": {
                        "baseUrl": "http://old/v1",
                        "api": "openai-completions",
                        "apiKey": "old",
                        "compat": {"supportsDeveloperRole": False, "custom": 1},
                        "models": [
                            {"id": "keep-me", "name": "Keep"},
                            {"id": "grok-4.5", "name": "Stale", "extra": True},
                        ],
                    },
                },
                "meta": {"note": "keep"},
            }
        ),
        encoding="utf-8",
    )
    info = build_connection_info(
        api_key="sk-gp-pi",
        host="127.0.0.1",
        port=8787,
        model_id="grok-4.5",
        source="generated",
    )
    path = install_pi_agent_model(info, models_path=models)
    assert path == models
    data = json.loads(models.read_text())

    assert data["meta"]["note"] == "keep"
    assert data["providers"]["openai"]["models"][0]["id"] == "gpt-4o"

    gp = data["providers"][PI_AGENT_PROVIDER_ID]
    assert gp["baseUrl"] == "http://127.0.0.1:8787/v1"
    assert gp["apiKey"] == "sk-gp-pi"
    assert gp["compat"]["custom"] == 1
    ids = [m["id"] for m in gp["models"]]
    assert ids == ["keep-me", "grok-4.5"]
    updated = next(m for m in gp["models"] if m["id"] == "grok-4.5")
    assert updated["extra"] is True
    assert updated["contextWindow"] >= 128_000
    assert "image" in updated["input"]

    install_pi_agent_model(info, models_path=models)
    data2 = json.loads(models.read_text())
    assert len(data2["providers"][PI_AGENT_PROVIDER_ID]["models"]) == 2


def test_install_pi_agent_all_discovered_models(tmp_path: Path):
    models = tmp_path / "models.json"
    info = build_connection_info(
        api_key="sk-gp-pi",
        host="127.0.0.1",
        port=8787,
        model_id="grok-4.6",
        source="generated",
    )
    install_pi_agent_model(info, models_path=models, model_ids=["grok-4.6", "grok-4.5"])
    data = json.loads(models.read_text())
    ids = [m["id"] for m in data["providers"][PI_AGENT_PROVIDER_ID]["models"]]
    assert ids == ["grok-4.6", "grok-4.5"]


def test_install_pi_agent_creates_file_when_missing(tmp_path: Path):
    models = tmp_path / "agent" / "models.json"
    info = build_connection_info(
        api_key="sk-gp-pi2",
        host="127.0.0.1",
        port=8787,
        model_id="grok-4.5",
        source="generated",
    )
    path = install_pi_agent_model(info, models_path=models, create_dirs=True)
    assert path == models
    data = json.loads(models.read_text())
    assert PI_AGENT_PROVIDER_ID in data["providers"]
    assert data["providers"][PI_AGENT_PROVIDER_ID]["models"][0]["id"] == "grok-4.5"


def test_banner_mentions_client_install_tips():
    key = generate_api_key()
    info = build_connection_info(
        api_key=key, host="127.0.0.1", port=8787, model_id="grok-4.5", source="generated"
    )
    banner = format_startup_banner(info)
    assert "--install-opencode" in banner
    assert "--install-pi-agent" in banner
    assert "--install-workbuddy" in banner

    banner2 = format_startup_banner(
        info,
        opencode_installed=Path("/tmp/opencode.json"),
        pi_agent_installed=Path("/tmp/models.json"),
    )
    assert "Installed into OpenCode" in banner2
    assert "Installed into Pi Agent" in banner2
    assert "--install-opencode" not in banner2


def test_bootstrap_settings(tmp_path: Path, monkeypatch):
    clear_settings_cache()
    monkeypatch.delenv("GROK_PROXY_API_KEY", raising=False)
    # Provide a fake models cache so bootstrap does not depend on live grok CLI
    grok_home = tmp_path / "grokhome"
    grok_home.mkdir()
    (grok_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": {
                    "grok-4.6": {"info": {"id": "grok-4.6"}},
                    "grok-4.5": {"info": {"id": "grok-4.5"}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    s = Settings(api_key="", host="127.0.0.1", port=8787, default_model="auto", models="")
    result = bootstrap_settings(
        s,
        install_workbuddy=False,
        install_opencode=False,
        install_pi_agent=False,
        state_dir=tmp_path,
        print_banner=False,
    )
    assert result.settings.api_key.startswith(API_KEY_PREFIX)
    assert load_persisted_api_key(tmp_path) == result.settings.api_key
    assert (tmp_path / "workbuddy-model.json").is_file()
    assert result.settings.default_model == "grok-4.6"
    assert result.runtime_models.available == ["grok-4.6", "grok-4.5"]
    assert result.opencode_path is None
    assert result.pi_agent_path is None
    wb = json.loads((tmp_path / "workbuddy-model.json").read_text())
    assert wb["id"] == "grok-4.6"
    assert wb["supportsImages"] is True
    assert wb["maxInputTokens"] >= 128_000


def test_bootstrap_installs_all_workbuddy_models(tmp_path: Path, monkeypatch):
    clear_settings_cache()
    monkeypatch.delenv("GROK_PROXY_API_KEY", raising=False)
    grok_home = tmp_path / "grokhome"
    grok_home.mkdir()
    (grok_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": {
                    "grok-4.6": {"info": {"id": "grok-4.6"}},
                    "grok-4.5": {"info": {"id": "grok-4.5"}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    wb_dir = tmp_path / "workbuddy"
    wb_dir.mkdir()
    models_path = wb_dir / "models.json"
    models_path.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(
        "grok_proxy.credentials.workbuddy_models_path",
        lambda: models_path,
    )
    s = Settings(api_key="", host="127.0.0.1", port=8787, default_model="auto", models="")
    result = bootstrap_settings(
        s,
        install_workbuddy=True,
        install_opencode=False,
        install_pi_agent=False,
        state_dir=tmp_path,
        print_banner=False,
    )
    assert result.workbuddy_path == models_path
    data = json.loads(models_path.read_text())
    assert {e["id"] for e in data} == {"grok-4.6", "grok-4.5"}
