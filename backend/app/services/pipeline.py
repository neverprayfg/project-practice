from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.errors import AppError
from app.models import (
    Confirmation,
    GlobalInput,
    Stage,
    StageStatus,
    SubtaskPlanDraft,
    TaskType,
)
from app.services.agent_graphs import AgentGraphCoordinator
from app.services.agent_recovery import AgentRecoveryCoordinator
from app.services.context_provider import AgentContextProvider
from app.services.dataset import DatasetService
from app.services.project_service import ProjectService
from app.services.recovery_policies import SolutionRecoveryPolicy
from app.services.runtime_parameters import profile_for_case, serialized_arguments
from app.services.sandbox import GenerationJob, Sandbox
from app.storage import ProjectStorage

STAGE_TASKS = {
    Stage.TEST_DATA_PLAN: TaskType.TEST_DATA_PLAN,
    Stage.SUBTASK_PLAN: TaskType.SUBTASK_PLAN,
}

logger = logging.getLogger(__name__)
MAX_SOLUTION_REPAIRS = 5
MAX_DOWNSTREAM_RECOVERIES = 3


class PipelineService:
    def __init__(
        self,
        storage: ProjectStorage,
        projects: ProjectService,
        agents: AgentGraphCoordinator,
        contexts: AgentContextProvider,
        sandbox: Sandbox,
        datasets: DatasetService,
    ) -> None:
        self.storage = storage
        self.projects = projects
        self.agents = agents
        self.contexts = contexts
        self.sandbox = sandbox
        self.datasets = datasets
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def project_lock(self, project_id: str) -> asyncio.Lock:
        return self._locks[project_id]

    def has_active_tasks(self) -> bool:
        return any(lock.locked() for lock in self._locks.values())

    async def normalize_input(self, project_id: str) -> dict[str, Any]:
        async with self.project_lock(project_id):
            task_type = TaskType.INPUT_NORMALIZATION
            context = self.contexts.build(project_id, task_type)
            candidate = self.storage.load_input(project_id).model_dump(mode="json")
            _thread_id, output, waiting_user = await self.agents.run_agent1(
                project_id, context, candidate
            )
            if waiting_user or output.confirmation != Confirmation.PASS:
                raise AppError(
                    "AGENT_RECOVERY_EXHAUSTED",
                    "阶段 1 规范化修复达到上限。",
                    stage=1,
                    details={"issues": output.issues},
                )
            value = self.projects.save_normalized_input(
                project_id,
                GlobalInput.model_validate(output.result),
            )
            return value.model_dump(mode="json")

    async def compile_solution(self, project_id: str) -> dict[str, Any]:
        async with self._locks[project_id]:
            result = await self.sandbox.compile(project_id, "solution")
            if result.ok:
                record = self.projects.mark_solution_compiled(project_id, True, None)
            else:
                error = {
                    "code": "COMPILE_FAILED",
                    "message": "标程编译失败，请修改后重新提交。",
                    "stage": 2,
                    "details": result.model_dump(mode="json"),
                }
                record = self.projects.mark_solution_compiled(project_id, False, error)
            return {
                "project": record.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
                "input": self.storage.load_input(project_id).model_dump(mode="json"),
            }

    async def run_stage(
        self,
        project_id: str,
        stage: int,
        *,
        user_instruction: str | None = None,
    ) -> dict[str, Any]:
        async with self._locks[project_id]:
            record = self.projects.get(project_id)
            self._check_stage_prerequisites(record, stage)
            if stage != int(record.current_stage):
                raise AppError(
                    "STALE_STAGE",
                    "只能运行当前活动阶段；请先保存实质修改以回退流程。",
                    stage=stage,
                    status_code=409,
                )
            stage_value = Stage(stage)
            state = record.stages[stage]
            stored_current = self.storage.load_draft(project_id, stage)
            current = dict(stored_current) if stored_current is not None else {}
            current.pop("issues", None)
            context = (
                self.contexts.build_agent4(project_id)
                if stage_value == Stage.CODE_DRAFT
                else self.contexts.build(project_id, STAGE_TASKS[stage_value])
            )
            interaction = None
            if user_instruction is not None:
                decision = await self.agents.classify_stage_instruction(
                    stage, context, current, user_instruction
                )
                target = decision.target
                if decision.action == "answer":
                    return {
                        "interaction": decision.model_dump(mode="json"),
                        "draft": stored_current,
                    }
                target = (
                    "current_artifact"
                    if stage_value in {Stage.TEST_DATA_PLAN, Stage.SUBTASK_PLAN}
                    else target if target in {"generator", "validator", "both"} else "both"
                )
                interaction = decision.model_copy(update={"target": target}).model_dump(
                    mode="json"
                )
                context["user_instruction"] = user_instruction
                context["user_instruction_target"] = target
            if state.status == StageStatus.WAITING_USER or stage in record.stage_threads:
                if stage_value in {Stage.TEST_DATA_PLAN, Stage.SUBTASK_PLAN}:
                    record.stage_threads.pop(stage, None)
                elif user_instruction is None:
                    raise AppError(
                        "CONFIRMATION_PENDING",
                        "当前候选正在等待用户确认；请先确认或保存修改后再重新运行。",
                        stage=stage,
                        status_code=409,
                    )
            if state.status == StageStatus.CHECKING:
                # Once this request holds the project lock, another active run
                # cannot exist in this process.  The persisted state is stale.
                state.status = StageStatus.FAILED
                state.issues = ["检测到未结束的上一次 AI 检查，已自动回收并重新开始。"]
                state.updated_at = datetime.now(UTC)
                record.last_error = {
                    "code": "STAGE_INTERRUPTED",
                    "message": "上一次 AI 检查未正常结束。",
                    "stage": stage,
                }
                self.storage.save_record(record)
            state.status = StageStatus.CHECKING
            state.ai_confirmed = False
            state.user_confirmed = False
            state.updated_at = datetime.now(UTC)
            record.last_error = None
            record.failed_stage_threads.pop(stage, None)
            self.storage.save_record(record)
            try:
                if stage_value == Stage.TEST_DATA_PLAN:
                    thread_id, output, waiting_user = await self.agents.run_agent2(
                        project_id, context, current, requires_user=True
                    )
                elif stage_value == Stage.SUBTASK_PLAN:
                    thread_id, output, waiting_user = await self.agents.run_agent3(
                        project_id, context, current, requires_user=True
                    )
                else:
                    thread_id, output, waiting_user = await self.agents.run_agent4(
                        project_id,
                        context,
                        current,
                        requires_user=True,
                    )
                result = dict(output.result)
                result["issues"] = output.issues
                saved = self.projects.save_ai_result(
                    project_id,
                    stage,
                    result,
                    confirmed=output.confirmation == Confirmation.PASS and waiting_user,
                    issues=output.issues,
                )
                if waiting_user and stage_value != Stage.CODE_DRAFT:
                    self.projects.set_stage_thread(project_id, stage, thread_id)
                response = {
                    "agent": {
                        "framework": "langgraph",
                        "thread_id": thread_id,
                        "confirmation": output.confirmation,
                        "waiting_user": waiting_user,
                        "issues": output.issues,
                    },
                    "draft": saved,
                }
                if interaction is not None:
                    response["interaction"] = interaction
                return response
            except AppError as exc:
                if exc.stage is None:
                    exc.stage = stage
                self._record_failure(project_id, stage, exc)
                raise
            except Exception as exc:
                logger.exception(
                    "Unexpected stage failure: project_id=%s stage=%s",
                    project_id,
                    stage,
                )
                details: dict[str, Any] = {"exception_type": type(exc).__name__}
                if isinstance(exc, ValidationError):
                    details["validation_errors"] = [
                        {
                            "location": ".".join(str(part) for part in item["loc"]),
                            "type": item["type"],
                            "message": item["msg"],
                        }
                        for item in exc.errors(include_url=False, include_context=False)
                    ]
                error = AppError(
                    "STAGE_RUN_FAILED",
                    "阶段执行发生未预期错误，已安全结束本次运行。",
                    stage=stage,
                    status_code=500,
                    details=details,
                )
                thread_id = getattr(exc, "agent_thread_id", None)
                if thread_id:
                    error.details = {"thread_id": thread_id, **(error.details or {})}
                self._record_failure(project_id, stage, error)
                raise error from exc

    async def confirm_stage(self, project_id: str, stage: int) -> Any:
        async with self._locks[project_id]:
            record = self.projects.get(project_id)
            if stage == Stage.CODE_DRAFT:
                state = record.stages[stage]
                if state.status != StageStatus.WAITING_USER or not state.ai_confirmed:
                    raise AppError(
                        "CONFIRMATION_REQUIRED",
                        "阶段五尚未通过确定性验证。",
                        stage=stage,
                        status_code=409,
                    )
                return self.projects.confirm(project_id, stage)
            thread_id = record.stage_threads.get(stage)
            if not thread_id:
                raise AppError(
                    "CONFIRMATION_REQUIRED",
                    "当前阶段没有等待确认的 LangGraph 运行。",
                    stage=stage,
                    status_code=409,
                )
            await self.agents.resume_confirmation(thread_id)
            confirmed = self.projects.confirm(project_id, stage)
            self.projects.pop_stage_thread(project_id, stage)
            return confirmed

    async def auto_run(self, project_id: str, base_seed: int) -> dict[str, Any]:
        """Run the current project through export with bounded stage-level repair."""
        async with self._locks[project_id]:
            steps: list[dict[str, Any]] = []
            attempts: dict[int, int] = defaultdict(int)
            downstream_recoveries = 0
            routed_contexts: dict[int, dict[str, Any]] = {}
            _record, resumed_stage = self.projects.prepare_auto_run_resume(project_id)
            if resumed_stage is not None:
                steps.append(
                    {
                        "stage": resumed_stage,
                        "attempt": 0,
                        "status": "resumed",
                        "message": "已回收上次中断的阶段，从此继续一键生成。",
                    }
                )

            while True:
                record = self.projects.get(project_id)
                stage = int(record.current_stage)

                if not record.solution_compiled:
                    input_data = self.storage.load_input(project_id)
                    outcome = await AgentRecoveryCoordinator(self.storage).run(
                        project_id,
                        policy=SolutionRecoveryPolicy(
                            self.storage,
                            self.projects,
                            self.sandbox,
                            input_data.solution.source,
                            self.agents.repair_solution,
                        ),
                        context={
                            "project_id": project_id,
                            "problem": input_data.problem.model_dump(mode="json"),
                        },
                    )
                    attempts[2] = outcome.summary.repair_attempts + 1
                    if outcome.status != "passed":
                        error = {
                            "code": "COMPILE_FAILED",
                            "message": "标程编译修复达到上限。",
                            "stage": 2,
                            "details": (
                                outcome.validation.diagnostics
                                if outcome.validation is not None
                                else {}
                            ),
                        }
                        raise self._auto_repair_exhausted(2, steps, error)
                    if outcome.summary.repair_attempts:
                        steps.append(
                            {
                                "stage": 2,
                                "attempt": outcome.summary.repair_attempts,
                                "status": "repairing",
                                "message": "标程编译失败，已根据真实诊断完成修复。",
                            }
                        )
                    steps.append(
                        {
                            "stage": 2,
                            "attempt": attempts[2],
                            "status": "passed",
                            "message": "标程编译通过。",
                        }
                    )
                    continue

                if stage == Stage.SOLUTION_COMPILE:
                    # A process can stop between persisting a successful compile
                    # and advancing current_stage. Normalize that recoverable state.
                    self.projects.mark_solution_compiled(project_id, True, None)
                    steps.append(
                        {
                            "stage": 2,
                            "attempt": 0,
                            "status": "resumed",
                            "message": "标程已编译通过，从阶段 3 继续执行。",
                        }
                    )
                    continue

                if stage in (3, 4, 5):
                    attempts[stage] += 1
                    try:
                        output = await self._run_auto_interactive_stage(
                            project_id,
                            stage,
                            extra_context=routed_contexts.pop(stage, None),
                        )
                    except AppError as exc:
                        steps.append(
                            {
                                "stage": stage,
                                "attempt": attempts[stage],
                                "status": "repairing",
                                "message": exc.message,
                            }
                        )
                        plan = (
                            exc.details.get("recovery_plan")
                            if isinstance(exc.details, dict)
                            else None
                        )
                        root_stage = plan.get("root_stage") if isinstance(plan, dict) else None
                        authorized = bool(
                            isinstance(plan, dict)
                            and not plan.get("requires_user_authorization")
                            and root_stage in {3, 4, 5}
                            and int(root_stage) < stage
                        )
                        if authorized:
                            root_stage = int(root_stage)
                            routed_contexts[root_stage] = {
                                "routed_recovery": plan,
                                "routed_from_stage": stage,
                            }
                            self.projects.mark_interactive_stage_failed(
                                project_id,
                                root_stage,
                                {
                                    "code": "AGENT_RECOVERY_REROUTED",
                                    "message": (
                                        f"阶段 {stage} 的失败已定位到阶段 "
                                        f"{root_stage}，将按限定授权自动修复。"
                                    ),
                                    "stage": root_stage,
                                    "details": {"recovery_plan": plan},
                                },
                            )
                            continue
                        raise exc
                    if output.confirmation != Confirmation.PASS:
                        steps.append(
                            {
                                "stage": stage,
                                "attempt": attempts[stage],
                                "status": "repairing",
                                "message": "；".join(output.issues) or "阶段检查未通过。",
                            }
                        )
                        error = AppError(
                            "AGENT_RECOVERY_STOPPED",
                            "阶段检查遇到不可由当前智能体修复的问题。",
                            stage=stage,
                            status_code=409,
                            details={"issues": output.issues},
                        )
                        self._record_failure(project_id, stage, error)
                        raise error
                    self.projects.confirm(project_id, stage)
                    steps.append(
                        {
                            "stage": stage,
                            "attempt": attempts[stage],
                            "status": "passed",
                            "message": "AI 检查通过，已自动确认。",
                        }
                    )
                    continue

                if stage == Stage.BUILD:
                    try:
                        generated = await self.datasets.generate_inputs(
                            project_id,
                            base_seed,
                            None,
                        )
                    except AppError as exc:
                        downstream_recoveries += 1
                        steps.append(
                            {
                                "stage": 6,
                                "attempt": downstream_recoveries,
                                "status": "repairing",
                                "message": exc.message,
                            }
                        )
                        if (
                            self.projects.get(project_id).current_stage == Stage.CODE_DRAFT
                            and downstream_recoveries <= MAX_DOWNSTREAM_RECOVERIES
                        ):
                            attempts[5] = 0
                            continue
                        raise self._auto_repair_exhausted(6, steps, exc.payload()) from exc
                    steps.append(
                        {
                            "stage": 6,
                            "attempt": 1,
                            "status": "passed",
                            "message": f"已生成 {generated['generated_tests']} 个输入文件。",
                        }
                    )
                    continue

                if stage == Stage.VALIDATE_AND_SOLVE:
                    try:
                        validated = await self.datasets.validate_and_solve(project_id, None)
                    except AppError as exc:
                        downstream_recoveries += 1
                        steps.append(
                            {
                                "stage": 7,
                                "attempt": downstream_recoveries,
                                "status": "repairing",
                                "message": exc.message,
                            }
                        )
                        if (
                            self.projects.get(project_id).current_stage == Stage.CODE_DRAFT
                            and downstream_recoveries <= MAX_DOWNSTREAM_RECOVERIES
                        ):
                            attempts[5] = 0
                            continue
                        raise self._auto_repair_exhausted(7, steps, exc.payload()) from exc
                    steps.append(
                        {
                            "stage": 7,
                            "attempt": 1,
                            "status": "passed",
                            "message": f"已验证 {validated['validated_tests']} 个测试点。",
                        }
                    )
                    continue

                if stage == Stage.EXPORT:
                    archive = self.datasets.export(project_id)
                    steps.append(
                        {
                            "stage": 8,
                            "attempt": 1,
                            "status": "passed",
                            "message": "数据包已生成。",
                        }
                    )
                    return {
                        "ok": True,
                        "project": self.projects.get(project_id).model_dump(mode="json"),
                        "steps": steps,
                        "archive": archive.name,
                        "download_url": f"/api/projects/{project_id}/export",
                    }

                raise AppError(
                    "INVALID_STAGE",
                    "当前项目状态无法进入一键生成流程。",
                    stage=stage,
                    status_code=409,
                    details={"steps": steps},
                )

    async def _run_auto_interactive_stage(
        self,
        project_id: str,
        stage: int,
        *,
        extra_context: dict[str, Any] | None = None,
    ) -> Any:
        record = self.projects.get(project_id)
        self._check_stage_prerequisites(record, stage)
        if stage != int(record.current_stage):
            raise AppError("STALE_STAGE", "自动流程检测到阶段状态已变化。", stage=stage)
        stage_value = Stage(stage)
        state = record.stages[stage]
        stored_current = self.storage.load_draft(project_id, stage)
        current = dict(stored_current) if stored_current is not None else {}
        current.pop("issues", None)
        context = (
            self.contexts.build_agent4(project_id)
            if stage_value == Stage.CODE_DRAFT
            else self.contexts.build(project_id, STAGE_TASKS[stage_value])
        )
        if extra_context:
            context.update(extra_context)
        state.status = StageStatus.CHECKING
        state.ai_confirmed = False
        state.user_confirmed = False
        state.updated_at = datetime.now(UTC)
        record.last_error = None
        record.stage_threads.pop(stage, None)
        record.failed_stage_threads.pop(stage, None)
        self.storage.save_record(record)
        try:
            if stage_value == Stage.TEST_DATA_PLAN:
                _thread_id, output, _waiting_user = await self.agents.run_agent2(
                    project_id, context, current, requires_user=False
                )
            elif stage_value == Stage.SUBTASK_PLAN:
                _thread_id, output, _waiting_user = await self.agents.run_agent3(
                    project_id, context, current, requires_user=False
                )
            else:
                _thread_id, output, _waiting_user = await self.agents.run_agent4(
                    project_id,
                    context,
                    current,
                    requires_user=False,
                )
            result = dict(output.result)
            result["issues"] = output.issues
            self.projects.save_ai_result(
                project_id,
                stage,
                result,
                confirmed=output.confirmation == Confirmation.PASS,
                issues=output.issues,
            )
            return output
        except AppError as exc:
            if exc.stage is None:
                exc.stage = stage
            self._record_failure(project_id, stage, exc)
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected auto-run stage failure: project_id=%s stage=%s",
                project_id,
                stage,
            )
            error = AppError(
                "STAGE_RUN_FAILED",
                "自动流程中的阶段执行发生未预期错误。",
                stage=stage,
                status_code=500,
                details={"exception_type": type(exc).__name__},
            )
            self._record_failure(project_id, stage, error)
            raise error from exc

    @staticmethod
    def _auto_repair_exhausted(
        stage: int,
        steps: list[dict[str, Any]],
        last_error: dict[str, Any],
    ) -> AppError:
        return AppError(
            "AUTO_REPAIR_EXHAUSTED",
            f"阶段 {stage} 自动修复达到上限。",
            stage=stage,
            status_code=409,
            details={
                "recovery_stage": stage,
                "max_repairs": MAX_SOLUTION_REPAIRS,
                "last_error": last_error,
                "steps": steps,
            },
        )

    async def preview(
        self, project_id: str, subtask_id: int, case_id: int, seed: int
    ) -> dict[str, Any]:
        async with self._locks[project_id]:
            record = self.projects.get(project_id)
            if record.current_stage < Stage.CODE_DRAFT:
                raise AppError("PREREQUISITE_REQUIRED", "尚未进入阶段 5。", stage=5)
            plan = self.storage.load_draft(project_id, 4)
            if plan is None or not any(item.get("id") == subtask_id for item in plan["subtasks"]):
                raise AppError("INVALID_SUBTASK", "预览子任务不存在。", stage=5)
            parsed_plan = SubtaskPlanDraft.model_validate(plan)
            subtask = next(item for item in parsed_plan.subtasks if item.id == subtask_id)
            try:
                profile = profile_for_case(subtask, case_id)
            except ValueError as exc:
                raise AppError("INVALID_TEST_CASE", "预览测试点不存在。", stage=5) from exc
            for role in ("generator", "validator", "solution"):
                compiled = await self.sandbox.compile(project_id, role)
                if not compiled.ok:
                    raise AppError(
                        "COMPILE_FAILED",
                        f"{role} 编译失败。",
                        stage=5,
                        details=compiled.model_dump(mode="json"),
                    )
            preview_id = uuid4().hex[:12]
            input_relative = f"preview/{preview_id}.in"
            output_relative = f"preview/{preview_id}.out"
            generated = (
                await self.sandbox.generate_batch(
                    project_id,
                    [
                        GenerationJob(
                            subtask_id,
                            seed,
                            input_relative,
                            case_id=case_id,
                            runtime_arguments=serialized_arguments(profile),
                        )
                    ],
                )
            )[0].result
            if not generated.ok:
                raise AppError("GENERATION_FAILED", "预览生成失败。", stage=5)
            validated = await self.sandbox.validate(project_id, input_relative)
            solved = await self.sandbox.solve(project_id, input_relative, output_relative)
            path = self.storage.project_dir(project_id) / input_relative
            return {
                "preview_id": preview_id,
                "subtask_id": subtask_id,
                "case_id": case_id,
                "seed": seed,
                "runtime_arguments": serialized_arguments(profile),
                "content": path.read_text(encoding="utf-8"),
                "validator": validated.model_dump(mode="json"),
                "solution": solved.model_dump(mode="json"),
            }

    @staticmethod
    def _check_stage_prerequisites(record: Any, stage: int) -> None:
        if stage not in (3, 4, 5):
            raise AppError("INVALID_STAGE", "只能运行阶段 3、4 或 5。", stage=stage)
        if not record.solution_compiled:
            raise AppError("PREREQUISITE_REQUIRED", "标程必须先编译通过。", stage=stage)
        for previous in range(3, stage):
            if record.stages[previous].status != StageStatus.PASSED:
                raise AppError(
                    "PREREQUISITE_REQUIRED",
                    f"阶段 {previous} 尚未由智能体和用户共同确认。",
                    stage=stage,
                    status_code=409,
                )

    def _record_failure(self, project_id: str, stage: int, error: AppError) -> None:
        self.projects.mark_interactive_stage_failed(
            project_id,
            stage,
            error.payload(),
        )
