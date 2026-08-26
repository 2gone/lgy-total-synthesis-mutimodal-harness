from __future__ import annotations

import importlib

import pytest


def _load_modules():
    try:
        return importlib.import_module("harness.config"), importlib.import_module("harness.api")
    except ModuleNotFoundError:
        pytest.fail("Harness configuration and API modules are not implemented yet")


def test_request_payload_cannot_be_switched_to_another_model() -> None:
    """Catches an override path or wrong default that sends paid work to another model."""
    config_module, api_module = _load_modules()
    settings = config_module.HarnessConfig.from_mapping(
        {
            "GEMINI_API_BASE": "https://gateway.example.invalid/v1",
            "GEMINI_API_KEY": "do-not-publish-this-secret",
        }
    )

    payload = api_module.build_chat_payload(settings, "Return JSON.", [])

    assert payload["model"] == "gemini-3.7-flash-high"
    assert payload["temperature"] == 0
    assert payload["top_p"] == 1
    assert payload["stream"] is True


def test_public_metadata_and_repr_do_not_expose_gateway_credentials() -> None:
    """Catches accidental serialization of the bearer key or private gateway URL."""
    config_module, _ = _load_modules()
    secret = "sentinel-private-bearer"
    gateway = "https://private-gateway.example.invalid/v1"
    settings = config_module.HarnessConfig.from_mapping(
        {"GEMINI_API_BASE": gateway, "GEMINI_API_KEY": secret}
    )

    published = repr(settings.public_metadata()) + repr(settings)

    assert secret not in published
    assert gateway not in published
    assert settings.chat_completions_url == gateway + "/chat/completions"


def test_legacy_curl_example_env_is_parsed_without_model_override(tmp_path) -> None:
    """Catches loss of compatibility with the supplied curl-style .env example."""
    config_module, _ = _load_modules()
    env_file = tmp_path / ".env"
    env_file.write_text(
        'curl "https://legacy-gateway.example.invalid/v1/chat/completions" \\\n+'
        '  -H "Authorization: Bearer legacy-secret"\n',
        encoding="utf-8",
    )

    settings = config_module.HarnessConfig.from_file(env_file)

    assert settings.chat_completions_url == (
        "https://legacy-gateway.example.invalid/v1/chat/completions"
    )
    assert settings.model == "gemini-3.7-flash-high"
    assert "legacy-secret" not in repr(settings)
