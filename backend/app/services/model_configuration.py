from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.errors import AppError
from app.models import ModelConfigurationUpdate, ModelRuntimeConfiguration
from app.services.model_client import (
    AgentModel,
    OpenAICompatibleAgentModel,
    structured_output_controls,
)
from app.storage import ProjectStorage

logger = logging.getLogger(__name__)


class ModelConfigurationService:
    def __init__(
        self,
        settings: Settings,
        storage: ProjectStorage,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.path = storage.root / "_system" / "model_config.json"
        self.lock = asyncio.Lock()
        self._client = client
        self._config = self._load()
        self._apply_to_settings(self._config)

    @property
    def config(self) -> ModelRuntimeConfiguration:
        return self._config.model_copy(deep=True)

    def build_model(self) -> AgentModel:
        return OpenAICompatibleAgentModel(self.settings)

    def public_view(self) -> dict[str, Any]:
        key = self._config.api_key
        return {
            "base_url": self._config.base_url,
            "model_name": self._config.model_name,
            "api_key_configured": bool(key),
            "api_key_hint": f"末四位 {key[-4:]}" if key else "未配置",
            "timeout_seconds": self._config.timeout_seconds,
            "trial_seeds_per_subtask": self._config.trial_seeds_per_subtask,
        }

    def resolve(self, payload: ModelConfigurationUpdate) -> ModelRuntimeConfiguration:
        current = self._config.model_dump()
        supplied_key = (payload.api_key or "").strip()
        current.update(
            base_url=payload.base_url,
            model_name=payload.model_name,
            timeout_seconds=payload.timeout_seconds,
            trial_seeds_per_subtask=payload.trial_seeds_per_subtask,
        )
        if payload.clear_api_key:
            current["api_key"] = ""
        elif supplied_key:
            current["api_key"] = supplied_key
        return ModelRuntimeConfiguration.model_validate(current)

    def update(self, payload: ModelConfigurationUpdate) -> ModelRuntimeConfiguration:
        config = self.resolve(payload)
        self._persist(config)
        self._config = config
        self._apply_to_settings(config)
        return self.config

    async def test_connection(
        self, payload: ModelConfigurationUpdate | None = None
    ) -> dict[str, Any]:
        config = self.resolve(payload) if payload is not None else self.config
        self._ensure_configured(config, status_code=400)

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=config.timeout_seconds)
        started = time.perf_counter()
        try:
            try:
                response = await client.post(
                    f"{config.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {config.api_key}"},
                    json={
                        "model": config.model_name,
                        "messages": [
                            {
                                "role": "system",
                                "content": "只返回 JSON（json）对象，不要输出解释或 Markdown。",
                            },
                            {
                                "role": "user",
                                "content": '请严格返回这个 JSON 示例：{"ok": true}',
                            },
                        ],
                        "temperature": 0,
                        "max_tokens": 64,
                        **structured_output_controls(),
                    },
                )
            except httpx.RequestError as exc:
                raise AppError(
                    "MODEL_CONNECTION_FAILED",
                    "无法连接模型服务，请检查 API 地址和网络连接。",
                    status_code=502,
                    details={"error_type": type(exc).__name__},
                ) from exc

            if response.is_error:
                details = self._provider_error_details(response)
                provider_message = details.get("provider_message")
                message = f"模型服务拒绝了连接测试请求（HTTP {response.status_code}）"
                if provider_message:
                    message += f"：{provider_message}"
                raise AppError(
                    "MODEL_PROVIDER_REQUEST_FAILED",
                    message,
                    status_code=502,
                    details=details,
                )

            try:
                body = response.json()
                choices = body["choices"]
                choice = choices[0]
                content = choice["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                raise AppError(
                    "MODEL_RESPONSE_INVALID",
                    "模型服务已连接，但返回内容不符合 Chat Completions 协议。",
                    status_code=502,
                ) from exc

            if not isinstance(content, str) or not content.strip():
                finish_reason = choice.get("finish_reason")
                truncated = finish_reason == "length"
                raise AppError(
                    "MODEL_RESPONSE_TRUNCATED" if truncated else "MODEL_RESPONSE_INVALID",
                    (
                        "模型服务已连接，但测试输出达到 token 上限，未生成最终 JSON。"
                        if truncated
                        else "模型服务已连接，但没有返回可验证的最终内容。"
                    ),
                    status_code=502,
                    details={
                        "finish_reason": finish_reason,
                        "reasoning_content_present": bool(
                            choice.get("message", {}).get("reasoning_content")
                        ),
                    },
                )

            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                raise AppError(
                    "MODEL_JSON_OUTPUT_INVALID",
                    "模型服务已连接，但没有返回有效的 JSON Object。",
                    status_code=502,
                ) from exc
            if not isinstance(parsed, dict) or parsed.get("ok") is not True:
                raise AppError(
                    "MODEL_JSON_OUTPUT_INVALID",
                    "模型服务已连接，但返回的 JSON 不符合连接测试契约。",
                    status_code=502,
                )
        except AppError:
            raise
        except Exception as exc:
            logger.exception("Unexpected model connection test failure")
            raise AppError(
                "MODEL_CONNECTION_FAILED",
                "模型连接测试发生未预期错误。",
                status_code=502,
                details={"error_type": type(exc).__name__},
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        return {
            "ok": True,
            "model_name": config.model_name,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": "模型连接测试通过。",
        }

    @staticmethod
    def _provider_error_details(response: httpx.Response) -> dict[str, Any]:
        details: dict[str, Any] = {"http_status": response.status_code}
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            return details
        if not isinstance(body, dict):
            return details
        error = body.get("error", body)
        if not isinstance(error, dict):
            return details
        for source, target in (
            ("code", "provider_code"),
            ("type", "provider_type"),
            ("param", "provider_param"),
            ("message", "provider_message"),
        ):
            value = error.get(source)
            if isinstance(value, (str, int, float, bool)):
                details[target] = str(value)[:500]
        return details

    def _load(self) -> ModelRuntimeConfiguration:
        if self.path.is_file():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                return ModelRuntimeConfiguration.model_validate(value)
            except (OSError, ValidationError, ValueError) as exc:
                logger.warning(
                    "Discarding invalid persisted model configuration: path=%s error=%s",
                    self.path,
                    type(exc).__name__,
                )
                try:
                    self.path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    logger.warning(
                        "Unable to delete invalid model configuration: path=%s error=%s",
                        self.path,
                        type(cleanup_error).__name__,
                    )
        return ModelRuntimeConfiguration(
            base_url=self.settings.model_base_url,
            model_name=self.settings.model_name,
            api_key=self.settings.model_api_key,
            timeout_seconds=self.settings.model_timeout_seconds,
            trial_seeds_per_subtask=self.settings.agent_trial_seeds_per_subtask,
        )

    def _persist(self, config: ModelRuntimeConfiguration) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        ProjectStorage.write_json(self.path, config.model_dump(mode="json"))
        self.path.chmod(0o600)

    def _apply_to_settings(self, config: ModelRuntimeConfiguration) -> None:
        self.settings.model_base_url = config.base_url
        self.settings.model_name = config.model_name
        self.settings.model_api_key = config.api_key
        self.settings.model_timeout_seconds = config.timeout_seconds
        self.settings.agent_trial_seeds_per_subtask = config.trial_seeds_per_subtask

    @staticmethod
    def _ensure_configured(config: ModelRuntimeConfiguration, *, status_code: int = 500) -> None:
        if config.api_key:
            return
        raise AppError(
            "MODEL_NOT_CONFIGURED",
            "未配置模型 API Key，请先在模型设置中完成配置。",
            status_code=status_code,
        )
