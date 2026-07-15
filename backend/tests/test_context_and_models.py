from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import (
    CodeDraft,
    ConstraintImplementationClaim,
    InputStructureDraft,
    Subtask,
    SubtaskPlanDraft,
)
from app.models import (
    TestPointRuntimeParameters as RuntimeProfile,
)
from app.services.jngen_document_context import JngenDocumentContext
from app.services.model_client import (
    AGENT1_PROMPT,
    AGENT2_PROMPT,
    AGENT3_PROMPT,
    AGENT4_AUDIT_PROMPT,
    AGENT4_GENERATE_PROMPT,
    AGENT4_RECHECK_PROMPT,
    AGENT4_REPAIR_PROMPT,
    OpenAICompatibleAgentModel,
)
from app.services.proof_obligations import (
    Agent4ContractPreflight,
    resolve_implementation_mapping,
)
from app.services.structure_tag_catalog import StructureTagCatalog


def _catalog() -> StructureTagCatalog:
    root = Path(__file__).parents[1] / "app" / "jngen_doc_context"
    return StructureTagCatalog(root)


def test_special_case_count_cannot_exceed_subtask_total() -> None:
    with pytest.raises(ValidationError):
        Subtask(
            id=1,
            constraints="n <= 10",
            test_count=1,
            expected_complexity="O(n)",
            special_cases=[{"count": 2, "description": "boundary"}],
        )


def test_runtime_parameters_must_cover_each_test_point_in_order() -> None:
    with pytest.raises(ValidationError):
        Subtask(
            id=1,
            constraints="n <= 10",
            test_count=2,
            expected_complexity="O(n)",
            runtime_parameters=[
                {
                    "case_id": 2,
                    "parameters": [{"name": "n", "value": 10, "category": "size"}],
                }
            ],
        )


def test_runtime_parameter_names_are_unique_and_reserved_names_rejected() -> None:
    with pytest.raises(ValidationError):
        RuntimeProfile(
            case_id=1,
            parameters=[
                {"name": "seed", "value": 1, "category": "size"},
                {"name": "seed", "value": 2, "category": "size"},
            ],
        )


def test_input_structure_accepts_unstructured_template_text() -> None:
    value = InputStructureDraft(
        template="第一行 n，随后 n 行每行两个整数。",
        structure_tags=[{"tag_id": "primitive.integer", "applies_to": "n", "evidence": "整数"}],
    )
    assert value.template.startswith("第一行")


def test_mixed_structure_route_loads_documents_for_global_and_subtask_tags() -> None:
    root = Path(__file__).parents[1] / "app" / "jngen_doc_context"
    catalog = StructureTagCatalog(root)
    documents = JngenDocumentContext(root, catalog)
    route = documents.route_documents(
        {
            "confirmed_structure_tags": [
                {
                    "tag_id": "graph.directed.weighted",
                    "applies_to": "edges",
                    "evidence": "directed weighted graph",
                }
            ],
            "subtasks": [{"subtask_tags": ["tree"]}],
        },
        200_000,
    )
    assert route is not None
    assert {"graph.md", "generic_graph.md", "tree.md"}.issubset(route["selected_filenames"])


def test_document_index_exposes_digest_symbols_and_targeted_fragments() -> None:
    root = Path(__file__).parents[1] / "app" / "jngen_doc_context"
    documents = JngenDocumentContext(root, StructureTagCatalog(root))
    loaded = documents.load_documents(["getting_started.md", "getopt.md"])
    assert all(len(item["digest"]) == 64 for item in loaded["selected_documents"])
    assert all(item["symbols"] for item in loaded["selected_documents"])
    fragments = documents.repair_fragments(
        loaded,
        {
            "defect_id": "defect_" + "0" * 20,
            "identity": {
                "category": "library_api",
                "constraint_id": "system:getopt",
                "error_code": "GETOPT_MISSING",
            },
            "evidence": {"check": {"issues": ["getOpt runtime parameter"]}},
        },
        8_000,
    )
    assert fragments["selection_method"] == "stable_defect_fragment_index"
    assert sum(len(item["content"]) for item in fragments["selected_fragments"]) <= 8_000


def test_backend_resolves_document_digest_instead_of_asking_model_to_echo_it() -> None:
    digest = "a" * 64
    claims = [
        ConstraintImplementationClaim(
            constraint_id="subtask:1:constraints",
            locations=[
                {
                    "target_file": "generator.cpp",
                    "symbol": "main",
                    "line_start": 1,
                    "line_end": 1,
                }
            ],
            document_evidence=[{"filename": "graph.md", "symbol": "Graph"}],
            test_strategy="按约束构造。",
        )
    ]

    resolved = resolve_implementation_mapping(
        claims,
        [{"filename": "graph.md", "digest": digest, "content": "Graph"}],
    )

    assert resolved[0].document_evidence[0].digest == digest


def test_agent_prompts_are_independent_and_review_operations_are_read_only() -> None:
    assert "Agent1" in AGENT1_PROMPT and "Agent2" not in AGENT1_PROMPT
    assert "Agent2" in AGENT2_PROMPT and "Agent3" not in AGENT2_PROMPT
    assert "Agent3" in AGENT3_PROMPT and "子任务契约" in AGENT3_PROMPT
    assert "implementation_mapping" in AGENT4_GENERATE_PROMPT
    assert "绝对禁止" in AGENT4_AUDIT_PROMPT
    assert "只处理 target_defect" in AGENT4_REPAIR_PROMPT
    assert "不得报告新缺陷" in AGENT4_RECHECK_PROMPT


def test_preflight_builds_generic_proof_obligations() -> None:
    obligations, issues = Agent4ContractPreflight(_catalog()).inspect(
        {
            "confirmed_structure_tags": [
                {
                    "tag_id": "primitive.integer",
                    "applies_to": "n",
                    "evidence": "integer",
                }
            ],
            "subtasks": [
                {
                    "id": 1,
                    "constraints": "1 <= n <= 10",
                    "test_count": 1,
                    "expected_complexity": "O(n)",
                    "special_cases": [{"count": 1, "description": "n=1"}],
                    "runtime_parameters": [
                        {
                            "case_id": 1,
                            "parameters": [{"name": "n", "value": 1, "category": "size"}],
                        }
                    ],
                    "subtask_tags": [],
                }
            ],
        }
    )
    assert issues == []
    ids = {item.constraint_id for item in obligations}
    assert "input:tag:primitive.integer" in ids
    assert "subtask:1:constraints" in ids
    assert "subtask:1:special:1" in ids
    assert "subtask:1:case:1:parameter:n" in ids


def test_code_draft_requires_proof_and_implementation_mapping() -> None:
    with pytest.raises(ValidationError):
        CodeDraft(generator_code="code", validator_code="code")


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class _RecordingClient:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content
        self.requests: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append({"url": url, **kwargs})
        return _Response({"choices": [{"message": {"content": json.dumps(self.content)}}]})


@pytest.mark.asyncio
async def test_remote_model_uses_explicit_operation_and_pydantic_schema() -> None:
    client = _RecordingClient({"defects": []})
    model = OpenAICompatibleAgentModel(Settings(model_api_key="test-key"), client)  # type: ignore[arg-type]
    audit = await model.agent4_audit({}, {}, {"ok": True})

    assert audit.defects == []
    message = json.loads(client.requests[0]["json"]["messages"][1]["content"])
    request_payload = client.requests[0]["json"]
    assert message["operation"] == "agent4.semantic_audit"
    assert message["response_contract"]["title"] == "SemanticAudit"
    assert "JSON" in request_payload["messages"][0]["content"]
    assert "JSON" in message["output_instructions"]
    assert request_payload["response_format"] == {"type": "json_object"}
    assert request_payload["max_tokens"] == 32_768


def test_subtask_plan_ids_remain_unique() -> None:
    with pytest.raises(ValidationError):
        SubtaskPlanDraft(
            subtasks=[
                {
                    "id": 1,
                    "constraints": "small",
                    "test_count": 1,
                    "expected_complexity": "O(n)",
                },
                {
                    "id": 1,
                    "constraints": "large",
                    "test_count": 1,
                    "expected_complexity": "O(n)",
                },
            ]
        )
