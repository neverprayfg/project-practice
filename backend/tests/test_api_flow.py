from __future__ import annotations

# ruff: noqa: E501
import io
import zipfile

from conftest import DeterministicTestModel, FakeSandbox
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import SandboxResult, Stage, StageStatus
from app.models import TestDataPlanDraft as DataPlanDraft

TEST_DATA_PLAN_DRAFT = {
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
    "issues": [],
}

PLAN_DRAFT = {
    "subtasks": [
        {
            "id": 1,
            "test_count": 3,
            "expected_complexity": "O(n)",
            "generation_profiles": [
                {
                    "id": "format_valid",
                    "category": "rules_format",
                    "count": 1,
                    "goal": "valid format",
                    "parameter_names": ["scale", "profile"],
                },
                {
                    "id": "complexity_stress",
                    "category": "anti_algorithm",
                    "count": 1,
                    "goal": "stress complexity",
                    "parameter_names": ["scale", "profile"],
                },
                {
                    "id": "boundary_extreme",
                    "category": "boundary_edge",
                    "count": 1,
                    "goal": "boundary cases",
                    "parameter_names": ["scale", "profile"],
                },
            ],
        }
    ],
    "issues": [],
}

WHOLE_PROBLEM_PLAN = {
    "subtasks": [
        {
            "id": 1,
            "test_count": 3,
            "expected_complexity": "O(n)",
            "generation_profiles": [
                {
                    "id": "format_valid",
                    "category": "rules_format",
                    "count": 1,
                    "goal": "valid format",
                    "parameter_names": ["scale", "profile"],
                },
                {
                    "id": "complexity_stress",
                    "category": "anti_algorithm",
                    "count": 1,
                    "goal": "stress complexity",
                    "parameter_names": ["scale", "profile"],
                },
                {
                    "id": "boundary_extreme",
                    "category": "boundary_edge",
                    "count": 1,
                    "goal": "boundary cases",
                    "parameter_names": ["scale", "profile"],
                },
            ],
        },
    ],
    "issues": [],
}

CODE_DRAFT = {
    "format_contract_id": "format_" + "0" * 24,
    "generator_code": (
        '#include "testlib.h"\nint main(int argc,char** argv){registerGen(argc,argv,1);}'
    ),
    "validator_code": (
        '#include "testlib.h"\nint main(int argc,char** argv){registerValidation(argc,argv);}'
    ),
    "issues": [],
}


def save_run_confirm(client: TestClient, project_id: str, stage: int, draft: dict) -> None:
    saved = client.put(f"/api/projects/{project_id}/stages/{stage}/draft", json={"draft": draft})
    assert saved.status_code == 200, saved.text
    run = client.post(f"/api/projects/{project_id}/stages/{stage}/run", json={})
    assert run.status_code == 200, run.text
    state = client.get(f"/api/projects/{project_id}").json()["project"]["stages"][str(stage)]
    assert state["ai_confirmed"] is True
    assert state["user_confirmed"] is False
    confirmed = client.post(
        f"/api/projects/{project_id}/stages/{stage}/confirm",
        json={"confirmed": True},
    )
    assert confirmed.status_code == 200, confirmed.text


class MultiInterruptedStage3Model(DeterministicTestModel):
    def __init__(self) -> None:
        self.stage3_generations = 0

    async def agent2_test_data_plan(
        self, context: dict, candidate: dict
    ) -> DataPlanDraft:
        self.stage3_generations += 1
        if self.stage3_generations <= 6:
            return DataPlanDraft(plan_markdown="# interrupted")
        return await super().agent2_test_data_plan(context, candidate)


def test_stage_five_locates_stage_four_plan_failure_and_auto_run_repairs_it(
    app_bundle,
) -> None:
    client, _sandbox = app_bundle
    created = client.post(
        "/api/projects",
        json={
            "problem_description": "Read n.",
            "solution_code": "int main(){}",
            "difficulty": "easy",
        },
    )
    project_id = created.json()["project_id"]
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)
    storage = client.app.state.storage
    corrupted = storage.load_draft(project_id, 4)
    assert corrupted is not None
    corrupted["subtasks"][0]["runtime_parameters"] = []
    storage.save_draft(project_id, 4, corrupted)

    response = client.post(f"/api/projects/{project_id}/stages/5/run", json={})

    assert response.status_code == 409
    assert response.json()["code"] == "AGENT_RECOVERY_REROUTED"
    assert response.json()["details"]["recovery_plan"]["root_stage"] == 4
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    assert project["current_stage"] == 5
    assert project["stages"]["4"]["status"] == "passed"
    assert project["stages"]["5"]["status"] == "failed"

    recovered = client.post(
        f"/api/projects/{project_id}/auto-run",
        json={"base_seed": 17},
    )

    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["ok"] is True
    repaired = storage.load_draft(project_id, 4)
    assert repaired is not None
    assert repaired["subtasks"][0]["runtime_parameters"]


def test_complete_mvp_flow_exports_only_required_files(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, sandbox = app_bundle
    created = client.post(
        "/api/projects",
        json={
            "problem_description": "Read n and print n.",
            "solution_code": "int main(){}",
            "difficulty": "easy",
        },
    )
    project_id = created.json()["project_id"]

    compiled = client.post(f"/api/projects/{project_id}/solution/compile")
    assert compiled.status_code == 200
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)
    save_run_confirm(client, project_id, 5, CODE_DRAFT)

    project_payload = client.get(f"/api/projects/{project_id}").json()
    release = project_payload["code_release"]
    assert release["revision_id"] == project_payload["drafts"]["5"]["revision_id"]
    assert len(release["generator_sha256"]) == 64
    assert len(release["validator_sha256"]) == 64
    assert len(release["content_sha256"]) == 64
    assert client.get(f"/api/projects/{project_id}/stage5/timings").status_code == 404
    assert client.get(f"/api/projects/{project_id}/stage5/decisions").status_code == 404

    preview = client.post(
        f"/api/projects/{project_id}/preview",
        json={"subtask_id": 1, "seed": 7},
    )
    assert preview.status_code == 200
    assert preview.json()["subtask_id"] == 1
    assert preview.json()["seed"] == 7

    manifest_writes = 0
    original_save_manifest = client.app.state.storage.save_batch_manifest

    def tracked_save_manifest(project: str, value: dict) -> None:
        nonlocal manifest_writes
        manifest_writes += 1
        original_save_manifest(project, value)

    client.app.state.storage.save_batch_manifest = tracked_save_manifest
    sandbox.batch_calls.clear()
    generated = client.post(f"/api/projects/{project_id}/generate", json={"base_seed": 9})
    assert generated.status_code == 200, generated.text
    assert generated.json()["ok"] is True
    assert generated.json()["generated_tests"] == 3
    validated = client.post(f"/api/projects/{project_id}/validate", json={})
    assert validated.status_code == 200, validated.text
    assert validated.json()["validated_tests"] == 3
    assert validated.json()["export_ready"] is True
    assert sandbox.batch_calls == [("generate", 3), ("validate_solve", 3)]
    assert manifest_writes == 4
    generated_paths = [
        call[4] for call in sandbox.calls if call[0] == "generate" and call[4].startswith("data/")
    ]
    assert generated_paths == ["data/1_1.in", "data/1_2.in", "data/1_3.in"]


def test_one_click_generation_auto_confirms_and_exports(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, _sandbox = app_bundle
    created = client.post(
        "/api/projects",
        json={
            "problem_description": "Read n and print n.",
            "solution_code": "int main(){}",
            "difficulty": "easy",
        },
    )
    project_id = created.json()["project_id"]

    response = client.post(
        f"/api/projects/{project_id}/auto-run",
        json={"base_seed": 17},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["archive"] == "dataset.zip"
    assert payload["download_url"] == f"/api/projects/{project_id}/export"
    assert [step["stage"] for step in payload["steps"]] == [2, 3, 4, 5, 6, 7, 8]
    project = payload["project"]
    assert project["current_stage"] == 8
    assert project["generation_complete"] is True
    assert project["build_complete"] is True
    assert project["export_ready"] is True
    assert all(
        project["stages"][str(stage)]["status"] == "passed" for stage in (3, 4, 5)
    )
    assert all(
        project["stages"][str(stage)]["ai_confirmed"]
        and project["stages"][str(stage)]["user_confirmed"]
        for stage in (3, 4, 5)
    )
    exported = client.get(f"/api/projects/{project_id}/export")
    assert exported.status_code == 200


def test_auto_run_can_resume_after_multiple_interrupted_stage_three_runs(tmp_path) -> None:
    model = MultiInterruptedStage3Model()
    sandbox = FakeSandbox(tmp_path)
    app = create_app(
        Settings(app_env="test", storage_root=tmp_path),
        model=model,
        sandbox=sandbox,
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/projects",
            json={
                "problem_description": "Read n and print n.",
                "solution_code": "int main(){}",
                "difficulty": "easy",
            },
        )
        project_id = created.json()["project_id"]

        first = client.post(f"/api/projects/{project_id}/auto-run", json={"base_seed": 17})
        second = client.post(f"/api/projects/{project_id}/auto-run", json={"base_seed": 17})

        assert first.status_code == 409
        assert second.status_code == 409
        paused = client.get(f"/api/projects/{project_id}").json()["project"]
        assert paused["current_stage"] == 3
        assert paused["stages"]["3"]["status"] == "failed"
        assert model.stage3_generations == 6

        resumed = client.post(f"/api/projects/{project_id}/auto-run", json={"base_seed": 17})

        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["ok"] is True
        assert resumed.json()["project"]["current_stage"] == 8
        assert model.stage3_generations == 7


def test_auto_run_reclaims_stale_checking_stage_before_continuing(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, _sandbox = app_bundle
    created = client.post(
        "/api/projects",
        json={
            "problem_description": "Read n and print n.",
            "solution_code": "int main(){}",
            "difficulty": "easy",
        },
    )
    project_id = created.json()["project_id"]
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)

    projects = client.app.state.projects
    record = projects.get(project_id)
    record.stages[4].status = StageStatus.CHECKING
    record.current_stage = Stage.CODE_DRAFT
    client.app.state.storage.save_record(record)

    resumed = client.post(f"/api/projects/{project_id}/auto-run", json={"base_seed": 17})

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["steps"][0]["stage"] == 4
    assert resumed.json()["steps"][0]["status"] == "resumed"
    assert resumed.json()["project"]["current_stage"] == 8


def test_auto_run_advances_a_compiled_project_still_marked_at_stage_two(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, _sandbox = app_bundle
    created = client.post(
        "/api/projects",
        json={
            "problem_description": "Read n and print n.",
            "solution_code": "int main(){}",
            "difficulty": "easy",
        },
    )
    project_id = created.json()["project_id"]
    client.post(f"/api/projects/{project_id}/solution/compile")

    record = client.app.state.projects.get(project_id)
    record.current_stage = Stage.SOLUTION_COMPILE
    client.app.state.storage.save_record(record)

    resumed = client.post(f"/api/projects/{project_id}/auto-run", json={"base_seed": 17})

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["steps"][0] == {
        "stage": 2,
        "attempt": 0,
        "status": "resumed",
        "message": "标程已编译通过，从阶段 3 继续执行。",
    }
    assert resumed.json()["project"]["current_stage"] == 8


def test_one_click_generation_repairs_solution_compile_failure(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, sandbox = app_bundle
    original_compile = sandbox.compile
    solution_attempts = 0

    async def fail_solution_once(project_id: str, role: str):
        nonlocal solution_attempts
        if role == "solution":
            solution_attempts += 1
            if solution_attempts == 1:
                return SandboxResult(
                    ok=False,
                    exit_code=1,
                    stderr="expected ';' before '}'",
                )
        return await original_compile(project_id, role)

    sandbox.compile = fail_solution_once  # type: ignore[method-assign]
    created = client.post(
        "/api/projects",
        json={
            "problem_description": "Read n and print n.",
            "solution_code": "int main(){}",
            "difficulty": "easy",
        },
    )
    project_id = created.json()["project_id"]

    response = client.post(f"/api/projects/{project_id}/auto-run", json={})

    assert response.status_code == 200, response.text
    stage_two = [step for step in response.json()["steps"] if step["stage"] == 2]
    assert [step["status"] for step in stage_two] == ["repairing", "passed"]

    exported = client.get(f"/api/projects/{project_id}/export")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert sorted(archive.namelist()) == [
            "data/1_1.in",
            "data/1_1.out",
            "data/1_2.in",
            "data/1_2.out",
            "data/1_3.in",
            "data/1_3.out",
            "generator.cpp",
            "validator.cpp",
        ]


def test_project_history_lists_saved_problem_records(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, _sandbox = app_bundle
    first = client.post(
        "/api/projects",
        json={
            "problem_description": "# First Problem\nRead n.",
            "solution_code": "int main(){}",
            "difficulty": "easy",
        },
    )
    second = client.post(
        "/api/projects",
        json={
            "problem_description": "Second Problem\nRead m.",
            "solution_code": "int main(){}",
            "difficulty": "medium",
        },
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    first_id = first.json()["project_id"]
    second_id = second.json()["project_id"]
    client.post(f"/api/projects/{first_id}/solution/compile")

    history = client.get("/api/projects")

    assert history.status_code == 200, history.text
    projects = history.json()["projects"]
    assert [project["project_id"] for project in projects] == [first_id, second_id]
    assert projects[0]["title"] == "First Problem"
    assert projects[0]["current_stage"] == 3
    assert projects[0]["solution_compiled"] is True
    assert projects[1]["title"] == "Second Problem"


def test_user_cannot_confirm_before_ai(created_project: tuple[TestClient, str]) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    client.put(
        f"/api/projects/{project_id}/stages/3/draft",
        json={"draft": TEST_DATA_PLAN_DRAFT},
    )
    response = client.post(f"/api/projects/{project_id}/stages/3/confirm", json={"confirmed": True})
    assert response.status_code == 409
    assert response.json()["code"] == "CONFIRMATION_REQUIRED"


def test_editing_waiting_draft_discards_stale_langgraph_interrupt(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    run = client.post(f"/api/projects/{project_id}/stages/3/run", json={})
    assert run.status_code == 200
    assert run.json()["agent"]["waiting_user"] is True

    edited = client.put(
        f"/api/projects/{project_id}/stages/3/draft",
        json={"draft": {**TEST_DATA_PLAN_DRAFT, "plan_markdown": TEST_DATA_PLAN_DRAFT["plan_markdown"] + "\n\n补充：覆盖 count。"}},
    )
    assert edited.status_code == 200
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    assert "3" not in project["stage_threads"]

    confirmation = client.post(
        f"/api/projects/{project_id}/stages/3/confirm",
        json={"confirmed": True},
    )
    assert confirmation.status_code == 409
    assert confirmation.json()["code"] == "CONFIRMATION_REQUIRED"


def test_stage_three_runs_langgraph_and_waits_for_user(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")

    response = client.post(f"/api/projects/{project_id}/stages/3/run", json={})

    assert response.status_code == 200
    assert response.json()["agent"]["framework"] == "langgraph"
    assert response.json()["agent"]["waiting_user"] is True
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    assert project["stages"]["3"]["status"] == "waiting_user"
    assert project["stage_threads"]["3"].startswith(f"{project_id}:agent2:")


def test_stage_three_and_four_can_regenerate_while_confirmation_is_pending(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")

    first_stage_three = client.post(f"/api/projects/{project_id}/stages/3/run", json={})
    second_stage_three = client.post(f"/api/projects/{project_id}/stages/3/run", json={})
    assert second_stage_three.status_code == 200, second_stage_three.text
    assert second_stage_three.json()["agent"]["thread_id"] != first_stage_three.json()["agent"][
        "thread_id"
    ]
    client.post(f"/api/projects/{project_id}/stages/3/confirm", json={"confirmed": True})

    first_stage_four = client.post(f"/api/projects/{project_id}/stages/4/run", json={})
    second_stage_four = client.post(f"/api/projects/{project_id}/stages/4/run", json={})
    assert second_stage_four.status_code == 200, second_stage_four.text
    assert second_stage_four.json()["agent"]["thread_id"] != first_stage_four.json()["agent"][
        "thread_id"
    ]


def test_stage_instruction_can_answer_without_changing_draft_or_confirmation(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    initial = client.post(f"/api/projects/{project_id}/stages/3/run", json={}).json()
    before = client.get(f"/api/projects/{project_id}").json()["project"]

    response = client.post(
        f"/api/projects/{project_id}/stages/3/run",
        json={"user_instruction": "为什么需要边界测试？"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["interaction"] == {
        "action": "answer",
        "answer": "这是阶段 3 的一次性回答。",
        "target": "none",
    }
    assert response.json()["draft"] == initial["draft"]
    after = client.get(f"/api/projects/{project_id}").json()["project"]
    assert after["stage_threads"]["3"] == before["stage_threads"]["3"]
    assert after["stages"]["3"] == before["stages"]["3"]


def test_stage_instructions_revise_stage_three_four_and_target_stage_five(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")

    stage_three = client.post(f"/api/projects/{project_id}/stages/3/run", json={}).json()
    revised_three = client.post(
        f"/api/projects/{project_id}/stages/3/run",
        json={"user_instruction": "请修改约束并强调最大值。"},
    )
    assert revised_three.status_code == 200, revised_three.text
    assert revised_three.json()["interaction"]["action"] == "revise"
    assert "用户意见：请修改约束并强调最大值。" in revised_three.json()["draft"][
        "plan_markdown"
    ]
    assert revised_three.json()["agent"]["thread_id"] != stage_three["agent"]["thread_id"]
    client.post(f"/api/projects/{project_id}/stages/3/confirm", json={"confirmed": True})

    client.post(f"/api/projects/{project_id}/stages/4/run", json={})
    revised_four = client.post(
        f"/api/projects/{project_id}/stages/4/run",
        json={"user_instruction": "请调整第一个子任务的期望复杂度。"},
    )
    assert revised_four.status_code == 200, revised_four.text
    assert revised_four.json()["interaction"]["action"] == "revise"
    assert revised_four.json()["draft"]["subtasks"][0]["expected_complexity"] == "O(n log n)"
    client.post(f"/api/projects/{project_id}/stages/4/confirm", json={"confirmed": True})

    initial_five = client.post(f"/api/projects/{project_id}/stages/5/run", json={}).json()[
        "draft"
    ]
    revised_five = client.post(
        f"/api/projects/{project_id}/stages/5/run",
        json={"user_instruction": "请修改 generator，增加一条注释。"},
    )
    assert revised_five.status_code == 200, revised_five.text
    assert revised_five.json()["interaction"]["target"] == "generator"
    assert revised_five.json()["draft"]["generator_code"].endswith(
        "// user instruction applied"
    )
    assert revised_five.json()["draft"]["validator_code"] == initial_five["validator_code"]


def test_agent_three_creates_one_whole_problem_subtask_on_initial_run(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")

    stage_three = client.post(f"/api/projects/{project_id}/stages/3/run", json={})
    assert stage_three.status_code == 200, stage_three.text
    client.post(
        f"/api/projects/{project_id}/stages/3/confirm",
        json={"confirmed": True},
    )
    stage_four = client.post(f"/api/projects/{project_id}/stages/4/run", json={})

    assert stage_four.status_code == 200, stage_four.text
    assert len(stage_four.json()["draft"]["subtasks"]) == 1
    assert stage_four.json()["agent"]["waiting_user"] is True


def test_agent_three_overwrites_existing_subtask_draft(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    stage_three = client.post(f"/api/projects/{project_id}/stages/3/run", json={})
    assert stage_three.status_code == 200, stage_three.text
    client.post(
        f"/api/projects/{project_id}/stages/3/confirm",
        json={"confirmed": True},
    )
    subtasks = [
        {
            "id": subtask_id,
            "test_count": 3,
            "expected_complexity": "O(n)",
            "generation_profiles": [
                {
                    "id": "format_valid",
                    "category": "rules_format",
                    "count": 1,
                    "goal": "valid format",
                    "parameter_names": ["n"],
                },
                {
                    "id": "complexity_stress",
                    "category": "anti_algorithm",
                    "count": 1,
                    "goal": "stress complexity",
                    "parameter_names": ["n"],
                },
                {
                    "id": "boundary_extreme",
                    "category": "boundary_edge",
                    "count": 1,
                    "goal": "boundary cases",
                    "parameter_names": ["n"],
                },
            ],
            "runtime_parameters": [
                {
                    "case_id": case_id,
                    "generation_profile_id": (
                        "format_valid"
                        if case_id == 1
                        else "complexity_stress"
                        if case_id == 2
                        else "boundary_extreme"
                    ),
                    "parameters": [
                        {"name": "n", "value": subtask_id * case_id, "category": "size"},
                    ],
                }
                for case_id in range(1, 4)
            ],
        }
        for subtask_id in (1, 2)
    ]
    saved = client.put(
        f"/api/projects/{project_id}/stages/4/draft",
        json={"draft": {"subtasks": subtasks, "issues": []}},
    )
    assert saved.status_code == 200, saved.text

    stage_four = client.post(f"/api/projects/{project_id}/stages/4/run", json={})

    assert stage_four.status_code == 200, stage_four.text
    assert [item["id"] for item in stage_four.json()["draft"]["subtasks"]] == [1]


def test_generate_whole_problem_plan_then_validate_as_separate_stages(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, sandbox = app_bundle
    created = client.post(
        "/api/projects",
        json={
            "problem_description": "Read n and print n.",
            "solution_code": "int main(){}",
            "difficulty": "easy",
        },
    )
    project_id = created.json()["project_id"]
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    save_run_confirm(client, project_id, 4, WHOLE_PROBLEM_PLAN)
    save_run_confirm(client, project_id, 5, CODE_DRAFT)

    generated = client.post(
        f"/api/projects/{project_id}/generate",
        json={"base_seed": 17, "selected_subtask_ids": [1]},
    )

    assert generated.status_code == 200, generated.text
    assert generated.json()["generated_tests"] == 3
    assert generated.json()["selected_subtasks"] == [1]
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    assert project["current_stage"] == 7
    assert project["generation_complete"] is True
    assert project["build_complete"] is False
    assert project["export_ready"] is False
    generated_paths = [
        call[4] for call in sandbox.calls if call[0] == "generate" and call[4].startswith("data/")
    ]
    assert generated_paths == ["data/1_1.in", "data/1_2.in", "data/1_3.in"]

    validated = client.post(f"/api/projects/{project_id}/validate", json={})

    assert validated.status_code == 200, validated.text
    assert validated.json()["validated_tests"] == 3
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    assert project["current_stage"] == 8
    assert project["build_complete"] is True
    assert project["export_ready"] is True
    manifest = client.app.state.storage.load_batch_manifest(project_id)
    assert manifest is not None
    assert manifest["status"] == "completed"
    assert manifest["base_seed"] == 17
    assert manifest["selected_subtasks"] == [1]
    assert len(manifest["files"]) == 3
    assert all(item["runtime_arguments"] for item in manifest["files"])
    assert {item["runtime_arguments"]["generation_profile"] for item in manifest["files"]} == {
        "format_valid",
        "complexity_stress",
        "boundary_extreme",
    }
    assert all(item["validation"]["ok"] for item in manifest["files"])


def test_updating_solution_invalidates_downstream_confirmations(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)

    response = client.put(
        f"/api/projects/{project_id}/solution",
        json={"solution_code": "int main(){return 0;}"},
    )

    assert response.status_code == 200
    project = response.json()
    assert project["current_stage"] == 2
    assert project["solution_compiled"] is False
    assert project["stages"]["3"]["status"] == "draft"
    assert project["stages"]["3"]["ai_confirmed"] is False
    loaded = client.get(f"/api/projects/{project_id}/solution")
    assert loaded.json()["solution_code"] == "int main(){return 0;}"


def test_subtask_constraints_are_required(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    invalid = {
        **PLAN_DRAFT,
        "subtasks": [{**PLAN_DRAFT["subtasks"][0], "constraints": ""}],
    }
    response = client.put(f"/api/projects/{project_id}/stages/4/draft", json={"draft": invalid})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_DRAFT"


def test_editing_test_data_plan_invalidates_later_confirmations(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)

    response = client.put(
        f"/api/projects/{project_id}/stages/3/draft",
        json={"draft": {**TEST_DATA_PLAN_DRAFT, "plan_markdown": TEST_DATA_PLAN_DRAFT["plan_markdown"] + "\n\n补充：覆盖 count。"}},
    )

    assert response.status_code == 200
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    assert project["current_stage"] == 3
    assert project["stages"]["4"]["status"] == "draft"
    assert project["stages"]["4"]["ai_confirmed"] is False
    assert project["stages"]["5"]["user_confirmed"] is False


def test_saving_identical_drafts_preserves_all_confirmations(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)
    save_run_confirm(client, project_id, 5, CODE_DRAFT)

    before = client.get(f"/api/projects/{project_id}").json()
    for stage in (3, 4, 5):
        response = client.put(
            f"/api/projects/{project_id}/stages/{stage}/draft",
            json={"draft": before["drafts"][str(stage)]},
        )
        assert response.status_code == 200, response.text

    after = client.get(f"/api/projects/{project_id}").json()
    assert after == before


def test_changing_only_issues_does_not_roll_back_passed_stage(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    before = client.get(f"/api/projects/{project_id}").json()

    response = client.put(
        f"/api/projects/{project_id}/stages/3/draft",
        json={"draft": {**before["drafts"]["3"], "issues": ["仅修改诊断文字"]}},
    )

    assert response.status_code == 200
    after = client.get(f"/api/projects/{project_id}").json()
    assert after == before


def test_editing_earlier_stage_clears_active_downstream_context_and_artifacts(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)
    save_run_confirm(client, project_id, 5, CODE_DRAFT)
    storage = client.app.state.storage
    before = client.get(f"/api/projects/{project_id}").json()["project"]
    storage.save_batch_manifest(project_id, {"status": "completed"})
    for directory, filename in (
        ("preview", "old.in"),
        ("data", "old.in"),
        ("bin", "old"),
        ("export", "old.zip"),
    ):
        storage.write_text(storage.project_dir(project_id) / directory / filename, "old")

    edited = client.put(
        f"/api/projects/{project_id}/stages/3/draft",
        json={"draft": {**TEST_DATA_PLAN_DRAFT, "plan_markdown": TEST_DATA_PLAN_DRAFT["plan_markdown"] + "\n\n补充：覆盖 count。"}},
    )

    assert edited.status_code == 200
    loaded = client.get(f"/api/projects/{project_id}").json()
    project = loaded["project"]
    assert project["current_stage"] == 3
    assert project["workflow_revision"] == before["workflow_revision"] + 1
    assert loaded["drafts"]["4"] is None
    assert loaded["drafts"]["5"] is None
    assert storage.load_batch_manifest(project_id) is None
    for directory in ("preview", "data", "bin", "export"):
        assert list((storage.project_dir(project_id) / directory).iterdir()) == []


def test_passed_historical_stage_cannot_be_rerun_without_saved_change(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)

    response = client.post(f"/api/projects/{project_id}/stages/3/run", json={})

    assert response.status_code == 409
    assert response.json()["code"] == "STALE_STAGE"


def test_recompiling_identical_solution_preserves_current_stage(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    before = client.get(f"/api/projects/{project_id}").json()["project"]
    source = client.get(f"/api/projects/{project_id}/solution").json()["solution_code"]

    saved = client.put(
        f"/api/projects/{project_id}/solution",
        json={"solution_code": source},
    )
    compiled = client.post(f"/api/projects/{project_id}/solution/compile")

    assert saved.status_code == 200
    assert compiled.status_code == 200
    after = compiled.json()["project"]
    assert after["current_stage"] == before["current_stage"]
    assert after["workflow_revision"] == before["workflow_revision"]
    assert after["stages"] == before["stages"]


def test_validator_failure_returns_project_to_stage_five(
    app_bundle: tuple[TestClient, FakeSandbox],
) -> None:
    client, sandbox = app_bundle
    created = client.post(
        "/api/projects",
        json={
            "problem_description": "Read n and print n.",
            "solution_code": "int main(){}",
            "difficulty": "easy",
        },
    )
    project_id = created.json()["project_id"]
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, TEST_DATA_PLAN_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)
    save_run_confirm(client, project_id, 5, CODE_DRAFT)
    sandbox.fail_validation = True

    generated = client.post(f"/api/projects/{project_id}/generate", json={"base_seed": 1})
    assert generated.status_code == 200, generated.text
    response = client.post(f"/api/projects/{project_id}/validate", json={})
    assert response.status_code == 400
    assert response.json()["stage"] == 5
    details = response.json()["details"]
    assert details["check"]["operation"] == "validate"
    assert details["execution"]["failure_category"] == "validation"
    assert details["execution"]["checks"][0]["runtime_arguments"]
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    assert project["current_stage"] == 5
    assert project["stages"]["5"]["ai_confirmed"] is False
    assert project["stages"]["5"]["user_confirmed"] is False
