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
    max_iterations: int = Field(default=4, ge=1, le=12)
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
    max_iterations: int = Field(ge=1, le=12)
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

    @model_validator(mode="before")
    @classmethod
    def convert_legacy_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "template" in value or "fields" not in value:
            return value
        lines = ["输入结构："]
        for index, field in enumerate(value.get("fields") or [], start=1):
            name = str(field.get("name") or f"字段{index}").strip()
            field_type = str(field.get("type") or "未注明类型").strip()
            description = str(field.get("description") or "").strip()
            lines.append(f"{index}. {name}（{field_type}）：{description or '输入值。'}")
        return {
            "template": "\n".join(lines),
            "structure_tags": value.get("structure_tags") or [],
            "issues": value.get("issues") or [],
        }


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

    @model_validator(mode="before")
    @classmethod
    def convert_legacy_ranges(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "constraints" in value or "ranges" not in value:
            return value
        lines = []
        for item in value.get("ranges") or []:
            field = str(item.get("field") or "输入").strip()
            constraint = str(item.get("constraint") or "未设置限制").strip()
            lines.append(f"{field}：{constraint}")
        normalized = dict(value)
        normalized.pop("ranges", None)
        normalized["constraints"] = "\n".join(lines) or "未设置额外数据限制。"
        return normalized

    @model_validator(mode="after")
    def special_count_fits_total(self) -> Subtask:
        if sum(item.count for item in self.special_cases) > self.test_count:
            raise ValueError("special case count exceeds the subtask test count")
        if self.runtime_parameters:
            case_ids = [profile.case_id for profile in self.runtime_parameters]
            if case_ids != list(range(1, self.test_count + 1)):
                raise ValueError(
                    "runtime parameter case ids must exactly match 1..test_count"
                )
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


class ConstraintCoverage(StrictModel):
    subtask_id: int = Field(gt=0)
    case_id: int = Field(gt=0)
    parameter_names: list[str] = Field(min_length=1)
    structure_tags: list[str] = Field(default_factory=list)
    generator_strategy: NonEmptyText
    validator_strategy: NonEmptyText
    boundary_or_special: NonEmptyText

    @model_validator(mode="after")
    def parameter_names_are_unique(self) -> ConstraintCoverage:
        if len(self.parameter_names) != len(set(self.parameter_names)):
            raise ValueError("constraint coverage parameter names must be unique")
        if len(self.structure_tags) != len(set(self.structure_tags)):
            raise ValueError("constraint coverage structure tags must be unique")
        return self


class CodeDraft(StrictModel):
    generator_code: NonEmptyText
    validator_code: NonEmptyText
    constraint_coverage: list[ConstraintCoverage] = Field(default_factory=list)
    revision_id: str | None = None
    input_revision: int | None = Field(default=None, ge=1)
    subtasks_revision: int | None = Field(default=None, ge=1)
    trial_results: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class JngenDocumentChoice(StrictModel):
    filename: NonEmptyText
    reason: NonEmptyText


class JngenDocumentSelection(StrictModel):
    # A later retrieval round may have no further document to add.  The flag
    # permits an early stop; the backend also has a fixed retrieval budget.
    selected_documents: list[JngenDocumentChoice] = Field(default_factory=list)
    # Older OpenAI-compatible providers may omit a false boolean despite the
    # JSON contract.  Missing means "continue"; an empty selection still must
    # explicitly close the retrieval conversation.
    selection_complete: bool = False

    @model_validator(mode="after")
    def filenames_are_unique(self) -> JngenDocumentSelection:
        filenames = [item.filename for item in self.selected_documents]
        if len(filenames) != len(set(filenames)):
            raise ValueError("selected jngen document filenames must be unique")
        return self


class ToolRequest(StrictModel):
    name: NonEmptyText
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_tool_name(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        # Some OpenAI-compatible models repeat the workflow-level decision on
        # each tool call. It does not belong to the tool gateway contract.
        normalized.pop("confirmation", None)
        if "name" not in normalized:
            if "tool_name" in normalized:
                normalized["name"] = normalized.pop("tool_name")
            elif "tool" in normalized:
                normalized["name"] = normalized.pop("tool")
        if "arguments" not in normalized:
            if "params" in normalized:
                normalized["arguments"] = normalized.pop("params")
            elif "args" in normalized:
                normalized["arguments"] = normalized.pop("args")
            else:
                argument_keys = {
                    "root",
                    "path",
                    "pattern",
                    "max_results",
                    "revision_id",
                    "subtask_id",
                    "seed",
                    "preview_id",
                }
                arguments = {
                    key: normalized.pop(key)
                    for key in tuple(normalized)
                    if key in argument_keys
                }
                if arguments:
                    normalized["arguments"] = arguments
        return normalized


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


class BuildRequest(StrictModel):
    base_seed: int = 1
    selected_subtask_ids: list[int] | None = None


class ValidateRequest(StrictModel):
    selected_subtask_ids: list[int] | None = None


class ToolExecutionResult(StrictModel):
    tool: str
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None


class SandboxResult(StrictModel):
    ok: bool
    exit_code: int
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    output_file: str | None = None
