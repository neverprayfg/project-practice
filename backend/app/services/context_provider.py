from __future__ import annotations

from typing import Any

from app.models import TaskType
from app.storage import ProjectStorage


class AgentContextProvider:
    def __init__(self, storage: ProjectStorage) -> None:
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
        if task_type in {TaskType.SUBTASK_PLAN, TaskType.CODE_DRAFT}:
            plan = self.storage.load_draft(project_id, 4)
            context["subtasks"] = (plan or {}).get("subtasks", [])
        if task_type == TaskType.CODE_DRAFT:
            code_revision = self.storage.current_revision(project_id)
            context["recovery_feedback"] = [
                item
                for item in self.storage.load_agent4_feedback(project_id)
                if item.get("workflow_revision") == record.workflow_revision
                and item.get("input_revision") == record.input_revision
                and item.get("subtasks_revision") == record.subtasks_revision
                and item.get("code_revision") == code_revision
            ]
        return context
