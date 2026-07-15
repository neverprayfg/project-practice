from __future__ import annotations

import asyncio
import hashlib
import json
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.models import CodeDraft, Counterexample, SubtaskPlanDraft
from app.services.agent4_document_context import (
    CONTEXT_FORMAT_VERSION,
    CONTEXT_LOADING_METHOD,
)
from app.services.compiler_diagnostics import parse_compiler_diagnostics
from app.services.generator_policy import generator_usage_issues
from app.services.runtime_parameters import runtime_parameter_issues, serialized_arguments
from app.services.sandbox import GenerationJob, Sandbox, ValidationJob
from app.services.testlib_policy import validator_usage_issues
from app.storage import ProjectStorage


def _recursive_context_value(value: Any) -> bool:
    if isinstance(value, str):
        return True
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(key, str) and bool(key) and _recursive_context_value(child)
        for key, child in value.items()
    )


def _context_has_text(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, dict):
        return False
    return any(_context_has_text(child) for child in value.values())


class Agent4CandidateVerifier:
    """Agent4-only deterministic validator and historical counterexample replayer."""

    def __init__(
        self,
        settings: Settings,
        storage: ProjectStorage,
        sandbox: Sandbox,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.sandbox = sandbox

    async def verify(
        self,
        project_id: str,
        candidate: dict[str, Any],
        context: dict[str, Any],
        counterexamples: list[Counterexample] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return await self._verify_code(
            project_id,
            candidate,
            context,
            counterexamples or [],
        )

    async def _verify_code(
        self,
        project_id: str,
        candidate: dict[str, Any],
        context: dict[str, Any],
        counterexamples: list[Counterexample],
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
        workspace_id = self.storage.prepare_agent4_verification_workspace(project_id, model)
        replayable_counterexamples = [
            item for item in counterexamples if self._replay_signature(item) is not None
        ]
        requested_replay_ids = {item.counterexample_id for item in replayable_counterexamples}
        completed_replay_ids: set[str] = set()
        fully_evaluated_operations: set[str] = set()

        def finish(
            verified: dict[str, Any], execution: dict[str, Any]
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            execution["requested_counterexample_ids"] = sorted(requested_replay_ids)
            execution["replayed_counterexample_ids"] = sorted(completed_replay_ids)
            execution["history_replay_complete"] = requested_replay_ids.issubset(
                completed_replay_ids
            )
            execution["fully_evaluated_operations"] = sorted(fully_evaluated_operations)
            return verified, execution

        documentation = context.get("agent4_library_context_bundle", {})
        documents = documentation.get("documents", []) if isinstance(documentation, dict) else []
        filenames = [
            str(item["filename"])
            for item in documents
            if isinstance(item, dict) and item.get("filename")
        ]
        role_contexts = (
            documentation.get("role_contexts", {})
            if isinstance(documentation, dict)
            else {}
        )
        raw_generator_libraries = (
            role_contexts.get("generator") if isinstance(role_contexts, dict) else None
        )
        raw_validator_libraries = (
            role_contexts.get("validator") if isinstance(role_contexts, dict) else None
        )
        generator_libraries = (
            raw_generator_libraries
            if isinstance(raw_generator_libraries, dict)
            else {}
        )
        validator_libraries = (
            raw_validator_libraries
            if isinstance(raw_validator_libraries, dict)
            else {}
        )
        documentation_ok = bool(
            isinstance(documentation, dict)
            and documentation.get("loading_method") == CONTEXT_LOADING_METHOD
            and documentation.get("format_version") == CONTEXT_FORMAT_VERSION
            and isinstance(documentation.get("roles"), dict)
            and isinstance(role_contexts, dict)
            and set(generator_libraries) == {"jngen_context"}
            and set(validator_libraries) == {"testlib_context"}
            and all(
                isinstance(library, dict)
                and set(library) == {"doc", "example"}
                and _recursive_context_value(library)
                and _context_has_text(library["doc"])
                and _context_has_text(library["example"])
                for library in (
                    generator_libraries.get("jngen_context"),
                    validator_libraries.get("testlib_context"),
                )
            )
            and bool(documents)
            and len(filenames) == documentation.get("document_count")
            and len(filenames) == len(set(filenames))
            and set(documentation["roles"]) == {"generator", "validator"}
            and all(
                documentation["roles"].get(role)
                and documentation["roles"][role]
                == [
                    item["filename"]
                    for item in documents
                    if isinstance(item, dict) and item.get("role") == role
                ]
                for role in ("generator", "validator")
            )
            and all(
                isinstance(item, dict)
                and item.get("role") in {"generator", "validator"}
                and isinstance(item.get("content"), str)
                and bool(item["content"])
                and isinstance(item.get("digest"), str)
                and item["digest"] == hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
                for item in documents
            )
            and documentation.get("total_characters")
            == sum(len(item["content"]) for item in documents)
        )
        expected_format_contract_id = (
            context.get("input_format_contract", {}).get("format_contract_id")
            if isinstance(context.get("input_format_contract"), dict)
            else None
        )
        format_contract_ok = bool(
            expected_format_contract_id
            and model.format_contract_id == expected_format_contract_id
        )
        checks: list[dict[str, Any]] = [
            {
                "operation": "input_format_contract",
                "ok": format_contract_ok,
                "expected_format_contract_id": expected_format_contract_id,
                "candidate_format_contract_id": model.format_contract_id,
            },
            {
                "operation": "agent4_library_context_bundle",
                "ok": documentation_ok,
                "filenames": filenames,
                "loading_method": documentation.get("loading_method")
                if isinstance(documentation, dict)
                else None,
                "document_count": len(filenames),
                "role_contexts_valid": bool(role_contexts),
            }
        ]
        fully_evaluated_operations.add("input_format_contract")
        if not format_contract_ok:
            return finish(
                model.model_dump(mode="json", exclude={"issues"}),
                self._failed(
                    "generator 与 validator 没有绑定到当前冻结的输入格式契约。",
                    checks,
                    "input_format",
                ),
            )
        fully_evaluated_operations.add("agent4_library_context_bundle")
        if not documentation_ok:
            return finish(
                model.model_dump(mode="json", exclude={"issues"}),
                self._failed(
                    "Agent4 必须接收 generator 与 validator 的完整角色文档上下文。",
                    checks,
                    "library_api",
                ),
            )
        usage_issues = generator_usage_issues(model.generator_code)
        fully_evaluated_operations.add("generator_library_usage")
        if usage_issues:
            return finish(
                model.model_dump(mode="json", exclude={"issues"}),
                {
                    "ok": False,
                    "failure_category": "library_api",
                    "validation_level": "static",
                    "message": "生成器未正确接入 testlib 或 jngen。",
                    "checks": [
                        *checks,
                        {
                            "operation": "generator_library_usage",
                            "ok": False,
                            "issues": usage_issues,
                        },
                    ],
                },
            )
        checks.append(
            {"operation": "generator_library_usage", "ok": True, "level": "static"}
        )
        input_format_contract = context.get("input_format_contract", {})
        sample_inputs = (
            input_format_contract.get("reference_sample_inputs", [])
            if isinstance(input_format_contract, dict)
            else []
        )
        requires_ascii_space = any(
            " " in line
            for sample in sample_inputs
            if isinstance(sample, str)
            for line in sample.splitlines()
        )
        validator_issues = validator_usage_issues(
            model.validator_code,
            requires_ascii_space=requires_ascii_space,
        )
        fully_evaluated_operations.add("testlib_usage")
        if validator_issues:
            return finish(
                model.model_dump(mode="json", exclude={"issues"}),
                self._failed(
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
                ),
            )
        checks.append({"operation": "testlib_usage", "ok": True, "level": "static"})
        plan_raw = self.storage.load_draft(project_id, 4)
        if plan_raw is None:
            return finish(
                model.model_dump(mode="json", exclude={"issues"}),
                self._failed("缺少已确认的生成计划。", checks, "generation_plan"),
            )
        plan = SubtaskPlanDraft.model_validate(plan_raw)
        parameter_issues = runtime_parameter_issues(plan)
        fully_evaluated_operations.add("runtime_parameters")
        if parameter_issues:
            return finish(
                model.model_dump(mode="json", exclude={"issues"}),
                self._failed("；".join(parameter_issues), checks, "generation_plan"),
            )
        checks.append({"operation": "runtime_parameters", "ok": True, "level": "static"})
        required_parameters = {
            parameter.name
            for subtask in plan.subtasks
            for profile in subtask.runtime_parameters
            for parameter in profile.parameters
        }
        usage_issues = generator_usage_issues(model.generator_code, required_parameters)
        fully_evaluated_operations.add("generator_runtime_parameters")
        if usage_issues:
            return finish(
                model.model_dump(mode="json", exclude={"issues"}),
                {
                    "ok": False,
                    "failure_category": "library_api",
                    "validation_level": "static",
                    "message": "生成器没有通过所选库读取全部运行时参数。",
                    "checks": [
                        *checks,
                        {
                            "operation": "generator_runtime_parameters",
                            "ok": False,
                            "issues": usage_issues,
                        },
                    ],
                },
            )
        checks.append(
            {"operation": "generator_runtime_parameters", "ok": True, "level": "static"}
        )
        roles = ("solution", "generator", "validator")
        compile_started = perf_counter()
        try:
            compiled_results = await asyncio.gather(
                *(self.sandbox.compile(workspace_id, role) for role in roles)
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
        fully_evaluated_operations.add("compile")
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
                return finish(
                    model.model_dump(mode="json", exclude={"issues"}),
                    self._failed(
                        f"{role} 编译失败。",
                        checks,
                        "timeout" if result.timed_out else "compile",
                        "compile",
                    ),
                )

        generation_jobs: list[GenerationJob] = []
        job_details: dict[str, tuple[int, int, int, str]] = {}
        for subtask in plan.subtasks:
            for profile in subtask.runtime_parameters:
                for seed_offset in range(self.settings.agent_trial_seeds_per_subtask):
                    seed = subtask.id * 1_000_003 + profile.case_id * 101 + seed_offset
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

        jobs_by_signature = {self._job_signature(job): job for job in generation_jobs}
        replay_ids_by_input: dict[str, set[str]] = {}
        for counterexample in replayable_counterexamples:
            signature = self._replay_signature(counterexample)
            if signature is None:  # Kept explicit for type narrowing.
                continue
            subtask_id, case_id, seed, runtime_arguments_json = signature
            runtime_arguments = json.loads(runtime_arguments_json)
            job = jobs_by_signature.get(signature)
            if job is None:
                preview_id = uuid4().hex[:12]
                input_relative = f"preview/{preview_id}.in"
                output_relative = f"preview/{preview_id}.out"
                job = GenerationJob(
                    subtask_id,
                    seed,
                    input_relative,
                    case_id=case_id,
                    runtime_arguments=runtime_arguments,
                )
                generation_jobs.append(job)
                job_details[input_relative] = (
                    subtask_id,
                    case_id,
                    seed,
                    output_relative,
                )
                jobs_by_signature[signature] = job
            replay_ids_by_input.setdefault(job.output_relative, set()).add(
                counterexample.counterexample_id
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
                project_id,
                workspace_id,
                jobs,
                job_details,
                checks,
                level,
                context,
                replay_ids_by_input,
                completed_replay_ids,
            )
            if failure is not None:
                message, category = failure
                return finish(
                    model.model_dump(mode="json", exclude={"issues"}),
                    self._failed(message, checks, category, level),
                )

        verified = model.model_copy(update={"trial_results": checks})
        return finish(
            verified.model_dump(mode="json", exclude={"issues"}),
            {
                "ok": True,
                "failure_category": None,
                "validation_level": "complete",
                "message": "代码编译及所有子任务试运行均通过。",
                "checks": checks,
            },
        )

    async def _run_trial_jobs(
        self,
        project_id: str,
        workspace_id: str,
        generation_jobs: list[GenerationJob],
        job_details: dict[str, tuple[int, int, int, str]],
        checks: list[dict[str, Any]],
        level: str,
        context: dict[str, Any],
        replay_ids_by_input: dict[str, set[str]],
        completed_replay_ids: set[str],
    ) -> tuple[str, str] | None:
        generation_started = perf_counter()
        try:
            generated_batches = await asyncio.gather(
                *(
                    self.sandbox.generate_batch(workspace_id, chunk)
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
            outcome.output_relative: outcome for batch in generated_batches for outcome in batch
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
                    "content": self._preview_content(workspace_id, job.output_relative),
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
                    self.sandbox.validate_solve_batch(workspace_id, chunk)
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
                    outcome.validation.ok and outcome.solution is not None and outcome.solution.ok
                    for batch in validated_batches
                    for outcome in batch
                )
                else "failed"
            ),
            metadata={"level": level, "jobs": len(validation_jobs)},
        )
        validated_outcomes = {
            outcome.input_relative: outcome for batch in validated_batches for outcome in batch
        }
        runtime_arguments = {job.output_relative: job.runtime_arguments for job in generation_jobs}
        for job in validation_jobs:
            subtask_id, case_id, seed, _output_relative = job_details[job.input_relative]
            outcome = validated_outcomes[job.input_relative]
            checks.append(
                {
                    "operation": "validate",
                    "level": level,
                    "subtask_id": subtask_id,
                    "case_id": case_id,
                    "seed": seed,
                    "runtime_arguments": runtime_arguments[job.input_relative],
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
                    "seed": seed,
                    "runtime_arguments": runtime_arguments[job.input_relative],
                    "result": outcome.solution.model_dump(mode="json"),
                }
            )
            if not outcome.solution.ok:
                category = "timeout" if outcome.solution.timed_out else "solution"
                return f"标程无法处理子任务 {subtask_id} 测试点 {case_id}。", category
            completed_replay_ids.update(replay_ids_by_input.get(job.input_relative, set()))
        return None

    @staticmethod
    def _job_signature(job: GenerationJob) -> tuple[int, int, int, str]:
        return (
            job.subtask_id,
            job.case_id,
            job.seed,
            json.dumps(
                job.runtime_arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _replay_signature(
        counterexample: Counterexample,
    ) -> tuple[int, int, int, str] | None:
        reproduction = counterexample.reproduction
        subtask_id = reproduction.get("subtask_id")
        case_id = reproduction.get("case_id")
        seed = reproduction.get("seed")
        runtime_arguments = reproduction.get("runtime_arguments")
        if not all(isinstance(value, int) for value in (subtask_id, case_id, seed)):
            return None
        if not isinstance(runtime_arguments, dict):
            return None
        serialized = {str(key): str(value) for key, value in runtime_arguments.items()}
        return (
            subtask_id,
            case_id,
            seed,
            json.dumps(
                serialized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def _failed(
        self,
        message: str,
        checks: list[dict[str, Any]],
        category: str = "unknown",
        validation_level: str = "static",
    ) -> dict[str, Any]:
        failed_checks = [
            check
            for check in checks
            if check.get("ok") is False
            or (isinstance(check.get("result"), dict) and check["result"].get("ok") is False)
        ]
        context_checks = [check for check in checks if check not in failed_checks][-8:]
        return {
            "ok": False,
            "message": message,
            "failure_category": category,
            "validation_level": validation_level,
            "checks": [*context_checks, *failed_checks],
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
