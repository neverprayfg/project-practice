from __future__ import annotations

import asyncio


def create_project(client, description: str) -> str:
    response = client.post(
        "/api/projects",
        json={
            "problem_description": description,
            "solution_code": "int main(){}",
            "difficulty": "easy",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["project_id"]


def test_projects_can_be_listed_and_deleted(app_bundle) -> None:
    client, _sandbox = app_bundle
    first_id = create_project(client, "First project")
    second_id = create_project(client, "Second project")

    listed = client.get("/api/projects")

    assert listed.status_code == 200
    projects = listed.json()["projects"]
    assert {project["project_id"] for project in projects} == {first_id, second_id}
    assert projects[0]["updated_at"] >= projects[1]["updated_at"]

    deleted = client.delete(f"/api/projects/{second_id}")

    assert deleted.status_code == 200
    assert deleted.json() == {"deleted_project_id": second_id}
    assert client.get(f"/api/projects/{second_id}").status_code == 404
    assert not (client.app.state.storage.root / second_id).exists()
    remaining = client.get("/api/projects").json()["projects"]
    assert [project["project_id"] for project in remaining] == [first_id]


def test_stage_one_persists_generated_short_project_name(app_bundle) -> None:
    client, _sandbox = app_bundle

    created = client.post(
        "/api/projects",
        json={
            "problem_description": "# 单源最短路\n给定带权图，求从起点到各点的最短距离。",
            "solution_code": "int main(){}",
            "difficulty": "easy",
        },
    )

    assert created.status_code == 201, created.text
    project_id = created.json()["project_id"]
    assert created.json()["project_name"] == "单源最短路"

    project = client.get(f"/api/projects/{project_id}").json()
    history = client.get("/api/projects").json()["projects"]

    assert project["project"]["project_name"] == "单源最短路"
    assert project["input"]["project_name"] == "单源最短路"
    assert history[0]["title"] == "单源最短路"


def test_deleting_unknown_project_returns_not_found(app_bundle) -> None:
    client, _sandbox = app_bundle

    response = client.delete("/api/projects/00000000000000000000000000000000")

    assert response.status_code == 404
    assert response.json()["code"] == "PROJECT_NOT_FOUND"


def test_project_activity_is_isolated_and_same_project_reentry_is_rejected(app_bundle) -> None:
    client, _sandbox = app_bundle
    first_id = create_project(client, "First project")
    first_lock = client.app.state.pipeline.project_lock(first_id)
    asyncio.run(first_lock.acquire())

    try:
        first = client.get(f"/api/projects/{first_id}")
        listed = client.get("/api/projects")
        second_id = create_project(client, "Second project")
        second_compile = client.post(f"/api/projects/{second_id}/solution/compile")
        duplicate = client.post(
            f"/api/projects/{first_id}/auto-run",
            json={"base_seed": 1},
        )
    finally:
        first_lock.release()

    assert first.status_code == 200
    assert first.json()["active_task"] is True
    history = {item["project_id"]: item for item in listed.json()["projects"]}
    assert history[first_id]["active_task"] is True
    assert client.get(f"/api/projects/{second_id}").status_code == 200
    assert second_compile.status_code == 200
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "PROJECT_BUSY"
