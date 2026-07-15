from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.errors import AppError
from app.models import (
    CodeGenerationSubmission,
    CodeRepairPatch,
    Defect,
    GlobalInput,
    InputStructureDraft,
    SemanticAudit,
    SubtaskPlanDraft,
    TargetedDefectCheck,
)

_TESTLIB_ROOT = Path(__file__).resolve().parent.parent / "testlib_doc_context"
TESTLIB_GUIDANCE = "\n\n".join(
    (_TESTLIB_ROOT / filename).read_text(encoding="utf-8").strip()
    for filename in ("testlib_generator.md", "testlib_validator.md")
)
TESTLIB_GUIDANCE_BY_TARGET = {
    "generator.cpp": (_TESTLIB_ROOT / "testlib_generator.md")
    .read_text(encoding="utf-8")
    .strip(),
    "validator.cpp": (_TESTLIB_ROOT / "testlib_validator.md")
    .read_text(encoding="utf-8")
    .strip(),
}

AGENT1_PROMPT = (
    "你是独立的 Agent1，只负责 INPUT 规范化。保留题目原文、难度和标程源码；"
    "整理输入说明、输出说明和样例，不得推断或补造事实。输出完整 GlobalInput。"
)
AGENT2_PROMPT = (
    "你是独立的 Agent2，只负责输入结构和结构标签。按标程真实读取顺序描述输入；"
    "标签只能来自给定目录。遇到歧义时在 issues 中输出 needs_tag_review，不得猜测或修复阶段四。"
)
AGENT3_PROMPT = (
    "你是独立的 Agent3，只负责子任务契约。首次生成恰好五个子任务；为每个测试点提供"
    "完整 runtime_parameters。约束、特殊情况、参数和标签必须互相一致，不能把矛盾留给 Agent4。"
)
AGENT4_GENERATE_PROMPT = (
    "你是独立的 Agent4 代码生成器。根据已确认契约和读取到的 jngen 文档生成 generator.cpp "
    "与 validator.cpp。proof_obligations 由后端注入，响应中不要返回；只为每项提交 "
    "implementation_mapping："
    "实现文件、符号、精确行号、实际使用的参数、已读取文档文件和 API 符号、测试构造策略。"
    "document_evidence 只能引用 inputs.context.jngen_documentation.selected_documents "
    "中的 filename 和 symbols，字段只有 filename 与 symbol，不包含 digest；不得把 testlib commit、"
    "候选 revision 或任何截断摘要当作文档证据。validator 的 testlib 接入由后端固定策略验证，"
    "不要伪造其文档证据。"
    "参数必须实际影响数据构造；文档没有出现的 API 禁止使用。validator 必须严格读取并 readEof。"
)
AGENT4_AUDIT_PROMPT = (
    "你是 Agent4 的只读语义审查器。只能输出结构化 defects，绝对禁止返回、改写或建议整份代码。"
    "逐项检查 proof_obligations 与 implementation_mapping 及源码是否一致；自然语言只用于 message，"
    "流程身份必须由 category、target_file、constraint_id、subtask、test_point、error_code 构成。"
    "每个缺陷必须标注 origin：只有修改 generator.cpp 或 validator.cpp 能关闭时才是 candidate；"
    "阶段三/四的约束、特殊情况或 runtime parameters 自相矛盾时必须是 upstream_contract，"
    "不得伪装成可修复的代码缺陷。"
    "context.known_semantic_defects 是必须逐项复验的历史缺陷：仍存在时原样保留其 identity，"
    "已不存在时不要返回；除此之外可报告本次首次发现的语义缺陷。"
)
AGENT4_REPAIR_PROMPT = (
    "你是 Agent4 的定向修复器。只处理 target_defect，返回最小字段补丁；不得处理或发现其他缺陷，"
    "不得重写无关角色。用 rationale 简述补丁为何能关闭目标缺陷；只能使用 repair_documentation "
    "中的目标文档片段。implementation_mapping_upserts 只返回需要新增或更新的约束映射，"
    "后端会按 constraint_id 合并，绝对不要重复返回整张映射表；只有删除错误映射时才填写 "
    "implementation_mapping_remove_ids。代码改动导致行号变化时，必须 upsert 所有受影响映射。"
)
AGENT4_RECHECK_PROMPT = (
    "你是 Agent4 的只读定向复验器。只回答给定 target_defect 是否仍存在；不得开放审查、"
    "不得报告新缺陷、不得修改代码。"
)
JSON_OUTPUT_PROMPT = (
    "必须只返回一个 JSON（json）object，不得输出 Markdown、解释或 JSON 之外的文本。"
    "JSON 的字段、嵌套结构和类型必须严格符合用户消息中的 response_contract。"
)

T = TypeVar("T", bound=BaseModel)


class AgentModel(Protocol):
    async def agent1_normalize(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> GlobalInput: ...

    async def agent2_structure(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> InputStructureDraft: ...

    async def agent3_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft: ...

    async def agent4_generate(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> CodeGenerationSubmission: ...

    async def agent4_audit(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
    ) -> SemanticAudit: ...

    async def agent4_repair(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        target_defect: Defect,
    ) -> CodeRepairPatch: ...

    async def agent4_recheck(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        target_defect: Defect,
        execution: dict[str, Any],
    ) -> TargetedDefectCheck: ...


class OpenAICompatibleAgentModel:
    """One shared HTTP client with explicit, agent-specific operations."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def agent1_normalize(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> GlobalInput:
        return await self._call(
            "agent1.normalize",
            AGENT1_PROMPT,
            GlobalInput,
            {"context": context, "candidate": candidate},
        )

    async def agent2_structure(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> InputStructureDraft:
        return await self._call(
            "agent2.structure",
            AGENT2_PROMPT,
            InputStructureDraft,
            {"context": context, "candidate": candidate},
        )

    async def agent3_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft:
        return await self._call(
            "agent3.plan",
            AGENT3_PROMPT,
            SubtaskPlanDraft,
            {"context": context, "candidate": candidate},
        )

    async def agent4_generate(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> CodeGenerationSubmission:
        return await self._call(
            "agent4.generate",
            AGENT4_GENERATE_PROMPT + "\n\n" + TESTLIB_GUIDANCE,
            CodeGenerationSubmission,
            {"context": context, "candidate": _candidate_for_model(candidate)},
        )

    async def agent4_audit(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
    ) -> SemanticAudit:
        return await self._call(
            "agent4.semantic_audit",
            AGENT4_AUDIT_PROMPT,
            SemanticAudit,
            {
                "context": _without_document_bodies(context),
                "candidate": _candidate_for_model(candidate),
                "execution": _execution_for_model(execution),
            },
        )

    async def agent4_repair(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        target_defect: Defect,
    ) -> CodeRepairPatch:
        targeted_guidance = TESTLIB_GUIDANCE_BY_TARGET.get(
            target_defect.identity.target_file, ""
        )
        return await self._call(
            "agent4.repair",
            AGENT4_REPAIR_PROMPT
            + ("\n\n目标文件的 testlib 说明：\n" + targeted_guidance if targeted_guidance else ""),
            CodeRepairPatch,
            {
                "context": context,
                "candidate": _candidate_for_model(candidate),
                "target_defect": target_defect.model_dump(mode="json"),
            },
        )

    async def agent4_recheck(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        target_defect: Defect,
        execution: dict[str, Any],
    ) -> TargetedDefectCheck:
        return await self._call(
            "agent4.targeted_recheck",
            AGENT4_RECHECK_PROMPT,
            TargetedDefectCheck,
            {
                "context": _without_document_bodies(context),
                "candidate": _candidate_for_model(candidate),
                "target_defect": target_defect.model_dump(mode="json"),
                "execution": _execution_for_model(execution),
            },
        )

    async def _call(
        self,
        operation: str,
        system_prompt: str,
        response_model: type[T],
        inputs: dict[str, Any],
    ) -> T:
        if not self.settings.model_api_key:
            raise AppError(
                "MODEL_NOT_CONFIGURED",
                "MODEL_API_KEY is not configured",
                status_code=503,
            )
        request = {
            "format_version": 2,
            "operation": operation,
            "inputs": inputs,
            "response_contract": response_model.model_json_schema(),
            "output_instructions": (
                "仅输出符合 response_contract 的 JSON object；"
                "例如对象必须使用 {\"字段名\": \"符合契约的值\"} 这种 JSON 形式。"
            ),
        }
        payload = {
            "model": self.settings.model_name,
            "messages": [
                {"role": "system", "content": system_prompt + "\n\n" + JSON_OUTPUT_PROMPT},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": self.settings.model_max_output_tokens,
        }
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.model_timeout_seconds)
        last_error: Exception | None = None
        attempt_failures: list[dict[str, Any]] = []
        for attempt in range(2):
            content: Any = None
            response_metadata: dict[str, Any] = {}
            try:
                response = await self._client.post(
                    f"{self.settings.model_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                choice = body["choices"][0]
                usage = body.get("usage") if isinstance(body, dict) else None
                response_metadata = {
                    "finish_reason": choice.get("finish_reason")
                    if isinstance(choice, dict)
                    else None,
                    "usage": {
                        key: usage.get(key)
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                        if usage.get(key) is not None
                    }
                    if isinstance(usage, dict)
                    else {},
                }
                content = choice["message"]["content"]
                return response_model.model_validate(_parse_json(content))
            except httpx.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                failure = {
                    "operation": operation,
                    "attempt": attempt + 1,
                    "kind": "transport",
                    **_http_error_details(exc),
                }
                attempt_failures.append(failure)
                if attempt == 0 and (
                    status in {408, 429, 500, 502, 503, 504}
                    or isinstance(exc, httpx.RequestError)
                ):
                    last_error = exc
                    continue
                raise AppError(
                    "MODEL_FAILED",
                    "模型服务请求失败，请检查服务地址、网络和模型状态。",
                    status_code=502,
                    details={
                        "operation": operation,
                        **_http_error_details(exc),
                        "attempts": attempt_failures,
                    },
                ) from exc
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                last_error = exc
                failure = _contract_failure_details(
                    operation,
                    attempt + 1,
                    exc,
                    content,
                    response_metadata,
                )
                attempt_failures.append(failure)
                if attempt == 0:
                    if isinstance(content, str):
                        payload["messages"].append(
                            {"role": "assistant", "content": str(content)}
                        )
                    payload["messages"].append(
                        {
                            "role": "user",
                            "content": (
                                "上一条响应未通过 response_contract 校验。请依据下面的精确错误修正"
                                "字段值、类型、缺失项或多余项，并返回完整 JSON object；不要解释，"
                                "不要输出 Markdown。\nvalidation_errors="
                                + json.dumps(failure["errors"], ensure_ascii=False)
                            ),
                        }
                    )
                    continue
        raise AppError(
            "MODEL_FAILED",
            "模型返回的 JSON 未通过响应契约校验。",
            status_code=502,
            details={
                "operation": operation,
                "failure_kind": attempt_failures[-1]["kind"] if attempt_failures else "unknown",
                "attempts": attempt_failures,
                "contract_error": str(last_error)[:800],
            },
        ) from last_error


def _http_error_details(exc: httpx.HTTPError) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    details: dict[str, Any] = {
        "http_status": getattr(response, "status_code", None),
        "error_type": type(exc).__name__,
    }
    if response is None:
        details["provider_message"] = str(exc)[:500]
        return details
    try:
        body = response.json()
    except (ValueError, TypeError):
        text = response.text.strip()
        if text:
            details["provider_message"] = text[:500]
        return details
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        for source, target in (
            ("code", "provider_code"),
            ("type", "provider_type"),
            ("message", "provider_message"),
            ("param", "provider_param"),
        ):
            value = error.get(source)
            if value is not None:
                details[target] = str(value)[:500]
    elif error is not None:
        details["provider_message"] = str(error)[:500]
    return details


def _without_document_bodies(context: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(context)
    documentation = prepared.get("jngen_documentation")
    if isinstance(documentation, dict):
        prepared["jngen_documentation"] = {
            **{key: value for key, value in documentation.items() if key != "selected_documents"},
            "selected_documents": [
                {
                    "filename": item.get("filename"),
                    "digest": item.get("digest"),
                    "symbols": item.get("symbols", []),
                }
                for item in documentation.get("selected_documents", [])
                if isinstance(item, dict)
            ],
        }
    return prepared


def _candidate_for_model(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in (
            "generator_code",
            "validator_code",
            "proof_obligations",
            "implementation_mapping",
        )
        if key in candidate
    }


def _execution_for_model(execution: dict[str, Any]) -> dict[str, Any]:
    gate_counts: dict[str, int] = {}
    failed_checks: list[dict[str, Any]] = []
    for check in execution.get("checks", []):
        if not isinstance(check, dict):
            continue
        operation = str(check.get("operation") or "unknown")
        gate_counts[operation] = gate_counts.get(operation, 0) + 1
        result = check.get("result")
        failed = check.get("ok") is False or (
            isinstance(result, dict) and result.get("ok") is False
        )
        if not failed:
            continue
        evidence = {
            key: value
            for key, value in check.items()
            if key
            in {
                "operation",
                "level",
                "role",
                "constraint_id",
                "target_file",
                "subtask_id",
                "case_id",
                "seed",
                "runtime_arguments",
                "error_code",
                "issues",
                "diagnostics",
            }
        }
        if isinstance(result, dict):
            evidence["result"] = {
                key: (
                    value[:2000]
                    if key in {"stdout", "stderr"} and isinstance(value, str)
                    else value
                )
                for key, value in result.items()
                if key in {"ok", "exit_code", "timed_out", "stdout", "stderr"}
            }
        failed_checks.append(evidence)
    return {
        "ok": execution.get("ok", False),
        "failure_category": execution.get("failure_category"),
        "validation_level": execution.get("validation_level"),
        "message": execution.get("message", ""),
        "gate_counts": gate_counts,
        "failed_checks": failed_checks,
    }


def _parse_json(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        raise TypeError("model output content must be a string")
    text = value.strip()
    if not text:
        raise ValueError("model output content is empty")
    if text.startswith("<think>") and "</think>" in text:
        text = text.split("</think>", 1)[1].strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1])
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("model output must be a JSON object")
    return parsed


def _contract_failure_details(
    operation: str,
    attempt: int,
    error: Exception,
    content: Any,
    response_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(error, ValidationError):
        kind = "schema_validation"
        errors = [
            {
                "type": item.get("type"),
                "location": [str(part) for part in item.get("loc", ())],
                "message": item.get("msg"),
                "input": _bounded_contract_value(item.get("input")),
            }
            for item in error.errors(include_url=False, include_context=False)[:20]
        ]
    elif isinstance(error, json.JSONDecodeError):
        kind = "json_syntax"
        errors = [
            {
                "type": "json_decode_error",
                "location": [str(error.lineno), str(error.colno)],
                "message": error.msg,
            }
        ]
    else:
        kind = (
            "response_envelope"
            if isinstance(error, (KeyError, TypeError))
            else "json_contract"
        )
        errors = [{"type": type(error).__name__, "location": [], "message": str(error)[:500]}]
    raw = content if isinstance(content, str) else ""
    return {
        "operation": operation,
        "attempt": attempt,
        "kind": kind,
        "errors": errors,
        "response_chars": len(raw),
        "response_digest": hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else None,
        "response": response_metadata or {},
    }


def _bounded_contract_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value)
        return value if len(text) <= 160 else text[:157] + "..."
    if isinstance(value, list):
        return [_bounded_contract_value(item) for item in value[:5]]
    if isinstance(value, dict):
        return {
            str(key): _bounded_contract_value(item)
            for key, item in list(value.items())[:8]
        }
    return str(value)[:160]
