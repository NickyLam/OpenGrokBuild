from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from grok_proxy.model_resolve import ModelCapabilities, get_model_capabilities

logger = logging.getLogger("grok_proxy")

# Prefix helps humans recognize this is a local proxy key, not an xAI key.
API_KEY_PREFIX = "sk-gp-"


def default_state_dir() -> Path:
    override = os.environ.get("GROK_PROXY_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".grok-proxy"


@dataclass
class StoredCredentials:
    api_key: str
    host: str
    port: int
    model_id: str
    base_url: str
    generated_at: str
    source: str  # "env" | "generated" | "file"


def _key_file(state_dir: Path) -> Path:
    return state_dir / "api_key"


def _credentials_json(state_dir: Path) -> Path:
    return state_dir / "credentials.json"


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except OSError as exc:
        logger.warning("could not restrict permissions on %s: %s", path, exc)


def mask_api_key(key: str) -> str:
    """Shorten a key for display: keep prefix + last 4 chars."""
    if len(key) <= 14:
        return "[hidden]"
    return f"{key[:10]}…{key[-4:]}"


def workbuddy_model_entry(
    *,
    api_key: str,
    base_url: str,
    model_id: str,
    caps: ModelCapabilities | None = None,
) -> dict:
    """Model entry shape used by ~/.workbuddy/models.json (single source of truth)."""
    caps = caps or get_model_capabilities(model_id)
    entry: dict = {
        "id": model_id,
        "name": caps.display_name if caps.display_name != model_id else model_id,
        "vendor": "Custom",
        "url": base_url,
        "apiKey": api_key,
        "supportsToolCall": caps.supports_tool_call,
        "supportsImages": caps.supports_images,
        "supportsReasoning": caps.supports_reasoning,
        "useCustomProtocol": False,
        # WorkBuddy uses maxInputTokens as the context window for UI / budgeting.
        "maxInputTokens": caps.context_window,
    }
    if caps.supports_reasoning and caps.reasoning_efforts:
        entry["reasoning"] = {
            "defaultEffort": caps.default_reasoning_effort or caps.reasoning_efforts[0],
            "supportedEfforts": list(caps.reasoning_efforts),
        }
    return entry


def generic_client_config(
    *,
    api_key: str,
    base_url: str,
    model_id: str,
    caps: ModelCapabilities | None = None,
) -> dict:
    """OpenAI-compatible + per-client fragments (Pi Agent, OpenCode, …)."""
    caps = caps or get_model_capabilities(model_id)
    input_modalities = ["text"]
    if caps.supports_images:
        input_modalities.append("image")
    return {
        "provider": "openai-compatible",
        "base_url": base_url,
        "api_key": api_key,
        "model": model_id,
        "model_id": model_id,
        "name": caps.display_name,
        "context_window": caps.context_window,
        "max_output_tokens": caps.max_output_tokens,
        "supports_images": caps.supports_images,
        "supports_reasoning": caps.supports_reasoning,
        "supports_tool_call": caps.supports_tool_call,
        "reasoning_efforts": list(caps.reasoning_efforts),
        "default_reasoning_effort": caps.default_reasoning_effort,
        "notes": (
            "Use with OpenAI SDKs / WorkBuddy Custom models / OpenCode / Pi Agent. "
            "Grok auth uses local `grok login` or XAI_API_KEY; this api_key is proxy-only. "
            "context_window and supports_images are taken from Grok models cache when available."
        ),
        # Ready-to-paste fragments for popular clients
        "pi_agent": {
            "id": model_id,
            "name": caps.display_name,
            "reasoning": caps.supports_reasoning,
            "input": input_modalities,
            "contextWindow": caps.context_window,
            "maxTokens": caps.max_output_tokens,
        },
        "opencode": {
            "name": f"{caps.display_name} (Grok Build CLI)",
            "limit": {
                "context": caps.context_window,
                "output": caps.max_output_tokens,
            },
            "modalities": {
                "input": input_modalities,
                "output": ["text"],
            },
        },
    }


def load_persisted_api_key(state_dir: Path | None = None) -> str | None:
    state_dir = state_dir or default_state_dir()
    key_path = _key_file(state_dir)
    if key_path.is_file():
        text = key_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    cred_path = _credentials_json(state_dir)
    if cred_path.is_file():
        try:
            data = json.loads(cred_path.read_text(encoding="utf-8"))
            key = str(data.get("api_key") or "").strip()
            if key:
                return key
        except (json.JSONDecodeError, OSError):
            pass
    return None


def persist_api_key(api_key: str, state_dir: Path | None = None) -> Path:
    state_dir = state_dir or default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        state_dir.chmod(stat.S_IRWXU)  # 0o700
    except OSError:
        pass
    key_path = _key_file(state_dir)
    key_path.write_text(api_key + "\n", encoding="utf-8")
    _chmod_private(key_path)
    return key_path


def ensure_api_key(
    configured: str,
    *,
    state_dir: Path | None = None,
    persist: bool = True,
) -> tuple[str, str]:
    """
    Resolve API key: env/config > persisted file > generate new.

    Returns:
        (api_key, source) where source is env|file|generated
    """
    state_dir = state_dir or default_state_dir()
    configured = (configured or "").strip()
    if configured:
        if persist:
            # Keep file in sync when user supplies env key (optional convenience)
            pass
        return configured, "env"

    existing = load_persisted_api_key(state_dir)
    if existing:
        return existing, "file"

    key = generate_api_key()
    if persist:
        persist_api_key(key, state_dir)
        logger.info("Generated new API key and saved to %s", _key_file(state_dir))
    return key, "generated"


def build_base_url(host: str, port: int, *, public_host: str | None = None) -> str:
    """Client-facing base URL (always http for local proxy)."""
    h = public_host or host
    if h in ("0.0.0.0", "::"):
        h = "127.0.0.1"
    return f"http://{h}:{port}/v1"


def build_connection_info(
    *,
    api_key: str,
    host: str,
    port: int,
    model_id: str,
    source: str,
    public_host: str | None = None,
) -> StoredCredentials:
    base_url = build_base_url(host, port, public_host=public_host)
    return StoredCredentials(
        api_key=api_key,
        host=host,
        port=port,
        model_id=model_id,
        base_url=base_url,
        generated_at=datetime.now(UTC).isoformat(),
        source=source,
    )


def write_client_config_files(
    info: StoredCredentials,
    state_dir: Path | None = None,
) -> dict[str, Path]:
    """Write credentials.json, client-config.json, workbuddy-model.json for easy copy."""
    state_dir = state_dir or default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}

    cred_path = _credentials_json(state_dir)
    cred_path.write_text(
        json.dumps(asdict(info), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _chmod_private(cred_path)
    written["credentials"] = cred_path

    caps = get_model_capabilities(info.model_id)

    # Generic OpenAI-compatible client config (any agent) + Pi/OpenCode fragments
    client = generic_client_config(
        api_key=info.api_key,
        base_url=info.base_url,
        model_id=info.model_id,
        caps=caps,
    )
    client_path = state_dir / "client-config.json"
    client_path.write_text(json.dumps(client, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _chmod_private(client_path)
    written["client_config"] = client_path

    # WorkBuddy models.json entry shape (matches ~/.workbuddy/models.json)
    workbuddy_entry = workbuddy_model_entry(
        api_key=info.api_key,
        base_url=info.base_url,
        model_id=info.model_id,
        caps=caps,
    )
    wb_path = state_dir / "workbuddy-model.json"
    wb_path.write_text(
        json.dumps(workbuddy_entry, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _chmod_private(wb_path)
    written["workbuddy_model"] = wb_path

    # Keep plain api_key file too
    written["api_key"] = persist_api_key(info.api_key, state_dir)

    return written


# Client config provider ids (stable keys we own inside third-party configs)
OPENCODE_PROVIDER_ID = "grok-proxy"
PI_AGENT_PROVIDER_ID = "grok-proxy"


def workbuddy_models_path() -> Path:
    return Path.home() / ".workbuddy" / "models.json"


def opencode_config_path() -> Path:
    """User-level OpenCode config (project opencode.json is not auto-touched)."""
    return Path.home() / ".config" / "opencode" / "opencode.json"


def pi_agent_models_path() -> Path:
    return Path.home() / ".pi" / "agent" / "models.json"


def _backup_json(path: Path) -> None:
    if not path.is_file():
        return
    bak = path.with_suffix(path.suffix + ".bak") if path.suffix else path.with_name(path.name + ".bak")
    # Prefer .json.bak for *.json (matches WorkBuddy convention)
    if path.suffix == ".json":
        bak = path.with_suffix(".json.bak")
    try:
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not backup %s: %s", path, exc)


def _ensure_parent_dir(path: Path, *, label: str, create: bool) -> bool:
    """Return True if parent is ready for writing."""
    if path.parent.is_dir():
        return True
    if not create:
        logger.warning(
            "%s config dir not found (%s); skip auto-install.",
            label,
            path.parent,
        )
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return True
    except OSError as exc:
        logger.warning("could not create %s dir %s: %s", label, path.parent, exc)
        return False


def _load_json_file(path: Path) -> object | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Could not parse %s; will rewrite with merge of empty base", path)
        return None
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None


def _normalize_install_model_ids(
    info: StoredCredentials,
    model_ids: list[str] | None,
) -> list[str]:
    """Default model first, then the rest of the discovered list (deduped)."""
    wanted: list[str] = []
    for mid in model_ids or []:
        mid = (mid or "").strip()
        if mid and mid not in wanted:
            wanted.append(mid)
    default = (info.model_id or "").strip()
    if default and default not in wanted:
        wanted.insert(0, default)
    elif default and default in wanted:
        wanted = [default] + [m for m in wanted if m != default]
    return wanted


def _upsert_workbuddy_entry(entries: list[dict], entry: dict) -> str:
    mid = entry.get("id")
    for i, existing in enumerate(entries):
        if existing.get("id") == mid or existing.get("name") == mid:
            entries[i] = {**existing, **entry}
            return "Updated"
    entries.append(entry)
    return "Added"


def install_workbuddy_model(
    info: StoredCredentials,
    *,
    models_path: Path | None = None,
    remove_ids: list[str] | None = None,
    model_ids: list[str] | None = None,
) -> Path | None:
    """
    Upsert this proxy's Custom model(s) in WorkBuddy models.json.
    Returns path written, or None if WorkBuddy dir does not exist.

    model_ids: all Grok ids to advertise (default: ``info.model_id`` only).
    remove_ids: drop stale entries (e.g. old invalid "grok-build" id).
    Existing unrelated models are always preserved. Entries that share this
    proxy URL but have a different id are kept (so 4.5 and 4.6 can coexist).
    """
    path = models_path or workbuddy_models_path()
    if not path.parent.is_dir():
        logger.warning(
            "WorkBuddy config dir not found (%s); skip auto-install. "
            "Copy %s manually.",
            path.parent,
            default_state_dir() / "workbuddy-model.json",
        )
        return None

    entries: list[dict] = []
    raw = _load_json_file(path)
    if isinstance(raw, list):
        entries = [e for e in raw if isinstance(e, dict)]

    drop = set(remove_ids or [])
    if drop:
        entries = [
            e
            for e in entries
            if e.get("id") not in drop and e.get("name") not in drop
        ]

    wanted = _normalize_install_model_ids(info, model_ids)
    actions: list[str] = []
    for mid in wanted:
        entry = workbuddy_model_entry(
            api_key=info.api_key, base_url=info.base_url, model_id=mid
        )
        actions.append(f"{_upsert_workbuddy_entry(entries, entry)} {mid}")

    _backup_json(path)
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _chmod_private(path)
    logger.info("WorkBuddy models in %s: %s", path, ", ".join(actions) or "(none)")
    return path


def install_opencode_model(
    info: StoredCredentials,
    *,
    config_path: Path | None = None,
    provider_id: str = OPENCODE_PROVIDER_ID,
    create_dirs: bool = True,
    model_ids: list[str] | None = None,
) -> Path | None:
    """
    Upsert provider ``grok-proxy`` + model(s) into OpenCode user config.

    Merges into existing ``opencode.json``: other providers, models, and top-level
    keys (including ``model`` default selection) are preserved. Only our
    provider entry and the given model ids are inserted/updated.
    """
    path = config_path or opencode_config_path()
    if not _ensure_parent_dir(path, label="OpenCode", create=create_dirs):
        return None

    data: dict = {}
    raw = _load_json_file(path)
    if isinstance(raw, dict):
        data = dict(raw)
    elif raw is not None:
        logger.warning("%s root is not an object; starting from empty object", path)

    providers = data.get("provider")
    if not isinstance(providers, dict):
        providers = {}
    else:
        providers = dict(providers)

    existing = providers.get(provider_id)
    if not isinstance(existing, dict):
        existing = {}
    else:
        existing = dict(existing)

    models = existing.get("models")
    if not isinstance(models, dict):
        models = {}
    else:
        models = dict(models)

    wanted = _normalize_install_model_ids(info, model_ids)
    actions: list[str] = []
    for mid in wanted:
        caps = get_model_capabilities(mid)
        fragment = generic_client_config(
            api_key=info.api_key,
            base_url=info.base_url,
            model_id=mid,
            caps=caps,
        )["opencode"]
        model_entry = {
            "name": fragment["name"],
            "limit": fragment["limit"],
            "modalities": fragment["modalities"],
        }
        prev = models.get(mid)
        if isinstance(prev, dict):
            models[mid] = {**prev, **model_entry}
            actions.append(f"Updated {mid}")
        else:
            models[mid] = model_entry
            actions.append(f"Added {mid}")

    options = existing.get("options")
    if not isinstance(options, dict):
        options = {}
    else:
        options = dict(options)
    options["baseURL"] = info.base_url
    options["apiKey"] = info.api_key

    providers[provider_id] = {
        **existing,
        "npm": existing.get("npm") or "@ai-sdk/openai-compatible",
        "name": existing.get("name") or "Grok Proxy (local)",
        "options": options,
        "models": models,
    }
    data["provider"] = providers
    if "$schema" not in data:
        data["$schema"] = "https://opencode.ai/config.json"
    # Do not overwrite data["model"] — keep the user's selected default.

    _backup_json(path)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _chmod_private(path)
    logger.info(
        "OpenCode models %s/* in %s: %s",
        provider_id,
        path,
        ", ".join(actions) or "(none)",
    )
    return path


def install_pi_agent_model(
    info: StoredCredentials,
    *,
    models_path: Path | None = None,
    provider_id: str = PI_AGENT_PROVIDER_ID,
    create_dirs: bool = True,
    model_ids: list[str] | None = None,
) -> Path | None:
    """
    Upsert provider ``grok-proxy`` + model(s) into Pi Agent models.json.

    Merges into existing config: other providers and models are preserved.
    Only our provider fields and models with matching ``id`` are upserted.
    """
    path = models_path or pi_agent_models_path()
    if not _ensure_parent_dir(path, label="Pi Agent", create=create_dirs):
        return None

    data: dict = {}
    raw = _load_json_file(path)
    if isinstance(raw, dict):
        data = dict(raw)
    elif raw is not None:
        logger.warning("%s root is not an object; starting from empty object", path)

    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    else:
        providers = dict(providers)

    existing = providers.get(provider_id)
    if not isinstance(existing, dict):
        existing = {}
    else:
        existing = dict(existing)

    models_raw = existing.get("models")
    models: list[dict] = []
    if isinstance(models_raw, list):
        models = [m for m in models_raw if isinstance(m, dict)]

    wanted = _normalize_install_model_ids(info, model_ids)
    actions: list[str] = []
    for mid in wanted:
        caps = get_model_capabilities(mid)
        model_entry = generic_client_config(
            api_key=info.api_key,
            base_url=info.base_url,
            model_id=mid,
            caps=caps,
        )["pi_agent"]
        replaced = False
        for i, m in enumerate(models):
            if m.get("id") == mid:
                models[i] = {**m, **model_entry}
                replaced = True
                break
        if not replaced:
            models.append(dict(model_entry))
        actions.append(f"{'Updated' if replaced else 'Added'} {mid}")

    compat = existing.get("compat")
    if not isinstance(compat, dict):
        compat = {"supportsDeveloperRole": False}

    providers[provider_id] = {
        **existing,
        "baseUrl": info.base_url,
        "api": existing.get("api") or "openai-completions",
        "apiKey": info.api_key,
        "compat": compat,
        "models": models,
    }
    data["providers"] = providers

    _backup_json(path)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _chmod_private(path)
    logger.info(
        "Pi Agent models %s/* in %s: %s",
        provider_id,
        path,
        ", ".join(actions) or "(none)",
    )
    return path


def format_startup_banner(
    info: StoredCredentials,
    *,
    written: dict[str, Path] | None = None,
    workbuddy_installed: Path | None = None,
    opencode_installed: Path | None = None,
    pi_agent_installed: Path | None = None,
    show_full_key: bool = False,
) -> str:
    # Never print the plaintext key by default; point to the key file instead
    # (set GROK_PROXY_BANNER_SHOW_KEY=1 to restore the old behaviour).
    display_key = info.api_key if show_full_key else mask_api_key(info.api_key)
    key_file = (written or {}).get("api_key") or (default_state_dir() / "api_key")
    lines = [
        "",
        "=" * 60,
        "  OpenGrokBuild is ready (OpenAI-compatible)",
        "=" * 60,
        f"  Base URL : {info.base_url}",
        f"  API Key  : {display_key}",
        f"  Model ID : {info.model_id}",
        f"  Key source: {info.source}",
        f"  Full key : {key_file}  (GROK_PROXY_BANNER_SHOW_KEY=1 to print)",
        "",
        "  WorkBuddy / any OpenAI-compatible client:",
        f"    url    = {info.base_url}",
        f"    apiKey = {display_key}",
        f"    model  = {info.model_id}",
        "",
        "  WorkBuddy models.json entry:",
        "  " + json.dumps(
            workbuddy_model_entry(
                api_key=display_key, base_url=info.base_url, model_id=info.model_id
            ),
            ensure_ascii=False,
        ),
    ]
    if written:
        lines.append("")
        lines.append("  Saved local config:")
        for name, p in written.items():
            lines.append(f"    - {name}: {p}")

    install_tips: list[str] = []
    if workbuddy_installed:
        lines.append("")
        lines.append(f"  Installed into WorkBuddy: {workbuddy_installed}")
        lines.append("  Restart / refresh WorkBuddy models if needed.")
    else:
        install_tips.append("--install-workbuddy → ~/.workbuddy/models.json")

    if opencode_installed:
        lines.append("")
        lines.append(f"  Installed into OpenCode: {opencode_installed}")
        lines.append("  Other OpenCode providers/models were left intact.")
    else:
        install_tips.append("--install-opencode → ~/.config/opencode/opencode.json")

    if pi_agent_installed:
        lines.append("")
        lines.append(f"  Installed into Pi Agent: {pi_agent_installed}")
        lines.append("  Other Pi providers/models were left intact.")
    else:
        install_tips.append("--install-pi-agent → ~/.pi/agent/models.json")

    if install_tips:
        lines.append("")
        lines.append("  Tip: register clients without wiping existing models:")
        for tip in install_tips:
            lines.append(f"    {tip}")

    lines.extend(
        [
            "",
            "  Note: this API Key only guards the local proxy.",
            "  Grok itself uses `grok login` (subscription) or XAI_API_KEY.",
            "=" * 60,
            "",
        ]
    )
    return "\n".join(lines)
