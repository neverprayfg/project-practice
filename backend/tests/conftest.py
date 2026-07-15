from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import SandboxResult
from app.services.sandbox import (
    GenerationJob,
    GenerationOutcome,
    ValidationJob,
    ValidationOutcome,
)


class FakeSandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple] = []
        self.batch_calls: list[tuple[str, int]] = []
        self.fail_validation = False

    async def compile(self, project_id: str, role: str) -> SandboxResult:
        self.calls.append(("compile", project_id, role))
        return SandboxResult(ok=True, exit_code=0)

    async def generate(
        self, project_id: str, subtask_id: int, seed: int, output_relative: str
    ) -> SandboxResult:
        self.calls.append(("generate", project_id, subtask_id, seed, output_relative))
        output = self.root / project_id / output_relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"{seed}\n", encoding="utf-8")
        return SandboxResult(
            ok=True,
            exit_code=0,
            output_file=output_relative,
        )

    async def generate_batch(
        self, project_id: str, jobs: list[GenerationJob]
    ) -> list[GenerationOutcome]:
        self.batch_calls.append(("generate", len(jobs)))
        return [
            GenerationOutcome(
                job.output_relative,
                await self.generate(
                    project_id,
                    job.subtask_id,
                    job.seed,
                    job.output_relative,
                ),
            )
            for job in jobs
        ]

    async def validate(self, project_id: str, input_relative: str) -> SandboxResult:
        self.calls.append(("validate", project_id, input_relative))
        return SandboxResult(
            ok=not self.fail_validation,
            exit_code=1 if self.fail_validation else 0,
            stderr="rejected" if self.fail_validation else "",
        )

    async def solve(
        self, project_id: str, input_relative: str, output_relative: str
    ) -> SandboxResult:
        self.calls.append(("solve", project_id, input_relative, output_relative))
        output = self.root / project_id / output_relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("answer\n", encoding="utf-8")
        return SandboxResult(ok=True, exit_code=0, output_file=output_relative)

    async def validate_solve_batch(
        self, project_id: str, jobs: list[ValidationJob]
    ) -> list[ValidationOutcome]:
        self.batch_calls.append(("validate_solve", len(jobs)))
        outcomes = []
        for job in jobs:
            validated = await self.validate(project_id, job.input_relative)
            solved = (
                await self.solve(project_id, job.input_relative, job.output_relative)
                if validated.ok
                else None
            )
            outcomes.append(
                ValidationOutcome(
                    job.input_relative,
                    job.output_relative,
                    validated,
                    solved,
                )
            )
        return outcomes


@pytest.fixture
def app_bundle(tmp_path: Path):
    sandbox = FakeSandbox(tmp_path)
    settings = Settings(
        app_env="test",
        storage_root=tmp_path,
        testlib_root=Path(__file__).parents[2] / "testlib",
        jngen_root=Path(__file__).parents[2] / "jngen",
        model_mode="mock",
    )
    app = create_app(settings, sandbox=sandbox)
    with TestClient(app) as client:
        yield client, sandbox


@pytest.fixture
def created_project(app_bundle: tuple[TestClient, FakeSandbox]) -> tuple[TestClient, str]:
    client, _sandbox = app_bundle
    response = client.post(
        "/api/projects",
        json={
            "problem_description": "Read n and print n.",
            "solution_code": "#include <iostream>\nint main(){int n;std::cin>>n;std::cout<<n;}",
            "difficulty": "easy",
        },
    )
    assert response.status_code == 201
    return client, response.json()["project_id"]
