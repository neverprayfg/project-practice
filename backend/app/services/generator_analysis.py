from __future__ import annotations

from typing import Any

from app.models import GeneratorAnalysisDraft


def normalize_generator_analysis(analysis: GeneratorAnalysisDraft) -> GeneratorAnalysisDraft:
    strategies = []
    changed = False
    for strategy in analysis.strategies:
        expected_policy = "fixed" if strategy.construction_mode == "fixed" else "diverse"
        dimensions = strategy.variation_dimensions
        if expected_policy == "fixed":
            dimensions = []
        elif not dimensions:
            dimensions = [
                "Use variation_budget to vary a constructed witness or structure while "
                "preserving every post_check."
            ]
        if strategy.seed_policy != expected_policy or dimensions != strategy.variation_dimensions:
            strategy = strategy.model_copy(
                update={
                    "seed_policy": expected_policy,
                    "variation_dimensions": dimensions,
                }
            )
            changed = True
        strategies.append(strategy)
    return analysis.model_copy(update={"strategies": strategies}) if changed else analysis


def generator_analysis_issues(
    analysis: GeneratorAnalysisDraft,
    subtasks: Any,
) -> list[str]:
    expected: dict[tuple[int, str, str], dict[str, Any]] = {}
    for subtask in subtasks if isinstance(subtasks, list) else []:
        if not isinstance(subtask, dict) or not isinstance(subtask.get("id"), int):
            continue
        subtask_id = subtask["id"]
        profiles = {
            profile.get("id"): profile
            for profile in subtask.get("generation_profiles", [])
            if isinstance(profile, dict) and isinstance(profile.get("id"), str)
        }
        for runtime in subtask.get("runtime_parameters", []):
            if not isinstance(runtime, dict):
                continue
            profile_id = runtime.get("generation_profile_id")
            profile = profiles.get(profile_id)
            if not isinstance(profile_id, str) or profile is None:
                continue
            parameters = {
                parameter.get("name"): parameter.get("value")
                for parameter in runtime.get("parameters", [])
                if isinstance(parameter, dict) and isinstance(parameter.get("name"), str)
            }
            mode = parameters.get("construction_mode")
            if not isinstance(mode, str):
                continue
            key = (subtask_id, profile_id, mode)
            expected[key] = {
                "category": profile.get("category"),
                "parameters": set(profile.get("parameter_names", []))
                | {"construction_mode"},
            }

    observed: dict[tuple[int, str, str], Any] = {}
    duplicate_keys: set[tuple[int, str, str]] = set()
    for strategy in analysis.strategies:
        key = (
            strategy.subtask_id,
            strategy.generation_profile_id,
            strategy.construction_mode,
        )
        if key in observed:
            duplicate_keys.add(key)
        observed[key] = strategy

    issues: list[str] = []
    if duplicate_keys:
        issues.append(f"生成器分析包含重复策略：{_format_keys(duplicate_keys)}。")
    missing = set(expected) - set(observed)
    if missing:
        issues.append(f"生成器分析缺少阶段四构造策略：{_format_keys(missing)}。")
    extra = set(observed) - set(expected)
    if extra:
        issues.append(f"生成器分析包含阶段四未授权的构造策略：{_format_keys(extra)}。")

    for key in sorted(set(expected) & set(observed)):
        specification = expected[key]
        strategy = observed[key]
        if strategy.profile_category != specification["category"]:
            issues.append(f"构造策略 {_format_key(key)} 的 profile_category 与阶段四不一致。")
        missing_parameters = specification["parameters"] - set(strategy.runtime_parameters)
        if missing_parameters:
            issues.append(
                f"构造策略 {_format_key(key)} 未说明参数用途："
                f"{', '.join(sorted(missing_parameters))}。"
            )
        expected_seed_policy = "fixed" if key[2] == "fixed" else "diverse"
        if strategy.seed_policy != expected_seed_policy:
            issues.append(
                f"构造策略 {_format_key(key)} 的 seed_policy 必须是 {expected_seed_policy}。"
            )
        if expected_seed_policy == "fixed" and strategy.variation_dimensions:
            issues.append(f"构造策略 {_format_key(key)} 是 fixed，不能声明随机变化维度。")
        if expected_seed_policy == "diverse" and not strategy.variation_dimensions:
            issues.append(f"构造策略 {_format_key(key)} 必须声明可保持语义的随机变化维度。")
    return issues


def _format_keys(keys: set[tuple[int, str, str]]) -> str:
    return ", ".join(_format_key(key) for key in sorted(keys))


def _format_key(key: tuple[int, str, str]) -> str:
    return f"subtask={key[0]}/profile={key[1]}/mode={key[2]}"
