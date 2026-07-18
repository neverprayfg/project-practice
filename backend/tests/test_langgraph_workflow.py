from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import DeterministicTestModel

from app.config import Settings
from app.errors import AppError
from app.models import (
    Confirmation,
    GeneratorAuditDraft,
    ProjectCreate,
    SubtaskPlanDraft,
)
from app.models import (
    TestDataPlanDraft as DataPlanDraft,
)
from app.services.agent4_document_context import Agent4DocumentContext
from app.services.agent_graphs import AgentGraphCoordinator
from app.services.agent_recovery import candidate_result
from app.services.project_service import ProjectService
from app.services.recovery_policies import _normalize_runtime_parameter_schema
from app.storage import ProjectStorage


def _context() -> dict[str, Any]:
    return {
        "workflow_revision": 1,
        "input_revision": 1,
        "subtasks_revision": 1,
        "input": {
            "problem": {
                "input_description": "第一行读取整数 n。",
                "samples": [{"input": "1\n", "output": ""}],
            },
        },
        "subtasks": [
            {
                "id": 1,
                "test_count": 3,
                "expected_complexity": "O(n)",
                "generation_profiles": [
                    {
                        "id": "format",
                        "category": "rules_format",
                        "count": 1,
                        "goal": "valid",
                        "parameter_names": ["construction_mode", "variation_budget", "n"],
                    },
                    {
                        "id": "stress",
                        "category": "anti_algorithm",
                        "count": 1,
                        "goal": "stress",
                        "parameter_names": ["construction_mode", "variation_budget", "n"],
                    },
                    {
                        "id": "edge",
                        "category": "boundary_edge",
                        "count": 1,
                        "goal": "edge",
                        "parameter_names": ["construction_mode", "variation_budget", "n"],
                    },
                ],
                "runtime_parameters": [
                    {
                        "case_id": 1,
                        "generation_profile_id": "format",
                        "parameters": [
                            {
                                "name": "construction_mode",
                                "value": "valid_constructed",
                                "category": "structure",
                            },
                            {"name": "variation_budget", "value": 8, "category": "limit"},
                            {"name": "n", "value": 5, "category": "size"},
                        ],
                    },
                    {
                        "case_id": 2,
                        "generation_profile_id": "stress",
                        "parameters": [
                            {
                                "name": "construction_mode",
                                "value": "high_branching",
                                "category": "structure",
                            },
                            {"name": "variation_budget", "value": 8, "category": "limit"},
                            {"name": "n", "value": 10, "category": "size"},
                        ],
                    },
                    {
                        "case_id": 3,
                        "generation_profile_id": "edge",
                        "parameters": [
                            {
                                "name": "construction_mode",
                                "value": "boundary_extreme",
                                "category": "structure",
                            },
                            {"name": "variation_budget", "value": 8, "category": "limit"},
                            {"name": "n", "value": 1, "category": "size"},
                        ],
                    },
                ],
            }
        ],
    }


class SequenceVerifier:
    def __init__(self, executions: list[dict[str, Any]]) -> None:
        self.executions = executions
        self.calls = 0

    async def verify(
        self,
        project_id: str,
        candidate: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del project_id, context
        execution = self.executions[min(self.calls, len(self.executions) - 1)]
        self.calls += 1
        return candidate, execution


def test_deterministic_fix_fills_a_partially_added_runtime_parameter_column() -> None:
    candidate = {"subtasks": _context()["subtasks"]}
    candidate["subtasks"][0]["generation_profiles"][0]["parameter_names"].append(
        "case_variant"
    )
    candidate["subtasks"][0]["runtime_parameters"][0]["parameters"].append(
        {"name": "case_variant", "value": 1, "category": "structure"}
    )

    result = _normalize_runtime_parameter_schema(candidate)

    assert result is not None and result.candidate is not None
    plan = SubtaskPlanDraft.model_validate(result.candidate)
    assert all(
        any(parameter.name == "case_variant" for parameter in runtime.parameters)
        for runtime in plan.subtasks[0].runtime_parameters
    )


class RepairModel(DeterministicTestModel):
    def __init__(self) -> None:
        self.analysis_context: dict[str, Any] | None = None
        self.generator_context: dict[str, Any] | None = None
        self.validator_context: dict[str, Any] | None = None
        self.generator_repairs = 0
        self.validator_repairs = 0

    async def agent4_analyze_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> Any:
        self.analysis_context = context
        return await super().agent4_analyze_generator(context, candidate)

    async def agent4_generate_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> Any:
        self.generator_context = context
        return await super().agent4_generate_generator(context, candidate)

    async def agent4_generate_validator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> Any:
        self.validator_context = context
        return await super().agent4_generate_validator(context, candidate)

    async def agent4_repair_generator(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
    ) -> str:
        del context, execution
        self.generator_repairs += 1
        return candidate["generator_code"] + "\n// repaired"

    async def agent4_repair_validator(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
    ) -> str:
        del context, execution
        self.validator_repairs += 1
        return candidate["validator_code"] + "\n// repaired"


class SemanticAuditRepairModel(RepairModel):
    def __init__(self) -> None:
        super().__init__()
        self.audit_calls = 0

    async def agent4_audit_generator(
        self,
        context: dict[str, Any],
        generator_code: str,
    ) -> GeneratorAuditDraft:
        del context, generator_code
        self.audit_calls += 1
        if self.audit_calls == 1:
            return GeneratorAuditDraft(
                passed=False,
                issues=["solvable_constructed 缺少可执行的见证闭环检查。"],
            )
        return GeneratorAuditDraft(passed=True, issues=[])


class Stage4CorrectionModel(DeterministicTestModel):
    def __init__(self) -> None:
        self.revision_issues: list[str] = []
        self.revision_calls = 0

    @staticmethod
    def _plan(invalid: bool) -> dict[str, Any]:
        return {
            "subtasks": [
                {
                    "id": 1,
                    "test_count": 3,
                    "expected_complexity": "O(1)",
                    "generation_profiles": [
                        {
                            "id": "format",
                            "category": "rules_format",
                            "count": 1,
                            "goal": "valid",
                            "parameter_names": ["construction_mode", "variation_budget", "n"],
                        },
                        {
                            "id": "stress",
                            "category": "anti_algorithm",
                            "count": 1,
                            "goal": "stress",
                            "parameter_names": ["construction_mode", "variation_budget", "n"],
                        },
                        {
                            "id": "edge",
                            "category": "boundary_edge",
                            "count": 1,
                            "goal": "edge",
                            "parameter_names": ["construction_mode", "variation_budget", "n"],
                        },
                    ],
                    "runtime_parameters": []
                    if invalid
                    else [
                        {
                            "case_id": case_id,
                            "generation_profile_id": (
                                "format" if case_id == 1 else "stress" if case_id == 2 else "edge"
                            ),
                            "parameters": [
                                {
                                    "name": "construction_mode",
                                    "value": (
                                        "valid_constructed"
                                        if case_id == 1
                                        else "high_branching"
                                        if case_id == 2
                                        else "boundary_extreme"
                                    ),
                                    "category": "structure",
                                },
                                {
                                    "name": "variation_budget",
                                    "value": 8,
                                    "category": "limit",
                                },
                                {"name": "n", "value": case_id, "category": "size"},
                            ],
                        }
                        for case_id in range(1, 4)
                    ],
                }
            ]
        }

    async def agent3_plan(self, context: dict[str, Any], candidate: dict[str, Any]) -> Any:
        del context, candidate
        return SubtaskPlanDraft.model_validate(self._plan(True))

    async def agent3_revise(self, context: dict[str, Any], candidate: dict[str, Any]) -> Any:
        del candidate
        self.revision_calls += 1
        self.revision_issues = list(context["validation_issues"])
        return SubtaskPlanDraft.model_validate(self._plan(False))


class MultiSubtaskModel(DeterministicTestModel):
    def __init__(self) -> None:
        self.received_candidates: list[dict[str, Any]] = []

    async def agent3_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft:
        del context
        self.received_candidates.append(candidate)
        plan = {
            "subtasks": [
                {"id": 1, "test_count": 3, "expected_complexity": "O(n)"},
                {"id": 2, "test_count": 3, "expected_complexity": "O(n log n)"},
            ]
        }
        return SubtaskPlanDraft.model_validate(self._runtime_parameters(plan))


class RawContractRecoveryModel(DeterministicTestModel):
    def __init__(self) -> None:
        self.revision_context: dict[str, Any] | None = None

    async def agent3_plan(self, context: dict[str, Any], candidate: dict[str, Any]) -> Any:
        del context, candidate
        raise AppError(
            "MODEL_FAILED",
            "模型返回的 JSON 未通过响应契约校验。",
            details={
                "failure_kind": "json_syntax",
                "raw_output": '{"subtasks": [',
                "candidate": None,
                "validation_errors": [
                    {
                        "type": "json_decode_error",
                        "location": ["1", "15"],
                        "message": "Expecting value",
                    }
                ],
                "response_metadata": {"finish_reason": "stop"},
            },
        )

    async def agent3_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft:
        del candidate
        self.revision_context = context
        return await super().agent3_plan({}, {})


class SchemaDiffAwareModel(DeterministicTestModel):
    def __init__(self) -> None:
        self.revision_context: dict[str, Any] | None = None

    async def agent3_plan_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> Any:
        del context, candidate
        plan = self._runtime_parameters(self._default_plan())
        plan["subtasks"][0]["runtime_parameters"][1]["parameters"] = [
            {"name": "minimum", "value": 1, "category": "range"}
        ]
        return candidate_result(plan)

    async def agent3_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft:
        del candidate
        self.revision_context = context
        return await super().agent3_plan({}, {})


class ProfileAccountingModel(DeterministicTestModel):
    def __init__(self) -> None:
        self.revision_calls = 0

    async def agent3_plan_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> Any:
        del context, candidate
        plan = self._runtime_parameters(self._default_plan())
        subtask = plan["subtasks"][0]
        subtask["runtime_parameters"].append(
            {
                "case_id": 7,
                "generation_profile_id": "boundary_extreme",
                "parameters": [
                    {
                        "name": "construction_mode",
                        "value": "boundary_extreme",
                        "category": "structure",
                    },
                    {"name": "variation_budget", "value": 8, "category": "limit"},
                    {"name": "scale", "value": 7, "category": "size"},
                    {"name": "profile", "value": "subtask_1", "category": "structure"},
                ],
            }
        )
        subtask["runtime_parameters"][0]["case_id"] = 4
        subtask["runtime_parameters"][1]["case_id"] = 5
        subtask["runtime_parameters"][2]["case_id"] = 6
        return candidate_result(plan)

    async def agent3_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft:
        del context, candidate
        self.revision_calls += 1
        return await super().agent3_plan({}, {})


class Stage3TagRepairModel(DeterministicTestModel):
    def __init__(self) -> None:
        self.repair_context: dict[str, Any] | None = None

    async def agent2_test_data_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> Any:
        del context, candidate
        return DataPlanDraft(plan_markdown="# broken\n<constraints>x</constraints>")

    async def agent2_revise(self, context: dict[str, Any], candidate: dict[str, Any]) -> Any:
        del candidate
        self.repair_context = context
        return await super().agent2_test_data_plan({}, {})


class RoundRegenerationModel(Stage4CorrectionModel):
    def __init__(self, *, always_invalid: bool = False) -> None:
        super().__init__()
        self.plan_calls = 0
        self.always_invalid = always_invalid

    async def agent3_plan(self, context: dict[str, Any], candidate: dict[str, Any]) -> Any:
        del context, candidate
        self.plan_calls += 1
        invalid = self.always_invalid or self.plan_calls == 1
        return SubtaskPlanDraft.model_validate(self._plan(invalid))

    async def agent3_revise(self, context: dict[str, Any], candidate: dict[str, Any]) -> Any:
        del context, candidate
        self.revision_calls += 1
        return SubtaskPlanDraft.model_validate(self._plan(True))


class FencedJsonModel(DeterministicTestModel):
    def __init__(self) -> None:
        self.revision_calls = 0

    async def agent3_plan_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> Any:
        from app.services.agent_recovery import candidate_result_from_error

        del context, candidate
        value = await super().agent3_plan({}, {})
        raw = "```json\n" + value.model_dump_json(exclude={"issues"}) + "\n```"
        error = AppError(
            "MODEL_FAILED",
            "invalid structured response",
            details={
                "failure_kind": "json_syntax",
                "raw_output": raw,
                "candidate": None,
                "validation_errors": [
                    {
                        "type": "json_invalid",
                        "location": [],
                        "message": "JSON is wrapped in a Markdown fence",
                    }
                ],
            },
        )
        result = candidate_result_from_error(error)
        assert result is not None
        return result

    async def agent3_revise(self, context: dict[str, Any], candidate: dict[str, Any]) -> Any:
        del context, candidate
        self.revision_calls += 1
        return await super().agent3_plan({}, {})


class FencedCodeModel(RepairModel):
    async def agent4_generate_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> str:
        source = await super().agent4_generate_generator(context, candidate)
        return f"```cpp\n{source}\n```"

    async def agent4_generate_validator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> str:
        source = await super().agent4_generate_validator(context, candidate)
        return f"```cpp\n{source}\n```"


class EnvironmentFailureModel(DeterministicTestModel):
    def __init__(self) -> None:
        self.generate_calls = 0
        self.revision_calls = 0

    async def agent3_plan(self, context: dict[str, Any], candidate: dict[str, Any]) -> Any:
        del context, candidate
        self.generate_calls += 1
        raise AppError("MODEL_UNAVAILABLE", "model service unavailable", status_code=503)

    async def agent3_revise(self, context: dict[str, Any], candidate: dict[str, Any]) -> Any:
        del context, candidate
        self.revision_calls += 1
        return await super().agent3_plan({}, {})


async def _coordinator(
    tmp_path: Path,
    model: Any,
    verifier: Any,
) -> tuple[AgentGraphCoordinator, ProjectStorage, str]:
    storage = ProjectStorage(tmp_path / "storage")
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(problem_description="read n", solution_code="int main(){}", difficulty="easy")
    )
    app_root = Path(__file__).parents[1] / "app"
    coordinator = AgentGraphCoordinator(
        Settings(app_env="test", storage_root=storage.root),
        storage,
        model,
        verifier,
        Agent4DocumentContext(app_root / "generator_context", app_root / "validator_context"),
    )
    await coordinator.start()
    return coordinator, storage, record.project_id


@pytest.mark.asyncio
async def test_agent3_performs_one_targeted_contract_revision(tmp_path: Path) -> None:
    model = Stage4CorrectionModel()
    coordinator, _storage, project_id = await _coordinator(
        tmp_path, model, SequenceVerifier([{"ok": True}])
    )
    try:
        _thread_id, output, waiting_user = await coordinator.run_agent3(
            project_id,
            {
                "workflow_revision": 1,
                "subtasks": [],
            },
            {},
            requires_user=True,
        )
    finally:
        await coordinator.close()

    assert waiting_user is True
    assert output.confirmation == Confirmation.PASS
    assert model.revision_calls == 1
    assert any("缺少逐测试点运行时参数" in issue for issue in model.revision_issues)


@pytest.mark.asyncio
async def test_agent2_repairs_fixed_markdown_tags_in_same_recovery_run(tmp_path: Path) -> None:
    model = Stage3TagRepairModel()
    coordinator, storage, project_id = await _coordinator(
        tmp_path, model, SequenceVerifier([{"ok": True}])
    )
    try:
        _thread_id, output, waiting_user = await coordinator.run_agent2(
            project_id,
            {"workflow_revision": 1},
            {},
            requires_user=False,
        )
    finally:
        await coordinator.close()

    assert waiting_user is False
    assert output.confirmation == Confirmation.PASS
    assert model.repair_context is not None
    assert "固定标签缺失" in model.repair_context["validation_errors"][0]["message"]
    summary = storage.load_record(project_id).recovery_summaries[3]
    assert summary.status == "passed"
    assert summary.repair_attempts == 1


@pytest.mark.asyncio
async def test_manual_stage_run_creates_new_recovery_run_and_keeps_old_audit(
    tmp_path: Path,
) -> None:
    coordinator, storage, project_id = await _coordinator(
        tmp_path,
        DeterministicTestModel(),
        SequenceVerifier([{"ok": True}]),
    )
    try:
        await coordinator.run_agent2(
            project_id, {"workflow_revision": 1}, {}, requires_user=False
        )
        first = storage.load_record(project_id).recovery_summaries[3]
        await coordinator.run_agent2(
            project_id, {"workflow_revision": 2}, {}, requires_user=False
        )
        second = storage.load_record(project_id).recovery_summaries[3]
    finally:
        await coordinator.close()

    assert first.run_id != second.run_id
    assert storage.load_recovery_manifest(project_id, first.run_id)["status"] == "passed"
    assert storage.load_recovery_manifest(project_id, second.run_id)["status"] == "passed"


@pytest.mark.asyncio
async def test_agent3_regenerates_and_keeps_model_inferred_subtask_count(tmp_path: Path) -> None:
    model = MultiSubtaskModel()
    coordinator, _storage, project_id = await _coordinator(
        tmp_path, model, SequenceVerifier([{"ok": True}])
    )
    existing = model._runtime_parameters(
        {"subtasks": [{"id": 1, "test_count": 3, "expected_complexity": "O(1)"}]}
    )
    try:
        _thread_id, output, waiting_user = await coordinator.run_agent3(
            project_id,
            {"workflow_revision": 1, "test_data_plan": {"plan_markdown": "two ranges"}},
            existing,
            requires_user=False,
        )
    finally:
        await coordinator.close()

    assert waiting_user is False
    assert output.confirmation == Confirmation.PASS
    assert [item["id"] for item in output.result["subtasks"]] == [1, 2]
    assert model.received_candidates == [{}]


@pytest.mark.asyncio
async def test_agent3_repairs_invalid_json_with_raw_output_and_audit(tmp_path: Path) -> None:
    model = RawContractRecoveryModel()
    coordinator, storage, project_id = await _coordinator(
        tmp_path, model, SequenceVerifier([{"ok": True}])
    )
    try:
        _thread_id, output, waiting_user = await coordinator.run_agent3(
            project_id,
            {"workflow_revision": 1, "test_data_plan": {"plan_markdown": "plan"}},
            {},
            requires_user=False,
        )
    finally:
        await coordinator.close()

    assert waiting_user is False
    assert output.confirmation == Confirmation.PASS
    assert model.revision_context is not None
    assert model.revision_context["raw_output"] == '{"subtasks": ['
    assert model.revision_context["validation_errors"][0]["type"] == "json_decode_error"
    summary = storage.load_record(project_id).recovery_summaries[4]
    assert summary.status == "passed"
    assert summary.generation_round == 1
    assert summary.repair_attempts == 1
    manifest = storage.load_recovery_manifest(project_id, summary.run_id)
    assert manifest["events"] == 5


@pytest.mark.asyncio
async def test_agent3_repair_receives_exact_runtime_schema_diffs(tmp_path: Path) -> None:
    model = SchemaDiffAwareModel()
    coordinator, storage, project_id = await _coordinator(
        tmp_path, model, SequenceVerifier([{"ok": True}])
    )
    try:
        _thread_id, output, _waiting = await coordinator.run_agent3(
            project_id,
            {"workflow_revision": 1, "test_data_plan": {"plan_markdown": "plan"}},
            {},
            requires_user=False,
        )
    finally:
        await coordinator.close()

    assert output.confirmation == Confirmation.PASS
    assert model.revision_context is not None
    plan = model.revision_context["recovery_plan"]
    assert plan["root_stage"] == 4
    assert plan["write_grants"][0]["artifact"] == "subtask_plan"
    diff = model.revision_context["recovery_evidence"]["runtime_schema_diffs"][0]
    assert diff["mismatched_cases"][0]["case_id"] == 2
    assert diff["mismatched_cases"][0]["missing_parameters"] == [
        "construction_mode",
        "profile",
        "scale",
        "variation_budget",
    ]
    assert diff["mismatched_cases"][0]["extra_parameters"] == ["minimum"]
    summary = storage.load_record(project_id).recovery_summaries[4]
    run_dir = storage.project_dir(project_id) / "logs" / "agent-recovery" / summary.run_id
    assert list(run_dir.rglob("locate-*.json"))


@pytest.mark.asyncio
async def test_agent3_normalizes_profile_accounting_without_ai_repair(tmp_path: Path) -> None:
    model = ProfileAccountingModel()
    coordinator, _storage, project_id = await _coordinator(
        tmp_path, model, SequenceVerifier([{"ok": True}])
    )
    try:
        _thread_id, output, _waiting = await coordinator.run_agent3(
            project_id,
            {"workflow_revision": 1, "test_data_plan": {"plan_markdown": "plan"}},
            {},
            requires_user=False,
        )
    finally:
        await coordinator.close()

    subtask = output.result["subtasks"][0]
    assert output.confirmation == Confirmation.PASS
    assert model.revision_calls == 0
    assert subtask["test_count"] == 4
    assert [case["case_id"] for case in subtask["runtime_parameters"]] == [1, 2, 3, 4]
    assert [profile["count"] for profile in subtask["generation_profiles"]] == [1, 1, 2]


@pytest.mark.asyncio
async def test_agent3_extracts_single_json_fence_without_ai_repair(tmp_path: Path) -> None:
    model = FencedJsonModel()
    coordinator, storage, project_id = await _coordinator(
        tmp_path, model, SequenceVerifier([{"ok": True}])
    )
    try:
        _thread_id, output, _waiting = await coordinator.run_agent3(
            project_id,
            {"workflow_revision": 1, "test_data_plan": {"plan_markdown": "plan"}},
            {},
            requires_user=False,
        )
    finally:
        await coordinator.close()

    assert output.confirmation == Confirmation.PASS
    assert model.revision_calls == 0
    summary = storage.load_record(project_id).recovery_summaries[4]
    assert summary.repair_attempts == 0


@pytest.mark.asyncio
async def test_agent3_uses_five_repairs_before_new_generation_round(tmp_path: Path) -> None:
    model = RoundRegenerationModel()
    coordinator, storage, project_id = await _coordinator(
        tmp_path, model, SequenceVerifier([{"ok": True}])
    )
    try:
        _thread_id, output, _waiting_user = await coordinator.run_agent3(
            project_id,
            {"workflow_revision": 1, "test_data_plan": {"plan_markdown": "plan"}},
            {},
            requires_user=False,
        )
    finally:
        await coordinator.close()

    assert output.confirmation == Confirmation.PASS
    assert model.plan_calls == 2
    assert model.revision_calls == 5
    summary = storage.load_record(project_id).recovery_summaries[4]
    assert summary.generation_round == 2
    assert summary.repair_attempts == 5


@pytest.mark.asyncio
async def test_agent3_exhaustion_stops_after_three_rounds_and_clears_draft(
    tmp_path: Path,
) -> None:
    model = RoundRegenerationModel(always_invalid=True)
    coordinator, storage, project_id = await _coordinator(
        tmp_path, model, SequenceVerifier([{"ok": True}])
    )
    try:
        with pytest.raises(AppError) as exc_info:
            await coordinator.run_agent3(
                project_id,
                {"workflow_revision": 1, "test_data_plan": {"plan_markdown": "plan"}},
                {},
                requires_user=False,
            )
    finally:
        await coordinator.close()

    assert exc_info.value.code == "AGENT_RECOVERY_EXHAUSTED"
    assert model.plan_calls == 3
    assert model.revision_calls == 15
    assert storage.load_draft(project_id, 4) is None
    summary = storage.load_record(project_id).recovery_summaries[4]
    assert summary.status == "failed"
    assert summary.generation_round == 3
    assert summary.repair_attempts == 15


@pytest.mark.asyncio
async def test_agent4_uses_one_working_template_and_repairs_only_generator(
    tmp_path: Path,
) -> None:
    failed = {
        "ok": False,
        "validation_level": "compile",
        "message": "generator 编译失败。",
        "checks": [
            {
                "operation": "compile",
                "role": "generator",
                "result": {"ok": False},
                "diagnostics": [{"message": "missing semicolon"}],
            }
        ],
    }
    passed = {
        "ok": True,
        "validation_level": "complete",
        "message": "passed",
        "checks": [],
    }
    model = RepairModel()
    verifier = SequenceVerifier([failed, passed])
    coordinator, storage, project_id = await _coordinator(tmp_path, model, verifier)
    try:
        _run_id, output, waiting = await coordinator.run_agent4(
            project_id, _context(), {}, requires_user=True
        )
    finally:
        await coordinator.close()

    assert output.confirmation == Confirmation.PASS
    assert waiting is True
    assert verifier.calls == 2
    assert model.generator_repairs == 1
    assert model.validator_repairs == 0
    draft = storage.load_draft(project_id, 5)
    assert draft is not None and draft["generator_code"].endswith("// repaired")
    state_root = storage.project_dir(project_id) / "state"
    assert not (state_root / "code-revisions").exists()
    assert len(list(state_root.glob(".working-code.*"))) == 1


@pytest.mark.asyncio
async def test_agent4_repairs_generator_when_semantic_analysis_audit_fails(
    tmp_path: Path,
) -> None:
    model = SemanticAuditRepairModel()
    verifier = SequenceVerifier(
        [{"ok": True, "validation_level": "complete", "message": "passed", "checks": []}]
    )
    coordinator, storage, project_id = await _coordinator(tmp_path, model, verifier)
    try:
        _thread, output, _waiting = await coordinator.run_agent4(
            project_id,
            _context(),
            {},
            requires_user=False,
        )
    finally:
        await coordinator.close()

    assert output.confirmation == Confirmation.PASS
    assert model.audit_calls == 2
    assert model.generator_repairs == 1
    assert verifier.calls == 1
    assert (storage.project_dir(project_id) / "state" / "generator-analysis.json").is_file()


@pytest.mark.asyncio
async def test_agent4_strips_code_fences_without_ai_repair(tmp_path: Path) -> None:
    generator_failed = {
        "ok": False,
        "validation_level": "compile",
        "message": "generator source contains Markdown fences",
        "checks": [
            {
                "operation": "compile",
                "role": "generator",
                "target_file": "generator.cpp",
                "result": {"ok": False},
            }
        ],
    }
    validator_failed = {
        "ok": False,
        "validation_level": "compile",
        "message": "validator source contains Markdown fences",
        "checks": [
            {
                "operation": "compile",
                "role": "validator",
                "target_file": "validator.cpp",
                "result": {"ok": False},
            }
        ],
    }
    passed = {"ok": True, "validation_level": "complete", "message": "passed", "checks": []}
    model = FencedCodeModel()
    coordinator, storage, project_id = await _coordinator(
        tmp_path,
        model,
        SequenceVerifier([generator_failed, validator_failed, passed]),
    )
    try:
        _run_id, output, _waiting = await coordinator.run_agent4(
            project_id, _context(), {}, requires_user=False
        )
    finally:
        await coordinator.close()

    assert output.confirmation == Confirmation.PASS
    assert model.generator_repairs == 0
    assert model.validator_repairs == 0
    assert not output.result["generator_code"].startswith("```")
    assert not output.result["validator_code"].startswith("```")
    assert storage.load_record(project_id).recovery_summaries[5].repair_attempts == 0


@pytest.mark.asyncio
async def test_environment_failure_stops_without_repair_or_regeneration(tmp_path: Path) -> None:
    model = EnvironmentFailureModel()
    coordinator, storage, project_id = await _coordinator(
        tmp_path, model, SequenceVerifier([{"ok": True}])
    )
    try:
        with pytest.raises(AppError) as exc_info:
            await coordinator.run_agent3(
                project_id,
                {"workflow_revision": 1, "test_data_plan": {"plan_markdown": "plan"}},
                {},
                requires_user=False,
            )
    finally:
        await coordinator.close()

    assert exc_info.value.code == "MODEL_UNAVAILABLE"
    assert exc_info.value.details["failure_class"] == "environment"
    assert model.generate_calls == 1
    assert model.revision_calls == 0
    summary = storage.load_record(project_id).recovery_summaries[4]
    assert summary.status == "failed"
    assert summary.generation_round == 1
    assert summary.repair_attempts == 0


@pytest.mark.asyncio
async def test_agent4_regenerates_after_five_repairs(tmp_path: Path) -> None:
    failed = {
        "ok": False,
        "failure_category": "compile",
        "validation_level": "compile",
        "message": "generator 编译失败。",
        "checks": [
            {
                "operation": "compile",
                "role": "generator",
                "target_file": "generator.cpp",
                "result": {"ok": False},
                "diagnostics": [{"message": "missing semicolon"}],
            }
        ],
    }
    passed = {
        "ok": True,
        "failure_category": None,
        "validation_level": "complete",
        "message": "passed",
        "checks": [],
    }
    model = RepairModel()
    verifier = SequenceVerifier([*[failed] * 6, passed])
    coordinator, storage, project_id = await _coordinator(tmp_path, model, verifier)
    try:
        _run_id, output, _waiting = await coordinator.run_agent4(
            project_id, _context(), {}, requires_user=False
        )
    finally:
        await coordinator.close()

    assert output.confirmation == Confirmation.PASS
    assert model.generator_repairs == 5
    assert verifier.calls == 7
    summary = storage.load_record(project_id).recovery_summaries[5]
    assert summary.generation_round == 2
    assert summary.repair_attempts == 5


@pytest.mark.asyncio
async def test_agent4_model_context_hides_runtime_parameter_instances(
    tmp_path: Path,
) -> None:
    model = RepairModel()
    verifier = SequenceVerifier(
        [{"ok": True, "validation_level": "complete", "message": "passed", "checks": []}]
    )
    coordinator, _storage, project_id = await _coordinator(tmp_path, model, verifier)
    try:
        await coordinator.run_agent4(project_id, _context(), {}, requires_user=False)
    finally:
        await coordinator.close()

    assert model.generator_context is not None
    assert model.validator_context is not None
    assert model.analysis_context is not None
    assert model.analysis_context["input"]["solution"]["source"] == "int main(){}"
    assert "compile" not in model.analysis_context["input"]["solution"]
    assert "difficulty" not in model.analysis_context["input"]["problem"]
    assert "generator_analysis" in model.generator_context
    assert "generator_analysis" not in model.validator_context
    assert "solution" not in model.generator_context["input"]
    assert "solution" not in model.validator_context["input"]
    assert all(
        "runtime_parameters" not in subtask for subtask in model.generator_context["subtasks"]
    )
    assert model.generator_context["runtime_parameter_schema"] == [
        {
            "subtask_id": 1,
            "parameters": [
                {
                    "name": "construction_mode",
                    "category": "structure",
                    "value_type": "string",
                },
                {
                    "name": "generation_profile",
                    "category": "structure",
                    "value_type": "string",
                },
                {"name": "n", "category": "size", "value_type": "integer"},
                {
                    "name": "variation_budget",
                    "category": "limit",
                    "value_type": "integer",
                },
            ],
        }
    ]
    assert model.generator_context["construction_controls"] == [
        {
            "subtask_id": 1,
            "profiles": [
                {
                    "generation_profile_id": "format",
                    "category": "rules_format",
                    "goal": "valid",
                    "controls": {"construction_mode": ["valid_constructed"]},
                },
                {
                    "generation_profile_id": "stress",
                    "category": "anti_algorithm",
                    "goal": "stress",
                    "controls": {"construction_mode": ["high_branching"]},
                },
                {
                    "generation_profile_id": "edge",
                    "category": "boundary_edge",
                    "goal": "edge",
                    "controls": {"construction_mode": ["boundary_extreme"]},
                },
            ],
        }
    ]
    assert "runtime_parameter_schema" not in model.validator_context
    assert "construction_controls" not in model.validator_context
    assert all(
        "runtime_parameters" not in subtask for subtask in model.validator_context["subtasks"]
    )
    assert all("generation_profiles" in subtask for subtask in model.generator_context["subtasks"])
    assert all(
        "generation_profiles" not in subtask for subtask in model.validator_context["subtasks"]
    )


@pytest.mark.asyncio
async def test_agent4_does_not_repair_unowned_solution_failure(tmp_path: Path) -> None:
    failed = {
        "ok": False,
        "validation_level": "complete",
        "message": "solution failed",
        "checks": [
            {
                "operation": "solve",
                "role": "solution",
                "result": {"ok": False},
            }
        ],
    }
    model = RepairModel()
    coordinator, _storage, project_id = await _coordinator(
        tmp_path, model, SequenceVerifier([failed])
    )
    try:
        with pytest.raises(AppError) as exc_info:
            await coordinator.run_agent4(project_id, _context(), {}, requires_user=False)
    finally:
        await coordinator.close()

    assert exc_info.value.code == "AGENT_RECOVERY_REROUTED"
    assert exc_info.value.details["recovery_plan"]["root_stage"] == 2
    assert exc_info.value.details["recovery_plan"]["requires_user_authorization"] is True
    assert model.generator_repairs == 0
    assert model.validator_repairs == 0
