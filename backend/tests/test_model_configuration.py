from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from conftest import FakeSandbox
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import AppError
from app.main import create_app
from app.models import TaskType
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
        "max_iterations": 6,
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
    assert body["max_iterations"] == 6
    assert isinstance(client.app.state.agent_runner.model, OpenAICompatibleAgentModel)

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
    assert configuration.status_code == 200
    assert configuration.json()["api_key_configured"] is False


@pytest.mark.asyncio
async def test_ai_operation_requires_model_configuration(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    service = ModelConfigurationService(
        Settings(_env_file=None, storage_root=storage.root, model_api_key=""),
        storage,
    )

    with pytest.raises(AppError) as exc_info:
        await service.build_model().run(
            TaskType.INPUT_STRUCTURE,
            "generate",
            {},
            {},
            {},
            [],
        )

    assert exc_info.value.code == "MODEL_NOT_CONFIGURED"


def test_legacy_remote_configuration_is_migrated(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    path = storage.root / "_system" / "model_config.json"
    legacy = model_payload(mode="remote")
    legacy.pop("clear_api_key")
    ProjectStorage.write_json(path, legacy)

    service = ModelConfigurationService(
        Settings(_env_file=None, storage_root=storage.root, model_api_key=""),
        storage,
    )

    assert service.config.api_key == "sk-test-secret-1234"
    assert "mode" not in service.public_view()


def test_legacy_mock_configuration_is_rejected(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    path = storage.root / "_system" / "model_config.json"
    legacy = model_payload(mode="mock")
    legacy.pop("clear_api_key")
    ProjectStorage.write_json(path, legacy)

    with pytest.raises(AppError) as exc_info:
        ModelConfigurationService(
            Settings(_env_file=None, storage_root=storage.root, model_api_key=""),
            storage,
        )

    assert exc_info.value.code == "MODEL_CONFIG_INVALID"


def test_model_configuration_rejects_busy_pipeline_and_invalid_url(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, _sandbox = app_bundle
    client.app.state.pipeline.has_active_tasks = lambda: True

    busy = client.put("/api/settings/model", json=model_payload())
    invalid = client.post(
        "/api/settings/model/test",
        json=model_payload(base_url="file:///tmp/model"),
    )

    assert busy.status_code == 409
    assert busy.json()["code"] == "MODEL_CONFIG_BUSY"
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "INVALID_REQUEST"
