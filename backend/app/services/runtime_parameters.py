from __future__ import annotations

from typing import Any

from app.models import CodeDraft, Subtask, SubtaskPlanDraft, TestPointRuntimeParameters
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
            f"子任务 {subtask.id}：{issue}"
            for issue in catalog.validate_tag_ids(effective_tags)
        )
        if any(tag_id not in catalog.entries for tag_id in effective_tags):
            continue
        required = catalog.required_runtime_parameters(effective_tags)
        expanded = catalog.expand(effective_tags)
        for profile in subtask.runtime_parameters:
            values = {parameter.name: parameter.value for parameter in profile.parameters}
            missing = sorted(required - values.keys())
            if missing:
                issues.append(
                    f"子任务 {subtask.id} 测试点 {profile.case_id} 缺少标签必需参数："
                    + ", ".join(missing)
                    + "。"
                )
            if (
                "tree" in expanded
                and isinstance(values.get("n"), int)
                and values.get("m") != values["n"] - 1
            ):
                issues.append(
                    f"子任务 {subtask.id} 测试点 {profile.case_id} 声明为树，"
                    "但运行时参数不满足 m = n - 1。"
                )
    return issues


def coverage_issues(
    draft: CodeDraft,
    plan: SubtaskPlanDraft,
    global_tag_ids: list[str] | None = None,
) -> list[str]:
    expected: dict[tuple[int, int], set[str]] = {}
    expected_tags: dict[tuple[int, int], set[str]] = {}
    global_tag_ids = global_tag_ids or []
    for subtask in plan.subtasks:
        for profile in subtask.runtime_parameters:
            expected[(subtask.id, profile.case_id)] = {
                parameter.name for parameter in profile.parameters
            }
            expected_tags[(subtask.id, profile.case_id)] = set(
                [*global_tag_ids, *subtask.subtask_tags]
            )
    actual: dict[tuple[int, int], set[str]] = {}
    actual_tags: dict[tuple[int, int], set[str]] = {}
    for item in draft.constraint_coverage:
        key = (item.subtask_id, item.case_id)
        if key in actual:
            return [f"约束覆盖表重复记录子任务 {key[0]} 测试点 {key[1]}。"]
        actual[key] = set(item.parameter_names)
        actual_tags[key] = set(item.structure_tags)
    issues: list[str] = []
    for key, names in expected.items():
        if key not in actual:
            issues.append(f"约束覆盖表缺少子任务 {key[0]} 测试点 {key[1]}。")
        elif actual[key] != names:
            issues.append(
                f"子任务 {key[0]} 测试点 {key[1]} 的覆盖参数与运行时参数不一致。"
            )
        elif actual_tags[key] != expected_tags[key]:
            issues.append(
                f"子任务 {key[0]} 测试点 {key[1]} 的覆盖表结构标签不完整。"
            )
    for key in actual.keys() - expected.keys():
        issues.append(f"约束覆盖表包含不存在的子任务 {key[0]} 测试点 {key[1]}。")
    return issues


def profile_for_case(subtask: Subtask, case_id: int) -> TestPointRuntimeParameters:
    for profile in subtask.runtime_parameters:
        if profile.case_id == case_id:
            return profile
    raise ValueError(f"subtask {subtask.id} case {case_id} has no runtime parameters")


def serialized_arguments(profile: TestPointRuntimeParameters) -> dict[str, str]:
    return {
        parameter.name: serialize_runtime_value(parameter.value)
        for parameter in profile.parameters
    }


def serialize_runtime_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)
