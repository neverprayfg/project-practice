from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.errors import AppError
from app.models import (
    CodeDraft,
    GlobalInput,
    InputStructureDraft,
    ProblemInput,
    ProjectCreate,
    ProjectRecord,
    SolutionInput,
    SolutionUpdate,
    Stage,
    StageStatus,
    SubtaskPlanDraft,
)
from app.services.structure_tag_catalog import StructureTagCatalog
from app.storage import ProjectStorage


class ProjectService:
    def __init__(
        self,
        storage: ProjectStorage,
        tag_catalog: StructureTagCatalog | None = None,
    ) -> None:
        self.storage = storage
        self.tag_catalog = tag_catalog or StructureTagCatalog()

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
        return record

    def get(self, project_id: str) -> ProjectRecord:
        return self.storage.load_record(project_id)

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
        input_data.input_structure.template = ""
        input_data.input_structure.status = "pending"
        input_data.revision += 1
        input_data.input_structure.revision = input_data.revision
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
        if current is not None and self._draft_content(stage, current) == self._draft_content(
            stage, validated
        ):
            return current
        validated["issues"] = []
        if stage == Stage.CODE_DRAFT:
            validated["trial_results"] = []
        if stage == Stage.CODE_DRAFT:
            validated["input_revision"] = record.input_revision
            validated["subtasks_revision"] = record.subtasks_revision
            validated_model = self.storage.save_code_draft(
                project_id, CodeDraft.model_validate(validated)
            )
            validated = validated_model.model_dump(mode="json")
        else:
            self.storage.save_draft(project_id, stage, validated)

        if stage == Stage.INPUT_STRUCTURE:
            self._update_input_structure(record, validated["template"], confirmed=False)
        elif stage == Stage.SUBTASK_PLAN:
            record.subtasks_revision += 1

        record.workflow_revision += 1

        state = record.stages[stage]
        state.status = StageStatus.DRAFT
        state.ai_confirmed = False
        state.user_confirmed = False
        state.issues = list(validated.get("issues", []))
        state.updated_at = datetime.now(UTC)
        record.stage_threads.pop(stage, None)
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
            Stage.INPUT_STRUCTURE: ("template", "structure_tags"),
            Stage.SUBTASK_PLAN: ("subtasks",),
            Stage.CODE_DRAFT: (
                "generator_code",
                "validator_code",
                "constraint_coverage",
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

        if stage == Stage.INPUT_STRUCTURE and content_changed:
            self._update_input_structure(record, validated["template"], confirmed=False)
        elif stage == Stage.SUBTASK_PLAN and content_changed:
            record.subtasks_revision += 1

        state = record.stages[stage]
        state.ai_confirmed = confirmed
        state.user_confirmed = False
        state.issues = issues
        state.status = StageStatus.WAITING_USER if confirmed else StageStatus.DRAFT
        state.updated_at = datetime.now(UTC)
        record.stage_threads.pop(stage, None)
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
        if stage == Stage.INPUT_STRUCTURE:
            input_data = self.storage.load_input(project_id)
            input_data.input_structure.status = "confirmed"
            self.storage.save_input(project_id, input_data)
        elif stage == Stage.CODE_DRAFT:
            record.code_input_revision = record.input_revision
            record.code_subtasks_revision = record.subtasks_revision
            self.storage.clear_agent4_feedback(project_id)
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
        value.input_structure = current.input_structure
        value.revision = current.revision
        self.storage.save_input(project_id, value)
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
                record.current_stage = Stage.INPUT_STRUCTURE
        else:
            if was_compiled or record.current_stage > Stage.SOLUTION_COMPILE:
                record.workflow_revision += 1
                record.stage_threads = {}
                self._invalidate_downstream(record, Stage.SOLUTION_COMPILE)
                self.storage.invalidate_downstream_artifacts(
                    project_id, Stage.SOLUTION_COMPILE
                )
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
        if recovery_stage in (Stage.INPUT_STRUCTURE, Stage.SUBTASK_PLAN, Stage.CODE_DRAFT):
            state = record.stages[int(recovery_stage)]
            state.status = StageStatus.DRAFT
            state.ai_confirmed = False
            state.user_confirmed = False
            state.issues = [error["message"]]
            state.updated_at = datetime.now(UTC)
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
            if stage == Stage.INPUT_STRUCTURE:
                model = InputStructureDraft.model_validate(draft)
                if model.structure_tags:
                    tag_issues = self.tag_catalog.validate_structure_tags(
                        model.structure_tags
                    )
                    if tag_issues and not allow_tag_review:
                        raise AppError(
                            "INVALID_STRUCTURE_TAGS",
                            "阶段三结构标签需要复核。",
                            stage=stage,
                            details={"issues": tag_issues},
                        )
                return model.model_dump(mode="json")
            if stage == Stage.SUBTASK_PLAN:
                model = SubtaskPlanDraft.model_validate(draft)
                structure = self.storage.load_draft(project_id, Stage.INPUT_STRUCTURE)
                if structure is None:
                    raise AppError(
                        "PREREQUISITE_REQUIRED",
                        "stage 3 input structure is missing",
                        stage=stage,
                        status_code=409,
                    )
                global_tag_ids = [
                    str(item["tag_id"])
                    for item in structure.get("structure_tags", [])
                    if isinstance(item, dict) and item.get("tag_id")
                ]
                for subtask in model.subtasks:
                    tag_issues = self.tag_catalog.validate_tag_ids(
                        [*global_tag_ids, *subtask.subtask_tags]
                    )
                    if tag_issues and not allow_tag_review:
                        raise AppError(
                            "INVALID_SUBTASK_TAGS",
                            f"子任务 {subtask.id} 的结构标签需要复核。",
                            stage=stage,
                            details={"issues": tag_issues},
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
            state = record.stages[downstream]
            state.status = StageStatus.DRAFT
            state.ai_confirmed = False
            state.user_confirmed = False
            state.issues = []
            state.updated_at = datetime.now(UTC)

    def _update_input_structure(
        self, record: ProjectRecord, template: str, *, confirmed: bool
    ) -> None:
        input_data = self.storage.load_input(record.project_id)
        input_data.revision += 1
        input_data.input_structure.template = template
        input_data.input_structure.status = "confirmed" if confirmed else "draft"
        input_data.input_structure.revision = input_data.revision
        self.storage.save_input(record.project_id, input_data)
        record.input_revision = input_data.revision

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
