from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.errors import AppError
from app.models import (
    Confirmation,
    GlobalInput,
    Stage,
    StageStatus,
    SubtaskPlanDraft,
    TaskType,
)
from app.services.context_provider import AgentContextProvider
from app.services.langgraph_runner import LangGraphAgentRunner
from app.services.project_service import ProjectService
from app.services.runtime_parameters import profile_for_case, serialized_arguments
from app.services.sandbox import GenerationJob, Sandbox
from app.storage import ProjectStorage

STAGE_TASKS = {
    Stage.INPUT_STRUCTURE: TaskType.INPUT_STRUCTURE,
    Stage.SUBTASK_PLAN: TaskType.SUBTASK_PLAN,
    Stage.CODE_DRAFT: TaskType.CODE_DRAFT,
}


class PipelineService:
    def __init__(
        self,
        storage: ProjectStorage,
        projects: ProjectService,
        runner: LangGraphAgentRunner,
        contexts: AgentContextProvider,
        sandbox: Sandbox,
    ) -> None:
        self.storage = storage
        self.projects = projects
        self.runner = runner
        self.contexts = contexts
        self.sandbox = sandbox
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
            _thread_id, output, waiting_user = await self.runner.run(
                project_id,
                task_type,
                context,
                candidate,
                requires_user=False,
            )
            if waiting_user or output.confirmation != Confirmation.PASS:
                raise AppError(
                    "AGENT_INCOMPLETE",
                    "Agent1 未能完成 INPUT 规范化。",
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
        self, project_id: str, stage: int, requested_task: TaskType | None = None
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
            task_type = STAGE_TASKS[Stage(stage)]
            if stage == Stage.CODE_DRAFT and requested_task not in {
                None,
                TaskType.CODE_DRAFT,
            }:
                raise AppError(
                    "INVALID_TASK_TYPE",
                    "阶段 5 只能运行统一的代码生成与验证任务。",
                    stage=5,
                )
            state = record.stages[stage]
            if state.status == StageStatus.WAITING_USER or stage in record.stage_threads:
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
            self.storage.save_record(record)
            try:
                current = self.storage.load_draft(project_id, stage)
                initial_issues: list[str] = []
                if current is not None:
                    current = dict(current)
                    initial_issues = [
                        str(issue) for issue in current.pop("issues", []) if str(issue).strip()
                    ]
                context = self.contexts.build(project_id, task_type)
                thread_id, output, waiting_user = await self.runner.run(
                    project_id,
                    task_type,
                    context,
                    current,
                    requires_user=True,
                    initial_issues=initial_issues,
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
                if waiting_user:
                    self.projects.set_stage_thread(project_id, stage, thread_id)
                return {
                    "agent": {
                        "framework": "langgraph",
                        "thread_id": thread_id,
                        "confirmation": output.confirmation,
                        "waiting_user": waiting_user,
                        "issues": output.issues,
                    },
                    "draft": saved,
                }
            except AppError as exc:
                if exc.stage is None:
                    exc.stage = stage
                self._record_failure(project_id, stage, exc)
                raise
            except Exception as exc:
                error = AppError(
                    "STAGE_RUN_FAILED",
                    "阶段执行发生未预期错误，已安全结束本次运行。",
                    stage=stage,
                    status_code=500,
                    details={"exception_type": type(exc).__name__},
                )
                self._record_failure(project_id, stage, error)
                raise error from exc

    async def confirm_stage(self, project_id: str, stage: int) -> Any:
        async with self._locks[project_id]:
            record = self.projects.get(project_id)
            thread_id = record.stage_threads.get(stage)
            if not thread_id:
                raise AppError(
                    "CONFIRMATION_REQUIRED",
                    "当前阶段没有等待确认的 LangGraph 运行。",
                    stage=stage,
                    status_code=409,
                )
            await self.runner.resume_confirmation(thread_id)
            confirmed = self.projects.confirm(project_id, stage)
            self.projects.pop_stage_thread(project_id, stage)
            return confirmed

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
                raise AppError(
                    "INVALID_TEST_CASE", "预览测试点不存在。", stage=5
                ) from exc
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
        record = self.projects.get(project_id)
        state = record.stages[stage]
        state.status = StageStatus.FAILED
        state.ai_confirmed = False
        state.user_confirmed = False
        state.issues = [error.message]
        state.updated_at = datetime.now(UTC)
        record.stage_threads.pop(stage, None)
        record.last_error = error.payload()
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
