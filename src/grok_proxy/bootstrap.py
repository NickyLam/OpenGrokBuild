from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from grok_proxy.config import Settings, clear_settings_cache, get_settings
from grok_proxy.credentials import (
    build_connection_info,
    default_state_dir,
    ensure_api_key,
    format_startup_banner,
    install_opencode_model,
    install_pi_agent_model,
    install_workbuddy_model,
    write_client_config_files,
)
from grok_proxy.model_resolve import RuntimeModels, resolve_runtime_models

logger = logging.getLogger("grok_proxy")


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _resolve_install_flag(explicit: bool | None, env_name: str) -> bool:
    if explicit is None:
        return _env_truthy(env_name)
    return explicit


@dataclass
class BootstrapResult:
    settings: Settings
    banner: str
    workbuddy_path: Path | None
    opencode_path: Path | None
    pi_agent_path: Path | None
    runtime_models: RuntimeModels


def bootstrap_settings(
    settings: Settings | None = None,
    *,
    install_workbuddy: bool | None = None,
    install_opencode: bool | None = None,
    install_pi_agent: bool | None = None,
    state_dir: Path | None = None,
    print_banner: bool = True,
) -> BootstrapResult:
    """
    Ensure API key exists (auto-generate if needed), resolve real Grok model ids,
    write client config files, optionally install client model entries.

    Client installs always upsert (merge) — existing unrelated models/providers
    are never wiped.
    """
    state_dir = state_dir or default_state_dir()
    cfg = settings if settings is not None else get_settings()

    key, source = ensure_api_key(cfg.api_key, state_dir=state_dir, persist=True)
    cfg.api_key = key

    runtime = resolve_runtime_models(
        configured_default=cfg.default_model,
        configured_models=cfg.models,
        grok_bin=cfg.grok_bin,
    )
    cfg.default_model = runtime.default_model
    cfg.models = ",".join(runtime.available)

    # Push into env so re-loaded Settings / uvicorn child import see it
    os.environ["GROK_PROXY_API_KEY"] = key
    os.environ["GROK_PROXY_DEFAULT_MODEL"] = cfg.default_model
    os.environ["GROK_PROXY_MODELS"] = cfg.models
    clear_settings_cache()

    model_id = cfg.default_model
    info = build_connection_info(
        api_key=key,
        host=cfg.host,
        port=cfg.port,
        model_id=model_id,
        source=source,
    )
    written = write_client_config_files(info, state_dir=state_dir)
    install_ids = list(runtime.available) or [model_id]

    do_wb = _resolve_install_flag(install_workbuddy, "GROK_PROXY_INSTALL_WORKBUDDY")
    do_oc = _resolve_install_flag(install_opencode, "GROK_PROXY_INSTALL_OPENCODE")
    do_pi = _resolve_install_flag(install_pi_agent, "GROK_PROXY_INSTALL_PI_AGENT")

    wb_path = None
    if do_wb:
        # Remove stale entry that used invalid id "grok-build"
        wb_path = install_workbuddy_model(
            info,
            model_ids=install_ids,
            remove_ids=["grok-build", "grok-build-plan"],
        )

    oc_path = None
    if do_oc:
        oc_path = install_opencode_model(info, model_ids=install_ids)

    pi_path = None
    if do_pi:
        pi_path = install_pi_agent_model(info, model_ids=install_ids)

    banner = format_startup_banner(
        info,
        written=written,
        workbuddy_installed=wb_path,
        opencode_installed=oc_path,
        pi_agent_installed=pi_path,
        show_full_key=cfg.banner_show_key,
    )
    # Append model discovery note
    extra = (
        f"\n  Grok CLI models ({runtime.source}): {', '.join(runtime.available) or '(none)'}\n"
        f"  Using model id: {runtime.default_model}\n"
    )
    banner = banner.replace(
        "  Note: this API Key only guards the local proxy.",
        extra + "  Note: this API Key only guards the local proxy.",
    )

    if print_banner:
        print(banner, flush=True)
        logger.info(
            "proxy ready base_url=%s model=%s models=%s key_source=%s model_source=%s",
            info.base_url,
            info.model_id,
            runtime.available,
            source,
            runtime.source,
        )

    return BootstrapResult(
        settings=cfg,
        banner=banner,
        workbuddy_path=wb_path,
        opencode_path=oc_path,
        pi_agent_path=pi_path,
        runtime_models=runtime,
    )
