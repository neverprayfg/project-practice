from __future__ import annotations

import json
import stat

from conftest import FakeSandbox
from fastapi.testclient import TestClient

from app.services.model_client import OpenAICompatibleAgentModel


def model_payload(**updates: object) -> dict:
    payload = {
        "mode": "remote",
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


def test_mock_connection_can_be_tested_without_external_api(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, _sandbox = app_bundle

    response = client.post(
        "/api/settings/model/test",
        json=model_payload(mode="mock", api_key=None),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "mode": "mock",
        "model_name": "deepseek-chat",
        "latency_ms": 0,
        "message": "Mock 模式无需连接外部模型服务。",
    }


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
