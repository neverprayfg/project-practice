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

Agent output never invokes tools or Docker directly. Agent1--4 compile four independent LangGraph graphs with separate states, prompts, validators, and failure policies while sharing infrastructure. Stage 3 selects global input-structure tags from the versioned `app/structure_context/tag_catalog.json` catalog, and stage 4 adds per-subtask tags. Stage 5 performs an upstream contract preflight, converts the confirmed contract into `ProofObligation` records, and requires code plus an auditable `implementation_mapping`.

Agent4 uses deterministic verification followed by exactly one open, read-only semantic audit. Reviews only report defects; repairs target one stable defect ID and are followed by deterministic and historical-counterexample replay. A patch is accepted only when it closes the target, reduces blockers, or advances the validation level without reopening a closed defect. Otherwise it is rolled back; a defect that survives one repair stops the run. Every Agent4 model call receives the strict recursive role-library JSON contract; the backend-only document index is never sent as a legacy model context. Stage 6/7 failures are normalized directly into the same stable-defect counterexample ledger, including replayable seed, case, and runtime arguments; the old natural-language feedback file is gone. Counterexamples, accepted revisions, candidate-scoped validation caches, and the full decision chain are persisted under each project. Old Agent4 drafts, checkpoints, ledgers, and caches are rejected rather than migrated.
