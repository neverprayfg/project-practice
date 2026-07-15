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
    Confirmation,
    JngenDocumentChoice,
    JngenDocumentSelection,
    SandboxResult,
    TaskType,
    WorkflowOutput,
)
from app.services.jngen_policy import jngen_usage_issues
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

    async def select_jngen_documents(
        self,
        context: dict[str, Any],
        available_filenames: list[str],
    ) -> JngenDocumentSelection:
        previously_selected = {
            str(item["filename"])
            for item in context.get("jngen_documentation", {}).get(
                "selected_documents", []
            )
            if isinstance(item, dict) and item.get("filename")
        }
        preferred = ["getting_started.md", "getopt.md", "array.md"]
        selected = [
            filename
            for filename in preferred
            if filename in available_filenames and filename not in previously_selected
        ][:2]
        if not selected:
            selected = [
                filename
                for filename in available_filenames
                if filename not in previously_selected
            ][:2]
        return JngenDocumentSelection(
            selected_documents=[
                JngenDocumentChoice(filename=filename, reason="测试夹具固定选择。")
                for filename in selected
            ],
            selection_complete=bool(previously_selected) or not selected,
        )

    async def run(
        self,
        task_type: TaskType,
        phase: str,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
        issues: list[str],
    ) -> WorkflowOutput:
        del issues
        if phase == "review":
            if execution.get("ok"):
                return WorkflowOutput(confirmation=Confirmation.PASS, result=candidate)
            return WorkflowOutput(
                confirmation=Confirmation.REVISE,
                result=candidate,
                issues=[str(execution.get("message") or "确定性检查未通过。")],
            )
        if task_type == TaskType.SUBTASK_PLAN:
            candidate = self._runtime_parameters(
                candidate or self._default_result(task_type, context)
            )
        if task_type == TaskType.CODE_DRAFT:
            generated = dict(candidate)
            if not candidate or jngen_usage_issues(candidate.get("generator_code", "")):
                generated = self._jngen_result(context)
            generated["constraint_coverage"] = self._constraint_coverage(context)
            candidate = generated
        result = candidate or self._default_result(task_type, context)
        return WorkflowOutput(confirmation=Confirmation.REVISE, result=result)

    @staticmethod
    def _jngen_result(context: dict[str, Any]) -> dict[str, Any]:
        parameter_names = sorted(
            {
                str(parameter["name"])
                for subtask in context.get("subtasks", [])
                for profile in subtask.get("runtime_parameters", [])
                for parameter in profile.get("parameters", [])
                if isinstance(parameter, dict) and parameter.get("name")
            }
        )
        reads = "".join(
            f'(void)getOpt("{name}");'
            for name in ["seed", "subtask", "case", *parameter_names]
        )
        return {
            "generator_code": (
                '#include "jngen.h"\n'
                "int main(int argc,char** argv){registerGen(argc,argv);parseArgs(argc,argv);"
                f"{reads}"
                "auto sample=Array::random(1,0,0);(void)sample;return 0;}"
            ),
            "validator_code": (
                '#include "testlib.h"\n'
                "int main(int argc,char** argv){registerValidation(argc,argv);"
                "inf.readEof();return 0;}"
            ),
            "constraint_coverage": [],
            "issues": [],
        }

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
    def _constraint_coverage(context: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "subtask_id": int(subtask["id"]),
                "case_id": int(profile["case_id"]),
                "parameter_names": [
                    str(parameter["name"]) for parameter in profile["parameters"]
                ],
                "structure_tags": list(
                    dict.fromkeys(
                        [
                            *(
                                str(item["tag_id"])
                                for item in context.get(
                                    "confirmed_structure_tags", []
                                )
                            ),
                            *(str(item) for item in subtask.get("subtask_tags", [])),
                        ]
                    )
                ),
                "generator_strategy": "按运行时参数构造测试数据。",
                "validator_strategy": "按已确认输入结构校验。",
                "boundary_or_special": "由测试点参数决定边界或特殊结构。",
            }
            for subtask in context.get("subtasks", [])
            for profile in subtask.get("runtime_parameters", [])
        ]

    @staticmethod
    def _default_result(task_type: TaskType, context: dict[str, Any]) -> dict[str, Any]:
        if task_type == TaskType.INPUT_NORMALIZATION:
            return dict(context["input"])
        if task_type == TaskType.INPUT_STRUCTURE:
            return {
                "template": "按题目与标程的读取顺序读取一个整数。",
                "structure_tags": [
                    {
                        "tag_id": "primitive.integer",
                        "applies_to": "输入整数 n",
                        "evidence": "标程从标准输入读取整数 n。",
                    }
                ],
                "issues": [],
            }
        if task_type == TaskType.SUBTASK_PLAN:
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
                ],
                "issues": [],
            }
        raise AssertionError(f"unexpected task type: {task_type}")


@pytest.fixture
def app_bundle(tmp_path: Path):
    sandbox = FakeSandbox(tmp_path)
    settings = Settings(
        app_env="test",
        storage_root=tmp_path,
        testlib_root=Path(__file__).parents[2] / "testlib",
        jngen_root=Path(__file__).parents[2] / "jngen",
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
