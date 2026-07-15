# Contest Dataset LangGraph Backend

The backend implements the workflow in `tmp/agentic_workflow_specification.md` with Python 3.12 and LangGraph 1.2.9. LangGraph uses a persistent SQLite checkpointer under the project storage volume. Dify is not part of the runtime.

## Local development

```bash
uv sync --dev
cp ../.env.example ../.env
MODEL_MODE=mock uv run uvicorn app.main:app --reload
```

Remote mode uses `MODEL_BASE_URL`, `MODEL_API_KEY`, and `MODEL_NAME` with an OpenAI-compatible chat-completions API.

## API flow

1. `POST /api/projects` runs Agent1 and creates the structured `INPUT`.
2. `POST /api/projects/{id}/solution/compile` performs deterministic stage 2.
3. Run, edit, and jointly confirm stages 3, 4, and 5.
4. Preview a subtask and seed with `POST /api/projects/{id}/preview`.
5. Generate selected subtasks with `POST /api/projects/{id}/generate`.
6. Validate and solve with `POST /api/projects/{id}/validate`.
7. Export generator, validator, and paired data with `GET /api/projects/{id}/export`.

Agent output never invokes tools or Docker directly. Before stage 5 generation, the backend performs a bounded multi-round selection from `app/jngen_doc_context/`: every round receives all filenames, later rounds also receive previously read contents, and only unread filenames are valid choices. The selector may finish early; otherwise the backend ends selection at its configured budget before Agent4 receives the selected document contents. Compilation and execution remain deterministic backend operations.
