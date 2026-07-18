from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from app.models import GlobalInput, SubtaskPlanDraft, TestDataPlanDraft
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
    """Agent2 validates the fixed Markdown layout for the test-data plan."""

    def verify(self, candidate: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        try:
            model = TestDataPlanDraft.model_validate(candidate)
        except ValidationError as exc:
            return candidate, [f"测试数据生成方案不符合 Schema：{exc}"]
        plan = model.plan_markdown
        required_sections = {
            "constraints": "变量与合规约束",
            "test-matrix": "核心测试点矩阵",
            "blueprint-for-generator": "生成器逻辑实现大纲",
        }
        invalid: list[str] = []
        for tag, label in required_sections.items():
            matches = re.findall(rf"<{tag}>(.*?)</{tag}>", plan, flags=re.DOTALL)
            if len(matches) != 1 or not matches[0].strip():
                invalid.append(label)
        if invalid:
            return model.model_dump(mode="json", exclude={"issues"}), [
                "测试数据设计方案的固定标签缺失、重复或内容为空："
                + "、".join(invalid)
            ]
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
