from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from app.errors import AppError
from app.models import (
    DraftUpdate,
    GenerateRequest,
    ModelConfigurationUpdate,
    PreviewRequest,
    ProjectCreate,
    SolutionUpdate,
    StageRunRequest,
    UserConfirmation,
    ValidateRequest,
)
from app.services.timing_report import summarize_agent4_timings

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    model_config = request.app.state.model_configuration.public_view()
    pipeline = request.app.state.pipeline
    return {
        "status": "ok",
        "environment": settings.app_env,
        "workflow": "langgraph",
        "model_name": model_config["model_name"],
        "model_api_configured": model_config["api_key_configured"],
        "sandbox": type(request.app.state.sandbox).__name__,
        "active_tasks": pipeline.has_active_tasks(),
    }


@router.get("/api/settings/model")
async def get_model_configuration(request: Request) -> dict:
    return request.app.state.model_configuration.public_view()


@router.get("/api/structure-tags")
async def get_structure_tags(request: Request) -> dict:
    return request.app.state.tag_catalog.public_view()


@router.put("/api/settings/model")
async def update_model_configuration(payload: ModelConfigurationUpdate, request: Request) -> dict:
    pipeline = request.app.state.pipeline
    if pipeline.has_active_tasks():
        raise AppError(
            "MODEL_CONFIG_BUSY",
            "当前有任务正在运行，请等待任务完成后再修改模型配置。",
            status_code=409,
        )
    service = request.app.state.model_configuration
    async with service.lock:
        service.update(payload)
        model = service.build_model()
        request.app.state.model = model
        await request.app.state.agent_graphs.replace_model(model)
        return service.public_view()


@router.post("/api/settings/model/test")
async def test_model_configuration(payload: ModelConfigurationUpdate, request: Request) -> dict:
    service = request.app.state.model_configuration
    async with service.lock:
        return await service.test_connection(payload)


@router.post("/api/projects", status_code=201)
async def create_project(payload: ProjectCreate, request: Request) -> dict:
    record = request.app.state.projects.create(payload)
    await request.app.state.pipeline.normalize_input(record.project_id)
    return request.app.state.projects.get(record.project_id).model_dump(mode="json")


@router.get("/api/projects/{project_id}")
async def get_project(project_id: str, request: Request) -> dict:
    storage = request.app.state.storage
    record = request.app.state.projects.get(project_id)
    drafts = {str(stage): storage.load_draft(project_id, stage) for stage in (3, 4, 5)}
    return {
        "project": record.model_dump(mode="json"),
        "input": storage.load_input(project_id).model_dump(mode="json"),
        "subtasks": (drafts["4"] or {}).get("subtasks", []),
        "drafts": drafts,
        "structure_tag_catalog": request.app.state.tag_catalog.public_view(),
    }


@router.get("/api/projects/{project_id}/stage5/timings")
async def get_stage5_timings(project_id: str, request: Request, run_id: str | None = None) -> dict:
    request.app.state.projects.get(project_id)
    events = request.app.state.storage.load_agent4_timings(project_id)
    if run_id is not None:
        events = [event for event in events if event.get("run_id") == run_id]
    return {
        "events": events,
        "runs": summarize_agent4_timings(events),
    }


@router.get("/api/projects/{project_id}/stage5/decisions")
async def get_stage5_decisions(
    project_id: str, request: Request, run_id: str | None = None
) -> dict:
    request.app.state.projects.get(project_id)
    storage = request.app.state.storage
    events = storage.load_agent4_decisions(project_id)
    if run_id is not None:
        events = [event for event in events if event.get("run_id") == run_id]
    return {
        "events": events,
        "counterexample_ledger": storage.load_agent4_ledger(project_id),
        "last_valid_candidate": storage.load_agent4_last_valid_candidate(project_id),
    }


@router.put("/api/projects/{project_id}/solution")
async def update_solution(project_id: str, payload: SolutionUpdate, request: Request) -> dict:
    record = request.app.state.projects.update_solution(project_id, payload)
    return record.model_dump(mode="json")


@router.get("/api/projects/{project_id}/solution")
async def get_solution(project_id: str, request: Request) -> dict:
    project_dir = request.app.state.storage.project_dir(project_id)
    path = project_dir / "source" / "solution.cpp"
    if not path.is_file():
        raise AppError("PROJECT_NOT_FOUND", "solution does not exist", status_code=404)
    return {"solution_code": path.read_text(encoding="utf-8")}


@router.post("/api/projects/{project_id}/solution/compile")
async def compile_solution(project_id: str, request: Request) -> dict:
    return await request.app.state.pipeline.compile_solution(project_id)


@router.put("/api/projects/{project_id}/stages/{stage}/draft")
async def update_draft(project_id: str, stage: int, payload: DraftUpdate, request: Request) -> dict:
    pipeline = request.app.state.pipeline
    lock = pipeline.project_lock(project_id)
    if lock.locked():
        raise AppError(
            "PROJECT_BUSY",
            "another operation is running for this project",
            stage=stage,
            status_code=409,
        )
    async with lock:
        draft = request.app.state.projects.save_user_draft(project_id, stage, payload.draft)
    return {"draft": draft}


@router.post("/api/projects/{project_id}/stages/{stage}/run")
async def run_stage(
    project_id: str, stage: int, payload: StageRunRequest, request: Request
) -> dict:
    lock = request.app.state.pipeline.project_lock(project_id)
    if lock.locked():
        raise AppError(
            "PROJECT_BUSY",
            "project already has a running task",
            stage=stage,
            status_code=409,
        )
    return await request.app.state.pipeline.run_stage(project_id, stage, payload.task_type)


@router.post("/api/projects/{project_id}/stages/{stage}/confirm")
async def confirm_stage(
    project_id: str, stage: int, _payload: UserConfirmation, request: Request
) -> dict:
    pipeline = request.app.state.pipeline
    lock = pipeline.project_lock(project_id)
    if lock.locked():
        raise AppError(
            "PROJECT_BUSY",
            "project already has a running task",
            stage=stage,
            status_code=409,
        )
    record = await pipeline.confirm_stage(project_id, stage)
    return record.model_dump(mode="json")


@router.post("/api/projects/{project_id}/preview")
async def preview(project_id: str, payload: PreviewRequest, request: Request) -> dict:
    lock = request.app.state.pipeline.project_lock(project_id)
    if lock.locked():
        raise AppError(
            "PROJECT_BUSY", "project already has a running task", stage=5, status_code=409
        )
    return await request.app.state.pipeline.preview(
        project_id, payload.subtask_id, payload.case_id, payload.seed
    )


@router.post("/api/projects/{project_id}/generate")
async def generate_dataset(project_id: str, payload: GenerateRequest, request: Request) -> dict:
    pipeline = request.app.state.pipeline
    lock = pipeline.project_lock(project_id)
    if lock.locked():
        raise AppError("PROJECT_BUSY", "项目已有任务正在运行。", stage=6, status_code=409)
    async with lock:
        return await request.app.state.datasets.generate_inputs(
            project_id,
            payload.base_seed,
            payload.selected_subtask_ids,
        )


@router.post("/api/projects/{project_id}/validate")
async def validate_dataset(project_id: str, payload: ValidateRequest, request: Request) -> dict:
    pipeline = request.app.state.pipeline
    lock = pipeline.project_lock(project_id)
    if lock.locked():
        raise AppError("PROJECT_BUSY", "项目已有任务正在运行。", stage=7, status_code=409)
    async with lock:
        return await request.app.state.datasets.validate_and_solve(
            project_id,
            payload.selected_subtask_ids,
        )


@router.get("/api/projects/{project_id}/export", response_class=FileResponse)
async def export_dataset(project_id: str, request: Request) -> FileResponse:
    pipeline = request.app.state.pipeline
    lock = pipeline.project_lock(project_id)
    if lock.locked():
        raise AppError("PROJECT_BUSY", "项目已有任务正在运行。", stage=7, status_code=409)
    async with lock:
        archive = await asyncio.to_thread(request.app.state.datasets.export, project_id)
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"{project_id}-dataset.zip",
    )
