from __future__ import annotations

# ruff: noqa: E501
import os
from pathlib import Path
from typing import Any

os.environ.setdefault("MODEL_API_KEY", "test-import-key")

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import (
    GeneratorAnalysisDraft,
    GeneratorAuditDraft,
    GlobalInput,
    InputNormalizationDraft,
    SandboxResult,
    StageInstructionDecision,
    SubtaskPlanDraft,
    TestDataPlanDraft,
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

    async def classify_stage_instruction(
        self,
        stage: int,
        context: dict[str, Any],
        candidate: dict[str, Any],
        instruction: str,
    ) -> StageInstructionDecision:
        del context, candidate
        revision_words = ("修改", "调整", "增加", "删除", "改为", "修复")
        if not any(word in instruction for word in revision_words):
            return StageInstructionDecision(
                action="answer",
                answer=f"这是阶段 {stage} 的一次性回答。",
                target="none",
            )
        target = "current_artifact"
        if stage == 5:
            mentions_generator = "generator" in instruction
            mentions_validator = "validator" in instruction
            target = (
                "both"
                if mentions_generator == mentions_validator
                else "generator" if mentions_generator else "validator"
            )
        return StageInstructionDecision(
            action="revise",
            answer="已按本次意见修改当前阶段产物。",
            target=target,
        )

    async def agent1_normalize(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> InputNormalizationDraft:
        value = GlobalInput.model_validate(candidate or context["input"])
        return InputNormalizationDraft(
            project_name=next(
                (
                    line.lstrip("#").strip()[:40]
                    for line in value.problem.description.splitlines()
                    if line.lstrip("#").strip()
                ),
                "竞赛测试数据",
            ),
            input_description=(
                value.problem.input_description
                if value.problem.input_description != "未提供"
                else "按题面说明读取一个测试点的全部输入字段。"
            ),
            output_description=value.problem.output_description,
            samples=value.problem.samples,
        )

    async def repair_solution(
        self, context: dict[str, Any], source: str, execution: dict[str, Any]
    ) -> str:
        del context, execution
        return source

    async def agent2_test_data_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> TestDataPlanDraft:
        del context, candidate
        return TestDataPlanDraft.model_validate(
            {
                "plan_markdown": """# P0 测试数据生成方案

<constraints>
## 1. 变量与合规约束 (YAML)
```yaml
variables:
  N: { min: 1, max: 10, type: "int" }
```
</constraints>

<test-matrix>
## 2. 核心测试点矩阵 (Markdown Table)
| 测试点编号 | 测试目的/卡常目标 | N 的规模 | W 的规模 | 特殊拓扑结构设计 (如链、星、环、孤立点分布描述) | 边权分布规律 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| #1 (边界) | 极小值 | N=1 | 不适用 | 单元素 | 不适用 |
</test-matrix>

<blueprint-for-generator>
## 3. 生成器逻辑实现大纲 (Generator Blueprint)
1. **测试点 #1 的生成逻辑**：
   - 步骤一：输出最小合法输入。
</blueprint-for-generator>""",
            }
        )

    async def agent2_apply_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> TestDataPlanDraft:
        del context
        markdown = str(candidate.get("plan_markdown") or "")
        if not markdown:
            return await self.agent2_test_data_plan({}, {})
        return TestDataPlanDraft(
            plan_markdown=markdown.replace(
                "</constraints>", f"\n用户意见：{instruction}\n</constraints>", 1
            )
        )

    async def agent3_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft:
        del context, candidate
        return SubtaskPlanDraft.model_validate(self._runtime_parameters(self._default_plan()))

    async def agent3_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft:
        del context
        return SubtaskPlanDraft.model_validate(self._runtime_parameters(candidate))

    async def agent3_apply_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> SubtaskPlanDraft:
        del context, instruction
        if not candidate:
            return await self.agent3_plan({}, {})
        revised = dict(candidate)
        revised["subtasks"] = [dict(item) for item in candidate.get("subtasks", [])]
        if revised["subtasks"]:
            revised["subtasks"][0]["expected_complexity"] = "O(n log n)"
        return SubtaskPlanDraft.model_validate(revised)

    async def agent4_analyze_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> GeneratorAnalysisDraft:
        del candidate
        strategies = []
        for subtask in context.get("subtasks", []):
            profiles = {
                profile["id"]: profile for profile in subtask.get("generation_profiles", [])
            }
            observed = set()
            for runtime in subtask.get("runtime_parameters", []):
                profile = profiles[runtime["generation_profile_id"]]
                parameters = {
                    parameter["name"]: parameter["value"]
                    for parameter in runtime.get("parameters", [])
                }
                key = (profile["id"], parameters["construction_mode"])
                if key in observed:
                    continue
                observed.add(key)
                strategies.append(
                    {
                        "subtask_id": subtask["id"],
                        "generation_profile_id": profile["id"],
                        "profile_category": profile["category"],
                        "construction_mode": parameters["construction_mode"],
                        "goal": profile["goal"],
                        "runtime_parameters": sorted(
                            set(profile.get("parameter_names", []))
                            | {"construction_mode"}
                        ),
                        "input_invariants": ["满足题面输入范围和字段关系"],
                        "construction_steps": ["按运行参数构造一个合法测试点"],
                        "post_checks": ["输出前断言全部字段满足范围"],
                        "seed_policy": (
                            "fixed"
                            if parameters["construction_mode"] == "fixed"
                            else "diverse"
                        ),
                        "variation_dimensions": (
                            []
                            if parameters["construction_mode"] == "fixed"
                            else ["seed-controlled witness choice"]
                        ),
                        "complexity_target": profile["goal"],
                    }
                )
        return GeneratorAnalysisDraft.model_validate(
            {
                "input_constraints": ["遵守题面输入约束"],
                "solution_branch_risks": ["覆盖标程的关键条件分支"],
                "overflow_and_resource_guards": ["乘法前检查上界"],
                "strategies": strategies,
            }
        )

    async def agent4_revise_generator_analysis(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        validation_errors: list[str],
    ) -> GeneratorAnalysisDraft:
        del candidate, validation_errors
        return await self.agent4_analyze_generator(context, {})

    async def agent4_audit_generator(
        self,
        context: dict[str, Any],
        generator_code: str,
    ) -> GeneratorAuditDraft:
        del context, generator_code
        return GeneratorAuditDraft(passed=True, issues=[])

    async def agent4_generate_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> str:
        del candidate
        return self._generator_submission(context)

    async def agent4_generate_validator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> str:
        del candidate
        return self._validator_submission(context)

    async def agent4_repair_generator(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("valid test candidate must not need repair")

    async def agent4_repair_validator(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("valid test candidate must not need repair")

    async def agent4_apply_generator_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> str:
        del context, instruction
        return candidate["generator_code"] + "\n// user instruction applied"

    async def agent4_apply_validator_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> str:
        del context, instruction
        return candidate["validator_code"] + "\n// user instruction applied"

    @staticmethod
    def _generator_submission(context: dict[str, Any]) -> str:
        parameter_names = sorted(
            {
                str(parameter["name"])
                for subtask in context.get("runtime_parameter_schema", [])
                for parameter in subtask.get("parameters", [])
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
        return generator_code

    @staticmethod
    def _validator_submission(context: dict[str, Any]) -> str:
        del context
        validator_code = (
            '#include "testlib.h"\n'
            "int main(int argc,char** argv){registerValidation(argc,argv);"
            'inf.readInt(-1000000000,1000000000,"value");'
            "inf.readEoln();inf.readEof();return 0;}"
        )
        return validator_code

    @staticmethod
    def _runtime_parameters(candidate: dict[str, Any]) -> dict[str, Any]:
        result = dict(candidate)
        subtasks = []
        for raw in result.get("subtasks", []):
            subtask = dict(raw)
            subtask.pop("special_cases", None)
            test_count = max(3, int(subtask["test_count"]))
            subtask["test_count"] = test_count
            subtask["generation_profiles"] = [
                {
                    "id": "format_valid",
                    "category": "rules_format",
                    "count": 1,
                    "goal": "strictly valid input",
                    "parameter_names": ["construction_mode", "variation_budget", "scale"],
                },
                {
                    "id": "complexity_stress",
                    "category": "anti_algorithm",
                    "count": test_count - 2,
                    "goal": "stress weak complexity",
                    "parameter_names": ["construction_mode", "variation_budget", "scale"],
                },
                {
                    "id": "boundary_extreme",
                    "category": "boundary_edge",
                    "count": 1,
                    "goal": "exercise boundaries",
                    "parameter_names": ["construction_mode", "variation_budget", "scale"],
                },
            ]
            if not subtask.get("runtime_parameters"):
                subtask["runtime_parameters"] = [
                    {
                        "case_id": case_id,
                        "generation_profile_id": (
                            "format_valid"
                            if case_id == 1
                            else "boundary_extreme"
                            if case_id == test_count
                            else "complexity_stress"
                        ),
                        "parameters": [
                            {
                                "name": "construction_mode",
                                "value": (
                                    "valid_constructed"
                                    if case_id == 1
                                    else "high_branching"
                                    if case_id < test_count
                                    else "boundary_extreme"
                                ),
                                "category": "structure",
                            },
                            {
                                "name": "variation_budget",
                                "value": 8,
                                "category": "limit",
                            },
                            {"name": "scale", "value": case_id, "category": "size"},
                            {
                                "name": "profile",
                                "value": f"subtask_{subtask['id']}",
                                "category": "structure",
                            },
                        ],
                    }
                    for case_id in range(1, test_count + 1)
                ]
            subtasks.append(subtask)
        result["subtasks"] = subtasks
        return result

    @staticmethod
    def _default_plan() -> dict[str, Any]:
        return {
            "subtasks": [
                {
                    "id": 1,
                    "test_count": 3,
                    "expected_complexity": "O(n)",
                }
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
