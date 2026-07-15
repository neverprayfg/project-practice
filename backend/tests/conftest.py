from __future__ import annotations

import os
from pathlib import Path
from typing import Any

os.environ.setdefault("MODEL_API_KEY", "test-import-key")

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import (
    CodeGenerationSubmission,
    CodeRepairPatch,
    GlobalInput,
    InputStructureDraft,
    SandboxResult,
    SemanticAudit,
    SubtaskPlanDraft,
    TargetedDefectCheck,
)
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


class DeterministicTestModel:
    """Test-only agent injected explicitly; it is not selectable at runtime."""

    async def agent1_normalize(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> GlobalInput:
        return GlobalInput.model_validate(candidate or context["input"])

    async def agent2_structure(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> InputStructureDraft:
        del context
        return InputStructureDraft.model_validate(
            candidate
            or {
                "template": "按题目与标程的读取顺序读取一个整数。",
                "structure_tags": [
                    {
                        "tag_id": "primitive.integer",
                        "applies_to": "输入整数 n",
                        "evidence": "标程从标准输入读取整数 n。",
                    }
                ],
            }
        )

    async def agent3_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft:
        value = candidate or self._default_plan()
        return SubtaskPlanDraft.model_validate(self._runtime_parameters(value))

    async def agent4_generate(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> CodeGenerationSubmission:
        del candidate
        return self._code_draft(context)

    async def agent4_audit(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
    ) -> SemanticAudit:
        del context, candidate, execution
        return SemanticAudit(defects=[])

    async def agent4_repair(self, *args: Any, **kwargs: Any) -> CodeRepairPatch:
        raise AssertionError("valid test candidate must not need repair")

    async def agent4_recheck(self, *args: Any, **kwargs: Any) -> TargetedDefectCheck:
        raise AssertionError("valid test candidate has no historical semantic defect")

    @staticmethod
    def _code_draft(context: dict[str, Any]) -> CodeGenerationSubmission:
        parameter_names = sorted(
            {
                str(parameter["name"])
                for subtask in context.get("subtasks", [])
                for profile in subtask.get("runtime_parameters", [])
                for parameter in profile.get("parameters", [])
                if isinstance(parameter, dict) and parameter.get("name")
            }
        )
        generator_lines = [
            '#include "jngen.h"',
            "#include <iostream>",
            "int main(int argc,char** argv){",
            "registerGen(argc,argv); parseArgs(argc,argv);",
        ]
        for name in parameter_names:
            generator_lines.extend(
                [
                    f'auto {name} = getOpt("{name}");',
                    f"std::cerr << {name};",
                ]
            )
        generator_lines.extend(
            ["auto sample=Array::random(1,0,0);", "std::cout << sample;", "return 0;}"]
        )
        generator_code = "\n".join(generator_lines)
        validator_code = (
            '#include "testlib.h"\n'
            "int main(int argc,char** argv){registerValidation(argc,argv);"
            "inf.readEof();return 0;}"
        )
        obligations = context["proof_obligations"]
        document = context["jngen_documentation"]["selected_documents"][0]
        symbol = document["symbols"][0]
        mapping = [
            {
                "constraint_id": obligation["constraint_id"],
                "locations": [
                    {
                        "target_file": "generator.cpp",
                        "symbol": "main",
                        "line_start": 3,
                        "line_end": len(generator_lines),
                    }
                ],
                "used_parameters": parameter_names,
                "document_evidence": [
                    {
                        "filename": document["filename"],
                        "symbol": symbol,
                    }
                ],
                "test_strategy": "按约束和测试点参数显式构造。",
            }
            for obligation in obligations
        ]
        return CodeGenerationSubmission.model_validate(
            {
                "generator_code": generator_code,
                "validator_code": validator_code,
                "implementation_mapping": mapping,
            }
        )

    @staticmethod
    def _runtime_parameters(candidate: dict[str, Any]) -> dict[str, Any]:
        result = dict(candidate)
        subtasks = []
        for raw in result.get("subtasks", []):
            subtask = dict(raw)
            if not subtask.get("runtime_parameters"):
                subtask["runtime_parameters"] = [
                    {
                        "case_id": case_id,
                        "parameters": [
                            {"name": "scale", "value": case_id, "category": "size"},
                            {
                                "name": "profile",
                                "value": f"subtask_{subtask['id']}",
                                "category": "structure",
                            },
                        ],
                    }
                    for case_id in range(1, int(subtask["test_count"]) + 1)
                ]
            subtasks.append(subtask)
        result["subtasks"] = subtasks
        return result

    @staticmethod
    def _default_plan() -> dict[str, Any]:
        return {
            "subtasks": [
                {
                    "id": index,
                    "constraints": f"第 {index} 个规模梯度。",
                    "test_count": 1,
                    "expected_complexity": "O(n)",
                    "special_cases": [],
                    "subtask_tags": [],
                }
                for index in range(1, 6)
            ]
        }


@pytest.fixture
def app_bundle(tmp_path: Path):
    sandbox = FakeSandbox(tmp_path)
    settings = Settings(
        app_env="test",
        storage_root=tmp_path,
    )
    app = create_app(settings, model=DeterministicTestModel(), sandbox=sandbox)
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
