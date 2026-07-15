from __future__ import annotations

from app.models import SubtaskPlanDraft
from app.services.runtime_parameters import runtime_parameter_issues


def subtask_plan_issues(plan: SubtaskPlanDraft) -> list[str]:
    """Validate only the executable shape of user-configured generation plans."""

    return list(dict.fromkeys(runtime_parameter_issues(plan)))
