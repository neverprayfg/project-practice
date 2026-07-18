from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from app.errors import AppError
from app.models import (
    CodeDraft,
    GlobalInput,
    InputNormalizationDraft,
    RawCandidate,
    RecoveryError,
    RecoveryFailureClass,
    RecoveryValidationResult,
    SolutionUpdate,
    SubtaskPlanDraft,
)
from app.services.agent_recovery import (
    MAX_GENERATION_ROUNDS,
    MAX_REPAIR_ATTEMPTS,
    candidate_result,
    candidate_result_from_error,
    recovery_error,
    validation_failure,
    validation_success,
)
from app.services.agent_validators import Agent1Validator, Agent2Validator, Agent3Validator
from app.services.candidate_verifier import Agent4CandidateVerifier
from app.services.model_client import AgentModel
from app.services.project_service import ProjectService
from app.services.sandbox import Sandbox
from app.storage import ProjectStorage


class _StagePolicy:
    max_generation_rounds = MAX_GENERATION_ROUNDS
    max_repair_attempts = MAX_REPAIR_ATTEMPTS

    def __init__(self, storage: ProjectStorage) -> None:
        self.storage = storage

    async def persist_verified(self, project_id: str, candidate: dict[str, Any]) -> None:
        del project_id, candidate

    async def deterministic_fix(
        self,
        result: RecoveryValidationResult,
        context: dict[str, Any],
    ) -> RawCandidate | None:
        del result, context
        return None


class Agent1RecoveryPolicy(_StagePolicy):
    stage = 1
    agent_role = "agent1"
    can_regenerate = False
    max_generation_rounds = 1

    def __init__(
        self,
        storage: ProjectStorage,
        model: AgentModel,
        validator: Agent1Validator,
        authoritative: GlobalInput,
    ) -> None:
        super().__init__(storage)
        self.model = model
        self.validator = validator
        self.authoritative = authoritative

    async def generate_fresh(self, context: dict[str, Any]) -> RawCandidate:
        method = getattr(self.model, "agent1_normalize_result", None)
        authoritative = self.authoritative.model_dump(mode="json")
        if method is not None:
            result = await method(context, authoritative)
        else:
            normalized = await self.model.agent1_normalize(context, authoritative)
            result = candidate_result(normalized.model_dump(mode="json"))
        return self._merge(result)

    async def validate(
        self,
        candidate: RawCandidate,
        context: dict[str, Any],
    ) -> RecoveryValidationResult:
        contract_failure = _response_contract_failure(candidate)
        if contract_failure is not None:
            return contract_failure
        assert candidate.candidate is not None
        normalized, issues = self.validator.verify(candidate.candidate, context)
        if issues:
            return _business_failure(candidate, normalized, issues)
        return validation_success(normalized, candidate)

    async def repair(
        self,
        context: dict[str, Any],
        result: RecoveryValidationResult,
    ) -> RawCandidate:
        _require_recovery_artifact(context, self.stage, {"input_normalization"})
        revision_context = _revision_context(context, result)
        revise_result = getattr(self.model, "agent1_revise_result", None)
        if revise_result is not None:
            revised = await revise_result(revision_context, result.candidate or {})
        else:
            revise = getattr(self.model, "agent1_revise", self.model.agent1_normalize)
            normalized = await revise(revision_context, result.candidate or {})
            revised = candidate_result(normalized.model_dump(mode="json"))
        return self._merge(revised)

    async def clear_working_candidate(self, project_id: str) -> None:
        del project_id

    def _merge(self, result: RawCandidate) -> RawCandidate:
        if result.candidate is None:
            return result
        try:
            normalized = InputNormalizationDraft.model_validate(result.candidate)
        except ValidationError:
            return result
        problem = self.authoritative.problem.model_copy(
            update={
                "input_description": normalized.input_description,
                "output_description": normalized.output_description,
                "samples": normalized.samples,
            }
        )
        merged = self.authoritative.model_copy(
            update={"problem": problem, "project_name": normalized.project_name}
        ).model_dump(mode="json")
        return result.model_copy(update={"candidate": merged})


class Agent2RecoveryPolicy(_StagePolicy):
    stage = 3
    agent_role = "agent2"
    can_regenerate = True

    def __init__(
        self,
        storage: ProjectStorage,
        model: AgentModel,
        validator: Agent2Validator,
        existing: dict[str, Any],
    ) -> None:
        super().__init__(storage)
        self.model = model
        self.validator = validator
        self.existing = existing

    async def generate_fresh(self, context: dict[str, Any]) -> RawCandidate:
        instruction = context.get("user_instruction")
        if isinstance(instruction, str) and instruction:
            value = await self.model.agent2_apply_instruction(
                context,
                self.existing,
                instruction,
            )
            return candidate_result(value.model_dump(mode="json", exclude={"issues"}))
        method = getattr(self.model, "agent2_test_data_plan_result", None)
        if method is not None:
            return await method(context, {})
        return await _candidate_from_structured_call(
            self.model.agent2_test_data_plan(context, {})
        )

    async def validate(
        self,
        candidate: RawCandidate,
        context: dict[str, Any],
    ) -> RecoveryValidationResult:
        del context
        contract_failure = _response_contract_failure(candidate)
        if contract_failure is not None:
            return contract_failure
        assert candidate.candidate is not None
        normalized, issues = self.validator.verify(candidate.candidate)
        if issues:
            return _business_failure(candidate, normalized, issues)
        return validation_success(normalized, candidate)

    async def repair(
        self,
        context: dict[str, Any],
        result: RecoveryValidationResult,
    ) -> RawCandidate:
        _require_recovery_artifact(context, self.stage, {"test_data_plan"})
        revision_context = _revision_context(context, result)
        method = getattr(self.model, "agent2_revise_result", None)
        if method is not None:
            return await method(revision_context, result.candidate or {})
        revise = getattr(self.model, "agent2_revise", None)
        if revise is None:
            value = await self.model.agent2_apply_instruction(
                revision_context,
                result.candidate or {},
                "只修复后端 validation_errors 指出的格式或内容问题。",
            )
        else:
            value = await revise(revision_context, result.candidate or {})
        return candidate_result(value.model_dump(mode="json", exclude={"issues"}))

    async def clear_working_candidate(self, project_id: str) -> None:
        self.storage.clear_working_draft(project_id, self.stage)


class SolutionRecoveryPolicy(_StagePolicy):
    stage = 2
    agent_role = "solution-repair"
    can_regenerate = False
    max_generation_rounds = 1

    def __init__(
        self,
        storage: ProjectStorage,
        projects: ProjectService,
        sandbox: Sandbox,
        source: str,
        repair_source: Callable[
            [dict[str, Any], str, dict[str, Any]],
            Awaitable[str],
        ],
    ) -> None:
        super().__init__(storage)
        self.projects = projects
        self.sandbox = sandbox
        self.source = source
        self.repair_source = repair_source

    async def generate_fresh(self, context: dict[str, Any]) -> RawCandidate:
        del context
        return candidate_result(
            {"source": self.source},
            operation="current-solution",
            raw_output=self.source,
        )

    async def validate(
        self,
        candidate: RawCandidate,
        context: dict[str, Any],
    ) -> RecoveryValidationResult:
        source = (candidate.candidate or {}).get("source")
        if not isinstance(source, str) or not source.strip():
            return validation_failure(
                RecoveryFailureClass.RESPONSE_CONTRACT,
                repairable=True,
                candidate=candidate.candidate,
                raw_output=candidate.raw_output,
                errors=[
                    RecoveryError(
                        source="pydantic",
                        location=["source"],
                        message="标程修复结果缺少完整源码。",
                        code="missing_source",
                    )
                ],
            )
        execution = await self.sandbox.compile(context["project_id"], "solution")
        if execution.ok:
            return validation_success(
                {"source": source},
                candidate,
                diagnostics={"execution": execution.model_dump(mode="json")},
            )
        message = execution.stderr or "标程编译失败。"
        return validation_failure(
            RecoveryFailureClass.DETERMINISTIC_EXECUTION,
            repairable=True,
            candidate={"source": source},
            raw_output=candidate.raw_output,
            errors=[
                RecoveryError(
                    source="compiler",
                    location=["solution.cpp"],
                    message=message,
                    code="compile_failed",
                )
            ],
            diagnostics={"execution": execution.model_dump(mode="json")},
        )

    async def deterministic_fix(
        self,
        result: RecoveryValidationResult,
        context: dict[str, Any],
    ) -> RawCandidate | None:
        _require_recovery_artifact(context, self.stage, {"solution.cpp"})
        source = (result.candidate or {}).get("source")
        if not isinstance(source, str):
            return None
        extracted = _single_fenced_body(source, {"cpp", "c++", "cc"})
        if extracted is None:
            return None
        self.projects.update_solution(
            context["project_id"],
            SolutionUpdate(solution_code=extracted),
        )
        return candidate_result(
            {"source": extracted},
            operation="deterministic-code-fence",
            raw_output=extracted,
        )

    async def repair(
        self,
        context: dict[str, Any],
        result: RecoveryValidationResult,
    ) -> RawCandidate:
        _require_recovery_artifact(context, self.stage, {"solution.cpp"})
        source = str((result.candidate or {}).get("source") or result.raw_output)
        execution = result.diagnostics.get("execution")
        assert isinstance(execution, dict)
        repaired = await self.repair_source(context, source, execution)
        self.projects.update_solution(
            context["project_id"],
            SolutionUpdate(solution_code=repaired),
        )
        return candidate_result(
            {"source": repaired},
            operation="repair-solution",
            raw_output=repaired,
        )

    async def persist_verified(self, project_id: str, candidate: dict[str, Any]) -> None:
        del candidate
        self.projects.mark_solution_compiled(project_id, True, None)

    async def clear_working_candidate(self, project_id: str) -> None:
        del project_id


class Agent3RecoveryPolicy(_StagePolicy):
    stage = 4
    agent_role = "agent3"
    can_regenerate = True

    def __init__(
        self,
        storage: ProjectStorage,
        model: AgentModel,
        validator: Agent3Validator,
        existing: dict[str, Any],
    ) -> None:
        super().__init__(storage)
        self.model = model
        self.validator = validator
        self.existing = existing
        self._first_generation = True

    async def generate_fresh(self, context: dict[str, Any]) -> RawCandidate:
        first = self._first_generation
        self._first_generation = False
        if first and context.get("routed_recovery") and self.existing:
            return candidate_result(self.existing, operation="routed-resume")
        instruction = context.get("user_instruction")
        if isinstance(instruction, str) and instruction:
            value = await self.model.agent3_apply_instruction(
                context,
                self.existing,
                instruction,
            )
            return candidate_result(value.model_dump(mode="json", exclude={"issues"}))
        method = getattr(self.model, "agent3_plan_result", None)
        if method is not None:
            return await method(context, {})
        return await _candidate_from_structured_call(self.model.agent3_plan(context, {}))

    async def validate(
        self,
        candidate: RawCandidate,
        context: dict[str, Any],
    ) -> RecoveryValidationResult:
        contract_failure = _response_contract_failure(candidate)
        if contract_failure is not None:
            return contract_failure
        assert candidate.candidate is not None
        normalized, issues = self.validator.verify(candidate.candidate, context)
        if issues:
            return _business_failure(candidate, normalized, issues)
        return validation_success(normalized, candidate)

    async def deterministic_fix(
        self,
        result: RecoveryValidationResult,
        context: dict[str, Any],
    ) -> RawCandidate | None:
        _require_recovery_artifact(context, self.stage, {"subtask_plan"})
        normalized_schema = _normalize_runtime_parameter_schema(result.candidate)
        if normalized_schema is not None:
            return normalized_schema
        normalized = _normalize_generation_profile_accounting(result.candidate)
        if normalized is not None:
            return normalized
        if result.failure_class != RecoveryFailureClass.RESPONSE_CONTRACT:
            return None
        extracted = _single_fenced_body(result.raw_output, {"json"})
        if extracted is None:
            return None
        try:
            value = json.loads(extracted)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        return candidate_result(value, operation="deterministic-json-fence", raw_output=extracted)

    async def repair(
        self,
        context: dict[str, Any],
        result: RecoveryValidationResult,
    ) -> RawCandidate:
        _require_recovery_artifact(context, self.stage, {"subtask_plan"})
        revision_context = _revision_context(context, result)
        method = getattr(self.model, "agent3_revise_result", None)
        if method is not None:
            return await method(revision_context, result.candidate or {})
        return await _candidate_from_structured_call(
            self.model.agent3_revise(revision_context, result.candidate or {})
        )

    async def clear_working_candidate(self, project_id: str) -> None:
        self.storage.clear_working_draft(project_id, self.stage)


class Agent4RecoveryPolicy(_StagePolicy):
    stage = 5
    agent_role = "agent4"
    can_regenerate = True

    def __init__(
        self,
        storage: ProjectStorage,
        model: AgentModel,
        verifier: Agent4CandidateVerifier,
        existing: dict[str, Any],
        *,
        role_context: Callable[[dict[str, Any], str], dict[str, Any]],
        repair_target: Callable[[dict[str, Any]], str | None],
        execution_issues: Callable[[dict[str, Any]], list[str]],
    ) -> None:
        super().__init__(storage)
        self.model = model
        self.verifier = verifier
        self.existing = existing
        self.role_context = role_context
        self.repair_target = repair_target
        self.execution_issues = execution_issues
        self._first_generation = True
        self._pending_execution: dict[str, Any] | None = None

    async def generate_fresh(self, context: dict[str, Any]) -> RawCandidate:
        first = self._first_generation
        self._first_generation = False
        failure = context.get("deterministic_failure")
        matches = _matches_format_contract(self.existing, context)
        if first and isinstance(failure, dict) and matches:
            self._pending_execution = failure
            return candidate_result(self.existing, operation="resume")
        instruction = context.get("user_instruction")
        if first and isinstance(instruction, str) and instruction and matches:
            updated = await self._apply_instruction(context, instruction)
            return candidate_result(updated, operation="instruction")
        generator, validator = await asyncio.gather(
            self.model.agent4_generate_generator(self.role_context(context, "generator"), {}),
            self.model.agent4_generate_validator(self.role_context(context, "validator"), {}),
        )
        value = {
            "format_contract_id": context["input_format_contract"]["format_contract_id"],
            "generator_code": generator,
            "validator_code": validator,
            "issues": [],
        }
        return candidate_result(value, operation="generate-code")

    async def validate(
        self,
        candidate: RawCandidate,
        context: dict[str, Any],
    ) -> RecoveryValidationResult:
        contract_failure = _response_contract_failure(candidate)
        if contract_failure is not None:
            return contract_failure
        assert candidate.candidate is not None
        try:
            current = CodeDraft.model_validate(candidate.candidate).model_dump(mode="json")
        except ValidationError as exc:
            errors = [recovery_error(item, default_source="pydantic") for item in exc.errors()]
            locations = {str(item.location[0]) for item in errors if item.location}
            target = {
                "generator_code": "generator.cpp",
                "validator_code": "validator.cpp",
            }.get(next(iter(locations))) if len(locations) == 1 else "both"
            return validation_failure(
                RecoveryFailureClass.RESPONSE_CONTRACT,
                repairable=True,
                candidate=candidate.candidate,
                raw_output=candidate.raw_output,
                errors=errors,
                diagnostics={"target_file": target},
                response_metadata=candidate.response_metadata,
            )
        current = self._save(context, current)
        audit_context = self.role_context(context, "generator")
        audit_context.pop("library_context", None)
        audit_context.pop("library_document_manifest", None)
        audit = await self.model.agent4_audit_generator(
            audit_context,
            current["generator_code"],
        )
        if not audit.passed:
            execution = {
                "ok": False,
                "message": "generator.cpp 未通过构造规格语义审计。",
                "failure_category": "semantic_analysis",
                "validation_level": "semantic",
                "checks": [
                    {
                        "operation": "generator_analysis_audit",
                        "role": "generator",
                        "target_file": "generator.cpp",
                        "ok": False,
                        "issues": audit.issues,
                    }
                ],
            }
            return validation_failure(
                RecoveryFailureClass.DETERMINISTIC_EXECUTION,
                repairable=True,
                candidate=current,
                raw_output=candidate.raw_output,
                errors=[
                    RecoveryError(
                        source="validator",
                        location=["generator.cpp"],
                        message=issue,
                        code="generator_analysis_audit_failed",
                    )
                    for issue in audit.issues
                ],
                diagnostics={"execution": execution, "target_file": "generator.cpp"},
                response_metadata=candidate.response_metadata,
            )
        if self._pending_execution is not None:
            execution = self._pending_execution
            self._pending_execution = None
        else:
            current, execution = await self.verifier.verify(
                context["project_id"],
                current,
                context,
            )
        if execution.get("ok"):
            return validation_success(current, candidate, diagnostics={"execution": execution})
        issues = self.execution_issues(execution)
        target = self.repair_target(execution)
        failure_class = (
            RecoveryFailureClass.DETERMINISTIC_EXECUTION
            if target is not None
            else RecoveryFailureClass.AUTHORIZATION
        )
        source = "compiler" if execution.get("validation_level") == "compile" else "runner"
        return validation_failure(
            failure_class,
            repairable=target is not None,
            candidate=current,
            raw_output=candidate.raw_output,
            errors=[
                RecoveryError(source=source, location=[], message=issue, code="execution_failed")
                for issue in issues
            ],
            diagnostics={"execution": execution, "target_file": target},
            response_metadata=candidate.response_metadata,
        )

    async def deterministic_fix(
        self,
        result: RecoveryValidationResult,
        context: dict[str, Any],
    ) -> RawCandidate | None:
        allowed_fields = _granted_paths(
            context,
            self.stage,
            {"generator.cpp", "validator.cpp", "working_code_template"},
        )
        candidate = result.candidate
        if not isinstance(candidate, dict):
            return None
        updated = dict(candidate)
        changed = False
        for field in ("generator_code", "validator_code"):
            if allowed_fields is not None and field not in allowed_fields:
                continue
            source = updated.get(field)
            if not isinstance(source, str):
                continue
            extracted = _single_fenced_body(source, {"cpp", "c++", "cc"})
            if extracted is not None:
                updated[field] = extracted
                changed = True
        if not changed:
            return None
        updated["revision_id"] = None
        updated["issues"] = []
        return candidate_result(
            self._save(context, updated),
            operation="deterministic-code-fence",
        )

    async def repair(
        self,
        context: dict[str, Any],
        result: RecoveryValidationResult,
    ) -> RawCandidate:
        candidate = result.candidate or {}
        if result.failure_class == RecoveryFailureClass.RESPONSE_CONTRACT:
            target = result.diagnostics.get("target_file")
            artifacts = {
                "generator.cpp" if target == "generator.cpp" else "",
                "validator.cpp" if target == "validator.cpp" else "",
                "working_code_template" if target == "both" else "",
            }
            _require_recovery_artifact(context, self.stage, artifacts - {""})
            return await self._repair_response_contract(context, candidate, result)
        execution = result.diagnostics.get("execution")
        target = result.diagnostics.get("target_file")
        assert isinstance(execution, dict)
        assert target in {"generator.cpp", "validator.cpp"}
        _require_recovery_artifact(context, self.stage, {target})
        if target == "generator.cpp":
            replacement = await self.model.agent4_repair_generator(
                self.role_context(context, "generator"), candidate, execution
            )
            field = "generator_code"
        else:
            replacement = await self.model.agent4_repair_validator(
                self.role_context(context, "validator"), candidate, execution
            )
            field = "validator_code"
        updated = {**candidate, field: replacement, "revision_id": None, "issues": []}
        return candidate_result(updated, operation=f"repair-{target}")

    async def clear_working_candidate(self, project_id: str) -> None:
        self.storage.clear_working_draft(project_id, self.stage)

    async def _apply_instruction(
        self,
        context: dict[str, Any],
        instruction: str,
    ) -> dict[str, Any]:
        target = context.get("user_instruction_target")
        updated = dict(self.existing)
        if target == "both":
            generator, validator = await asyncio.gather(
                self.model.agent4_apply_generator_instruction(
                    self.role_context(context, "generator"), self.existing, instruction
                ),
                self.model.agent4_apply_validator_instruction(
                    self.role_context(context, "validator"), self.existing, instruction
                ),
            )
            updated.update(generator_code=generator, validator_code=validator)
        elif target == "generator":
            updated["generator_code"] = await self.model.agent4_apply_generator_instruction(
                self.role_context(context, "generator"), self.existing, instruction
            )
        elif target == "validator":
            updated["validator_code"] = await self.model.agent4_apply_validator_instruction(
                self.role_context(context, "validator"), self.existing, instruction
            )
        updated.update(revision_id=None, issues=[])
        return updated

    async def _repair_response_contract(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        result: RecoveryValidationResult,
    ) -> RawCandidate:
        target = result.diagnostics.get("target_file")
        updated = dict(candidate)
        calls: list[Awaitable[str]] = []
        fields: list[str] = []
        if target in {"generator.cpp", "both"}:
            calls.append(
                self.model.agent4_generate_generator(
                    self.role_context(context, "generator"), candidate
                )
            )
            fields.append("generator_code")
        if target in {"validator.cpp", "both"}:
            calls.append(
                self.model.agent4_generate_validator(
                    self.role_context(context, "validator"), candidate
                )
            )
            fields.append("validator_code")
        replacements = await asyncio.gather(*calls)
        updated.update(zip(fields, replacements, strict=True))
        updated.update(revision_id=None, issues=[])
        return candidate_result(updated, operation="repair-code-contract")

    def _save(self, context: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        saved = self.storage.save_code_draft(
            context["project_id"],
            CodeDraft.model_validate(
                {
                    **candidate,
                    "input_revision": context.get("input_revision"),
                    "subtasks_revision": context.get("subtasks_revision"),
                }
            ),
        )
        return saved.model_dump(mode="json")


def _normalize_runtime_parameter_schema(
    candidate: dict[str, Any] | None,
) -> RawCandidate | None:
    if not isinstance(candidate, dict):
        return None
    updated = deepcopy(candidate)
    subtasks = updated.get("subtasks")
    if not isinstance(subtasks, list):
        return None
    changed = False
    for subtask in subtasks:
        if not isinstance(subtask, dict):
            return None
        cases = subtask.get("runtime_parameters")
        if not isinstance(cases, list) or not cases:
            continue
        exemplars: dict[str, dict[str, Any]] = {}
        signatures: dict[str, tuple[Any, Any]] = {}
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("parameters"), list):
                return None
            for parameter in case["parameters"]:
                if not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str):
                    return None
                name = parameter["name"]
                signature = (parameter.get("category"), type(parameter.get("value")))
                if name in signatures and signatures[name] != signature:
                    return None
                signatures[name] = signature
                exemplars.setdefault(name, parameter)
        for case in cases:
            parameters = {
                parameter["name"]: parameter for parameter in case["parameters"]
            }
            for name, exemplar in exemplars.items():
                if name in parameters:
                    continue
                added = deepcopy(exemplar)
                if name == "case_variant" and isinstance(case.get("case_id"), int):
                    added["value"] = case["case_id"]
                case["parameters"].append(added)
                changed = True

        fixed_seen: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        for case in cases:
            parameters = {
                parameter["name"]: parameter for parameter in case["parameters"]
            }
            mode = parameters.get("construction_mode", {}).get("value")
            if mode != "fixed":
                continue
            signature = (
                str(case.get("generation_profile_id") or ""),
                tuple(
                    sorted(
                        (name, str(parameter.get("value")))
                        for name, parameter in parameters.items()
                    )
                ),
            )
            if signature not in fixed_seen:
                fixed_seen[signature] = case
                continue
            case_variant = parameters.get("case_variant")
            if case_variant is None or not isinstance(case.get("case_id"), int):
                continue
            if case_variant.get("value") != case["case_id"]:
                case_variant["value"] = case["case_id"]
                changed = True
    if not changed:
        return None
    try:
        validated = SubtaskPlanDraft.model_validate(updated)
    except ValidationError:
        return None
    return candidate_result(
        validated.model_dump(mode="json", exclude={"issues"}),
        operation="deterministic-runtime-schema",
    )


def _normalize_generation_profile_accounting(
    candidate: dict[str, Any] | None,
) -> RawCandidate | None:
    """Repair only mechanically inconsistent stage-4 profile bookkeeping."""
    if not isinstance(candidate, dict):
        return None
    updated = deepcopy(candidate)
    subtasks = updated.get("subtasks")
    if not isinstance(subtasks, list):
        return None

    for subtask in subtasks:
        if not isinstance(subtask, dict):
            return None
        profiles = subtask.get("generation_profiles")
        cases = subtask.get("runtime_parameters")
        if not isinstance(profiles, list) or not isinstance(cases, list) or len(cases) < 3:
            return None

        profiles_by_id: dict[str, dict[str, Any]] = {}
        for profile in profiles:
            if not isinstance(profile, dict) or not isinstance(profile.get("id"), str):
                return None
            profile_id = profile["id"]
            if profile_id in profiles_by_id:
                return None
            profiles_by_id[profile_id] = profile

        assignments = {profile_id: 0 for profile_id in profiles_by_id}
        for case_id, case in enumerate(cases, start=1):
            if not isinstance(case, dict) or not isinstance(case.get("generation_profile_id"), str):
                return None
            profile_id = case["generation_profile_id"]
            if profile_id not in assignments:
                return None
            assignments[profile_id] += 1
            case["case_id"] = case_id

        if any(count == 0 for count in assignments.values()):
            return None
        subtask["test_count"] = len(cases)
        for profile_id, profile in profiles_by_id.items():
            profile["count"] = assignments[profile_id]

    if updated == candidate:
        return None
    try:
        validated = SubtaskPlanDraft.model_validate(updated)
    except ValidationError:
        return None
    return candidate_result(
        validated.model_dump(mode="json", exclude={"issues"}),
        operation="deterministic-profile-accounting",
    )


def _response_contract_failure(candidate: RawCandidate) -> RecoveryValidationResult | None:
    if candidate.candidate is not None and not candidate.validation_errors:
        return None
    errors = candidate.validation_errors or [
        RecoveryError(
            source="json",
            location=[],
            message="模型输出无法解析为响应契约要求的对象。",
            code="invalid_response_contract",
        )
    ]
    return validation_failure(
        RecoveryFailureClass.RESPONSE_CONTRACT,
        repairable=True,
        candidate=candidate.candidate,
        raw_output=candidate.raw_output,
        errors=errors,
        response_metadata=candidate.response_metadata,
    )


def _business_failure(
    raw: RawCandidate,
    candidate: dict[str, Any],
    issues: list[str],
) -> RecoveryValidationResult:
    return validation_failure(
        RecoveryFailureClass.BUSINESS_CONTRACT,
        repairable=True,
        candidate=candidate,
        raw_output=raw.raw_output,
        errors=[recovery_error(issue) for issue in issues],
        response_metadata=raw.response_metadata,
    )


def _revision_context(
    context: dict[str, Any],
    result: RecoveryValidationResult,
) -> dict[str, Any]:
    errors = [
        {
            **item.model_dump(mode="json"),
            "type": item.code,
        }
        for item in result.errors
    ]
    return {
        **context,
        "raw_output": result.raw_output,
        "validation_errors": errors,
        "validation_issues": [item.message for item in result.errors],
    }


def _single_fenced_body(value: str, languages: set[str]) -> str | None:
    match = re.fullmatch(r"\s*```([^\n\r]*)\r?\n([\s\S]*?)\r?\n```\s*", value)
    if match is None:
        return None
    language = match.group(1).strip().lower()
    if language not in languages:
        return None
    body = match.group(2).strip("\r\n")
    return body if body.strip() else None


def _matches_format_contract(candidate: dict[str, Any], context: dict[str, Any]) -> bool:
    required = {"format_contract_id", "generator_code", "validator_code"}
    return required.issubset(candidate) and candidate.get("format_contract_id") == context[
        "input_format_contract"
    ]["format_contract_id"]


async def _candidate_from_structured_call(call: Awaitable[Any]) -> RawCandidate:
    try:
        value = await call
    except AppError as exc:
        recovered = candidate_result_from_error(exc)
        if recovered is None:
            raise
        return recovered
    return candidate_result(value.model_dump(mode="json", exclude={"issues"}))


def _require_recovery_artifact(
    context: dict[str, Any],
    stage: int,
    artifacts: set[str],
) -> None:
    if _granted_paths(context, stage, artifacts) is None:
        return


def _granted_paths(
    context: dict[str, Any],
    stage: int,
    artifacts: set[str],
) -> set[str] | None:
    plan = context.get("recovery_plan")
    if not isinstance(plan, dict):
        return None
    grants = plan.get("write_grants")
    if not isinstance(grants, list):
        grants = []
    matched = [
        grant
        for grant in grants
        if isinstance(grant, dict)
        and grant.get("stage") == stage
        and grant.get("artifact") in artifacts
    ]
    if not matched or plan.get("requires_user_authorization"):
        raise AppError(
            "RECOVERY_SCOPE_DENIED",
            "问题定位器未授权当前修复器修改该工件。",
            stage=stage,
            status_code=409,
            details={"requested_artifacts": sorted(artifacts), "recovery_plan": plan},
        )
    return {
        str(path)
        for grant in matched
        for path in grant.get("paths", [])
        if isinstance(path, str)
    }
