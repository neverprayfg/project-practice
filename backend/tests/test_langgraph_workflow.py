from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import DeterministicTestModel

from app.config import Settings
from app.errors import AppError
from app.models import (
    CodeDraft,
    CodeRepairPatch,
    Confirmation,
    Counterexample,
    CounterexampleLedger,
    Defect,
    DefectIdentity,
    ProjectCreate,
    ReportedDefect,
    SemanticAudit,
    TargetedDefectCheck,
)
from app.services.agent_graphs import (
    AgentGraphCoordinator,
    _merge_implementation_mappings,
)
from app.services.candidate_verifier import AGENT4_VERIFIER_REVISION
from app.services.counterexample_ledger import CounterexampleLedgerService
from app.services.defects import stable_defect_id
from app.services.jngen_document_context import JngenDocumentContext
from app.services.project_service import ProjectService
from app.services.proof_obligations import (
    _parameter_affects_code,
    implementation_mapping_issues,
    resolve_implementation_mapping,
)
from app.services.structure_tag_catalog import StructureTagCatalog
from app.storage import ProjectStorage


def _context() -> dict[str, Any]:
    return {
        "workflow_revision": 1,
        "confirmed_structure_tags": [
            {
                "tag_id": "primitive.integer",
                "applies_to": "n",
                "evidence": "输入包含整数 n。",
            }
        ],
        "subtasks": [
            {
                "id": 1,
                "constraints": "1 <= n <= 10",
                "test_count": 1,
                "expected_complexity": "O(n)",
                "special_cases": [],
                "runtime_parameters": [
                    {
                        "case_id": 1,
                        "parameters": [{"name": "n", "value": 10, "category": "size"}],
                    }
                ],
                "subtask_tags": [],
            }
        ],
    }


class PassVerifier:
    def __init__(self) -> None:
        self.calls = 0
        self.replayed: list[list[str]] = []

    async def verify(
        self,
        project_id: str,
        candidate: dict[str, Any],
        context: dict[str, Any],
        counterexamples: list[Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del project_id, context
        self.calls += 1
        self.replayed.append([item.counterexample_id for item in counterexamples])
        return candidate, {
            "ok": True,
            "validation_level": "complete",
            "message": "passed",
            "checks": [{"operation": "compile", "role": "generator", "ok": True}],
        }


class SemanticRepairModel(DeterministicTestModel):
    def __init__(self, *, closes_target: bool, regress_known: str | None = None) -> None:
        self.closes_target = closes_target
        self.regress_known = regress_known
        self.audit_calls = 0
        self.repair_calls = 0
        self.recheck_calls: list[str] = []
        identity = DefectIdentity(
            category="semantic_constraint",
            target_file="generator.cpp",
            constraint_id="subtask:1:constraints",
            subtask="1",
            test_point="1",
            error_code="CONSTRAINT_NOT_GUARANTEED",
        )
        self.target_id = stable_defect_id(identity)
        self.report = ReportedDefect(
            origin="candidate",
            identity=identity,
            message="生成逻辑没有保证子任务约束。",
            evidence={"symbol": "main"},
        )

    async def agent4_audit(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
    ) -> SemanticAudit:
        del context, candidate, execution
        self.audit_calls += 1
        return SemanticAudit(defects=[self.report])

    async def agent4_repair(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        target_defect: Any,
    ) -> CodeRepairPatch:
        del context
        self.repair_calls += 1
        return CodeRepairPatch(
            target_defect_id=target_defect.defect_id,
            rationale="仅修改目标生成器缺陷。",
            generator_code=candidate["generator_code"] + "\n// targeted patch",
        )

    async def agent4_recheck(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        target_defect: Any,
        execution: dict[str, Any],
    ) -> TargetedDefectCheck:
        del context, candidate, execution
        self.recheck_calls.append(target_defect.defect_id)
        still_present = (
            not self.closes_target if target_defect.defect_id == self.target_id else False
        )
        if self.regress_known == target_defect.defect_id:
            still_present = True
        return TargetedDefectCheck(
            defect_id=target_defect.defect_id,
            still_present=still_present,
            message="仍存在" if still_present else "已关闭",
        )


class UpstreamAuditModel(DeterministicTestModel):
    def __init__(self) -> None:
        self.audit_calls = 0
        self.repair_calls = 0

    async def agent4_audit(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
    ) -> SemanticAudit:
        del context, candidate, execution
        self.audit_calls += 1
        return SemanticAudit(
            defects=[
                ReportedDefect(
                    origin="upstream_contract",
                    identity=DefectIdentity(
                        category="UPSTREAM_CONTRACT_CONTRADICTION",
                        target_file="stage4_contract",
                        constraint_id="subtask:1:constraints",
                        subtask="subtask:1",
                        test_point="case:1",
                        error_code="CONTRACT_CONTRADICTION",
                    ),
                    message="阶段四约束与特殊情况相互矛盾。",
                )
            ]
        )

    async def agent4_repair(self, *args: Any, **kwargs: Any) -> CodeRepairPatch:
        self.repair_calls += 1
        raise AssertionError("upstream defects must never enter code repair")


class FailGenerationOnceModel(DeterministicTestModel):
    def __init__(self) -> None:
        self.calls = 0

    async def agent4_generate(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise AppError(
                "MODEL_FAILED",
                "模型响应契约无效。",
                details={"failure_kind": "schema_validation"},
            )
        return await super().agent4_generate(context, candidate)


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
    docs_root = Path(__file__).parents[1] / "app" / "jngen_doc_context"
    catalog = StructureTagCatalog(docs_root)
    coordinator = AgentGraphCoordinator(
        Settings(app_env="test", storage_root=storage.root),
        storage,
        model,
        verifier,
        JngenDocumentContext(docs_root, catalog),
        catalog,
    )
    await coordinator.start()
    return coordinator, storage, record.project_id


@pytest.mark.asyncio
async def test_each_agent_has_an_independent_state_graph(tmp_path: Path) -> None:
    coordinator, _storage, _project_id = await _coordinator(
        tmp_path, DeterministicTestModel(), PassVerifier()
    )
    try:
        assert coordinator.agent1 is not None
        assert coordinator.agent2 is not None
        assert coordinator.agent3 is not None
        assert coordinator.agent4 is not None
        node_sets = [
            set(agent.graph.get_graph().nodes)
            for agent in (
                coordinator.agent1,
                coordinator.agent2,
                coordinator.agent3,
                coordinator.agent4,
            )
        ]
        assert node_sets[0] == {"__start__", "normalize", "validate", "__end__"}
        assert "draft_structure" in node_sets[1]
        assert "plan_subtasks" in node_sets[2]
        assert {"semantic_audit", "repair_defect", "evaluate_progress"}.issubset(node_sets[3])
        assert (
            len(
                {
                    id(agent.graph)
                    for agent in (
                        coordinator.agent1,
                        coordinator.agent2,
                        coordinator.agent3,
                        coordinator.agent4,
                    )
                }
            )
            == 4
        )
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_stage_four_contract_preflight_stops_before_model_call(tmp_path: Path) -> None:
    model = SemanticRepairModel(closes_target=True)
    verifier = PassVerifier()
    coordinator, _storage, project_id = await _coordinator(tmp_path, model, verifier)
    invalid = _context()
    invalid["subtasks"][0]["runtime_parameters"] = []
    try:
        with pytest.raises(AppError) as raised:
            await coordinator.run_agent4(project_id, invalid, {}, requires_user=False)
    finally:
        await coordinator.close()

    assert raised.value.code == "UPSTREAM_CONTRACT_INVALID"
    assert raised.value.stage == 4
    assert verifier.calls == 0
    assert model.audit_calls == 0


@pytest.mark.asyncio
async def test_stage_four_runtime_parameters_must_satisfy_arithmetic_constraints(
    tmp_path: Path,
) -> None:
    model = SemanticRepairModel(closes_target=True)
    verifier = PassVerifier()
    coordinator, _storage, project_id = await _coordinator(tmp_path, model, verifier)
    invalid = _context()
    invalid["subtasks"][0]["constraints"] = "n <= 10, m = n*(n-1)/2"
    invalid["subtasks"][0]["runtime_parameters"][0]["parameters"].append(
        {"name": "m", "value": 90, "category": "size"}
    )
    try:
        with pytest.raises(AppError) as raised:
            await coordinator.run_agent4(project_id, invalid, {}, requires_user=False)
    finally:
        await coordinator.close()

    assert raised.value.code == "UPSTREAM_CONTRACT_INVALID"
    assert raised.value.stage == 4
    assert "m = n*(n-1)/2" in str(raised.value.details)
    assert verifier.calls == 0
    assert model.audit_calls == 0


@pytest.mark.asyncio
async def test_semantic_audit_returns_upstream_defect_without_repair(
    tmp_path: Path,
) -> None:
    model = UpstreamAuditModel()
    verifier = PassVerifier()
    coordinator, _storage, project_id = await _coordinator(tmp_path, model, verifier)
    try:
        with pytest.raises(AppError) as raised:
            await coordinator.run_agent4(
                project_id, _context(), {}, requires_user=False
            )
    finally:
        await coordinator.close()

    assert raised.value.code == "UPSTREAM_CONTRACT_INVALID"
    assert raised.value.stage == 4
    assert model.audit_calls == 1
    assert model.repair_calls == 0


@pytest.mark.asyncio
async def test_agent4_protocol_failure_keeps_thread_and_resumes_failed_node(
    tmp_path: Path,
) -> None:
    model = FailGenerationOnceModel()
    coordinator, _storage, project_id = await _coordinator(tmp_path, model, PassVerifier())
    try:
        with pytest.raises(AppError) as raised:
            await coordinator.run_agent4(project_id, _context(), {}, requires_user=False)
        thread_id = raised.value.details["thread_id"]
        resumed_thread, output, waiting = await coordinator.retry_agent4(thread_id)
    finally:
        await coordinator.close()

    assert resumed_thread == thread_id
    assert output.confirmation == Confirmation.PASS
    assert waiting is False
    assert model.calls == 2


@pytest.mark.asyncio
async def test_same_defect_is_repaired_once_then_rolled_back_and_stopped(tmp_path: Path) -> None:
    model = SemanticRepairModel(closes_target=False)
    verifier = PassVerifier()
    coordinator, storage, project_id = await _coordinator(tmp_path, model, verifier)
    try:
        _thread, output, waiting = await coordinator.run_agent4(
            project_id, _context(), {}, requires_user=False
        )
    finally:
        await coordinator.close()

    assert output.confirmation == Confirmation.REVISE
    assert waiting is False
    assert model.audit_calls == 1
    assert model.repair_calls == 1
    assert model.recheck_calls.count(model.target_id) == 1
    assert any("修复一次后仍存在" in issue for issue in output.issues)
    ledger = storage.load_agent4_ledger(project_id)
    history = ledger["counterexamples"][0]["repair_history"]
    assert history[-1]["outcome"] == "rolled_back"


@pytest.mark.asyncio
async def test_targeted_recheck_closes_defect_without_second_open_audit(tmp_path: Path) -> None:
    model = SemanticRepairModel(closes_target=True)
    verifier = PassVerifier()
    coordinator, storage, project_id = await _coordinator(tmp_path, model, verifier)
    try:
        _thread, output, waiting = await coordinator.run_agent4(
            project_id, _context(), {}, requires_user=False
        )
    finally:
        await coordinator.close()

    assert output.confirmation == Confirmation.PASS
    assert waiting is False
    assert model.audit_calls == 1
    assert model.repair_calls == 1
    assert storage.load_agent4_ledger(project_id)["counterexamples"][0]["status"] == "closed"
    decisions = (
        (storage.project_dir(project_id) / "logs" / "agent4-decisions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    parsed = [json.loads(line) for line in decisions]
    assert any(item["decision"] == "accepted" and item["progress"] for item in parsed)


@pytest.mark.asyncio
async def test_reintroduced_closed_defect_rolls_back_otherwise_successful_patch(
    tmp_path: Path,
) -> None:
    known_identity = DefectIdentity(
        category="semantic_constraint",
        target_file="validator.cpp",
        constraint_id="input:tag:primitive.integer",
        subtask="all",
        test_point="all",
        error_code="VALIDATOR_RANGE_GAP",
    )
    known = Defect(
        defect_id=stable_defect_id(known_identity),
        identity=known_identity,
        validation_level="semantic",
        message="已关闭的 validator 范围缺陷。",
    )
    model = SemanticRepairModel(closes_target=True, regress_known=known.defect_id)
    verifier = PassVerifier()
    coordinator, storage, project_id = await _coordinator(tmp_path, model, verifier)
    storage.save_agent4_ledger(
        project_id,
        CounterexampleLedger(
            counterexamples=[
                Counterexample(
                    counterexample_id="case_" + "1" * 20,
                    defect=known,
                    status="closed",
                    first_seen_revision="old",
                    last_seen_revision="old",
                )
            ],
            last_valid_candidate_revision="old",
        ).model_dump(mode="json"),
    )
    try:
        _thread, output, _waiting = await coordinator.run_agent4(
            project_id, _context(), {}, requires_user=False
        )
    finally:
        await coordinator.close()

    assert output.confirmation == Confirmation.REVISE
    assert any("重新引入" in issue for issue in output.issues)
    assert known.defect_id in model.recheck_calls
    decisions = (storage.project_dir(project_id) / "logs" / "agent4-decisions.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"decision":"rolled_back"' in decisions


@pytest.mark.asyncio
async def test_candidate_revision_cache_skips_reverification(tmp_path: Path) -> None:
    model = DeterministicTestModel()
    verifier = PassVerifier()
    coordinator, _storage, project_id = await _coordinator(tmp_path, model, verifier)
    try:
        _thread, first, _waiting = await coordinator.run_agent4(
            project_id, _context(), {}, requires_user=False
        )
        assert first.confirmation == Confirmation.PASS
        first_calls = verifier.calls
        _thread, second, _waiting = await coordinator.run_agent4(
            project_id, _context(), first.result or {}, requires_user=False
        )
    finally:
        await coordinator.close()

    assert second.confirmation == Confirmation.PASS
    assert verifier.calls == first_calls


def test_stable_defect_id_ignores_message_logs_and_line_numbers() -> None:
    identity = DefectIdentity(
        category="compile",
        target_file="generator.cpp",
        constraint_id="subtask:1:constraints",
        subtask="1",
        test_point="2",
        error_code="CPP_UNDECLARED_IDENTIFIER",
    )
    assert stable_defect_id(identity) == stable_defect_id(
        {**identity.model_dump(), "message": "different", "line": 999}
    )


def test_mapping_rejects_parameter_that_is_only_read(tmp_path: Path) -> None:
    docs_root = Path(__file__).parents[1] / "app" / "jngen_doc_context"
    catalog = StructureTagCatalog(docs_root)
    documents = JngenDocumentContext(docs_root, catalog).load_documents(["random.md"])
    document = documents["selected_documents"][0]
    obligation = {
        "constraint_id": "subtask:1:case:1:parameter:n",
        "scope": "subtask:1:case:1",
        "severity": "blocker",
        "verification_method": "static",
        "requirement": "n 必须影响构造。",
    }
    submission = DeterministicTestModel._code_draft(
        {
            "subtasks": [],
            "proof_obligations": [obligation],
            "jngen_documentation": documents,
        }
    )
    draft = CodeDraft(
        generator_code=submission.generator_code,
        validator_code=submission.validator_code,
        proof_obligations=[obligation],
        implementation_mapping=resolve_implementation_mapping(
            submission.implementation_mapping,
            documents["selected_documents"],
        ),
    )
    draft.generator_code = draft.generator_code.replace(
        "auto sample=Array::random(1,0,0);",
        '(void)getOpt("n");\nauto sample=Array::random(1,0,0);',
    )
    draft.implementation_mapping[0].used_parameters = ["n"]
    issues = implementation_mapping_issues(draft, {"random.md": document})

    assert any("没有可验证的实际用途" in issue for issue in issues)


def test_parameter_effect_accepts_assignment_after_declaration() -> None:
    code = """
int main() {
    int n;
    n = getOpt("n", 5);
    auto graph = Graph::random(n, 7);
}
"""

    assert _parameter_affects_code(code, "n") is True
    assert _parameter_affects_code('(void)getOpt("n");', "n") is False


def test_ledger_from_old_verifier_is_invalidated(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path)
    project_id = "a" * 32
    storage.save_agent4_ledger(
        project_id,
        {
            "counterexamples": [{"stale": "old-verifier-result"}],
            "last_valid_candidate_revision": "stale",
        },
    )
    storage.save_agent4_last_valid_candidate(project_id, {"revision_id": "stale"})

    ledger = CounterexampleLedgerService(storage).load(project_id)

    assert ledger.verifier_revision == AGENT4_VERIFIER_REVISION
    assert ledger.counterexamples == []
    assert ledger.last_valid_candidate_revision is None
    assert storage.load_agent4_ledger(project_id)["verifier_revision"] == (
        AGENT4_VERIFIER_REVISION
    )
    assert storage.load_agent4_last_valid_candidate(project_id) is None


def test_incremental_mapping_patch_retains_untouched_constraints() -> None:
    current = [
        {"constraint_id": "constraint:a", "test_strategy": "old-a"},
        {"constraint_id": "constraint:b", "test_strategy": "old-b"},
        {"constraint_id": "constraint:obsolete", "test_strategy": "remove"},
    ]
    merged = _merge_implementation_mappings(
        current,
        [{"constraint_id": "constraint:a", "test_strategy": "new-a"}],
        ["constraint:obsolete"],
        ["constraint:a", "constraint:b"],
    )

    assert merged == [
        {"constraint_id": "constraint:a", "test_strategy": "new-a"},
        {"constraint_id": "constraint:b", "test_strategy": "old-b"},
    ]
