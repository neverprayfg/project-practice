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
            continue
        fixed_signatures: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        for runtime in subtask.runtime_parameters:
            parameters = {parameter.name: parameter for parameter in runtime.parameters}
            control = parameters.get("construction_mode")
            if control is None:
                issues.append(
                    f"子任务 {subtask.id} 测试点 {runtime.case_id} "
                    "缺少 construction_mode 构造控制。"
                )
            elif control.category != "structure" or not isinstance(control.value, str):
                issues.append(
                    f"子任务 {subtask.id} 测试点 {runtime.case_id} 的 construction_mode "
                    "必须是 structure 类字符串。"
                )
            variation = parameters.get("variation_budget")
            if variation is None:
                issues.append(
                    f"子任务 {subtask.id} 测试点 {runtime.case_id} 缺少 variation_budget 参数。"
                )
            elif (
                variation.category != "limit"
                or not isinstance(variation.value, int)
                or isinstance(variation.value, bool)
            ):
                issues.append(
                    f"子任务 {subtask.id} 测试点 {runtime.case_id} 的 variation_budget "
                    "必须是 limit 类整数。"
                )
            elif control is not None and isinstance(control.value, str):
                if control.value == "fixed" and variation.value != 0:
                    issues.append(
                        f"子任务 {subtask.id} 测试点 {runtime.case_id} 的 fixed 模式必须使用 "
                        "variation_budget=0。"
                    )
                elif control.value != "fixed" and variation.value <= 0:
                    issues.append(
                        f"子任务 {subtask.id} 测试点 {runtime.case_id} 的非 fixed 模式必须使用"
                        "正整数 variation_budget。"
                    )
            if control is not None and control.value == "fixed":
                signature = (
                    runtime.generation_profile_id,
                    tuple(
                        sorted(
                            (parameter.name, serialize_runtime_value(parameter.value))
                            for parameter in runtime.parameters
                        )
                    ),
                )
                previous_case = fixed_signatures.get(signature)
                if previous_case is not None:
                    issues.append(
                        f"子任务 {subtask.id} 的 fixed 测试点 {previous_case} 与 "
                        f"{runtime.case_id} 运行参数完全相同，无法构造两个不同固定测试点。"
                    )
                else:
                    fixed_signatures[signature] = runtime.case_id
        profiles_by_id = {profile.id: profile for profile in subtask.generation_profiles}
        modes_by_profile = {profile_id: set() for profile_id in profiles_by_id}
        for runtime in subtask.runtime_parameters:
            for parameter in runtime.parameters:
                if parameter.name == "construction_mode" and isinstance(parameter.value, str):
                    modes_by_profile[runtime.generation_profile_id].add(parameter.value)
        for profile_id, profile in profiles_by_id.items():
            if "variation_budget" not in profile.parameter_names:
                issues.append(
                    f"子任务 {subtask.id} 的 profile {profile_id} 必须声明 variation_budget。"
                )
            if profile.category not in {"rules_format", "anti_algorithm"}:
                continue
            if not modes_by_profile[profile_id].difference({"fixed"}):
                issues.append(
                    f"子任务 {subtask.id} 的 {profile.category} profile {profile_id} "
                    "必须至少包含一种非 fixed 的可执行构造模式。"
                )
    return issues


def profile_for_case(subtask: Subtask, case_id: int) -> TestPointRuntimeParameters:
    for profile in subtask.runtime_parameters:
        if profile.case_id == case_id:
            return profile
    raise ValueError(f"subtask {subtask.id} case {case_id} has no runtime parameters")


def serialized_arguments(profile: TestPointRuntimeParameters) -> dict[str, str]:
    return {
        "generation_profile": profile.generation_profile_id,
        **{
            parameter.name: serialize_runtime_value(parameter.value)
            for parameter in profile.parameters
        },
    }


def serialize_runtime_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)
