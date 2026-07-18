from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from app.errors import AppError
from app.models import (
    RawCandidate,
    RecoveryError,
    RecoveryFailureClass,
    RecoveryOutcome,
    RecoveryPlan,
    RecoveryValidationResult,
    StageRecoverySummary,
)
from app.services.recovery_diagnostics import (
    RecoveryContextAssembler,
    RecoveryProblemLocator,
)
from app.storage import ProjectStorage

MAX_GENERATION_ROUNDS = 3
MAX_REPAIR_ATTEMPTS = 5

# Backwards-compatible name used by the model boundary. It is now the same
# stable raw-candidate model consumed by every stage policy.
AgentCandidateResult = RawCandidate


class RecoveryPolicy(Protocol):
    stage: int
    agent_role: str
    max_generation_rounds: int
    max_repair_attempts: int
    can_regenerate: bool

    async def generate_fresh(self, context: dict[str, Any]) -> RawCandidate: ...

    async def validate(
        self,
        candidate: RawCandidate,
        context: dict[str, Any],
    ) -> RecoveryValidationResult: ...

    async def deterministic_fix(
        self,
        result: RecoveryValidationResult,
        context: dict[str, Any],
    ) -> RawCandidate | None: ...

    async def repair(
        self,
        context: dict[str, Any],
        result: RecoveryValidationResult,
    ) -> RawCandidate: ...

    async def persist_verified(
        self,
        project_id: str,
        candidate: dict[str, Any],
    ) -> None: ...

    async def clear_working_candidate(self, project_id: str) -> None: ...


class AgentRecoveryCoordinator:
    """Execute one bounded, auditable recovery policy."""

    def __init__(
        self,
        storage: ProjectStorage,
        *,
        locator: RecoveryProblemLocator | None = None,
        context_assembler: RecoveryContextAssembler | None = None,
    ) -> None:
        self.storage = storage
        self.locator = locator or RecoveryProblemLocator()
        self.context_assembler = context_assembler or RecoveryContextAssembler()

    async def run(
        self,
        project_id: str,
        *,
        policy: RecoveryPolicy,
        context: dict[str, Any],
        resume_summary: StageRecoverySummary | None = None,
    ) -> RecoveryOutcome:
        summary = (
            self.storage.resume_recovery_run(
                project_id,
                resume_summary,
                agent_role=policy.agent_role,
            )
            if resume_summary is not None
            else self.storage.start_recovery_run(
                project_id,
                stage=policy.stage,
                agent_role=policy.agent_role,
            )
        )
        total_repairs = summary.repair_attempts
        start_round = max(1, summary.generation_round)
        rounds = policy.max_generation_rounds if policy.can_regenerate else 1
        last_result: RecoveryValidationResult | None = None
        last_plan: RecoveryPlan | None = None
        deterministic_fixes: set[str] = set()
        try:
            for generation_round in range(start_round, rounds + 1):
                summary = summary.model_copy(update={"generation_round": generation_round})
                operation_context = {
                    **context,
                    "recovery_run_id": summary.run_id,
                    "generation_round": generation_round,
                }
                current = await policy.generate_fresh(operation_context)
                self._record_candidate(
                    project_id,
                    summary,
                    agent_role=policy.agent_role,
                    operation="generate",
                    generation_round=generation_round,
                    repair_attempt=0,
                    candidate=current,
                )

                repairs_this_round = total_repairs if resume_summary is not None else 0
                while True:
                    last_result = await policy.validate(current, operation_context)
                    self._record_validation(
                        project_id,
                        summary,
                        generation_round=generation_round,
                        repair_attempt=repairs_this_round,
                        result=last_result,
                    )
                    if last_result.passed:
                        assert last_result.candidate is not None
                        await policy.persist_verified(project_id, last_result.candidate)
                        passed = summary.model_copy(
                            update={
                                "status": "passed",
                                "repair_attempts": total_repairs,
                                "last_error_summary": "",
                            }
                        )
                        finished = self.storage.finish_recovery_run(project_id, passed)
                        return RecoveryOutcome(
                            status="passed",
                            candidate=last_result.candidate,
                            summary=finished,
                            validation=last_result,
                            recovery_plan=last_plan,
                        )

                    last_plan = self.locator.locate(
                        policy.stage,
                        last_result,
                        operation_context,
                    )
                    self._record_plan(
                        project_id,
                        summary,
                        generation_round=generation_round,
                        repair_attempt=repairs_this_round,
                        plan=last_plan,
                    )
                    repair_context = self.context_assembler.build(
                        operation_context,
                        last_result,
                        last_plan,
                    )
                    if not last_plan.allows_stage(policy.stage):
                        routed = last_result.model_copy(
                            update={
                                "repairable": False,
                                "diagnostics": {
                                    **last_result.diagnostics,
                                    "recovery_plan": last_plan.model_dump(mode="json"),
                                },
                            }
                        )
                        failed = self._finish_failure(
                            project_id,
                            summary,
                            total_repairs,
                            routed,
                        )
                        return RecoveryOutcome(
                            status="failed",
                            candidate=routed.candidate,
                            summary=failed,
                            validation=routed,
                            recovery_plan=last_plan,
                        )

                    if not last_result.repairable:
                        failed = self._finish_failure(
                            project_id,
                            summary,
                            total_repairs,
                            last_result,
                        )
                        return RecoveryOutcome(
                            status="failed",
                            candidate=last_result.candidate,
                            summary=failed,
                            validation=last_result,
                            recovery_plan=last_plan,
                        )

                    fixed = await policy.deterministic_fix(last_result, repair_context)
                    if fixed is not None:
                        signature = _candidate_signature(fixed)
                        current_signature = _candidate_signature(current)
                        if signature not in deterministic_fixes and signature != current_signature:
                            deterministic_fixes.add(signature)
                            current = fixed
                            self._record_candidate(
                                project_id,
                                summary,
                                agent_role=policy.agent_role,
                                operation="deterministic-fix",
                                generation_round=generation_round,
                                repair_attempt=repairs_this_round,
                                candidate=current,
                            )
                            continue

                    if repairs_this_round >= policy.max_repair_attempts:
                        self._record_validation(
                            project_id,
                            summary,
                            generation_round=generation_round,
                            repair_attempt=repairs_this_round,
                            result=last_result,
                            operation="exhausted",
                        )
                        if policy.can_regenerate:
                            await policy.clear_working_candidate(project_id)
                        break

                    current = await policy.repair(repair_context, last_result)
                    repairs_this_round += 1
                    total_repairs += 1
                    self._record_candidate(
                        project_id,
                        summary,
                        agent_role=policy.agent_role,
                        operation="repair",
                        generation_round=generation_round,
                        repair_attempt=repairs_this_round,
                        candidate=current,
                    )

                resume_summary = None
                if not policy.can_regenerate:
                    break

            failed = self._finish_failure(
                project_id,
                summary,
                total_repairs,
                last_result,
            )
            return RecoveryOutcome(
                status="exhausted",
                candidate=last_result.candidate if last_result is not None else None,
                summary=failed,
                validation=last_result,
                recovery_plan=last_plan,
            )
        except AppError as exc:
            environment = validation_failure(
                RecoveryFailureClass.ENVIRONMENT,
                repairable=False,
                candidate=None,
                raw_output="",
                errors=[
                    RecoveryError(
                        source="system",
                        location=[],
                        message=exc.message,
                        code=exc.code,
                    )
                ],
                diagnostics={"app_error": exc.payload()},
            )
            self._record_validation(
                project_id,
                summary,
                generation_round=max(1, summary.generation_round),
                repair_attempt=total_repairs,
                result=environment,
                operation="environment-stop",
            )
            finished = self._finish_failure(
                project_id,
                summary,
                total_repairs,
                environment,
            )
            details = dict(exc.details) if isinstance(exc.details, dict) else {}
            details.setdefault("recovery_run_id", finished.run_id)
            details.setdefault("failure_class", RecoveryFailureClass.ENVIRONMENT)
            details.setdefault("generation_round", finished.generation_round)
            details.setdefault("repair_attempts", finished.repair_attempts)
            exc.details = details
            raise

    def _finish_failure(
        self,
        project_id: str,
        summary: StageRecoverySummary,
        total_repairs: int,
        result: RecoveryValidationResult | None,
    ) -> StageRecoverySummary:
        failed = summary.model_copy(
            update={
                "status": "failed",
                "repair_attempts": total_repairs,
                "last_error_summary": _error_summary(result.errors if result else []),
            }
        )
        return self.storage.finish_recovery_run(project_id, failed)

    def _record_candidate(
        self,
        project_id: str,
        summary: StageRecoverySummary,
        *,
        agent_role: str,
        operation: str,
        generation_round: int,
        repair_attempt: int,
        candidate: RawCandidate,
    ) -> None:
        self.storage.append_recovery_event(
            project_id,
            summary.run_id,
            {
                "stage": summary.stage,
                "agent_role": agent_role,
                "operation": operation,
                "generation_round": generation_round,
                "repair_attempt": repair_attempt,
                "raw_output": candidate.raw_output,
                "candidate": candidate.candidate,
                "validation_errors": [
                    item.model_dump(mode="json") for item in candidate.validation_errors
                ],
                "response_metadata": candidate.response_metadata,
                "status": "candidate",
            },
        )

    def _record_validation(
        self,
        project_id: str,
        summary: StageRecoverySummary,
        *,
        generation_round: int,
        repair_attempt: int,
        result: RecoveryValidationResult,
        operation: str = "validate",
    ) -> None:
        self.storage.append_recovery_event(
            project_id,
            summary.run_id,
            {
                "stage": summary.stage,
                "operation": operation,
                "generation_round": generation_round,
                "repair_attempt": repair_attempt,
                "raw_output": result.raw_output,
                "candidate": result.candidate,
                "validation_errors": [item.model_dump(mode="json") for item in result.errors],
                "failure_class": result.failure_class,
                "repairable": result.repairable,
                "diagnostics": result.diagnostics,
                "response_metadata": result.response_metadata,
                "status": "passed" if result.passed else "failed",
            },
        )

    def _record_plan(
        self,
        project_id: str,
        summary: StageRecoverySummary,
        *,
        generation_round: int,
        repair_attempt: int,
        plan: RecoveryPlan,
    ) -> None:
        self.storage.append_recovery_event(
            project_id,
            summary.run_id,
            {
                "stage": summary.stage,
                "operation": "locate",
                "generation_round": generation_round,
                "repair_attempt": repair_attempt,
                "raw_output": "",
                "candidate": None,
                "validation_errors": [],
                "recovery_plan": plan.model_dump(mode="json"),
                "status": "routed",
            },
        )


def candidate_result(
    candidate: dict[str, Any],
    *,
    operation: str = "",
    raw_output: str | None = None,
    response_metadata: dict[str, Any] | None = None,
) -> RawCandidate:
    return RawCandidate(
        operation=operation,
        candidate=candidate,
        raw_output=raw_output or json.dumps(candidate, ensure_ascii=False),
        response_metadata=response_metadata or {},
    )


def candidate_result_from_error(error: AppError) -> RawCandidate | None:
    details = error.details if isinstance(error.details, dict) else {}
    failure_kind = details.get("failure_kind")
    if error.code not in {"MODEL_FAILED", "MODEL_RESPONSE_TRUNCATED"} or failure_kind in {
        "transport",
        "response_envelope",
    }:
        return None
    errors = details.get("validation_errors")
    return RawCandidate(
        operation=str(details.get("operation") or ""),
        candidate=(
            details.get("candidate")
            if isinstance(details.get("candidate"), dict)
            else None
        ),
        raw_output=str(details.get("raw_output") or ""),
        validation_errors=[
            recovery_error(item, default_source="pydantic")
            for item in errors if isinstance(item, dict)
        ]
        if isinstance(errors, list)
        else [],
        response_metadata=(
            details.get("response_metadata")
            if isinstance(details.get("response_metadata"), dict)
            else {}
        ),
    )


def validation_success(
    candidate: dict[str, Any],
    raw: RawCandidate,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> RecoveryValidationResult:
    return RecoveryValidationResult(
        passed=True,
        candidate=candidate,
        raw_output=raw.raw_output,
        diagnostics=diagnostics or {},
        response_metadata=raw.response_metadata,
    )


def validation_failure(
    failure_class: RecoveryFailureClass,
    *,
    repairable: bool,
    candidate: dict[str, Any] | None,
    raw_output: str,
    errors: list[RecoveryError],
    diagnostics: dict[str, Any] | None = None,
    response_metadata: dict[str, Any] | None = None,
) -> RecoveryValidationResult:
    return RecoveryValidationResult(
        passed=False,
        failure_class=failure_class,
        repairable=repairable,
        candidate=candidate,
        raw_output=raw_output,
        errors=errors,
        diagnostics=diagnostics or {},
        response_metadata=response_metadata or {},
    )


def recovery_error(
    value: dict[str, Any] | str,
    *,
    default_source: str = "validator",
    default_code: str = "validation_failed",
) -> RecoveryError:
    if isinstance(value, str):
        return RecoveryError(
            source=default_source,
            location=[],
            message=value or "验证失败。",
            code=default_code,
        )
    raw_location = value.get("location", value.get("loc", []))
    location = list(raw_location) if isinstance(raw_location, (list, tuple)) else []
    error_type = str(value.get("type") or value.get("code") or default_code)
    source = "json" if "json" in error_type else default_source
    return RecoveryError(
        source=source,
        location=location,
        message=str(value.get("message") or value.get("msg") or "验证失败。"),
        code=error_type,
    )


def _candidate_signature(candidate: RawCandidate) -> str:
    payload = json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _error_summary(errors: list[RecoveryError]) -> str:
    return "；".join(item.message for item in errors if item.message)[:1000]
