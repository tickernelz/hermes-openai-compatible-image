from __future__ import annotations

import base64
import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = ROOT / "openai-compatible-image" / "__init__.py"
INSTALLER = ROOT / "scripts" / "install.py"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png"


def load_installer():
    module_name = f"installer_test_{id(object())}"
    spec = importlib.util.spec_from_file_location(module_name, INSTALLER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_plugin(monkeypatch, tmp_path, cfg=None):
    cfg = cfg or {}
    cache_dir = tmp_path / "cache" / "images"

    image_mod = types.ModuleType("agent.image_gen_provider")

    class ImageGenProvider:
        pass

    def resolve_aspect_ratio(value):
        return value if value in {"landscape", "portrait", "square"} else "landscape"

    def save_b64_image(b64_data, *, prefix="image", extension="png"):
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{prefix}.{extension}"
        path.write_bytes(base64.b64decode(b64_data))
        return path

    def save_url_image(url, *, prefix="image", timeout=60.0, max_bytes=25 * 1024 * 1024):
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{prefix}.png"
        path.write_bytes(PNG_BYTES)
        return path

    def success_response(*, image, model, prompt, aspect_ratio, provider, extra=None):
        return {
            "success": True,
            "image": image,
            "model": model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "provider": provider,
            **(extra or {}),
        }

    def error_response(*, error, error_type, provider, model, prompt, aspect_ratio):
        return {
            "success": False,
            "error": error,
            "error_type": error_type,
            "provider": provider,
            "model": model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
        }

    image_mod.DEFAULT_ASPECT_RATIO = "landscape"
    image_mod.ImageGenProvider = ImageGenProvider
    image_mod.resolve_aspect_ratio = resolve_aspect_ratio
    image_mod.save_b64_image = save_b64_image
    image_mod.save_url_image = save_url_image
    image_mod.success_response = success_response
    image_mod.error_response = error_response

    agent_pkg = types.ModuleType("agent")
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: tmp_path
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_config = types.ModuleType("hermes_cli.config")
    hermes_config.load_config = lambda: cfg
    hermes_config.save_config = lambda new_cfg: cfg.clear() or cfg.update(new_cfg)

    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.image_gen_provider", image_mod)
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)

    module_name = f"openai_compatible_image_test_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_FILE)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_registers_custom_provider_aliases(monkeypatch, tmp_path):
    cfg = {
        "image_gen": {
            "provider": "custom:lokal_sub2api",
            "preset": "auto",
            "openai_compatible_image": {
                "presets": {"auto": {"model": "gpt-image-2", "size": "1024x1024"}}
            },
        },
        "providers": {"lokal_sub2api": {"api": "http://localhost:62173/v1", "api_key": "secret"}},
    }
    mod = load_plugin(monkeypatch, tmp_path, cfg)

    class Ctx:
        def __init__(self):
            self.providers = []
            self.commands = []

        def register_image_gen_provider(self, provider):
            self.providers.append(provider)

        def register_command(self, **kwargs):
            self.commands.append(kwargs)

    ctx = Ctx()
    mod.register(ctx)

    names = {provider.name for provider in ctx.providers}
    assert {"openai-compatible-image", "custom:lokal_sub2api", "lokal_sub2api"} <= names
    assert ctx.commands and ctx.commands[0]["name"] == "image-preset"


def test_custom_provider_credentials_precede_stale_direct_config(monkeypatch, tmp_path):
    cfg = {
        "image_gen": {
            "provider": "custom:foo",
            "openai_compatible_image": {
                "base_url": "https://old.example/v1",
                "api_key": "old-key",
                "presets": {"auto": {"model": "m", "size": "1024x1024"}},
            },
        },
        "providers": {"foo": {"api": "https://new.example/v1", "api_key": "new-key"}},
    }
    mod = load_plugin(monkeypatch, tmp_path, cfg)
    provider = mod.OpenAICompatibleImageProvider(provider_name="custom:foo", custom_provider="foo")

    assert provider._base_url(cfg) == "https://new.example/v1"
    assert provider._api_key(cfg) == "new-key"


def test_env_references_are_resolved(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGE_KEY", "resolved-key")
    cfg = {"image_gen": {"openai_compatible_image": {"api_key": "${IMAGE_KEY}"}}}
    mod = load_plugin(monkeypatch, tmp_path, cfg)
    provider = mod.OpenAICompatibleImageProvider()

    assert provider._api_key(cfg) == "resolved-key"


def test_generate_accepts_url_response_and_saves_local_file(monkeypatch, tmp_path):
    cfg = {
        "image_gen": {
            "provider": "openai-compatible-image",
            "preset": "auto",
            "openai_compatible_image": {
                "base_url": "https://provider.example/v1/images/generations",
                "api_key": "secret",
                "presets": {"auto": {"model": "image-model", "size": "1024x1024", "response_format": "url"}},
            },
        }
    }
    mod = load_plugin(monkeypatch, tmp_path, cfg)
    calls = []

    class Response:
        status_code = 200
        text = "ok"

        def json(self):
            return {"data": [{"url": "https://cdn.example/image.png", "revised_prompt": "clean"}]}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(mod.requests, "post", fake_post)
    provider = mod.OpenAICompatibleImageProvider()
    result = provider.generate("tiny dot", "square")

    assert result["success"] is True
    assert result["provider"] == "openai-compatible-image"
    assert result["output_kind"] == "url"
    assert Path(result["image"]).read_bytes() == PNG_BYTES
    assert calls[0][0] == "https://provider.example/v1/images/generations"
    assert calls[0][1]["json"]["response_format"] == "url"


def test_installer_updates_multiple_profiles(tmp_path):
    home = tmp_path / "hermes"
    (home / "profiles" / "work").mkdir(parents=True)

    result = subprocess.run(
        [
            sys.executable,
            str(INSTALLER),
            "--source-dir",
            str(ROOT),
            "--hermes-home",
            str(home),
            "--profile",
            "default,work",
            "--yes",
            "--custom-provider",
            "foo",
            "--base-url",
            "http://localhost:9999/v1",
            "--api-key-env",
            "FOO_KEY",
            "--model",
            "gpt-image-2",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "[default]" in result.stdout
    assert "[work]" in result.stdout

    for cfg_path in [home / "config.yaml", home / "profiles" / "work" / "config.yaml"]:
        cfg = yaml.safe_load(cfg_path.read_text())
        assert "image_gen/openai-compatible-image" in cfg["plugins"]["enabled"]
        assert cfg["image_gen"]["provider"] == "custom:foo"
        assert cfg["image_gen"]["openai_compatible_image"]["custom_provider"] == "foo"
        assert cfg["providers"]["foo"]["api"] == "http://localhost:9999/v1"
        assert cfg["providers"]["foo"]["key_env"] == "FOO_KEY"
        assert cfg["image_gen"]["openai_compatible_image"]["presets"]["auto"]["model"] == "gpt-image-2"

    assert (home / "plugins" / "image_gen" / "openai-compatible-image" / "__init__.py").exists()
    assert (home / "profiles" / "work" / "plugins" / "image_gen" / "openai-compatible-image" / "__init__.py").exists()


class FakeTui:
    def __init__(self, answers):
        self.answers = list(answers)
        self.events = []

    def _pop(self):
        if not self.answers:
            raise AssertionError("FakeTui ran out of answers")
        return self.answers.pop(0)

    def banner(self, title, subtitle):
        self.events.append(("banner", title, subtitle))

    def text(self, prompt, default=""):
        self.events.append(("text", prompt, default))
        answer = self._pop()
        return default if answer is None else answer

    def password(self, prompt):
        self.events.append(("password", prompt))
        return self._pop()

    def confirm(self, prompt, default=False):
        self.events.append(("confirm", prompt, default))
        answer = self._pop()
        return default if answer is None else bool(answer)

    def select(self, prompt, choices, default=""):
        self.events.append(("select", prompt, tuple(choices), default))
        answer = self._pop()
        return default if answer is None else answer

    def provider_table(self, providers):
        self.events.append(("provider_table", tuple(p.name for p in providers)))


def test_interactive_installer_selects_custom_provider_and_model_from_v1_models(monkeypatch, tmp_path):
    mod = load_installer()
    home = tmp_path / "hermes"
    cfg_path = home / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "lokal_sub2api": {
                        "name": "Local Sub2API",
                        "api": "http://localhost:62173/v1",
                        "key_env": "LOKAL_SUB2API_API_KEY",
                    }
                }
            },
            sort_keys=False,
        )
    )
    monkeypatch.setenv("LOKAL_SUB2API_API_KEY", "secret-from-env")
    monkeypatch.setattr(mod, "fetch_models_from_v1", lambda provider, timeout=10.0: ["gpt-image-2", "flux-kontext"])
    monkeypatch.setattr(mod, "provider_choices_from_hermes", lambda *args, **kwargs: [])

    args = SimpleNamespace(
        hermes_home=str(home),
        source_dir=str(ROOT),
        profile=[],
        profiles=[],
        all_profiles=False,
        create_profile=False,
        no_config=False,
        no_select_provider=False,
        custom_provider=None,
        base_url=None,
        api_key=None,
        api_key_env=None,
        model=None,
        preset="auto",
        size=None,
        quality=None,
        output_format="png",
        response_format=None,
        timeout=240,
        retry_attempts=3,
        retry_backoff=2.0,
        dry_run=False,
        yes=False,
        interactive=True,
        prompt_input="",
        hermes_python="",
    )
    ui = FakeTui([
        "default",          # target profiles
        "Local Sub2API",    # provider
        "gpt-image-2",      # model from /v1/models
        None,               # API key env var default
        None,               # confirm install
    ])

    profiles = mod.apply_interactive_choices(args, ui=ui)

    assert profiles == ["default"]
    assert args.custom_provider == "lokal_sub2api"
    assert args.model == "gpt-image-2"
    assert args.api_key_env == "LOKAL_SUB2API_API_KEY"
    assert args.api_key == "secret-from-env"
    assert any(event[0] == "provider_table" for event in ui.events)
    model_selects = [event for event in ui.events if event[0] == "select" and "model" in event[1].lower()]
    assert model_selects and model_selects[0][2] == ("gpt-image-2", "flux-kontext")
    mod.write_profile_env(home, args)
    assert "LOKAL_SUB2API_API_KEY=secret-from-env" in (home / ".env").read_text()


def test_interactive_installer_prompts_api_key_and_writes_profile_env(monkeypatch, tmp_path):
    mod = load_installer()
    home = tmp_path / "hermes"
    (home / "profiles" / "work").mkdir(parents=True)
    monkeypatch.setattr(mod, "fetch_models_from_v1", lambda provider, timeout=10.0: ["gpt-image-2"])
    monkeypatch.setattr(mod, "provider_choices_from_hermes", lambda *args, **kwargs: [])

    args = SimpleNamespace(
        hermes_home=str(home),
        source_dir=str(ROOT),
        profile=[],
        profiles=[],
        all_profiles=False,
        create_profile=False,
        no_config=False,
        no_select_provider=False,
        custom_provider=None,
        base_url=None,
        api_key=None,
        api_key_env=None,
        model=None,
        preset="auto",
        size=None,
        quality=None,
        output_format="png",
        response_format=None,
        timeout=240,
        retry_attempts=3,
        retry_backoff=2.0,
        dry_run=False,
        yes=False,
        interactive=True,
        prompt_input="",
        hermes_python="",
    )
    ui = FakeTui([
        "work",                          # target profile
        "Manual endpoint",               # provider path
        "https://img.example/v1",         # base URL
        "IMG_API_KEY",                   # env var name
        "super-secret",                  # password
        "gpt-image-2",                   # model from /v1/models
        None,                            # confirm install
    ])

    profiles = mod.apply_interactive_choices(args, ui=ui)

    assert profiles == ["work"]
    assert args.custom_provider == "img_example"
    assert args.base_url == "https://img.example/v1"
    assert args.api_key_env == "IMG_API_KEY"
    assert args.api_key == "super-secret"
    assert args.model == "gpt-image-2"

    mod.write_profile_env(home / "profiles" / "work", args)
    env_text = (home / "profiles" / "work" / ".env").read_text()
    assert "IMG_API_KEY=super-secret" in env_text


def test_interactive_installer_uses_existing_profile_env_for_model_probe(monkeypatch, tmp_path):
    mod = load_installer()
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {"providers": {"foo": {"api": "https://foo.example/v1", "key_env": "FOO_KEY"}}},
            sort_keys=False,
        )
    )
    (home / ".env").write_text('FOO_KEY="from-profile-env"\n')
    seen = {}

    def fake_fetch(provider, timeout=10.0):
        seen["key"] = provider.resolved_api_key
        return ["gpt-image-2"]

    monkeypatch.setattr(mod, "fetch_models_from_v1", fake_fetch)
    monkeypatch.setattr(mod, "provider_choices_from_hermes", lambda *args, **kwargs: [])
    args = SimpleNamespace(
        hermes_home=str(home),
        source_dir=str(ROOT),
        profile=[],
        profiles=[],
        all_profiles=False,
        create_profile=False,
        no_config=False,
        no_select_provider=False,
        custom_provider=None,
        base_url=None,
        api_key=None,
        api_key_env=None,
        model=None,
        preset="auto",
        size=None,
        quality=None,
        output_format="png",
        response_format=None,
        timeout=240,
        retry_attempts=3,
        retry_backoff=2.0,
        dry_run=False,
        yes=False,
        interactive=True,
        prompt_input="",
        hermes_python="",
    )
    ui = FakeTui(["default", "foo", "gpt-image-2", None])

    mod.apply_interactive_choices(args, ui=ui)

    assert seen["key"] == "from-profile-env"
    assert args.api_key is None


def test_yes_mode_stays_noninteractive(tmp_path):
    home = tmp_path / "hermes"
    result = subprocess.run(
        [
            sys.executable,
            str(INSTALLER),
            "--source-dir",
            str(ROOT),
            "--hermes-home",
            str(home),
            "--profile",
            "default",
            "--yes",
            "--custom-provider",
            "foo",
            "--base-url",
            "http://localhost:9999/v1",
            "--api-key-env",
            "FOO_KEY",
            "--model",
            "gpt-image-2",
        ],
        input="",
        text=True,
        capture_output=True,
        check=True,
    )

    assert "OpenAI-compatible image installer" not in result.stdout
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["image_gen"]["provider"] == "custom:foo"
