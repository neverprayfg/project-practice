from __future__ import annotations

from typing import Any

from app.models import TaskType
from app.storage import ProjectStorage


class AgentContextProvider:
    def __init__(
        self,
        storage: ProjectStorage,
    ) -> None:
        self.storage = storage

    def build(self, project_id: str, task_type: TaskType) -> dict[str, Any]:
        input_data = self.storage.load_input(project_id)
        record = self.storage.load_record(project_id)
        context: dict[str, Any] = {
            "input": input_data.model_dump(mode="json"),
            "input_revision": input_data.revision,
            "workflow_revision": record.workflow_revision,
            "library_guidance": [],
        }
        if task_type == TaskType.SUBTASK_PLAN:
            plan = self.storage.load_draft(project_id, 4)
            context["subtasks"] = (plan or {}).get("subtasks", [])
            context["subtasks_revision"] = record.subtasks_revision
        return context

    def build_agent4(self, project_id: str) -> dict[str, Any]:
        input_data = self.storage.load_input(project_id)
        record = self.storage.load_record(project_id)
        plan = self.storage.load_draft(project_id, 4) or {}
        return {
            "input": input_data.model_dump(mode="json"),
            "input_revision": input_data.revision,
            "workflow_revision": record.workflow_revision,
            "subtasks": plan.get("subtasks", []),
            "subtasks_revision": record.subtasks_revision,
        }
