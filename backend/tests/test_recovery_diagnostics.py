from __future__ import annotations

from app.models import RecoveryError, RecoveryFailureClass
from app.services.agent_recovery import validation_failure
from app.services.recovery_diagnostics import (
    RecoveryContextAssembler,
    RecoveryProblemLocator,
    generation_profile_diffs,
    runtime_schema_diffs,
)


def _candidate() -> dict:
    def case(case_id: int, parameters: list[dict]) -> dict:
        return {
            "case_id": case_id,
            "generation_profile_id": "profile",
            "parameters": parameters,
        }

    return {
        "subtasks": [
            {
                "id": 3,
                "test_count": 3,
                "generation_profiles": [
                    {
                        "id": "profile",
                        "category": "rules_format",
                        "count": 3,
                    }
                ],
                "runtime_parameters": [
                    case(
                        1,
                        [
                            {"name": "n", "category": "size", "value": 10},
                            {"name": "mode", "category": "shape", "value": "random"},
                        ],
                    ),
                    case(
                        2,
                        [
                            {"name": "n", "category": "size", "value": "10"},
                            {"name": "minimum", "category": "range", "value": 1},
                        ],
                    ),
                    case(
                        3,
                        [
                            {"name": "n", "category": "size", "value": 100},
                            {"name": "mode", "category": "shape", "value": "edge"},
                        ],
                    ),
                ],
            }
        ]
    }


def _failure(*, diagnostics: dict | None = None):
    return validation_failure(
        RecoveryFailureClass.BUSINESS_CONTRACT,
        repairable=True,
        candidate=None,
        raw_output="",
        errors=[
            RecoveryError(
                source="validator",
                location=["subtasks", 0],
                message="schema mismatch",
                code="schema_mismatch",
            )
        ],
        diagnostics=diagnostics,
    )


def test_runtime_schema_diffs_reports_exact_case_level_changes() -> None:
    diff = runtime_schema_diffs(_candidate())[0]

    assert diff["subtask_id"] == 3
    assert diff["expected_from_case_id"] == 1
    assert diff["expected_schema"]["n"]["value_type"] == "integer"
    mismatch = diff["mismatched_cases"][0]
    assert mismatch["case_id"] == 2
    assert mismatch["missing_parameters"] == ["mode"]
    assert mismatch["extra_parameters"] == ["minimum"]
    assert mismatch["type_or_category_mismatches"] == [
        {
            "name": "n",
            "expected": {"category": "size", "value_type": "integer"},
            "actual": {"category": "size", "value_type": "string"},
        }
    ]


def test_generation_profile_diffs_reports_categories_and_count() -> None:
    diff = generation_profile_diffs(_candidate())[0]

    assert diff["missing_categories"] == ["anti_algorithm", "boundary_edge"]
    assert diff["profile_count_sum"] == 3
    assert diff["expected_test_count"] == 3
    assert diff["runtime_case_count"] == 3
    assert diff["expected_case_ids"] == [1, 2, 3]
    assert diff["actual_case_ids"] == [1, 2, 3]


def test_generation_profile_diffs_reports_case_id_and_assignment_accounting() -> None:
    candidate = _candidate()
    subtask = candidate["subtasks"][0]
    subtask["generation_profiles"] = [
        {"id": "format", "category": "rules_format", "count": 1},
        {"id": "stress", "category": "anti_algorithm", "count": 1},
        {"id": "edge", "category": "boundary_edge", "count": 1},
    ]
    for case_id, profile_id in zip((4, 5, 6), ("format", "stress", "edge"), strict=True):
        case = subtask["runtime_parameters"][case_id - 4]
        case["case_id"] = case_id
        case["generation_profile_id"] = profile_id

    diff = generation_profile_diffs(candidate)[0]

    assert diff["expected_case_ids"] == [1, 2, 3]
    assert diff["actual_case_ids"] == [4, 5, 6]
    assert diff["profile_counts"] == {"format": 1, "stress": 1, "edge": 1}
    assert diff["assigned_profile_counts"] == {"format": 1, "stress": 1, "edge": 1}


def test_stage5_generation_plan_failure_routes_to_stage4_with_stage4_evidence() -> None:
    result = _failure(
        diagnostics={
            "execution": {
                "ok": False,
                "failure_category": "generation_plan",
                "checks": [],
            }
        }
    )
    locator = RecoveryProblemLocator()
    plan = locator.locate(5, result, {"subtasks": _candidate()["subtasks"]})
    context = RecoveryContextAssembler().build(
        {"subtasks": _candidate()["subtasks"]},
        result,
        plan,
    )

    assert plan.root_stage == 4
    assert plan.allows_stage(4)
    assert not plan.allows_stage(5)
    assert context["recovery_evidence"]["runtime_schema_diffs"][0]["subtask_id"] == 3


def test_stage5_solution_failure_requires_user_authorization() -> None:
    result = _failure(
        diagnostics={
            "execution": {
                "ok": False,
                "checks": [
                    {
                        "operation": "solve",
                        "role": "solution",
                        "result": {"ok": False},
                    }
                ],
            }
        }
    )

    plan = RecoveryProblemLocator().locate(5, result, {})

    assert plan.root_stage == 2
    assert plan.requires_user_authorization is True
    assert plan.write_grants == []
