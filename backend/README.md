# Contest Dataset LangGraph Backend

The backend implements the workflow in `tmp/agentic_workflow_specification.md` with Python 3.12 and LangGraph 1.2.9. LangGraph uses a persistent SQLite checkpointer under the project storage volume. Dify is not part of the runtime.

## Local development

```bash
uv sync --dev
cp ../.env.example ../.env
uv run uvicorn app.main:app --reload
```

The backend starts without an API key. Model-backed generation and review return `MODEL_NOT_CONFIGURED` until an OpenAI-compatible model connection is configured in the UI or environment.

## API flow

1. `POST /api/projects` runs Agent1 and creates the structured `INPUT`.
2. `POST /api/projects/{id}/solution/compile` performs deterministic stage 2.
3. Run, edit, and jointly confirm stages 3, 4, and 5.
4. Preview a subtask and seed with `POST /api/projects/{id}/preview`.
5. Generate selected subtasks with `POST /api/projects/{id}/generate`.
6. Validate and solve with `POST /api/projects/{id}/validate`.
7. Export generator, validator, and paired data with `GET /api/projects/{id}/export`.

Agent output never invokes tools or Docker directly. Stage 3 selects input-structure tags from the versioned `app/jngen_doc_context/tag_catalog.json` catalog and asks the user to confirm them together with the input template. Stage 5 deterministically resolves jngen documents from those confirmed tags. Old projects without confirmed tags must return to stage 3; `AGENT_ALLOW_LEGACY_KEYWORD_ROUTING=true` temporarily enables the legacy keyword/model-selection recovery path during migration. Compilation and execution remain deterministic backend operations.
