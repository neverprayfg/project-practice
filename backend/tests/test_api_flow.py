from __future__ import annotations

import io
import zipfile

from conftest import FakeSandbox
from fastapi.testclient import TestClient

INPUT_DRAFT = {
    "template": "第一行读取整数 n，表示后续整数序列的长度。",
    "issues": [],
}

PLAN_DRAFT = {
    "subtasks": [
        {
            "id": 1,
            "test_count": 2,
            "expected_complexity": "O(n)",
            "special_cases": [{"count": 1, "description": "minimum value"}],
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
            "special_cases": [],
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


def test_structure_tag_catalog_is_exposed(app_bundle) -> None:
    client, _sandbox = app_bundle

    response = client.get("/api/structure-tags")

    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert any(item["id"] == "tree" for item in response.json()["tags"])


def test_stage_five_does_not_return_project_to_stage_four_for_plan_advice(app_bundle) -> None:
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
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)
    storage = client.app.state.storage
    corrupted = storage.load_draft(project_id, 4)
    assert corrupted is not None
    corrupted["subtasks"][0]["runtime_parameters"] = []
    storage.save_draft(project_id, 4, corrupted)

    response = client.post(f"/api/projects/{project_id}/stages/5/run", json={})

    assert response.status_code == 200
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    assert project["current_stage"] == 5
    assert project["stages"]["4"]["status"] == "passed"
    assert project["stages"]["5"]["status"] == "draft"


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
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)
    save_run_confirm(client, project_id, 5, CODE_DRAFT)

    timings = client.get(f"/api/projects/{project_id}/stage5/timings")
    assert timings.status_code == 200, timings.text
    timing_payload = timings.json()
    assert {
        "retrieval",
        "model_generation",
        "compile",
        "trial_generation",
        "validation",
        "semantic_audit",
        "workflow_total",
    }.issubset({event["segment"] for event in timing_payload["events"]})
    assert len(timing_payload["runs"]) == 1
    timing_run = timing_payload["runs"][0]
    assert timing_run["workflow_total_ms"] >= timing_run["measured_segments_ms"]
    assert timing_run["rounds"][0]["round"] == 1
    assert timing_run["segments"]["compile"]["calls"] == 1
    decisions = client.get(f"/api/projects/{project_id}/stage5/decisions")
    assert decisions.status_code == 200, decisions.text
    decision_events = decisions.json()["events"]
    assert decision_events
    assert {
        "candidate_revision",
        "target_defect_id",
        "model_call_type",
        "modified_files",
        "before",
        "after",
        "progress",
        "decision",
        "reason",
    }.issubset(decision_events[-1])

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
    assert generated.json()["generated_tests"] == 2
    validated = client.post(f"/api/projects/{project_id}/validate", json={})
    assert validated.status_code == 200, validated.text
    assert validated.json()["validated_tests"] == 2
    assert validated.json()["export_ready"] is True
    assert sandbox.batch_calls == [("generate", 2), ("validate_solve", 2)]
    assert manifest_writes == 4
    generated_paths = [
        call[4] for call in sandbox.calls if call[0] == "generate" and call[4].startswith("data/")
    ]
    assert generated_paths == ["data/1_1.in", "data/1_2.in"]

    exported = client.get(f"/api/projects/{project_id}/export")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert sorted(archive.namelist()) == [
            "data/1_1.in",
            "data/1_1.out",
            "data/1_2.in",
            "data/1_2.out",
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
        json={"draft": INPUT_DRAFT},
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
        json={"draft": {"template": "第一行读取整数 count。", "issues": []}},
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


def test_agent_three_preserves_user_configured_subtask_count(
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
            "test_count": 1,
            "expected_complexity": "O(n)",
            "special_cases": [],
            "runtime_parameters": [
                {
                    "case_id": 1,
                    "parameters": [
                        {"name": "n", "value": subtask_id, "category": "size"},
                    ],
                }
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
    assert [item["id"] for item in stage_four.json()["draft"]["subtasks"]] == [1, 2]


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
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)
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
    assert all(item["validation"]["ok"] for item in manifest["files"])


def test_updating_solution_invalidates_downstream_confirmations(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)

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
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)
    invalid = {
        **PLAN_DRAFT,
        "subtasks": [{**PLAN_DRAFT["subtasks"][0], "constraints": ""}],
    }
    response = client.put(f"/api/projects/{project_id}/stages/4/draft", json={"draft": invalid})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_DRAFT"


def test_editing_input_template_invalidates_later_confirmations(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)

    response = client.put(
        f"/api/projects/{project_id}/stages/3/draft",
        json={"draft": {**INPUT_DRAFT, "template": "第一行读取整数 count。"}},
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
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)
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
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)
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
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)
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
        json={"draft": {**INPUT_DRAFT, "template": "第一行读取整数 count。"}},
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
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)

    response = client.post(f"/api/projects/{project_id}/stages/3/run", json={})

    assert response.status_code == 409
    assert response.json()["code"] == "STALE_STAGE"


def test_recompiling_identical_solution_preserves_current_stage(
    created_project: tuple[TestClient, str],
) -> None:
    client, project_id = created_project
    client.post(f"/api/projects/{project_id}/solution/compile")
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)
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
    save_run_confirm(client, project_id, 3, INPUT_DRAFT)
    save_run_confirm(client, project_id, 4, PLAN_DRAFT)
    save_run_confirm(client, project_id, 5, CODE_DRAFT)
    sandbox.fail_validation = True

    generated = client.post(f"/api/projects/{project_id}/generate", json={"base_seed": 1})
    assert generated.status_code == 200, generated.text
    response = client.post(f"/api/projects/{project_id}/validate", json={})
    assert response.status_code == 400
    assert response.json()["stage"] == 5
    details = response.json()["details"]
    assert details["defect_id"].startswith("defect_")
    assert details["defect"]["identity"]["category"] == "validation"
    assert details["defect"]["identity"]["target_file"] == "generator.cpp"
    assert details["check"]["operation"] == "validate"
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    assert project["current_stage"] == 5
    assert project["stages"]["5"]["ai_confirmed"] is False
    assert project["stages"]["5"]["user_confirmed"] is False
