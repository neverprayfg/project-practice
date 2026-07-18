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
ProjectName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=40),
]
UserInstruction = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]
GeneratorAuditIssue = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]
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
FormatContractId = Annotated[str, StringConstraints(pattern=r"^format_[0-9a-f]{24}$")]
GenerationProfileId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=32,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]
AGENT4_ARCHITECTURE_ID = "agent4-profiled-working-template-v3-stage3-test-data-plan"


def _runtime_scalar_type(value: RuntimeScalar) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stage(IntEnum):
    CREATE = 1
    SOLUTION_COMPILE = 2
    TEST_DATA_PLAN = 3
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
    TEST_DATA_PLAN = "test_data_plan"
    SUBTASK_PLAN = "subtask_plan"


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


class InputNormalizationDraft(StrictModel):
    """Agent1 may enrich parsed problem fields but cannot echo authoritative input."""

    project_name: ProjectName
    input_description: NonEmptyText
    output_description: NonEmptyText
    samples: list[ProblemSample] = Field(default_factory=list)


class CompileState(StrictModel):
    status: Literal["pending", "passed", "failed"] = "pending"
    log: str = ""


class SolutionInput(StrictModel):
    language: Literal["cpp"] = "cpp"
    source: NonEmptyText
    compile: CompileState = Field(default_factory=CompileState)


class InputStructureState(StrictModel):
    """Legacy persisted field retained only to read projects created before Stage 3."""

    template: str = ""
    status: Literal["pending", "draft", "confirmed"] = "pending"
    revision: int = Field(default=0, ge=0)


class GlobalInput(StrictModel):
    problem: ProblemInput
    solution: SolutionInput
    project_name: str = ""
    input_structure: InputStructureState = Field(
        default_factory=InputStructureState,
        exclude=True,
    )
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


class StageRecoverySummary(StrictModel):
    run_id: str
    stage: int = Field(ge=1, le=8)
    status: Literal["running", "passed", "failed"] = "running"
    generation_round: int = Field(default=0, ge=0)
    repair_attempts: int = Field(default=0, ge=0)
    last_error_summary: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


class RecoveryFailureClass(StrEnum):
    RESPONSE_CONTRACT = "response_contract"
    BUSINESS_CONTRACT = "business_contract"
    DETERMINISTIC_EXECUTION = "deterministic_execution"
    ENVIRONMENT = "environment"
    AUTHORIZATION = "authorization"


class RecoveryError(StrictModel):
    source: Literal["json", "pydantic", "validator", "compiler", "runner", "system"]
    location: list[str | int] = Field(default_factory=list)
    message: NonEmptyText
    code: NonEmptyText


class RawCandidate(StrictModel):
    operation: str = ""
    raw_output: str = ""
    candidate: dict[str, Any] | None = None
    validation_errors: list[RecoveryError] = Field(default_factory=list)
    response_metadata: dict[str, Any] = Field(default_factory=dict)


class RecoveryValidationResult(StrictModel):
    passed: bool
    failure_class: RecoveryFailureClass | None = None
    repairable: bool = False
    candidate: dict[str, Any] | None = None
    raw_output: str = ""
    errors: list[RecoveryError] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    response_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def failure_has_class_and_pass_has_candidate(self) -> RecoveryValidationResult:
        if self.passed and self.candidate is None:
            raise ValueError("passed recovery validation requires a candidate")
        if not self.passed and self.failure_class is None:
            raise ValueError("failed recovery validation requires a failure class")
        return self


class RecoveryMutationGrant(StrictModel):
    stage: int = Field(ge=1, le=8)
    artifact: NonEmptyText
    paths: list[NonEmptyText] = Field(min_length=1)


class RecoveryPlan(StrictModel):
    observed_stage: int = Field(ge=1, le=8)
    root_stage: int = Field(ge=1, le=8)
    failure_class: RecoveryFailureClass
    evidence: list[NonEmptyText] = Field(default_factory=list)
    context_requirements: list[NonEmptyText] = Field(default_factory=list)
    write_grants: list[RecoveryMutationGrant] = Field(default_factory=list)
    protected_fields: list[NonEmptyText] = Field(default_factory=list)
    revalidate_from_stage: int = Field(ge=1, le=8)
    invalidate_downstream_from_stage: int | None = Field(default=None, ge=1, le=8)
    requires_user_authorization: bool = False
    confidence: float = Field(ge=0, le=1)

    def allows_stage(self, stage: int) -> bool:
        return not self.requires_user_authorization and any(
            grant.stage == stage for grant in self.write_grants
        )


class RecoveryOutcome(StrictModel):
    status: Literal["passed", "failed", "exhausted"]
    candidate: dict[str, Any] | None = None
    summary: StageRecoverySummary
    validation: RecoveryValidationResult | None = None
    recovery_plan: RecoveryPlan | None = None


class ProjectRecord(StrictModel):
    project_id: str
    project_name: str = ""
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
    recovery_summaries: dict[int, StageRecoverySummary] = Field(default_factory=dict)
    build_complete: bool = False
    export_ready: bool = False
    last_error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TestDataPlanDraft(StrictModel):
    """The human-reviewable Stage 3 test-data design document."""

    plan_markdown: NonEmptyText
    issues: list[str] = Field(default_factory=list)


class GenerationProfile(StrictModel):
    id: GenerationProfileId
    category: Literal["rules_format", "anti_algorithm", "boundary_edge"]
    count: int = Field(gt=0)
    goal: NonEmptyText
    parameter_names: list[GenerationProfileId] = Field(default_factory=list, max_length=23)

    @model_validator(mode="after")
    def parameter_names_are_unique(self) -> GenerationProfile:
        if len(self.parameter_names) != len(set(self.parameter_names)):
            raise ValueError("generation profile parameter names must be unique")
        return self


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
    generation_profile_id: GenerationProfileId
    parameters: list[RuntimeParameter] = Field(min_length=1, max_length=23)

    @model_validator(mode="after")
    def parameter_names_are_unique_and_reserved_names_are_rejected(
        self,
    ) -> TestPointRuntimeParameters:
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("runtime parameter names must be unique within a test point")
        reserved = {"seed", "subtask", "case", "generation_profile"}.intersection(names)
        if reserved:
            raise ValueError("runtime parameter names cannot shadow runner arguments")
        return self


class Subtask(StrictModel):
    id: int = Field(gt=0)
    test_count: int = Field(ge=3)
    expected_complexity: NonEmptyText
    generation_profiles: list[GenerationProfile] = Field(min_length=3)
    runtime_parameters: list[TestPointRuntimeParameters] = Field(default_factory=list)

    @model_validator(mode="after")
    def generation_contract_is_complete(self) -> Subtask:
        profile_ids = [profile.id for profile in self.generation_profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("generation profile ids must be unique")
        categories = {profile.category for profile in self.generation_profiles}
        required_categories = {"rules_format", "anti_algorithm", "boundary_edge"}
        if categories != required_categories:
            raise ValueError("generation profiles must cover all three generation categories")
        if sum(profile.count for profile in self.generation_profiles) != self.test_count:
            raise ValueError("generation profile counts must exactly match test_count")
        if self.runtime_parameters:
            case_ids = [profile.case_id for profile in self.runtime_parameters]
            if case_ids != list(range(1, self.test_count + 1)):
                raise ValueError("runtime parameter case ids must exactly match 1..test_count")
            profiles_by_id = {profile.id: profile for profile in self.generation_profiles}
            observed_counts = {profile_id: 0 for profile_id in profiles_by_id}
            expected_schema: dict[str, tuple[str, str]] | None = None
            for runtime in self.runtime_parameters:
                if runtime.generation_profile_id not in profiles_by_id:
                    raise ValueError("runtime parameters reference an unknown generation profile")
                observed_counts[runtime.generation_profile_id] += 1
                schema = {
                    parameter.name: (parameter.category, _runtime_scalar_type(parameter.value))
                    for parameter in runtime.parameters
                }
                if expected_schema is None:
                    expected_schema = schema
                elif schema != expected_schema:
                    raise ValueError(
                        "all test points in a subtask must share one runtime parameter schema"
                    )
            expected_counts = {profile.id: profile.count for profile in self.generation_profiles}
            if observed_counts != expected_counts:
                raise ValueError("runtime generation profile assignments must match profile counts")
            schema_names = set(expected_schema or {})
            for profile in self.generation_profiles:
                if not set(profile.parameter_names).issubset(schema_names):
                    raise ValueError(
                        f"generation profile {profile.id} references an unknown runtime parameter"
                    )
        return self


class SubtaskPlanDraft(StrictModel):
    subtasks: list[Subtask] = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def subtask_ids_are_contiguous(self) -> SubtaskPlanDraft:
        ids = [subtask.id for subtask in self.subtasks]
        if ids != list(range(1, len(self.subtasks) + 1)):
            raise ValueError("subtask ids must be contiguous and ordered from 1")
        return self


class GeneratorConstructionStrategy(StrictModel):
    subtask_id: int = Field(gt=0)
    generation_profile_id: GenerationProfileId
    profile_category: Literal["rules_format", "anti_algorithm", "boundary_edge"]
    construction_mode: GenerationProfileId
    goal: NonEmptyText
    runtime_parameters: list[GenerationProfileId] = Field(min_length=1, max_length=23)
    input_invariants: list[NonEmptyText] = Field(min_length=1)
    construction_steps: list[NonEmptyText] = Field(min_length=1)
    post_checks: list[NonEmptyText] = Field(min_length=1)
    seed_policy: Literal["fixed", "diverse"]
    variation_dimensions: list[NonEmptyText] = Field(default_factory=list)
    complexity_target: NonEmptyText

    @model_validator(mode="after")
    def runtime_parameters_are_unique(self) -> GeneratorConstructionStrategy:
        if len(self.runtime_parameters) != len(set(self.runtime_parameters)):
            raise ValueError("generator strategy runtime parameters must be unique")
        return self


class GeneratorAnalysisDraft(StrictModel):
    input_constraints: list[NonEmptyText] = Field(min_length=1)
    solution_branch_risks: list[NonEmptyText] = Field(min_length=1)
    overflow_and_resource_guards: list[NonEmptyText] = Field(min_length=1)
    strategies: list[GeneratorConstructionStrategy] = Field(default_factory=list)


class GeneratorAuditDraft(StrictModel):
    passed: bool
    issues: list[GeneratorAuditIssue] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def result_matches_issues(self) -> GeneratorAuditDraft:
        if self.passed and self.issues:
            raise ValueError("a passed generator audit cannot contain issues")
        if not self.passed and not self.issues:
            raise ValueError("a failed generator audit must contain issues")
        return self


class InputWhitespaceContract(StrictModel):
    token_separator: Literal["single_ascii_space"] = "single_ascii_space"
    leading_space: Literal["forbidden"] = "forbidden"
    trailing_space: Literal["forbidden"] = "forbidden"
    tab_character: Literal["forbidden"] = "forbidden"
    blank_line: Literal["forbidden_unless_template_requires"] = "forbidden_unless_template_requires"
    line_ending: Literal["lf"] = "lf"
    final_newline: Literal["required"] = "required"


class InputFormatContract(StrictModel):
    """Backend-owned interface shared by parallel generator/validator calls."""

    format_version: Literal[1] = 1
    format_contract_id: FormatContractId
    input_template: NonEmptyText
    reference_sample_inputs: list[str] = Field(default_factory=list)
    testcase_cardinality: Literal["one_testcase_per_process"] = "one_testcase_per_process"
    encoding: Literal["utf-8"] = "utf-8"
    layout_policy: Literal["follow_input_template_exactly"] = "follow_input_template_exactly"
    whitespace: InputWhitespaceContract = Field(default_factory=InputWhitespaceContract)
    generator_stdout_policy: Literal["input_only_no_diagnostics"] = "input_only_no_diagnostics"
    validator_consumption_policy: Literal["read_exact_template_then_eof"] = (
        "read_exact_template_then_eof"
    )


class CodeDraft(StrictModel):
    format_contract_id: FormatContractId
    generator_code: NonEmptyText
    validator_code: NonEmptyText
    revision_id: str | None = None
    input_revision: int | None = Field(default=None, ge=1)
    subtasks_revision: int | None = Field(default=None, ge=1)
    issues: list[str] = Field(default_factory=list)


class CodeRelease(StrictModel):
    architecture: Literal[
        "agent4-profiled-working-template-v3-stage3-test-data-plan"
    ] = AGENT4_ARCHITECTURE_ID
    format_contract_id: FormatContractId
    revision_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
    input_revision: int = Field(ge=1)
    subtasks_revision: int = Field(ge=1)
    generator_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    validator_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    content_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    frozen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkflowOutput(StrictModel):
    confirmation: Confirmation
    result: dict[str, Any] | None = None
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def pass_has_no_pending_work(self) -> WorkflowOutput:
        if self.confirmation == Confirmation.PASS and self.issues:
            raise ValueError("pass cannot contain unresolved issues")
        return self


class StageInstructionDecision(StrictModel):
    action: Literal["answer", "revise"]
    answer: UserInstruction
    target: Literal["none", "current_artifact", "generator", "validator", "both"]

    @model_validator(mode="after")
    def action_matches_target(self) -> StageInstructionDecision:
        if self.action == "answer" and self.target != "none":
            raise ValueError("answer action must use the none target")
        if self.action == "revise" and self.target == "none":
            raise ValueError("revise action must select a target")
        return self


class DraftUpdate(StrictModel):
    draft: dict[str, Any]


class StageRunRequest(StrictModel):
    user_instruction: UserInstruction | None = None


class AutoRunRequest(StrictModel):
    base_seed: int = 1


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
