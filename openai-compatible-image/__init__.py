"""OpenAI-compatible preset image generation provider for Hermes.

User-installed backend plugin. It calls an OpenAI-compatible
`/images/generations` endpoint and forces `response_format=b64_json` so Hermes
stores the returned image locally instead of depending on provider URLs that may
point at inaccessible private hosts.

This provider is intentionally preset-driven: it does not hard-code Gemini,
GPT, 2K, or any model defaults. A configured preset must resolve to a model and
size for the requested aspect ratio.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Tuple

import requests
import yaml

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_PROVIDER_NAME = "openai-compatible-image"
_CONFIG_KEY = "openai_compatible_image"
_COMMAND_NAME = "image-preset"
_STATE_DIR = get_hermes_home() / "state"
_STATE_PATH = _STATE_DIR / "image_preset_overrides.json"
_STATE_LOCK = Lock()

_SIZE_BY_ASPECT = {
    "landscape": "1536x1024",
    "portrait": "1024x1536",
    "square": "1024x1024",
}

_EXT_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"RIFF", "webp"),
)

_SECRET_RE = re.compile(r"(api[_-]?key|token|secret|password|authorization|auth)", re.I)
_ENV_REF_RE = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$")


def _retry_cfg(cfg: Dict[str, Any], preset: Dict[str, Any]) -> Tuple[int, float, set[int]]:
    provider_cfg = _provider_cfg(cfg)
    raw = preset.get("retry")
    if not isinstance(raw, dict):
        raw = provider_cfg.get("retry")
    if not isinstance(raw, dict):
        raw = {}

    try:
        max_attempts = int(raw.get("max_attempts", 1))
    except Exception:
        max_attempts = 1
    max_attempts = max(1, min(max_attempts, 5))

    try:
        backoff_seconds = float(raw.get("backoff_seconds", 1.5))
    except Exception:
        backoff_seconds = 1.5
    backoff_seconds = max(0.0, min(backoff_seconds, 30.0))

    raw_statuses = raw.get("status_codes", [502, 503, 504])
    retry_statuses: set[int] = set()
    if isinstance(raw_statuses, (list, tuple, set)):
        for item in raw_statuses:
            try:
                retry_statuses.add(int(item))
            except Exception:
                continue
    if not retry_statuses:
        retry_statuses = {502, 503, 504}
    return max_attempts, backoff_seconds, retry_statuses


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        logger.debug("Could not load Hermes config", exc_info=True)
        return {}


def _save_config(cfg: Dict[str, Any]) -> None:
    from hermes_cli.config import save_config

    save_config(cfg)


def _nested(d: Dict[str, Any], *keys: str) -> Optional[Any]:
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _raw_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            text = value.strip()
            match = _ENV_REF_RE.match(text)
            if match:
                resolved = os.getenv(match.group(1) or match.group(2) or "", "").strip()
                if resolved:
                    return resolved
                continue
            return text
    return ""


def _strip_custom_prefix(value: str) -> str:
    raw = (value or "").strip()
    if raw.lower().startswith("custom:"):
        return raw.split(":", 1)[1].strip()
    return raw


def _custom_provider_matches(requested: str, *names: Any) -> bool:
    req_raw = (requested or "").strip().lower()
    if not req_raw:
        return False
    req_base = _strip_custom_prefix(req_raw).lower()
    candidates = set()
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        raw = name.strip().lower()
        base = _strip_custom_prefix(raw).lower()
        candidates.add(raw)
        candidates.add(base)
        candidates.add(f"custom:{base}")
    return req_raw in candidates or req_base in candidates


def _configured_custom_provider_name(cfg: Dict[str, Any], override: str = "") -> str:
    """Return the custom provider name this plugin should inherit, if any."""
    if override:
        return override.strip()
    ax = _provider_cfg(cfg)
    explicit = _first_str(ax.get("custom_provider"), ax.get("provider_name"))
    if explicit:
        return explicit
    selected = _raw_str(_image_section(cfg).get("provider"))
    if selected and selected != _PROVIDER_NAME and _find_custom_provider(cfg, selected):
        return selected
    return ""


def _normalised_custom_provider(entry: Dict[str, Any], *, fallback_name: str = "") -> Dict[str, Any]:
    key_env = _first_str(entry.get("key_env"))
    env_key = os.getenv(key_env, "").strip() if key_env else ""
    return {
        "name": _first_str(entry.get("name"), fallback_name),
        "base_url": _first_str(entry.get("api"), entry.get("url"), entry.get("base_url")),
        "api_key": _first_str(env_key, entry.get("api_key")),
    }


def _find_custom_provider(cfg: Dict[str, Any], requested: str) -> Optional[Dict[str, Any]]:
    """Resolve ``providers:`` / legacy ``custom_providers:`` entries.

    Supports both ``my_provider`` and ``custom:my_provider``. This keeps
    image generation aligned with Hermes model-provider config instead of
    forcing a second image-specific base_url/api_key block.
    """
    requested = _first_str(requested)
    if not requested:
        return None

    providers = cfg.get("providers")
    if isinstance(providers, dict):
        for provider_key, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            if _custom_provider_matches(requested, str(provider_key), _first_str(entry.get("name"))):
                resolved = _normalised_custom_provider(entry, fallback_name=str(provider_key))
                return resolved if resolved.get("base_url") else None

    custom = cfg.get("custom_providers")
    if isinstance(custom, list):
        for entry in custom:
            if not isinstance(entry, dict):
                continue
            if _custom_provider_matches(
                requested,
                _first_str(entry.get("name")),
                _first_str(entry.get("provider_key")),
            ):
                resolved = _normalised_custom_provider(entry, fallback_name=_first_str(entry.get("name")))
                return resolved if resolved.get("base_url") else None
    return None


def _image_custom_provider_aliases(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Provider names this plugin should register as aliases.

    Hermes core dispatches image_gen calls by exact ``provider.name``. A plugin
    can therefore support ``image_gen.provider: custom:my_provider`` without
    touching Hermes core by registering an alias provider with that exact name.
    """
    aliases: Dict[str, str] = {}

    selected = _raw_str(_image_section(cfg).get("provider"))
    if selected and selected != _PROVIDER_NAME:
        if _find_custom_provider(cfg, selected) or selected.lower().startswith("custom:"):
            aliases[selected] = selected

    explicit = _configured_custom_provider_name(cfg)
    if explicit and _find_custom_provider(cfg, explicit):
        base = _strip_custom_prefix(explicit)
        aliases.setdefault(explicit, explicit)
        aliases.setdefault(base, explicit)
        aliases.setdefault(f"custom:{base}", explicit)

    return aliases


def _image_generations_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/images/generations"):
        return url
    return f"{url}/images/generations"


def _coerce_b64(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if text.startswith("data:image/") and "," in text:
        return text.split(",", 1)[1].strip()
    return text


def _env_named(value: Any) -> str:
    name = _first_str(value)
    return os.getenv(name, "").strip() if name else ""


def _detect_ext(raw: bytes) -> str:
    for magic, ext in _EXT_MAGIC:
        if raw.startswith(magic):
            return ext
    return "png"


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:96] or "image"


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            key: ("[REDACTED]" if _SECRET_RE.search(str(key)) and value else _redact(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    return obj


def _load_state() -> Dict[str, str]:
    with _STATE_LOCK:
        try:
            if not _STATE_PATH.exists():
                return {}
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v.strip()}
        except Exception:
            logger.debug("Could not load image preset overrides", exc_info=True)
            return {}


def _save_state(data: Dict[str, str]) -> None:
    with _STATE_LOCK:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(_STATE_PATH)


def _session_key() -> str:
    try:
        from gateway.session_context import get_session_env

        return get_session_env("HERMES_SESSION_KEY", "") or ""
    except Exception:
        return os.getenv("HERMES_SESSION_KEY", "") or ""


def _last_active_session_key() -> Tuple[str, str]:
    """Best-effort fallback for plugin commands that lack event/source.

    Telegram's adapter logs the text-batch key before dispatching slash
    commands, e.g. `[Telegram] Flushing text batch agent:main:...`. That key is
    already the same coarse session key used by the gateway. For other cases,
    fall back to the most recently updated gateway session entry.
    """
    log_dir = get_hermes_home() / "logs"
    pattern = re.compile(r"Flushing text batch (agent:main:\S+) ")
    candidates = [log_dir / "agent.log"]
    candidates.extend(sorted(log_dir.glob("agent.log-*"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:3])
    for path in candidates:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for line in reversed(lines[-400:]):
            match = pattern.search(line)
            if match:
                return match.group(1), f"latest Telegram text batch in `{path.name}`"

    sessions_path = get_hermes_home() / "sessions" / "sessions.json"
    try:
        data = json.loads(sessions_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if isinstance(data, dict):
        best_key = ""
        best_updated = ""
        best_sid = ""
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            session_key = str(entry.get("session_key") or key or "").strip()
            updated = str(entry.get("updated_at") or "")
            if session_key and updated >= best_updated:
                best_key = session_key
                best_updated = updated
                best_sid = str(entry.get("session_id") or "")
        if best_key:
            note = f"most recently updated session in sessions.json"
            if best_sid:
                note += f" (`{best_sid}`)"
            return best_key, note
    return "", ""


def _session_key_or_guess() -> Tuple[str, str]:
    session_key = _session_key()
    if session_key:
        return session_key, "current session context"
    return _last_active_session_key()


def _looks_like_session_key(value: str) -> bool:
    value = str(value or "").strip()
    return value.startswith("agent:main:") and value.count(":") >= 3


def _resolve_session_identifier(value: str) -> Tuple[str, str]:
    """Resolve a user-supplied session identifier to the session key used by overrides.

    Accepts the full session key (agent:main:...), a gateway session_id from
    `~/.hermes/sessions/sessions.json`, or a unique prefix of either. Returns
    `(session_key, note)` where note explains how it was resolved.
    """
    ident = str(value or "").strip()
    if not ident:
        return "", ""
    if _looks_like_session_key(ident):
        return ident, "session key"

    sessions_path = get_hermes_home() / "sessions" / "sessions.json"
    try:
        data = json.loads(sessions_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    matches = []
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        session_key = str(entry.get("session_key") or key or "").strip()
        session_id = str(entry.get("session_id") or "").strip()
        if not session_key:
            continue
        if ident == session_id:
            return session_key, f"session_id `{session_id}`"
        if ident == session_key:
            return session_key, "session key"
        if session_id.startswith(ident) or session_key.startswith(ident):
            matches.append((session_key, session_id))

    if len(matches) == 1:
        key, sid = matches[0]
        label = f"session_id prefix `{ident}` → `{sid}`" if sid.startswith(ident) else f"session key prefix `{ident}`"
        return key, label
    if len(matches) > 1:
        raise ValueError(f"Session identifier `{ident}` is ambiguous; provide the full session_id or full session key.")
    return ident, "literal session key"


def _current_session_override() -> Tuple[str, str]:
    session_key, _ = _session_key_or_guess()
    if not session_key:
        return "", ""
    return _load_state().get(session_key, ""), session_key


def _set_session_override(preset: str, session_key: str = "") -> Tuple[bool, str, str]:
    note = "explicit session" if session_key else ""
    if session_key:
        session_key = str(session_key).strip()
    else:
        session_key, note = _session_key_or_guess()
    if not session_key:
        return False, "", ""
    state = _load_state()
    state[session_key] = preset
    _save_state(state)
    return True, session_key, note


def _clear_session_override(session_key: str = "") -> Tuple[bool, str, str]:
    note = "explicit session" if session_key else ""
    if session_key:
        session_key = str(session_key).strip()
    else:
        session_key, note = _session_key_or_guess()
    if not session_key:
        return False, "", ""
    state = _load_state()
    existed = session_key in state
    if existed:
        state.pop(session_key, None)
        _save_state(state)
    return existed, session_key, note


def _image_section(cfg: Dict[str, Any]) -> Dict[str, Any]:
    image_cfg = cfg.get("image_gen")
    return image_cfg if isinstance(image_cfg, dict) else {}


def _provider_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    image_cfg = _image_section(cfg)
    ax = image_cfg.get(_CONFIG_KEY)
    return ax if isinstance(ax, dict) else {}


def _presets(cfg: Dict[str, Any]) -> Dict[str, Any]:
    ax = _provider_cfg(cfg)
    presets = ax.get("presets")
    return presets if isinstance(presets, dict) else {}


def _global_preset(cfg: Dict[str, Any]) -> str:
    return _first_str(_image_section(cfg).get("preset"))


def _effective_preset(cfg: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str]:
    cfg = cfg if isinstance(cfg, dict) else _load_config()
    override, session_key = _current_session_override()
    if override:
        return override, "session", session_key
    global_name = _global_preset(cfg)
    if global_name:
        return global_name, "global", session_key
    return "", "missing", session_key


def _format_presets(cfg: Dict[str, Any]) -> str:
    presets = _presets(cfg)
    if not presets:
        return "No presets configured under `image_gen.openai_compatible_image.presets`."
    current, scope, _ = _effective_preset(cfg)
    lines = ["Available image presets:"]
    for name in sorted(presets):
        preset = presets.get(name) if isinstance(presets.get(name), dict) else {}
        display = _first_str(preset.get("display"), preset.get("description")) if isinstance(preset, dict) else ""
        marker = " ✅" if name == current else ""
        suffix = f" — {display}" if display else ""
        lines.append(f"- `{name}`{marker}{suffix}")
    if current:
        lines.append(f"\nCurrent: `{current}` ({scope})")
    else:
        lines.append("\nCurrent: not configured")
    return "\n".join(lines)


def _format_help(cfg: Dict[str, Any], *, include_status: bool = True) -> str:
    lines = []
    if include_status:
        current, scope, session_key = _effective_preset(cfg)
        global_name = _global_preset(cfg)
        override, _ = _current_session_override()
        lines.append("Image preset status:")
        lines.append(f"Provider: `{_image_section(cfg).get('provider', '') or 'unset'}`")
        lines.append(f"Global preset: `{global_name}`" if global_name else "Global preset: not configured")
        lines.append(f"Session override: `{override}`" if override else "Session override: none")
        lines.append(f"Effective preset: `{current}` ({scope})" if current else "Effective preset: not configured")
        if session_key:
            lines.append(f"Inferred session key: `{session_key}`")
        else:
            lines.append("Inferred session key: unavailable")
        lines.append("")
    lines.append(_format_presets(cfg))
    lines.append("")
    lines.append("Usage:")
    lines.append("- `/image_preset` — show current status and available presets")
    lines.append("- `/image_preset list` — show available presets")
    lines.append("- `/image_preset <preset>` — set a session override for the inferred current conversation")
    lines.append("- `/image_preset <preset> --global` — set the global default")
    lines.append("- `/image_preset reset` — clear the inferred current conversation override")
    lines.append("")
    lines.append("Tip: automatic inference uses Hermes' latest Telegram text-batch session key, then falls back to the most recently updated gateway session.")
    return "\n".join(lines)


def _parse_command_args(raw_args: str) -> Tuple[str, bool, str]:
    try:
        parts = shlex.split(str(raw_args or ""))
    except ValueError:
        parts = [p for p in str(raw_args or "").strip().split() if p]
    is_global = False
    explicit_session = ""
    kept = []
    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "--global":
            is_global = True
        elif part in {"--session", "--session-id", "--session_id", "--session-key", "--session_key"}:
            if i + 1 >= len(parts):
                raise ValueError(f"{part} requires a session_id_or_key value")
            explicit_session = parts[i + 1].strip()
            i += 1
        elif part.startswith("--session="):
            explicit_session = part.split("=", 1)[1].strip()
        elif part.startswith("--session-id=") or part.startswith("--session_id="):
            explicit_session = part.split("=", 1)[1].strip()
        elif part.startswith("--session-key=") or part.startswith("--session_key="):
            explicit_session = part.split("=", 1)[1].strip()
        else:
            kept.append(part)
        i += 1
    return " ".join(kept).strip(), is_global, explicit_session


def _image_preset_command(raw_args: str) -> str:
    cfg = _load_config()
    try:
        arg, persist_global, explicit_session_raw = _parse_command_args(raw_args)
    except ValueError as exc:
        return f"Invalid `/image_preset` arguments: {exc}\n\n" + _format_help(cfg, include_status=False)
    action = arg.strip()
    action_key = action.lower()

    if not action or action_key == "help":
        return _format_help(cfg)
    if action_key == "list":
        return _format_presets(cfg)
    explicit_session_key = ""
    explicit_session_note = ""
    if explicit_session_raw:
        try:
            explicit_session_key, explicit_session_note = _resolve_session_identifier(explicit_session_raw)
        except ValueError as exc:
            return str(exc)
    if action_key == "reset":
        if persist_global:
            return "`/image_preset reset --global` is not supported. Set a concrete global preset with `/image_preset <preset> --global`."
        existed, cleared_key, cleared_note = _clear_session_override(explicit_session_key)
        current, scope, _ = _effective_preset(cfg)
        if cleared_key:
            if explicit_session_key:
                lines = [
                    f"Cleared image preset override for `{cleared_key}`." if existed else f"No image preset override existed for `{cleared_key}`.",
                    f"Resolved from {explicit_session_note}.",
                ]
            else:
                lines = [
                    f"Cleared image preset override for inferred session `{cleared_key}`." if existed else f"No image preset override existed for inferred session `{cleared_key}`.",
                    f"Inferred from {cleared_note}.",
                ]
        else:
            lines = ["Could not determine the current session key. Run `/image_preset` to inspect inference status."]
        if current and not explicit_session_key:
            lines.append(f"Effective preset is now `{current}` ({scope}).")
        elif not explicit_session_key:
            lines.append("No global image preset is configured. Set one with `/image_preset <preset> --global`.")
        return "\n".join(lines)

    preset = action
    presets = _presets(cfg)
    if preset not in presets:
        available = ", ".join(f"`{name}`" for name in sorted(presets)) or "none"
        return f"Unknown image preset `{preset}`. Available presets: {available}"

    session_ok, session_key_used, session_note = _set_session_override(preset, explicit_session_key)
    if persist_global:
        image_cfg = cfg.setdefault("image_gen", {})
        if not isinstance(image_cfg, dict):
            cfg["image_gen"] = image_cfg = {}
        image_cfg["preset"] = preset
        image_cfg["provider"] = _PROVIDER_NAME
        try:
            _save_config(cfg)
        except Exception as exc:
            return f"Failed to save global image preset `{preset}`: {exc}"
        lines = [f"Image preset switched to `{preset}`.", "Saved to config.yaml (`--global`)."]
        if session_ok:
            lines.append("This conversation override was also set, matching `/model --global` behavior.")
        else:
            lines.append("No session key was available, so only the global preset was changed.")
        return "\n".join(lines)

    if not session_ok:
        return "Could not infer the current session key; try `/image_preset <preset> --global` to set the global preset, or run `/image_preset` to inspect inference status."
    if explicit_session_key:
        return f"Image preset switched to `{preset}` for `{session_key_used}`.\nResolved from {explicit_session_note}.\n_(session only -- add `--global` to also persist globally)_"
    return f"Image preset switched to `{preset}` for inferred session `{session_key_used}`.\nInferred from {session_note}.\n_(session only -- add `--global` to persist)_"


class OpenAICompatibleImageProvider(ImageGenProvider):
    def __init__(self, provider_name: str = _PROVIDER_NAME, custom_provider: str = "") -> None:
        self._provider_name = provider_name
        self._custom_provider = custom_provider

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def display_name(self) -> str:
        if self._custom_provider:
            return f"OpenAI-Compatible Image ({self._custom_provider})"
        return "OpenAI-Compatible Image"

    def is_available(self) -> bool:
        cfg = _load_config()
        return bool(self._base_url(cfg) and self._api_key(cfg))

    def list_models(self):
        cfg = _load_config()
        seen = set()
        models = []
        for preset_name, preset in _presets(cfg).items():
            if not isinstance(preset, dict):
                continue
            display = _first_str(preset.get("display"), preset_name)
            values = []
            model = preset.get("model")
            if isinstance(model, str):
                values.append(model)
            pmodels = preset.get("models")
            if isinstance(pmodels, dict):
                values.extend(v for v in pmodels.values() if isinstance(v, str))
            for model_id in values:
                model_id = model_id.strip()
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                models.append({"id": model_id, "display": f"{model_id} ({display})"})
        return models

    def default_model(self) -> str:
        cfg = _load_config()
        preset_name, _, _ = _effective_preset(cfg)
        try:
            preset, model, _size = self._resolve_preset_model_size(cfg, preset_name, DEFAULT_ASPECT_RATIO)
            return model
        except Exception:
            return ""

    def get_setup_schema(self):
        return {
            "name": self.display_name,
            "badge": "custom",
            "tag": "OpenAI-compatible /v1/images/generations; presets, custom-provider aliases, local cache output.",
            "env_vars": [],
        }

    def _base_url(self, cfg: Dict[str, Any]) -> str:
        ax = _provider_cfg(cfg)
        custom_name = _configured_custom_provider_name(cfg, self._custom_provider)
        matching_custom = _find_custom_provider(cfg, custom_name) if custom_name else None
        return _first_str(
            os.getenv("OPENAI_COMPAT_IMAGE_BASE_URL"),
            _env_named(ax.get("base_url_env")),
            _nested(matching_custom or {}, "base_url"),
            ax.get("base_url"),
            _nested(cfg, "model", "base_url"),
        ).rstrip("/")

    def _api_key(self, cfg: Dict[str, Any]) -> str:
        ax = _provider_cfg(cfg)
        custom_name = _configured_custom_provider_name(cfg, self._custom_provider)
        matching_custom = _find_custom_provider(cfg, custom_name) if custom_name else None
        return _first_str(
            os.getenv("OPENAI_COMPAT_IMAGE_API_KEY"),
            _env_named(ax.get("api_key_env")),
            _nested(matching_custom or {}, "api_key"),
            ax.get("api_key"),
            _nested(cfg, "model", "api_key"),
        )

    def _resolve_preset_model_size(self, cfg: Dict[str, Any], preset_name: str, aspect: str) -> Tuple[Dict[str, Any], str, str]:
        if not preset_name:
            raise ValueError("No image preset configured. Set one with `/image_preset <preset> --global` or `/image_preset <preset>`.")
        presets = _presets(cfg)
        preset = presets.get(preset_name)
        if not isinstance(preset, dict):
            raise ValueError(f"Image preset '{preset_name}' is not defined under image_gen.{_CONFIG_KEY}.presets")

        models = preset.get("models")
        model = ""
        if isinstance(models, dict):
            model = _first_str(models.get(aspect))
        model = _first_str(model, preset.get("model"))
        if not model:
            raise ValueError(f"Image preset '{preset_name}' does not define a model for aspect '{aspect}' or a fallback model")

        sizes = preset.get("sizes")
        size = ""
        if isinstance(sizes, dict):
            size = _first_str(sizes.get(aspect))
        size = _first_str(size, preset.get("size"))
        if not size:
            raise ValueError(f"Image preset '{preset_name}' does not define a size for aspect '{aspect}' or a fallback size")

        return preset, model, size

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        aspect = resolve_aspect_ratio(aspect_ratio)
        cfg = _load_config()
        base_url = self._base_url(cfg)
        api_key = self._api_key(cfg)
        preset_name, preset_scope, session_key = _effective_preset(cfg)

        try:
            preset, model, size = self._resolve_preset_model_size(cfg, preset_name, aspect)
        except Exception as exc:
            return error_response(
                error=str(exc),
                error_type="configuration_error",
                provider=self.name,
                model="",
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if not base_url or not api_key:
            return error_response(
                error="OpenAI-compatible image base_url/api_key not configured",
                error_type="missing_credentials",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        response_format = _first_str(
            preset.get("response_format"),
            _provider_cfg(cfg).get("response_format"),
            "b64_json",
        )
        if response_format not in {"b64_json", "url"}:
            response_format = "b64_json"

        payload = {
            "model": model,
            "prompt": prompt,
            "n": int(preset.get("n", 1)) if isinstance(preset.get("n", 1), int) else 1,
            "size": size,
            "response_format": response_format,
        }
        extra_body = preset.get("extra_body")
        if not isinstance(extra_body, dict):
            extra_body = _provider_cfg(cfg).get("extra_body")
        if isinstance(extra_body, dict):
            payload.update(extra_body)
            # Keep Hermes output durable by default. Operators can intentionally
            # choose URL mode with response_format: url; URL responses are still
            # materialised into the local cache below.
            if response_format == "b64_json":
                payload["response_format"] = "b64_json"

        timeout = preset.get("timeout", _provider_cfg(cfg).get("timeout", 240))
        max_attempts, backoff_seconds, retry_statuses = _retry_cfg(cfg, preset)
        response = None
        last_exc = None
        generations_url = _image_generations_url(base_url)
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    generations_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=float(timeout),
                )
                if response.status_code not in retry_statuses or attempt >= max_attempts:
                    break
                body_preview = response.text[:300]
                sleep_for = backoff_seconds * attempt
                logger.warning(
                    "OpenAI-compatible image API returned retryable HTTP %s on attempt %s/%s; retrying in %.1fs: %s",
                    response.status_code,
                    attempt,
                    max_attempts,
                    sleep_for,
                    body_preview,
                )
                time.sleep(sleep_for)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    response = None
                    break
                sleep_for = backoff_seconds * attempt
                logger.warning(
                    "OpenAI-compatible image request failed on attempt %s/%s; retrying in %.1fs: %s",
                    attempt,
                    max_attempts,
                    sleep_for,
                    exc,
                )
                time.sleep(sleep_for)
        if response is None:
            return error_response(
                error=f"OpenAI-compatible image request failed after {max_attempts} attempt(s): {last_exc}",
                error_type="connection_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        if response.status_code >= 400:
            body = response.text[:1200]
            return error_response(
                error=f"OpenAI-compatible image API returned HTTP {response.status_code} after {max_attempts} attempt(s): {body}",
                error_type="api_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        try:
            data = response.json()
        except Exception as exc:
            return error_response(
                error=f"OpenAI-compatible image API returned non-JSON response: {exc}",
                error_type="invalid_response",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        items = data.get("data") if isinstance(data, dict) else None
        first = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
        b64 = _coerce_b64(first.get("b64_json") or first.get("image_base64") or first.get("base64"))
        image_url = _first_str(first.get("url"), first.get("image_url"))
        revised_prompt = first.get("revised_prompt")
        raw = b""
        output_kind = "b64_json"
        try:
            if b64:
                raw = base64.b64decode(b64)
                ext = _detect_ext(raw)
                saved = save_b64_image(
                    b64,
                    prefix=f"openai_compatible_image_{_safe_name(preset_name)}_{_safe_name(model)}",
                    extension=ext,
                )
            elif image_url:
                saved = save_url_image(
                    image_url,
                    prefix=f"openai_compatible_image_{_safe_name(preset_name)}_{_safe_name(model)}",
                    timeout=float(timeout),
                )
                raw = saved.read_bytes()
                output_kind = "url"
            else:
                return error_response(
                    error="OpenAI-compatible image response did not include b64_json/base64 or url",
                    error_type="empty_response",
                    provider=self.name,
                    model=model,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
        except Exception as exc:
            return error_response(
                error=f"Could not save returned image: {exc}",
                error_type="io_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {
            "size": payload.get("size"),
            "response_format": payload.get("response_format", response_format),
            "output_kind": output_kind,
            "bytes": len(raw),
            "preset": preset_name,
            "preset_scope": preset_scope,
        }
        if session_key:
            extra["session_key"] = session_key
        if revised_prompt:
            extra["revised_prompt"] = revised_prompt

        return success_response(
            image=str(saved),
            model=model,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=self.name,
            extra=extra,
        )


def register(ctx) -> None:
    cfg = _load_config()
    ctx.register_image_gen_provider(OpenAICompatibleImageProvider())
    for provider_name, custom_provider in _image_custom_provider_aliases(cfg).items():
        if provider_name == _PROVIDER_NAME:
            continue
        ctx.register_image_gen_provider(
            OpenAICompatibleImageProvider(
                provider_name=provider_name,
                custom_provider=custom_provider,
            )
        )
    ctx.register_command(
        name=_COMMAND_NAME,
        handler=_image_preset_command,
        description="Show or switch the OpenAI-compatible image generation preset",
        args_hint="[list|reset|<preset> [--global]]",
    )
