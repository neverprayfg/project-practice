from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.config import Settings
from app.models import CodeDraft, GlobalInput, InputStructureDraft, SubtaskPlanDraft, TaskType
from app.services.compiler_diagnostics import parse_compiler_diagnostics
from app.services.jngen_policy import jngen_usage_issues
from app.services.sandbox import GenerationJob, Sandbox, ValidationJob
from app.storage import ProjectStorage


class AgentCandidateVerifier:
    """Runs deterministic checks outside the model and returns bounded feedback."""

    def __init__(self, settings: Settings, storage: ProjectStorage, sandbox: Sandbox) -> None:
        self.settings = settings
        self.storage = storage
        self.sandbox = sandbox

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
                return model.model_dump(mode="json", exclude={"issues"}), {
                    "ok": True,
                    "checks": ["template_schema", "template_non_empty"],
                }
            if task_type == TaskType.SUBTASK_PLAN:
                model = SubtaskPlanDraft.model_validate(candidate)
                if not context.get("subtasks") and len(model.subtasks) != 5:
                    return model.model_dump(mode="json", exclude={"issues"}), {
                        "ok": False,
                        "message": "首次规划必须自动生成 5 个子任务。",
                        "checks": ["subtask_schema", "initial_five_subtasks"],
                    }
                return model.model_dump(mode="json", exclude={"issues"}), {
                    "ok": True,
                    "checks": ["subtask_schema", "counts", "unique_ids"],
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
            return model.model_dump(mode="json", exclude={"issues"}), self._failed(
                "缺少 Agent4 已读取的 jngen 文档上下文。", checks
            )
        usage_issues = jngen_usage_issues(model.generator_code)
        if usage_issues:
            return model.model_dump(mode="json", exclude={"issues"}), {
                "ok": False,
                "message": "生成器未按当前输入结构使用 jngen。",
                "checks": [
                    *checks,
                    {"operation": "jngen_usage", "ok": False, "issues": usage_issues},
                ],
            }
        saved = self.storage.save_code_draft(project_id, model)
        roles = ("solution", "generator", "validator")
        compiled_results = await asyncio.gather(
            *(self.sandbox.compile(project_id, role) for role in roles)
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
                    f"{role} 编译失败。", checks
                )

        plan_raw = self.storage.load_draft(project_id, 4)
        if plan_raw is None:
            return saved.model_dump(mode="json", exclude={"issues"}), self._failed(
                "缺少已确认的子任务。", checks
            )
        plan = SubtaskPlanDraft.model_validate(plan_raw)
        self.storage.clear_directory(project_id, "preview")
        generation_jobs: list[GenerationJob] = []
        job_details: dict[str, tuple[int, int, str]] = {}
        for subtask in plan.subtasks:
            for seed_offset in range(self.settings.agent_trial_seeds_per_subtask):
                seed = subtask.id * 1_000_003 + seed_offset
                preview_id = uuid4().hex[:12]
                input_relative = f"preview/{preview_id}.in"
                output_relative = f"preview/{preview_id}.out"
                generation_jobs.append(GenerationJob(subtask.id, seed, input_relative))
                job_details[input_relative] = (subtask.id, seed, output_relative)

        generated_batches = await asyncio.gather(
            *(
                self.sandbox.generate_batch(project_id, chunk)
                for chunk in self._chunks(generation_jobs)
            )
        )
        generated_outcomes = {
            outcome.output_relative: outcome
            for batch in generated_batches
            for outcome in batch
        }
        validation_jobs: list[ValidationJob] = []
        for job in generation_jobs:
            subtask_id, seed, output_relative = job_details[job.output_relative]
            generated = generated_outcomes[job.output_relative].result
            checks.append(
                {
                    "operation": "generate",
                    "subtask_id": subtask_id,
                    "seed": seed,
                    "result": generated.model_dump(mode="json"),
                    "content": self._preview_content(project_id, job.output_relative),
                }
            )
            if not generated.ok:
                return saved.model_dump(mode="json", exclude={"issues"}), self._failed(
                    f"子任务 {subtask_id} 试生成失败。", checks
                )
            validation_jobs.append(ValidationJob(job.output_relative, output_relative))

        validated_batches = await asyncio.gather(
            *(
                self.sandbox.validate_solve_batch(project_id, chunk)
                for chunk in self._chunks(validation_jobs)
            )
        )
        validated_outcomes = {
            outcome.input_relative: outcome
            for batch in validated_batches
            for outcome in batch
        }
        for job in validation_jobs:
            subtask_id, _seed, _output_relative = job_details[job.input_relative]
            outcome = validated_outcomes[job.input_relative]
            checks.append(
                {
                    "operation": "validate",
                    "subtask_id": subtask_id,
                    "result": outcome.validation.model_dump(mode="json"),
                }
            )
            if not outcome.validation.ok:
                return saved.model_dump(mode="json", exclude={"issues"}), self._failed(
                    f"子任务 {subtask_id} 的试生成数据未通过 validator。", checks
                )
            if outcome.solution is None:
                return saved.model_dump(mode="json", exclude={"issues"}), self._failed(
                    f"标程未处理子任务 {subtask_id} 的试生成数据。", checks
                )
            checks.append(
                {
                    "operation": "solve",
                    "subtask_id": subtask_id,
                    "result": outcome.solution.model_dump(mode="json"),
                }
            )
            if not outcome.solution.ok:
                return saved.model_dump(mode="json", exclude={"issues"}), self._failed(
                    f"标程无法处理子任务 {subtask_id} 的试生成数据。", checks
                )

        saved = self.storage.save_code_draft(
            project_id,
            saved.model_copy(update={"trial_results": checks}),
        )
        return saved.model_dump(mode="json", exclude={"issues"}), {
            "ok": True,
            "message": "代码编译及所有子任务试运行均通过。",
            "checks": checks,
        }

    def _failed(self, message: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "ok": False,
            "message": message,
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
