from __future__ import annotations

from typing import Any

from app.models import StageStatus, TaskType
from app.services.structure_tag_catalog import StructureTagCatalog
from app.storage import ProjectStorage


class AgentContextProvider:
    def __init__(
        self,
        storage: ProjectStorage,
        tag_catalog: StructureTagCatalog | None = None,
    ) -> None:
        self.storage = storage
        self.tag_catalog = tag_catalog or StructureTagCatalog()

    def build(self, project_id: str, task_type: TaskType) -> dict[str, Any]:
        input_data = self.storage.load_input(project_id)
        record = self.storage.load_record(project_id)
        context: dict[str, Any] = {
            "input": input_data.model_dump(mode="json"),
            "input_revision": input_data.revision,
            "workflow_revision": record.workflow_revision,
            "library_guidance": [],
        }
        if task_type in {
            TaskType.INPUT_STRUCTURE,
            TaskType.SUBTASK_PLAN,
            TaskType.CODE_DRAFT,
        }:
            context["structure_tag_catalog"] = self.tag_catalog.model_view()
        if task_type in {TaskType.SUBTASK_PLAN, TaskType.CODE_DRAFT}:
            structure = self.storage.load_draft(project_id, 3) or {}
            context["confirmed_structure_tags"] = (
                structure.get("structure_tags", [])
                if record.stages[3].status == StageStatus.PASSED
                else []
            )
            context["structure_tag_catalog_version"] = self.tag_catalog.version
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
