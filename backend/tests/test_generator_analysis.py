from __future__ import annotations

from app.models import GeneratorAnalysisDraft
from app.services.generator_analysis import (
    generator_analysis_issues,
    normalize_generator_analysis,
)


def _subtasks() -> list[dict]:
    return [
        {
            "id": 1,
            "generation_profiles": [
                {
                    "id": "positive",
                    "category": "rules_format",
                    "goal": "construct valid positive cases",
                    "parameter_names": ["construction_mode", "variation_budget", "n"],
                }
            ],
            "runtime_parameters": [
                {
                    "case_id": 1,
                    "generation_profile_id": "positive",
                    "parameters": [
                        {
                            "name": "construction_mode",
                            "value": "solvable_constructed",
                            "category": "structure",
                        },
                        {"name": "variation_budget", "value": 8, "category": "limit"},
                        {"name": "n", "value": 100, "category": "size"},
                    ],
                }
            ],
        }
    ]


def _analysis(**updates: object) -> GeneratorAnalysisDraft:
    strategy = {
        "subtask_id": 1,
        "generation_profile_id": "positive",
        "profile_category": "rules_format",
        "construction_mode": "solvable_constructed",
        "goal": "construct valid positive cases",
        "runtime_parameters": ["construction_mode", "variation_budget", "n"],
        "input_invariants": ["1 <= n <= 100"],
        "construction_steps": ["construct from a valid witness"],
        "post_checks": ["recompute the witness value"],
        "seed_policy": "diverse",
        "variation_dimensions": ["witness factors"],
        "complexity_target": "cover the successful branch",
        **updates,
    }
    return GeneratorAnalysisDraft.model_validate(
        {
            "input_constraints": ["1 <= n <= 100"],
            "solution_branch_risks": ["successful and failed branches"],
            "overflow_and_resource_guards": ["check before multiplication"],
            "strategies": [strategy],
        }
    )


def test_generator_analysis_accepts_complete_stage4_strategy_mapping() -> None:
    assert generator_analysis_issues(_analysis(), _subtasks()) == []


def test_generator_analysis_rejects_wrong_seed_policy_and_missing_parameter() -> None:
    issues = generator_analysis_issues(
        _analysis(
            runtime_parameters=["construction_mode"],
            seed_policy="fixed",
            variation_dimensions=[],
        ),
        _subtasks(),
    )

    assert any("未说明参数用途" in issue for issue in issues)
    assert any("seed_policy 必须是 diverse" in issue for issue in issues)


def test_generator_analysis_rejects_unplanned_mode() -> None:
    issues = generator_analysis_issues(
        _analysis(construction_mode="random_uniform"),
        _subtasks(),
    )

    assert any("缺少阶段四构造策略" in issue for issue in issues)
    assert any("未授权的构造策略" in issue for issue in issues)


def test_generator_analysis_normalizes_seed_policy_from_construction_mode() -> None:
    normalized = normalize_generator_analysis(
        _analysis(seed_policy="fixed", variation_dimensions=[])
    )

    assert normalized.strategies[0].seed_policy == "diverse"
    assert normalized.strategies[0].variation_dimensions
