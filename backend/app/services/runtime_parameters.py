from __future__ import annotations

from typing import Any

from app.models import (
    Subtask,
    SubtaskPlanDraft,
    TestPointRuntimeParameters,
)
from app.services.structure_tag_catalog import StructureTagCatalog


def runtime_parameter_issues(plan: SubtaskPlanDraft) -> list[str]:
    issues: list[str] = []
    for subtask in plan.subtasks:
        if not subtask.runtime_parameters:
            issues.append(f"子任务 {subtask.id} 缺少逐测试点运行时参数。")
    return issues


def structure_tag_parameter_issues(
    plan: SubtaskPlanDraft,
    global_tag_ids: list[str],
    catalog: StructureTagCatalog,
) -> list[str]:
    issues: list[str] = []
    for subtask in plan.subtasks:
        effective_tags = list(dict.fromkeys([*global_tag_ids, *subtask.subtask_tags]))
        issues.extend(
            f"子任务 {subtask.id}：{issue}" for issue in catalog.validate_tag_ids(effective_tags)
        )
        if any(tag_id not in catalog.entries for tag_id in effective_tags):
            continue
        required = catalog.required_runtime_parameters(effective_tags)
        for profile in subtask.runtime_parameters:
            values = {parameter.name: parameter.value for parameter in profile.parameters}
            missing = sorted(required - values.keys())
            if missing:
                issues.append(
                    f"子任务 {subtask.id} 测试点 {profile.case_id} 缺少标签必需参数："
                    + ", ".join(missing)
                    + "。"
                )
    return issues


def profile_for_case(subtask: Subtask, case_id: int) -> TestPointRuntimeParameters:
    for profile in subtask.runtime_parameters:
        if profile.case_id == case_id:
            return profile
    raise ValueError(f"subtask {subtask.id} case {case_id} has no runtime parameters")


def serialized_arguments(profile: TestPointRuntimeParameters) -> dict[str, str]:
    return {
        parameter.name: serialize_runtime_value(parameter.value) for parameter in profile.parameters
    }


def serialize_runtime_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)
