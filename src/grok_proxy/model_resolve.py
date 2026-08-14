from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("grok_proxy")

# Friendly / outdated ids clients may send → map to CLI default when unknown.
MODEL_ALIASES = frozenset(
    {
        "grok-build",
        "grok-build-plan",
        "grok",
        "default",
        "auto",
    }
)

FALLBACK_MODEL = "grok-4.5"

# When cache omits max_completion_tokens, use a coding-agent friendly default.
DEFAULT_MAX_OUTPUT_TOKENS = 65_536

# Hard-coded fallbacks when ~/.grok/models_cache.json is missing or incomplete.
# Prefer cache values at runtime; these keep client configs usable offline.
_KNOWN_MODEL_CAPS: dict[str, dict[str, object]] = {
    "grok-4.6": {
        "display_name": "Grok 4.6",
        "context_window": 500_000,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "supports_images": True,
        "supports_reasoning": True,
        # Keep xhigh off the default WorkBuddy effort. It burns the 10-minute
        # proxy budget on thinking before the agent finishes the task.
        "reasoning_efforts": ["high", "medium", "low"],
        "default_reasoning_effort": "high",
    },
    "grok-4.5": {
        "display_name": "Grok 4.5",
        "context_window": 500_000,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "supports_images": True,
        "supports_reasoning": True,
        "reasoning_efforts": ["high", "medium", "low"],
        "default_reasoning_effort": "high",
    },
    "grok-build-0.1": {
        "display_name": "Grok Build 0.1",
        "context_window": 256_000,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "supports_images": True,
        "supports_reasoning": True,
        "reasoning_efforts": ["high", "medium", "low"],
        "default_reasoning_effort": "high",
    },
}


@dataclass(frozen=True)
class ModelCapabilities:
    """Client-facing model metadata (context window, modalities, reasoning)."""

    model_id: str
    display_name: str
    context_window: int
    max_output_tokens: int
    supports_images: bool
    supports_reasoning: bool
    # OpenAI-style client tool calling is not the proxy's primary surface
    # (the gateway itself is the agent). Keep False for WorkBuddy etc.
    supports_tool_call: bool = False
    reasoning_efforts: tuple[str, ...] = ()
    default_reasoning_effort: str | None = None
    source: str = "fallback"  # cache | known | fallback


@dataclass
class RuntimeModels:
    default_model: str
    available: list[str] = field(default_factory=list)
    source: str = "fallback"  # cache | cli | config | fallback


def _grok_home() -> Path:
    env = os.environ.get("GROK_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".grok"


def _read_models_cache_obj(cache_path: Path | None = None) -> dict[str, object] | None:
    path = cache_path or (_grok_home() / "models_cache.json")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read models cache %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _normalize_default_reasoning_effort(
    efforts: list[str],
    default: str | None,
) -> str | None:
    """Do not default clients to xhigh — it is optional, not the install default."""
    if not default:
        return None
    if default.lower() != "xhigh":
        return default
    for candidate in ("high", "medium", "low"):
        if candidate in efforts:
            return candidate
    for item in efforts:
        if item.lower() != "xhigh":
            return item
    return "high"


def _caps_from_info(model_id: str, info: dict[str, object], *, source: str) -> ModelCapabilities:
    known = _KNOWN_MODEL_CAPS.get(model_id, {})
    display = str(info.get("name") or known.get("display_name") or model_id)

    ctx_raw = info.get("context_window")
    if isinstance(ctx_raw, (int, float)) and int(ctx_raw) > 0:
        context_window = int(ctx_raw)
    else:
        context_window = int(known.get("context_window") or 128_000)

    out_raw = info.get("max_completion_tokens")
    if isinstance(out_raw, (int, float)) and int(out_raw) > 0:
        max_output = int(out_raw)
    else:
        max_output = int(known.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS)

    efforts: list[str] = []
    raw_efforts = info.get("reasoning_efforts")
    if isinstance(raw_efforts, list):
        for item in raw_efforts:
            if isinstance(item, dict):
                val = item.get("value") or item.get("id")
                if val:
                    efforts.append(str(val))
            elif isinstance(item, str) and item.strip():
                efforts.append(item.strip())
    if not efforts:
        known_efforts = known.get("reasoning_efforts")
        if isinstance(known_efforts, list):
            efforts = [str(x) for x in known_efforts]

    default_effort: str | None = None
    if isinstance(info.get("reasoning_effort"), str) and info["reasoning_effort"]:
        default_effort = str(info["reasoning_effort"])
    elif efforts:
        # Prefer explicit default flag in cache
        if isinstance(raw_efforts, list):
            for item in raw_efforts:
                if isinstance(item, dict) and item.get("default"):
                    default_effort = str(item.get("value") or item.get("id") or efforts[0])
                    break
        if not default_effort:
            known_default = known.get("default_reasoning_effort")
            default_effort = str(known_default) if known_default else efforts[0]

    supports_reasoning = bool(
        info.get("supports_reasoning_effort")
        if "supports_reasoning_effort" in info
        else (known.get("supports_reasoning", bool(efforts)))
    )
    # Grok multimodal models accept images; cache does not always expose a flag.
    if "supports_images" in info:
        supports_images = bool(info.get("supports_images"))
    else:
        supports_images = bool(known.get("supports_images", True))

    return ModelCapabilities(
        model_id=model_id,
        display_name=display,
        context_window=context_window,
        max_output_tokens=max_output,
        supports_images=supports_images,
        supports_reasoning=supports_reasoning,
        supports_tool_call=False,
        reasoning_efforts=tuple(efforts),
        default_reasoning_effort=(
            _normalize_default_reasoning_effort(efforts, default_effort)
            if supports_reasoning
            else None
        ),
        source=source,
    )


def get_model_capabilities(
    model_id: str,
    *,
    cache_path: Path | None = None,
) -> ModelCapabilities:
    """Resolve context window / modalities for client config generation.

    Priority: models_cache.json info → known table → conservative fallback.
    """
    mid = (model_id or "").strip() or FALLBACK_MODEL
    data = _read_models_cache_obj(cache_path)
    if data:
        models_obj = data.get("models") or {}
        if isinstance(models_obj, dict):
            meta = models_obj.get(mid)
            if isinstance(meta, dict):
                info = meta.get("info") if isinstance(meta.get("info"), dict) else meta
                if isinstance(info, dict) and info:
                    return _caps_from_info(mid, info, source="cache")

    if mid in _KNOWN_MODEL_CAPS:
        known = _KNOWN_MODEL_CAPS[mid]
        return ModelCapabilities(
            model_id=mid,
            display_name=str(known.get("display_name") or mid),
            context_window=int(known.get("context_window") or 128_000),
            max_output_tokens=int(known.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS),
            supports_images=bool(known.get("supports_images", True)),
            supports_reasoning=bool(known.get("supports_reasoning", False)),
            supports_tool_call=False,
            reasoning_efforts=tuple(str(x) for x in (known.get("reasoning_efforts") or [])),  # type: ignore[arg-type]
            default_reasoning_effort=(
                str(known["default_reasoning_effort"])
                if known.get("default_reasoning_effort")
                else None
            ),
            source="known",
        )

    # Unknown model: still advertise multimodal + a usable window so clients
    # do not default to tiny limits; CLI will reject truly invalid ids.
    return ModelCapabilities(
        model_id=mid,
        display_name=mid,
        context_window=128_000,
        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        supports_images=True,
        supports_reasoning=True,
        supports_tool_call=False,
        reasoning_efforts=("high", "medium", "low"),
        default_reasoning_effort="high",
        source="fallback",
    )


def load_models_from_cache(cache_path: Path | None = None) -> RuntimeModels | None:
    path = cache_path or (_grok_home() / "models_cache.json")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read models cache %s: %s", path, e)
        return None

    models_obj = data.get("models") or {}
    if not isinstance(models_obj, dict) or not models_obj:
        return None

    available: list[str] = []
    default: str | None = None
    for mid, meta in models_obj.items():
        if not isinstance(mid, str):
            continue
        available.append(mid)
        info = (meta or {}).get("info") if isinstance(meta, dict) else None
        if isinstance(info, dict) and info.get("hidden"):
            continue
    # Prefer non-hidden list order; default = first non-hidden or first key
    non_hidden: list[str] = []
    for mid, meta in models_obj.items():
        info = (meta or {}).get("info") if isinstance(meta, dict) else {}
        if isinstance(info, dict) and info.get("hidden"):
            continue
        non_hidden.append(mid)
    available = non_hidden or list(models_obj.keys())
    default = available[0] if available else None
    if not default:
        return None
    return RuntimeModels(default_model=default, available=available, source="cache")


def load_models_from_cli(grok_bin: str = "grok", timeout: float = 15.0) -> RuntimeModels | None:
    try:
        proc = subprocess.run(
            [grok_bin, "models"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug("grok models failed: %s", e)
        return None

    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0 and not text.strip():
        return None

    default: str | None = None
    m = re.search(r"Default model:\s*(\S+)", text)
    if m:
        default = m.group(1).strip()

    available: list[str] = []
    for line in text.splitlines():
        # lines like "  * grok-4.5 (default)" or "  - grok-4.5"
        mm = re.match(r"^\s*[\*\-•]\s+(\S+)", line)
        if mm:
            mid = mm.group(1).strip()
            mid = mid.rstrip(",")
            if mid and mid not in available:
                available.append(mid)

    if default and default not in available:
        available.insert(0, default)
    if not default and available:
        default = available[0]
    if not default:
        return None
    return RuntimeModels(default_model=default, available=available, source="cli")


def resolve_runtime_models(
    *,
    configured_default: str | None = None,
    configured_models: str | None = None,
    grok_bin: str = "grok",
    prefer_cli: bool = False,
) -> RuntimeModels:
    """
    Resolve which model ids this proxy should advertise and use by default.

    Priority for available list:
      1. GROK_PROXY_MODELS if non-empty (explicit)
      2. grok models CLI (if prefer_cli) else models_cache.json then CLI
      3. fallback grok-4.5
    """
    configured_default = (configured_default or "").strip()
    configured_list = [
        m.strip() for m in (configured_models or "").split(",") if m.strip()
    ]
    # Treat legacy default "grok-build" as unset so we auto-detect real CLI models
    if configured_default in MODEL_ALIASES:
        configured_default = ""
    if configured_list == ["grok-build"] or all(m in MODEL_ALIASES for m in configured_list):
        configured_list = []

    discovered: RuntimeModels | None = None
    if prefer_cli:
        discovered = load_models_from_cli(grok_bin) or load_models_from_cache()
    else:
        discovered = load_models_from_cache() or load_models_from_cli(grok_bin)

    if configured_list:
        default = configured_default or (
            configured_list[0]
            if configured_list
            else (discovered.default_model if discovered else FALLBACK_MODEL)
        )
        # If configured default is alias / empty, use first configured or discovered
        if not configured_default and discovered:
            default = discovered.default_model if discovered.default_model in configured_list else configured_list[0]
        elif configured_default:
            default = configured_default
        return RuntimeModels(
            default_model=default,
            available=configured_list,
            source="config",
        )

    if discovered:
        default = configured_default or discovered.default_model
        if default not in discovered.available:
            # User forced a specific id; keep it but advertise discovered list + this id
            available = list(discovered.available)
            if default not in available:
                available.insert(0, default)
            return RuntimeModels(default_model=default, available=available, source=discovered.source)
        return RuntimeModels(
            default_model=default,
            available=discovered.available,
            source=discovered.source,
        )

    default = configured_default or FALLBACK_MODEL
    return RuntimeModels(default_model=default, available=[default], source="fallback")


def resolve_request_model(
    requested: str | None,
    runtime: RuntimeModels,
) -> str:
    """
    Map an incoming OpenAI `model` field to a Grok CLI model id.
    Unknown aliases like grok-build → runtime.default_model.
    """
    name = (requested or "").strip()
    if not name or name in MODEL_ALIASES:
        return runtime.default_model
    if name in runtime.available:
        return name
    # Soft alias: any grok-build* → default
    if name.startswith("grok-build"):
        logger.info("Remapping model %r → %r (CLI alias)", name, runtime.default_model)
        return runtime.default_model
    # Pass through (CLI will error if truly invalid)
    return name
