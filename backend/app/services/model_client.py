from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.errors import AppError
from app.models import (
    CodeRepairPatch,
    Defect,
    GeneratorGenerationSubmission,
    InputNormalizationDraft,
    InputStructureDraft,
    SemanticAudit,
    SubtaskPlanDraft,
    TargetedDefectCheck,
    ValidatorGenerationSubmission,
)

AGENT1_PROMPT = (
    "你是独立的 Agent1，只负责从原始题目中整理输入说明、输出说明和样例，不得推断或补造事实。"
    "只输出 InputNormalizationDraft；题目原文、难度、标程源码、编译状态、输入结构和 revision "
    "均由后端持有，禁止在响应中返回。"
)
AGENT2_PROMPT = (
    "你是独立的 Agent2，只负责输入结构模板。结合题面和标程，按真实读取顺序清晰描述字段、"
    "类型和重复关系；不要返回结构标签，也不要处理阶段四。"
)
AGENT3_PROMPT = (
    "你是独立的 Agent3，只负责生成阶段四的初始配置。首次生成恰好一个子任务，id 必须为 1；"
    "为每个测试点提供完整 runtime_parameters。规模、分布和构造策略只写入 special_cases 或 "
    "runtime_parameters；不要创建约束清单。特殊情况和参数必须互相一致，"
    "不能把矛盾留给 Agent4。"
)
AGENT3_REVISE_PROMPT = (
    "你是 Agent3 的契约修订器。只修复 inputs.context.validation_issues 明确指出的阶段四问题，"
    "必须保留候选的子任务数量、顺序和 id，以及已经合法的测试点与参数，"
    "不得新增、删除或重做无关内容。"
    "规模、分布和构造策略必须留在 special_cases 或 runtime_parameters，不要创建约束清单。"
    "返回完整 SubtaskPlanDraft；这是唯一一次自动修订，仍不合法时流程会停止。"
)
AGENT4_GENERATOR_PROMPT = (
    "你是 Agent4 的独立 generator.cpp 生成器。本次响应绝对禁止返回 validator.cpp。"
    "结合题意，严格参照 inputs.context.library_context JSON 中唯一提供的 "
    "jngen_context 文档与实例生成 generator.cpp。"
    "inputs.context.input_format_contract 是后端冻结的输入格式：必须原样回显 format_contract_id，"
    "并根据题面、input_template、样例和完整文档自行判断数据结构，按所有 policy 向标准输出写出"
    "一个完整测试点。"
    "同一行相邻 token 必须恰好使用一个 ASCII 空格 U+0020；禁止行首空格、行尾空格、Tab、"
    "模板未要求的空行和 CRLF；必须使用 LF 换行且文件末尾必须有一个换行。"
    "标准输出只能包含测试数据，禁止日志或解释。"
    '必须先调用 registerGen(argc, argv) 和 parseArgs(argc, argv)，再通过 '
    'getOpt("参数名") 读取 runtime_parameters；参数名必须与 runtime_parameters.name '
    "逐字一致，禁止 getOpt(0)、getOpt(1) 等位置参数，也禁止自行缩写或改名；读取值必须实际影响构造。"
    "inputs.context.library_context 的递归 JSON 中 jngen_context 包含 doc 和 example；"
    "同一子目录内的文件"
    "使用 <<<FILE_SEPARATOR>>> 分隔。inputs.context.library_document_manifest 是对应文件清单；"
    "参数必须实际影响数据构造；文档没有出现的 API 禁止使用。"
)
AGENT4_VALIDATOR_PROMPT = (
    "你是 Agent4 的独立 validator.cpp 生成器。本次响应绝对禁止返回 generator.cpp。"
    "参考 inputs.context.library_context JSON 中 testlib validator 的文档和实例生成校验器。"
    "inputs.context.input_format_contract 是与 generator 并行共享且由后端冻结的输入格式："
    "必须原样回显 format_contract_id，根据题面、input_template、样例和完整文档自行判断字段，"
    "严格按 input_template 的顺序读取一个测试点，"
    "不得自行添加、删除或重排字段。必须用 readSpace、readEoln 和 readEof 等 testlib 接口严格"
    "约束格式：同一行相邻 token 恰好一个 ASCII 空格 U+0020，禁止行首/行尾空格、Tab、"
    "模板未要求的空行和 CRLF，要求 LF 换行及文件末尾换行；完成全部约束检查后必须 readEof。"
    "只生成 validator.cpp。"
    "inputs.context.library_context 只提供 validator 角色的 testlib_context，且递归包含 doc 和 "
    "example；不要假设或引用未提供的 jngen 文档。"
)
AGENT4_AUDIT_PROMPT = (
    "你是 Agent4 的只读语义审查器。只能输出结构化 defects，绝对禁止返回、改写或建议整份代码。"
    "根据题面、已确认输入格式、样例、完整文档和实际运行结果检查当前源码；自然语言只用于 message，"
    "流程身份必须由 category、target_file、constraint_id、subtask、test_point、error_code 构成。"
    "只报告能通过修改 generator.cpp 或 validator.cpp 关闭的 candidate 缺陷；"
    "不要审查或退回上游阶段。"
    "context.known_semantic_defects 是必须逐项复验的历史缺陷：仍存在时原样保留其 identity，"
    "已不存在时不要返回；除此之外可报告本次首次发现的语义缺陷。"
    "context.library_contexts 是 generator 与 validator 的严格递归 JSON 文档和实例；"
    "library_document_manifests 是对应证据清单，不得假设存在未提供的文档。"
)
AGENT4_REPAIR_PROMPT = (
    "你是 Agent4 的定向修复器。只处理 target_defect，返回最小字段补丁；不得处理或发现其他缺陷，"
    "不得重写无关角色。用 rationale 简述补丁为何能关闭目标缺陷；"
    "context.required_patch_field 非空时必须返回该源码字段。PARAMETER_NO_EFFECT 必须修改参数读取"
    "或实际构造逻辑。"
    "一次修复响应只能返回 generator_code 或 validator_code 之一，绝对禁止同时返回两者。"
    "context.library_contexts 只包含目标缺陷所需角色，或在目标无法归属单一角色时包含两个角色；"
    "必须以递归 JSON 的完整正文为依据。"
)
AGENT4_RECHECK_PROMPT = (
    "你是 Agent4 的只读定向复验器。只回答给定 target_defect 是否仍存在；不得开放审查、"
    "不得报告新缺陷、不得修改代码。必须基于本次请求 candidate 中的当前源码判断，禁止复用"
    "target_defect 里的旧代码片段。若 still_present=true，evidence 必须给出 target_file、"
    "当前源码中逐字连续存在且足以证明同一缺陷的 code_snippet，以及解释该片段为何仍违反约束的"
    "rationale；若无法提供当前源码证据，必须返回 still_present=false。"
    "context.library_contexts 是目标角色的完整递归 JSON 文档集。"
)
JSON_OUTPUT_PROMPT = (
    "必须只返回一个 JSON（json）object，不得输出 Markdown、解释或 JSON 之外的文本。"
    "JSON 的字段、嵌套结构和类型必须严格符合用户消息中的 response_contract。"
)


def structured_output_controls() -> dict[str, Any]:
    """Disable model reasoning and require JSON for every structured call."""
    return {
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }


T = TypeVar("T", bound=BaseModel)


class AgentModel(Protocol):
    async def agent1_normalize(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> InputNormalizationDraft: ...

    async def agent2_structure(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> InputStructureDraft: ...

    async def agent3_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft: ...

    async def agent3_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft: ...

    async def agent4_generate_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> GeneratorGenerationSubmission: ...

    async def agent4_generate_validator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> ValidatorGenerationSubmission: ...

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
    ) -> InputNormalizationDraft:
        return await self._call(
            "agent1.normalize",
            AGENT1_PROMPT,
            InputNormalizationDraft,
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

    async def agent3_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft:
        return await self._call(
            "agent3.revise",
            AGENT3_REVISE_PROMPT,
            SubtaskPlanDraft,
            {"context": context, "candidate": candidate},
        )

    async def agent4_generate_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> GeneratorGenerationSubmission:
        return await self._call(
            "agent4.generate_generator",
            AGENT4_GENERATOR_PROMPT,
            GeneratorGenerationSubmission,
            {"context": context, "candidate": _candidate_for_model(candidate)},
        )

    async def agent4_generate_validator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> ValidatorGenerationSubmission:
        return await self._call(
            "agent4.generate_validator",
            AGENT4_VALIDATOR_PROMPT,
            ValidatorGenerationSubmission,
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
                "context": context,
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
        return await self._call(
            "agent4.repair",
            AGENT4_REPAIR_PROMPT,
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
                "context": context,
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
                '例如对象必须使用 {"字段名": "符合契约的值"} 这种 JSON 形式。'
            ),
        }
        payload = {
            "model": self.settings.model_name,
            "messages": [
                {"role": "system", "content": system_prompt + "\n\n" + JSON_OUTPUT_PROMPT},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": self.settings.model_max_output_tokens,
            **structured_output_controls(),
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
                message = choice["message"]
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
                    "reasoning_content_present": bool(message.get("reasoning_content"))
                    if isinstance(message, dict)
                    else False,
                }
                content = message["content"]
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
                    status in {408, 429, 500, 502, 503, 504} or isinstance(exc, httpx.RequestError)
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
                truncated = response_metadata.get("finish_reason") == "length"
                if attempt == 0 and not truncated:
                    if isinstance(content, str):
                        payload["messages"].append({"role": "assistant", "content": str(content)})
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
                if truncated:
                    break
        truncated = bool(
            attempt_failures
            and attempt_failures[-1].get("response", {}).get("finish_reason") == "length"
        )
        raise AppError(
            "MODEL_RESPONSE_TRUNCATED" if truncated else "MODEL_FAILED",
            (
                "模型输出达到 token 上限，未形成可验证的最终 JSON。"
                if truncated
                else "模型返回的 JSON 未通过响应契约校验。"
            ),
            status_code=502,
            details={
                "operation": operation,
                "max_tokens": self.settings.model_max_output_tokens,
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


def _candidate_for_model(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in (
            "generator_code",
            "validator_code",
            "format_contract_id",
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
        kind = "response_envelope" if isinstance(error, (KeyError, TypeError)) else "json_contract"
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
        return {str(key): _bounded_contract_value(item) for key, item in list(value.items())[:8]}
    return str(value)[:160]
