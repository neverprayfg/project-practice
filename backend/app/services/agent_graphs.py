from __future__ import annotations

import asyncio
import difflib
import hashlib
import inspect
import json
from contextlib import AbstractAsyncContextManager
from time import perf_counter
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from app.config import Settings
from app.errors import AppError
from app.models import (
    AGENT4_GRAPH_ID,
    AGENT4_VERIFIER_REVISION,
    AgentDecisionEvent,
    CodeDraft,
    Confirmation,
    CounterexampleLedger,
    Defect,
    GlobalInput,
    SemanticAudit,
    TargetedDefectCheck,
    WorkflowOutput,
)
from app.services.agent4_document_context import Agent4DocumentContext
from app.services.agent_validators import Agent1Validator, Agent2Validator, Agent3Validator
from app.services.candidate_verifier import Agent4CandidateVerifier
from app.services.code_candidate import candidate_revision
from app.services.code_format_contract import build_input_format_contract
from app.services.counterexample_ledger import CounterexampleLedgerService
from app.services.defects import (
    VALIDATION_LEVELS,
    defects_from_execution,
    ledger_digest,
    stable_defect_id,
    verification_summary,
)
from app.services.model_client import AgentModel
from app.storage import ProjectStorage


class Agent1State(TypedDict, total=False):
    project_id: str
    context: dict[str, Any]
    candidate: dict[str, Any]
    issues: list[str]
    complete: bool


class Agent2State(TypedDict, total=False):
    project_id: str
    context: dict[str, Any]
    candidate: dict[str, Any]
    issues: list[str]
    complete: bool
    requires_user: bool
    user_confirmed: bool


class Agent3State(TypedDict, total=False):
    project_id: str
    context: dict[str, Any]
    candidate: dict[str, Any]
    issues: list[str]
    revision_attempted: bool
    complete: bool
    requires_user: bool
    user_confirmed: bool


class Agent4State(TypedDict, total=False):
    run_id: str
    project_id: str
    context: dict[str, Any]
    candidate: dict[str, Any]
    accepted_candidate: dict[str, Any]
    candidate_revision: str
    accepted_revision: str
    execution: dict[str, Any]
    defects: list[dict[str, Any]]
    issues: list[str]
    ledger: dict[str, Any]
    target_defect: dict[str, Any]
    attempted_defect_ids: list[str]
    closed_defect_ids_before: list[str]
    baseline_summary: dict[str, Any]
    accepted_ledger: dict[str, Any]
    semantic_audit_done: bool
    validation_level: str
    patch_scope: list[str]
    patch_summary: dict[str, Any]
    complete: bool
    stopped: bool
    stop_reason: str
    requires_user: bool
    user_confirmed: bool


class Agent1Graph:
    def __init__(
        self, model: AgentModel, validator: Agent1Validator, saver: AsyncSqliteSaver
    ) -> None:
        self.model = model
        self.validator = validator
        builder = StateGraph(Agent1State)
        builder.add_node("normalize", self._normalize)
        builder.add_node("validate", self._validate)
        builder.add_edge(START, "normalize")
        builder.add_edge("normalize", "validate")
        builder.add_edge("validate", END)
        self.graph = builder.compile(checkpointer=saver)

    async def run(
        self, project_id: str, context: dict[str, Any], candidate: dict[str, Any]
    ) -> tuple[str, WorkflowOutput, bool]:
        thread_id = _thread_id(project_id, "agent1", context)
        state = await self.graph.ainvoke(
            {"project_id": project_id, "context": context, "candidate": candidate, "issues": []},
            _config(thread_id),
        )
        return thread_id, _basic_output(state), False

    async def _normalize(self, state: Agent1State) -> dict[str, Any]:
        result = await self.model.agent1_normalize(state["context"], state.get("candidate", {}))
        authoritative = GlobalInput.model_validate(
            state.get("candidate") or state["context"]["input"]
        )
        normalized_problem = authoritative.problem.model_copy(
            update={
                "input_description": result.input_description,
                "output_description": result.output_description,
                "samples": result.samples,
            }
        )
        candidate = authoritative.model_copy(update={"problem": normalized_problem})
        return {"candidate": candidate.model_dump(mode="json")}

    def _validate(self, state: Agent1State) -> dict[str, Any]:
        candidate, issues = self.validator.verify(state["candidate"], state["context"])
        return {"candidate": candidate, "issues": issues, "complete": not issues}


class Agent2Graph:
    def __init__(
        self, model: AgentModel, validator: Agent2Validator, saver: AsyncSqliteSaver
    ) -> None:
        self.model = model
        self.validator = validator
        builder = StateGraph(Agent2State)
        builder.add_node("draft_structure", self._draft)
        builder.add_node("validate_structure", self._validate)
        builder.add_node("wait_user", self._wait_user)
        builder.add_edge(START, "draft_structure")
        builder.add_edge("draft_structure", "validate_structure")
        builder.add_conditional_edges(
            "validate_structure",
            self._route_after_validation,
            {"wait_user": "wait_user", "end": END},
        )
        builder.add_edge("wait_user", END)
        self.graph = builder.compile(checkpointer=saver)

    async def run(
        self,
        project_id: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        *,
        requires_user: bool,
    ) -> tuple[str, WorkflowOutput, bool]:
        thread_id = _thread_id(project_id, "agent2", context)
        state = await self.graph.ainvoke(
            {
                "project_id": project_id,
                "context": context,
                "candidate": candidate,
                "issues": [],
                "requires_user": requires_user,
                "complete": False,
                "user_confirmed": False,
            },
            _config(thread_id),
        )
        return thread_id, _basic_output(state), bool(state.get("__interrupt__"))

    async def resume(self, thread_id: str) -> None:
        state = await self.graph.ainvoke(Command(resume=True), _config(thread_id))
        if not state.get("user_confirmed"):
            raise AppError("CONFIRMATION_FAILED", "Agent2 未记录用户确认。", status_code=409)

    async def _draft(self, state: Agent2State) -> dict[str, Any]:
        result = await self.model.agent2_structure(state["context"], state.get("candidate", {}))
        return {"candidate": result.model_dump(mode="json", exclude={"issues"})}

    def _validate(self, state: Agent2State) -> dict[str, Any]:
        candidate, issues = self.validator.verify(state["candidate"])
        return {"candidate": candidate, "issues": issues, "complete": not issues}

    @staticmethod
    def _route_after_validation(state: Agent2State) -> str:
        return "wait_user" if state.get("complete") and state.get("requires_user") else "end"

    @staticmethod
    def _wait_user(state: Agent2State) -> dict[str, Any]:
        approved = interrupt({"agent": "agent2", "candidate": state["candidate"]})
        return {"user_confirmed": bool(approved)}


class Agent3Graph:
    def __init__(
        self, model: AgentModel, validator: Agent3Validator, saver: AsyncSqliteSaver
    ) -> None:
        self.model = model
        self.validator = validator
        builder = StateGraph(Agent3State)
        builder.add_node("plan_subtasks", self._plan)
        builder.add_node("validate_contract", self._validate)
        builder.add_node("revise_contract", self._revise)
        builder.add_node("wait_user", self._wait_user)
        builder.add_edge(START, "plan_subtasks")
        builder.add_edge("plan_subtasks", "validate_contract")
        builder.add_conditional_edges(
            "validate_contract",
            self._route_after_validation,
            {"wait_user": "wait_user", "revise": "revise_contract", "end": END},
        )
        builder.add_edge("revise_contract", "validate_contract")
        builder.add_edge("wait_user", END)
        self.graph = builder.compile(checkpointer=saver)

    async def run(
        self,
        project_id: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        *,
        requires_user: bool,
    ) -> tuple[str, WorkflowOutput, bool]:
        thread_id = _thread_id(project_id, "agent3", context)
        state = await self.graph.ainvoke(
            {
                "project_id": project_id,
                "context": context,
                "candidate": candidate,
                "issues": [],
                "revision_attempted": False,
                "requires_user": requires_user,
                "complete": False,
                "user_confirmed": False,
            },
            _config(thread_id),
        )
        return thread_id, _basic_output(state), bool(state.get("__interrupt__"))

    async def resume(self, thread_id: str) -> None:
        state = await self.graph.ainvoke(Command(resume=True), _config(thread_id))
        if not state.get("user_confirmed"):
            raise AppError("CONFIRMATION_FAILED", "Agent3 未记录用户确认。", status_code=409)

    async def _plan(self, state: Agent3State) -> dict[str, Any]:
        existing = state.get("candidate") or {}
        if existing:
            return {"candidate": existing}
        result = await self.model.agent3_plan(state["context"], state.get("candidate", {}))
        first = result.subtasks[0].model_copy(update={"id": 1})
        initial = result.model_copy(update={"subtasks": [first]})
        return {"candidate": initial.model_dump(mode="json", exclude={"issues"})}

    def _validate(self, state: Agent3State) -> dict[str, Any]:
        candidate, issues = self.validator.verify(state["candidate"], state["context"])
        return {"candidate": candidate, "issues": issues, "complete": not issues}

    async def _revise(self, state: Agent3State) -> dict[str, Any]:
        revision_context = {
            **state["context"],
            "validation_issues": list(state.get("issues", [])),
        }
        result = await self.model.agent3_revise(revision_context, state["candidate"])
        return {
            "candidate": result.model_dump(mode="json", exclude={"issues"}),
            "revision_attempted": True,
        }

    @staticmethod
    def _route_after_validation(state: Agent3State) -> str:
        if state.get("complete"):
            return "wait_user" if state.get("requires_user") else "end"
        return "end" if state.get("revision_attempted") else "revise"

    @staticmethod
    def _wait_user(state: Agent3State) -> dict[str, Any]:
        approved = interrupt({"agent": "agent3", "candidate": state["candidate"]})
        return {"user_confirmed": bool(approved)}


class Agent4Graph:
    def __init__(
        self,
        settings: Settings,
        storage: ProjectStorage,
        model: AgentModel,
        verifier: Agent4CandidateVerifier,
        documents: Agent4DocumentContext,
        ledger_service: CounterexampleLedgerService,
        saver: AsyncSqliteSaver,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.model = model
        self.verifier = verifier
        self.documents = documents
        self.ledger_service = ledger_service
        builder = StateGraph(Agent4State)
        builder.add_node("prepare_documents", self._prepare_documents)
        builder.add_node("generate_candidate", self._generate_candidate)
        builder.add_node("verify_candidate", self._verify_candidate)
        builder.add_node("semantic_audit", self._semantic_audit)
        builder.add_node("select_defect", self._select_defect)
        builder.add_node("repair_defect", self._repair_defect)
        builder.add_node("recheck_history", self._recheck_history)
        builder.add_node("evaluate_progress", self._evaluate_progress)
        builder.add_node("approve", self._approve)
        builder.add_node("wait_user", self._wait_user)
        builder.add_edge(START, "prepare_documents")
        builder.add_conditional_edges(
            "prepare_documents",
            self._route_after_prepare,
            {"generate": "generate_candidate", "verify": "verify_candidate"},
        )
        builder.add_edge("generate_candidate", "verify_candidate")
        builder.add_conditional_edges(
            "verify_candidate",
            self._route_after_verify,
            {"select": "select_defect", "audit": "semantic_audit", "approve": "approve"},
        )
        builder.add_conditional_edges(
            "semantic_audit",
            self._route_after_audit,
            {"select": "select_defect", "approve": "approve"},
        )
        builder.add_conditional_edges(
            "select_defect",
            self._route_after_select,
            {"repair": "repair_defect", "end": END},
        )
        builder.add_conditional_edges(
            "repair_defect",
            self._route_after_repair,
            {"verify": "recheck_history", "end": END},
        )
        builder.add_edge("recheck_history", "evaluate_progress")
        builder.add_conditional_edges(
            "evaluate_progress",
            self._route_after_progress,
            {
                "select": "select_defect",
                "audit": "semantic_audit",
                "approve": "approve",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "approve",
            self._route_after_approve,
            {"wait_user": "wait_user", "end": END},
        )
        builder.add_edge("wait_user", END)
        self.graph = builder.compile(checkpointer=saver)

    async def run(
        self,
        project_id: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        *,
        requires_user: bool,
        thread_id: str | None = None,
    ) -> tuple[str, WorkflowOutput, bool]:
        if candidate:
            try:
                candidate = CodeDraft.model_validate(candidate).model_dump(mode="json")
            except ValidationError as exc:
                raise AppError(
                    "AGENT4_STATE_INCOMPATIBLE",
                    "已有阶段五候选不符合当前严格契约；旧候选不会被重新解释或兼容。",
                    stage=5,
                    status_code=409,
                ) from exc
        thread_id = thread_id or _thread_id(project_id, AGENT4_GRAPH_ID, context)
        if not _is_current_agent4_thread(thread_id):
            raise AppError(
                "AGENT4_CHECKPOINT_INCOMPATIBLE",
                "旧阶段五 checkpoint 不会被当前 Agent4 图恢复或兼容。",
                stage=5,
                status_code=409,
                details={"thread_id": thread_id, "required_graph": AGENT4_GRAPH_ID},
            )
        initial_revision = candidate_revision(candidate) if candidate else "uninitialized"
        started = perf_counter()
        status = "ok"
        try:
            ledger = self.ledger_service.load(project_id)
            state = await self.graph.ainvoke(
                {
                    "run_id": thread_id,
                    "project_id": project_id,
                    "context": context,
                    "candidate": candidate,
                    "accepted_candidate": candidate,
                    "candidate_revision": initial_revision,
                    "accepted_revision": initial_revision,
                    "execution": {},
                    "defects": [],
                    "issues": [],
                    "ledger": ledger.model_dump(mode="json"),
                    "attempted_defect_ids": [],
                    "semantic_audit_done": False,
                    "validation_level": "contract",
                    "patch_scope": [],
                    "patch_summary": {},
                    "complete": False,
                    "stopped": False,
                    "stop_reason": "",
                    "requires_user": requires_user,
                    "user_confirmed": False,
                },
                _config(thread_id),
            )
        except AppError as exc:
            status = "error"
            details = dict(exc.details) if isinstance(exc.details, dict) else {}
            details["thread_id"] = thread_id
            exc.details = details
            raise
        except Exception as exc:
            status = "error"
            exc.agent_thread_id = thread_id
            raise
        finally:
            self.storage.append_agent4_timing(
                project_id,
                {
                    "run_id": thread_id,
                    "segment": "workflow_total",
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "status": status,
                    "metadata": {},
                },
            )
        return thread_id, self._output(state), bool(state.get("__interrupt__"))

    async def retry(self, thread_id: str) -> tuple[str, WorkflowOutput, bool]:
        if not _is_current_agent4_thread(thread_id):
            raise AppError(
                "AGENT4_CHECKPOINT_INCOMPATIBLE",
                "旧阶段五 checkpoint 不会被当前 Agent4 图恢复或兼容。",
                stage=5,
                status_code=409,
                details={"thread_id": thread_id, "required_graph": AGENT4_GRAPH_ID},
            )
        started = perf_counter()
        status = "ok"
        try:
            state = await self.graph.ainvoke(None, _config(thread_id))
        except AppError as exc:
            status = "error"
            details = dict(exc.details) if isinstance(exc.details, dict) else {}
            details["thread_id"] = thread_id
            exc.details = details
            raise
        except Exception as exc:
            status = "error"
            exc.agent_thread_id = thread_id
            raise
        finally:
            self.storage.append_agent4_timing(
                thread_id.split(":", 1)[0],
                {
                    "run_id": thread_id,
                    "segment": "workflow_retry_total",
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "status": status,
                    "metadata": {"resumed_checkpoint": True},
                },
            )
        return thread_id, self._output(state), bool(state.get("__interrupt__"))

    @staticmethod
    def _output(state: dict[str, Any]) -> WorkflowOutput:
        defects = [Defect.model_validate(item) for item in state.get("defects", [])]
        issues = list(state.get("issues", []))
        if not state.get("complete"):
            issues.extend(item.message for item in defects if item.severity == "blocker")
            if state.get("stop_reason"):
                issues.append(state["stop_reason"])
        output = WorkflowOutput(
            confirmation=Confirmation.PASS if state.get("complete") else Confirmation.REVISE,
            result=state.get("accepted_candidate") or state.get("candidate"),
            issues=list(dict.fromkeys(item for item in issues if item)),
        )
        return output

    async def resume(self, thread_id: str) -> None:
        if not _is_current_agent4_thread(thread_id):
            raise AppError(
                "AGENT4_CHECKPOINT_INCOMPATIBLE",
                "旧阶段五 checkpoint 不会被当前 Agent4 图恢复或兼容。",
                stage=5,
                status_code=409,
                details={"thread_id": thread_id, "required_graph": AGENT4_GRAPH_ID},
            )
        state = await self.graph.ainvoke(Command(resume=True), _config(thread_id))
        if not state.get("user_confirmed"):
            raise AppError("CONFIRMATION_FAILED", "Agent4 未记录用户确认。", status_code=409)

    def _prepare_documents(self, state: Agent4State) -> dict[str, Any]:
        started = perf_counter()
        documentation = self.documents.load_all_documents()
        format_contract = build_input_format_contract(state["context"])
        context = dict(state["context"])
        context["agent4_library_context_bundle"] = documentation
        context["input_format_contract"] = format_contract.model_dump(mode="json")
        context["_agent4_timing"] = {"run_id": state["run_id"], "round": 1}
        self._timing(
            state,
            "retrieval",
            started,
            metadata={
                "purpose": "complete_document_load",
                "document_count": documentation["document_count"],
                "total_characters": documentation["total_characters"],
            },
        )
        return {"context": context}

    async def _generate_candidate(self, state: Agent4State) -> dict[str, Any]:
        started = perf_counter()
        results = await asyncio.gather(
            self.model.agent4_generate_generator(
                _context_for_code_role(state["context"], "generator"),
                state.get("candidate", {}),
            ),
            self.model.agent4_generate_validator(
                _context_for_code_role(state["context"], "validator"),
                state.get("candidate", {}),
            ),
            return_exceptions=True,
        )
        roles = ("generator", "validator")
        failures = [
            (role, result)
            for role, result in zip(roles, results, strict=True)
            if isinstance(result, Exception)
        ]
        if failures:
            for role, error in failures:
                self._decision(
                    state,
                    state["candidate_revision"],
                    model_call_type=f"{role}_generation",
                    decision="stopped",
                    reason=f"{role} 初始生成响应未通过协议校验，未形成联合候选。",
                    after={
                        "model_error": error.payload()
                        if isinstance(error, AppError)
                        else {"error_type": type(error).__name__}
                    },
                )
            self._timing(
                state,
                "model_generation",
                started,
                status="error",
                metadata={"parallel_calls": 2, "failed_roles": [item[0] for item in failures]},
            )
            raise failures[0][1]

        generated_generator, generated_validator = results
        expected_contract_id = state["context"]["input_format_contract"]["format_contract_id"]
        returned_contract_ids = {
            "generator": generated_generator.format_contract_id,
            "validator": generated_validator.format_contract_id,
        }
        mismatched_roles = [
            role
            for role, contract_id in returned_contract_ids.items()
            if contract_id != expected_contract_id
        ]
        if mismatched_roles:
            for role in mismatched_roles:
                self._decision(
                    state,
                    state["candidate_revision"],
                    model_call_type=f"{role}_generation",
                    decision="stopped",
                    reason=f"{role} 未遵守后端冻结的输入格式契约。",
                    after={
                        "expected_format_contract_id": expected_contract_id,
                        "returned_format_contract_id": returned_contract_ids[role],
                    },
                )
            self._timing(
                state,
                "model_generation",
                started,
                status="error",
                metadata={"parallel_calls": 2, "format_mismatch_roles": mismatched_roles},
            )
            raise AppError(
                "FORMAT_CONTRACT_MISMATCH",
                "generator 或 validator 未遵守冻结的输入格式契约。",
                stage=5,
                details={
                    "expected_format_contract_id": expected_contract_id,
                    "returned_format_contract_ids": returned_contract_ids,
                },
            )

        candidate = {
            "format_contract_id": expected_contract_id,
            "generator_code": generated_generator.generator_code,
            "validator_code": generated_validator.validator_code,
        }
        revision = candidate_revision(candidate)
        for role, modified_file in (
            ("generator", "generator.cpp"),
            ("validator", "validator.cpp"),
        ):
            self._decision(
                state,
                revision,
                model_call_type=f"{role}_generation",
                decision="observed",
                reason=f"并行生成 {modified_file} 并合入联合候选，尚待确定性验证。",
                modified_files=[modified_file],
                after={"format_contract_id": expected_contract_id},
            )
        self._timing(
            state,
            "model_generation",
            started,
            metadata={"parallel_calls": 2, "roles": list(roles)},
        )
        return {"candidate": candidate, "candidate_revision": revision}

    async def _verify_candidate(self, state: Agent4State) -> dict[str, Any]:
        ledger = CounterexampleLedger.model_validate(state["ledger"])
        candidate, execution = await self._cached_verify(
            state["project_id"], state["candidate"], state["context"], ledger
        )
        defects = defects_from_execution(execution)
        revision = candidate_revision(candidate)
        covered_ids = self._deterministically_covered_defect_ids(ledger, execution)
        ledger = self.ledger_service.observe(
            state["project_id"],
            ledger,
            defects,
            revision,
            closable_defect_ids=covered_ids,
        )
        level = str(execution.get("validation_level") or "static")
        if execution.get("ok"):
            level = "complete"
        self._decision(
            state,
            revision,
            after={
                **verification_summary(defects, level),
                "covered_historical_defect_ids": sorted(covered_ids),
                "history_replay_complete": execution.get("history_replay_complete", False),
            },
            decision="observed",
            reason=(
                "候选确定性验证通过。" if execution.get("ok") else "候选确定性验证发现阻断缺陷。"
            ),
        )
        return {
            "candidate": candidate,
            "candidate_revision": revision,
            "execution": execution,
            "defects": [item.model_dump(mode="json") for item in defects],
            "ledger": ledger.model_dump(mode="json"),
            "validation_level": level,
        }

    async def _semantic_audit(self, state: Agent4State) -> dict[str, Any]:
        started = perf_counter()
        existing_ledger = CounterexampleLedger.model_validate(state["ledger"])
        known_semantic = [
            item.defect.model_dump(mode="json")
            for item in existing_ledger.counterexamples
            if item.defect.validation_level == "semantic"
        ]
        audit_context = {
            **_context_for_review(state["context"], ("generator", "validator")),
            "known_semantic_defects": known_semantic,
        }
        try:
            audit = await self.model.agent4_audit(
                audit_context, state["candidate"], state["execution"]
            )
        except AppError as exc:
            self._decision(
                state,
                state["candidate_revision"],
                model_call_type="semantic_audit",
                decision="stopped",
                reason="只读语义审查的模型响应未通过协议校验，未产生缺陷。",
                after={"model_error": exc.payload()},
            )
            self._timing(state, "semantic_audit", started, status="error")
            raise
        except Exception:
            self._timing(state, "semantic_audit", started, status="error")
            raise
        self._timing(state, "semantic_audit", started)
        semantic_defects = self._normalize_audit(audit)
        deterministic = [
            Defect.model_validate(item)
            for item in state.get("defects", [])
            if item.get("validation_level") != "semantic"
        ]
        defects = [*deterministic, *semantic_defects]
        semantic_ids = {
            item.defect.defect_id
            for item in existing_ledger.counterexamples
            if item.defect.validation_level == "semantic"
        }
        ledger = self.ledger_service.observe(
            state["project_id"],
            existing_ledger,
            defects,
            state["candidate_revision"],
            closable_defect_ids=semantic_ids,
        )
        self._decision(
            state,
            state["candidate_revision"],
            model_call_type="semantic_audit",
            decision="observed",
            reason=f"一次开放只读审查发现 {len(semantic_defects)} 个缺陷。",
            after={
                "reported_defect_ids": [item.defect_id for item in semantic_defects],
                "rechecked_historical_defect_ids": sorted(semantic_ids),
            },
        )
        return {
            "defects": [item.model_dump(mode="json") for item in defects],
            "ledger": ledger.model_dump(mode="json"),
            "semantic_audit_done": True,
            "validation_level": "semantic" if not semantic_defects else state["validation_level"],
        }

    def _select_defect(self, state: Agent4State) -> dict[str, Any]:
        defects = [Defect.model_validate(item) for item in state.get("defects", [])]
        blockers = sorted(
            (item for item in defects if item.severity == "blocker"),
            key=lambda item: (VALIDATION_LEVELS[item.validation_level], item.defect_id),
        )
        if not blockers:
            reason = "没有可修复的阻断缺陷。"
            self._decision(
                state,
                state["candidate_revision"],
                decision="stopped",
                reason=reason,
            )
            return {"stopped": True, "stop_reason": reason}
        target = blockers[0]
        if target.identity.target_file not in {
            "generator.cpp",
            "validator.cpp",
        }:
            reason = (
                f"缺陷 {target.defect_id} 的目标 {target.identity.target_file} 不属于 Agent4 "
                "可修改范围，已停止且未调用修复模型。"
            )
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                decision="stopped",
                reason=reason,
            )
            return {"stopped": True, "stop_reason": reason}
        attempted = state.get("attempted_defect_ids", [])
        ledger = CounterexampleLedger.model_validate(state["ledger"])
        if target.defect_id in attempted:
            reason = f"缺陷 {target.defect_id} 修复一次后仍存在，已停止。"
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                decision="stopped",
                reason=reason,
            )
            return {"stopped": True, "stop_reason": reason}
        closed_before = [
            item.defect.defect_id for item in ledger.counterexamples if item.status == "closed"
        ]
        return {
            "target_defect": target.model_dump(mode="json"),
            "attempted_defect_ids": [*attempted, target.defect_id],
            "closed_defect_ids_before": closed_before,
            "baseline_summary": verification_summary(defects, state["validation_level"]),
            "accepted_ledger": state["ledger"],
            "accepted_candidate": state["candidate"],
            "accepted_revision": state["candidate_revision"],
        }

    async def _repair_defect(self, state: Agent4State) -> dict[str, Any]:
        started = perf_counter()
        target = Defect.model_validate(state["target_defect"])
        try:
            patch = await self.model.agent4_repair(
                _context_for_defect(state["context"], target),
                state["candidate"],
                target,
            )
        except AppError as exc:
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                model_call_type="repair",
                decision="stopped",
                reason="定向修复的模型响应未通过协议校验，未形成补丁。",
                after={"model_error": exc.payload()},
            )
            self._timing(state, "targeted_repair", started, status="error")
            raise
        except Exception:
            self._timing(state, "targeted_repair", started, status="error")
            raise
        self._timing(
            state,
            "targeted_repair",
            started,
            metadata={"target_defect_id": target.defect_id},
        )
        if patch.target_defect_id != target.defect_id:
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                model_call_type="repair",
                decision="stopped",
                reason="定向修复模型返回了不同的目标缺陷 ID。",
            )
            raise AppError(
                "REPAIR_TARGET_MISMATCH",
                "修复模型返回了不同的目标缺陷 ID。",
                stage=5,
            )
        expected_code_field = {
            "generator.cpp": "generator_code",
            "validator.cpp": "validator_code",
        }.get(target.identity.target_file)
        returned_code_fields = {
            field
            for field in ("generator_code", "validator_code")
            if getattr(patch, field) is not None
        }
        if expected_code_field and returned_code_fields - {expected_code_field}:
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                model_call_type="repair",
                decision="stopped",
                reason="定向修复返回了目标角色之外的源码。",
                after={"returned_code_fields": sorted(returned_code_fields)},
            )
            raise AppError(
                "REPAIR_SCOPE_MISMATCH",
                "定向修复只能返回目标缺陷所属角色的源码。",
                stage=5,
            )
        required_code_field = _required_source_patch_field(target)
        if required_code_field and getattr(patch, required_code_field) is None:
            reason = (
                f"缺陷 {target.defect_id} 属于源码行为缺陷，但补丁没有返回 "
                f"{required_code_field}，已停止。"
            )
            ledger = self.ledger_service.record_repair(
                state["project_id"],
                CounterexampleLedger.model_validate(state["ledger"]),
                target.defect_id,
                state["candidate_revision"],
                [],
                "still_open",
                reason,
            )
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                model_call_type="repair",
                decision="stopped",
                reason=reason,
                after={
                    "required_patch_field": required_code_field,
                    "model_rationale": patch.rationale,
                },
            )
            return {
                "stopped": True,
                "stop_reason": reason,
                "patch_scope": [],
                "ledger": ledger.model_dump(mode="json"),
            }
        proposed = dict(state["candidate"])
        patch_scope: list[str] = []
        for field, scope_name in (
            ("generator_code", "generator.cpp"),
            ("validator_code", "validator.cpp"),
        ):
            value = getattr(patch, field)
            if value is not None:
                proposed[field] = value
                patch_scope.append(scope_name)
        revision = candidate_revision(proposed)
        patch_summary = _patch_summary(state["candidate"], proposed, patch_scope)
        if revision == state["candidate_revision"]:
            reason = f"缺陷 {target.defect_id} 的补丁没有形成新候选，已停止。"
            ledger = self.ledger_service.record_repair(
                state["project_id"],
                CounterexampleLedger.model_validate(state["ledger"]),
                target.defect_id,
                revision,
                patch_scope,
                "still_open",
                reason,
            )
            self._decision(
                state,
                revision,
                target_defect_id=target.defect_id,
                model_call_type="repair",
                decision="stopped",
                reason=reason,
                modified_files=patch_scope,
                after={"patch": patch_summary, "model_rationale": patch.rationale},
            )
            return {
                "stopped": True,
                "stop_reason": reason,
                "patch_scope": patch_scope,
                "patch_summary": patch_summary,
                "ledger": ledger.model_dump(mode="json"),
            }
        self._decision(
            state,
            revision,
            target_defect_id=target.defect_id,
            model_call_type="repair",
            modified_files=patch_scope,
            after={"patch": patch_summary, "model_rationale": patch.rationale},
            decision="observed",
            reason="定向修复模型已提交补丁，等待全部历史反例复验。",
        )
        return {
            "candidate": proposed,
            "candidate_revision": revision,
            "patch_scope": patch_scope,
            "patch_summary": patch_summary,
        }

    async def _recheck_history(self, state: Agent4State) -> dict[str, Any]:
        started = perf_counter()
        ledger = CounterexampleLedger.model_validate(state["ledger"])
        candidate, execution = await self._cached_verify(
            state["project_id"], state["candidate"], state["context"], ledger
        )
        deterministic = defects_from_execution(execution)
        known_semantic = [
            item.defect
            for item in ledger.counterexamples
            if item.defect.validation_level == "semantic"
        ]
        try:
            checks = await asyncio.gather(
                *(
                    self.model.agent4_recheck(
                        {
                            **_context_for_defect(state["context"], defect),
                            "candidate_revision": state["candidate_revision"],
                        },
                        candidate,
                        defect,
                        execution,
                    )
                    for defect in known_semantic
                )
            )
        except AppError as exc:
            self._decision(
                state,
                state["candidate_revision"],
                model_call_type="targeted_recheck",
                decision="stopped",
                reason="历史反例定向复验的模型响应未通过协议校验，未改变缺陷状态。",
                after={"model_error": exc.payload()},
            )
            self._timing(state, "targeted_recheck", started, status="error")
            raise
        semantic_open: list[Defect] = []
        for defect, check in zip(known_semantic, checks, strict=True):
            if check.defect_id != defect.defect_id:
                self._decision(
                    state,
                    state["candidate_revision"],
                    target_defect_id=defect.defect_id,
                    model_call_type="targeted_recheck",
                    decision="stopped",
                    reason="定向复验返回了不同的缺陷 ID。",
                )
                raise AppError(
                    "RECHECK_TARGET_MISMATCH",
                    "定向复验返回了不同的缺陷 ID。",
                    stage=5,
                )
            evidence_grounded = _semantic_recheck_evidence_is_grounded(check, defect, candidate)
            accepted_still_present = bool(check.still_present and evidence_grounded)
            if accepted_still_present:
                assert check.evidence is not None
                semantic_open.append(
                    defect.model_copy(
                        update={
                            "message": check.message,
                            "evidence": {
                                **check.evidence.model_dump(mode="json"),
                                "origin": "candidate",
                                "grounded_in_candidate_revision": state["candidate_revision"],
                            },
                        }
                    )
                )
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=defect.defect_id,
                model_call_type="targeted_recheck",
                after={
                    "reported_still_present": check.still_present,
                    "accepted_still_present": accepted_still_present,
                    "evidence_grounded": evidence_grounded,
                    "message": check.message,
                    "evidence": check.evidence.model_dump(mode="json")
                    if check.evidence is not None
                    else None,
                },
                decision="observed",
                reason=(
                    "历史语义反例定向复验完成。"
                    if not check.still_present or evidence_grounded
                    else "复验声称缺陷仍存在，但证据不属于当前候选源码，未作为阻断缺陷接受。"
                ),
            )
        self._timing(
            state,
            "targeted_recheck",
            started,
            metadata={"known_semantic_defects": len(known_semantic)},
        )
        defects = [*deterministic, *semantic_open]
        revision = candidate_revision(candidate)
        covered_ids = self._deterministically_covered_defect_ids(ledger, execution)
        covered_ids.update(item.defect_id for item in known_semantic)
        ledger = self.ledger_service.observe(
            state["project_id"],
            ledger,
            defects,
            revision,
            closable_defect_ids=covered_ids,
        )
        level = (
            "complete"
            if execution.get("ok")
            else str(execution.get("validation_level") or "static")
        )
        return {
            "candidate": candidate,
            "candidate_revision": revision,
            "execution": execution,
            "defects": [item.model_dump(mode="json") for item in defects],
            "ledger": ledger.model_dump(mode="json"),
            "validation_level": level,
        }

    def _evaluate_progress(self, state: Agent4State) -> dict[str, Any]:
        target = Defect.model_validate(state["target_defect"])
        defects = [Defect.model_validate(item) for item in state.get("defects", [])]
        after = verification_summary(defects, state["validation_level"])
        before = state["baseline_summary"]
        after_ids = set(after["defect_ids"])
        newly_observed_blockers = sorted(set(after["blocker_ids"]) - set(before["blocker_ids"]))
        regression = bool(set(state.get("closed_defect_ids_before", [])) & after_ids)
        target_closed = target.defect_id not in after_ids
        progress = (
            after["open_blockers"] < before["open_blockers"]
            or target_closed
            or after["validation_rank"] > before["validation_rank"]
        ) and not regression
        ledger = CounterexampleLedger.model_validate(state["ledger"])
        if not target_closed:
            progress = False
            reason = f"缺陷 {target.defect_id} 修复一次后仍存在，候选已回滚并停止。"
        elif regression:
            progress = False
            reason = "补丁重新引入了已关闭缺陷，候选已回滚并停止。"
        elif not progress:
            reason = "阻断缺陷、目标缺陷和验证等级均未改善，候选已回滚并停止。"
        else:
            reason = "目标缺陷已关闭或验证等级前进，且未发生回归。"

        if not progress:
            ledger = self.ledger_service.rollback_repair(
                state["project_id"],
                CounterexampleLedger.model_validate(state["accepted_ledger"]),
                ledger,
                target.defect_id,
                state["candidate_revision"],
                state.get("patch_scope", []),
                "rolled_back",
                reason,
            )
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                model_call_type="repair",
                modified_files=state.get("patch_scope", []),
                before=before,
                after={
                    **after,
                    "newly_observed_blocker_ids": newly_observed_blockers,
                    "patch": state.get("patch_summary", {}),
                },
                progress=False,
                decision="rolled_back",
                reason=reason,
            )
            return {
                "candidate": state["accepted_candidate"],
                "candidate_revision": state["accepted_revision"],
                "ledger": ledger.model_dump(mode="json"),
                "stopped": True,
                "stop_reason": reason,
            }

        ledger = self.ledger_service.record_repair(
            state["project_id"],
            ledger,
            target.defect_id,
            state["candidate_revision"],
            state.get("patch_scope", []),
            "accepted",
            reason,
        )
        self._decision(
            state,
            state["candidate_revision"],
            target_defect_id=target.defect_id,
            model_call_type="repair",
            modified_files=state.get("patch_scope", []),
            before=before,
            after={
                **after,
                "newly_observed_blocker_ids": newly_observed_blockers,
                "patch": state.get("patch_summary", {}),
            },
            progress=True,
            decision="accepted",
            reason=reason,
        )
        return {
            "accepted_candidate": state["candidate"],
            "accepted_revision": state["candidate_revision"],
            "ledger": ledger.model_dump(mode="json"),
        }

    def _approve(self, state: Agent4State) -> dict[str, Any]:
        ledger = CounterexampleLedger.model_validate(state["ledger"])
        if any(
            item.status != "closed" and item.defect.severity == "blocker"
            for item in ledger.counterexamples
        ):
            reason = "仍有未关闭或回归的历史反例，不能确认通过。"
            self._decision(
                state,
                state["candidate_revision"],
                decision="stopped",
                reason=reason,
            )
            return {
                "stopped": True,
                "stop_reason": reason,
            }
        accepted_candidate = CodeDraft.model_validate(state["candidate"]).model_dump(
            mode="json", exclude={"issues"}
        )
        self._decision(
            state,
            state["candidate_revision"],
            decision="accepted",
            progress=True,
            reason="确定性验证、全部历史反例和一次只读语义审查均通过。",
        )
        return {
            "candidate": accepted_candidate,
            "accepted_candidate": accepted_candidate,
            "accepted_revision": state["candidate_revision"],
            "complete": True,
            "issues": [],
        }

    @staticmethod
    def _wait_user(state: Agent4State) -> dict[str, Any]:
        approved = interrupt(
            {
                "agent": "agent4",
                "candidate_revision": state["candidate_revision"],
                "candidate": state["candidate"],
            }
        )
        return {"user_confirmed": bool(approved)}

    async def _cached_verify(
        self,
        project_id: str,
        candidate: dict[str, Any],
        context: dict[str, Any],
        ledger: CounterexampleLedger,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        revision = candidate_revision(candidate)
        replay_fingerprints = [
            json.dumps(
                {
                    "counterexample_id": item.counterexample_id,
                    "reproduction": item.reproduction,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in ledger.counterexamples
            if item.reproduction.get("seed") is not None
        ]
        documentation = context.get("agent4_library_context_bundle", {})
        document_fingerprints = [
            f"{item.get('filename')}:{item.get('digest')}"
            for item in documentation.get("documents", [])
            if isinstance(item, dict)
        ]
        role_digests = _role_digests(candidate, project_id, self.storage)
        environment_fingerprint = ledger_digest(
            [
                *document_fingerprints,
                f"input_revision:{context.get('input_revision')}",
                f"subtasks_revision:{context.get('subtasks_revision')}",
                f"solution:{role_digests['solution']}",
                f"compile_image:{self.settings.runner_compile_image}",
                f"execute_image:{self.settings.runner_execute_image}",
                f"trial_seeds:{self.settings.agent_trial_seeds_per_subtask}",
                AGENT4_VERIFIER_REVISION,
            ]
        )
        key = f"{revision}:{environment_fingerprint}:{ledger_digest(replay_fingerprints)}"
        cache = self.storage.load_agent4_cache(project_id)
        cached = cache.setdefault("candidates", {}).get(key)
        if isinstance(cached, dict):
            timing = context.get("_agent4_timing", {})
            self.storage.append_agent4_timing(
                project_id,
                {
                    "run_id": timing.get("run_id", "cache"),
                    "segment": "verification_cache",
                    "duration_ms": 0.0,
                    "status": "hit",
                    "metadata": {"candidate_revision": revision},
                },
            )
            return cached["candidate"], cached["execution"]
        verified, execution = await self.verifier.verify(
            project_id,
            candidate,
            context,
            ledger.counterexamples,
        )
        cache["candidates"][key] = {
            "candidate": verified,
            "execution": execution,
            "replayed_counterexamples": execution.get("replayed_counterexample_ids", []),
            "gates": [
                check.get("operation")
                for check in execution.get("checks", [])
                if isinstance(check, dict) and check.get("ok") is not False
            ],
            "role_digests": role_digests,
            "environment_fingerprint": environment_fingerprint,
        }
        self.storage.save_agent4_cache(project_id, cache)
        return verified, execution

    @staticmethod
    def _deterministically_covered_defect_ids(
        ledger: CounterexampleLedger,
        execution: dict[str, Any],
    ) -> set[str]:
        """Return only historical defects this verification actually covered.

        Fail-fast verification proves gates strictly before the failed gate, plus
        individual counterexamples reported as replayed.  Same- or later-level
        defects remain open until their reproduction is really executed.
        """

        replayed_ids = set(execution.get("replayed_counterexample_ids", []))
        fully_evaluated_operations = set(execution.get("fully_evaluated_operations", []))
        current_level = (
            "complete"
            if execution.get("ok")
            else str(execution.get("validation_level") or "static")
        )
        current_rank = VALIDATION_LEVELS.get(current_level, 0)
        covered: set[str] = set()
        for item in ledger.counterexamples:
            if item.defect.validation_level == "semantic":
                continue
            if item.counterexample_id in replayed_ids or execution.get("ok"):
                covered.add(item.defect.defect_id)
                continue
            check = item.defect.evidence.get("check", {})
            if isinstance(check, dict) and check.get("operation") in fully_evaluated_operations:
                covered.add(item.defect.defect_id)
                continue
            if VALIDATION_LEVELS[item.defect.validation_level] < current_rank:
                covered.add(item.defect.defect_id)
        return covered

    def _timing(
        self,
        state: Agent4State,
        segment: str,
        started: float,
        *,
        status: str = "ok",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.storage.append_agent4_timing(
            state["project_id"],
            {
                "run_id": state["run_id"],
                "segment": segment,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "status": status,
                "metadata": metadata or {},
            },
        )

    @staticmethod
    def _normalize_audit(audit: SemanticAudit) -> list[Defect]:
        defects: list[Defect] = []
        for report in audit.defects:
            defects.append(
                Defect(
                    defect_id=stable_defect_id(report.identity),
                    identity=report.identity,
                    severity=report.severity,
                    validation_level="semantic",
                    message=report.message,
                    evidence=report.evidence,
                )
            )
        return list({item.defect_id: item for item in defects}.values())

    def _decision(
        self,
        state: Agent4State,
        revision: str,
        *,
        target_defect_id: str | None = None,
        model_call_type: str = "none",
        modified_files: list[str] | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        progress: bool = False,
        decision: str,
        reason: str,
    ) -> None:
        event = AgentDecisionEvent(
            run_id=state["run_id"],
            candidate_revision=revision,
            target_defect_id=target_defect_id,
            model_call_type=model_call_type,
            modified_files=modified_files or [],
            before=before or {},
            after=after or {},
            progress=progress,
            decision=decision,
            reason=reason,
        )
        self.storage.append_agent4_decision(state["project_id"], event.model_dump(mode="json"))

    @staticmethod
    def _route_after_prepare(state: Agent4State) -> str:
        candidate = state.get("candidate", {})
        required = {
            "format_contract_id",
            "generator_code",
            "validator_code",
        }
        if not required.issubset(candidate):
            return "generate"
        contract_id = (
            state.get("context", {}).get("input_format_contract", {}).get("format_contract_id")
        )
        return "verify" if candidate.get("format_contract_id") == contract_id else "generate"

    @staticmethod
    def _route_after_verify(state: Agent4State) -> str:
        if any(item.get("severity") == "blocker" for item in state.get("defects", [])):
            return "select"
        if not state.get("semantic_audit_done"):
            return "audit"
        return "approve"

    @staticmethod
    def _route_after_audit(state: Agent4State) -> str:
        return (
            "select"
            if any(item.get("severity") == "blocker" for item in state.get("defects", []))
            else "approve"
        )

    @staticmethod
    def _route_after_select(state: Agent4State) -> str:
        return "end" if state.get("stopped") else "repair"

    @staticmethod
    def _route_after_repair(state: Agent4State) -> str:
        return "end" if state.get("stopped") else "verify"

    @staticmethod
    def _route_after_progress(state: Agent4State) -> str:
        if state.get("stopped"):
            return "end"
        if any(item.get("severity") == "blocker" for item in state.get("defects", [])):
            return "select"
        if not state.get("semantic_audit_done"):
            return "audit"
        return "approve"

    @staticmethod
    def _route_after_approve(state: Agent4State) -> str:
        return "wait_user" if state.get("complete") and state.get("requires_user") else "end"


class AgentGraphCoordinator:
    """Owns shared infrastructure; each agent owns its state, graph and failure policy."""

    def __init__(
        self,
        settings: Settings,
        storage: ProjectStorage,
        model: AgentModel,
        verifier: Agent4CandidateVerifier,
        documents: Agent4DocumentContext,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.model = model
        self.verifier = verifier
        self.documents = documents
        self._saver_context: AbstractAsyncContextManager[AsyncSqliteSaver] | None = None
        self._saver: AsyncSqliteSaver | None = None
        self.agent1: Agent1Graph | None = None
        self.agent2: Agent2Graph | None = None
        self.agent3: Agent3Graph | None = None
        self.agent4: Agent4Graph | None = None

    async def start(self) -> None:
        if self._saver is not None:
            return
        path = self.storage.root / "langgraph-checkpoints.sqlite"
        self._saver_context = AsyncSqliteSaver.from_conn_string(str(path))
        self._saver = await self._saver_context.__aenter__()
        self.agent1 = Agent1Graph(self.model, Agent1Validator(), self._saver)
        self.agent2 = Agent2Graph(self.model, Agent2Validator(), self._saver)
        self.agent3 = Agent3Graph(self.model, Agent3Validator(), self._saver)
        self.agent4 = Agent4Graph(
            self.settings,
            self.storage,
            self.model,
            self.verifier,
            self.documents,
            CounterexampleLedgerService(self.storage),
            self._saver,
        )

    async def close(self) -> None:
        if self._saver_context is not None:
            await self._saver_context.__aexit__(None, None, None)
        self._saver_context = None
        self._saver = None
        self.agent1 = None
        self.agent2 = None
        self.agent3 = None
        self.agent4 = None
        await _close_model(self.model)

    async def replace_model(self, model: AgentModel) -> None:
        """Atomically point every independent graph at the shared model client."""
        previous = self.model
        self.model = model
        for agent in (self.agent1, self.agent2, self.agent3, self.agent4):
            if agent is not None:
                agent.model = model
        if previous is not model:
            await _close_model(previous)

    async def delete_project_checkpoints(self, project_id: str) -> None:
        """Remove every LangGraph thread that belongs to a deleted project."""
        await self.start()
        assert self._saver is not None
        prefix = f"{project_id}:"
        thread_ids: set[str] = set()
        async for checkpoint in self._saver.alist(None):
            thread_id = str(checkpoint.config.get("configurable", {}).get("thread_id", ""))
            if thread_id.startswith(prefix):
                thread_ids.add(thread_id)
        for thread_id in thread_ids:
            await self._saver.adelete_thread(thread_id)

    async def run_agent1(
        self, project_id: str, context: dict[str, Any], candidate: dict[str, Any]
    ) -> tuple[str, WorkflowOutput, bool]:
        await self.start()
        assert self.agent1 is not None
        return await self.agent1.run(project_id, context, candidate)

    async def run_agent2(
        self,
        project_id: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        *,
        requires_user: bool,
    ) -> tuple[str, WorkflowOutput, bool]:
        await self.start()
        assert self.agent2 is not None
        return await self.agent2.run(project_id, context, candidate, requires_user=requires_user)

    async def run_agent3(
        self,
        project_id: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        *,
        requires_user: bool,
    ) -> tuple[str, WorkflowOutput, bool]:
        await self.start()
        assert self.agent3 is not None
        return await self.agent3.run(project_id, context, candidate, requires_user=requires_user)

    async def run_agent4(
        self,
        project_id: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        *,
        requires_user: bool,
        thread_id: str | None = None,
    ) -> tuple[str, WorkflowOutput, bool]:
        await self.start()
        assert self.agent4 is not None
        return await self.agent4.run(
            project_id,
            context,
            candidate,
            requires_user=requires_user,
            thread_id=thread_id,
        )

    @staticmethod
    def new_agent4_thread_id(project_id: str, context: dict[str, Any]) -> str:
        return _thread_id(project_id, AGENT4_GRAPH_ID, context)

    async def retry_agent4(self, thread_id: str) -> tuple[str, WorkflowOutput, bool]:
        if not _is_current_agent4_thread(thread_id):
            raise AppError(
                "AGENT4_CHECKPOINT_INCOMPATIBLE",
                "旧阶段五 checkpoint 不会被当前 Agent4 图恢复或兼容。",
                stage=5,
                status_code=409,
                details={"thread_id": thread_id, "required_graph": AGENT4_GRAPH_ID},
            )
        await self.start()
        assert self.agent4 is not None
        return await self.agent4.retry(thread_id)

    async def resume_confirmation(self, thread_id: str) -> None:
        await self.start()
        agent_name = thread_id.split(":", 2)[1]
        if agent_name == "agent2":
            assert self.agent2 is not None
            await self.agent2.resume(thread_id)
            return
        if agent_name == "agent3":
            assert self.agent3 is not None
            await self.agent3.resume(thread_id)
            return
        if agent_name == AGENT4_GRAPH_ID:
            assert self.agent4 is not None
            await self.agent4.resume(thread_id)
            return
        raise AppError("CONFIRMATION_FAILED", "该 Agent 运行不支持用户确认。", status_code=409)


def _thread_id(project_id: str, agent: str, context: dict[str, Any]) -> str:
    revision = int(context.get("workflow_revision", 1))
    return f"{project_id}:{agent}:r{revision}:{uuid4().hex}"


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _is_current_agent4_thread(thread_id: str) -> bool:
    parts = thread_id.split(":", 2)
    return len(parts) == 3 and parts[1] == AGENT4_GRAPH_ID


async def _close_model(model: AgentModel) -> None:
    close = getattr(model, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _basic_output(state: dict[str, Any]) -> WorkflowOutput:
    return WorkflowOutput(
        confirmation=Confirmation.PASS if state.get("complete") else Confirmation.REVISE,
        result=state.get("candidate"),
        issues=state.get("issues", []),
    )


def _context_for_code_role(context: dict[str, Any], role: str) -> dict[str, Any]:
    prepared = dict(context)
    role_context = Agent4DocumentContext.for_role(
        context.get("agent4_library_context_bundle", {}), role
    )
    prepared.pop("agent4_library_context_bundle", None)
    prepared["library_context"] = role_context["library_context"]
    prepared["library_document_manifest"] = role_context["document_manifest"]
    return prepared


def _context_for_review(context: dict[str, Any], roles: tuple[str, ...]) -> dict[str, Any]:
    prepared = dict(context)
    documentation = prepared.pop("agent4_library_context_bundle", {})
    role_contexts = {role: Agent4DocumentContext.for_role(documentation, role) for role in roles}
    prepared["library_contexts"] = {
        role: role_context["library_context"] for role, role_context in role_contexts.items()
    }
    prepared["library_document_manifests"] = {
        role: role_context["document_manifest"] for role, role_context in role_contexts.items()
    }
    return prepared


def _context_for_defect(context: dict[str, Any], defect: Defect) -> dict[str, Any]:
    role = {
        "generator.cpp": "generator",
        "validator.cpp": "validator",
    }.get(defect.identity.target_file)
    prepared = _context_for_review(
        context,
        (role,) if role is not None else ("generator", "validator"),
    )
    prepared["required_patch_field"] = _required_source_patch_field(defect)
    return prepared


def _required_source_patch_field(defect: Defect) -> str | None:
    return {
        "generator.cpp": "generator_code",
        "validator.cpp": "validator_code",
    }.get(defect.identity.target_file)


def _semantic_recheck_evidence_is_grounded(
    check: TargetedDefectCheck,
    defect: Defect,
    candidate: dict[str, Any],
) -> bool | None:
    """Accept persistence claims only when they quote the current owned source."""

    if not check.still_present:
        return None
    evidence = check.evidence
    if evidence is None or evidence.target_file != defect.identity.target_file:
        return False
    source_field = {
        "generator.cpp": "generator_code",
        "validator.cpp": "validator_code",
    }.get(evidence.target_file)
    if source_field is None:
        return False
    source = str(candidate.get(source_field, "")).replace("\r\n", "\n").replace("\r", "\n")
    snippet = evidence.code_snippet.replace("\r\n", "\n").replace("\r", "\n").strip()
    return bool(snippet and snippet in source)


def _role_digests(
    candidate: dict[str, Any], project_id: str, storage: ProjectStorage
) -> dict[str, str]:
    solution_path = storage.project_dir(project_id) / "source" / "solution.cpp"
    solution = solution_path.read_text(encoding="utf-8") if solution_path.is_file() else ""
    return {
        "solution": hashlib.sha256(solution.encode("utf-8")).hexdigest(),
        "generator": hashlib.sha256(
            str(candidate.get("generator_code", "")).encode("utf-8")
        ).hexdigest(),
        "validator": hashlib.sha256(
            str(candidate.get("validator_code", "")).encode("utf-8")
        ).hexdigest(),
    }


def _patch_summary(
    before: dict[str, Any], after: dict[str, Any], modified_files: list[str]
) -> dict[str, Any]:
    summary: dict[str, Any] = {"modified_files": modified_files}
    for field, filename in (
        ("generator_code", "generator.cpp"),
        ("validator_code", "validator.cpp"),
    ):
        if filename not in modified_files:
            continue
        old = str(before.get(field, ""))
        new = str(after.get(field, ""))
        summary[filename] = {
            "before_digest": hashlib.sha256(old.encode("utf-8")).hexdigest()[:16],
            "after_digest": hashlib.sha256(new.encode("utf-8")).hexdigest()[:16],
            "before_lines": len(old.splitlines()),
            "after_lines": len(new.splitlines()),
            "changed_ranges": _changed_line_ranges(old, new),
        }
    return summary


def _changed_line_ranges(before: str, after: str) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines(), autojunk=False)
    for kind, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if kind == "equal":
            continue
        ranges.append(
            {
                "kind": kind,
                "before_start": old_start + 1,
                "before_end": old_end,
                "after_start": new_start + 1,
                "after_end": new_end,
            }
        )
        if len(ranges) == 200:
            break
    return ranges
