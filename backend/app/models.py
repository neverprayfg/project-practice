from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RuntimeString = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    ),
]
RuntimeScalar = StrictBool | StrictInt | StrictFloat | RuntimeString


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stage(IntEnum):
    CREATE = 1
    SOLUTION_COMPILE = 2
    INPUT_STRUCTURE = 3
    SUBTASK_PLAN = 4
    CODE_DRAFT = 5
    BUILD = 6
    VALIDATE_AND_SOLVE = 7
    EXPORT = 8


class StageStatus(StrEnum):
    DRAFT = "draft"
    CHECKING = "checking"
    WAITING_USER = "waiting_user"
    PASSED = "passed"
    FAILED = "failed"


class TaskType(StrEnum):
    INPUT_NORMALIZATION = "input_normalization"
    INPUT_STRUCTURE = "input_structure"
    SUBTASK_PLAN = "subtask_plan"
    CODE_DRAFT = "code_draft"


class Confirmation(StrEnum):
    PASS = "pass"
    REVISE = "revise"
    ERROR = "error"


class ProjectCreate(StrictModel):
    problem_description: NonEmptyText
    solution_code: NonEmptyText
    difficulty: NonEmptyText


class ModelRuntimeConfiguration(StrictModel):
    base_url: NonEmptyText = "https://api.deepseek.com/v1"
    model_name: NonEmptyText = "deepseek-chat"
    api_key: str = ""
    timeout_seconds: float = Field(default=120.0, ge=5, le=600)
    trial_seeds_per_subtask: int = Field(default=1, ge=1, le=5)

    @model_validator(mode="after")
    def base_url_is_http_endpoint(self) -> ModelRuntimeConfiguration:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("API Base URL 必须是有效的 http 或 https 地址")
        if parsed.username or parsed.password:
            raise ValueError("API Base URL 中不能包含用户名或密码")
        self.base_url = self.base_url.rstrip("/")
        self.api_key = self.api_key.strip()
        return self


class ModelConfigurationUpdate(StrictModel):
    base_url: NonEmptyText
    model_name: NonEmptyText
    api_key: str | None = None
    clear_api_key: bool = False
    timeout_seconds: float = Field(ge=5, le=600)
    trial_seeds_per_subtask: int = Field(ge=1, le=5)

    @model_validator(mode="after")
    def validate_update(self) -> ModelConfigurationUpdate:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("API Base URL 必须是有效的 http 或 https 地址")
        if parsed.username or parsed.password:
            raise ValueError("API Base URL 中不能包含用户名或密码")
        if self.clear_api_key and self.api_key and self.api_key.strip():
            raise ValueError("不能同时设置和清除 API Key")
        self.base_url = self.base_url.rstrip("/")
        if self.api_key is not None:
            self.api_key = self.api_key.strip()
        return self


class SolutionUpdate(StrictModel):
    solution_code: NonEmptyText


class ProblemSample(StrictModel):
    input: str = ""
    output: str = ""
    note: str = ""


class ProblemInput(StrictModel):
    description: NonEmptyText
    input_description: str = "未提供"
    output_description: str = "未提供"
    samples: list[ProblemSample] = Field(default_factory=list)
    difficulty: NonEmptyText


class CompileState(StrictModel):
    status: Literal["pending", "passed", "failed"] = "pending"
    log: str = ""


class SolutionInput(StrictModel):
    language: Literal["cpp"] = "cpp"
    source: NonEmptyText
    compile: CompileState = Field(default_factory=CompileState)


class InputStructureState(StrictModel):
    template: str = ""
    status: Literal["pending", "draft", "confirmed"] = "pending"
    revision: int = Field(default=0, ge=0)


class GlobalInput(StrictModel):
    problem: ProblemInput
    solution: SolutionInput
    input_structure: InputStructureState = Field(default_factory=InputStructureState)
    revision: int = Field(default=1, ge=1)


class StageState(StrictModel):
    status: StageStatus = StageStatus.DRAFT
    ai_confirmed: bool = False
    user_confirmed: bool = False
    issues: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def passed_requires_both_confirmations(self) -> StageState:
        if self.status == StageStatus.PASSED and not (self.ai_confirmed and self.user_confirmed):
            raise ValueError("a passed interactive stage requires AI and user confirmation")
        return self


class ProjectRecord(StrictModel):
    project_id: str
    problem_description: str
    difficulty: str
    current_stage: Stage = Stage.SOLUTION_COMPILE
    solution_compiled: bool = False
    input_revision: int = Field(default=1, ge=1)
    subtasks_revision: int = Field(default=0, ge=0)
    workflow_revision: int = Field(default=1, ge=1)
    code_input_revision: int | None = Field(default=None, ge=1)
    code_subtasks_revision: int | None = Field(default=None, ge=1)
    stage_threads: dict[int, str] = Field(default_factory=dict)
    failed_stage_threads: dict[int, str] = Field(default_factory=dict)
    generated_subtasks: list[int] = Field(default_factory=list)
    generation_complete: bool = False
    stages: dict[int, StageState] = Field(
        default_factory=lambda: {stage: StageState() for stage in (3, 4, 5)}
    )
    build_complete: bool = False
    export_ready: bool = False
    last_error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StructureTag(StrictModel):
    tag_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=64,
            pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$",
        ),
    ]
    applies_to: NonEmptyText
    evidence: NonEmptyText


class InputStructureDraft(StrictModel):
    template: NonEmptyText
    structure_tags: list[StructureTag] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class SpecialCase(StrictModel):
    count: int = Field(gt=0)
    description: NonEmptyText


class RuntimeParameter(StrictModel):
    name: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=32,
            pattern=r"^[a-z][a-z0-9_]*$",
        ),
    ]
    value: RuntimeScalar
    category: Literal["size", "limit", "structure"]

    @model_validator(mode="after")
    def value_fits_runner_argument(self) -> RuntimeParameter:
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("runtime parameter floats must be finite")
        serialized = "1" if self.value is True else "0" if self.value is False else str(self.value)
        if len(serialized) > 64:
            raise ValueError("runtime parameter value exceeds runner argument limit")
        return self


class TestPointRuntimeParameters(StrictModel):
    case_id: int = Field(gt=0)
    parameters: list[RuntimeParameter] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def parameter_names_are_unique_and_reserved_names_are_rejected(
        self,
    ) -> TestPointRuntimeParameters:
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("runtime parameter names must be unique within a test point")
        reserved = {"seed", "subtask", "case"}.intersection(names)
        if reserved:
            raise ValueError("runtime parameter names cannot shadow runner arguments")
        return self


class Subtask(StrictModel):
    id: int = Field(gt=0)
    constraints: NonEmptyText
    test_count: int = Field(gt=0)
    expected_complexity: NonEmptyText
    special_cases: list[SpecialCase] = Field(default_factory=list)
    runtime_parameters: list[TestPointRuntimeParameters] = Field(default_factory=list)
    subtask_tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def special_count_fits_total(self) -> Subtask:
        if sum(item.count for item in self.special_cases) > self.test_count:
            raise ValueError("special case count exceeds the subtask test count")
        if self.runtime_parameters:
            case_ids = [profile.case_id for profile in self.runtime_parameters]
            if case_ids != list(range(1, self.test_count + 1)):
                raise ValueError("runtime parameter case ids must exactly match 1..test_count")
        if len(self.subtask_tags) != len(set(self.subtask_tags)):
            raise ValueError("subtask structure tags must be unique")
        return self


class SubtaskPlanDraft(StrictModel):
    subtasks: list[Subtask] = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def subtask_ids_are_unique(self) -> SubtaskPlanDraft:
        ids = [subtask.id for subtask in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError("subtask ids must be unique")
        return self


class ProofObligation(StrictModel):
    constraint_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=160,
            pattern=r"^[a-z0-9][a-z0-9_.:-]*$",
        ),
    ]
    scope: NonEmptyText
    severity: Literal["blocker", "warning"]
    verification_method: Literal[
        "static",
        "compile",
        "counterexample",
        "semantic",
        "contract",
    ]
    requirement: NonEmptyText


class ImplementationLocation(StrictModel):
    target_file: Literal["generator.cpp", "validator.cpp"]
    symbol: NonEmptyText
    line_start: int = Field(gt=0)
    line_end: int = Field(gt=0)

    @model_validator(mode="after")
    def line_range_is_ordered(self) -> ImplementationLocation:
        if self.line_end < self.line_start:
            raise ValueError("implementation line range is reversed")
        return self


class DocumentApiClaim(StrictModel):
    filename: NonEmptyText
    symbol: NonEmptyText


class ConstraintImplementationClaim(StrictModel):
    constraint_id: NonEmptyText
    locations: list[ImplementationLocation] = Field(min_length=1)
    used_parameters: list[str] = Field(default_factory=list)
    document_evidence: list[DocumentApiClaim] = Field(default_factory=list)
    test_strategy: NonEmptyText

    @model_validator(mode="after")
    def implementation_fields_are_unique(self) -> ConstraintImplementationClaim:
        if len(self.used_parameters) != len(set(self.used_parameters)):
            raise ValueError("implementation parameter names must be unique")
        evidence = [(item.filename, item.symbol) for item in self.document_evidence]
        if len(evidence) != len(set(evidence)):
            raise ValueError("document evidence must be unique")
        return self


class DocumentApiEvidence(StrictModel):
    filename: NonEmptyText
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    symbol: NonEmptyText


class ConstraintImplementation(StrictModel):
    constraint_id: NonEmptyText
    locations: list[ImplementationLocation] = Field(min_length=1)
    used_parameters: list[str] = Field(default_factory=list)
    document_evidence: list[DocumentApiEvidence] = Field(default_factory=list)
    test_strategy: NonEmptyText

    @model_validator(mode="after")
    def implementation_fields_are_unique(self) -> ConstraintImplementation:
        if len(self.used_parameters) != len(set(self.used_parameters)):
            raise ValueError("implementation parameter names must be unique")
        evidence = [
            (item.filename, item.digest, item.symbol) for item in self.document_evidence
        ]
        if len(evidence) != len(set(evidence)):
            raise ValueError("document evidence must be unique")
        return self


class DefectIdentity(StrictModel):
    category: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    target_file: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    ]
    constraint_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
    ]
    subtask: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    test_point: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    ]
    error_code: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Z0-9_]+$",
        ),
    ]


class Defect(StrictModel):
    defect_id: Annotated[
        str,
        StringConstraints(pattern=r"^defect_[0-9a-f]{20}$"),
    ]
    identity: DefectIdentity
    severity: Literal["blocker", "warning"] = "blocker"
    validation_level: Literal[
        "contract",
        "static",
        "compile",
        "smoke",
        "complete",
        "semantic",
    ]
    message: NonEmptyText
    evidence: dict[str, Any] = Field(default_factory=dict)


class CounterexampleRepair(StrictModel):
    candidate_revision: NonEmptyText
    patch_scope: list[str] = Field(default_factory=list)
    outcome: Literal["accepted", "rolled_back", "still_open", "regressed"]
    reason: NonEmptyText
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Counterexample(StrictModel):
    counterexample_id: Annotated[
        str,
        StringConstraints(pattern=r"^case_[0-9a-f]{20}$"),
    ]
    defect: Defect
    status: Literal["open", "closed", "regressed"] = "open"
    reproduction: dict[str, Any] = Field(default_factory=dict)
    first_seen_revision: NonEmptyText
    last_seen_revision: NonEmptyText
    repair_history: list[CounterexampleRepair] = Field(default_factory=list)


class CounterexampleLedger(StrictModel):
    verifier_revision: NonEmptyText = "agent4-verifier-v5"
    counterexamples: list[Counterexample] = Field(default_factory=list)
    last_valid_candidate_revision: str | None = None


class AgentDecisionEvent(StrictModel):
    run_id: NonEmptyText
    candidate_revision: NonEmptyText
    target_defect_id: str | None = None
    model_call_type: Literal[
        "generation",
        "semantic_audit",
        "targeted_recheck",
        "repair",
        "none",
    ] = "none"
    modified_files: list[str] = Field(default_factory=list)
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    progress: bool = False
    decision: Literal["accepted", "rolled_back", "stopped", "observed"]
    reason: NonEmptyText
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReportedDefect(StrictModel):
    origin: Literal["candidate", "upstream_contract"]
    identity: DefectIdentity
    severity: Literal["blocker", "warning"] = "blocker"
    validation_level: Literal["semantic"] = "semantic"
    message: NonEmptyText
    evidence: dict[str, Any] = Field(default_factory=dict)


class SemanticAudit(StrictModel):
    defects: list[ReportedDefect] = Field(default_factory=list)


class TargetedDefectCheck(StrictModel):
    defect_id: NonEmptyText
    still_present: bool
    message: NonEmptyText
    evidence: dict[str, Any] = Field(default_factory=dict)


class CodeRepairPatch(StrictModel):
    target_defect_id: NonEmptyText
    rationale: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
    ]
    generator_code: str | None = None
    validator_code: str | None = None
    implementation_mapping_upserts: list[ConstraintImplementationClaim] = Field(
        default_factory=list
    )
    implementation_mapping_remove_ids: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def patch_changes_at_least_one_field(self) -> CodeRepairPatch:
        if (
            self.generator_code is None
            and self.validator_code is None
            and not self.implementation_mapping_upserts
            and not self.implementation_mapping_remove_ids
        ):
            raise ValueError("repair patch must change at least one field")
        if self.generator_code is not None and not self.generator_code.strip():
            raise ValueError("generator patch cannot be blank")
        if self.validator_code is not None and not self.validator_code.strip():
            raise ValueError("validator patch cannot be blank")
        upsert_ids = [item.constraint_id for item in self.implementation_mapping_upserts]
        if len(upsert_ids) != len(set(upsert_ids)):
            raise ValueError("implementation mapping upsert ids must be unique")
        if len(self.implementation_mapping_remove_ids) != len(
            set(self.implementation_mapping_remove_ids)
        ):
            raise ValueError("implementation mapping remove ids must be unique")
        overlap = set(upsert_ids) & set(self.implementation_mapping_remove_ids)
        if overlap:
            raise ValueError("implementation mapping cannot upsert and remove the same id")
        return self


class CodeGenerationSubmission(StrictModel):
    """Agent4 wire contract; backend-owned proof data is injected by the graph."""

    generator_code: NonEmptyText
    validator_code: NonEmptyText
    implementation_mapping: list[ConstraintImplementationClaim] = Field(min_length=1)


class CodeDraft(StrictModel):
    generator_code: NonEmptyText
    validator_code: NonEmptyText
    proof_obligations: list[ProofObligation] = Field(min_length=1)
    implementation_mapping: list[ConstraintImplementation] = Field(min_length=1)
    revision_id: str | None = None
    input_revision: int | None = Field(default=None, ge=1)
    subtasks_revision: int | None = Field(default=None, ge=1)
    trial_results: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def proof_and_implementation_ids_are_unique(self) -> CodeDraft:
        proof_ids = [item.constraint_id for item in self.proof_obligations]
        mapping_ids = [item.constraint_id for item in self.implementation_mapping]
        if len(proof_ids) != len(set(proof_ids)):
            raise ValueError("proof obligation ids must be unique")
        if len(mapping_ids) != len(set(mapping_ids)):
            raise ValueError("implementation mapping ids must be unique")
        return self


class WorkflowOutput(StrictModel):
    confirmation: Confirmation
    result: dict[str, Any] | None = None
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def pass_has_no_pending_work(self) -> WorkflowOutput:
        if self.confirmation == Confirmation.PASS and self.issues:
            raise ValueError("pass cannot contain unresolved issues")
        return self


class DraftUpdate(StrictModel):
    draft: dict[str, Any]


class StageRunRequest(StrictModel):
    task_type: TaskType | None = None


class UserConfirmation(StrictModel):
    confirmed: Literal[True]


class PreviewRequest(StrictModel):
    subtask_id: int = Field(gt=0)
    case_id: int = Field(default=1, gt=0)
    seed: int


class GenerateRequest(StrictModel):
    base_seed: int = 1
    selected_subtask_ids: list[int] | None = None


class ValidateRequest(StrictModel):
    selected_subtask_ids: list[int] | None = None


class SandboxResult(StrictModel):
    ok: bool
    exit_code: int
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    output_file: str | None = None
