![Hermes OpenAI-Compatible Image Provider banner](assets/banner.png)

# Hermes OpenAI-Compatible Image Provider

A Hermes `image_gen` backend for any service that exposes an OpenAI-compatible `POST /v1/images/generations` API. It supports direct endpoint config, Hermes `providers:` reuse, custom-provider aliases like `custom:lokal_sub2api`, named presets, retries, and durable local image cache output.

## Install

Inspect first:

```bash
curl -fsSL https://raw.githubusercontent.com/tickernelz/hermes-openai-compatible-image/main/install.sh | bash -s -- --dry-run
```

Install into the default profile:

```bash
curl -fsSL https://raw.githubusercontent.com/tickernelz/hermes-openai-compatible-image/main/install.sh | bash -s -- --yes
```

Install into every existing profile and reuse a Hermes custom provider:

```bash
curl -fsSL https://raw.githubusercontent.com/tickernelz/hermes-openai-compatible-image/main/install.sh | bash -s -- \
  --yes --all-profiles \
  --custom-provider lokal_sub2api \
  --model gpt-image-2
```

Install specific profiles:

```bash
curl -fsSL https://raw.githubusercontent.com/tickernelz/hermes-openai-compatible-image/main/install.sh | bash -s -- \
  --yes --profile default,work --profile lab \
  --base-url https://provider.example/v1 \
  --api-key-env OPENAI_COMPAT_IMAGE_API_KEY \
  --model provider/image-model
```

Restart Hermes CLI/gateway sessions after install so the plugin registry reloads.

## Config shapes

Direct endpoint:

```yaml
plugins:
  enabled:
    - image_gen/openai-compatible-image

image_gen:
  provider: openai-compatible-image
  preset: auto
  openai_compatible_image:
    base_url: https://provider.example/v1
    api_key_env: OPENAI_COMPAT_IMAGE_API_KEY
    presets:
      auto:
        model: provider/image-model
        sizes:
          landscape: 1536x1024
          portrait: 1024x1536
          square: 1024x1024
```

Reuse an existing Hermes provider:

```yaml
image_gen:
  provider: custom:lokal_sub2api
  preset: auto
  openai_compatible_image:
    custom_provider: lokal_sub2api
    presets:
      auto:
        model: gpt-image-2
        size: 1024x1024

providers:
  lokal_sub2api:
    api: http://localhost:62173/v1
    key_env: LOKAL_SUB2API_KEY
```

## Features

- Registers `openai-compatible-image` plus configured custom aliases (`custom:<name>` and `<name>`).
- Resolves credentials from env overrides, `api_key_env`, `providers:`, legacy `custom_providers:`, or direct image config.
- Supports `b64_json`, data-URL base64, and URL responses; URL outputs are downloaded into Hermes cache.
- Preset-driven model/size/extra body config, including per-aspect `landscape`, `portrait`, and `square` routing.
- Retries transient HTTP `502/503/504` failures without retrying auth/client errors.
- Adds `/image_preset` for listing and switching presets globally or per session.

## Verify

```bash
python3 -m py_compile ~/.hermes/plugins/image_gen/openai-compatible-image/__init__.py
hermes chat -q 'Generate a tiny blue dot on a white background. Use image generation.' --toolsets image_gen --quiet
```

For a named profile:

```bash
hermes -p work chat -q 'Generate a tiny blue dot on a white background. Use image generation.' --toolsets image_gen --quiet
```

Generated images are saved under `$HERMES_HOME/cache/images/`.

## Local development

```bash
python -m pytest -q
python -m py_compile openai-compatible-image/__init__.py scripts/install.py
bash -n install.sh
git diff --check
```

See [`openai-compatible-image/config.example.yaml`](openai-compatible-image/config.example.yaml) for a fuller config fragment.
