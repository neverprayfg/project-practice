from __future__ import annotations


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


def test_deleting_unknown_project_returns_not_found(app_bundle) -> None:
    client, _sandbox = app_bundle

    response = client.delete("/api/projects/00000000000000000000000000000000")

    assert response.status_code == 404
    assert response.json()["code"] == "PROJECT_NOT_FOUND"
