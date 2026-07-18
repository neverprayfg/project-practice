from __future__ import annotations

import inspect
from contextlib import AbstractAsyncContextManager
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from app.config import Settings
from app.errors import AppError
from app.models import (
    CodeDraft,
    Confirmation,
    GeneratorAnalysisDraft,
    GlobalInput,
    RecoveryFailureClass,
    RecoveryMutationGrant,
    RecoveryOutcome,
    RecoveryPlan,
    StageInstructionDecision,
    SubtaskPlanDraft,
    WorkflowOutput,
)
from app.services.agent4_document_context import Agent4DocumentContext
from app.services.agent_recovery import (
    AgentRecoveryCoordinator,
)
from app.services.agent_validators import Agent1Validator, Agent2Validator, Agent3Validator
from app.services.candidate_verifier import Agent4CandidateVerifier
from app.services.code_format_contract import build_input_format_contract
from app.services.generator_analysis import (
    generator_analysis_issues,
    normalize_generator_analysis,
)
from app.services.model_client import AgentModel
from app.services.recovery_policies import (
    Agent1RecoveryPolicy,
    Agent2RecoveryPolicy,
    Agent3RecoveryPolicy,
    Agent4RecoveryPolicy,
)
from app.services.runtime_parameters import runtime_parameter_issues
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
    complete: bool
    requires_user: bool
    user_confirmed: bool


class Agent1Graph:
    def __init__(
        self,
        model: AgentModel,
        validator: Agent1Validator,
        saver: AsyncSqliteSaver,
        recovery: AgentRecoveryCoordinator,
    ) -> None:
        self.model = model
        self.validator = validator
        self.recovery = recovery
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
        authoritative = GlobalInput.model_validate(
            state.get("candidate") or state["context"]["input"]
        )
        outcome = await self.recovery.run(
            state["project_id"],
            policy=Agent1RecoveryPolicy(
                self.recovery.storage,
                self.model,
                self.validator,
                authoritative,
            ),
            context=state["context"],
        )
        return {"candidate": _require_recovered_candidate(outcome)}

    def _validate(self, state: Agent1State) -> dict[str, Any]:
        candidate, issues = self.validator.verify(state["candidate"], state["context"])
        return {"candidate": candidate, "issues": issues, "complete": not issues}


class Agent2Graph:
    def __init__(
        self,
        model: AgentModel,
        validator: Agent2Validator,
        saver: AsyncSqliteSaver,
        recovery: AgentRecoveryCoordinator,
    ) -> None:
        self.model = model
        self.validator = validator
        self.recovery = recovery
        builder = StateGraph(Agent2State)
        builder.add_node("draft_test_data_plan", self._draft)
        builder.add_node("validate_test_data_plan", self._validate)
        builder.add_node("wait_user", self._wait_user)
        builder.add_edge(START, "draft_test_data_plan")
        builder.add_edge("draft_test_data_plan", "validate_test_data_plan")
        builder.add_conditional_edges(
            "validate_test_data_plan",
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
        outcome = await self.recovery.run(
            state["project_id"],
            policy=Agent2RecoveryPolicy(
                self.recovery.storage,
                self.model,
                self.validator,
                state["candidate"],
            ),
            context=state["context"],
        )
        return {"candidate": _require_recovered_candidate(outcome)}

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
        self,
        model: AgentModel,
        validator: Agent3Validator,
        saver: AsyncSqliteSaver,
        recovery: AgentRecoveryCoordinator,
    ) -> None:
        self.model = model
        self.validator = validator
        self.recovery = recovery
        builder = StateGraph(Agent3State)
        builder.add_node("plan_subtasks", self._plan)
        builder.add_node("validate_contract", self._validate)
        builder.add_node("wait_user", self._wait_user)
        builder.add_edge(START, "plan_subtasks")
        builder.add_edge("plan_subtasks", "validate_contract")
        builder.add_conditional_edges(
            "validate_contract",
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
        thread_id = _thread_id(project_id, "agent3", context)
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
            raise AppError("CONFIRMATION_FAILED", "Agent3 未记录用户确认。", status_code=409)

    async def _plan(self, state: Agent3State) -> dict[str, Any]:
        outcome = await self.recovery.run(
            state["project_id"],
            policy=Agent3RecoveryPolicy(
                self.recovery.storage,
                self.model,
                self.validator,
                state["candidate"],
            ),
            context=state["context"],
        )
        return {"candidate": _require_recovered_candidate(outcome)}

    def _validate(self, state: Agent3State) -> dict[str, Any]:
        candidate, issues = self.validator.verify(state["candidate"], state["context"])
        return {"candidate": candidate, "issues": issues, "complete": not issues}

    @staticmethod
    def _route_after_validation(state: Agent3State) -> str:
        return "wait_user" if state.get("complete") and state.get("requires_user") else "end"

    @staticmethod
    def _wait_user(state: Agent3State) -> dict[str, Any]:
        approved = interrupt({"agent": "agent3", "candidate": state["candidate"]})
        return {"user_confirmed": bool(approved)}


class Agent4Runner:
    """Own one mutable code template and repair only deterministic failures."""

    def __init__(
        self,
        storage: ProjectStorage,
        model: AgentModel,
        verifier: Agent4CandidateVerifier,
        documents: Agent4DocumentContext,
    ) -> None:
        self.storage = storage
        self.model = model
        self.verifier = verifier
        self.documents = documents

    async def run(
        self,
        project_id: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        *,
        requires_user: bool,
    ) -> tuple[str, WorkflowOutput, bool]:
        prepared = await self._prepare_context(project_id, context)
        prepared["project_id"] = project_id
        existing = self._validated_candidate(candidate)
        previous_failure = prepared.get("deterministic_failure")
        previous_summary = self.storage.load_record(project_id).recovery_summaries.get(5)
        policy = Agent4RecoveryPolicy(
            self.storage,
            self.model,
            self.verifier,
            existing,
            role_context=_context_for_code_role,
            repair_target=_repair_target,
            execution_issues=_execution_issues,
        )
        resume = (
            previous_summary
            if isinstance(previous_failure, dict) and previous_summary is not None
            else None
        )
        outcome = await AgentRecoveryCoordinator(self.storage).run(
            project_id,
            policy=policy,
            context=prepared,
            resume_summary=resume,
        )
        if outcome.status == "passed":
            return (
                outcome.summary.run_id,
                WorkflowOutput(
                    confirmation=Confirmation.PASS,
                    result=outcome.candidate,
                    issues=[],
                ),
                requires_user,
            )
        issues = [item.message for item in outcome.validation.errors] if outcome.validation else []
        if (
            outcome.recovery_plan is not None
            and not outcome.recovery_plan.allows_stage(5)
        ):
            raise AppError(
                "AGENT_RECOVERY_REROUTED",
                f"问题定位到阶段 {outcome.recovery_plan.root_stage}，当前 Agent4 无权修改。",
                stage=5,
                status_code=409,
                details={
                    "recovery_run_id": outcome.summary.run_id,
                    "last_errors": issues,
                    "recovery_plan": outcome.recovery_plan.model_dump(mode="json"),
                    "recovery_evidence": (
                        outcome.validation.diagnostics
                        if outcome.validation is not None
                        else {}
                    ),
                },
            )
        if outcome.status == "failed":
            return (
                outcome.summary.run_id,
                WorkflowOutput(
                    confirmation=Confirmation.REVISE,
                    result=outcome.candidate,
                    issues=issues,
                ),
                False,
            )
        raise _recovery_exhausted(5, outcome)

    async def _prepare_context(
        self,
        project_id: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            plan = SubtaskPlanDraft.model_validate({"subtasks": context.get("subtasks", [])})
            stage4_issues = runtime_parameter_issues(plan)
        except ValidationError as exc:
            stage4_issues = [item["msg"] for item in exc.errors(include_url=False)]
        if stage4_issues:
            recovery_plan = RecoveryPlan(
                observed_stage=5,
                root_stage=4,
                failure_class=RecoveryFailureClass.BUSINESS_CONTRACT,
                evidence=stage4_issues,
                context_requirements=[
                    "confirmed_stage3_plan",
                    "current_stage4_plan",
                    "generator_analysis_precheck",
                ],
                write_grants=[
                    RecoveryMutationGrant(
                        stage=4,
                        artifact="subtask_plan",
                        paths=[
                            "subtasks[*].generation_profiles",
                            "subtasks[*].runtime_parameters",
                        ],
                    )
                ],
                protected_fields=["problem.description", "solution.source", "released_code"],
                revalidate_from_stage=4,
                invalidate_downstream_from_stage=4,
                requires_user_authorization=False,
                confidence=1,
            )
            raise AppError(
                "AGENT_RECOVERY_REROUTED",
                "阶段四构造控制不完整，已定位回阶段四修复。",
                stage=5,
                status_code=409,
                details={
                    "last_errors": stage4_issues,
                    "recovery_plan": recovery_plan.model_dump(mode="json"),
                },
            )
        prepared = dict(context)
        prepared["agent4_library_context_bundle"] = self.documents.load_all_documents()
        prepared["input_format_contract"] = build_input_format_contract(context).model_dump(
            mode="json"
        )
        prepared["generator_analysis"] = await self._analyze_generator(
            project_id,
            context,
        )
        return prepared

    async def _analyze_generator(
        self,
        project_id: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        input_data = self.storage.load_input(project_id)
        analysis_context = {
            "input": {
                "problem": input_data.problem.model_dump(mode="json", exclude={"difficulty"}),
                "solution": {"source": input_data.solution.source},
            },
            "test_data_plan": context.get("test_data_plan", {}),
            "subtasks": context.get("subtasks", []),
        }
        candidate: dict[str, Any] = {}
        issues: list[str] = []
        last_error: AppError | None = None
        for attempt in range(3):
            try:
                if attempt == 0 or not candidate:
                    analysis = await self.model.agent4_analyze_generator(
                        analysis_context,
                        candidate,
                    )
                else:
                    analysis = await self.model.agent4_revise_generator_analysis(
                        analysis_context,
                        candidate,
                        issues,
                    )
            except AppError as exc:
                last_error = exc
                continue
            analysis = normalize_generator_analysis(analysis)
            candidate = analysis.model_dump(mode="json")
            issues = generator_analysis_issues(
                GeneratorAnalysisDraft.model_validate(candidate),
                context.get("subtasks", []),
            )
            if not issues:
                self.storage.save_generator_analysis(project_id, candidate)
                return candidate
        if last_error is not None and not candidate:
            raise last_error
        raise AppError(
            "GENERATOR_ANALYSIS_INVALID",
            "生成器分析智能体未能覆盖阶段四的全部构造控制。",
            stage=5,
            status_code=502,
            details={"issues": issues},
        )

    @staticmethod
    def _validated_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        if not candidate:
            return {}
        try:
            return CodeDraft.model_validate(candidate).model_dump(mode="json")
        except ValidationError as exc:
            raise AppError(
                "AGENT4_STATE_INCOMPATIBLE",
                "已有阶段五工作模板不符合当前代码契约。",
                stage=5,
                status_code=409,
            ) from exc

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
        self.recovery = AgentRecoveryCoordinator(storage)
        self._saver_context: AbstractAsyncContextManager[AsyncSqliteSaver] | None = None
        self._saver: AsyncSqliteSaver | None = None
        self.agent1: Agent1Graph | None = None
        self.agent2: Agent2Graph | None = None
        self.agent3: Agent3Graph | None = None
        self.agent4: Agent4Runner | None = None

    async def start(self) -> None:
        if self._saver is not None:
            return
        path = self.storage.root / "langgraph-checkpoints.sqlite"
        self._saver_context = AsyncSqliteSaver.from_conn_string(str(path))
        self._saver = await self._saver_context.__aenter__()
        self.agent1 = Agent1Graph(
            self.model, Agent1Validator(), self._saver, self.recovery
        )
        self.agent2 = Agent2Graph(
            self.model, Agent2Validator(), self._saver, self.recovery
        )
        self.agent3 = Agent3Graph(
            self.model, Agent3Validator(), self._saver, self.recovery
        )
        self.agent4 = Agent4Runner(
            self.storage,
            self.model,
            self.verifier,
            self.documents,
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

    async def repair_solution(
        self,
        context: dict[str, Any],
        source: str,
        execution: dict[str, Any],
    ) -> str:
        await self.start()
        return await self.model.repair_solution(context, source, execution)

    async def classify_stage_instruction(
        self,
        stage: int,
        context: dict[str, Any],
        candidate: dict[str, Any],
        instruction: str,
    ) -> StageInstructionDecision:
        await self.start()
        return await self.model.classify_stage_instruction(
            stage, context, candidate, instruction
        )

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
    ) -> tuple[str, WorkflowOutput, bool]:
        await self.start()
        assert self.agent4 is not None
        return await self.agent4.run(
            project_id,
            context,
            candidate,
            requires_user=requires_user,
        )

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
        raise AppError("CONFIRMATION_FAILED", "该 Agent 运行不支持用户确认。", status_code=409)


def _thread_id(project_id: str, agent: str, context: dict[str, Any]) -> str:
    revision = int(context.get("workflow_revision", 1))
    return f"{project_id}:{agent}:r{revision}:{uuid4().hex}"


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


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


def _require_recovered_candidate(outcome: RecoveryOutcome) -> dict[str, Any]:
    if outcome.status == "passed" and outcome.candidate is not None:
        return outcome.candidate
    raise _recovery_exhausted(outcome.summary.stage, outcome)


def _recovery_exhausted(stage: int, outcome: RecoveryOutcome) -> AppError:
    errors = [item.message for item in outcome.validation.errors] if outcome.validation else []
    return AppError(
        "AGENT_RECOVERY_EXHAUSTED",
        f"阶段 {stage} 的候选已用尽受限修复次数，仍未通过验证。",
        stage=stage,
        status_code=409,
        details={
            "recovery_run_id": outcome.summary.run_id,
            "generation_round": outcome.summary.generation_round,
            "repair_attempts": outcome.summary.repair_attempts,
            "failure_class": (
                outcome.validation.failure_class if outcome.validation is not None else None
            ),
            "last_errors": errors,
            "recovery_plan": (
                outcome.recovery_plan.model_dump(mode="json")
                if outcome.recovery_plan is not None
                else None
            ),
        },
    )


def _context_for_code_role(context: dict[str, Any], role: str) -> dict[str, Any]:
    prepared = dict(context)
    prepared.pop("deterministic_failure", None)
    if role == "validator":
        prepared.pop("test_data_plan", None)
        prepared.pop("generator_analysis", None)
    subtasks = context.get("subtasks", [])
    prepared["subtasks"] = []
    for subtask in subtasks:
        if not isinstance(subtask, dict):
            continue
        hidden = {"runtime_parameters"}
        if role == "validator":
            hidden.add("generation_profiles")
        prepared["subtasks"].append(
            {key: value for key, value in subtask.items() if key not in hidden}
        )
    if role == "generator":
        prepared["runtime_parameter_schema"] = _runtime_parameter_schema(subtasks)
        prepared["construction_controls"] = _construction_controls(subtasks)
    else:
        prepared.pop("runtime_parameter_schema", None)
        prepared.pop("construction_controls", None)
    role_context = Agent4DocumentContext.for_role(
        context.get("agent4_library_context_bundle", {}), role
    )
    prepared.pop("agent4_library_context_bundle", None)
    prepared["library_context"] = role_context["library_context"]
    prepared["library_document_manifest"] = role_context["document_manifest"]
    return prepared


def _runtime_parameter_schema(subtasks: Any) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    for subtask in subtasks if isinstance(subtasks, list) else []:
        if not isinstance(subtask, dict):
            continue
        parameters: dict[str, tuple[str, str]] = {"generation_profile": ("structure", "string")}
        for profile in subtask.get("runtime_parameters", []):
            if not isinstance(profile, dict):
                continue
            for parameter in profile.get("parameters", []):
                if not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str):
                    continue
                signature = (
                    str(parameter.get("category") or ""),
                    _runtime_value_type(parameter.get("value")),
                )
                previous = parameters.get(parameter["name"])
                if previous is not None and previous != signature:
                    raise AppError(
                        "RUNTIME_PARAMETER_SCHEMA_INVALID",
                        f"参数 {parameter['name']} 在不同测试点中的类别或类型不一致。",
                        stage=4,
                        status_code=409,
                    )
                parameters[parameter["name"]] = signature
        schemas.append(
            {
                "subtask_id": subtask.get("id"),
                "parameters": [
                    {"name": name, "category": category, "value_type": value_type}
                    for name, (category, value_type) in sorted(parameters.items())
                ],
            }
        )
    return schemas


def _construction_controls(subtasks: Any) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    for subtask in subtasks if isinstance(subtasks, list) else []:
        if not isinstance(subtask, dict):
            continue
        profiles = {
            str(profile.get("id")): profile
            for profile in subtask.get("generation_profiles", [])
            if isinstance(profile, dict) and profile.get("id") is not None
        }
        values: dict[str, dict[str, set[str]]] = {
            profile_id: {} for profile_id in profiles
        }
        for runtime in subtask.get("runtime_parameters", []):
            if not isinstance(runtime, dict):
                continue
            profile_id = str(runtime.get("generation_profile_id") or "")
            if profile_id not in values:
                continue
            for parameter in runtime.get("parameters", []):
                if (
                    not isinstance(parameter, dict)
                    or parameter.get("category") != "structure"
                    or not isinstance(parameter.get("name"), str)
                ):
                    continue
                values[profile_id].setdefault(parameter["name"], set()).add(
                    str(parameter.get("value"))
                )
        controls.append(
            {
                "subtask_id": subtask.get("id"),
                "profiles": [
                    {
                        "generation_profile_id": profile_id,
                        "category": profile.get("category"),
                        "goal": profile.get("goal"),
                        "controls": {
                            name: sorted(items)
                            for name, items in sorted(values[profile_id].items())
                        },
                    }
                    for profile_id, profile in profiles.items()
                ],
            }
        )
    return controls


def _runtime_value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _repair_target(execution: dict[str, Any]) -> str | None:
    targets: set[str] = set()
    for check in execution.get("checks", []):
        if not isinstance(check, dict):
            continue
        result = check.get("result")
        failed = check.get("ok") is False or (
            isinstance(result, dict) and result.get("ok") is False
        )
        if not failed:
            continue
        target = check.get("target_file")
        if target not in {"generator.cpp", "validator.cpp"}:
            role = check.get("role")
            operation = check.get("operation")
            if role in {"generator", "validator"}:
                target = f"{role}.cpp"
            else:
                target = {
                    "generator_library_usage": "generator.cpp",
                    "generator_runtime_parameters": "generator.cpp",
                    "construction_diversity": "generator.cpp",
                    "generate": "generator.cpp",
                    "validate": "generator.cpp",
                    "testlib_usage": "validator.cpp",
                }.get(str(operation))
        if target not in {"generator.cpp", "validator.cpp"}:
            return None
        targets.add(target)
    return next(iter(targets)) if len(targets) == 1 else None


def _execution_issues(execution: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    message = str(execution.get("message") or "").strip()
    if message:
        issues.append(message)
    for check in execution.get("checks", []):
        if not isinstance(check, dict):
            continue
        result = check.get("result")
        failed = check.get("ok") is False or (
            isinstance(result, dict) and result.get("ok") is False
        )
        if not failed:
            continue
        issues.extend(str(item).strip() for item in check.get("issues", []) if str(item).strip())
        issues.extend(
            str(item.get("message")).strip()
            for item in check.get("diagnostics", [])
            if isinstance(item, dict) and str(item.get("message") or "").strip()
        )
    return list(dict.fromkeys(issues)) or ["阶段五确定性验证失败。"]
