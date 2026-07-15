from pathlib import Path

import pytest

from app.errors import AppError
from app.models import ProjectCreate, SubtaskPlanDraft
from app.services.project_service import ProjectService
from app.services.runtime_parameters import structure_tag_parameter_issues
from app.services.structure_tag_catalog import StructureTagCatalog
from app.storage import ProjectStorage

APP_ROOT = Path(__file__).parents[1] / "app"
CATALOG_PATH = APP_ROOT / "structure_context" / "tag_catalog.json"
DOCUMENT_ROOT = APP_ROOT / "generator_context" / "jngen_context" / "doc"


def test_catalog_rejects_unknown_conflicting_and_manual_only_tags() -> None:
    catalog = StructureTagCatalog(CATALOG_PATH, DOCUMENT_ROOT)

    assert catalog.validate_tag_ids(["not.a.tag"])
    assert catalog.validate_tag_ids(["graph.directed", "graph.undirected"])
    assert catalog.validate_tag_ids(["geometry.grid"])


def test_scoped_mixed_primitive_tags_do_not_conflict() -> None:
    catalog = StructureTagCatalog(CATALOG_PATH, DOCUMENT_ROOT)

    issues = catalog.validate_structure_tags(
        [
            {
                "tag_id": "primitive.integer",
                "applies_to": "count n",
                "evidence": "n is an integer",
            },
            {
                "tag_id": "primitive.real",
                "applies_to": "coordinates",
                "evidence": "coordinates are real numbers",
            },
        ]
    )

    assert issues == []


def test_catalog_document_metadata_is_deterministic_and_budgeted() -> None:
    catalog = StructureTagCatalog(CATALOG_PATH, DOCUMENT_ROOT)
    tag_ids = ["tree", "collection.query_sequence"]
    chinese = catalog.resolve_documents(tag_ids, 100_000)
    english = catalog.resolve_documents(tag_ids, 100_000)

    assert chinese == english
    assert {"tree.md", "generic_graph.md", "random.md"}.issubset(chinese["selected_filenames"])
    with pytest.raises(AppError) as error:
        catalog.resolve_documents(tag_ids, 1)
    assert error.value.code == "STRUCTURE_TAG_DOCUMENT_BUDGET_EXCEEDED"


def test_runtime_parameter_requirements_are_driven_by_catalog_metadata() -> None:
    catalog = StructureTagCatalog(CATALOG_PATH, DOCUMENT_ROOT)
    plan = SubtaskPlanDraft.model_validate(
        {
            "subtasks": [
                {
                    "id": 1,
                    "constraints": "tree with n vertices",
                    "test_count": 1,
                    "expected_complexity": "O(n)",
                    "additional_structure_tag_ids": [],
                    "runtime_parameters": [
                        {
                            "case_id": 1,
                            "parameters": [
                                {"name": "n", "value": 10, "category": "size"},
                            ],
                        }
                    ],
                }
            ]
        }
    )

    issues = structure_tag_parameter_issues(plan, ["tree"], catalog)

    assert any("缺少标签必需参数：m" in issue for issue in issues)


def test_ai_result_preserves_manual_tag_for_visible_review(tmp_path: Path) -> None:
    catalog = StructureTagCatalog(CATALOG_PATH, DOCUMENT_ROOT)
    projects = ProjectService(ProjectStorage(tmp_path), catalog)
    record = projects.create(
        ProjectCreate(
            problem_description="h by w grid",
            solution_code="int main(){}",
            difficulty="easy",
        )
    )

    saved = projects.save_ai_result(
        record.project_id,
        3,
        {
            "template": "读取 h 行 w 列的字符网格。",
            "structure_tags": [
                {
                    "tag_id": "geometry.grid",
                    "applies_to": "grid",
                    "evidence": "h rows with w characters each",
                }
            ],
        },
        confirmed=False,
        issues=["needs_tag_review：geometry.grid 需人工复核。"],
    )

    assert saved["structure_tags"][0]["tag_id"] == "geometry.grid"
    assert projects.get(record.project_id).stages[3].ai_confirmed is False
