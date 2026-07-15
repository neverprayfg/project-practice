from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.errors import AppError
from app.models import (
    AGENT4_CACHE_FORMAT_VERSION,
    AGENT4_GRAPH_ID,
    AGENT4_VERIFIER_REVISION,
    CodeDraft,
    CodeRepairPatch,
    CounterexampleLedger,
    Defect,
    GeneratorGenerationSubmission,
    InputNormalizationDraft,
    InputStructureDraft,
    ProjectCreate,
    Stage,
    StageRunRequest,
    StageStatus,
    Subtask,
    SubtaskPlanDraft,
    TargetedDefectCheck,
    ValidatorGenerationSubmission,
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
from app.services.agent_graphs import (
    Agent1Graph,
    Agent4Graph,
    AgentGraphCoordinator,
    _context_for_defect,
    _context_for_review,
    _is_current_agent4_thread,
    _required_source_patch_field,
    _semantic_recheck_evidence_is_grounded,
)
from app.services.agent_validators import Agent1Validator, Agent3Validator
from app.services.code_format_contract import build_input_format_contract
from app.services.dataset import DatasetService
from app.services.defects import defects_from_execution
from app.services.generator_policy import generator_usage_issues
from app.services.model_client import (
    AGENT1_PROMPT,
    AGENT2_PROMPT,
    AGENT3_PROMPT,
    AGENT3_REVISE_PROMPT,
    AGENT4_AUDIT_PROMPT,
    AGENT4_GENERATOR_PROMPT,
    AGENT4_RECHECK_PROMPT,
    AGENT4_REPAIR_PROMPT,
    AGENT4_VALIDATOR_PROMPT,
    OpenAICompatibleAgentModel,
    _parse_json,
)
from app.services.project_service import ProjectService
from app.services.structure_tag_catalog import StructureTagCatalog
from app.services.testlib_policy import validator_usage_issues
from app.storage import ProjectStorage


def _catalog() -> StructureTagCatalog:
    return StructureTagCatalog()


def test_special_case_count_cannot_exceed_subtask_total() -> None:
    with pytest.raises(ValidationError):
        Subtask(
            id=1,
            constraints="n <= 10",
            test_count=1,
            expected_complexity="O(n)",
            special_cases=[{"count": 2, "description": "boundary"}],
        )


def test_runtime_parameters_must_cover_each_test_point_in_order() -> None:
    with pytest.raises(ValidationError):
        Subtask(
            id=1,
            constraints="n <= 10",
            test_count=2,
            expected_complexity="O(n)",
            runtime_parameters=[
                {
                    "case_id": 2,
                    "parameters": [{"name": "n", "value": 10, "category": "size"}],
                }
            ],
        )


def test_runtime_parameter_names_are_unique_and_reserved_names_rejected() -> None:
    with pytest.raises(ValidationError):
        RuntimeProfile(
            case_id=1,
            parameters=[
                {"name": "seed", "value": 1, "category": "size"},
                {"name": "seed", "value": 2, "category": "size"},
            ],
        )


def test_subtask_rejects_removed_ambiguous_tag_field() -> None:
    with pytest.raises(ValidationError):
        Subtask.model_validate(
            {
                "id": 1,
                "constraints": "n <= 10",
                "test_count": 1,
                "expected_complexity": "O(n)",
                "subtask_tags": ["small"],
            }
        )


def test_input_structure_accepts_unstructured_template_text() -> None:
    value = InputStructureDraft(template="第一行 n，随后 n 行每行两个整数。")
    assert value.template.startswith("第一行")


@pytest.mark.asyncio
async def test_agent1_merges_only_normalized_fields_and_preserves_authoritative_input() -> None:
    original = {
        "problem": {
            "description": "题目原文\n```\n1 2\n```",
            "input_description": "未提供",
            "output_description": "未提供",
            "samples": [],
            "difficulty": "普及",
        },
        "solution": {"source": "int main() { return 0; }"},
        "revision": 1,
    }

    class NormalizationModel:
        async def agent1_normalize(
            self, context: dict[str, Any], candidate: dict[str, Any]
        ) -> InputNormalizationDraft:
            del context, candidate
            return InputNormalizationDraft(
                input_description="第一行输入两个整数。",
                output_description="输出它们的和。",
                samples=[{"input": "1 2", "output": "3"}],
            )

    graph = object.__new__(Agent1Graph)
    graph.model = NormalizationModel()  # type: ignore[assignment]
    context = {"input": original}

    update = await graph._normalize({"context": context, "candidate": original})
    candidate, issues = Agent1Validator().verify(update["candidate"], context)

    assert issues == []
    assert candidate["problem"]["description"] == original["problem"]["description"]
    assert candidate["problem"]["difficulty"] == original["problem"]["difficulty"]
    assert candidate["solution"]["source"] == original["solution"]["source"]
    assert candidate["problem"]["input_description"] == "第一行输入两个整数。"
    assert candidate["problem"]["output_description"] == "输出它们的和。"
    assert candidate["problem"]["samples"] == [{"input": "1 2", "output": "3", "note": ""}]


def test_document_context_loads_every_role_file_without_selection() -> None:
    app_root = Path(__file__).parents[1] / "app"
    roots = {
        "generator": app_root / "generator_context",
        "validator": app_root / "validator_context",
    }
    loader = Agent4DocumentContext(roots["generator"], roots["validator"])
    loaded = loader.load_all_documents()
    expected = sorted(
        f"{role}/{path.relative_to(root).as_posix()}"
        for role, root in roots.items()
        for path in root.rglob("*")
        if path.is_file()
    )

    assert loaded["format_version"] == CONTEXT_FORMAT_VERSION
    assert loaded["loading_method"] == CONTEXT_LOADING_METHOD
    assert loaded["document_count"] == len(expected)
    assert sorted(item["filename"] for item in loaded["documents"]) == expected
    assert set(loaded["roles"]) == {"generator", "validator"}
    assert all(len(item["digest"]) == 64 for item in loaded["documents"])
    assert all(item["content"] for item in loaded["documents"])
    assert loaded["total_characters"] == sum(len(item["content"]) for item in loaded["documents"])
    generator_context = loaded["role_contexts"]["generator"]
    validator_context = loaded["role_contexts"]["validator"]
    assert set(generator_context) == {"jngen_context"}
    assert set(validator_context) == {"testlib_context"}
    for library in (*generator_context.values(), *validator_context.values()):
        assert {"doc", "example"}.issubset(library)
        assert library["doc"]
        assert library["example"]
    assert (
        "<<<FILE:generator/jngen_context/doc/array.md>>>"
        in generator_context["jngen_context"]["doc"]
    )
    assert FILE_SEPARATOR in generator_context["jngen_context"]["doc"]
    assert ".cpp>>>" in generator_context["jngen_context"]["example"]


def test_document_context_recurses_and_combines_each_child_directory(
    tmp_path: Path,
) -> None:
    generator_root = tmp_path / "generator"
    validator_root = tmp_path / "validator"
    (generator_root / "jngen_context" / "doc" / "graph").mkdir(parents=True)
    (generator_root / "jngen_context" / "example").mkdir(parents=True)
    (validator_root / "testlib_context" / "doc").mkdir(parents=True)
    (validator_root / "testlib_context" / "example").mkdir(parents=True)
    files = {
        generator_root / "jngen_context" / "doc" / "graph" / "a.md": "alpha",
        generator_root / "jngen_context" / "doc" / "graph" / "b.md": "beta",
        generator_root / "jngen_context" / "example" / "g.cpp": "generator",
        validator_root / "testlib_context" / "doc" / "v.md": "validator doc",
        validator_root / "testlib_context" / "example" / "v.cpp": "validator example",
    }
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")

    loaded = Agent4DocumentContext(generator_root, validator_root).load_all_documents()
    graph_docs = loaded["role_contexts"]["generator"]["jngen_context"]["doc"]["graph"]

    assert graph_docs.count("<<<FILE:") == 2
    assert FILE_SEPARATOR in graph_docs
    assert "alpha" in graph_docs and "beta" in graph_docs


def test_document_context_rejects_every_old_layout_and_contract(tmp_path: Path) -> None:
    generator_root = tmp_path / "generator"
    validator_root = tmp_path / "validator"
    for path in (
        generator_root / "jngen_context" / "doc",
        generator_root / "jngen_context" / "examples",
        generator_root / "testlib_context" / "doc",
        generator_root / "testlib_context" / "example",
        validator_root / "testlib_context" / "doc",
        validator_root / "testlib_context" / "example",
    ):
        path.mkdir(parents=True)
        (path / "context.txt").write_text("context", encoding="utf-8")

    loader = Agent4DocumentContext(generator_root, validator_root)
    with pytest.raises(AppError) as plural_examples:
        loader.load_all_documents()
    assert plural_examples.value.code == "AGENT4_CONTEXT_LAYOUT_INVALID"

    (generator_root / "jngen_context" / "examples").rename(
        generator_root / "jngen_context" / "example"
    )
    (validator_root / "doc").mkdir()
    (validator_root / "doc" / "legacy.md").write_text("legacy", encoding="utf-8")
    with pytest.raises(AppError) as validator_root_fallback:
        loader.load_all_documents()
    assert validator_root_fallback.value.code == "AGENT4_CONTEXT_LAYOUT_INVALID"

    with pytest.raises(AppError) as old_contract:
        Agent4DocumentContext.for_role(
            {
                "format_version": 4,
                "loading_method": "recursive_role_json",
                "role_contexts": {},
                "documents": [],
            },
            "generator",
        )
    assert old_contract.value.code == "AGENT4_CONTEXT_CONTRACT_INVALID"


def test_stage5_api_and_checkpoint_reject_old_task_and_graph_ids() -> None:
    assert StageRunRequest.model_validate({}).model_dump() == {}
    with pytest.raises(ValidationError):
        StageRunRequest.model_validate({"task_type": "code_draft"})
    assert _is_current_agent4_thread(f"{'0' * 32}:{AGENT4_GRAPH_ID}:r1:test")
    assert not _is_current_agent4_thread(f"{'0' * 32}:agent4:r1:test")


@pytest.mark.asyncio
async def test_agent4_coordinator_refuses_old_checkpoint_before_startup() -> None:
    coordinator = object.__new__(AgentGraphCoordinator)
    with pytest.raises(AppError) as rejected:
        await coordinator.retry_agent4(f"{'0' * 32}:agent4:r1:legacy")
    assert rejected.value.code == "AGENT4_CHECKPOINT_INCOMPATIBLE"


def test_model_json_parser_rejects_old_wrappers() -> None:
    assert _parse_json('{"ok": true}') == {"ok": True}
    with pytest.raises(json.JSONDecodeError):
        _parse_json('```json\n{"ok": true}\n```')
    with pytest.raises(json.JSONDecodeError):
        _parse_json('<think>legacy</think>{"ok": true}')


def test_stage5_sources_have_no_legacy_contract_paths() -> None:
    app_root = Path(__file__).parents[1] / "app"
    graph_source = (app_root / "services" / "agent_graphs.py").read_text(encoding="utf-8")
    model_source = (app_root / "services" / "model_client.py").read_text(encoding="utf-8")
    documents_source = (app_root / "services" / "agent4_document_context.py").read_text(
        encoding="utf-8"
    )
    pipeline_source = (app_root / "services" / "pipeline.py").read_text(encoding="utf-8")
    storage_source = (app_root / "storage.py").read_text(encoding="utf-8")
    dataset_source = (app_root / "services" / "dataset.py").read_text(encoding="utf-8")

    for legacy in (
        "jngen_documentation",
        "selected_documents",
        "route_documents",
        "candidate_changed",
    ):
        assert legacy not in graph_source
    assert "def agent4_generate(" not in model_source
    assert "CodeGenerationSubmission" not in model_source
    assert "agent4_documentation" not in model_source
    assert "_validator_testlib_json" not in documents_source
    assert "_merge_recursive_values" not in documents_source
    assert '"examples"' not in documents_source
    assert "TaskType.CODE_DRAFT" not in pipeline_source
    assert "agent4_feedback" not in storage_source
    assert "agent4_feedback" not in dataset_source
    assert graph_source.count('builder.add_node("semantic_audit"') == 1


def test_old_stage5_persistence_is_rejected_without_migration(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path)
    project_id = "a" * 32
    state_root = storage.project_dir(project_id) / "state"
    storage.write_json(
        state_root / "agent4-ledger.json",
        {
            "counterexamples": [{"stale": "old-verifier-result"}],
            "last_valid_candidate_revision": "stale",
        },
    )
    storage.write_json(state_root / "agent4-cache.json", {"candidates": {}})
    storage.write_json(state_root / "agent4-last-valid-candidate.json", {"revision_id": "stale"})
    storage.write_json(
        state_root / "current-code" / "code_review.json",
        {"generator_code": "old", "validator_code": "old"},
    )

    for load in (
        storage.load_agent4_ledger,
        storage.load_agent4_cache,
        storage.load_agent4_last_valid_candidate,
        lambda _project_id: storage.load_draft(_project_id, 5),
    ):
        with pytest.raises(AppError) as rejected:
            load(project_id)
        assert rejected.value.code == "AGENT4_STATE_INCOMPATIBLE"

    assert "verifier_revision" not in json.loads(
        (state_root / "agent4-ledger.json").read_text(encoding="utf-8")
    )


def test_current_stage5_cache_contract_round_trips(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path)
    project_id = "b" * 32
    candidate = {
        "format_contract_id": "format_" + "0" * 24,
        "generator_code": "int main(){}",
        "validator_code": "int main(){}",
    }
    cache = {
        "format_version": AGENT4_CACHE_FORMAT_VERSION,
        "verifier_revision": AGENT4_VERIFIER_REVISION,
        "candidates": {
            "revision-key": {
                "candidate": candidate,
                "execution": {"ok": True},
                "replayed_counterexamples": [],
                "gates": ["input_format_contract"],
                "role_digests": {
                    "solution": "solution-digest",
                    "generator": "generator-digest",
                    "validator": "validator-digest",
                },
                "environment_fingerprint": "environment-digest",
            }
        },
    }

    storage.save_agent4_cache(project_id, cache)

    loaded = storage.load_agent4_cache(project_id)
    assert loaded["format_version"] == AGENT4_CACHE_FORMAT_VERSION
    assert loaded["verifier_revision"] == AGENT4_VERIFIER_REVISION
    assert loaded["candidates"]["revision-key"]["execution"] == {"ok": True}
    assert (
        CodeDraft.model_validate(
            loaded["candidates"]["revision-key"]["candidate"]
        ).format_contract_id
        == candidate["format_contract_id"]
    )
    assert storage.load_agent4_ledger(project_id)["verifier_revision"] == (AGENT4_VERIFIER_REVISION)


def test_architecture_migration_discards_obsolete_constraint_plan_and_stage5_state(
    tmp_path: Path,
) -> None:
    storage = ProjectStorage(tmp_path)
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(problem_description="read n", solution_code="int main(){}", difficulty="easy")
    )
    record.current_stage = Stage.CODE_DRAFT
    stage4 = record.stages[int(Stage.SUBTASK_PLAN)]
    stage4.status = StageStatus.PASSED
    stage4.ai_confirmed = True
    stage4.user_confirmed = True
    storage.save_record(record)
    storage.write_json(
        storage.project_dir(record.project_id) / "state" / "subtask_plan.json",
        {
            "subtasks": [
                {
                    "id": index,
                    "constraints": f"part {index}",
                    "test_count": 1,
                    "expected_complexity": "O(n)",
                }
                for index in range(1, 3)
            ]
        },
    )
    storage.write_json(
        storage.project_dir(record.project_id) / "state" / "agent4-ledger.json",
        {"obsolete": True},
    )

    assert projects.invalidate_obsolete_agent4_state() == [record.project_id]

    migrated = storage.load_record(record.project_id)
    assert migrated.current_stage == Stage.SUBTASK_PLAN
    assert migrated.stages[int(Stage.SUBTASK_PLAN)].status == StageStatus.DRAFT
    assert not (
        storage.project_dir(record.project_id) / "state" / "subtask_plan.json"
    ).exists()
    assert not (
        storage.project_dir(record.project_id) / "state" / "agent4-ledger.json"
    ).exists()
    assert projects.invalidate_obsolete_agent4_state() == []


def test_later_stage_failure_enters_the_counterexample_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ProjectsStub:
        failure: dict[str, Any] | None = None

        @staticmethod
        def get(project_id: str) -> Any:
            del project_id
            return SimpleNamespace(
                workflow_revision=7,
                input_revision=3,
                subtasks_revision=4,
            )

        def mark_pipeline_failure(self, project_id: str, stage: Any, error: dict[str, Any]) -> None:
            del project_id, stage
            self.failure = error

    storage = ProjectStorage(tmp_path)
    project_id = "c" * 32
    monkeypatch.setattr(storage, "current_revision", lambda _project_id: "revision")
    projects = ProjectsStub()
    service = object.__new__(DatasetService)
    service.storage = storage
    service.projects = projects

    with pytest.raises(AppError) as failed:
        service._fail_to_agent4(
            project_id,
            "GENERATION_FAILED",
            "生成器在历史测试点失败。",
            {
                "operation": "generate",
                "subtask_id": 2,
                "case_id": 3,
                "seed": 123,
                "runtime_arguments": {"n": "10"},
                "result": {"ok": False, "exit_code": 1},
            },
        )

    ledger = storage.load_agent4_ledger(project_id)
    counterexample = ledger["counterexamples"][0]
    assert failed.value.code == "GENERATION_FAILED"
    assert counterexample["defect"]["identity"]["error_code"] == "GENERATION_FAILED"
    assert counterexample["reproduction"] == {
        "defect_id": counterexample["defect"]["defect_id"],
        "operation": "generate",
        "subtask_id": 2,
        "case_id": 3,
        "seed": 123,
        "runtime_arguments": {"n": "10"},
    }
    assert projects.failure is not None
    assert not (storage.project_dir(project_id) / "state" / "agent4_feedback.json").exists()


def test_stage5_wire_contract_has_no_proof_or_mapping_fields() -> None:
    assert "proof_obligations" not in CodeDraft.model_fields
    assert "implementation_mapping" not in CodeDraft.model_fields
    assert "implementation_mapping" not in GeneratorGenerationSubmission.model_fields
    assert "implementation_mapping" not in ValidatorGenerationSubmission.model_fields


def test_agent_prompts_are_independent_and_review_operations_are_read_only() -> None:
    assert "Agent1" in AGENT1_PROMPT and "Agent2" not in AGENT1_PROMPT
    assert "Agent2" in AGENT2_PROMPT and "Agent3" not in AGENT2_PROMPT
    assert "Agent3" in AGENT3_PROMPT and "恰好一个子任务" in AGENT3_PROMPT
    assert "additional_structure_tag_ids" not in AGENT3_PROMPT
    assert "唯一一次自动修订" in AGENT3_REVISE_PROMPT
    assert "generator.cpp" in AGENT4_GENERATOR_PROMPT
    assert "唯一提供的 jngen_context" in AGENT4_GENERATOR_PROMPT
    assert "registerGen(argc, argv)" in AGENT4_GENERATOR_PROMPT
    assert "testlib" not in AGENT4_GENERATOR_PROMPT.lower()
    assert "一个 ASCII 空格 U+0020" in AGENT4_GENERATOR_PROMPT
    assert "validator.cpp" in AGENT4_VALIDATOR_PROMPT
    assert "testlib validator 的文档和实例" in AGENT4_VALIDATOR_PROMPT
    assert "readSpace、readEoln 和 readEof" in AGENT4_VALIDATOR_PROMPT
    assert "绝对禁止" in AGENT4_AUDIT_PROMPT
    assert "只处理 target_defect" in AGENT4_REPAIR_PROMPT
    assert "不得报告新缺陷" in AGENT4_RECHECK_PROMPT


def test_input_format_contract_ignores_structure_tags() -> None:
    context = {
        "input": {
            "input_structure": {"template": "第一行读取整数 n。", "status": "confirmed"},
            "problem": {"samples": []},
        },
        "confirmed_structure_tags": [
            {"tag_id": "primitive.integer", "applies_to": "n"}
        ],
    }

    contract = build_input_format_contract(context).model_dump(mode="json")

    assert "structure" not in contract


def test_stage4_plan_allows_user_defined_contiguous_subtask_count() -> None:
    first = Subtask(
        id=1,
        test_count=1,
        expected_complexity="O(n)",
    )
    second = first.model_copy(update={"id": 2})

    assert SubtaskPlanDraft(subtasks=[first]).subtasks[0].id == 1
    assert len(SubtaskPlanDraft(subtasks=[first, second]).subtasks) == 2
    with pytest.raises(ValidationError):
        SubtaskPlanDraft(subtasks=[first, second.model_copy(update={"id": 3})])


def test_agent3_uses_the_same_runtime_contract_gate_as_agent4() -> None:
    candidate = {
        "subtasks": [
            {
                "id": 1,
                "constraints": "n <= 10, m = n*(n-1)/2",
                "test_count": 1,
                "expected_complexity": "O(n)",
                "special_cases": [],
                "runtime_parameters": [
                    {
                        "case_id": 1,
                        "parameters": [
                            {"name": "n", "value": 10, "category": "size"},
                            {"name": "m", "value": 90, "category": "size"},
                        ],
                    }
                ],
                "additional_structure_tag_ids": [],
            }
        ]
    }
    context = {
        "subtasks": [],
        "confirmed_structure_tags": [
            {"tag_id": "primitive.integer", "applies_to": "n"}
        ],
    }

    _normalized, issues = Agent3Validator(_catalog()).verify(candidate, context)

    assert any("m = n*(n-1)/2" in issue for issue in issues)


def test_input_format_contract_is_stable_and_changes_with_the_template() -> None:
    context = {
        "input": {
            "input_structure": {
                "template": "第一行读取整数 n。",
                "status": "confirmed",
            },
            "problem": {"samples": [{"input": "1\n"}]},
        },
        "confirmed_structure_tags": [{"tag_id": "primitive.integer", "applies_to": "n"}],
    }

    first = build_input_format_contract(context)
    second = build_input_format_contract(context)
    changed = build_input_format_contract(
        {
            **context,
            "input": {
                **context["input"],
                "input_structure": {
                    "template": "第一行读取两个整数 n 和 m。",
                    "status": "confirmed",
                },
            },
        }
    )

    assert first == second
    assert first.format_contract_id != changed.format_contract_id
    assert first.validator_consumption_policy == "read_exact_template_then_eof"
    assert first.whitespace.token_separator == "single_ascii_space"
    assert first.whitespace.tab_character == "forbidden"
    assert first.whitespace.final_newline == "required"


def test_generator_policy_accepts_testlib_only_and_optional_jngen() -> None:
    testlib_code = r"""
#include "testlib.h"
int main(int argc, char** argv) {
    registerGen(argc, argv, 1);
    int n = opt<int>("n");
    println(rnd.next(1, n));
}
"""
    jngen_code = r"""
#include "jngen.h"
int main(int argc, char** argv) {
    registerGen(argc, argv);
    parseArgs(argc, argv);
    int n = getOpt("n");
    std::cout << Array::random(n, 1, n) << std::endl;
}
"""

    assert generator_usage_issues(testlib_code, {"n"}) == []
    assert generator_usage_issues(jngen_code, {"n"}) == []
    assert any("opt<T>" in issue for issue in generator_usage_issues(testlib_code, {"m"}))

    defects = defects_from_execution(
        {
            "ok": False,
            "failure_category": "library_api",
            "validation_level": "static",
            "message": "生成器库接入失败。",
            "checks": [
                {
                    "operation": "generator_library_usage",
                    "ok": False,
                    "issues": ["缺少生成器库。"],
                }
            ],
        }
    )
    assert defects[0].identity.target_file == "generator.cpp"


def test_generated_input_rejected_by_validator_targets_the_generator() -> None:
    defects = defects_from_execution(
        {
            "ok": False,
            "failure_category": "validation",
            "validation_level": "complete",
            "message": "测试点未通过 validator。",
            "checks": [
                {
                    "operation": "validate",
                    "subtask_id": 1,
                    "case_id": 3,
                    "result": {
                        "ok": False,
                        "exit_code": 3,
                        "stderr": "FAIL Expected EOF",
                    },
                }
            ],
        }
    )

    assert len(defects) == 1
    assert defects[0].identity.target_file == "generator.cpp"
    assert defects[0].identity.error_code == "VALIDATE_FAILED"


def test_validator_policy_requires_strict_whitespace_consumption() -> None:
    strict = r"""
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
    loose = r"""
#include "testlib.h"
int main(int argc, char** argv) {
    registerValidation(argc, argv);
    inf.readInt();
    inf.readInt();
    inf.readEof();
}
"""

    assert validator_usage_issues(strict, requires_ascii_space=True) == []
    loose_issues = validator_usage_issues(loose, requires_ascii_space=True)
    assert any("readEoln" in issue for issue in loose_issues)
    assert any("ASCII 空格" in issue for issue in loose_issues)


def test_code_draft_requires_only_format_contract_and_two_sources() -> None:
    with pytest.raises(ValidationError):
        CodeDraft(generator_code="code", validator_code="code")
    draft = CodeDraft(
        format_contract_id="format_" + "0" * 24,
        generator_code="int main(){}",
        validator_code="int main(){}",
    )
    assert set(draft.model_dump()) >= {"format_contract_id", "generator_code", "validator_code"}


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class _RecordingClient:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content
        self.requests: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append({"url": url, **kwargs})
        return _Response({"choices": [{"message": {"content": json.dumps(self.content)}}]})


class _SequenceClient:
    def __init__(self, contents: list[str | dict[str, Any]]) -> None:
        self.contents = list(contents)
        self.requests: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append({"url": url, **kwargs})
        content = self.contents.pop(0)
        serialized = content if isinstance(content, str) else json.dumps(content)
        return _Response({"choices": [{"message": {"content": serialized}}]})


class _ParallelGenerationModel:
    def __init__(self, format_contract_id: str) -> None:
        self.format_contract_id = format_contract_id
        self.started: set[str] = set()
        self.received_libraries: dict[str, set[str]] = {}
        self.both_started = asyncio.Event()

    async def _join(self, role: str) -> None:
        self.started.add(role)
        if len(self.started) == 2:
            self.both_started.set()
        await self.both_started.wait()

    async def agent4_generate_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> GeneratorGenerationSubmission:
        del candidate
        self.received_libraries["generator"] = set(context["library_context"])
        assert "agent4_documentation" not in context
        assert "agent4_library_context_bundle" not in context
        assert all(
            item["filename"].startswith("generator/")
            for item in context["library_document_manifest"]
        )
        await self._join("generator")
        return GeneratorGenerationSubmission.model_validate(
            {
                "format_contract_id": self.format_contract_id,
                "generator_code": "int main(){}",
            }
        )

    async def agent4_generate_validator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> ValidatorGenerationSubmission:
        del candidate
        self.received_libraries["validator"] = set(context["library_context"])
        assert "agent4_documentation" not in context
        assert "agent4_library_context_bundle" not in context
        assert all(
            item["filename"].startswith("validator/")
            for item in context["library_document_manifest"]
        )
        await self._join("validator")
        return ValidatorGenerationSubmission.model_validate(
            {
                "format_contract_id": self.format_contract_id,
                "validator_code": "int main(){}",
            }
        )


class _Agent4EventStorage:
    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []
        self.timings: list[dict[str, Any]] = []

    def append_agent4_decision(self, project_id: str, event: dict[str, Any]) -> None:
        del project_id
        self.decisions.append(event)

    def append_agent4_timing(self, project_id: str, event: dict[str, Any]) -> None:
        del project_id
        self.timings.append(event)


class _LedgerServiceStub:
    def __init__(self) -> None:
        self.rollback_calls = 0

    def rollback_repair(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.rollback_calls += 1
        return CounterexampleLedger(verifier_revision=AGENT4_VERIFIER_REVISION)

    def record_repair(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return CounterexampleLedger(verifier_revision=AGENT4_VERIFIER_REVISION)


def test_agent4_routing_never_runs_a_second_open_semantic_audit() -> None:
    assert Agent4Graph._route_after_verify({"defects": [], "semantic_audit_done": False}) == "audit"
    assert (
        Agent4Graph._route_after_verify({"defects": [], "semantic_audit_done": True}) == "approve"
    )
    assert (
        Agent4Graph._route_after_progress(
            {"defects": [], "semantic_audit_done": False, "stopped": False}
        )
        == "audit"
    )
    assert (
        Agent4Graph._route_after_progress(
            {"defects": [], "semantic_audit_done": True, "stopped": False}
        )
        == "approve"
    )


def test_agent4_limits_one_repair_per_run_but_history_does_not_lock_future_runs() -> None:
    defect = Defect.model_validate(
        {
            "defect_id": "defect_" + "1" * 20,
            "identity": {
                "category": "compile",
                "target_file": "generator.cpp",
                "constraint_id": "system:compile",
                "subtask": "global",
                "test_point": "global",
                "error_code": "CPP_COMPILE_FAILED",
            },
            "validation_level": "compile",
            "message": "编译失败。",
        }
    )
    graph = object.__new__(Agent4Graph)
    graph.storage = _Agent4EventStorage()  # type: ignore[assignment]
    base_state = {
        "run_id": "run",
        "project_id": "0" * 32,
        "candidate_revision": "revision",
        "candidate": {"generator_code": "broken"},
        "defects": [defect.model_dump(mode="json")],
        "validation_level": "compile",
        "ledger": {
            "verifier_revision": AGENT4_VERIFIER_REVISION,
            "counterexamples": [],
            "last_valid_candidate_revision": None,
        },
    }

    repeated_in_run = graph._select_defect(
        {**base_state, "attempted_defect_ids": [defect.defect_id]}
    )
    repeated_after_restart = graph._select_defect(
        {
            **base_state,
            "attempted_defect_ids": [],
            "ledger": {
                "verifier_revision": AGENT4_VERIFIER_REVISION,
                "counterexamples": [
                    {
                        "counterexample_id": "case_" + "1" * 20,
                        "defect": defect.model_dump(mode="json"),
                        "status": "open",
                        "first_seen_revision": "first",
                        "last_seen_revision": "last",
                        "repair_history": [
                            {
                                "candidate_revision": "repaired",
                                "outcome": "still_open",
                                "reason": "一次修复后仍存在。",
                            }
                        ],
                    }
                ],
                "last_valid_candidate_revision": None,
            },
        }
    )

    assert repeated_in_run["stopped"] is True
    assert defect.defect_id in repeated_in_run["stop_reason"]
    assert repeated_after_restart.get("stopped") is not True
    assert repeated_after_restart["target_defect"]["defect_id"] == defect.defect_id


def test_agent4_rolls_back_changed_code_when_target_defect_remains() -> None:
    defect = Defect.model_validate(
        {
            "defect_id": "defect_" + "2" * 20,
            "identity": {
                "category": "compile",
                "target_file": "generator.cpp",
                "constraint_id": "system:compile",
                "subtask": "global",
                "test_point": "global",
                "error_code": "CPP_COMPILE_FAILED",
            },
            "validation_level": "compile",
            "message": "编译失败仍然存在。",
        }
    )
    graph = object.__new__(Agent4Graph)
    graph.storage = _Agent4EventStorage()  # type: ignore[assignment]
    graph.ledger_service = _LedgerServiceStub()  # type: ignore[assignment]
    empty_ledger = {
        "verifier_revision": AGENT4_VERIFIER_REVISION,
        "counterexamples": [],
        "last_valid_candidate_revision": None,
    }

    result = graph._evaluate_progress(
        {
            "run_id": "run",
            "project_id": "0" * 32,
            "candidate": {"generator_code": "changed code"},
            "candidate_revision": "changed-revision",
            "accepted_candidate": {"generator_code": "accepted code"},
            "accepted_revision": "accepted-revision",
            "target_defect": defect.model_dump(mode="json"),
            "defects": [defect.model_dump(mode="json")],
            "validation_level": "compile",
            "baseline_summary": {
                "open_blockers": 1,
                "blocker_ids": [defect.defect_id],
                "defect_ids": [defect.defect_id],
                "validation_level": "compile",
                "validation_rank": 2,
            },
            "closed_defect_ids_before": [],
            "ledger": empty_ledger,
            "accepted_ledger": empty_ledger,
            "patch_scope": ["generator.cpp"],
            "patch_summary": {"changed": True},
        }
    )

    assert result["stopped"] is True
    assert result["candidate"] == {"generator_code": "accepted code"}
    assert result["candidate_revision"] == "accepted-revision"
    assert graph.ledger_service.rollback_calls == 1


def test_closing_target_accepts_later_gate_defects_as_newly_observed() -> None:
    target = Defect.model_validate(
        {
            "defect_id": "defect_" + "5" * 20,
            "identity": {
                "category": "library_api",
                "target_file": "generator.cpp",
                "constraint_id": "system:generator_runtime_parameters",
                "subtask": "all",
                "test_point": "all",
                "error_code": "GENERATOR_RUNTIME_PARAMETERS_FAILED",
            },
            "validation_level": "static",
            "message": "命名参数读取错误。",
        }
    )
    later = Defect.model_validate(
        {
            "defect_id": "defect_" + "6" * 20,
            "identity": {
                "category": "proof_obligation",
                "target_file": "implementation_mapping",
                "constraint_id": "input:tag:primitive.integer",
                "subtask": "all",
                "test_point": "all",
                "error_code": "API_SYMBOL_NOT_DOCUMENTED",
            },
            "validation_level": "static",
            "message": "后续实现映射门禁发现文档证据错误。",
        }
    )
    graph = object.__new__(Agent4Graph)
    graph.storage = _Agent4EventStorage()  # type: ignore[assignment]
    graph.ledger_service = _LedgerServiceStub()  # type: ignore[assignment]
    ledger = {
        "verifier_revision": AGENT4_VERIFIER_REVISION,
        "counterexamples": [],
        "last_valid_candidate_revision": None,
    }

    result = graph._evaluate_progress(
        {
            "run_id": "run",
            "project_id": "0" * 32,
            "candidate": {"generator_code": "fixed"},
            "candidate_revision": "fixed-revision",
            "target_defect": target.model_dump(mode="json"),
            "defects": [later.model_dump(mode="json")],
            "validation_level": "static",
            "baseline_summary": {
                "open_blockers": 1,
                "blocker_ids": [target.defect_id],
                "defect_ids": [target.defect_id],
                "validation_level": "static",
                "validation_rank": 1,
            },
            "closed_defect_ids_before": [],
            "ledger": ledger,
            "patch_scope": ["generator.cpp"],
            "patch_summary": {"changed": True},
        }
    )

    assert result["accepted_candidate"] == {"generator_code": "fixed"}
    assert result["accepted_revision"] == "fixed-revision"
    event = graph.storage.decisions[-1]
    assert event["decision"] == "accepted"
    assert event["after"]["newly_observed_blocker_ids"] == [later.defect_id]


def test_agent4_never_repairs_files_outside_its_owned_roles() -> None:
    defect = Defect.model_validate(
        {
            "defect_id": "defect_" + "3" * 20,
            "identity": {
                "category": "solution",
                "target_file": "solution.cpp",
                "constraint_id": "system:solve",
                "subtask": "1",
                "test_point": "1",
                "error_code": "SOLUTION_FAILED",
            },
            "validation_level": "complete",
            "message": "标程失败。",
        }
    )
    graph = object.__new__(Agent4Graph)
    graph.storage = _Agent4EventStorage()  # type: ignore[assignment]

    result = graph._select_defect(
        {
            "run_id": "run",
            "project_id": "0" * 32,
            "candidate_revision": "revision",
            "defects": [defect.model_dump(mode="json")],
            "attempted_defect_ids": [],
            "ledger": {
                "verifier_revision": AGENT4_VERIFIER_REVISION,
                "counterexamples": [],
                "last_valid_candidate_revision": None,
            },
        }
    )

    assert result["stopped"] is True
    assert "不属于 Agent4 可修改范围" in result["stop_reason"]


def test_every_owned_defect_requires_a_source_patch() -> None:
    source_defect = Defect.model_validate(
        {
            "defect_id": "defect_" + "4" * 20,
            "identity": {
                "category": "library_api",
                "target_file": "generator.cpp",
                "constraint_id": "system:generator_runtime_parameters",
                "subtask": "all",
                "test_point": "all",
                "error_code": "GENERATOR_RUNTIME_PARAMETERS_FAILED",
            },
            "validation_level": "static",
            "message": "命名参数未读取。",
            "evidence": {"check": {"operation": "generator_runtime_parameters"}},
        }
    )
    no_effect = source_defect.model_copy(
        update={
            "identity": source_defect.identity.model_copy(
                update={
                    "constraint_id": "subtask:1:case:1:parameter:n",
                    "error_code": "PARAMETER_NO_EFFECT",
                }
            ),
            "evidence": {"check": {"operation": "implementation_mapping"}},
        }
    )
    mapping_only = source_defect.model_copy(
        update={
            "identity": source_defect.identity.model_copy(
                update={"error_code": "PARAMETER_NOT_MAPPED"}
            ),
            "evidence": {"check": {"operation": "implementation_mapping"}},
        }
    )

    assert _required_source_patch_field(source_defect) == "generator_code"
    assert _required_source_patch_field(no_effect) == "generator_code"
    assert _required_source_patch_field(mapping_only) == "generator_code"


def test_semantic_recheck_persistence_requires_current_source_evidence() -> None:
    defect = Defect.model_validate(
        {
            "defect_id": "defect_" + "7" * 20,
            "identity": {
                "category": "semantic",
                "target_file": "generator.cpp",
                "constraint_id": "input:tag:graph.directed.weighted",
                "subtask": "all",
                "test_point": "branch",
                "error_code": "PROPERTY_MISSING",
            },
            "validation_level": "semantic",
            "message": "目标性质缺失。",
        }
    )
    current = TargetedDefectCheck.model_validate(
        {
            "defect_id": defect.defect_id,
            "still_present": True,
            "message": "当前源码仍有问题。",
            "evidence": {
                "target_file": "generator.cpp",
                "code_snippet": "auto graph = make_current_graph();",
                "rationale": "该构造仍未实现目标性质。",
            },
        }
    )
    stale = current.model_copy(
        update={
            "evidence": current.evidence.model_copy(
                update={"code_snippet": "auto graph = make_old_graph();"}
            )
        }
    )

    candidate = {"generator_code": "auto graph = make_current_graph();"}
    assert _semantic_recheck_evidence_is_grounded(current, defect, candidate) is True
    assert _semantic_recheck_evidence_is_grounded(stale, defect, candidate) is False
    with pytest.raises(ValidationError):
        TargetedDefectCheck(
            defect_id=defect.defect_id,
            still_present=True,
            message="没有当前源码证据。",
        )


def _generation_state(format_contract_id: str) -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "project_id": "0" * 32,
        "candidate_revision": "uninitialized",
        "candidate": {},
        "context": {
            "input_format_contract": {"format_contract_id": format_contract_id},
            "agent4_library_context_bundle": {
                "format_version": CONTEXT_FORMAT_VERSION,
                "loading_method": CONTEXT_LOADING_METHOD,
                "roles": {
                    "generator": ["generator/jngen_context/doc/random.md"],
                    "validator": ["validator/testlib_context/doc/doc.md"],
                },
                "role_contexts": {
                    "generator": {
                        "jngen_context": {"doc": "jngen doc", "example": "jngen example"},
                    },
                    "validator": {
                        "testlib_context": {
                            "doc": "testlib validator doc",
                            "example": "testlib validator example",
                        }
                    },
                },
                "documents": [
                    {
                        "role": "generator",
                        "filename": "generator/jngen_context/doc/random.md",
                        "digest": "0" * 64,
                        "symbols": ["main"],
                        "content": "main",
                    },
                    {
                        "role": "validator",
                        "filename": "validator/testlib_context/doc/doc.md",
                        "digest": "1" * 64,
                        "symbols": ["main"],
                        "content": "main",
                    },
                ],
            },
        },
    }


@pytest.mark.asyncio
async def test_remote_model_uses_explicit_operation_and_pydantic_schema() -> None:
    client = _RecordingClient({"defects": []})
    model = OpenAICompatibleAgentModel(Settings(model_api_key="test-key"), client)  # type: ignore[arg-type]
    audit = await model.agent4_audit({}, {}, {"ok": True})

    assert audit.defects == []
    message = json.loads(client.requests[0]["json"]["messages"][1]["content"])
    request_payload = client.requests[0]["json"]
    assert message["operation"] == "agent4.semantic_audit"
    assert message["response_contract"]["title"] == "SemanticAudit"
    assert "JSON" in request_payload["messages"][0]["content"]
    assert "JSON" in message["output_instructions"]
    assert request_payload["thinking"] == {"type": "disabled"}
    assert request_payload["response_format"] == {"type": "json_object"}
    assert request_payload["max_tokens"] == 131_072


@pytest.mark.asyncio
async def test_agent1_wire_contract_excludes_authoritative_fields() -> None:
    client = _RecordingClient(
        {
            "input_description": "第一行输入两个整数。",
            "output_description": "输出它们的和。",
            "samples": [{"input": "1 2", "output": "3"}],
        }
    )
    model = OpenAICompatibleAgentModel(Settings(model_api_key="test-key"), client)  # type: ignore[arg-type]

    result = await model.agent1_normalize({"input": {}}, {})

    assert result.input_description == "第一行输入两个整数。"
    payload = client.requests[0]["json"]
    request = json.loads(payload["messages"][1]["content"])
    assert set(request["response_contract"]["properties"]) == {
        "input_description",
        "output_description",
        "samples",
    }
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_remote_model_repairs_one_invalid_json_contract_response() -> None:
    client = _SequenceClient(["not-json", {"defects": []}])
    model = OpenAICompatibleAgentModel(Settings(model_api_key="test-key"), client)  # type: ignore[arg-type]

    audit = await model.agent4_audit({}, {}, {"ok": True})

    assert audit.defects == []
    assert len(client.requests) == 2
    retry_messages = client.requests[1]["json"]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": "not-json"}
    assert "validation_errors=" in retry_messages[-1]["content"]


@pytest.mark.asyncio
async def test_remote_model_stops_after_two_invalid_json_contract_responses() -> None:
    client = _SequenceClient(["not-json", "{"])
    model = OpenAICompatibleAgentModel(Settings(model_api_key="test-key"), client)  # type: ignore[arg-type]

    with pytest.raises(AppError) as failure:
        await model.agent4_audit({}, {}, {"ok": True})

    assert failure.value.code == "MODEL_FAILED"
    assert failure.value.details["failure_kind"] == "json_syntax"
    assert len(failure.value.details["attempts"]) == 2
    assert all("response_digest" in item for item in failure.value.details["attempts"])
    assert all("response_text" not in item for item in failure.value.details["attempts"])


@pytest.mark.asyncio
async def test_remote_model_does_not_retry_a_length_truncated_response() -> None:
    class TruncatedClient:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        async def post(self, url: str, **kwargs: Any) -> _Response:
            self.requests.append({"url": url, **kwargs})
            return _Response(
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "content": "",
                                "reasoning_content": "unfinished reasoning",
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 52_347,
                        "completion_tokens": 131_072,
                        "total_tokens": 183_419,
                    },
                }
            )

    client = TruncatedClient()
    model = OpenAICompatibleAgentModel(
        Settings(model_api_key="test-key"), client  # type: ignore[arg-type]
    )

    with pytest.raises(AppError) as failure:
        await model.agent4_generate_generator({}, {})

    assert failure.value.code == "MODEL_RESPONSE_TRUNCATED"
    assert failure.value.details["max_tokens"] == 131_072
    assert len(failure.value.details["attempts"]) == 1
    assert failure.value.details["attempts"][0]["response"] == {
        "finish_reason": "length",
        "usage": {
            "prompt_tokens": 52_347,
            "completion_tokens": 131_072,
            "total_tokens": 183_419,
        },
        "reasoning_content_present": True,
    }
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_agent3_revision_has_a_distinct_targeted_model_contract() -> None:
    revised = {
        "subtasks": [
            {
                "id": 1,
                "constraints": "n <= 10",
                "test_count": 1,
                "expected_complexity": "O(n)",
                "runtime_parameters": [
                    {
                        "case_id": 1,
                        "parameters": [{"name": "n", "value": 10, "category": "size"}],
                    }
                ],
                "additional_structure_tag_ids": [],
            }
        ]
    }
    client = _RecordingClient(revised)
    model = OpenAICompatibleAgentModel(Settings(model_api_key="test-key"), client)  # type: ignore[arg-type]

    result = await model.agent3_revise(
        {"validation_issues": ["存在未知结构标签：small。"]},
        {
            **revised,
            "subtasks": [
                {
                    **revised["subtasks"][0],
                    "additional_structure_tag_ids": ["small"],
                }
            ],
        },
    )

    assert result.subtasks[0].additional_structure_tag_ids == []
    request = json.loads(client.requests[0]["json"]["messages"][1]["content"])
    assert client.requests[0]["json"]["thinking"] == {"type": "disabled"}
    assert client.requests[0]["json"]["response_format"] == {"type": "json_object"}
    assert request["operation"] == "agent3.revise"
    assert request["inputs"]["context"]["validation_issues"]
    subtask_properties = request["response_contract"]["$defs"]["Subtask"]["properties"]
    assert "additional_structure_tag_ids" in subtask_properties


@pytest.mark.asyncio
async def test_agent4_generates_roles_in_parallel_then_merges_one_candidate() -> None:
    format_contract_id = "format_" + "0" * 24
    model = _ParallelGenerationModel(format_contract_id)
    storage = _Agent4EventStorage()
    graph = object.__new__(Agent4Graph)
    graph.model = model  # type: ignore[assignment]
    graph.storage = storage  # type: ignore[assignment]
    state = _generation_state(format_contract_id)

    result = await asyncio.wait_for(graph._generate_candidate(state), timeout=1)

    assert model.started == {"generator", "validator"}
    assert model.received_libraries == {
        "generator": {"jngen_context"},
        "validator": {"testlib_context"},
    }
    assert result["candidate"]["generator_code"] == "int main(){}"
    assert result["candidate"]["validator_code"] == "int main(){}"
    assert set(result["candidate"]) == {
        "format_contract_id",
        "generator_code",
        "validator_code",
    }
    assert [event["model_call_type"] for event in storage.decisions] == [
        "generator_generation",
        "validator_generation",
    ]


@pytest.mark.asyncio
async def test_parallel_generation_rejects_a_mismatched_format_contract() -> None:
    expected_contract_id = "format_" + "0" * 24
    model = _ParallelGenerationModel("format_" + "1" * 24)
    storage = _Agent4EventStorage()
    graph = object.__new__(Agent4Graph)
    graph.model = model  # type: ignore[assignment]
    graph.storage = storage  # type: ignore[assignment]

    with pytest.raises(AppError) as raised:
        await graph._generate_candidate(_generation_state(expected_contract_id))

    assert raised.value.code == "FORMAT_CONTRACT_MISMATCH"
    assert {event["model_call_type"] for event in storage.decisions} == {
        "generator_generation",
        "validator_generation",
    }


@pytest.mark.asyncio
async def test_every_agent4_model_call_receives_only_recursive_role_json() -> None:
    app_root = Path(__file__).parents[1] / "app"
    documentation = Agent4DocumentContext(
        app_root / "generator_context", app_root / "validator_context"
    ).load_all_documents()
    format_contract_id = "format_" + "0" * 24
    context = {
        "agent4_library_context_bundle": documentation,
        "input_format_contract": {"format_contract_id": format_contract_id},
    }
    defect = Defect.model_validate(
        {
            "defect_id": "defect_" + "0" * 20,
            "identity": {
                "category": "library_api",
                "target_file": "generator.cpp",
                "constraint_id": "system:jngen",
                "subtask": "global",
                "test_point": "global",
                "error_code": "JNGEN_API_INVALID",
            },
            "validation_level": "semantic",
            "message": "API 使用错误。",
        }
    )
    calls = [
        (
            "generate_generator",
            {
                "format_contract_id": format_contract_id,
                "generator_code": "g",
            },
        ),
        (
            "generate_validator",
            {
                "format_contract_id": format_contract_id,
                "validator_code": "v",
            },
        ),
        ("audit", {"defects": []}),
        (
            "repair",
            {
                "target_defect_id": defect.defect_id,
                "rationale": "修复目标 API。",
                "generator_code": "fixed",
            },
        ),
        (
            "recheck",
            {
                "defect_id": defect.defect_id,
                "still_present": False,
                "message": "已关闭。",
            },
        ),
    ]

    for operation, response in calls:
        client = _RecordingClient(response)
        model = OpenAICompatibleAgentModel(
            Settings(model_api_key="test-key"),
            client,  # type: ignore[arg-type]
        )
        if operation in {"generate_generator", "generate_validator"}:
            role = operation.removeprefix("generate_")
            role_context = Agent4DocumentContext.for_role(documentation, role)
            generation_context = {
                "input_format_contract": context["input_format_contract"],
                "library_context": role_context["library_context"],
                "library_document_manifest": role_context["document_manifest"],
            }
            if role == "generator":
                await model.agent4_generate_generator(generation_context, {})
            else:
                await model.agent4_generate_validator(generation_context, {})
        elif operation == "audit":
            await model.agent4_audit(
                _context_for_review(context, ("generator", "validator")),
                {},
                {"ok": True},
            )
        elif operation == "repair":
            await model.agent4_repair(_context_for_defect(context, defect), {}, defect)
        else:
            await model.agent4_recheck(
                _context_for_defect(context, defect), {}, defect, {"ok": True}
            )
        request = json.loads(client.requests[0]["json"]["messages"][1]["content"])
        request_payload = client.requests[0]["json"]
        assert request_payload["thinking"] == {"type": "disabled"}
        assert request_payload["response_format"] == {"type": "json_object"}
        received_context = request["inputs"]["context"]
        response_properties = request["response_contract"]["properties"]
        system_prompt = client.requests[0]["json"]["messages"][0]["content"]
        if operation == "generate_generator":
            assert "generator_code" in response_properties
            assert "validator_code" not in response_properties
            assert set(received_context["library_context"]) == {"jngen_context"}
            assert all(
                {"doc", "example"}.issubset(library)
                for library in received_context["library_context"].values()
            )
            assert "结合题意" in system_prompt
            assert "唯一提供的 jngen_context" in system_prompt
            assert "testlib" not in system_prompt.lower()
        elif operation == "generate_validator":
            assert "validator_code" in response_properties
            assert "generator_code" not in response_properties
            assert set(received_context["library_context"]) == {"testlib_context"}
            assert {"doc", "example"}.issubset(
                received_context["library_context"]["testlib_context"]
            )
            assert "参考 inputs.context.library_context JSON" in system_prompt
            assert "readSpace、readEoln 和 readEof" in system_prompt
        else:
            assert "agent4_documentation" not in received_context
            assert "agent4_library_context_bundle" not in received_context
            expected_roles = {"generator", "validator"} if operation == "audit" else {"generator"}
            assert set(received_context["library_contexts"]) == expected_roles
            assert set(received_context["library_document_manifests"]) == expected_roles
            assert all(
                {"doc", "example"}.issubset(library)
                for role_context in received_context["library_contexts"].values()
                for library in role_context.values()
            )
            assert "agent4_documentation" not in system_prompt


def test_generation_and_repair_contracts_never_return_both_code_roles() -> None:
    format_contract_id = "format_" + "0" * 24
    with pytest.raises(ValidationError):
        GeneratorGenerationSubmission.model_validate(
            {
                "format_contract_id": format_contract_id,
                "generator_code": "g",
                "validator_code": "v",
            }
        )
    with pytest.raises(ValidationError):
        ValidatorGenerationSubmission.model_validate(
            {
                "format_contract_id": format_contract_id,
                "generator_code": "g",
                "validator_code": "v",
            }
        )
    with pytest.raises(ValidationError):
        CodeRepairPatch.model_validate(
            {
                "target_defect_id": "defect_" + "0" * 20,
                "rationale": "错误地同时修改两个角色。",
                "generator_code": "g",
                "validator_code": "v",
            }
        )


def test_subtask_plan_rejects_more_than_one_subtask() -> None:
    with pytest.raises(ValidationError):
        SubtaskPlanDraft(
            subtasks=[
                {
                    "id": 1,
                    "constraints": "small",
                    "test_count": 1,
                    "expected_complexity": "O(n)",
                },
                {
                    "id": 1,
                    "constraints": "large",
                    "test_count": 1,
                    "expected_complexity": "O(n)",
                },
            ]
        )
