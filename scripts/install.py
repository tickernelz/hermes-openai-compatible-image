#!/usr/bin/env python3
"""Guided installer for the Hermes openai-compatible-image plugin."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import yaml
except Exception as exc:  # pragma: no cover - exercised by shell users
    raise SystemExit(
        "PyYAML is required to update Hermes config.yaml. Install it or run with the "
        "Python used by Hermes. Original error: %s" % exc
    )

PLUGIN_REF = "image_gen/openai-compatible-image"
PLUGIN_DIRNAME = "openai-compatible-image"
DEFAULT_PRESET = "auto"
DEFAULT_SIZES = {
    "landscape": "1536x1024",
    "portrait": "1024x1536",
    "square": "1024x1024",
}
_SAFE_ENV_VALUE = re.compile(r"^[A-Za-z0-9_./:@%+=,~-]+$")
_PROVIDER_ID_RE = re.compile(r"[^A-Za-z0-9_]+")


class TuiDependencyError(RuntimeError):
    """Raised when interactive TUI dependencies are unavailable."""


@dataclass(frozen=True)
class ProviderCandidate:
    name: str
    label: str
    base_url: str = ""
    api_key_env: str = ""
    api_key: str = ""
    api_key_source: str = ""
    source: str = "config"
    models: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        suffixes: list[str] = []
        if self.source:
            suffixes.append(self.source)
        if self.base_url:
            suffixes.append(self.base_url)
        return f"{self.label} ({' · '.join(suffixes)})" if suffixes else self.label

    @property
    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""


class RichPromptTui:
    """Small TUI facade backed by Rich output and prompt_toolkit input."""

    def __init__(self) -> None:
        try:
            from prompt_toolkit import prompt as toolkit_prompt  # type: ignore
            from prompt_toolkit.completion import WordCompleter  # type: ignore
            from rich.console import Console  # type: ignore
            from rich.panel import Panel  # type: ignore
            from rich.table import Table  # type: ignore
        except ImportError as exc:  # pragma: no cover - environment dependent
            manual = f"{sys.executable} -m pip install 'PyYAML>=6' 'rich>=13' 'prompt_toolkit>=3'"
            raise TuiDependencyError(
                "Interactive install requires TUI dependencies: rich and prompt_toolkit. "
                "The install.sh bootstrapper installs them automatically. "
                f"If you run the Python script directly, install them manually with: {manual}"
            ) from exc
        self._prompt = toolkit_prompt
        self._word_completer = WordCompleter
        self.console = Console()
        self._panel = Panel
        self._table = Table

    def banner(self, title: str, subtitle: str) -> None:
        self.console.print(self._panel(subtitle, title=title, border_style="cyan"))

    def text(self, prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        value = self._prompt(f"{prompt}{suffix}: ").strip()
        return value or default

    def password(self, prompt: str) -> str:
        return self._prompt(f"{prompt}: ", is_password=True).strip()

    def confirm(self, prompt: str, default: bool = False) -> bool:
        suffix = "Y/n" if default else "y/N"
        while True:
            answer = self._prompt(f"{prompt} [{suffix}] ").strip().lower()
            if not answer:
                return default
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self.console.print("[yellow]Please answer yes or no.[/yellow]")

    def select(self, prompt: str, choices: Sequence[str], default: str = "") -> str:
        choices = tuple(choices)
        if not choices:
            return default
        default = default if default in choices else choices[0]
        table = self._table.grid(padding=(0, 2))
        table.add_column(justify="right")
        table.add_column()
        for index, choice in enumerate(choices, start=1):
            marker = "*" if choice == default else " "
            table.add_row(f"{index}.", f"{marker} {choice}")
        self.console.print(prompt)
        self.console.print(table)
        completer = self._word_completer(list(choices), ignore_case=True)
        while True:
            answer = self._prompt(f"Select [{default}]: ", completer=completer).strip()
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return choices[int(answer) - 1]
            for choice in choices:
                if answer.lower() == choice.lower():
                    return choice
            self.console.print(f"[yellow]Choose one of: {', '.join(choices)}[/yellow]")

    def provider_table(self, providers: Sequence[ProviderCandidate]) -> None:
        table = self._table(title="Detected Hermes providers")
        table.add_column("Provider", style="cyan")
        table.add_column("Source")
        table.add_column("Endpoint")
        table.add_column("Models")
        for provider in providers:
            models = ", ".join(provider.models[:3])
            if len(provider.models) > 3:
                models += f", +{len(provider.models) - 3}"
            table.add_row(provider.label, provider.source or "-", provider.base_url or "-", models or "probe /v1/models")
        self.console.print(table)


def create_tui() -> RichPromptTui:
    return RichPromptTui()


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install Hermes openai-compatible-image plugin")
    parser.add_argument("--source-dir", default=".", help=argparse.SUPPRESS)
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    parser.add_argument("--hermes-python", default=os.environ.get("HOII_HERMES_PYTHON") or os.environ.get("HERMES_PYTHON") or "")
    parser.add_argument("--profile", action="append", default=[], help="Profile name(s). Repeat or comma-separate. Use 'default' for ~/.hermes.")
    parser.add_argument("--profiles", action="append", default=[], help="Alias for --profile; accepts comma-separated names.")
    parser.add_argument("--all-profiles", action="store_true", help="Install into default plus every existing profile under profiles/.")
    parser.add_argument("--create-profile", action="store_true", help="Create missing named profile directories.")
    parser.add_argument("--no-config", action="store_true", help="Copy plugin only; do not edit config.yaml.")
    parser.add_argument("--no-select-provider", action="store_true", help="Enable plugin but leave image_gen.provider unchanged.")
    parser.add_argument("--custom-provider", help="Reuse or create a Hermes provider by name; sets image_gen.provider=custom:<name>.")
    parser.add_argument("--base-url", help="OpenAI-compatible API base URL, usually https://host/v1.")
    parser.add_argument("--api-key", help="API key. Interactive mode stores it in profile .env when --api-key-env is set.")
    parser.add_argument("--api-key-env", help="Environment variable name that contains the API key, e.g. OPENAI_COMPAT_IMAGE_API_KEY.")
    parser.add_argument("--model", help="Default image model for the installed preset, e.g. gpt-image-2.")
    parser.add_argument("--preset", default=DEFAULT_PRESET, help="Preset name to create/select when --model is supplied. Default: auto.")
    parser.add_argument("--size", help="Fallback image size for all aspects, e.g. 1024x1024. Defaults to per-aspect sizes.")
    parser.add_argument("--quality", help="Optional provider-specific quality value added to extra_body.")
    parser.add_argument("--output-format", default="png", help="Optional provider-specific output_format added to extra_body. Default: png.")
    parser.add_argument("--response-format", choices=["b64_json", "url"], help="Request b64_json or url responses. URL responses are downloaded to local cache.")
    parser.add_argument("--timeout", type=int, default=240, help="Request timeout seconds. Default: 240.")
    parser.add_argument("--retry-attempts", type=int, default=3, help="Retry attempts for transient HTTP statuses. Default: 3.")
    parser.add_argument("--retry-backoff", type=float, default=2.0, help="Linear retry backoff seconds. Default: 2.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without writing files.")
    parser.add_argument("--yes", "-y", action="store_true", help="Noninteractive mode; do not open the TUI or prompt before writing.")
    parser.add_argument("--interactive", "-i", action="store_true", help="Force the guided TUI even if all CLI options are supplied.")
    parser.add_argument("--prompt-input", default="", help="Read TUI answers from a file; useful for tests/automation.")
    return parser.parse_args(list(argv) if argv is not None else None)


def split_profiles(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for part in str(raw).split(","):
            name = part.strip()
            if not name:
                continue
            if name not in seen:
                out.append(name)
                seen.add(name)
    return out


def discover_profiles(root: Path) -> list[str]:
    names = ["default"]
    profiles_dir = root / "profiles"
    if profiles_dir.exists():
        for child in sorted(profiles_dir.iterdir()):
            if child.is_dir() and child.name not in names:
                names.append(child.name)
    return names


def profile_home(root: Path, name: str) -> Path:
    return root if name in {"default", "main"} else root / "profiles" / name


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\'", "'").replace("\\$", "$").replace("\\`", "`").replace("\\\\", "\\")


def load_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = _unquote_env_value(raw_value)
    return values


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def ensure_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def ensure_plugin_enabled(cfg: dict[str, Any]) -> None:
    plugins = ensure_dict(cfg, "plugins")
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        enabled = []
        plugins["enabled"] = enabled
    if PLUGIN_REF not in enabled:
        enabled.append(PLUGIN_REF)


def custom_provider_id(value: str | None) -> str:
    raw = (value or "").strip()
    if raw.lower().startswith("custom:"):
        raw = raw.split(":", 1)[1].strip()
    return raw


def sanitize_provider_id(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/", 1)[0].split(":", 1)[0]
    if text.startswith("www."):
        text = text[4:]
    text = _PROVIDER_ID_RE.sub("_", text).strip("_")
    return text or "openai_compatible_image"


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _provider_entry_to_candidate(name: str, entry: dict[str, Any], *, source: str) -> ProviderCandidate:
    display = _first_str(entry.get("display"), entry.get("label"), entry.get("name"), name)
    base_url = _first_str(entry.get("api"), entry.get("base_url"), entry.get("url"))
    key_env = _first_str(entry.get("key_env"), entry.get("api_key_env"), entry.get("env"))
    api_key = _first_str(entry.get("api_key"), entry.get("key"))
    models_raw = entry.get("models") or []
    if isinstance(models_raw, dict):
        values: list[str] = []
        for key, value in models_raw.items():
            if isinstance(value, str) and value.strip():
                values.append(value)
            elif isinstance(key, str) and key.strip():
                values.append(key)
        models = tuple(values)
    elif isinstance(models_raw, (list, tuple)):
        models = tuple(str(v) for v in models_raw if str(v).strip())
    else:
        models = ()
    return ProviderCandidate(
        name=custom_provider_id(name),
        label=display,
        base_url=base_url.rstrip("/"),
        api_key_env=key_env,
        api_key=api_key,
        api_key_source="config" if api_key else "",
        source=source,
        models=models,
    )


def _with_env_secret(candidate: ProviderCandidate, env_values: dict[str, str]) -> ProviderCandidate:
    if candidate.api_key or not candidate.api_key_env:
        return candidate
    if candidate.api_key_env in env_values and env_values[candidate.api_key_env]:
        return replace(candidate, api_key=env_values[candidate.api_key_env], api_key_source="profile_env")
    env_value = os.environ.get(candidate.api_key_env, "")
    return replace(candidate, api_key=env_value, api_key_source="process_env") if env_value else candidate


def provider_candidates_from_config(cfg: dict[str, Any], env_values: dict[str, str] | None = None) -> list[ProviderCandidate]:
    env_values = env_values or {}
    candidates: list[ProviderCandidate] = []
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        for key, entry in providers.items():
            if isinstance(entry, dict):
                candidates.append(_provider_entry_to_candidate(str(key), entry, source="providers"))
    legacy = cfg.get("custom_providers")
    if isinstance(legacy, list):
        for entry in legacy:
            if not isinstance(entry, dict):
                continue
            name = _first_str(entry.get("name"), entry.get("id"), entry.get("provider"))
            if name:
                candidates.append(_provider_entry_to_candidate(name, entry, source="custom_providers"))
    image = cfg.get("image_gen") if isinstance(cfg.get("image_gen"), dict) else {}
    provider_cfg = image.get("openai_compatible_image") if isinstance(image, dict) and isinstance(image.get("openai_compatible_image"), dict) else {}
    if provider_cfg:
        base_url = _first_str(provider_cfg.get("base_url"), provider_cfg.get("api"))
        key_env = _first_str(provider_cfg.get("api_key_env"), provider_cfg.get("key_env"))
        api_key = _first_str(provider_cfg.get("api_key"), provider_cfg.get("key"))
        custom = _first_str(provider_cfg.get("custom_provider"), image.get("provider") if isinstance(image, dict) else "")
        if custom.lower().startswith("custom:"):
            custom = custom.split(":", 1)[1]
        if base_url:
            candidates.append(
                ProviderCandidate(
                    name=custom or sanitize_provider_id(base_url),
                    label=f"Current image_gen ({custom or 'direct'})",
                    base_url=base_url.rstrip("/"),
                    api_key_env=key_env,
                    api_key=api_key,
                    api_key_source="config" if api_key else "",
                    source="image_gen",
                    models=_models_from_image_config(provider_cfg),
                )
            )
    return dedupe_provider_candidates(_with_env_secret(candidate, env_values) for candidate in candidates)


def _models_from_image_config(provider_cfg: dict[str, Any]) -> tuple[str, ...]:
    models: list[str] = []
    seen: set[str] = set()
    presets = provider_cfg.get("presets")
    if isinstance(presets, dict):
        for preset in presets.values():
            if not isinstance(preset, dict):
                continue
            raw = [preset.get("model")]
            pmodels = preset.get("models")
            if isinstance(pmodels, dict):
                raw.extend(pmodels.values())
            for model in raw:
                model_id = str(model or "").strip()
                if model_id and model_id not in seen:
                    seen.add(model_id)
                    models.append(model_id)
    return tuple(models)


def dedupe_provider_candidates(candidates: Iterable[ProviderCandidate]) -> list[ProviderCandidate]:
    merged: dict[str, ProviderCandidate] = {}
    for candidate in candidates:
        key = custom_provider_id(candidate.name).lower()
        if not key:
            continue
        previous = merged.get(key)
        if previous is None:
            merged[key] = candidate
            continue
        models = tuple(dict.fromkeys((*previous.models, *candidate.models)))
        merged[key] = ProviderCandidate(
            name=previous.name or candidate.name,
            label=previous.label or candidate.label,
            base_url=previous.base_url or candidate.base_url,
            api_key_env=previous.api_key_env or candidate.api_key_env,
            api_key=previous.api_key or candidate.api_key,
            api_key_source=previous.api_key_source or candidate.api_key_source,
            source=previous.source if previous.source == candidate.source else f"{previous.source}+{candidate.source}",
            models=models,
        )
    return list(merged.values())


def provider_choices_from_hermes(hermes_python: str | Path | None, hermes_home: Path) -> list[ProviderCandidate]:
    python = str(hermes_python or "").strip()
    if not python:
        return []
    script = """
import json
try:
    from hermes_cli.inventory import build_models_payload, load_picker_context
    rows = build_models_payload(load_picker_context(), max_models=80).get('providers', [])
except Exception:
    try:
        from hermes_cli.model_switch import list_authenticated_providers
        rows = list_authenticated_providers(max_models=80)
    except Exception:
        rows = []
print(json.dumps(rows))
"""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    try:
        completed = subprocess.run(
            [python, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        rows = json.loads(completed.stdout or "[]")
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    candidates: list[ProviderCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = _first_str(row.get("slug"), row.get("id"))
        if not slug:
            continue
        raw_name = custom_provider_id(slug)
        label = _first_str(row.get("name"), raw_name)
        models = tuple(str(model) for model in (row.get("models") or []) if str(model).strip())
        candidates.append(ProviderCandidate(name=raw_name, label=label, source="Hermes inventory", models=models))
    return candidates


def gather_provider_candidates(root: Path, profiles: Sequence[str], hermes_python: str = "") -> list[ProviderCandidate]:
    candidates: list[ProviderCandidate] = []
    for profile in profiles or ["default"]:
        home = profile_home(root, profile)
        candidates.extend(provider_candidates_from_config(load_yaml(home / "config.yaml"), load_env_values(home / ".env")))
    candidates.extend(provider_choices_from_hermes(hermes_python, root))
    return dedupe_provider_candidates(candidates)


def fetch_models_from_v1(provider: ProviderCandidate, timeout: float = 10.0) -> list[str]:
    if not provider.base_url:
        return []
    api_key = provider.resolved_api_key
    url = provider.base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - user-configured endpoint
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    raw_models = payload.get("data") if isinstance(payload, dict) else payload
    models: list[str] = []
    if isinstance(raw_models, list):
        for item in raw_models:
            if isinstance(item, dict):
                model = _first_str(item.get("id"), item.get("name"), item.get("model"))
            else:
                model = str(item or "").strip()
            if model and model not in models:
                models.append(model)
    return models


def quote_env_value(value: str) -> str:
    if value == "":
        return '""'
    if _SAFE_ENV_VALUE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def merge_env_var(existing: str, key: str, value: str) -> str:
    line = f"{key}={quote_env_value(value)}"
    output: list[str] = []
    replaced = False
    for raw_line in existing.splitlines():
        stripped = raw_line.lstrip()
        if not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == key:
            output.append(line)
            replaced = True
        else:
            output.append(raw_line)
    if not replaced:
        if output and output[-1].strip():
            output.append("")
        output.append("# Managed by hermes-openai-compatible-image installer")
        output.append(line)
    return "\n".join(output).rstrip() + "\n"


def write_profile_env(profile_home_path: Path, args: argparse.Namespace, dry_run: bool = False) -> None:
    api_key = (args.api_key or "").strip()
    api_key_env = (args.api_key_env or "").strip()
    if not api_key or not api_key_env:
        return
    env_path = profile_home_path / ".env"
    if dry_run:
        print(f"  env: would update {env_path} ({api_key_env}=<redacted>)")
        return
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(merge_env_var(existing, api_key_env, api_key), encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    print(f"  env: updated {env_path} ({api_key_env}=<redacted>)")


def _select_profiles_interactive(root: Path, ui: Any) -> tuple[list[str], bool]:
    discovered = discover_profiles(root)
    choices = ["all", *discovered]
    selected = ui.select("Target Hermes profile(s)", choices, "all")
    if selected == "all":
        return discovered, True
    return split_profiles([selected]) or ["default"], False


def _default_key_env(provider_name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "_", provider_name).strip("_").upper()
    return f"{clean or 'OPENAI_COMPAT_IMAGE'}_API_KEY"


def _prompt_provider_credentials(candidate: ProviderCandidate, ui: Any) -> tuple[ProviderCandidate, str]:
    api_key_env = candidate.api_key_env
    api_key = candidate.api_key
    entered_key = ""
    if not api_key_env:
        api_key_env = ui.text("API key env var name", _default_key_env(candidate.name))
    if not api_key and api_key_env and not os.environ.get(api_key_env):
        entered_key = ui.password(f"API key for {candidate.label} (stored in profile .env as {api_key_env})")
        api_key = entered_key
    elif api_key and api_key_env and candidate.api_key_source in {"config", "process_env"}:
        # Inline config secrets and parent-shell env secrets work for this process,
        # but they are not a durable profile setup. Persist them to profile .env.
        entered_key = api_key
    return replace(candidate, api_key_env=api_key_env, api_key=api_key), entered_key


def _preferred_model_default(models: Sequence[str], requested: str = "") -> str:
    if requested and requested in models:
        return requested
    for exact in ("gpt-image-2", "gpt-image-1", "dall-e-3", "dall-e-2"):
        if exact in models:
            return exact
    image_markers = ("image", "img", "flux", "kontext", "dall", "sdxl", "stable-diffusion")
    for model in models:
        lower = model.lower()
        if any(marker in lower for marker in image_markers):
            return model
    return models[0] if models else requested


def _select_model(candidate: ProviderCandidate, args: argparse.Namespace, ui: Any) -> str:
    models = list(candidate.models)
    fetched = fetch_models_from_v1(candidate)
    for model in fetched:
        if model not in models:
            models.append(model)
    if models:
        default = _preferred_model_default(models, args.model or "")
        return ui.select("Image model", tuple(models), default)
    return ui.text("Image model (model list unavailable)", args.model or "gpt-image-2")


def apply_interactive_choices(args: argparse.Namespace, ui: Any | None = None) -> list[str]:
    root = Path(args.hermes_home).expanduser().resolve()
    tui = ui or create_tui()
    tui.banner(
        "Hermes OpenAI-compatible image installer",
        "Installs the image_gen plugin, configures provider/model, and writes API keys to profile .env.",
    )
    profiles, all_profiles = _select_profiles_interactive(root, tui)
    args.all_profiles = all_profiles
    args.profile = [] if all_profiles else profiles
    args.profiles = []

    candidates = gather_provider_candidates(root, profiles, getattr(args, "hermes_python", ""))
    if hasattr(tui, "provider_table"):
        tui.provider_table(candidates)
    display_map = {candidate.label: candidate for candidate in candidates}
    choices = tuple([*display_map.keys(), "Manual endpoint"])
    default_choice = next(iter(display_map), "Manual endpoint")
    selected = tui.select("OpenAI-compatible image provider", choices, default_choice)
    if selected == "Manual endpoint":
        base_url = tui.text("OpenAI-compatible base URL", args.base_url or "http://localhost:62173/v1").rstrip("/")
        name = custom_provider_id(args.custom_provider) or sanitize_provider_id(base_url)
        candidate = ProviderCandidate(name=name, label=name, base_url=base_url, source="manual")
    else:
        candidate = display_map[selected]
        if not candidate.base_url:
            base_url = tui.text(f"Base URL for {candidate.label}", args.base_url or "").rstrip("/")
            candidate = replace(candidate, base_url=base_url)

    candidate, entered_key = _prompt_provider_credentials(candidate, tui)
    model = _select_model(candidate, args, tui)

    args.custom_provider = custom_provider_id(candidate.name)
    args.base_url = candidate.base_url or args.base_url
    args.api_key_env = candidate.api_key_env or args.api_key_env
    args.api_key = entered_key or args.api_key
    args.model = model

    if not args.dry_run and not tui.confirm(f"Install into {', '.join(profiles)} under {root}?", True):
        raise SystemExit("Aborted.")
    return profiles


def patch_config(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    ensure_plugin_enabled(cfg)
    image = ensure_dict(cfg, "image_gen")
    provider_cfg = ensure_dict(image, "openai_compatible_image")

    custom_id = custom_provider_id(args.custom_provider)
    if not args.no_select_provider:
        image["provider"] = f"custom:{custom_id}" if custom_id else "openai-compatible-image"
    image["preset"] = args.preset

    provider_cfg["timeout"] = int(args.timeout)
    provider_cfg["retry"] = {
        "max_attempts": max(1, min(int(args.retry_attempts), 5)),
        "backoff_seconds": max(0.0, float(args.retry_backoff)),
        "status_codes": [502, 503, 504],
    }
    if args.response_format:
        provider_cfg["response_format"] = args.response_format
    if custom_id:
        provider_cfg["custom_provider"] = custom_id
    if args.base_url:
        provider_cfg["base_url"] = args.base_url.rstrip("/")
    if args.api_key_env:
        provider_cfg["api_key_env"] = args.api_key_env
        provider_cfg.pop("api_key", None)
    elif args.api_key:
        provider_cfg["api_key"] = args.api_key

    if args.model:
        presets = ensure_dict(provider_cfg, "presets")
        preset = ensure_dict(presets, args.preset)
        preset["display"] = preset.get("display") or f"{args.model} ({args.preset})"
        preset["model"] = args.model
        if args.size:
            preset["size"] = args.size
            preset.pop("sizes", None)
        else:
            preset.setdefault("sizes", dict(DEFAULT_SIZES))
        extra_body = ensure_dict(preset, "extra_body")
        if args.output_format:
            extra_body["output_format"] = args.output_format
        if args.quality:
            extra_body["quality"] = args.quality

    if custom_id:
        providers = ensure_dict(cfg, "providers")
        entry = providers.get(custom_id)
        if not isinstance(entry, dict):
            entry = {}
            providers[custom_id] = entry
        entry.setdefault("name", custom_id)
        if args.base_url:
            entry["api"] = args.base_url.rstrip("/")
        if args.api_key_env:
            entry["key_env"] = args.api_key_env
            entry.pop("api_key", None)
        elif args.api_key:
            entry["api_key"] = args.api_key


def copy_plugin(source_root: Path, profile_home_path: Path, dry_run: bool, copied_targets: set[Path]) -> Path:
    source = source_root / PLUGIN_DIRNAME
    if not (source / "__init__.py").exists():
        raise SystemExit(f"Plugin source not found: {source}")

    image_dir = profile_home_path / "plugins" / "image_gen"
    real_image_dir = image_dir.resolve() if image_dir.exists() else image_dir
    target = real_image_dir / PLUGIN_DIRNAME
    real_target = target.resolve() if target.exists() else target

    if real_target in copied_targets:
        return target
    copied_targets.add(real_target)

    if dry_run:
        print(f"  plugin: would copy {source} -> {target}")
        return target

    real_image_dir.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        backup = target.with_name(f"{target.name}.bak.{time.strftime('%Y%m%d%H%M%S')}")
        target.rename(backup)
        print(f"  plugin: backed up existing {target} -> {backup}")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".DS_Store")
    shutil.copytree(source, target, ignore=ignore)
    print(f"  plugin: installed {target}")
    return target


def prompt_or_abort(args: argparse.Namespace, profiles: list[str], root: Path) -> None:
    if args.dry_run or args.yes or args.interactive:
        return
    raise SystemExit("Refusing to write without --yes. Run interactively without --yes to use the TUI.")


def _load_prompt_stream(path: str):
    if not path:
        return None
    return Path(path).open(encoding="utf-8")  # noqa: SIM115


def _ui_from_prompt_stream(stream) -> Any:
    class LineTui:
        def banner(self, title: str, subtitle: str) -> None:
            print(f"{title}\n{subtitle}")

        def text(self, prompt: str, default: str = "") -> str:
            line = stream.readline()
            if line == "":
                raise EOFError("interactive input ended unexpectedly")
            value = line.strip()
            return value or default

        def password(self, prompt: str) -> str:
            return self.text(prompt, "")

        def confirm(self, prompt: str, default: bool = False) -> bool:
            line = stream.readline()
            if line == "":
                raise EOFError("interactive input ended unexpectedly")
            answer = line.strip().lower()
            if not answer:
                return default
            return answer in {"y", "yes", "1", "true"}

        def select(self, prompt: str, choices: Sequence[str], default: str = "") -> str:
            line = stream.readline()
            if line == "":
                raise EOFError("interactive input ended unexpectedly")
            answer = line.strip()
            return answer or default

        def provider_table(self, providers: Sequence[ProviderCandidate]) -> None:
            return None

    return LineTui()


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    source_root = Path(args.source_dir).resolve()
    root = Path(args.hermes_home).expanduser().resolve()

    prompt_stream = _load_prompt_stream(args.prompt_input)
    try:
        interactive = args.interactive or not args.yes
        if interactive:
            args.interactive = True
            ui = _ui_from_prompt_stream(prompt_stream) if prompt_stream else None
            profiles = apply_interactive_choices(args, ui=ui)
        elif args.all_profiles:
            profiles = discover_profiles(root)
        else:
            profiles = split_profiles(args.profile + args.profiles) or ["default"]
    except (EOFError, TuiDependencyError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        if prompt_stream is not None:
            prompt_stream.close()

    print("Hermes OpenAI-Compatible Image Provider installer")
    print(f"  source: {source_root}")
    print(f"  hermes home: {root}")
    print(f"  profiles: {', '.join(profiles)}")
    if args.model:
        print(f"  image model: {args.model}")
    if args.custom_provider:
        print(f"  provider: custom:{custom_provider_id(args.custom_provider)}")
    if args.api_key_env:
        print(f"  api key env: {args.api_key_env}")
    if args.dry_run:
        print("  mode: dry-run")

    prompt_or_abort(args, profiles, root)

    copied_targets: set[Path] = set()
    for profile in profiles:
        home = profile_home(root, profile)
        if profile not in {"default", "main"} and not home.exists() and not args.create_profile:
            raise SystemExit(f"Profile '{profile}' does not exist at {home}. Use --create-profile to create it.")
        print(f"\n[{profile}] {home}")
        if not args.dry_run:
            home.mkdir(parents=True, exist_ok=True)
        copy_plugin(source_root, home, args.dry_run, copied_targets)
        write_profile_env(home, args, args.dry_run)

        cfg_path = home / "config.yaml"
        if args.no_config:
            print("  config: skipped (--no-config)")
            continue
        cfg = load_yaml(cfg_path)
        patch_config(cfg, args)
        if args.dry_run:
            print(f"  config: would update {cfg_path}")
        else:
            dump_yaml(cfg_path, cfg)
            print(f"  config: updated {cfg_path}")

    print("\nDone. Restart Hermes CLI/gateway sessions for plugin registry changes to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
