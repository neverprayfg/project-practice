from __future__ import annotations

import hashlib
import json
from contextlib import AbstractAsyncContextManager
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.config import Settings
from app.errors import AppError
from app.models import Confirmation, TaskType, WorkflowOutput
from app.services.candidate_verifier import AgentCandidateVerifier
from app.services.jngen_document_context import JngenDocumentContext
from app.services.model_client import AgentModel
from app.storage import ProjectStorage


class AgentLoopState(TypedDict, total=False):
    run_id: str
    project_id: str
    task_type: str
    context: dict[str, Any]
    candidate: dict[str, Any]
    execution: dict[str, Any]
    issues: list[str]
    attempts: int
    max_iterations: int
    requires_user: bool
    complete: bool
    exhausted: bool
    user_confirmed: bool
    candidate_changed: bool


class LangGraphAgentRunner:
    def __init__(
        self,
        settings: Settings,
        storage: ProjectStorage,
        model: AgentModel,
        verifier: AgentCandidateVerifier,
        jngen_documents: JngenDocumentContext,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.model = model
        self.verifier = verifier
        self.jngen_documents = jngen_documents
        self._saver_context: AbstractAsyncContextManager[AsyncSqliteSaver] | None = None
        self._saver: AsyncSqliteSaver | None = None
        self._graph: Any = None

    async def start(self) -> None:
        if self._graph is not None:
            return
        checkpoint_path = self.storage.root / "langgraph-checkpoints.sqlite"
        self._saver_context = AsyncSqliteSaver.from_conn_string(str(checkpoint_path))
        self._saver = await self._saver_context.__aenter__()
        builder = StateGraph(AgentLoopState)
        builder.add_node("generate", self._generate)
        builder.add_node("verify", self._verify)
        builder.add_node("review", self._review)
        builder.add_node("wait_user", self._wait_user)
        builder.add_edge(START, "generate")
        builder.add_edge("generate", "verify")
        builder.add_edge("verify", "review")
        builder.add_conditional_edges(
            "review",
            self._route_after_review,
            {
                "generate": "generate",
                "verify": "verify",
                "wait_user": "wait_user",
                "end": END,
            },
        )
        builder.add_edge("wait_user", END)
        self._graph = builder.compile(checkpointer=self._saver)

    async def close(self) -> None:
        if self._saver_context is not None:
            await self._saver_context.__aexit__(None, None, None)
        self._saver_context = None
        self._saver = None
        self._graph = None

    async def run(
        self,
        project_id: str,
        task_type: TaskType,
        context: dict[str, Any],
        candidate: dict[str, Any] | None,
        *,
        requires_user: bool,
        initial_issues: list[str] | None = None,
    ) -> tuple[str, WorkflowOutput, bool]:
        await self.start()
        workflow_revision = int(context.get("workflow_revision", 1))
        thread_id = (
            f"{project_id}:{task_type.value}:r{workflow_revision}:{uuid4().hex}"
        )
        if task_type == TaskType.CODE_DRAFT:
            try:
                context = await self._with_selected_jngen_documents(
                    context,
                    project_id=project_id,
                    run_id=thread_id,
                    purpose="initial",
                )
            except AppError as exc:
                self.storage.append_agent4_document_selection(
                    project_id,
                    {
                        "run_id": thread_id,
                        "event": "selection_failed",
                        "purpose": "initial",
                        "error": exc.payload(),
                    },
                )
                raise
        config = self._config(thread_id)
        state = await self._graph.ainvoke(
            {
                "run_id": thread_id,
                "project_id": project_id,
                "task_type": task_type.value,
                "context": context,
                "candidate": candidate or {},
                "execution": {},
                "issues": initial_issues or [],
                "attempts": 0,
                "max_iterations": self.settings.agent_max_iterations,
                "requires_user": requires_user,
                "complete": False,
                "exhausted": False,
                "user_confirmed": False,
                "candidate_changed": False,
            },
            config,
        )
        waiting_user = bool(state.get("__interrupt__"))
        return thread_id, self._to_output(state), waiting_user

    async def resume_confirmation(self, thread_id: str) -> dict[str, Any]:
        await self.start()
        state = await self._graph.ainvoke(Command(resume=True), self._config(thread_id))
        if not state.get("user_confirmed"):
            raise AppError("CONFIRMATION_FAILED", "LangGraph 未记录用户确认", status_code=409)
        return state

    async def _generate(self, state: AgentLoopState) -> dict[str, Any]:
        output = await self.model.run(
            TaskType(state["task_type"]),
            "generate",
            state["context"],
            state.get("candidate", {}),
            state.get("execution", {}),
            state.get("issues", []),
        )
        issues = list(output.issues)
        return {
            "candidate": output.result or state.get("candidate", {}),
            "issues": issues,
            "execution": {},
            "complete": False,
            "exhausted": False,
            "candidate_changed": False,
        }

    async def _verify(self, state: AgentLoopState) -> dict[str, Any]:
        candidate, execution = await self.verifier.verify(
            state["project_id"],
            TaskType(state["task_type"]),
            state.get("candidate", {}),
            state["context"],
        )
        execution = dict(execution)
        execution["verified_candidate_fingerprint"] = self._candidate_fingerprint(candidate)
        return {"candidate": candidate, "execution": execution}

    async def _review(self, state: AgentLoopState) -> dict[str, Any]:
        context = state["context"]
        if (
            TaskType(state["task_type"]) == TaskType.CODE_DRAFT
            and not state.get("execution", {}).get("ok")
        ):
            context = await self._refresh_jngen_documents(
                context,
                state.get("execution", {}),
                project_id=state["project_id"],
                run_id=state["run_id"],
            )
        output = await self.model.run(
            TaskType(state["task_type"]),
            "review",
            context,
            state.get("candidate", {}),
            self._execution_feedback_for_model(state.get("execution", {})),
            state.get("issues", []),
        )
        attempts = state.get("attempts", 0)
        verified_fingerprint = state.get("execution", {}).get(
            "verified_candidate_fingerprint"
        )
        execution_ok = bool(state.get("execution", {}).get("ok")) and (
            verified_fingerprint == self._candidate_fingerprint(state.get("candidate", {}))
        )
        issues = list(output.issues)
        if not execution_ok:
            message = str(state.get("execution", {}).get("message") or "确定性检查未通过。")
            issues.append(message)
        candidate_changed = bool(output.result) and output.result != state.get("candidate", {})
        if candidate_changed:
            issues.append("自检修改了候选结果，修改后的版本必须重新执行确定性检查。")
        complete = (
            execution_ok
            and output.confirmation == Confirmation.PASS
            and not issues
            and not candidate_changed
        )
        exhausted = False
        candidate = state.get("candidate", {})
        if not complete:
            if attempts < state["max_iterations"]:
                attempts += 1
                if candidate_changed:
                    candidate = output.result
            else:
                exhausted = True
                if candidate_changed:
                    issues.append("已达到修复上限，最后一次未验证的修改未被保存。")
                issues.append(f"智能体在 {attempts} 轮自检修复后仍未完成任务。")
        return {
            "candidate": candidate,
            "issues": list(dict.fromkeys(issue for issue in issues if issue)),
            "attempts": attempts,
            "complete": complete,
            "exhausted": exhausted,
            "candidate_changed": candidate_changed and not exhausted,
            "context": context,
        }

    @staticmethod
    def _route_after_review(state: AgentLoopState) -> str:
        if state.get("complete"):
            return "wait_user" if state.get("requires_user") else "end"
        if state.get("exhausted"):
            return "end"
        if state.get("candidate_changed"):
            return "verify"
        return "generate"

    async def _with_selected_jngen_documents(
        self,
        context: dict[str, Any],
        *,
        project_id: str,
        run_id: str,
        purpose: str,
    ) -> dict[str, Any]:
        available_filenames = self.jngen_documents.available_filenames()
        document_catalog = self.jngen_documents.document_catalog(available_filenames)
        prepared = dict(context)
        prior_documentation = prepared.get("jngen_documentation", {})
        if not isinstance(prior_documentation, dict):
            prior_documentation = {}
        selected_documents = [
            dict(document)
            for document in prior_documentation.get("selected_documents", [])
            if isinstance(document, dict)
            and document.get("filename") in available_filenames
            and isinstance(document.get("content"), str)
        ]
        selected_filenames = {
            str(document["filename"]) for document in selected_documents
        }
        selection_rounds = [
            dict(round_record)
            for round_record in prior_documentation.get("selection_rounds", [])
            if isinstance(round_record, dict)
        ]

        # The initial retrieval deliberately has at least two turns: the first
        # turn discovers a small set of documents, and the next one can make a
        # decision using their actual contents.  Retry retrieval already has
        # those contents, so one additional turn is sufficient before a repair.
        minimum_rounds = 2 if not selected_documents else 1
        selection_complete = False
        selection_termination = "budget"
        for round_index in range(
            1, self.settings.agent_jngen_document_selection_rounds + 1
        ):
            selection_context = dict(prepared)
            selection_context["jngen_documentation"] = {
                "format_version": 1,
                "selection_method": "multi_round_model_structured_selection",
                "available_filenames": available_filenames,
                "selected_documents": selected_documents,
                "selection_rounds": selection_rounds,
            }
            selection_context["jngen_document_catalog"] = document_catalog
            selection_context["jngen_document_selection"] = {
                "round": round_index,
                "minimum_rounds": minimum_rounds,
                "maximum_rounds": self.settings.agent_jngen_document_selection_rounds,
                "maximum_documents": self.settings.agent_jngen_documents_per_round,
                "maximum_context_characters": self.settings.agent_jngen_document_context_chars,
            }
            selection = await self.model.select_jngen_documents(
                selection_context,
                available_filenames,
            )
            invalid_filenames = [
                choice.filename
                for choice in selection.selected_documents
                if choice.filename in selected_filenames
                or choice.filename not in available_filenames
            ]
            if invalid_filenames:
                raise AppError(
                    "JNGEN_DOCUMENT_SELECTION_INVALID",
                    "文档选择包含已读或不存在的文件。",
                    stage=5,
                    details={"filenames": invalid_filenames, "round": round_index},
                )
            if (
                len(selection.selected_documents)
                > self.settings.agent_jngen_documents_per_round
            ):
                raise AppError(
                    "JNGEN_DOCUMENT_SELECTION_INVALID",
                    "单轮选择的 jngen 文档数量超过上限。",
                    stage=5,
                    details={"round": round_index},
                )
            new_choices = list(selection.selected_documents)
            if new_choices:
                new_documents = self.jngen_documents.format_selected_documents(
                    available_filenames,
                    selection.model_copy(update={"selected_documents": new_choices}),
                )["selected_documents"]
                selected_characters = sum(
                    len(str(document.get("content") or ""))
                    for document in [*selected_documents, *new_documents]
                )
                if selected_characters > self.settings.agent_jngen_document_context_chars:
                    raise AppError(
                        "JNGEN_DOCUMENT_CONTEXT_TOO_LARGE",
                        "所选 jngen 文档正文超过 Agent4 上下文上限。",
                        stage=5,
                        details={
                            "maximum_context_characters": self.settings.agent_jngen_document_context_chars,
                            "selected_characters": selected_characters,
                        },
                    )
                selected_documents.extend(new_documents)
                selected_filenames.update(choice.filename for choice in new_choices)
            selection_rounds.append(
                {
                    "round": round_index,
                    "selected_filenames": [choice.filename for choice in new_choices],
                    "selection_complete": selection.selection_complete,
                }
            )
            prepared["jngen_documentation"] = {
                "format_version": 1,
                "selection_method": "multi_round_model_structured_selection",
                "available_filenames": available_filenames,
                "selected_documents": selected_documents,
                "selection_rounds": selection_rounds,
            }
            self.storage.append_agent4_document_selection(
                project_id,
                {
                    "run_id": run_id,
                    "event": "selection_round",
                    "purpose": purpose,
                    "round": round_index,
                    "selected_filenames": [choice.filename for choice in new_choices],
                    "selection_complete": selection.selection_complete,
                },
            )
            if round_index >= minimum_rounds and selection.selection_complete:
                selection_complete = True
                selection_termination = "model"
                break

        prepared.pop("jngen_document_selection", None)
        if not selected_documents:
            raise AppError(
                "JNGEN_DOCUMENT_SELECTION_EMPTY",
                "Agent4 在代码生成前未选择任何 jngen 文档。",
                stage=5,
            )
        prepared["jngen_documentation"]["selection_termination"] = (
            selection_termination
        )
        self.storage.append_agent4_document_selection(
            project_id,
            {
                "run_id": run_id,
                "event": "selection_finished",
                "purpose": purpose,
                "termination": selection_termination,
                "model_signalled_complete": selection_complete,
                "selected_filenames": sorted(selected_filenames),
            },
        )
        return prepared

    async def _refresh_jngen_documents(
        self,
        context: dict[str, Any],
        execution: dict[str, Any],
        *,
        project_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        refresh_context = dict(context)
        previous_feedback = list(refresh_context.get("recovery_feedback", []))[-2:]
        refresh_context["recovery_feedback"] = [
            *previous_feedback,
            {
                "source": "deterministic_verifier",
                "execution": self._bounded_execution_feedback(execution),
            },
        ]
        try:
            return await self._with_selected_jngen_documents(
                refresh_context,
                project_id=project_id,
                run_id=run_id,
                purpose="repair",
            )
        except AppError as exc:
            self.storage.append_agent4_document_selection(
                project_id,
                {
                    "run_id": run_id,
                    "event": "selection_failed",
                    "purpose": "repair",
                    "error": exc.payload(),
                },
            )
            raise

    @staticmethod
    def _bounded_execution_feedback(execution: dict[str, Any]) -> dict[str, Any]:
        feedback = {
            "ok": bool(execution.get("ok")),
            "message": str(execution.get("message") or ""),
            "checks": [],
        }
        for check in list(execution.get("checks", []))[-8:]:
            bounded = dict(check)
            result = bounded.get("result")
            if isinstance(result, dict):
                bounded["result"] = {
                    **result,
                    "stdout": str(result.get("stdout") or "")[-2000:],
                    "stderr": str(result.get("stderr") or "")[-6000:],
                }
            feedback["checks"].append(bounded)
        return feedback

    @staticmethod
    def _execution_feedback_for_model(execution: dict[str, Any]) -> dict[str, Any]:
        feedback: dict[str, Any] = {
            "ok": bool(execution.get("ok")),
            "message": str(execution.get("message") or ""),
            "verified_candidate_fingerprint": execution.get(
                "verified_candidate_fingerprint"
            ),
            "checks": [],
        }
        for check in execution.get("checks", []):
            if not isinstance(check, dict):
                feedback["checks"].append(
                    {
                        "operation": "deterministic_check",
                        "detail": str(check),
                    }
                )
                continue
            operation = check.get("operation")
            compact: dict[str, Any] = {
                key: check[key]
                for key in (
                    "operation",
                    "role",
                    "subtask_id",
                    "seed",
                    "content",
                    "selected_filenames",
                    "selection_rounds",
                )
                if key in check
            }
            result = check.get("result")
            if isinstance(result, dict):
                compact["result"] = {
                    "ok": bool(result.get("ok")),
                    "exit_code": result.get("exit_code"),
                    "stderr": str(result.get("stderr") or "")[-3000:]
                    if not result.get("ok")
                    else "",
                }
            if operation == "compile":
                compact["diagnostics"] = [
                    diagnostic
                    for diagnostic in check.get("diagnostics", [])
                    if diagnostic.get("severity") in {"error", "fatal error"}
                ]
            feedback["checks"].append(compact)
        return feedback

    @staticmethod
    def _candidate_fingerprint(candidate: dict[str, Any]) -> str:
        payload = json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _wait_user(state: AgentLoopState) -> dict[str, Any]:
        confirmed = interrupt(
            {
                "task_type": state["task_type"],
                "candidate": state.get("candidate", {}),
                "attempts": state.get("attempts", 0),
            }
        )
        return {"user_confirmed": bool(confirmed)}

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self.settings.agent_max_iterations * 4 + 12,
        }

    @staticmethod
    def _to_output(state: AgentLoopState) -> WorkflowOutput:
        complete = bool(state.get("complete"))
        issues = [] if complete else list(state.get("issues", []))
        return WorkflowOutput(
            confirmation=Confirmation.PASS if complete else Confirmation.REVISE,
            result=state.get("candidate", {}),
            issues=issues,
        )
