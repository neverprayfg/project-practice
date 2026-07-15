from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.errors import AppError
from app.models import (
    CodeDraft,
    Confirmation,
    GlobalInput,
    InputStructureDraft,
    JngenDocumentChoice,
    JngenDocumentSelection,
    SubtaskPlanDraft,
    TaskType,
    WorkflowOutput,
)
from app.services.jngen_policy import jngen_usage_issues

_TESTLIB_CONTEXT_ROOT = Path(__file__).resolve().parent.parent / "testlib_doc_context"
TESTLIB_SYSTEM_GUIDANCE = "\n\n".join(
    (_TESTLIB_CONTEXT_ROOT / filename).read_text(encoding="utf-8").strip()
    for filename in ("testlib_generator.md", "testlib_validator.md")
)

_STRING = {"type": "string"}
_NON_EMPTY_STRING = {"type": "string", "minLength": 1}
TASK_RESULT_CONTRACTS: dict[TaskType, dict[str, Any]] = {
    TaskType.INPUT_NORMALIZATION: {
        "type": "object",
        "additionalProperties": False,
        "required": ["problem", "solution", "input_structure", "revision"],
        "properties": {
            "problem": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "description",
                    "input_description",
                    "output_description",
                    "samples",
                    "difficulty",
                ],
                "properties": {
                    "description": _NON_EMPTY_STRING,
                    "input_description": _STRING,
                    "output_description": _STRING,
                    "samples": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["input", "output", "note"],
                            "properties": {
                                "input": _STRING,
                                "output": _STRING,
                                "note": _STRING,
                            },
                        },
                    },
                    "difficulty": _NON_EMPTY_STRING,
                },
            },
            "solution": {
                "type": "object",
                "additionalProperties": False,
                "required": ["language", "source", "compile"],
                "properties": {
                    "language": {"const": "cpp"},
                    "source": _NON_EMPTY_STRING,
                    "compile": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["status", "log"],
                        "properties": {
                            "status": {"enum": ["pending", "passed", "failed"]},
                            "log": _STRING,
                        },
                    },
                },
            },
            "input_structure": {
                "type": "object",
                "additionalProperties": False,
                "required": ["template", "status", "revision"],
                "properties": {
                    "template": _STRING,
                    "status": {"enum": ["pending", "draft", "confirmed"]},
                    "revision": {"type": "integer", "minimum": 0},
                },
            },
            "revision": {"type": "integer", "minimum": 1},
        },
    },
    TaskType.INPUT_STRUCTURE: {
        "type": "object",
        "additionalProperties": False,
        "required": ["template"],
        "properties": {"template": _NON_EMPTY_STRING},
    },
    TaskType.SUBTASK_PLAN: {
        "type": "object",
        "additionalProperties": False,
        "required": ["subtasks"],
        "properties": {
            "subtasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "constraints",
                        "test_count",
                        "expected_complexity",
                        "special_cases",
                    ],
                    "properties": {
                        "id": {"type": "integer", "minimum": 1},
                        "constraints": _NON_EMPTY_STRING,
                        "test_count": {"type": "integer", "minimum": 1},
                        "expected_complexity": _NON_EMPTY_STRING,
                        "special_cases": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["count", "description"],
                                "properties": {
                                    "count": {"type": "integer", "minimum": 1},
                                    "description": _NON_EMPTY_STRING,
                                },
                            },
                        },
                    },
                },
            }
        },
    },
    TaskType.CODE_DRAFT: {
        "type": "object",
        "additionalProperties": False,
        "required": ["generator_code", "validator_code"],
        "properties": {
            "generator_code": _NON_EMPTY_STRING,
            "validator_code": _NON_EMPTY_STRING,
            "revision_id": {"type": ["string", "null"]},
            "input_revision": {
                "type": ["integer", "null"],
                "minimum": 1,
            },
            "subtasks_revision": {
                "type": ["integer", "null"],
                "minimum": 1,
            },
            "trial_results": {
                "type": "array",
                "items": {"type": "object"},
            },
        },
    },
}

TASK_GUIDANCE = {
    TaskType.INPUT_NORMALIZATION: (
        "你是 Agent1。整理 INPUT.problem 中的输入说明、输出说明和样例，但必须保留题目原文、"
        "难度和标程源码，不得改变题意或臆造缺失事实。输出完整 GlobalInput JSON。"
    ),
    TaskType.INPUT_STRUCTURE: (
        "你是 Agent2。综合 INPUT 中的题面和标程读取逻辑，输出 result.template，一段按实际"
        "读取顺序编写的中文输入结构描述。使用一致变量名，覆盖类型、重复、分组、数量和依赖。"
    ),
    TaskType.SUBTASK_PLAN: (
        "你是 Agent3。首次规划时自动生成 5 个适合当前题目的子任务；用户已有列表时审核并"
        "修订该列表。每项包含 id、自由文本 constraints、正整数 test_count、"
        "expected_complexity 和 special_cases。接受自然语言、不等式、区间、键值与分行约束。"
        "result 必须是对象 {\"subtasks\": [...]}，不得直接把子任务数组作为 result。"
    ),
    TaskType.CODE_DRAFT: (
        "你是 Agent4。根据 INPUT 与全部 SUBTASKS 同时输出 generator_code 和 validator_code。"
        "你必须综合题面、输入结构和子任务。context.jngen_documentation"
        ".selected_documents 是在生成前经多轮结构化选择和阅读得到的 jngen 文档，必须以其正文为准，"
        "不得再请求任何工具。generator 必须包含 jngen.h、实际使用已选文档中的 jngen "
        "接口，并接受 -seed 与 -subtask；"
        "validator 使用 testlib 严格匹配输入结构。根据编译、试运行、校验和标程日志持续修复。"
        "selected_documents 是 jngen API 的唯一事实来源。自身经验、训练记忆和名称相似性都不能"
        "作为接口存在或签名正确的依据。只有当文档正文明确给出符号与兼容调用方式时才能使用；"
        "证据不足时应改用文档明确支持的更基础写法，不得把猜测与文档内容融合。"
        "确定性检查通过只代表代码可编译、样例格式可读且标程能运行，不代表子任务语义正确。"
        "在 review 阶段必须逐项审计每个 SUBTASK 的 constraints、test_count 和 special_cases："
        "要求必须由生成算法的构造逻辑保证，不能依赖随机碰巧满足，也不能擅自缩小或改写约束。"
        "必须检查所有边界取值下随机区间和计数关系均合法，输出声明的数量必须与实际记录数一致。"
        "execution.checks 中的 generate.content 是实际样例证据，但单个样例不能替代对生成器源码的"
        "逻辑证明。任何仅用于满足接口检查、其结果不影响最终输出的 jngen 调用都属于实质错误，"
        "必须删除并改为让文档支持的接口真正参与数据构造。"
        "修复时必须以 execution.checks 中的结构化诊断为准，只改动导致失败的最小代码范围，"
        "不得重写无关且已经通过检查的部分。"
        "生成代码至少包含 testlib.h 或 jngen.h。"
    ),
}


class AgentModel(Protocol):
    async def select_jngen_documents(
        self,
        context: dict[str, Any],
        available_filenames: list[str],
    ) -> JngenDocumentSelection: ...

    async def run(
        self,
        task_type: TaskType,
        phase: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
        issues: list[str],
    ) -> WorkflowOutput: ...


class OpenAICompatibleAgentModel:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    @staticmethod
    def _workflow_response_contract(task_type: TaskType) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["confirmation", "result", "issues"],
            "properties": {
                "confirmation": {"enum": ["pass", "revise", "error"]},
                "result": TASK_RESULT_CONTRACTS[task_type],
                "issues": {"type": "array", "items": {"type": "string"}},
            },
        }

    async def select_jngen_documents(
        self,
        context: dict[str, Any],
        available_filenames: list[str],
    ) -> JngenDocumentSelection:
        if not self.settings.model_api_key:
            raise AppError(
                "MODEL_NOT_CONFIGURED",
                "MODEL_API_KEY is not configured",
                status_code=503,
            )
        selection_state = context.get("jngen_document_selection", {})
        if not isinstance(selection_state, dict):
            selection_state = {}
        round_index = int(selection_state.get("round", 1))
        previously_selected = [
            item
            for item in context.get("jngen_documentation", {}).get(
                "selected_documents", []
            )
            if isinstance(item, dict) and item.get("filename")
        ]
        selected_filenames = {
            str(item["filename"])
            for item in previously_selected
        }
        remaining_filenames = [
            filename
            for filename in available_filenames
            if filename not in selected_filenames
        ]
        document_catalog = context.get("jngen_document_catalog", [])
        if not isinstance(document_catalog, list):
            document_catalog = []
        remaining_documents = [
            item
            for item in document_catalog
            if isinstance(item, dict) and item.get("filename") in remaining_filenames
        ] or [{"filename": filename} for filename in remaining_filenames]
        must_select_document = round_index == 1 and not previously_selected
        system = (
            "你是 Agent4 在代码生成前使用的 jngen 文档检索选择器。你只负责本轮选择文档，"
            "绝不生成代码或 API 调用。每轮都会提供全部文件名、已阅读的正文和尚未阅读的文件名。"
            "根据 INPUT、已确认的输入结构、SUBTASKS 和恢复反馈，从尚未阅读的文件中选择本轮最有"
            "信息价值的少量文档；不要试图一次选全，不要重复已读文件。后续轮会根据已读正文继续"
            "补选。若提供 content_chars，选择后的总正文不得超过 maximum_context_characters。"
            "若已读资料足够或没有相关文件，返回空 selected_documents 并把 selection_complete"
            "设为 true。不得臆造文件名，不得输出 Markdown，只返回 JSON 对象。"
        )
        selection_input = {
            "format_version": 1,
            "request": {
                "task_type": "jngen_document_selection",
                "phase": "select_documents_before_generation",
                "round": round_index,
            },
            "inputs": {
                "input": context.get("input", {}),
                "subtasks": context.get("subtasks", []),
                "recovery_feedback": context.get("recovery_feedback", []),
                "maximum_context_characters": selection_state.get(
                    "maximum_context_characters"
                ),
                "previously_selected_documents": previously_selected,
                "all_available_documents": document_catalog
                or [{"filename": filename} for filename in available_filenames],
                "remaining_documents": remaining_documents,
            },
            "response_contract": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "selected_documents": {
                        "type": "array",
                        "minItems": 1 if must_select_document else 0,
                        "maxItems": min(
                            self.settings.agent_jngen_documents_per_round,
                            len(remaining_filenames),
                        ),
                        "uniqueItems": True,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["filename", "reason"],
                            "properties": {
                                "filename": {
                                    "enum": remaining_filenames or available_filenames
                                },
                                "reason": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                    "selection_complete": {"type": "boolean"},
                },
                "required": ["selected_documents", "selection_complete"],
            },
            "response_example": {
                "selected_documents": (
                    [
                        {
                            "filename": remaining_filenames[0],
                            "reason": "根据当前题目需求选择该文档。",
                        }
                    ]
                    if remaining_filenames
                    else []
                ),
                "selection_complete": not bool(remaining_filenames),
            },
        }
        payload = {
            "model": self.settings.model_name,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(selection_input, ensure_ascii=False),
                },
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.settings.model_timeout_seconds)
        try:
            last_contract_error: Exception | None = None
            last_content = ""
            for attempt in range(2):
                try:
                    response = await client.post(
                        f"{self.settings.model_base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise AppError(
                        "MODEL_FAILED",
                        "jngen 文档选择请求失败。",
                        stage=5,
                        status_code=502,
                        details={
                            "http_status": getattr(
                                getattr(exc, "response", None), "status_code", None
                            )
                        },
                    ) from exc
                content = ""
                try:
                    content = str(response.json()["choices"][0]["message"]["content"] or "")
                    last_content = content
                    parsed = self._parse_json(content)
                    # Compatible providers occasionally omit this advisory
                    # boolean.  Empty selection has an unambiguous meaning;
                    # otherwise the runner's bounded retrieval budget decides.
                    parsed.setdefault(
                        "selection_complete",
                        not bool(parsed.get("selected_documents")),
                    )
                    selection = JngenDocumentSelection.model_validate(parsed)
                    invalid = [
                        item.filename
                        for item in selection.selected_documents
                        if item.filename not in remaining_filenames
                    ]
                    if invalid:
                        raise ValueError(
                            f"filenames are not available for this round: {invalid}"
                        )
                    return selection
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    last_contract_error = exc
                    if attempt == 0 and content:
                        payload["messages"].extend(
                            [
                                {"role": "assistant", "content": content},
                                {
                                    "role": "user",
                                    "content": (
                                        "上一条文档选择不符合 JSON 契约或包含候选列表外"
                                        "的文件名。请返回 selected_documents 数组和 "
                                        "selection_complete 布尔值；只可使用 remaining_documents "
                                        "中的文件名，不要解释。"
                                    ),
                                },
                            ]
                        )
                        continue
                    break
            raise AppError(
                "MODEL_FAILED",
                "模型连续返回了无效的 jngen 文档选择。",
                stage=5,
                status_code=502,
                details={
                    "contract_error": str(last_contract_error)[:800],
                    "response_preview": last_content[:1200],
                },
            ) from last_contract_error
        finally:
            if owns_client:
                await client.aclose()

    async def run(
        self,
        task_type: TaskType,
        phase: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
        issues: list[str],
    ) -> WorkflowOutput:
        if not self.settings.model_api_key:
            raise AppError(
                "MODEL_NOT_CONFIGURED",
                "MODEL_API_KEY is not configured",
                status_code=503,
            )
        system = self._system_prompt(task_type, phase)
        model_context = dict(context)
        model_context.pop("library_guidance", None)
        model_input = {
            "format_version": 1,
            "request": {
                "task_type": task_type.value,
                "phase": phase,
            },
            "inputs": {
                "context": model_context,
                "candidate": candidate,
                "execution": execution,
                "previous_issues": issues,
            },
            "response_contract": self._workflow_response_contract(task_type),
        }
        payload = {
            "model": self.settings.model_name,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(model_input, ensure_ascii=False),
                },
            ],
            "temperature": 0.0 if task_type == TaskType.CODE_DRAFT else 0.2,
            "response_format": {"type": "json_object"},
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.settings.model_timeout_seconds)
        try:
            last_contract_error: Exception | None = None
            for attempt in range(2):
                try:
                    response = await client.post(
                        f"{self.settings.model_base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise AppError(
                        "MODEL_FAILED",
                        "模型服务请求失败，请检查服务地址、网络和模型状态。",
                        status_code=502,
                        details={
                            "http_status": getattr(
                                getattr(exc, "response", None), "status_code", None
                            )
                        },
                    ) from exc

                content = ""
                try:
                    content = response.json()["choices"][0]["message"]["content"]
                    value = self._parse_json(content)
                    value = self._normalize_contract(task_type, value)
                    value["issues"] = self._normalize_issues(value.get("issues"))
                    self._validate_task_result(task_type, value.get("result"))
                    return WorkflowOutput.model_validate(value)
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    last_contract_error = exc
                    if attempt == 0 and content:
                        payload["messages"].extend(
                            [
                                {"role": "assistant", "content": content},
                                {
                                    "role": "user",
                                    "content": (
                                        "上一条响应不符合 response_contract，可能是 JSON 语法、"
                                        "顶层包装或 result 必填字段错误。请仅按 Schema 修正"
                                        "结构并保留候选语义，不要解释、不要输出 Markdown。"
                                    ),
                                },
                            ]
                        )
                        continue
                    break
            raise AppError(
                "MODEL_FAILED",
                "模型连续返回了无法解析的 JSON 契约。",
                status_code=502,
                details={"contract_error": str(last_contract_error)[:800]},
            ) from last_contract_error
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _system_prompt(task_type: TaskType, phase: str) -> str:
        phase_rule = (
            "生成或修订候选结果。"
            if phase == "generate"
            else (
                "独立检查候选、执行结果与完成条件；发现实质问题时可直接修复候选。"
                "若确定性检查已经通过且没有实质问题，必须原样返回 candidate 并确认通过，"
                "不得改写措辞、补充默认字段或调整格式。"
            )
        )
        tool_rule = (
            "你没有 Shell、文件、网络、Docker 或工具调用权限，"
            "不得输出 tool_requests 字段。"
        )
        prompt = (
            f"{TASK_GUIDANCE[task_type]} {phase_rule} "
            f"{tool_rule} "
            "严格按用户消息中的 response_contract 返回单个 JSON 对象，且仅包含 "
            "confirmation、result、issues 三个顶层字段。"
            "confirmation 为 pass/revise/error，result 必须是符合当前任务 Schema "
            "的完整候选对象，issues 为中文字符串数组。"
            "仅当任务确实完成、执行检查"
            "通过且 issues 为空时返回 pass；不得因达到重试次数而伪造通过。JSON 字符串中的"
            "换行、引号和反斜杠必须正确转义，不得输出 Markdown 代码块。"
        )
        if task_type == TaskType.CODE_DRAFT:
            prompt += "\n\n以下 testlib 说明为必须遵守的系统级上下文：\n" + TESTLIB_SYSTEM_GUIDANCE
        return prompt

    @staticmethod
    def _parse_json(value: str) -> dict[str, Any]:
        text = value.strip()
        if text.startswith("<think>") and "</think>" in text:
            text = text.split("</think>", 1)[1].strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1])
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("model output must be a JSON object")
        return parsed

    @staticmethod
    def _normalize_contract(task_type: TaskType, value: dict[str, Any]) -> dict[str, Any]:
        value = dict(value)
        legacy_tool_requests = value.pop("tool_requests", [])
        if legacy_tool_requests:
            raise ValueError("tool requests are disabled for every agent task")
        result = value.get("result")
        if task_type == TaskType.SUBTASK_PLAN and isinstance(result, list):
            result = {"subtasks": result}
        if task_type not in {
            TaskType.INPUT_STRUCTURE,
            TaskType.SUBTASK_PLAN,
            TaskType.CODE_DRAFT,
        } or not isinstance(result, dict):
            return value

        normalized_result = dict(result)
        nested_issues = normalized_result.pop("issues", [])
        top_level_issues = value.get("issues", [])
        if isinstance(top_level_issues, str):
            top_level_issues = [top_level_issues]
        if isinstance(nested_issues, str):
            nested_issues = [nested_issues]
        return {
            **value,
            "result": normalized_result,
            "issues": [*(top_level_issues or []), *(nested_issues or [])],
        }

    @staticmethod
    def _normalize_issues(value: Any) -> list[str]:
        raw_issues = [value] if isinstance(value, str) else (value or [])
        issues = [str(item).strip() for item in raw_issues if str(item).strip()]
        return [
            issue if re.search(r"[\u4e00-\u9fff]", issue) else "模型问题说明不是中文，请重新生成。"
            for issue in issues
        ]

    @staticmethod
    def _validate_task_result(task_type: TaskType, value: Any) -> None:
        if not isinstance(value, dict):
            raise ValueError("workflow result must be an object")
        if task_type == TaskType.INPUT_NORMALIZATION:
            GlobalInput.model_validate(value.get("input", value))
        elif task_type == TaskType.INPUT_STRUCTURE:
            InputStructureDraft.model_validate(value)
        elif task_type == TaskType.SUBTASK_PLAN:
            SubtaskPlanDraft.model_validate(value)
        elif task_type == TaskType.CODE_DRAFT:
            CodeDraft.model_validate(value)


class MockAgentModel:
    """Deterministic model used only by tests and explicit mock mode."""

    async def select_jngen_documents(
        self,
        context: dict[str, Any],
        available_filenames: list[str],
    ) -> JngenDocumentSelection:
        previously_selected = {
            str(item["filename"])
            for item in context.get("jngen_documentation", {}).get(
                "selected_documents", []
            )
            if isinstance(item, dict) and item.get("filename")
        }
        preferred = ["getting_started.md", "getopt.md", "array.md"]
        selected = [
            filename
            for filename in preferred
            if filename in available_filenames and filename not in previously_selected
        ][:2]
        if not selected:
            selected = [
                filename
                for filename in available_filenames
                if filename not in previously_selected
            ][:2]
        return JngenDocumentSelection(
            selected_documents=[
                JngenDocumentChoice(filename=filename, reason="模拟模式固定选择。")
                for filename in selected
            ],
            selection_complete=bool(previously_selected) or not selected,
        )

    async def run(
        self,
        task_type: TaskType,
        phase: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
        issues: list[str],
    ) -> WorkflowOutput:
        del issues
        if phase == "review":
            if execution.get("ok"):
                return WorkflowOutput(confirmation=Confirmation.PASS, result=candidate)
            return WorkflowOutput(
                confirmation=Confirmation.REVISE,
                result=candidate,
                issues=[str(execution.get("message") or "确定性检查未通过。")],
            )
        if task_type == TaskType.CODE_DRAFT and (
            not candidate or jngen_usage_issues(candidate.get("generator_code", ""))
        ):
            candidate = self._mock_jngen_result()
        result = candidate or self._default_result(task_type, context)
        return WorkflowOutput(confirmation=Confirmation.REVISE, result=result)

    @staticmethod
    def _mock_jngen_result() -> dict[str, Any]:
        return {
            "generator_code": (
                '#include "jngen.h"\n'
                "int main(int argc,char** argv){registerGen(argc,argv);parseArgs(argc,argv);"
                "auto sample=Array::random(1,0,0);(void)sample;return 0;}"
            ),
            "validator_code": (
                '#include "testlib.h"\n'
                "int main(int argc,char** argv){registerValidation(argc,argv);"
                "inf.readEof();return 0;}"
            ),
            "issues": [],
        }

    @staticmethod
    def _default_result(task_type: TaskType, context: dict[str, Any]) -> dict[str, Any]:
        if task_type == TaskType.INPUT_NORMALIZATION:
            return dict(context["input"])
        if task_type == TaskType.INPUT_STRUCTURE:
            return {
                "template": "按题目与标程的读取顺序读取输入；请审核变量、类型和数量关系。",
                "issues": [],
            }
        if task_type == TaskType.SUBTASK_PLAN:
            return {
                "subtasks": [
                    {
                        "id": index,
                        "constraints": f"第 {index} 个规模梯度，按 INPUT 中变量描述。",
                        "test_count": 1,
                        "expected_complexity": "O(n)",
                        "special_cases": [],
                    }
                    for index in range(1, 6)
                ],
                "issues": [],
            }
        return {
            "generator_code": (
                '#include "testlib.h"\n'
                "int main(int argc,char** argv){registerGen(argc,argv,1);return 0;}"
            ),
            "validator_code": (
                '#include "testlib.h"\n'
                "int main(int argc,char** argv){registerValidation(argc,argv);"
                "inf.readEof();return 0;}"
            ),
            "issues": [],
        }
