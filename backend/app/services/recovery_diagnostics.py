from __future__ import annotations

from collections import Counter
from typing import Any

from app.models import (
    RecoveryFailureClass,
    RecoveryMutationGrant,
    RecoveryPlan,
    RecoveryValidationResult,
)

REQUIRED_PROFILE_CATEGORIES = {
    "rules_format",
    "anti_algorithm",
    "boundary_edge",
}


class RecoveryProblemLocator:
    """Locate the owning stage and produce a minimal mutation grant."""

    def locate(
        self,
        observed_stage: int,
        result: RecoveryValidationResult,
        context: dict[str, Any],
    ) -> RecoveryPlan:
        failure_class = result.failure_class or RecoveryFailureClass.ENVIRONMENT
        evidence = [item.message for item in result.errors]
        if observed_stage == 1:
            return self._plan(
                observed_stage,
                1,
                failure_class,
                evidence,
                ["authoritative_input", "previous_candidate", "validation_errors"],
                RecoveryMutationGrant(
                    stage=1,
                    artifact="input_normalization",
                    paths=[
                        "problem.input_description",
                        "problem.output_description",
                        "problem.samples",
                    ],
                ),
                ["problem.description", "problem.difficulty", "solution.source"],
            )
        if observed_stage == 2:
            return self._plan(
                observed_stage,
                2,
                failure_class,
                evidence,
                ["problem_input_output", "solution_source", "compiler_diagnostics"],
                RecoveryMutationGrant(
                    stage=2,
                    artifact="solution.cpp",
                    paths=["solution.source"],
                ),
                ["problem.description", "problem.difficulty"],
            )
        if observed_stage == 3:
            return self._plan(
                observed_stage,
                3,
                failure_class,
                evidence,
                ["problem_input_output", "previous_markdown", "validation_errors"],
                RecoveryMutationGrant(
                    stage=3,
                    artifact="test_data_plan",
                    paths=["plan_markdown"],
                ),
                ["problem.description", "solution.source"],
            )
        if observed_stage == 4:
            return self._plan(
                observed_stage,
                4,
                failure_class,
                evidence,
                [
                    "confirmed_stage3_plan",
                    "previous_stage4_candidate",
                    "validation_errors",
                    "runtime_schema_diffs",
                    "generation_profile_diffs",
                ],
                RecoveryMutationGrant(
                    stage=4,
                    artifact="subtask_plan",
                    paths=[
                        "subtasks[*].generation_profiles",
                        "subtasks[*].runtime_parameters",
                    ],
                ),
                ["problem.description", "solution.source", "test_data_plan.plan_markdown"],
            )
        if observed_stage == 5:
            return self._locate_stage5(result, context, evidence)
        return RecoveryPlan(
            observed_stage=observed_stage,
            root_stage=observed_stage,
            failure_class=RecoveryFailureClass.AUTHORIZATION,
            evidence=evidence or ["当前错误没有可授权的 AI 工件。"],
            context_requirements=["validation_errors"],
            write_grants=[],
            protected_fields=["problem.description", "solution.source", "released_code"],
            revalidate_from_stage=observed_stage,
            invalidate_downstream_from_stage=None,
            requires_user_authorization=True,
            confidence=1,
        )

    def _locate_stage5(
        self,
        result: RecoveryValidationResult,
        context: dict[str, Any],
        evidence: list[str],
    ) -> RecoveryPlan:
        del context
        execution = result.diagnostics.get("execution")
        failed_checks = _failed_checks(execution)
        if any(
            check.get("role") == "solution" or check.get("operation") == "solve"
            for check in failed_checks
        ):
            return RecoveryPlan(
                observed_stage=5,
                root_stage=2,
                failure_class=RecoveryFailureClass.AUTHORIZATION,
                evidence=evidence,
                context_requirements=[
                    "problem_input_output",
                    "solution_source",
                    "failing_input",
                    "solution_execution",
                ],
                write_grants=[],
                protected_fields=["solution.source"],
                revalidate_from_stage=2,
                invalidate_downstream_from_stage=2,
                requires_user_authorization=True,
                confidence=1,
            )
        if any(
            check.get("operation") in {"runtime_parameters", "generation_plan"}
            for check in failed_checks
        ) or execution.get("failure_category") == "generation_plan":
            return self._plan(
                5,
                4,
                RecoveryFailureClass.BUSINESS_CONTRACT,
                evidence,
                [
                    "confirmed_stage3_plan",
                    "current_stage4_plan",
                    "validation_errors",
                    "runtime_schema_diffs",
                ],
                RecoveryMutationGrant(
                    stage=4,
                    artifact="subtask_plan",
                    paths=[
                        "subtasks[*].generation_profiles",
                        "subtasks[*].runtime_parameters",
                    ],
                ),
                ["problem.description", "solution.source", "released_code"],
            )
        target = result.diagnostics.get("target_file")
        if target == "generator.cpp":
            paths = ["generator_code"]
            artifact = "generator.cpp"
        elif target == "validator.cpp":
            paths = ["validator_code"]
            artifact = "validator.cpp"
        elif target == "both":
            paths = ["generator_code", "validator_code"]
            artifact = "working_code_template"
        else:
            return RecoveryPlan(
                observed_stage=5,
                root_stage=5,
                failure_class=RecoveryFailureClass.AUTHORIZATION,
                evidence=evidence,
                context_requirements=["deterministic_execution", "current_code_template"],
                write_grants=[],
                protected_fields=["solution.source", "released_code"],
                revalidate_from_stage=5,
                invalidate_downstream_from_stage=None,
                requires_user_authorization=True,
                confidence=0.7,
            )
        return self._plan(
            5,
            5,
            result.failure_class or RecoveryFailureClass.DETERMINISTIC_EXECUTION,
            evidence,
            [
                "current_code_template",
                "owned_role_documentation",
                "deterministic_execution",
                "relevant_subtask_case_parameters",
            ],
            RecoveryMutationGrant(stage=5, artifact=artifact, paths=paths),
            ["problem.description", "solution.source", "released_code"],
        )

    @staticmethod
    def _plan(
        observed_stage: int,
        root_stage: int,
        failure_class: RecoveryFailureClass,
        evidence: list[str],
        context_requirements: list[str],
        grant: RecoveryMutationGrant,
        protected_fields: list[str],
    ) -> RecoveryPlan:
        return RecoveryPlan(
            observed_stage=observed_stage,
            root_stage=root_stage,
            failure_class=failure_class,
            evidence=evidence,
            context_requirements=context_requirements,
            write_grants=[grant],
            protected_fields=protected_fields,
            revalidate_from_stage=root_stage,
            invalidate_downstream_from_stage=root_stage if root_stage < 5 else None,
            requires_user_authorization=False,
            confidence=1,
        )


class RecoveryContextAssembler:
    """Inject only deterministic evidence selected by the recovery plan."""

    def build(
        self,
        context: dict[str, Any],
        result: RecoveryValidationResult,
        plan: RecoveryPlan,
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "validation_errors": [item.model_dump(mode="json") for item in result.errors],
        }
        candidate = result.candidate
        plan_candidate = candidate
        if not isinstance(candidate, dict) or not isinstance(candidate.get("subtasks"), list):
            subtasks = context.get("subtasks")
            plan_candidate = {"subtasks": subtasks} if isinstance(subtasks, list) else None
        if "runtime_schema_diffs" in plan.context_requirements:
            evidence["runtime_schema_diffs"] = runtime_schema_diffs(plan_candidate)
        if "generation_profile_diffs" in plan.context_requirements:
            evidence["generation_profile_diffs"] = generation_profile_diffs(plan_candidate)
        if "deterministic_execution" in plan.context_requirements:
            evidence["deterministic_execution"] = result.diagnostics.get("execution", {})
        return {
            **context,
            "recovery_plan": plan.model_dump(mode="json"),
            "recovery_evidence": evidence,
        }


def runtime_schema_diffs(candidate: dict[str, Any] | None) -> list[dict[str, Any]]:
    subtasks = candidate.get("subtasks") if isinstance(candidate, dict) else None
    if not isinstance(subtasks, list):
        return []
    diffs: list[dict[str, Any]] = []
    for subtask in subtasks:
        if not isinstance(subtask, dict):
            continue
        raw_cases = subtask.get("runtime_parameters")
        if not isinstance(raw_cases, list) or not raw_cases:
            continue
        cases = [_case_schema(case) for case in raw_cases if isinstance(case, dict)]
        signatures = [item["signature"] for item in cases]
        if not signatures:
            continue
        expected_signature = Counter(signatures).most_common(1)[0][0]
        expected_case = next(item for item in cases if item["signature"] == expected_signature)
        expected = expected_case["schema"]
        mismatches: list[dict[str, Any]] = []
        for case in cases:
            actual = case["schema"]
            if case["signature"] == expected_signature and not case["duplicates"]:
                continue
            shared = sorted(set(expected).intersection(actual))
            type_mismatches = [
                {
                    "name": name,
                    "expected": expected[name],
                    "actual": actual[name],
                }
                for name in shared
                if expected[name] != actual[name]
            ]
            mismatches.append(
                {
                    "case_id": case["case_id"],
                    "actual_schema": actual,
                    "missing_parameters": sorted(set(expected).difference(actual)),
                    "extra_parameters": sorted(set(actual).difference(expected)),
                    "type_or_category_mismatches": type_mismatches,
                    "duplicate_parameters": case["duplicates"],
                }
            )
        if mismatches:
            diffs.append(
                {
                    "subtask_id": subtask.get("id"),
                    "expected_from_case_id": expected_case["case_id"],
                    "expected_schema": expected,
                    "mismatched_cases": mismatches,
                    "repair_rule": (
                        "Every case in this subtask must use exactly the expected parameter "
                        "names, categories and value types; only values may differ."
                    ),
                }
            )
    return diffs


def generation_profile_diffs(candidate: dict[str, Any] | None) -> list[dict[str, Any]]:
    subtasks = candidate.get("subtasks") if isinstance(candidate, dict) else None
    if not isinstance(subtasks, list):
        return []
    diffs: list[dict[str, Any]] = []
    for subtask in subtasks:
        if not isinstance(subtask, dict):
            continue
        profiles = subtask.get("generation_profiles")
        if not isinstance(profiles, list):
            continue
        observed = {
            str(profile.get("category"))
            for profile in profiles
            if isinstance(profile, dict) and profile.get("category") is not None
        }
        missing = sorted(REQUIRED_PROFILE_CATEGORIES.difference(observed))
        unexpected = sorted(observed.difference(REQUIRED_PROFILE_CATEGORIES))
        expected_count = subtask.get("test_count")
        actual_count = sum(
            int(profile.get("count") or 0)
            for profile in profiles
            if isinstance(profile, dict)
        )
        runtime_parameters = subtask.get("runtime_parameters")
        cases = runtime_parameters if isinstance(runtime_parameters, list) else []
        actual_case_ids = [
            case.get("case_id") for case in cases if isinstance(case, dict)
        ]
        expected_case_ids = (
            list(range(1, expected_count + 1))
            if isinstance(expected_count, int) and expected_count >= 0
            else []
        )
        profile_counts = {
            str(profile.get("id")): int(profile.get("count") or 0)
            for profile in profiles
            if isinstance(profile, dict) and profile.get("id") is not None
        }
        assigned_profile_counts: dict[str, int] = {}
        for case in cases:
            if not isinstance(case, dict) or case.get("generation_profile_id") is None:
                continue
            profile_id = str(case["generation_profile_id"])
            assigned_profile_counts[profile_id] = assigned_profile_counts.get(profile_id, 0) + 1
        accounting_mismatch = (
            len(cases) != expected_count
            or actual_case_ids != expected_case_ids
            or assigned_profile_counts != profile_counts
        )
        if missing or unexpected or actual_count != expected_count or accounting_mismatch:
            diffs.append(
                {
                    "subtask_id": subtask.get("id"),
                    "required_categories": sorted(REQUIRED_PROFILE_CATEGORIES),
                    "observed_categories": sorted(observed),
                    "missing_categories": missing,
                    "unexpected_categories": unexpected,
                    "expected_test_count": expected_count,
                    "profile_count_sum": actual_count,
                    "runtime_case_count": len(cases),
                    "expected_case_ids": expected_case_ids,
                    "actual_case_ids": actual_case_ids,
                    "profile_counts": profile_counts,
                    "assigned_profile_counts": assigned_profile_counts,
                }
            )
    return diffs


def _case_schema(case: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, dict[str, str]] = {}
    names: list[str] = []
    parameters = case.get("parameters")
    for parameter in parameters if isinstance(parameters, list) else []:
        if not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str):
            continue
        name = parameter["name"]
        names.append(name)
        schema[name] = {
            "category": str(parameter.get("category") or ""),
            "value_type": _value_type(parameter.get("value")),
        }
    signature = tuple(
        (name, value["category"], value["value_type"])
        for name, value in sorted(schema.items())
    )
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    return {
        "case_id": case.get("case_id"),
        "schema": schema,
        "signature": signature,
        "duplicates": duplicates,
    }


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _failed_checks(execution: Any) -> list[dict[str, Any]]:
    if not isinstance(execution, dict):
        return []
    failed: list[dict[str, Any]] = []
    for check in execution.get("checks", []):
        if not isinstance(check, dict):
            continue
        result = check.get("result")
        if check.get("ok") is False or (
            isinstance(result, dict) and result.get("ok") is False
        ):
            failed.append(check)
    return failed
