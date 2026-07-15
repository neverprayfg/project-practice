from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.config import Settings, get_settings
from app.errors import AppError, install_error_handlers
from app.services.candidate_verifier import AgentCandidateVerifier
from app.services.context_provider import AgentContextProvider
from app.services.dataset import DatasetService
from app.services.jngen_document_context import JngenDocumentContext
from app.services.langgraph_runner import LangGraphAgentRunner
from app.services.model_client import AgentModel
from app.services.model_configuration import ModelConfigurationService
from app.services.pipeline import PipelineService
from app.services.project_service import ProjectService
from app.services.sandbox import DockerSandbox, Sandbox, UnavailableSandbox
from app.services.structure_tag_catalog import StructureTagCatalog
from app.storage import ProjectStorage


def create_app(
    settings: Settings | None = None,
    *,
    model: AgentModel | None = None,
    sandbox: Sandbox | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    storage = ProjectStorage(settings.storage_root)
    jngen_document_root = Path(__file__).parent / "jngen_doc_context"
    tag_catalog = StructureTagCatalog(jngen_document_root)
    projects = ProjectService(storage, tag_catalog)
    model_configuration = ModelConfigurationService(settings, storage)
    if sandbox is None:
        try:
            sandbox = DockerSandbox(settings)
        except AppError:
            sandbox = UnavailableSandbox()
    model = model or model_configuration.build_model()
    contexts = AgentContextProvider(storage, tag_catalog)
    jngen_documents = JngenDocumentContext(jngen_document_root, tag_catalog)
    verifier = AgentCandidateVerifier(settings, storage, sandbox, tag_catalog)
    agent_runner = LangGraphAgentRunner(
        settings,
        storage,
        model,
        verifier,
        jngen_documents,
    )
    pipeline = PipelineService(storage, projects, agent_runner, contexts, sandbox)
    datasets = DatasetService(settings, storage, projects, sandbox)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        projects.recover_interrupted_checks()
        await agent_runner.start()
        try:
            yield
        finally:
            await agent_runner.close()

    app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    app.state.settings = settings
    app.state.storage = storage
    app.state.projects = projects
    app.state.sandbox = sandbox
    app.state.model = model
    app.state.model_configuration = model_configuration
    app.state.agent_runner = agent_runner
    app.state.contexts = contexts
    app.state.tag_catalog = tag_catalog
    app.state.pipeline = pipeline
    app.state.datasets = datasets
    install_error_handlers(app)
    app.include_router(router)
    frontend_candidates = (
        Path(__file__).resolve().parent.parent / "frontend",
        Path(__file__).resolve().parents[2] / "demo前端样式设计",
    )
    frontend_root = next((path for path in frontend_candidates if path.is_dir()), None)
    if frontend_root is not None:
        app.mount("/", StaticFiles(directory=frontend_root, html=True), name="frontend")
    return app


app = create_app()
