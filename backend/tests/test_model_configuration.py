from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest
from conftest import FakeSandbox
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import AppError
from app.main import create_app
from app.services.model_client import OpenAICompatibleAgentModel
from app.services.model_configuration import ModelConfigurationService
from app.storage import ProjectStorage


def model_payload(**updates: object) -> dict:
    payload = {
        "base_url": "https://api.deepseek.com/v1",
        "model_name": "deepseek-chat",
        "api_key": "sk-test-secret-1234",
        "clear_api_key": False,
        "timeout_seconds": 90,
        "trial_seeds_per_subtask": 2,
    }
    payload.update(updates)
    return payload


def test_model_configuration_is_masked_persisted_and_applied(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, _sandbox = app_bundle

    response = client.put("/api/settings/model", json=model_payload())

    assert response.status_code == 200, response.text
    body = response.json()
    assert "api_key" not in body
    assert body["api_key_configured"] is True
    assert body["api_key_hint"] == "末四位 1234"
    assert isinstance(client.app.state.agent_graphs.model, OpenAICompatibleAgentModel)
    assert all(
        agent.model is client.app.state.agent_graphs.model
        for agent in (
            client.app.state.agent_graphs.agent1,
            client.app.state.agent_graphs.agent2,
            client.app.state.agent_graphs.agent3,
            client.app.state.agent_graphs.agent4,
        )
    )

    path = client.app.state.storage.root / "_system" / "model_config.json"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["api_key"] == "sk-test-secret-1234"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_blank_api_key_keeps_existing_secret(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, _sandbox = app_bundle
    client.put("/api/settings/model", json=model_payload())

    response = client.put(
        "/api/settings/model",
        json=model_payload(api_key=None, model_name="deepseek-reasoner"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["model_name"] == "deepseek-reasoner"
    assert response.json()["api_key_hint"] == "末四位 1234"
    assert client.app.state.settings.model_api_key == "sk-test-secret-1234"


def test_model_configuration_can_clear_api_key_and_disable_ai(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, _sandbox = app_bundle
    client.put("/api/settings/model", json=model_payload())

    response = client.put(
        "/api/settings/model",
        json=model_payload(api_key=None, clear_api_key=True),
    )

    assert response.status_code == 200, response.text
    assert response.json()["api_key_configured"] is False
    assert client.app.state.settings.model_api_key == ""
    path = client.app.state.storage.root / "_system" / "model_config.json"
    assert json.loads(path.read_text(encoding="utf-8"))["api_key"] == ""


def test_app_starts_without_model_configuration(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        storage_root=tmp_path,
        model_api_key="",
    )

    app = create_app(settings, sandbox=FakeSandbox(tmp_path))
    with TestClient(app) as client:
        health = client.get("/health")
        configuration = client.get("/api/settings/model")

    assert health.status_code == 200
    assert health.json()["model_api_configured"] is False
    assert health.json()["active_tasks"] is False
    assert configuration.status_code == 200
    assert configuration.json()["api_key_configured"] is False


def test_invalid_saved_model_configuration_is_discarded(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    path = storage.root / "_system" / "model_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "mode": "remote",
                "base_url": "https://persisted.invalid/v1",
                "model_name": "obsolete-model",
                "api_key": "obsolete-secret",
                "timeout_seconds": 90,
                "max_iterations": 4,
                "trial_seeds_per_subtask": 2,
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        storage_root=storage.root,
        model_base_url="https://current.example/v1",
        model_name="current-model",
        model_api_key="current-secret",
    )

    service = ModelConfigurationService(settings, storage)

    assert service.config.model_name == "current-model"
    assert service.config.api_key == "current-secret"
    assert not path.exists()


@pytest.mark.asyncio
async def test_ai_operation_requires_model_configuration(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    service = ModelConfigurationService(
        Settings(_env_file=None, storage_root=storage.root, model_api_key=""),
        storage,
    )

    with pytest.raises(AppError) as exc_info:
        await service.build_model().agent2_structure({}, {})

    assert exc_info.value.code == "MODEL_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_connection_checks_json_output_capability(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == 64
        assert any("JSON" in message["content"] for message in payload["messages"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    storage = ProjectStorage(tmp_path / "storage")
    settings = Settings(
        _env_file=None,
        storage_root=storage.root,
        model_api_key="test-key",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = ModelConfigurationService(settings, storage, client)
        result = await service.test_connection()

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_connection_reports_provider_error_details(tmp_path: Path) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "invalid_api_key",
                    "message": "API key is invalid",
                    "type": "authentication_error",
                }
            },
        )

    storage = ProjectStorage(tmp_path / "storage")
    settings = Settings(
        _env_file=None,
        storage_root=storage.root,
        model_api_key="test-key",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = ModelConfigurationService(settings, storage, client)
        with pytest.raises(AppError) as exc_info:
            await service.test_connection()

    error = exc_info.value
    assert error.code == "MODEL_PROVIDER_REQUEST_FAILED"
    assert error.details == {
        "http_status": 401,
        "provider_code": "invalid_api_key",
        "provider_type": "authentication_error",
        "provider_message": "API key is invalid",
    }


def test_model_configuration_rejects_busy_pipeline_and_invalid_url(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, _sandbox = app_bundle
    client.app.state.pipeline.has_active_tasks = lambda: True

    health = client.get("/health")
    busy = client.put("/api/settings/model", json=model_payload())
    invalid = client.post(
        "/api/settings/model/test",
        json=model_payload(base_url="file:///tmp/model"),
    )

    assert health.status_code == 200
    assert health.json()["active_tasks"] is True
    assert busy.status_code == 409
    assert busy.json()["code"] == "MODEL_CONFIG_BUSY"
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "INVALID_REQUEST"
