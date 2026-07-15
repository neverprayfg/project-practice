from __future__ import annotations

import asyncio
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from shutil import copy2
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.errors import AppError
from app.models import Stage, StageStatus, Subtask, SubtaskPlanDraft
from app.services.counterexample_ledger import CounterexampleLedgerService
from app.services.defects import defects_from_execution
from app.services.project_service import ProjectService
from app.services.runtime_parameters import profile_for_case, serialized_arguments
from app.services.sandbox import GenerationJob, Sandbox, ValidationJob
from app.storage import ProjectStorage


class DatasetService:
    def __init__(
        self,
        settings: Settings,
        storage: ProjectStorage,
        projects: ProjectService,
        sandbox: Sandbox,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.projects = projects
        self.sandbox = sandbox

    async def generate_inputs(
        self,
        project_id: str,
        base_seed: int,
        selected_subtask_ids: list[int] | None,
    ) -> dict[str, Any]:
        record = self.projects.get(project_id)
        if record.stages[5].status != StageStatus.PASSED:
            raise AppError(
                "CONFIRMATION_REQUIRED",
                "阶段 5 必须由智能体和用户共同确认后才能生成数据。",
                stage=5,
                status_code=409,
            )
        if (
            record.code_input_revision != record.input_revision
            or record.code_subtasks_revision != record.subtasks_revision
        ):
            raise AppError(
                "STALE_CODE",
                "当前代码不是基于最新 INPUT 和 SUBTASKS 生成的。",
                stage=5,
                status_code=409,
            )
        plan = self._load_plan(project_id)
        selected = self._select_subtasks(plan, selected_subtask_ids)
        code_revision = self.storage.current_revision(project_id)
        if code_revision is None:
            raise AppError("PREREQUISITE_REQUIRED", "缺少已确认的代码修订。", stage=5)
        manifest: dict[str, Any] = {
            "batch_id": uuid4().hex,
            "status": "generating",
            "workflow_revision": record.workflow_revision,
            "input_revision": record.input_revision,
            "subtasks_revision": record.subtasks_revision,
            "code_revision": code_revision,
            "base_seed": base_seed,
            "selected_subtasks": [subtask.id for subtask in selected],
            "files": [],
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self.storage.save_batch_manifest(project_id, manifest)
        compiled = await self.sandbox.compile(project_id, "generator")
        if not compiled.ok:
            self._fail_to_agent4(
                project_id,
                "COMPILE_FAILED",
                "generator 编译失败。",
                {"operation": "compile", "role": "generator", "result": compiled},
            )

        selected_ids = {subtask.id for subtask in selected}
        self.storage.clear_directory(project_id, "data")
        jobs: list[GenerationJob] = []
        job_details: dict[str, tuple[Subtask, int, int, str]] = {}
        for subtask in selected:
            for internal_id in range(1, subtask.test_count + 1):
                stem = f"{subtask.id}_{internal_id}"
                input_relative = f"data/{stem}.in"
                seed = base_seed + subtask.id * 1_000_000 + internal_id
                profile = profile_for_case(subtask, internal_id)
                runtime_arguments = serialized_arguments(profile)
                jobs.append(
                    GenerationJob(
                        subtask.id,
                        seed,
                        input_relative,
                        case_id=internal_id,
                        runtime_arguments=runtime_arguments,
                    )
                )
                job_details[input_relative] = (subtask, internal_id, seed, stem)

        batches = await asyncio.gather(
            *(
                self.sandbox.generate_batch(project_id, chunk)
                for chunk in self._chunks(jobs, self.settings.runner_batch_size)
            )
        )
        outcomes = {
            outcome.output_relative: outcome
            for batch in batches
            for outcome in batch
        }
        generated_count = 0
        for job in jobs:
            subtask, internal_id, seed, stem = job_details[job.output_relative]
            generated = outcomes[job.output_relative].result
            if not generated.ok:
                self._fail_to_agent4(
                    project_id,
                    "GENERATION_FAILED",
                    f"子任务 {subtask.id} 的测试点 {internal_id} 生成失败。",
                    {
                        "operation": "generate",
                        "subtask_id": subtask.id,
                        "case_id": internal_id,
                        "seed": seed,
                        "runtime_arguments": job.runtime_arguments,
                        "result": generated,
                    },
                )
            manifest["files"].append(
                {
                    "subtask_id": subtask.id,
                    "internal_id": internal_id,
                    "seed": seed,
                    "runtime_arguments": job.runtime_arguments,
                    "input_file": f"{stem}.in",
                    "generation": self._result_summary(generated),
                }
            )
            generated_count += 1
            if (
                generated_count % self.settings.manifest_checkpoint_interval == 0
                and generated_count < len(jobs)
            ):
                manifest["updated_at"] = datetime.now(UTC).isoformat()
                self.storage.save_batch_manifest(project_id, manifest)
        manifest["status"] = "generated"
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        self.storage.save_batch_manifest(project_id, manifest)
        self.projects.mark_generation_complete(project_id, sorted(selected_ids))
        return {
            "ok": True,
            "generated_tests": generated_count,
            "selected_subtasks": sorted(selected_ids),
            "next_stage": 7,
        }

    async def validate_and_solve(
        self,
        project_id: str,
        selected_subtask_ids: list[int] | None,
    ) -> dict[str, Any]:
        record = self.projects.get(project_id)
        if not record.generation_complete:
            raise AppError(
                "PREREQUISITE_REQUIRED",
                "阶段 6 尚未完成批量生成。",
                stage=7,
                status_code=409,
            )
        plan = self._load_plan(project_id)
        requested = selected_subtask_ids or record.generated_subtasks
        selected = self._select_subtasks(plan, requested)
        selected_ids = {subtask.id for subtask in selected}
        if not selected_ids.issubset(set(record.generated_subtasks)):
            raise AppError(
                "PREREQUISITE_REQUIRED",
                "所选子任务尚未在当前批次中生成。",
                stage=7,
                status_code=409,
            )
        manifest = self.storage.load_batch_manifest(project_id)
        if manifest is None or not self._manifest_matches(record, manifest):
            raise AppError(
                "STALE_BATCH",
                "当前数据批次与最新 INPUT、SUBTASKS 或代码修订不一致。",
                stage=6,
                status_code=409,
            )
        if selected_ids != set(manifest.get("selected_subtasks", [])):
            raise AppError(
                "STALE_BATCH",
                "验证范围必须与当前生成批次一致。",
                stage=6,
                status_code=409,
            )
        manifest["status"] = "validating"
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        self.storage.save_batch_manifest(project_id, manifest)
        roles = ("validator", "solution")
        compiled_results = await asyncio.gather(
            *(self.sandbox.compile(project_id, role) for role in roles)
        )
        for role, compiled in zip(roles, compiled_results, strict=True):
            if not compiled.ok:
                self._fail_to_agent4(
                    project_id,
                    "COMPILE_FAILED",
                    f"{role} 编译失败。",
                    {"operation": "compile", "role": role, "result": compiled},
                )

        jobs: list[ValidationJob] = []
        job_details: dict[str, tuple[Subtask, int, str]] = {}
        for subtask in selected:
            for internal_id in range(1, subtask.test_count + 1):
                stem = f"{subtask.id}_{internal_id}"
                input_relative = f"data/{stem}.in"
                output_relative = f"data/{stem}.out"
                input_path = self.storage.project_dir(project_id) / input_relative
                if not input_path.is_file():
                    batch_entry = next(
                        (
                            item
                            for item in manifest["files"]
                            if item.get("subtask_id") == subtask.id
                            and item.get("internal_id") == internal_id
                        ),
                        {},
                    )
                    self._fail_to_agent4(
                        project_id,
                        "INPUT_MISSING",
                        f"缺少输入文件 {stem}.in。",
                        {
                            "operation": "generate",
                            "target_file": "generator.cpp",
                            "subtask_id": subtask.id,
                            "case_id": internal_id,
                            "seed": batch_entry.get("seed"),
                            "runtime_arguments": batch_entry.get(
                                "runtime_arguments", {}
                            ),
                        },
                    )
                jobs.append(ValidationJob(input_relative, output_relative))
                job_details[input_relative] = (subtask, internal_id, stem)

        batches = await asyncio.gather(
            *(
                self.sandbox.validate_solve_batch(project_id, chunk)
                for chunk in self._chunks(jobs, self.settings.runner_batch_size)
            )
        )
        outcomes = {
            outcome.input_relative: outcome
            for batch in batches
            for outcome in batch
        }
        file_index = {
            (item.get("subtask_id"), item.get("internal_id")): item
            for item in manifest["files"]
        }
        validated_count = 0
        for job in jobs:
            subtask, internal_id, stem = job_details[job.input_relative]
            outcome = outcomes[job.input_relative]
            file_entry = file_index.get((subtask.id, internal_id))
            if file_entry is None:
                self._fail_to_agent4(
                    project_id,
                    "INPUT_MISSING",
                    f"批次清单缺少 {stem}.in。",
                    {
                        "operation": "generate",
                        "target_file": "generator.cpp",
                        "subtask_id": subtask.id,
                        "case_id": internal_id,
                    },
                )
            if not outcome.validation.ok:
                self._fail_to_agent4(
                    project_id,
                    "VALIDATION_FAILED",
                    f"validator 拒绝了 {stem}.in。",
                    {
                        "operation": "validate",
                        "subtask_id": subtask.id,
                        "case_id": internal_id,
                        "seed": file_entry.get("seed"),
                        "runtime_arguments": file_entry.get("runtime_arguments", {}),
                        "result": outcome.validation,
                    },
                )
            if outcome.solution is None or not outcome.solution.ok:
                self._fail_to_agent4(
                    project_id,
                    "SOLUTION_FAILED",
                    f"标程处理 {stem}.in 失败。",
                    {
                        "operation": "solve",
                        "subtask_id": subtask.id,
                        "case_id": internal_id,
                        "seed": file_entry.get("seed"),
                        "runtime_arguments": file_entry.get("runtime_arguments", {}),
                        "result": outcome.solution,
                    },
                )
            file_entry["validation"] = self._result_summary(outcome.validation)
            file_entry["solution"] = self._result_summary(outcome.solution)
            file_entry["output_file"] = f"{stem}.out"
            validated_count += 1
            if (
                validated_count % self.settings.manifest_checkpoint_interval == 0
                and validated_count < len(jobs)
            ):
                manifest["updated_at"] = datetime.now(UTC).isoformat()
                self.storage.save_batch_manifest(project_id, manifest)
        manifest["status"] = "completed"
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        self.storage.save_batch_manifest(project_id, manifest)
        self.projects.mark_validation_complete(project_id)
        return {
            "validation_ok": True,
            "validated_tests": validated_count,
            "export_ready": True,
        }

    def export(self, project_id: str) -> Path:
        record = self.projects.get(project_id)
        if not record.export_ready:
            raise AppError(
                "EXPORT_NOT_READY",
                "阶段 7 尚未完成，当前不能导出。",
                stage=8,
                status_code=409,
            )
        manifest = self.storage.load_batch_manifest(project_id)
        if manifest is not None and (
            manifest.get("status") != "completed"
            or not self._manifest_matches(record, manifest)
        ):
            raise AppError(
                "EXPORT_NOT_READY",
                "当前数据包不属于最新确认的 INPUT、SUBTASKS 与代码修订。",
                stage=8,
                status_code=409,
            )
        project_dir = self.storage.project_dir(project_id)
        data_files = sorted((project_dir / "data").glob("*.*"))
        inputs = {path.stem for path in data_files if path.suffix == ".in"}
        outputs = {path.stem for path in data_files if path.suffix == ".out"}
        if not inputs or inputs != outputs:
            raise AppError("EXPORT_NOT_READY", "输入输出文件未完整配对。", stage=8)
        export_dir = self.storage.clear_directory(project_id, "export")
        archive = export_dir / "dataset.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.write(project_dir / "generated" / "generator.cpp", "generator.cpp")
            output.write(project_dir / "generated" / "validator.cpp", "validator.cpp")
            for path in data_files:
                output.write(path, f"data/{path.name}")
        if self.settings.export_root is not None:
            self.settings.export_root.mkdir(parents=True, exist_ok=True)
            copy2(archive, self.settings.export_root / f"{project_id}-dataset.zip")
        return archive

    def _load_plan(self, project_id: str) -> SubtaskPlanDraft:
        raw = self.storage.load_draft(project_id, 4)
        if raw is None:
            raise AppError("PREREQUISITE_REQUIRED", "缺少子任务配置。", stage=4)
        plan = SubtaskPlanDraft.model_validate(raw)
        missing = [
            subtask.id for subtask in plan.subtasks if not subtask.runtime_parameters
        ]
        if missing:
            raise AppError(
                "PREREQUISITE_REQUIRED",
                "子任务缺少逐测试点运行时参数，请返回阶段 4 重新确认。",
                stage=4,
                details={"subtask_ids": missing},
            )
        return plan

    @staticmethod
    def _select_subtasks(
        plan: SubtaskPlanDraft,
        selected_subtask_ids: list[int] | None,
    ) -> list[Subtask]:
        if selected_subtask_ids is None:
            return plan.subtasks
        if not selected_subtask_ids:
            raise AppError("INVALID_SUBTASK", "至少选择一个子任务。", stage=6)
        requested = set(selected_subtask_ids)
        selected = [subtask for subtask in plan.subtasks if subtask.id in requested]
        if {subtask.id for subtask in selected} != requested:
            raise AppError("INVALID_SUBTASK", "选择中包含不存在的子任务。", stage=6)
        return selected

    def _fail_to_agent4(
        self,
        project_id: str,
        code: str,
        message: str,
        check_data: dict[str, Any],
    ) -> None:
        record = self.projects.get(project_id)
        serialized = {
            key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
            for key, value in check_data.items()
        }
        check = {
            **serialized,
            "ok": False,
            "error_code": code,
            "issues": [message],
        }
        execution = {
            "ok": False,
            "failure_category": self._failure_category(code, serialized),
            "validation_level": (
                "compile" if serialized.get("operation") == "compile" else "complete"
            ),
            "message": message,
            "checks": [check],
        }
        defects = defects_from_execution(execution)
        if len(defects) != 1:
            raise AppError(
                "AGENT4_DEFECT_NORMALIZATION_FAILED",
                "后续阶段失败无法转换为唯一稳定缺陷。",
                stage=5,
            )
        defect = defects[0]
        revision = self.storage.current_revision(project_id)
        if revision is None:
            raise AppError(
                "AGENT4_STATE_INCOMPATIBLE",
                "后续阶段失败缺少当前阶段五候选修订。",
                stage=5,
                status_code=409,
            )
        ledger_service = CounterexampleLedgerService(self.storage)
        ledger = ledger_service.load(project_id)
        ledger_service.observe(
            project_id,
            ledger,
            [defect],
            revision,
            closable_defect_ids=set(),
        )
        entry = {
            "code": code,
            "message": message,
            "defect_id": defect.defect_id,
            "defect": defect.model_dump(mode="json"),
            "workflow_revision": record.workflow_revision,
            "input_revision": record.input_revision,
            "subtasks_revision": record.subtasks_revision,
            "code_revision": self.storage.current_revision(project_id),
            "check": check,
        }
        manifest = self.storage.load_batch_manifest(project_id)
        if manifest is not None:
            manifest["status"] = "failed"
            manifest["failure"] = entry
            manifest["updated_at"] = datetime.now(UTC).isoformat()
            self.storage.save_batch_manifest(project_id, manifest)
        error = {"code": code, "message": message, "stage": 5, "details": entry}
        self.projects.mark_pipeline_failure(project_id, Stage.CODE_DRAFT, error)
        raise AppError(code, message, stage=5, details=entry)

    @staticmethod
    def _failure_category(code: str, feedback: dict[str, Any]) -> str:
        result = feedback.get("result")
        if isinstance(result, dict) and result.get("timed_out"):
            return "timeout"
        return {
            "COMPILE_FAILED": "compile",
            "GENERATION_FAILED": "generation",
            "INPUT_MISSING": "generation",
            "VALIDATION_FAILED": "validation",
            "SOLUTION_FAILED": "solution",
            "OUTPUT_MISSING": "solution",
        }.get(code, "unknown")

    def _manifest_matches(self, record: Any, manifest: dict[str, Any]) -> bool:
        return (
            manifest.get("workflow_revision", 1) == record.workflow_revision
            and manifest.get("input_revision") == record.input_revision
            and manifest.get("subtasks_revision") == record.subtasks_revision
            and manifest.get("code_revision") == self.storage.current_revision(record.project_id)
        )

    @staticmethod
    def _chunks(items: list[Any], size: int) -> list[list[Any]]:
        return [items[index : index + size] for index in range(0, len(items), size)]

    @staticmethod
    def _result_summary(result: Any) -> dict[str, Any]:
        return {
            "ok": result.ok,
            "exit_code": result.exit_code,
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
            "output_file": result.output_file,
        }
