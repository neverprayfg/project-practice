from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.models import GlobalInput, InputStructureDraft, SubtaskPlanDraft
from app.services.stage4_plan import subtask_plan_issues


class Agent1Validator:
    """Agent1 fails closed when normalization changes authoritative facts."""

    def verify(
        self, candidate: dict[str, Any], context: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        try:
            model = GlobalInput.model_validate(candidate.get("input", candidate))
        except ValidationError as exc:
            return candidate, [f"INPUT 规范化结果不符合 Schema：{exc}"]
        original = GlobalInput.model_validate(context["input"])
        issues: list[str] = []
        if model.problem.description != original.problem.description:
            issues.append("Agent1 不得修改题目原文。")
        if model.problem.difficulty != original.problem.difficulty:
            issues.append("Agent1 不得修改题目难度。")
        if model.solution.source != original.solution.source:
            issues.append("Agent1 不得修改标程源码。")
        return model.model_dump(mode="json"), issues


class Agent2Validator:
    """Agent2 validates only the human-readable input template."""

    def verify(self, candidate: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        try:
            model = InputStructureDraft.model_validate(candidate)
        except ValidationError as exc:
            return candidate, [f"输入结构结果不符合 Schema：{exc}"]
        return model.model_dump(mode="json", exclude={"issues"}), []


class Agent3Validator:
    """Agent3 rejects invalid plans; it does not push contradictions to Agent4."""

    def verify(
        self, candidate: dict[str, Any], context: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        try:
            model = SubtaskPlanDraft.model_validate(candidate)
        except ValidationError as exc:
            return candidate, [f"子任务计划不符合 Schema：{exc}"]
        issues: list[str] = []
        issues.extend(subtask_plan_issues(model))
        return model.model_dump(mode="json", exclude={"issues"}), list(dict.fromkeys(issues))
