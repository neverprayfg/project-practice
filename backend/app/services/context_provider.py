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
        model_input = input_data.model_dump(mode="json", exclude={"input_structure"})
        if task_type == TaskType.TEST_DATA_PLAN:
            model_input["solution"] = {
                key: value
                for key, value in model_input["solution"].items()
                if key != "compile"
            }
        elif task_type == TaskType.SUBTASK_PLAN:
            model_input.pop("solution", None)
        context: dict[str, Any] = {
            "input": model_input,
            "input_revision": input_data.revision,
            "workflow_revision": record.workflow_revision,
            "library_guidance": [],
        }
        if task_type == TaskType.SUBTASK_PLAN:
            test_data_plan = self.storage.load_draft(project_id, 3)
            context["test_data_plan"] = test_data_plan or {}
            context["subtasks_revision"] = record.subtasks_revision
        return context

    def build_agent4(self, project_id: str) -> dict[str, Any]:
        input_data = self.storage.load_input(project_id)
        record = self.storage.load_record(project_id)
        plan = self.storage.load_draft(project_id, 4) or {}
        test_data_plan = self.storage.load_draft(project_id, 3) or {}
        model_input = input_data.model_dump(mode="json", exclude={"input_structure"})
        model_input.pop("solution", None)
        model_input["problem"].pop("difficulty", None)
        context = {
            "input": model_input,
            "input_revision": input_data.revision,
            "workflow_revision": record.workflow_revision,
            "subtasks": plan.get("subtasks", []),
            "subtasks_revision": record.subtasks_revision,
            "test_data_plan": test_data_plan,
        }
        details = record.last_error.get("details") if isinstance(record.last_error, dict) else None
        if isinstance(details, dict) and isinstance(details.get("execution"), dict):
            context["deterministic_failure"] = details["execution"]
        return context
