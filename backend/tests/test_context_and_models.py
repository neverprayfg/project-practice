from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import (
    CodeDraft,
    InputStructureDraft,
    ProjectCreate,
    SpecialCase,
    Subtask,
    TaskType,
    ToolRequest,
)
from app.services.context_provider import AgentContextProvider
from app.services.jngen_policy import jngen_usage_issues
from app.services.model_client import MockAgentModel, OpenAICompatibleAgentModel
from app.services.project_service import ProjectService
from app.storage import ProjectStorage


def test_special_case_count_cannot_exceed_subtask_total() -> None:
    with pytest.raises(ValidationError):
        Subtask(
            id=1,
            constraints="n 至少为 1。",
            test_count=1,
            expected_complexity="O(n)",
            special_cases=[SpecialCase(count=2, description="boundaries")],
        )


def test_input_structure_accepts_unstructured_template_text() -> None:
    draft = InputStructureDraft(
        template="第一行读取 n，第二行读取由 n 个整数组成的 nums。"
    )
    assert "nums" in draft.template


def test_model_prompt_disables_tools_and_embeds_testlib_for_code_draft() -> None:
    code_prompt = OpenAICompatibleAgentModel._system_prompt(TaskType.CODE_DRAFT, "generate")
    input_prompt = OpenAICompatibleAgentModel._system_prompt(
        TaskType.INPUT_STRUCTURE,
        "generate",
    )

    assert "不得输出 tool_requests 字段" in code_prompt
    assert "selected_documents" in code_prompt
    assert "registerGen(argc, argv, 1)" in code_prompt
    assert "registerValidation(argc, argv)" in code_prompt
    assert "不得输出 tool_requests 字段" in input_prompt


def _valid_model_result(task_type: TaskType) -> dict:
    if task_type == TaskType.INPUT_NORMALIZATION:
        return {
            "problem": {
                "description": "读取 n 并输出 n。",
                "input_description": "一个整数 n。",
                "output_description": "输出 n。",
                "samples": [{"input": "1", "output": "1", "note": ""}],
                "difficulty": "easy",
            },
            "solution": {
                "language": "cpp",
                "source": "int main(){}",
                "compile": {"status": "pending", "log": ""},
            },
            "input_structure": {"template": "", "status": "pending", "revision": 0},
            "revision": 1,
        }
    if task_type == TaskType.INPUT_STRUCTURE:
        return {"template": "第一行读取整数 n。"}
    if task_type == TaskType.SUBTASK_PLAN:
        return {
            "subtasks": [
                {
                    "id": 1,
                    "constraints": "1 <= n <= 10",
                    "test_count": 1,
                    "expected_complexity": "O(n)",
                    "special_cases": [],
                }
            ]
        }
    return {
        "generator_code": '#include "jngen.h"\nint main(){}',
        "validator_code": '#include "testlib.h"\nint main(){}',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_type",
    [
        TaskType.INPUT_NORMALIZATION,
        TaskType.INPUT_STRUCTURE,
        TaskType.SUBTASK_PLAN,
        TaskType.CODE_DRAFT,
    ],
)
async def test_every_agent_request_uses_structured_envelope_and_result_schema(
    task_type: TaskType,
) -> None:
    captured: list[dict] = []
    result = _valid_model_result(task_type)

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "confirmation": "revise",
                                    "result": result,
                                    "issues": [],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = OpenAICompatibleAgentModel(
            Settings(model_mode="remote", model_api_key="test-key"),
            client,
        )
        output = await model.run(
            task_type,
            "generate",
            {"input": {"revision": 1}},
            result,
            {"ok": False},
            ["待修复问题"],
        )

    payload = captured[0]
    sent = json.loads(payload["messages"][1]["content"])
    assert payload["response_format"] == {"type": "json_object"}
    assert sent["format_version"] == 1
    assert sent["request"] == {"task_type": task_type.value, "phase": "generate"}
    assert set(sent["inputs"]) == {
        "context",
        "candidate",
        "execution",
        "previous_issues",
    }
    contract = sent["response_contract"]
    assert contract["additionalProperties"] is False
    assert contract["required"] == [
        "confirmation",
        "result",
        "issues",
    ]
    assert "tool_requests" not in contract["properties"]
    assert contract["properties"]["result"]["type"] == "object"
    if task_type == TaskType.CODE_DRAFT:
        code_properties = contract["properties"]["result"]["properties"]
        assert "trial_results" in code_properties
        assert "revision_id" in code_properties
    assert output.result == result


def test_tool_request_accepts_openai_style_tool_and_params_aliases() -> None:
    request = ToolRequest.model_validate(
        {
            "tool": "list_dir",
            "params": {"root": "jngen", "path": "doc", "pattern": "*.md"},
        }
    )

    assert request.name == "list_dir"
    assert request.arguments["path"] == "doc"


def test_tool_request_accepts_flat_readonly_tool_arguments() -> None:
    request = ToolRequest.model_validate(
        {"name": "read_doc", "root": "jngen", "path": "doc/graph.md"}
    )

    assert request.arguments == {"root": "jngen", "path": "doc/graph.md"}


def test_legacy_input_fields_are_converted_to_template_text() -> None:
    draft = InputStructureDraft(
        fields=[
            {
                "name": "nums",
                "type": "integer sequence",
                "order": 2,
                "group": None,
                "repeat_by": "n",
                "relation": None,
            }
        ]
    )

    assert draft.model_dump() == {
        "template": "输入结构：\n1. nums（integer sequence）：输入值。",
        "issues": [],
    }


def test_legacy_subtask_ranges_are_converted_to_constraints_text() -> None:
    subtask = Subtask.model_validate(
        {
            "id": 1,
            "ranges": [{"field": "n", "constraint": "1 <= n <= 10"}],
            "test_count": 2,
            "expected_complexity": "O(n)",
            "special_cases": [],
        }
    )

    assert subtask.constraints == "n：1 <= n <= 10"


@pytest.mark.parametrize(
    "constraints",
    [
        "n：1e3 <= n <= 5e3",
        "1e3 <= n <= 5e3",
        "n 的范围是 1e3 到 5e3",
        "数组长度 n 不小于 1000，且不超过 5000",
        "n: [1000, 5000]",
        "n >= 1000\nn <= 5000",
    ],
)
def test_subtask_accepts_clear_free_form_constraints(constraints: str) -> None:
    subtask = Subtask(
        id=1,
        constraints=constraints,
        test_count=1,
        expected_complexity="O(n)",
    )

    assert subtask.constraints == constraints


def test_global_input_is_sent_as_single_agent_context(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(
            problem_description="A graph problem",
            solution_code="int main(){}",
            difficulty="very hard",
        )
    )
    provider = AgentContextProvider(storage)
    context = provider.build(record.project_id, TaskType.INPUT_STRUCTURE)
    assert context["input"]["problem"]["difficulty"] == "very hard"
    assert context["input"]["solution"]["source"] == "int main(){}"
    assert context["library_guidance"] == []


def test_agent4_context_keeps_testlib_out_of_user_message(
    tmp_path: Path,
) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(
            problem_description="problem",
            solution_code="int main(){}",
            difficulty="easy",
        )
    )
    storage.save_code_draft(
        record.project_id, CodeDraft(generator_code="g1", validator_code="v1")
    )
    code_revision = storage.current_revision(record.project_id)
    storage.append_agent4_feedback(
        record.project_id,
        {
            "code": "GENERATION_FAILED",
            "message": "生成失败",
            "workflow_revision": record.workflow_revision,
            "input_revision": record.input_revision,
            "subtasks_revision": record.subtasks_revision,
            "code_revision": code_revision,
        },
    )
    provider = AgentContextProvider(storage)
    context = provider.build(record.project_id, TaskType.CODE_DRAFT)
    assert context["recovery_feedback"][0]["code"] == "GENERATION_FAILED"
    assert context["library_guidance"] == []
    assert "jngen_documentation" not in context


def test_input_structure_context_does_not_include_downstream_subtasks(
    tmp_path: Path,
) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(
            problem_description="problem",
            solution_code="int main(){}",
            difficulty="easy",
        )
    )
    storage.save_draft(
        record.project_id,
        4,
        {
            "subtasks": [
                {
                    "id": 1,
                    "constraints": "n <= 10",
                    "test_count": 1,
                    "expected_complexity": "O(n)",
                    "special_cases": [],
                }
            ],
            "issues": [],
        },
    )

    provider = AgentContextProvider(storage)
    context = provider.build(record.project_id, TaskType.INPUT_STRUCTURE)

    assert "subtasks" not in context


def test_agent4_base_context_leaves_jngen_selection_to_runner(
    tmp_path: Path,
) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(
            problem_description="graph problem",
            solution_code="int main(){}",
            difficulty="hard",
        )
    )
    input_data = storage.load_input(record.project_id)
    input_data.input_structure.template = "输入 n 个顶点和 m 条有向边。"
    storage.save_input(record.project_id, input_data)

    provider = AgentContextProvider(storage)
    context = provider.build(record.project_id, TaskType.CODE_DRAFT)

    assert context["library_guidance"] == []
    assert "jngen_documentation" not in context
    assert "required_jngen_structures" not in context


def test_jngen_usage_check_rejects_mentions_that_only_appear_in_comments() -> None:
    issues = jngen_usage_issues(
        '// #include "jngen.h"\n// Graph::random(10, 20)\nint main(){}',
    )

    assert any("jngen.h" in issue for issue in issues)
    assert any("数据生成接口" in issue for issue in issues)


def test_tool_request_ignores_repeated_workflow_confirmation() -> None:
    request = ToolRequest.model_validate(
        {
            "tool": "list_dir",
            "params": {"root": "jngen", "path": "doc"},
            "confirmation": "revise",
        }
    )

    assert request.name == "list_dir"
    assert request.arguments == {"root": "jngen", "path": "doc"}


@pytest.mark.asyncio
async def test_remote_model_retries_when_tool_request_is_returned() -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        tool_requests = (
            [
                {
                    "name": "list_dir",
                    "arguments": {
                        "root": "jngen",
                        "path": "doc",
                        "pattern": "*.md",
                    },
                    "confirmation": "revise",
                }
            ]
            if calls == 1
            else []
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "confirmation": "revise",
                                    "result": {
                                        "generator_code": "int main(){}",
                                        "validator_code": "int main(){}",
                                    },
                                    "tool_requests": tool_requests,
                                    "issues": [],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = OpenAICompatibleAgentModel(
            Settings(model_mode="remote", model_api_key="test-key"),
            client,
        )
        output = await model.run(TaskType.CODE_DRAFT, "generate", {}, {}, {}, [])

    assert calls == 2
    assert output.confirmation.value == "revise"
    assert not hasattr(output, "tool_requests")


def test_mock_validator_obeys_testlib_end_of_file_contract() -> None:
    result = MockAgentModel._default_result(TaskType.CODE_DRAFT, {})

    assert "inf.readEof()" in result["validator_code"]


@pytest.mark.asyncio
async def test_remote_model_selects_jngen_documents_with_structured_json() -> None:
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "selected_documents": [
                                        {
                                            "filename": "graph.md",
                                            "reason": "题目需要生成有向图。",
                                        }
                                    ],
                                    "selection_complete": False,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = OpenAICompatibleAgentModel(
            Settings(model_mode="remote", model_api_key="test-key"),
            client,
        )
        selection = await model.select_jngen_documents(
            {
                "input": {"input_structure": {"template": "输入 n 个点 m 条边"}},
                "subtasks": [{"id": 1, "constraints": "n <= 10"}],
            },
            ["array.md", "graph.md"],
        )

    assert selection.selected_documents[0].filename == "graph.md"
    sent = json.loads(requests[0]["messages"][1]["content"])
    assert sent["format_version"] == 1
    assert sent["request"] == {
        "task_type": "jngen_document_selection",
        "phase": "select_documents_before_generation",
        "round": 1,
    }
    assert sent["inputs"]["all_available_documents"] == [
        {"filename": "array.md"},
        {"filename": "graph.md"},
    ]
    selection_contract = sent["response_contract"]["properties"]["selected_documents"]
    assert selection_contract["minItems"] == 1
    assert selection_contract["items"]["properties"]["filename"]["enum"] == [
        "array.md",
        "graph.md",
    ]
    assert "selection_complete" in sent["response_contract"]["required"]


@pytest.mark.asyncio
async def test_remote_model_wraps_stage_four_result_array() -> None:
    subtask = {
        "id": 1,
        "constraints": "1 <= n <= 10",
        "test_count": 2,
        "expected_complexity": "O(n)",
        "special_cases": [],
    }

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"confirmation":"revise","result":'
                                f'{json.dumps([subtask], ensure_ascii=False)},'
                                '"tool_requests":[],"issues":[]}'
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = OpenAICompatibleAgentModel(
            Settings(model_mode="remote", model_api_key="test-key"),
            client,
        )
        output = await model.run(TaskType.SUBTASK_PLAN, "generate", {}, {}, {}, [])

    assert output.result == {"subtasks": [subtask]}


@pytest.mark.asyncio
async def test_remote_model_retries_malformed_json_contract() -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = (
            '{"confirmation":"revise","result":{"template":"第一行读取 n"},'
            '"tool_requests":[],"issues":[]}'
            if calls == 2
            else '{"confirmation":"revise","result":{"template":"缺少结束括号"}'
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = OpenAICompatibleAgentModel(
            Settings(model_mode="remote", model_api_key="test-key"),
            client,
        )
        output = await model.run(TaskType.INPUT_STRUCTURE, "generate", {}, {}, {}, [])

    assert calls == 2
    assert output.result["template"] == "第一行读取 n"


@pytest.mark.asyncio
async def test_remote_model_retries_when_task_result_is_missing_required_field() -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        result = (
            {"generator_code": "int main(){}"}
            if calls == 1
            else {
                "generator_code": "int main(){}",
                "validator_code": "int main(){}",
            }
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "confirmation": "revise",
                                    "result": result,
                                    "tool_requests": [],
                                    "issues": [],
                                }
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = OpenAICompatibleAgentModel(
            Settings(model_mode="remote", model_api_key="test-key"),
            client,
        )
        output = await model.run(TaskType.CODE_DRAFT, "generate", {}, {}, {}, [])

    assert calls == 2
    assert output.result["validator_code"] == "int main(){}"
