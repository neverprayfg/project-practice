from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeSandbox

from app.config import Settings
from app.docker_proxy import is_allowed
from app.models import CodeDraft, ProjectCreate, Stage, TaskType, ToolRequest
from app.services.project_service import ProjectService
from app.services.tool_gateway import AgentToolGateway
from app.storage import ProjectStorage

CODE_DRAFT = CodeDraft(
    generator_code='#include "testlib.h"\nint main(){}',
    validator_code='#include "testlib.h"\nint main(){}',
)


def test_docker_proxy_exposes_only_runner_lifecycle() -> None:
    assert is_allowed("GET", "/version")
    assert is_allowed("POST", "/v1.47/containers/create")
    assert is_allowed("POST", "/v1.47/containers/abc123/start")
    assert is_allowed("DELETE", "/v1.47/containers/abc123?force=1")
    assert not is_allowed("POST", "/v1.47/containers/abc123/exec")
    assert not is_allowed("POST", "/v1.47/build")
    assert not is_allowed("POST", "/v1.47/images/create?fromImage=busybox")
    assert not is_allowed("GET", "/v1.47/volumes")


@pytest.mark.asyncio
async def test_agent_gateway_rejects_shell_and_extra_arguments_before_sandbox(
    tmp_path: Path,
) -> None:
    storage = ProjectStorage(tmp_path)
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(
            problem_description="problem",
            solution_code="int main(){}",
            difficulty="easy",
        )
    )
    projects.mark_solution_compiled(record.project_id, True, None)
    current = projects.get(record.project_id)
    current.current_stage = Stage.CODE_DRAFT
    storage.save_record(current)
    saved = storage.save_code_draft(record.project_id, CODE_DRAFT)
    sandbox = FakeSandbox(tmp_path)
    gateway = AgentToolGateway(Settings(storage_root=tmp_path), storage, sandbox)

    shell = await gateway.execute(
        record.project_id,
        5,
        TaskType.CODE_DRAFT,
        ToolRequest(name="shell", arguments={"command": "id"}),
        run_id="run-shell",
    )
    extra = await gateway.execute(
        record.project_id,
        5,
        TaskType.CODE_DRAFT,
        ToolRequest(
            name="compile_generator",
            arguments={"revision_id": saved.revision_id, "path": "/etc/passwd"},
        ),
        run_id="run-extra",
    )
    assert shell.ok is False
    assert extra.ok is False
    assert sandbox.calls == []


def test_tool_request_accepts_legacy_tool_name_alias() -> None:
    request = ToolRequest.model_validate(
        {"tool_name": "compile_generator", "arguments": {}}
    )

    assert request.model_dump() == {"name": "compile_generator", "arguments": {}}


def readonly_gateway(tmp_path: Path) -> tuple[AgentToolGateway, ProjectStorage, str]:
    testlib_root = tmp_path / "libraries" / "testlib"
    jngen_root = tmp_path / "libraries" / "jngen"
    testlib_root.mkdir(parents=True)
    jngen_root.mkdir(parents=True)
    storage = ProjectStorage(tmp_path / "storage")
    projects = ProjectService(storage)
    record = projects.create(
        ProjectCreate(
            problem_description="problem",
            solution_code="int main(){}",
            difficulty="easy",
        )
    )
    projects.mark_solution_compiled(record.project_id, True, None)
    current = projects.get(record.project_id)
    current.current_stage = Stage.CODE_DRAFT
    storage.save_record(current)
    settings = Settings(
        storage_root=storage.root,
        testlib_root=testlib_root,
        jngen_root=jngen_root,
        model_mode="mock",
    )
    gateway = AgentToolGateway(settings, storage, FakeSandbox(storage.root))
    return gateway, storage, record.project_id


@pytest.mark.asyncio
async def test_read_doc_rejects_traversal_and_nested_symlink(tmp_path: Path) -> None:
    gateway, _storage, project_id = readonly_gateway(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (gateway.settings.testlib_root / "linked").symlink_to(tmp_path, target_is_directory=True)

    traversal = await gateway.execute(
        project_id,
        Stage.CODE_DRAFT,
        TaskType.CODE_DRAFT,
        ToolRequest(name="read_doc", arguments={"root": "testlib", "path": "../secret.txt"}),
        run_id="traversal",
        allowed_tools=AgentToolGateway.READONLY_TOOLS,
    )
    symlink = await gateway.execute(
        project_id,
        Stage.CODE_DRAFT,
        TaskType.CODE_DRAFT,
        ToolRequest(
            name="read_doc",
            arguments={"root": "testlib", "path": "linked/secret.txt"},
        ),
        run_id="symlink",
        allowed_tools=AgentToolGateway.READONLY_TOOLS,
    )

    assert traversal.ok is False
    assert traversal.error and traversal.error["code"] == "TOOL_DENIED"
    assert symlink.ok is False
    assert symlink.error and symlink.error["code"] == "TOOL_DENIED"


@pytest.mark.asyncio
async def test_read_doc_truncates_without_reading_outside_root(tmp_path: Path) -> None:
    gateway, _storage, project_id = readonly_gateway(tmp_path)
    content = "界" * 8_000
    (gateway.settings.jngen_root / "jngen.h").write_text(content, encoding="utf-8")

    result = await gateway.execute(
        project_id,
        Stage.CODE_DRAFT,
        TaskType.CODE_DRAFT,
        ToolRequest(name="read_doc", arguments={"root": "jngen", "path": "jngen.h"}),
        run_id="large-read",
        allowed_tools=AgentToolGateway.READONLY_TOOLS,
    )

    assert result.ok is True
    assert result.output["truncated"] is True
    assert result.output["size_bytes"] == len(content.encode())
    assert len(result.output["content"].encode()) <= AgentToolGateway.MAX_READ_BYTES + 2


@pytest.mark.asyncio
async def test_grep_doc_is_literal_bounded_and_skips_symlinks(tmp_path: Path) -> None:
    gateway, _storage, project_id = readonly_gateway(tmp_path)
    docs = gateway.settings.testlib_root / "docs"
    docs.mkdir()
    (docs / "api.md").write_text(
        "\n".join(f"line {index}: rnd.next" for index in range(50)),
        encoding="utf-8",
    )
    (docs / "outside.md").symlink_to(tmp_path / "missing.md")

    result = await gateway.execute(
        project_id,
        Stage.CODE_DRAFT,
        TaskType.CODE_DRAFT,
        ToolRequest(
            name="grep_doc",
            arguments={
                "root": "testlib",
                "path": "docs",
                "pattern": "rnd.next",
                "max_results": 10,
            },
        ),
        run_id="grep",
        allowed_tools=AgentToolGateway.READONLY_TOOLS,
    )

    assert result.ok is True
    assert len(result.output["matches"]) == 10
    assert result.output["total_matches"] == 50
    assert result.output["truncated"] is True
    assert {match["path"] for match in result.output["matches"]} == {"docs/api.md"}


@pytest.mark.asyncio
async def test_agent_readonly_policy_rejects_compile_tool(tmp_path: Path) -> None:
    gateway, _storage, project_id = readonly_gateway(tmp_path)
    sandbox = gateway.sandbox

    result = await gateway.execute(
        project_id,
        Stage.CODE_DRAFT,
        TaskType.CODE_DRAFT,
        ToolRequest(name="compile_generator", arguments={"revision_id": "0" * 12}),
        run_id="readonly-policy",
        allowed_tools=AgentToolGateway.READONLY_TOOLS,
    )

    assert result.ok is False
    assert result.error and result.error["code"] == "TOOL_DENIED"
    assert isinstance(sandbox, FakeSandbox)
    assert sandbox.calls == []
