from __future__ import annotations

from typing import Any

from app.models import (
    Subtask,
    SubtaskPlanDraft,
    TestPointRuntimeParameters,
)


def runtime_parameter_issues(plan: SubtaskPlanDraft) -> list[str]:
    issues: list[str] = []
    for subtask in plan.subtasks:
        if not subtask.runtime_parameters:
            issues.append(f"子任务 {subtask.id} 缺少逐测试点运行时参数。")
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
