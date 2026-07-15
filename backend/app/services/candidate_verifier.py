from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.config import Settings
from app.models import CodeDraft, GlobalInput, InputStructureDraft, SubtaskPlanDraft, TaskType
from app.services.compiler_diagnostics import parse_compiler_diagnostics
from app.services.jngen_policy import jngen_usage_issues
from app.services.runtime_parameters import (
    coverage_issues,
    runtime_parameter_issues,
    serialized_arguments,
    structure_tag_parameter_issues,
)
from app.services.sandbox import GenerationJob, Sandbox, ValidationJob
from app.services.structure_tag_catalog import StructureTagCatalog
from app.services.testlib_policy import validator_usage_issues
from app.storage import ProjectStorage


class AgentCandidateVerifier:
    """Runs deterministic checks outside the model and returns bounded feedback."""

    def __init__(
        self,
        settings: Settings,
        storage: ProjectStorage,
        sandbox: Sandbox,
        tag_catalog: StructureTagCatalog | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.sandbox = sandbox
        self.tag_catalog = tag_catalog or StructureTagCatalog()

    async def verify(
        self,
        project_id: str,
        task_type: TaskType,
        candidate: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            if task_type == TaskType.INPUT_NORMALIZATION:
                value = candidate.get("input", candidate)
                model = GlobalInput.model_validate(value)
                return model.model_dump(mode="json"), {"ok": True, "checks": ["input_schema"]}
            if task_type == TaskType.INPUT_STRUCTURE:
                model = InputStructureDraft.model_validate(candidate)
                tag_issues = (
                    self.tag_catalog.validate_structure_tags(model.structure_tags)
                    if model.structure_tags
                    else ["needs_tag_review：阶段三必须确认至少一个结构标签。"]
                )
                return model.model_dump(mode="json", exclude={"issues"}), {
                    "ok": not tag_issues,
                    "message": "；".join(tag_issues) if tag_issues else "输入结构与标签检查通过。",
                    "checks": [
                        "template_schema",
                        "template_non_empty",
                        "structure_tag_catalog",
                        "structure_tag_conflicts",
                    ],
                }
            if task_type == TaskType.SUBTASK_PLAN:
                model = SubtaskPlanDraft.model_validate(candidate)
                if not context.get("subtasks") and len(model.subtasks) != 5:
                    return model.model_dump(mode="json", exclude={"issues"}), {
                        "ok": False,
                        "message": "首次规划必须自动生成 5 个子任务。",
                        "checks": ["subtask_schema", "initial_five_subtasks"],
                    }
                parameter_issues = runtime_parameter_issues(model)
                global_tag_ids = [
                    str(item.get("tag_id"))
                    for item in context.get("confirmed_structure_tags", [])
                    if isinstance(item, dict) and item.get("tag_id")
                ]
                parameter_issues.extend(
                    structure_tag_parameter_issues(
                        model,
                        global_tag_ids,
                        self.tag_catalog,
                    )
                )
                return model.model_dump(mode="json", exclude={"issues"}), {
                    "ok": not parameter_issues,
                    "message": (
                        "；".join(parameter_issues)
                        if parameter_issues
                        else "子任务及逐测试点运行时参数检查通过。"
                    ),
                    "checks": [
                        "subtask_schema",
                        "counts",
                        "unique_ids",
                        "runtime_parameters",
                        "structure_tag_runtime_parameters",
                    ],
                }
            if task_type == TaskType.CODE_DRAFT:
                return await self._verify_code(project_id, candidate, context)
        except ValidationError as exc:
            return candidate, {
                "ok": False,
                "message": "候选结果不符合当前阶段的数据结构。",
                "details": exc.errors(include_url=False),
            }
        return candidate, {"ok": False, "message": "未知的智能体任务类型。"}

    async def _verify_code(
        self, project_id: str, candidate: dict[str, Any], context: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        record = self.storage.load_record(project_id)
        model = CodeDraft.model_validate(
            {
                **candidate,
                "input_revision": record.input_revision,
                "subtasks_revision": record.subtasks_revision,
                "trial_results": [],
            }
        )
        documentation = context.get("jngen_documentation", {})
        selected_documents = (
            documentation.get("selected_documents", [])
            if isinstance(documentation, dict)
            else []
        )
        selected_filenames = [
            str(item["filename"])
            for item in selected_documents
            if isinstance(item, dict) and item.get("filename")
        ]
        checks: list[dict[str, Any]] = [
            {
                "operation": "jngen_documentation",
                "selected_filenames": selected_filenames,
                "selection_rounds": (
                    documentation.get("selection_rounds", [])
                    if isinstance(documentation, dict)
                    else []
                ),
            }
        ]
        if not selected_filenames:
            execution = self._failed(
                "缺少 Agent4 已读取的 jngen 文档上下文。", checks, "library_api"
            )
            execution["retrieval_required"] = True
            return model.model_dump(mode="json", exclude={"issues"}), execution
        usage_issues = jngen_usage_issues(model.generator_code)
        if usage_issues:
            return model.model_dump(mode="json", exclude={"issues"}), {
                "ok": False,
                "failure_category": "library_api",
                "validation_level": "static",
                "message": "生成器未按当前输入结构使用 jngen。",
                "checks": [
                    *checks,
                    {"operation": "jngen_usage", "ok": False, "issues": usage_issues},
                ],
            }
        checks.append({"operation": "jngen_usage", "ok": True, "level": "static"})
        validator_issues = validator_usage_issues(model.validator_code)
        if validator_issues:
            return model.model_dump(mode="json", exclude={"issues"}), self._failed(
                "validator 未遵守 testlib 固定接入骨架。",
                [
                    *checks,
                    {
                        "operation": "testlib_usage",
                        "ok": False,
                        "issues": validator_issues,
                    },
                ],
                "library_api",
            )
        checks.append({"operation": "testlib_usage", "ok": True, "level": "static"})
        plan_raw = self.storage.load_draft(project_id, 4)
        if plan_raw is None:
            return model.model_dump(mode="json", exclude={"issues"}), self._failed(
                "缺少已确认的子任务。", checks, "constraint_coverage"
            )
        plan = SubtaskPlanDraft.model_validate(plan_raw)
        global_tag_ids = [
            str(item.get("tag_id"))
            for item in context.get("confirmed_structure_tags", [])
            if isinstance(item, dict) and item.get("tag_id")
        ]
        parameter_issues = runtime_parameter_issues(plan)
        parameter_issues.extend(
            structure_tag_parameter_issues(plan, global_tag_ids, self.tag_catalog)
        )
        if parameter_issues:
            return model.model_dump(mode="json", exclude={"issues"}), self._failed(
                "；".join(parameter_issues), checks, "constraint_coverage"
            )
        checks.append(
            {"operation": "runtime_parameters", "ok": True, "level": "static"}
        )
        coverage_problems = coverage_issues(model, plan, global_tag_ids)
        if coverage_problems:
            return model.model_dump(mode="json", exclude={"issues"}), {
                "ok": False,
                "failure_category": "constraint_coverage",
                "validation_level": "static",
                "message": "约束覆盖表与逐测试点参数不一致。",
                "checks": [
                    *checks,
                    {
                        "operation": "constraint_coverage",
                        "ok": False,
                        "issues": coverage_problems,
                    },
                ],
            }
        checks.append(
            {"operation": "constraint_coverage", "ok": True, "level": "static"}
        )
        required_parameters = {
            parameter.name
            for subtask in plan.subtasks
            for profile in subtask.runtime_parameters
            for parameter in profile.parameters
        }
        usage_issues = jngen_usage_issues(model.generator_code, required_parameters)
        if usage_issues:
            return model.model_dump(mode="json", exclude={"issues"}), {
                "ok": False,
                "failure_category": "library_api",
                "validation_level": "static",
                "message": "生成器未按当前输入结构使用 jngen。",
                "checks": [
                    *checks,
                    {"operation": "jngen_usage", "ok": False, "issues": usage_issues},
                ],
            }
        saved = self.storage.save_code_draft(project_id, model)
        roles = ("solution", "generator", "validator")
        compile_started = perf_counter()
        try:
            compiled_results = await asyncio.gather(
                *(self.sandbox.compile(project_id, role) for role in roles)
            )
        except Exception:
            self._append_timing(
                project_id,
                context,
                "compile",
                compile_started,
                status="error",
                metadata={"roles": list(roles)},
            )
            raise
        self._append_timing(
            project_id,
            context,
            "compile",
            compile_started,
            status="ok" if all(result.ok for result in compiled_results) else "failed",
            metadata={"roles": list(roles)},
        )
        for role, result in zip(roles, compiled_results, strict=True):
            serialized = result.model_dump(mode="json")
            compile_check = {
                "operation": "compile",
                "role": role,
                "result": serialized,
                "diagnostics": parse_compiler_diagnostics(result.stderr),
            }
            checks.append(compile_check)
            if not result.ok:
                return saved.model_dump(mode="json", exclude={"issues"}), self._failed(
                    f"{role} 编译失败。",
                    checks,
                    "timeout" if result.timed_out else "compile",
                    "compile",
                )

        self.storage.clear_directory(project_id, "preview")
        generation_jobs: list[GenerationJob] = []
        job_details: dict[str, tuple[int, int, int, str]] = {}
        for subtask in plan.subtasks:
            for profile in subtask.runtime_parameters:
                for seed_offset in range(self.settings.agent_trial_seeds_per_subtask):
                    seed = (
                        subtask.id * 1_000_003
                        + profile.case_id * 101
                        + seed_offset
                    )
                    preview_id = uuid4().hex[:12]
                    input_relative = f"preview/{preview_id}.in"
                    output_relative = f"preview/{preview_id}.out"
                    generation_jobs.append(
                        GenerationJob(
                            subtask.id,
                            seed,
                            input_relative,
                            case_id=profile.case_id,
                            runtime_arguments=serialized_arguments(profile),
                        )
                    )
                    job_details[input_relative] = (
                        subtask.id,
                        profile.case_id,
                        seed,
                        output_relative,
                    )

        smoke_paths: set[str] = set()
        for subtask in plan.subtasks:
            first = next(job for job in generation_jobs if job.subtask_id == subtask.id)
            smoke_paths.add(first.output_relative)
        phases = (
            ("smoke", [job for job in generation_jobs if job.output_relative in smoke_paths]),
            (
                "complete",
                [job for job in generation_jobs if job.output_relative not in smoke_paths],
            ),
        )
        for level, jobs in phases:
            if not jobs:
                continue
            failure = await self._run_trial_jobs(
                project_id, jobs, job_details, checks, level, context
            )
            if failure is not None:
                message, category = failure
                return saved.model_dump(mode="json", exclude={"issues"}), self._failed(
                    message, checks, category, level
                )

        saved = self.storage.save_code_draft(
            project_id,
            saved.model_copy(update={"trial_results": checks}),
        )
        return saved.model_dump(mode="json", exclude={"issues"}), {
            "ok": True,
            "failure_category": None,
            "validation_level": "complete",
            "message": "代码编译及所有子任务试运行均通过。",
            "checks": checks,
        }

    async def _run_trial_jobs(
        self,
        project_id: str,
        generation_jobs: list[GenerationJob],
        job_details: dict[str, tuple[int, int, int, str]],
        checks: list[dict[str, Any]],
        level: str,
        context: dict[str, Any],
    ) -> tuple[str, str] | None:
        generation_started = perf_counter()
        try:
            generated_batches = await asyncio.gather(
                *(
                    self.sandbox.generate_batch(project_id, chunk)
                    for chunk in self._chunks(generation_jobs)
                )
            )
        except Exception:
            self._append_timing(
                project_id,
                context,
                "trial_generation",
                generation_started,
                status="error",
                metadata={"level": level, "jobs": len(generation_jobs)},
            )
            raise
        self._append_timing(
            project_id,
            context,
            "trial_generation",
            generation_started,
            status=(
                "ok"
                if all(outcome.result.ok for batch in generated_batches for outcome in batch)
                else "failed"
            ),
            metadata={"level": level, "jobs": len(generation_jobs)},
        )
        generated_outcomes = {
            outcome.output_relative: outcome
            for batch in generated_batches
            for outcome in batch
        }
        validation_jobs: list[ValidationJob] = []
        for job in generation_jobs:
            subtask_id, case_id, seed, output_relative = job_details[job.output_relative]
            generated = generated_outcomes[job.output_relative].result
            checks.append(
                {
                    "operation": "generate",
                    "level": level,
                    "subtask_id": subtask_id,
                    "case_id": case_id,
                    "seed": seed,
                    "runtime_arguments": job.runtime_arguments,
                    "result": generated.model_dump(mode="json"),
                    "content": self._preview_content(project_id, job.output_relative),
                }
            )
            if not generated.ok:
                category = "timeout" if generated.timed_out else "generation"
                return f"子任务 {subtask_id} 测试点 {case_id} 试生成失败。", category
            validation_jobs.append(ValidationJob(job.output_relative, output_relative))

        validation_started = perf_counter()
        try:
            validated_batches = await asyncio.gather(
                *(
                    self.sandbox.validate_solve_batch(project_id, chunk)
                    for chunk in self._chunks(validation_jobs)
                )
            )
        except Exception:
            self._append_timing(
                project_id,
                context,
                "validation",
                validation_started,
                status="error",
                metadata={"level": level, "jobs": len(validation_jobs)},
            )
            raise
        self._append_timing(
            project_id,
            context,
            "validation",
            validation_started,
            status=(
                "ok"
                if all(
                    outcome.validation.ok
                    and outcome.solution is not None
                    and outcome.solution.ok
                    for batch in validated_batches
                    for outcome in batch
                )
                else "failed"
            ),
            metadata={"level": level, "jobs": len(validation_jobs)},
        )
        validated_outcomes = {
            outcome.input_relative: outcome
            for batch in validated_batches
            for outcome in batch
        }
        for job in validation_jobs:
            subtask_id, case_id, _seed, _output_relative = job_details[job.input_relative]
            outcome = validated_outcomes[job.input_relative]
            checks.append(
                {
                    "operation": "validate",
                    "level": level,
                    "subtask_id": subtask_id,
                    "case_id": case_id,
                    "result": outcome.validation.model_dump(mode="json"),
                }
            )
            if not outcome.validation.ok:
                category = "timeout" if outcome.validation.timed_out else "validation"
                return (
                    f"子任务 {subtask_id} 测试点 {case_id} 未通过 validator。",
                    category,
                )
            if outcome.solution is None:
                return f"标程未处理子任务 {subtask_id} 测试点 {case_id}。", "solution"
            checks.append(
                {
                    "operation": "solve",
                    "level": level,
                    "subtask_id": subtask_id,
                    "case_id": case_id,
                    "result": outcome.solution.model_dump(mode="json"),
                }
            )
            if not outcome.solution.ok:
                category = "timeout" if outcome.solution.timed_out else "solution"
                return f"标程无法处理子任务 {subtask_id} 测试点 {case_id}。", category
        return None

    def _failed(
        self,
        message: str,
        checks: list[dict[str, Any]],
        category: str = "unknown",
        validation_level: str = "static",
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "message": message,
            "failure_category": category,
            "validation_level": validation_level,
            "checks": checks[-12:],
        }

    def _chunks(self, items: list[Any]) -> list[list[Any]]:
        size = self.settings.runner_batch_size
        return [items[index : index + size] for index in range(0, len(items), size)]

    def _preview_content(self, project_id: str, relative: str) -> str:
        path = self.storage.project_dir(project_id) / relative
        if not path.is_file():
            return ""
        content = path.read_text(encoding="utf-8", errors="replace")
        limit = min(self.settings.max_log_chars, 4000)
        if len(content) <= limit:
            return content
        head = content[: limit // 2]
        tail = content[-(limit // 2) :]
        return f"{head}\n...<truncated {len(content) - limit} chars>...\n{tail}"

    def _append_timing(
        self,
        project_id: str,
        context: dict[str, Any],
        segment: str,
        started: float,
        *,
        status: str,
        metadata: dict[str, Any],
    ) -> None:
        timing = context.get("_agent4_timing")
        if not isinstance(timing, dict) or not isinstance(timing.get("run_id"), str):
            return
        entry: dict[str, Any] = {
            "run_id": timing["run_id"],
            "segment": segment,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "status": status,
            "metadata": metadata,
        }
        round_index = timing.get("round")
        if isinstance(round_index, int):
            entry["round"] = round_index
        self.storage.append_agent4_timing(project_id, entry)
