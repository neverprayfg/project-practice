from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.models import GlobalInput, InputStructureDraft, SubtaskPlanDraft
from app.services.runtime_parameters import (
    runtime_parameter_issues,
    structure_tag_parameter_issues,
)
from app.services.structure_tag_catalog import StructureTagCatalog


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
    """Agent2 exposes tag ambiguity to the user instead of repairing it itself."""

    def __init__(self, tag_catalog: StructureTagCatalog) -> None:
        self.tag_catalog = tag_catalog

    def verify(self, candidate: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        try:
            model = InputStructureDraft.model_validate(candidate)
        except ValidationError as exc:
            return candidate, [f"输入结构结果不符合 Schema：{exc}"]
        issues = (
            self.tag_catalog.validate_structure_tags(model.structure_tags)
            if model.structure_tags
            else ["needs_tag_review：阶段三必须确认至少一个结构标签。"]
        )
        return model.model_dump(mode="json", exclude={"issues"}), issues


class Agent3Validator:
    """Agent3 rejects invalid plans; it does not push contradictions to Agent4."""

    def __init__(self, tag_catalog: StructureTagCatalog) -> None:
        self.tag_catalog = tag_catalog

    def verify(
        self, candidate: dict[str, Any], context: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        try:
            model = SubtaskPlanDraft.model_validate(candidate)
        except ValidationError as exc:
            return candidate, [f"子任务计划不符合 Schema：{exc}"]
        issues: list[str] = []
        if not context.get("subtasks") and len(model.subtasks) != 5:
            issues.append("首次规划必须生成 5 个子任务。")
        issues.extend(runtime_parameter_issues(model))
        global_tags = [
            str(item["tag_id"])
            for item in context.get("confirmed_structure_tags", [])
            if isinstance(item, dict) and item.get("tag_id")
        ]
        issues.extend(structure_tag_parameter_issues(model, global_tags, self.tag_catalog))
        return model.model_dump(mode="json", exclude={"issues"}), list(dict.fromkeys(issues))
