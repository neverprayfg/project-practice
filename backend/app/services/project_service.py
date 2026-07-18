from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.errors import AppError
from app.models import (
    AGENT4_ARCHITECTURE_ID,
    CodeDraft,
    GlobalInput,
    ProblemInput,
    ProjectCreate,
    ProjectRecord,
    SolutionInput,
    SolutionUpdate,
    Stage,
    StageStatus,
    SubtaskPlanDraft,
    TestDataPlanDraft,
)
from app.storage import ProjectStorage


class ProjectService:
    def __init__(
        self,
        storage: ProjectStorage,
    ) -> None:
        self.storage = storage

    def create(self, payload: ProjectCreate) -> ProjectRecord:
        record = ProjectRecord(
            project_id=uuid4().hex,
            problem_description=payload.problem_description,
            difficulty=payload.difficulty,
        )
        input_data = GlobalInput(
            problem=ProblemInput(
                description=payload.problem_description,
                difficulty=payload.difficulty,
            ),
            solution=SolutionInput(source=payload.solution_code),
        )
        self.storage.initialize(record, input_data)
        self.storage.write_json(
            self.storage.project_dir(record.project_id) / "state" / "agent4-architecture.json",
            {"architecture": AGENT4_ARCHITECTURE_ID},
        )
        return record

    def get(self, project_id: str) -> ProjectRecord:
        return self.storage.load_record(project_id)

    def list(self) -> list[ProjectRecord]:
        records = [self.get(project_id) for project_id in self.storage.project_ids()]
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    def list_history(self) -> list[dict[str, Any]]:
        return [
            {
                "project_id": record.project_id,
                "title": record.project_name
                or self._history_title(record.problem_description, record.project_id),
                "project_name": record.project_name,
                "problem_description": record.problem_description,
                "difficulty": record.difficulty,
                "current_stage": int(record.current_stage),
                "solution_compiled": record.solution_compiled,
                "generation_complete": record.generation_complete,
                "build_complete": record.build_complete,
                "export_ready": record.export_ready,
                "last_error": record.last_error,
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
            }
            for record in self.list()
        ]

    def delete(self, project_id: str) -> ProjectRecord:
        record = self.get(project_id)
        self.storage.delete_project(project_id)
        return record

    @staticmethod
    def _history_title(problem_description: str, project_id: str) -> str:
        for line in problem_description.splitlines():
            title = line.lstrip("#").strip()
            if title:
                return title[:80]
        return f"项目 {project_id[:8]}"

    def recover_interrupted_checks(self) -> list[str]:
        """Release checking states left behind by a process restart or crash."""
        recovered: list[str] = []
        for project_id in self.storage.project_ids():
            record = self.get(project_id)
            interrupted = [
                stage
                for stage, state in record.stages.items()
                if state.status == StageStatus.CHECKING
            ]
            if not interrupted:
                continue
            for stage in interrupted:
                state = record.stages[stage]
                state.status = StageStatus.FAILED
                state.ai_confirmed = False
                state.user_confirmed = False
                state.issues = ["上一次 AI 检查未正常结束；可以重新运行。"]
                state.updated_at = datetime.now(UTC)
                record.stage_threads.pop(stage, None)
            record.last_error = {
                "code": "STAGE_INTERRUPTED",
                "message": "服务重启或请求中断，已结束遗留的 AI 检查。",
                "stage": max(interrupted),
                "details": {"interrupted_stages": interrupted},
            }
            record.updated_at = datetime.now(UTC)
            self.storage.save_record(record)
            recovered.append(project_id)
        return recovered

    def prepare_auto_run_resume(self, project_id: str) -> tuple[ProjectRecord, int | None]:
        """Make the earliest interrupted AI stage the safe auto-run restart point."""

        record = self.get(project_id)
        interrupted = sorted(
            stage
            for stage, state in record.stages.items()
            if state.status == StageStatus.CHECKING
        )
        if not interrupted:
            return record, None

        recovery_stage = interrupted[0]
        state = record.stages[recovery_stage]
        state.status = StageStatus.FAILED
        state.ai_confirmed = False
        state.user_confirmed = False
        state.issues = ["上次一键生成未正常结束，已从此阶段恢复。"]
        state.updated_at = datetime.now(UTC)
        record.stage_threads.pop(recovery_stage, None)
        record.failed_stage_threads.pop(recovery_stage, None)
        self._invalidate_downstream(record, recovery_stage)
        record.current_stage = Stage(recovery_stage)
        record.build_complete = False
        record.export_ready = False
        record.generation_complete = False
        record.generated_subtasks = []
        if recovery_stage <= Stage.SUBTASK_PLAN:
            record.code_input_revision = None
            record.code_subtasks_revision = None
        record.last_error = {
            "code": "AUTO_RUN_RESUMED",
            "message": "一键生成已回收上次中断的阶段并准备继续执行。",
            "stage": recovery_stage,
            "details": {"interrupted_stages": interrupted},
        }
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        self.storage.invalidate_downstream_artifacts(project_id, recovery_stage)
        return record, recovery_stage

    def invalidate_obsolete_agent4_state(self) -> list[str]:
        """Discard state from older Agent4 architectures instead of interpreting it."""

        migrated: list[str] = []
        architecture = {"architecture": AGENT4_ARCHITECTURE_ID}
        for project_id in self.storage.project_ids():
            project_dir = self.storage.project_dir(project_id)
            marker_path = project_dir / "state" / "agent4-architecture.json"
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                marker = None
            if marker == architecture:
                continue

            record = self.get(project_id)
            # Stage 3 changed from an input-structure template to a test-data
            # design document.  Old drafts cannot be reinterpreted safely.
            (project_dir / "state" / "input_structure.json").unlink(missing_ok=True)
            (project_dir / "state" / "test_data_plan.json").unlink(missing_ok=True)
            self.storage.invalidate_downstream_artifacts(project_id, Stage.TEST_DATA_PLAN)
            self._invalidate_downstream(record, Stage.TEST_DATA_PLAN)
            record.stage_threads.pop(int(Stage.TEST_DATA_PLAN), None)
            record.failed_stage_threads.pop(int(Stage.TEST_DATA_PLAN), None)
            if int(record.current_stage) >= int(Stage.TEST_DATA_PLAN):
                stage3 = record.stages[int(Stage.TEST_DATA_PLAN)]
                stage3.status = StageStatus.DRAFT
                stage3.ai_confirmed = False
                stage3.user_confirmed = False
                stage3.issues = ["阶段三已重构为测试数据生成方案，请重新运行 AI 检查。"]
                stage3.updated_at = datetime.now(UTC)
                record.current_stage = Stage.TEST_DATA_PLAN
            record.workflow_revision += 1
            record.code_input_revision = None
            record.code_subtasks_revision = None
            record.generated_subtasks = []
            record.generation_complete = False
            record.build_complete = False
            record.export_ready = False
            record.last_error = None
            record.updated_at = datetime.now(UTC)
            self.storage.save_record(record)
            self.storage.write_json(marker_path, architecture)
            migrated.append(project_id)
        return migrated

    def update_solution(self, project_id: str, payload: SolutionUpdate) -> ProjectRecord:
        record = self.get(project_id)
        input_data = self.storage.load_input(project_id)
        if input_data.solution.source == payload.solution_code:
            return record
        project_dir = self.storage.project_dir(project_id)
        self.storage.write_text(project_dir / "source" / "solution.cpp", payload.solution_code)
        input_data.solution.source = payload.solution_code
        input_data.solution.compile.status = "pending"
        input_data.solution.compile.log = ""
        input_data.revision += 1
        self.storage.save_input(project_id, input_data)
        record.input_revision = input_data.revision
        record.workflow_revision += 1
        record.solution_compiled = False
        record.current_stage = Stage.SOLUTION_COMPILE
        record.build_complete = False
        record.export_ready = False
        record.generation_complete = False
        record.generated_subtasks = []
        record.code_input_revision = None
        record.code_subtasks_revision = None
        record.stage_threads = {}
        record.failed_stage_threads = {}
        record.last_error = None
        for state in record.stages.values():
            state.status = StageStatus.DRAFT
            state.ai_confirmed = False
            state.user_confirmed = False
            state.issues = []
            state.updated_at = datetime.now(UTC)
        self.storage.invalidate_downstream_artifacts(project_id, Stage.SOLUTION_COMPILE)
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        return record

    def save_user_draft(self, project_id: str, stage: int, draft: dict[str, Any]) -> dict[str, Any]:
        record = self.get(project_id)
        self._require_interactive_stage(stage)
        self._require_reached(record, stage)
        validated = self.validate_draft(project_id, stage, draft)
        current = self.storage.load_draft(project_id, stage)
        unchanged = current is not None and self._draft_content(
            stage, current
        ) == self._draft_content(stage, validated)
        waiting_for_confirmation = (
            record.stages[stage].status == StageStatus.WAITING_USER or stage in record.stage_threads
        )
        if unchanged and not waiting_for_confirmation:
            return current
        validated["issues"] = []
        if stage == Stage.CODE_DRAFT:
            validated["input_revision"] = record.input_revision
            validated["subtasks_revision"] = record.subtasks_revision
            validated_model = self.storage.save_code_draft(
                project_id, CodeDraft.model_validate(validated)
            )
            validated = validated_model.model_dump(mode="json")
        else:
            self.storage.save_draft(project_id, stage, validated)

        if stage == Stage.SUBTASK_PLAN:
            record.subtasks_revision += 1

        record.workflow_revision += 1

        state = record.stages[stage]
        state.status = StageStatus.DRAFT
        state.ai_confirmed = False
        state.user_confirmed = False
        state.issues = list(validated.get("issues", []))
        state.updated_at = datetime.now(UTC)
        record.stage_threads.pop(stage, None)
        record.failed_stage_threads.pop(stage, None)
        self._invalidate_downstream(record, stage)
        self.storage.invalidate_downstream_artifacts(project_id, stage)
        record.current_stage = Stage(stage)
        record.build_complete = False
        record.export_ready = False
        record.generation_complete = False
        record.generated_subtasks = []
        record.code_input_revision = None
        record.code_subtasks_revision = None
        record.last_error = None
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        return validated

    @staticmethod
    def _draft_content(stage: int, draft: dict[str, Any]) -> dict[str, Any]:
        fields = {
            Stage.TEST_DATA_PLAN: ("plan_markdown",),
            Stage.SUBTASK_PLAN: ("subtasks",),
            Stage.CODE_DRAFT: (
                "format_contract_id",
                "generator_code",
                "validator_code",
            ),
        }[Stage(stage)]
        return {field: draft.get(field) for field in fields}

    def save_ai_result(
        self,
        project_id: str,
        stage: int,
        result: dict[str, Any],
        *,
        confirmed: bool,
        issues: list[str],
    ) -> dict[str, Any]:
        record = self.get(project_id)
        validated = self.validate_draft(
            project_id,
            stage,
            result,
            allow_tag_review=True,
        )
        current = self.storage.load_draft(project_id, stage)
        content_changed = current is None or self._draft_content(
            stage, current
        ) != self._draft_content(stage, validated)
        if stage == Stage.CODE_DRAFT:
            validated["input_revision"] = record.input_revision
            validated["subtasks_revision"] = record.subtasks_revision
            saved = self.storage.save_code_draft(project_id, CodeDraft.model_validate(validated))
            validated = saved.model_dump(mode="json")
        else:
            self.storage.save_draft(project_id, stage, validated)

        if stage == Stage.SUBTASK_PLAN and content_changed:
            record.subtasks_revision += 1

        state = record.stages[stage]
        state.ai_confirmed = confirmed
        state.user_confirmed = False
        state.issues = issues
        state.status = StageStatus.WAITING_USER if confirmed else StageStatus.DRAFT
        state.updated_at = datetime.now(UTC)
        record.stage_threads.pop(stage, None)
        record.failed_stage_threads.pop(stage, None)
        self._invalidate_downstream(record, stage)
        if content_changed:
            self.storage.invalidate_downstream_artifacts(project_id, stage)
        record.current_stage = Stage(stage)
        if stage <= Stage.SUBTASK_PLAN:
            record.code_input_revision = None
            record.code_subtasks_revision = None
        record.generation_complete = False
        record.generated_subtasks = []
        record.last_error = None
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        return validated

    def confirm(self, project_id: str, stage: int) -> ProjectRecord:
        record = self.get(project_id)
        self._require_interactive_stage(stage)
        self._require_current(record, stage)
        state = record.stages[stage]
        if not state.ai_confirmed:
            raise AppError(
                "CONFIRMATION_REQUIRED",
                "AI confirmation is required before user confirmation",
                stage=stage,
                status_code=409,
            )
        state.user_confirmed = True
        state.status = StageStatus.PASSED
        state.updated_at = datetime.now(UTC)
        record.current_stage = Stage(stage + 1)
        if stage == Stage.CODE_DRAFT:
            release = self.storage.freeze_code_release(
                project_id,
                input_revision=record.input_revision,
                subtasks_revision=record.subtasks_revision,
            )
            record.code_input_revision = record.input_revision
            record.code_subtasks_revision = record.subtasks_revision
            if release.revision_id != self.storage.current_revision(project_id):
                raise AppError(
                    "CODE_RELEASE_HASH_MISMATCH",
                    "冻结的发布快照修订号校验失败。",
                    stage=5,
                    status_code=409,
                )
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        return record

    def save_normalized_input(self, project_id: str, value: GlobalInput) -> GlobalInput:
        record = self.get(project_id)
        current = self.storage.load_input(project_id)
        value.problem.description = current.problem.description
        value.problem.difficulty = current.problem.difficulty
        value.solution.source = current.solution.source
        value.solution.compile = current.solution.compile
        value.revision = current.revision
        self.storage.save_input(project_id, value)
        record.project_name = value.project_name
        record.problem_description = value.problem.description
        record.difficulty = value.problem.difficulty
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        return value

    def mark_solution_compiled(
        self, project_id: str, compiled: bool, error: dict | None
    ) -> ProjectRecord:
        record = self.get(project_id)
        was_compiled = record.solution_compiled
        input_data = self.storage.load_input(project_id)
        input_data.solution.compile.status = "passed" if compiled else "failed"
        input_data.solution.compile.log = "" if compiled else str((error or {}).get("details", ""))
        self.storage.save_input(project_id, input_data)
        record.solution_compiled = compiled
        if compiled:
            if not was_compiled or record.current_stage <= Stage.SOLUTION_COMPILE:
                record.current_stage = Stage.TEST_DATA_PLAN
        else:
            if was_compiled or record.current_stage > Stage.SOLUTION_COMPILE:
                record.workflow_revision += 1
                record.stage_threads = {}
                record.failed_stage_threads = {}
                self._invalidate_downstream(record, Stage.SOLUTION_COMPILE)
                self.storage.invalidate_downstream_artifacts(project_id, Stage.SOLUTION_COMPILE)
                record.build_complete = False
                record.export_ready = False
                record.generation_complete = False
                record.generated_subtasks = []
                record.code_input_revision = None
                record.code_subtasks_revision = None
            record.current_stage = Stage.SOLUTION_COMPILE
        record.last_error = error
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        return record

    def set_stage_thread(self, project_id: str, stage: int, thread_id: str) -> None:
        record = self.get(project_id)
        record.stage_threads[stage] = thread_id
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)

    def pop_stage_thread(self, project_id: str, stage: int) -> str | None:
        record = self.get(project_id)
        thread_id = record.stage_threads.pop(stage, None)
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        return thread_id

    def mark_generation_complete(self, project_id: str, subtask_ids: list[int]) -> ProjectRecord:
        record = self.get(project_id)
        record.generated_subtasks = sorted(set(subtask_ids))
        record.generation_complete = True
        record.build_complete = False
        record.export_ready = False
        record.current_stage = Stage.VALIDATE_AND_SOLVE
        record.last_error = None
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        return record

    def mark_validation_complete(self, project_id: str) -> ProjectRecord:
        record = self.get(project_id)
        record.build_complete = True
        record.export_ready = True
        record.current_stage = Stage.EXPORT
        record.last_error = None
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        return record

    def mark_build_complete(self, project_id: str) -> ProjectRecord:
        return self.mark_validation_complete(project_id)

    def mark_pipeline_failure(
        self,
        project_id: str,
        recovery_stage: Stage,
        error: dict[str, Any],
    ) -> ProjectRecord:
        record = self.get(project_id)
        record.current_stage = recovery_stage
        record.build_complete = False
        record.export_ready = False
        record.generation_complete = False
        record.last_error = error
        if recovery_stage in (Stage.TEST_DATA_PLAN, Stage.SUBTASK_PLAN, Stage.CODE_DRAFT):
            state = record.stages[int(recovery_stage)]
            state.status = StageStatus.DRAFT
            state.ai_confirmed = False
            state.user_confirmed = False
            state.issues = [error["message"]]
            state.updated_at = datetime.now(UTC)
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        return record

    def mark_interactive_stage_failed(
        self,
        project_id: str,
        stage: int,
        error: dict[str, Any],
    ) -> ProjectRecord:
        self._require_interactive_stage(stage)
        record = self.get(project_id)
        state = record.stages[stage]
        state.status = StageStatus.FAILED
        state.ai_confirmed = False
        state.user_confirmed = False
        state.issues = [str(error.get("message") or "阶段检查失败。")]
        state.updated_at = datetime.now(UTC)
        record.stage_threads.pop(stage, None)
        record.failed_stage_threads.pop(stage, None)
        self._invalidate_downstream(record, stage)
        record.current_stage = Stage(stage)
        record.build_complete = False
        record.export_ready = False
        record.generation_complete = False
        record.generated_subtasks = []
        if stage <= Stage.SUBTASK_PLAN:
            record.code_input_revision = None
            record.code_subtasks_revision = None
        record.last_error = error
        record.updated_at = datetime.now(UTC)
        self.storage.save_record(record)
        return record

    def validate_draft(
        self,
        project_id: str,
        stage: int,
        draft: dict[str, Any],
        *,
        allow_tag_review: bool = False,
    ) -> dict[str, Any]:
        try:
            if stage == Stage.TEST_DATA_PLAN:
                model = TestDataPlanDraft.model_validate(draft)
                return model.model_dump(mode="json")
            if stage == Stage.SUBTASK_PLAN:
                model = SubtaskPlanDraft.model_validate(draft)
                test_data_plan = self.storage.load_draft(project_id, Stage.TEST_DATA_PLAN)
                if test_data_plan is None:
                    raise AppError(
                        "PREREQUISITE_REQUIRED",
                        "stage 3 test-data plan is missing",
                        stage=stage,
                        status_code=409,
                    )
                return model.model_dump(mode="json")
            if stage == Stage.CODE_DRAFT:
                return CodeDraft.model_validate(draft).model_dump(mode="json")
        except ValidationError as exc:
            raise AppError(
                "INVALID_DRAFT",
                "draft does not match the stage schema",
                stage=stage,
                details=exc.errors(include_url=False),
            ) from exc
        raise AppError("INVALID_STAGE", "only stages 3, 4 and 5 have drafts", stage=stage)

    @staticmethod
    def _invalidate_downstream(record: ProjectRecord, stage: int) -> None:
        for downstream in range(stage + 1, 6):
            record.stage_threads.pop(downstream, None)
            record.failed_stage_threads.pop(downstream, None)
            state = record.stages[downstream]
            state.status = StageStatus.DRAFT
            state.ai_confirmed = False
            state.user_confirmed = False
            state.issues = []
            state.updated_at = datetime.now(UTC)

    @staticmethod
    def _require_interactive_stage(stage: int) -> None:
        if stage not in (3, 4, 5):
            raise AppError("INVALID_STAGE", "interactive stage must be 3, 4 or 5", stage=stage)

    @staticmethod
    def _require_reached(record: ProjectRecord, stage: int) -> None:
        if stage > int(record.current_stage):
            raise AppError(
                "PREREQUISITE_REQUIRED",
                "the previous stage has not passed",
                stage=stage,
                status_code=409,
            )

    @staticmethod
    def _require_current(record: ProjectRecord, stage: int) -> None:
        if stage != int(record.current_stage):
            raise AppError(
                "STALE_STAGE",
                "只能核验或确认当前活动阶段。",
                stage=stage,
                status_code=409,
            )
