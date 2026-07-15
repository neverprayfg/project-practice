from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeSandbox

from app.config import Settings
from app.models import (
    Confirmation,
    JngenDocumentChoice,
    JngenDocumentSelection,
    ProjectCreate,
    Stage,
    TaskType,
    WorkflowOutput,
)
from app.services.candidate_verifier import AgentCandidateVerifier
from app.services.jngen_document_context import JngenDocumentContext
from app.services.langgraph_runner import LangGraphAgentRunner
from app.services.project_service import ProjectService
from app.storage import ProjectStorage


class RepairingModel:
    def __init__(self) -> None:
        self.generations = 0
        self.reviews = 0

    async def run(
        self,
        task_type: TaskType,
        phase: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
        issues: list[str],
    ) -> WorkflowOutput:
        del task_type, context, issues
        if phase == "generate":
            self.generations += 1
            return WorkflowOutput(
                confirmation=Confirmation.REVISE,
                result={"template": f"第 {self.generations} 版输入结构。", "issues": []},
            )
        self.reviews += 1
        return WorkflowOutput(
            confirmation=Confirmation.PASS if execution.get("ok") else Confirmation.REVISE,
            result=candidate,
            issues=[] if execution.get("ok") else ["需要根据检查结果修复。"],
        )


class SecondAttemptVerifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(
        self,
        project_id: str,
        task_type: TaskType,
        candidate: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del project_id, task_type, context
        self.calls += 1
        if self.calls == 1:
            return candidate, {"ok": False, "message": "第一轮检查失败。"}
        return candidate, {"ok": True, "message": "第二轮检查通过。"}


@pytest.mark.asyncio
async def test_langgraph_agent_self_repairs_then_interrupts_for_user(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        storage_root=tmp_path,
        agent_max_iterations=3,
    )
    storage = ProjectStorage(tmp_path)
    model = RepairingModel()
    verifier = SecondAttemptVerifier()
    documents = JngenDocumentContext(tmp_path / "jngen-docs")
    runner = LangGraphAgentRunner(settings, storage, model, verifier, documents)  # type: ignore[arg-type]
    try:
        thread_id, output, waiting_user = await runner.run(
            "0" * 32,
            TaskType.INPUT_STRUCTURE,
            {"input": {}},
            None,
            requires_user=True,
        )

        assert output.confirmation == Confirmation.PASS
        assert output.result["template"] == "第 2 版输入结构。"
        assert waiting_user is True
        assert model.generations == 2
        assert model.reviews == 2
        resumed = await runner.resume_confirmation(thread_id)
        assert resumed["user_confirmed"] is True
        assert (tmp_path / "langgraph-checkpoints.sqlite").is_file()
    finally:
        await runner.close()


class LastAllowedRepairModel:
    def __init__(self) -> None:
        self.generations = 0
        self.reviews = 0

    async def run(
        self,
        task_type: TaskType,
        phase: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
        issues: list[str],
    ) -> WorkflowOutput:
        del task_type, context, issues
        if phase == "generate":
            self.generations += 1
            return WorkflowOutput(
                confirmation=Confirmation.REVISE,
                result={"template": "无法编译的版本"},
            )
        self.reviews += 1
        if execution.get("ok"):
            return WorkflowOutput(confirmation=Confirmation.PASS, result=candidate)
        return WorkflowOutput(
            confirmation=Confirmation.REVISE,
            result={"template": "最后一次修复后的版本"},
        )


class PassesOnlyRepairedCandidateVerifier:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def verify(
        self,
        project_id: str,
        task_type: TaskType,
        candidate: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del project_id, task_type, context
        template = str(candidate["template"])
        self.seen.append(template)
        return candidate, {
            "ok": template == "最后一次修复后的版本",
            "message": "编译通过" if template == "最后一次修复后的版本" else "编译失败",
        }


@pytest.mark.asyncio
async def test_last_allowed_repair_is_verified_before_loop_exhaustion(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        storage_root=tmp_path,
        agent_max_iterations=1,
    )
    storage = ProjectStorage(tmp_path)
    model = LastAllowedRepairModel()
    verifier = PassesOnlyRepairedCandidateVerifier()
    runner = LangGraphAgentRunner(
        settings,
        storage,
        model,
        verifier,  # type: ignore[arg-type]
        JngenDocumentContext(tmp_path / "jngen-docs"),
    )
    try:
        _thread_id, output, waiting_user = await runner.run(
            "0" * 32,
            TaskType.INPUT_STRUCTURE,
            {"input": {}},
            None,
            requires_user=False,
        )
    finally:
        await runner.close()

    assert output.confirmation == Confirmation.PASS
    assert output.result == {"template": "最后一次修复后的版本"}
    assert output.issues == []
    assert waiting_user is False
    assert verifier.seen == ["无法编译的版本", "最后一次修复后的版本"]
    assert model.generations == 1
    assert model.reviews == 2


class SelectThenGenerateModel:
    def __init__(self) -> None:
        self.generations = 0
        self.selection_inputs: list[tuple[dict[str, Any], list[str]]] = []
        self.saw_selected_document = False

    async def select_jngen_documents(
        self,
        context: dict[str, Any],
        available_filenames: list[str],
    ) -> JngenDocumentSelection:
        self.selection_inputs.append((context, available_filenames))
        if len(self.selection_inputs) == 1:
            filename = "graph.md"
        else:
            assert context["jngen_documentation"]["selected_documents"][0][
                "filename"
            ] == "graph.md"
            filename = "array.md"
        return JngenDocumentSelection(
            selected_documents=[
                JngenDocumentChoice(
                    filename=filename,
                    reason="需要生成图数据。",
                )
            ],
            selection_complete=len(self.selection_inputs) >= 2,
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
        del task_type, issues
        if phase == "review":
            return WorkflowOutput(confirmation=Confirmation.PASS, result=candidate)
        self.generations += 1
        del execution
        documents = context["jngen_documentation"]["selected_documents"]
        self.saw_selected_document = bool(
            documents
            and documents[0]["filename"] == "graph.md"
            and "Graph::random" in documents[0]["content"]
        )
        return WorkflowOutput(
            confirmation=Confirmation.REVISE,
            result={"generator_code": "generator", "validator_code": "validator"},
        )


class AlwaysPassVerifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(
        self,
        project_id: str,
        task_type: TaskType,
        candidate: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del project_id, task_type, context
        self.calls += 1
        return candidate, {"ok": True}


@pytest.mark.asyncio
async def test_code_agent_selects_and_embeds_documents_before_verification(
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
    projects.mark_solution_compiled(record.project_id, True, None)
    current = projects.get(record.project_id)
    current.current_stage = Stage.CODE_DRAFT
    storage.save_record(current)
    docs_root = tmp_path / "jngen-docs"
    docs_root.mkdir()
    (docs_root / "._graph.md").write_bytes(b"macOS metadata")
    (docs_root / "array.md").write_text("Use Array::random.", encoding="utf-8")
    (docs_root / "graph.md").write_text("Use Graph::random.", encoding="utf-8")
    settings = Settings(
        app_env="test",
        storage_root=storage.root,
        agent_max_iterations=2,
        agent_allow_legacy_keyword_routing=True,
    )
    model = SelectThenGenerateModel()
    verifier = AlwaysPassVerifier()
    documents = JngenDocumentContext(docs_root)
    runner = LangGraphAgentRunner(
        settings,
        storage,
        model,
        verifier,
        documents,
    )  # type: ignore[arg-type]
    try:
        _thread_id, output, waiting_user = await runner.run(
            record.project_id,
            TaskType.CODE_DRAFT,
            {"input": {}, "subtasks": []},
            None,
            requires_user=False,
        )
    finally:
        await runner.close()

    assert output.confirmation == Confirmation.PASS
    assert waiting_user is False
    assert model.generations == 1
    assert model.saw_selected_document is True
    assert model.selection_inputs[0][1] == ["array.md", "graph.md"]
    assert len(model.selection_inputs) == 2
    assert verifier.calls == 1
    assert not (storage.project_dir(record.project_id) / "logs" / "tool-audit.jsonl").exists()


class DiagnosticDrivenCodeModel:
    def __init__(self) -> None:
        self.selection_contexts: list[dict[str, Any]] = []

    async def select_jngen_documents(
        self,
        context: dict[str, Any],
        available_filenames: list[str],
    ) -> JngenDocumentSelection:
        assert available_filenames == ["graph.md"]
        self.selection_contexts.append(context)
        if context.get("jngen_documentation", {}).get("selected_documents"):
            return JngenDocumentSelection(
                selected_documents=[],
                selection_complete=True,
            )
        return JngenDocumentSelection(
            selected_documents=[
                JngenDocumentChoice(filename="graph.md", reason="根据当前诊断选择。")
            ],
            selection_complete=False,
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
        del task_type, issues
        if phase == "generate":
            return WorkflowOutput(
                confirmation=Confirmation.REVISE,
                result={"generator_code": "bad", "validator_code": "validator"},
            )
        if execution.get("ok"):
            return WorkflowOutput(confirmation=Confirmation.PASS, result=candidate)
        if context.get("recovery_feedback"):
            assert context["recovery_feedback"][-1]["source"] == (
                "deterministic_verifier"
            )
            assert context["recovery_feedback"][-1]["execution"]["checks"][0][
                "diagnostics"
            ][0]["message"] == "unknown compiler failure"
        return WorkflowOutput(
            confirmation=Confirmation.REVISE,
            result={"generator_code": "fixed", "validator_code": "validator"},
        )


class DiagnosticDrivenVerifier:
    def __init__(self, *, retrieval_required: bool = True) -> None:
        self.retrieval_required = retrieval_required

    async def verify(
        self,
        project_id: str,
        task_type: TaskType,
        candidate: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del project_id, task_type, context
        if candidate["generator_code"] == "fixed":
            return candidate, {"ok": True, "checks": []}
        return candidate, {
            "ok": False,
            "message": "generator 编译失败。",
            "failure_category": (
                "library_api" if self.retrieval_required else "compile"
            ),
            "retrieval_required": self.retrieval_required,
            "checks": [
                {
                    "operation": "compile",
                    "role": "generator",
                    "diagnostics": [
                        {
                            "severity": "error",
                            "line": 1,
                            "message": "unknown compiler failure",
                        }
                    ],
                    "result": {"exit_code": 1, "stdout": "", "stderr": "raw error"},
                }
            ],
        }


@pytest.mark.asyncio
async def test_code_agent_reselects_documents_from_structured_failure_feedback(
    tmp_path: Path,
) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    docs_root = tmp_path / "jngen-docs"
    docs_root.mkdir()
    (docs_root / "graph.md").write_text("Graph APIs", encoding="utf-8")
    model = DiagnosticDrivenCodeModel()
    runner = LangGraphAgentRunner(
        Settings(
            app_env="test",
            storage_root=storage.root,
            agent_max_iterations=1,
            agent_allow_legacy_keyword_routing=True,
        ),
        storage,
        model,
        DiagnosticDrivenVerifier(),  # type: ignore[arg-type]
        JngenDocumentContext(docs_root),
    )
    try:
        _thread_id, output, waiting_user = await runner.run(
            "0" * 32,
            TaskType.CODE_DRAFT,
            {"input": {}, "subtasks": []},
            None,
            requires_user=False,
        )
    finally:
        await runner.close()

    assert output.confirmation == Confirmation.PASS
    assert output.result["generator_code"] == "fixed"
    assert waiting_user is False
    assert len(model.selection_contexts) == 3
    assert "recovery_feedback" not in model.selection_contexts[0]
    assert "recovery_feedback" not in model.selection_contexts[1]
    assert model.selection_contexts[2]["recovery_feedback"][-1]["source"] == (
        "deterministic_verifier"
    )


@pytest.mark.asyncio
async def test_code_agent_does_not_retrieve_documents_for_compile_failure(
    tmp_path: Path,
) -> None:
    storage = ProjectStorage(tmp_path / "storage")
    docs_root = tmp_path / "jngen-docs"
    docs_root.mkdir()
    (docs_root / "graph.md").write_text("Graph APIs", encoding="utf-8")
    model = DiagnosticDrivenCodeModel()
    runner = LangGraphAgentRunner(
        Settings(
            app_env="test",
            storage_root=storage.root,
            agent_max_iterations=1,
            agent_allow_legacy_keyword_routing=True,
        ),
        storage,
        model,
        DiagnosticDrivenVerifier(retrieval_required=False),  # type: ignore[arg-type]
        JngenDocumentContext(docs_root),
    )
    try:
        _thread_id, output, waiting_user = await runner.run(
            "0" * 32,
            TaskType.CODE_DRAFT,
            {"input": {}, "subtasks": []},
            None,
            requires_user=False,
        )
    finally:
        await runner.close()

    assert output.confirmation == Confirmation.PASS
    assert output.result is not None
    assert output.result["generator_code"] == "fixed"
    assert waiting_user is False
    assert len(model.selection_contexts) == 2
    assert all("recovery_feedback" not in item for item in model.selection_contexts)


@pytest.mark.asyncio
async def test_candidate_verifier_rejects_complex_generator_without_jngen(
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
    record.subtasks_revision = 1
    storage.save_record(record)
    sandbox = FakeSandbox(storage.root)
    verifier = AgentCandidateVerifier(
        Settings(app_env="test", storage_root=storage.root), storage, sandbox
    )

    _candidate, execution = await verifier.verify(
        record.project_id,
        TaskType.CODE_DRAFT,
        {
            "generator_code": (
                '#include "testlib.h"\n'
                "int main(int argc,char** argv){registerGen(argc,argv,1);}"
            ),
            "validator_code": (
                '#include "testlib.h"\n'
                "int main(int argc,char** argv){registerValidation(argc,argv);}"
            ),
        },
        {
            "jngen_documentation": {
                "selected_documents": [
                    {"filename": "graph.md", "content": "Graph APIs"}
                ]
            }
        },
    )

    assert execution["ok"] is False
    assert execution["checks"][1]["operation"] == "jngen_usage"
    assert sandbox.calls == []
