from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.errors import AppError
from app.models import (
    CodeDraft,
    ProjectCreate,
    Subtask,
    SubtaskPlanDraft,
    TaskType,
)
from app.models import (
    TestPointRuntimeParameters as RuntimeProfile,
)
from app.services.agent4_document_context import (
    CONTEXT_FORMAT_VERSION,
    CONTEXT_LOADING_METHOD,
    FILE_SEPARATOR,
    Agent4DocumentContext,
)
from app.services.agent_validators import Agent2Validator
from app.services.code_format_contract import build_input_format_contract
from app.services.context_provider import AgentContextProvider
from app.services.generator_policy import generator_usage_issues
from app.services.model_client import (
    AGENT3_INSTRUCTION_PROMPT,
    AGENT3_PROMPT,
    AGENT3_REVISE_PROMPT,
    AGENT4_GENERATOR_PROMPT,
    AGENT4_REPAIR_GENERATOR_PROMPT,
    AGENT4_REPAIR_VALIDATOR_PROMPT,
    AGENT4_VALIDATOR_PROMPT,
    OpenAICompatibleAgentModel,
    _parse_json,
)
from app.services.project_service import ProjectService
from app.services.runtime_parameters import runtime_parameter_issues, serialized_arguments
from app.services.testlib_policy import validator_usage_issues
from app.storage import ProjectStorage


def test_stage_three_and_four_model_contexts_hide_solution_fields(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(
            problem_description="read n",
            solution_code="int main(){}",
            difficulty="easy",
        )
    )
    contexts = AgentContextProvider(storage)

    stage_three = contexts.build(record.project_id, TaskType.TEST_DATA_PLAN)
    assert stage_three["input"]["solution"]["source"] == "int main(){}"
    assert "compile" not in stage_three["input"]["solution"]

    stage_four = contexts.build(record.project_id, TaskType.SUBTASK_PLAN)
    assert "solution" not in stage_four["input"]
    assert "subtasks" not in stage_four

    stage_five = contexts.build_agent4(record.project_id)
    assert "difficulty" not in stage_five["input"]["problem"]
    assert "solution" not in stage_five["input"]


def test_stage_three_requires_each_readonly_section_exactly_once() -> None:
    validator = Agent2Validator()
    valid = {
        "plan_markdown": (
            "# plan\n"
            "<constraints>c</constraints>\n"
            "<test-matrix>m</test-matrix>\n"
            "<blueprint-for-generator>b</blueprint-for-generator>"
        )
    }
    _candidate, issues = validator.verify(valid)
    assert issues == []

    duplicated = {
        "plan_markdown": valid["plan_markdown"] + "\n<constraints>again</constraints>"
    }
    _candidate, issues = validator.verify(duplicated)
    assert any("变量与合规约束" in issue for issue in issues)


def test_runtime_parameters_cover_each_test_point_and_reject_reserved_names() -> None:
    with pytest.raises(ValidationError):
        Subtask(
            id=1,
            test_count=3,
            expected_complexity="O(n)",
            generation_profiles=[
                {"id": "format", "category": "rules_format", "count": 1, "goal": "valid"},
                {"id": "stress", "category": "anti_algorithm", "count": 1, "goal": "stress"},
                {"id": "edge", "category": "boundary_edge", "count": 1, "goal": "edge"},
            ],
            runtime_parameters=[
                {
                    "case_id": 2,
                    "generation_profile_id": "stress",
                    "parameters": [
                        {
                            "name": "construction_mode",
                            "value": "high_branching",
                            "category": "structure",
                        },
                        {"name": "n", "value": 10, "category": "size"},
                    ],
                }
            ],
        )
    with pytest.raises(ValidationError):
        RuntimeProfile(
            case_id=1,
            generation_profile_id="format",
            parameters=[{"name": "seed", "value": 1, "category": "size"}],
        )


def test_generation_profiles_cover_three_goals_and_drive_runner_only() -> None:
    payload = {
        "id": 1,
        "test_count": 3,
        "expected_complexity": "O(n log n)",
        "generation_profiles": [
            {
                "id": "format",
                "category": "rules_format",
                "count": 1,
                "goal": "strict valid format",
                "parameter_names": ["construction_mode", "variation_budget", "n"],
            },
            {
                "id": "stress",
                "category": "anti_algorithm",
                "count": 1,
                "goal": "stress quadratic solutions",
                "parameter_names": ["construction_mode", "variation_budget", "n"],
            },
            {
                "id": "edge",
                "category": "boundary_edge",
                "count": 1,
                "goal": "cover minimum boundary",
                "parameter_names": ["construction_mode", "variation_budget", "n"],
            },
        ],
        "runtime_parameters": [
            {
                "case_id": case_id,
                "generation_profile_id": profile_id,
                "parameters": [
                    {
                        "name": "construction_mode",
                        "value": (
                            "valid_constructed",
                            "high_branching",
                            "boundary_extreme",
                        )[case_id - 1],
                        "category": "structure",
                    },
                    {"name": "variation_budget", "value": 8, "category": "limit"},
                    {"name": "n", "value": case_id, "category": "size"},
                ],
            }
            for case_id, profile_id in enumerate(("format", "stress", "edge"), start=1)
        ],
    }
    subtask = Subtask.model_validate(payload)

    assert serialized_arguments(subtask.runtime_parameters[1]) == {
        "construction_mode": "high_branching",
        "generation_profile": "stress",
        "n": "2",
        "variation_budget": "8",
    }

    wrong_assignment = json.loads(json.dumps(payload))
    wrong_assignment["runtime_parameters"][2]["generation_profile_id"] = "stress"
    with pytest.raises(ValidationError, match="assignments must match profile counts"):
        Subtask.model_validate(wrong_assignment)

    inconsistent_schema = json.loads(json.dumps(payload))
    inconsistent_schema["runtime_parameters"][2]["parameters"][2]["value"] = "3"
    with pytest.raises(ValidationError, match="share one runtime parameter schema"):
        Subtask.model_validate(inconsistent_schema)

    missing_control = json.loads(json.dumps(payload))
    for runtime in missing_control["runtime_parameters"]:
        runtime["parameters"] = [
            parameter
            for parameter in runtime["parameters"]
            if parameter["name"] != "construction_mode"
        ]
    for profile in missing_control["generation_profiles"]:
        profile["parameter_names"] = ["variation_budget", "n"]
    plan = SubtaskPlanDraft(subtasks=[Subtask.model_validate(missing_control)])
    assert any("缺少 construction_mode" in issue for issue in runtime_parameter_issues(plan))

    missing_variation = json.loads(json.dumps(payload))
    for runtime in missing_variation["runtime_parameters"]:
        runtime["parameters"] = [
            parameter
            for parameter in runtime["parameters"]
            if parameter["name"] != "variation_budget"
        ]
    for profile in missing_variation["generation_profiles"]:
        profile["parameter_names"] = ["construction_mode", "n"]
    variation_issues = runtime_parameter_issues(
        SubtaskPlanDraft(subtasks=[Subtask.model_validate(missing_variation)])
    )
    assert any("缺少 variation_budget" in issue for issue in variation_issues)

    all_fixed = json.loads(json.dumps(payload))
    for runtime in all_fixed["runtime_parameters"]:
        runtime["parameters"][0]["value"] = "fixed"
        runtime["parameters"][1]["value"] = 0
    fixed_issues = runtime_parameter_issues(
        SubtaskPlanDraft(subtasks=[Subtask.model_validate(all_fixed)])
    )
    assert any("rules_format" in issue and "非 fixed" in issue for issue in fixed_issues)
    assert any("anti_algorithm" in issue and "非 fixed" in issue for issue in fixed_issues)

    duplicate_fixed = json.loads(json.dumps(payload))
    duplicate_fixed["test_count"] = 4
    duplicate_fixed["generation_profiles"][2]["count"] = 2
    duplicate_fixed["runtime_parameters"][2]["parameters"][0]["value"] = "fixed"
    duplicate_fixed["runtime_parameters"][2]["parameters"][1]["value"] = 0
    repeated = json.loads(json.dumps(duplicate_fixed["runtime_parameters"][2]))
    repeated["case_id"] = 4
    duplicate_fixed["runtime_parameters"].append(repeated)
    duplicate_issues = runtime_parameter_issues(
        SubtaskPlanDraft(subtasks=[Subtask.model_validate(duplicate_fixed)])
    )
    assert any("运行参数完全相同" in issue for issue in duplicate_issues)


def test_document_context_loads_one_prebuilt_generator_reference() -> None:
    app_root = Path(__file__).parents[1] / "app"
    context = Agent4DocumentContext(
        app_root / "generator_context",
        app_root / "validator_context",
    ).load_all_documents()

    assert context["format_version"] == CONTEXT_FORMAT_VERSION
    assert context["loading_method"] == CONTEXT_LOADING_METHOD
    assert set(context["role_contexts"]["generator"]) == {"jngen_context"}
    assert set(context["role_contexts"]["validator"]) == {"testlib_context"}
    assert set(context["role_contexts"]["generator"]["jngen_context"]) == {"reference"}
    assert set(context["role_contexts"]["validator"]["testlib_context"]) == {"doc", "example"}
    assert context["document_count"] == len(context["documents"])
    assert context["total_characters"] == sum(len(item["content"]) for item in context["documents"])
    assert all(
        item["digest"] == hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
        for item in context["documents"]
    )
    generator_documents = [
        item["filename"] for item in context["documents"] if item["role"] == "generator"
    ]
    assert generator_documents == ["generator/jngen_context/agent4_reference.md"]
    reference = context["role_contexts"]["generator"]["jngen_context"]["reference"]
    assert "--- BEGIN DOCUMENT: array.md ---" in reference
    assert "--- BEGIN EXAMPLE: some_random_graph_problem.cpp ---" in reference
    assert FILE_SEPARATOR not in reference
    assert FILE_SEPARATOR in context["role_contexts"]["validator"]["testlib_context"]["example"]


def test_document_context_rejects_old_layout(tmp_path: Path) -> None:
    generator = tmp_path / "generator"
    validator = tmp_path / "validator"
    (generator / "jngen_context" / "doc").mkdir(parents=True)
    (generator / "jngen_context" / "example").mkdir(parents=True)
    (validator / "testlib_context" / "doc").mkdir(parents=True)
    (validator / "testlib_context" / "example").mkdir(parents=True)
    for path in (
        validator / "testlib_context" / "doc" / "doc.md",
        validator / "testlib_context" / "example" / "example.cpp",
    ):
        path.write_text("content", encoding="utf-8")
    (generator / "jngen_context" / "agent4_reference.md").write_text(
        "reference", encoding="utf-8"
    )
    (generator / "legacy.md").write_text("legacy", encoding="utf-8")

    with pytest.raises(AppError) as rejected:
        Agent4DocumentContext(generator, validator).load_all_documents()
    assert rejected.value.code == "AGENT4_CONTEXT_LAYOUT_INVALID"


def test_model_json_parser_rejects_wrappers() -> None:
    assert _parse_json('{"ok": true}') == {"ok": True}
    with pytest.raises(json.JSONDecodeError):
        _parse_json('```json\n{"ok": true}\n```')
    with pytest.raises(json.JSONDecodeError):
        _parse_json('<think>legacy</think>{"ok": true}')


def test_agent4_sources_have_no_history_or_semantic_review_paths() -> None:
    app_root = Path(__file__).parents[1] / "app"
    graph_source = (app_root / "services" / "agent_graphs.py").read_text(encoding="utf-8")
    model_source = (app_root / "services" / "model_client.py").read_text(encoding="utf-8")
    for removed in (
        "semantic_audit",
        "recheck_history",
        "evaluate_progress",
        "CounterexampleLedger",
        "agent4_recheck",
    ):
        assert removed not in graph_source
        assert removed not in model_source


def test_obsolete_agent4_state_is_deleted_during_invalidation(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(problem_description="read n", solution_code="int main(){}", difficulty="easy")
    )
    project_dir = storage.project_dir(record.project_id)
    obsolete = (
        project_dir / "state" / "agent4-cache.json",
        project_dir / "state" / "agent4-ledger.json",
        project_dir / "state" / "agent4-last-valid-candidate.json",
        project_dir / "logs" / "agent4-decisions.jsonl",
        project_dir / "logs" / "agent4-timings.jsonl",
    )
    for path in obsolete:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("legacy", encoding="utf-8")

    storage.invalidate_downstream_artifacts(record.project_id, 4)

    assert all(not path.exists() for path in obsolete)


def test_working_template_is_overwritten_and_release_is_frozen(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(problem_description="read n", solution_code="int main(){}", difficulty="easy")
    )
    first = CodeDraft(
        format_contract_id="format_" + "0" * 24,
        generator_code="generator one",
        validator_code="validator",
        input_revision=record.input_revision,
        subtasks_revision=1,
    )
    second = first.model_copy(update={"generator_code": "generator two"})
    storage.save_code_draft(record.project_id, first)
    saved = storage.save_code_draft(record.project_id, second)

    state = storage.project_dir(record.project_id) / "state"
    assert len(list(state.glob(".working-code.*"))) == 1
    assert storage.load_draft(record.project_id, 5)["generator_code"] == "generator two"
    assert not (state / "code-revisions").exists()

    release = storage.freeze_code_release(
        record.project_id,
        input_revision=record.input_revision,
        subtasks_revision=1,
    )
    assert release.revision_id == saved.revision_id
    assert release.generator_sha256 == hashlib.sha256(b"generator two").hexdigest()
    assert storage.verify_code_release(record.project_id) == release
    assert (storage.project_dir(record.project_id) / "generated").resolve() == (
        state / "released-code" / "generated"
    ).resolve()


def test_saving_working_template_preserves_frozen_release(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(problem_description="read n", solution_code="int main(){}", difficulty="easy")
    )
    draft = CodeDraft(
        format_contract_id="format_" + "0" * 24,
        generator_code="g1",
        validator_code="v1",
        input_revision=record.input_revision,
        subtasks_revision=1,
    )
    storage.save_code_draft(record.project_id, draft)
    release = storage.freeze_code_release(
        record.project_id,
        input_revision=record.input_revision,
        subtasks_revision=1,
    )
    storage.save_code_draft(record.project_id, draft.model_copy(update={"generator_code": "g2"}))

    assert storage.load_code_release(record.project_id) == release
    assert (storage.project_dir(record.project_id) / "generated").resolve() == (
        storage.project_dir(record.project_id) / "state" / "current-code" / "generated"
    ).resolve()

    updated = storage.freeze_code_release(
        record.project_id,
        input_revision=record.input_revision,
        subtasks_revision=1,
    )
    assert updated.revision_id != release.revision_id
    assert (
        storage.project_dir(record.project_id)
        / "state"
        / "released-code-history"
        / release.revision_id
        / "release.json"
    ).is_file()
    storage.clear_working_draft(record.project_id, 5)
    assert storage.load_code_release(record.project_id) == updated


def test_input_format_contract_is_stable_and_problem_input_owned() -> None:
    context = {
        "input": {
            "problem": {"input_description": "第一行读取整数 n。", "samples": [{"input": "1\n"}]},
        }
    }
    first = build_input_format_contract(context)
    second = build_input_format_contract(context)
    changed = build_input_format_contract(
        {
            "input": {
                "problem": {"input_description": "第一行读取整数 n 和 m。", "samples": []},
            }
        }
    )

    assert first == second
    assert first.format_contract_id != changed.format_contract_id
    assert first.whitespace.token_separator == "single_ascii_space"
    assert first.whitespace.final_newline == "required"


def test_generator_and_validator_policies_remain_deterministic() -> None:
    generator = r"""
#include "jngen.h"
int main(int argc, char** argv) {
    registerGen(argc, argv);
    parseArgs(argc, argv);
    int n = getOpt("n");
    std::cout << Array::random(n, 1, n) << std::endl;
}
"""
    validator = r"""
#include "testlib.h"
int main(int argc, char** argv) {
    registerValidation(argc, argv);
    inf.readInt();
    inf.readSpace();
    inf.readInt();
    inf.readEoln();
    inf.readEof();
}
"""
    assert generator_usage_issues(generator, {"n"}) == []
    assert validator_usage_issues(validator, requires_ascii_space=True) == []


def test_generator_policy_rejects_discarded_controls_and_dummy_randomness() -> None:
    generator = r'''
#include "jngen.h"
int main(int argc, char** argv) {
    registerGen(argc, argv);
    parseArgs(argc, argv);
    auto generation_profile = getOpt("generation_profile");
    auto construction_mode = getOpt("construction_mode");
    int dummy = rnd.next(1, 100);
    (void)generation_profile;
    (void)construction_mode;
    (void)dummy;
    std::cout << 1 << std::endl;
}
'''

    issues = generator_usage_issues(
        generator, {"generation_profile", "construction_mode"}
    )

    assert any("generation_profile 被读取后丢弃" in issue for issue in issues)
    assert any("construction_mode 被读取后丢弃" in issue for issue in issues)
    assert any("随机结果被直接丢弃" in issue for issue in issues)


def test_generator_policy_rejects_runtime_echo_for_non_fixed_modes() -> None:
    generator = r'''
#include "jngen.h"
int main(int argc, char** argv) {
    registerGen(argc, argv);
    parseArgs(argc, argv);
    std::string generation_profile = getOpt("generation_profile");
    std::string construction_mode = getOpt("construction_mode");
    long long n = getOpt("n");
    if (construction_mode == "solvable_constructed") std::cout << n << std::endl;
    else std::cout << n << std::endl;
    std::cerr << generation_profile;
    rnd.next(1, 10);
}
'''

    issues = generator_usage_issues(
        generator,
        {"generation_profile", "construction_mode", "n"},
        require_constructive_output=True,
    )

    assert any("只原样输出运行时参数" in issue for issue in issues)


def test_code_draft_keeps_backend_owned_format_contract() -> None:
    assert "format_contract_id" in CodeDraft.model_fields
    assert "trial_results" not in CodeDraft.model_fields


def test_agent4_prompts_enforce_schema_only_parameter_visibility() -> None:
    assert "runtime_parameter_schema" in AGENT4_GENERATOR_PROMPT
    assert "逐测试点实例" in AGENT4_GENERATOR_PROMPT
    assert "construction_controls" in AGENT4_GENERATOR_PROMPT
    assert "不会收到 generation_profiles、运行时参数 schema 或逐测试点实例" in (
        AGENT4_VALIDATOR_PROMPT
    )
    assert "只负责" in AGENT4_REPAIR_GENERATOR_PROMPT
    assert "validator.cpp" in AGENT4_REPAIR_GENERATOR_PROMPT
    assert "generator.cpp" in AGENT4_REPAIR_VALIDATOR_PROMPT
    for prompt in (
        AGENT4_GENERATOR_PROMPT,
        AGENT4_VALIDATOR_PROMPT,
        AGENT4_REPAIR_GENERATOR_PROMPT,
        AGENT4_REPAIR_VALIDATOR_PROMPT,
    ):
        assert "纯 C++ 源码" in prompt
        assert "禁止 JSON" in prompt


def test_agent4_generator_prompt_requires_constructive_coverage() -> None:
    assert "反向构造" in AGENT4_GENERATOR_PROMPT
    assert "最小反例或受控扰动" in AGENT4_GENERATOR_PROMPT
    assert "时间、空间、分支或候选数量" in AGENT4_GENERATOR_PROMPT
    assert "不能只读取参数后原样输出" in AGENT4_GENERATOR_PROMPT
    assert "当方案未指定固定具体值时" in AGENT4_GENERATOR_PROMPT
    assert "固定测试点" in AGENT4_GENERATOR_PROMPT
    assert "不能将其替换为无关的随机样例" in AGENT4_GENERATOR_PROMPT


def test_all_agent3_paths_forbid_composite_runtime_parameter_values() -> None:
    for prompt in (AGENT3_PROMPT, AGENT3_REVISE_PROMPT, AGENT3_INSTRUCTION_PROMPT):
        assert "^[A-Za-z0-9_.:-]+$" in prompt
        assert "禁止把边列表" in prompt
        assert "construction_mode" in prompt


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class _RecordingClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append({"url": url, **kwargs})
        return _Response({"choices": [{"message": {"content": self.content}}]})


class _SequenceRecordingClient(_RecordingClient):
    def __init__(self, contents: list[str]) -> None:
        super().__init__(contents[0])
        self.contents = contents

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append({"url": url, **kwargs})
        content = self.contents[min(len(self.requests) - 1, len(self.contents) - 1)]
        return _Response({"choices": [{"message": {"content": content}}]})


@pytest.mark.asyncio
async def test_stage_instruction_classifier_returns_answer_or_revision_decision() -> None:
    client = _RecordingClient(
        json.dumps(
            {
                "action": "revise",
                "answer": "已按意见修改当前产物。",
                "target": "current_artifact",
            }
        )
    )
    model = OpenAICompatibleAgentModel(Settings(model_api_key="test-key"), client)  # type: ignore[arg-type]

    result = await model.classify_stage_instruction(
        3,
        {"input": {"problem": {"description": "read n"}}},
        {"plan_markdown": "current", "issues": ["hidden"]},
        "请修改边界测试。",
    )

    assert result.action == "revise"
    assert result.target == "current_artifact"
    payload = client.requests[0]["json"]
    request = json.loads(payload["messages"][1]["content"])
    assert payload["temperature"] == 0.5
    assert payload["response_format"] == {"type": "json_object"}
    assert request["inputs"]["user_instruction"] == "请修改边界测试。"
    assert "issues" not in request["inputs"]["candidate"]


@pytest.mark.asyncio
async def test_remote_agent4_generation_returns_plain_source_without_json_controls() -> None:
    source = "int main() { return 0; }"
    for operation, method_name in (
        ("agent4.generate_generator", "agent4_generate_generator"),
        ("agent4.generate_validator", "agent4_generate_validator"),
    ):
        client = _RecordingClient(source)
        model = OpenAICompatibleAgentModel(Settings(model_api_key="test-key"), client)  # type: ignore[arg-type]

        result = await getattr(model, method_name)({}, {})

        assert result == source
        payload = client.requests[0]["json"]
        request = json.loads(payload["messages"][1]["content"])
        assert request["operation"] == operation
        assert "response_contract" not in request
        assert "response_format" not in payload
        assert "纯 C++ 源码" in request["output_instructions"]


@pytest.mark.asyncio
async def test_generator_analysis_uses_structured_contract_and_stage_five_temperature() -> None:
    content = json.dumps(
        {
            "input_constraints": ["1 <= n <= 100"],
            "solution_branch_risks": ["successful branch"],
            "overflow_and_resource_guards": ["check multiplication"],
            "strategies": [
                {
                    "subtask_id": 1,
                    "generation_profile_id": "positive",
                    "profile_category": "rules_format",
                    "construction_mode": "solvable_constructed",
                    "goal": "construct positives",
                    "runtime_parameters": ["construction_mode", "n"],
                    "input_invariants": ["n is in range"],
                    "construction_steps": ["construct from a witness"],
                    "post_checks": ["recompute the witness"],
                    "seed_policy": "diverse",
                    "variation_dimensions": ["witness factors"],
                    "complexity_target": "successful branch",
                }
            ],
        }
    )
    client = _RecordingClient(content)
    model = OpenAICompatibleAgentModel(Settings(model_api_key="test-key"), client)  # type: ignore[arg-type]

    result = await model.agent4_analyze_generator(
        {"input": {"solution": {"source": "int main(){}"}}},
        {},
    )

    assert result.strategies[0].construction_mode == "solvable_constructed"
    payload = client.requests[0]["json"]
    request = json.loads(payload["messages"][1]["content"])
    assert request["operation"] == "agent4.analyze_generator"
    assert payload["temperature"] == 0.5
    assert payload["response_format"] == {"type": "json_object"}
    assert request["inputs"]["context"]["input"]["solution"]["source"] == "int main(){}"


@pytest.mark.asyncio
async def test_generator_audit_repairs_one_invalid_json_response() -> None:
    client = _SequenceRecordingClient(
        [
            '{"passed": false, "issues": ["unterminated]',
            '{"passed": true, "issues": []}',
        ]
    )
    model = OpenAICompatibleAgentModel(Settings(model_api_key="test-key"), client)  # type: ignore[arg-type]

    result = await model.agent4_audit_generator(
        {"generator_analysis": {"strategies": []}},
        "int main() { return 0; }",
    )

    assert result.passed is True
    assert len(client.requests) == 2
    retry = json.loads(client.requests[1]["json"]["messages"][1]["content"])
    assert retry["operation"] == "agent4.audit_generator.revise_contract"
    assert retry["inputs"]["previous_validation_errors"]


@pytest.mark.asyncio
async def test_remote_repair_includes_failed_runtime_arguments() -> None:
    client = _RecordingClient("int main() { return 0; }")
    model = OpenAICompatibleAgentModel(Settings(model_api_key="test-key"), client)  # type: ignore[arg-type]

    result = await model.agent4_repair_generator(
        {"runtime_parameter_schema": [{"name": "n", "value_type": "integer"}]},
        {
            "format_contract_id": "format_" + "0" * 24,
            "generator_code": "broken",
            "validator_code": "valid",
        },
        {
            "ok": False,
            "message": "failed",
            "checks": [
                {
                    "operation": "generate",
                    "runtime_arguments": {"n": "100"},
                    "result": {"ok": False, "stderr": "bad"},
                }
            ],
        },
    )

    assert result == "int main() { return 0; }"
    request = json.loads(client.requests[0]["json"]["messages"][1]["content"])
    assert request["operation"] == "agent4.repair_generator"
    assert "response_contract" not in request
    assert request["inputs"]["candidate"]["validator_code"] == "valid"
    assert "response_format" not in client.requests[0]["json"]
    assert "纯 C++ 源码" in request["output_instructions"]
    failed_check = request["inputs"]["execution"]["failed_checks"][0]
    assert failed_check["runtime_arguments"] == {"n": "100"}
