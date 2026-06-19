#!/usr/bin/env python3
"""Install the Hermes openai-compatible-image plugin into one or more profiles."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install Hermes openai-compatible-image plugin")
    parser.add_argument("--source-dir", default=".", help=argparse.SUPPRESS)
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    parser.add_argument("--profile", action="append", default=[], help="Profile name(s). Repeat or comma-separate. Use 'default' for ~/.hermes.")
    parser.add_argument("--profiles", action="append", default=[], help="Alias for --profile; accepts comma-separated names.")
    parser.add_argument("--all-profiles", action="store_true", help="Install into default plus every existing profile under profiles/.")
    parser.add_argument("--create-profile", action="store_true", help="Create missing named profile directories.")
    parser.add_argument("--no-config", action="store_true", help="Copy plugin only; do not edit config.yaml.")
    parser.add_argument("--no-select-provider", action="store_true", help="Enable plugin but leave image_gen.provider unchanged.")
    parser.add_argument("--custom-provider", help="Reuse an existing Hermes provider by name; sets image_gen.provider=custom:<name>.")
    parser.add_argument("--base-url", help="OpenAI-compatible API base URL, usually https://host/v1.")
    parser.add_argument("--api-key", help="API key to write into config.yaml. Prefer --api-key-env for public/shared configs.")
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
    parser.add_argument("--yes", "-y", action="store_true", help="Do not prompt before writing.")
    return parser.parse_args()


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
        preset.setdefault("display", f"{args.model} ({args.preset})")
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
    if args.dry_run or args.yes:
        return
    if not sys.stdin.isatty():
        tty = None
        try:
            tty = open("/dev/tty", "r", encoding="utf-8")
            answer = input_from_tty(tty, f"Install into {', '.join(profiles)} under {root}? [y/N] ")
        except Exception:
            raise SystemExit("Refusing to write without --yes because no interactive stdin/tty is available.")
        finally:
            if tty:
                tty.close()
    else:
        answer = input(f"Install into {', '.join(profiles)} under {root}? [y/N] ")
    if answer.strip().lower() not in {"y", "yes"}:
        raise SystemExit("Aborted.")


def input_from_tty(tty, prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return tty.readline()


def main() -> int:
    args = parse_args()
    source_root = Path(args.source_dir).resolve()
    root = Path(args.hermes_home).expanduser().resolve()

    if args.all_profiles:
        profiles = discover_profiles(root)
    else:
        profiles = split_profiles(args.profile + args.profiles) or ["default"]

    print("Hermes OpenAI-Compatible Image Provider installer")
    print(f"  source: {source_root}")
    print(f"  hermes home: {root}")
    print(f"  profiles: {', '.join(profiles)}")
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
