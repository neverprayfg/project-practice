from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.errors import AppError
from app.models import Stage, TaskType, ToolExecutionResult, ToolRequest
from app.services.sandbox import Sandbox
from app.storage import ProjectStorage


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RevisionArguments(ToolArguments):
    revision_id: str = Field(pattern=r"^[0-9a-f]{12}$")


class PreviewArguments(RevisionArguments):
    subtask_id: int = Field(gt=0)
    seed: int


class ValidateArguments(RevisionArguments):
    preview_id: str = Field(pattern=r"^[0-9a-f]{12}$")


class ReadDocArguments(ToolArguments):
    root: Literal["testlib", "jngen"]
    path: str = Field(min_length=1, max_length=512)


class ListDirArguments(ToolArguments):
    root: Literal["testlib", "jngen"]
    path: str = Field(default=".", min_length=1, max_length=512)
    pattern: str | None = Field(default=None, min_length=1, max_length=128)


class GrepDocArguments(ToolArguments):
    root: Literal["testlib", "jngen"]
    pattern: str = Field(min_length=1, max_length=256)
    path: str = Field(default=".", min_length=1, max_length=512)
    max_results: int = Field(default=20, ge=1, le=100)


class AgentToolGateway:
    READONLY_TOOLS = frozenset({"read_doc", "list_dir", "grep_doc"})
    MAX_READ_BYTES = 16_000
    MAX_DIR_ENTRIES = 200
    MAX_GREP_FILES = 500
    MAX_GREP_BYTES = 2_000_000
    MAX_MATCH_CHARS = 500

    PERMISSIONS: ClassVar[dict[TaskType, dict[str, type[ToolArguments]]]] = {
        TaskType.INPUT_NORMALIZATION: {},
        TaskType.INPUT_STRUCTURE: {},
        TaskType.SUBTASK_PLAN: {},
        TaskType.CODE_DRAFT: {
            "compile_generator": RevisionArguments,
            "preview_generator": PreviewArguments,
            "compile_validator": RevisionArguments,
            "validate_preview": ValidateArguments,
            "read_doc": ReadDocArguments,
            "list_dir": ListDirArguments,
            "grep_doc": GrepDocArguments,
        },
    }

    def __init__(self, settings: Settings, storage: ProjectStorage, sandbox: Sandbox) -> None:
        self.settings = settings
        self.storage = storage
        self.sandbox = sandbox
        self._calls: dict[str, int] = defaultdict(int)

    async def execute(
        self,
        project_id: str,
        stage: int,
        task_type: TaskType,
        request: ToolRequest,
        *,
        run_id: str,
        allowed_tools: frozenset[str] | None = None,
    ) -> ToolExecutionResult:
        started = datetime.now(UTC)
        args_hash = hashlib.sha256(
            json.dumps(request.arguments, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        try:
            arguments = self._authorize(
                project_id,
                stage,
                task_type,
                request,
                run_id,
                allowed_tools,
            )
            output = await self._dispatch(project_id, request.name, arguments)
            result = ToolExecutionResult(tool=request.name, ok=True, output=output)
        except (AppError, ValidationError, OSError) as exc:
            if isinstance(exc, AppError):
                app_error = exc
            elif isinstance(exc, ValidationError):
                app_error = AppError(
                    "TOOL_DENIED",
                    "tool arguments do not match the allowed schema",
                    details=exc.errors(),
                )
            else:
                app_error = AppError(
                    "TOOL_FAILED",
                    "只读文档工具读取失败。",
                    details={"errno": exc.errno},
                )
            result = ToolExecutionResult(
                tool=request.name,
                ok=False,
                error=app_error.payload(),
            )
        self.storage.append_audit(
            project_id,
            {
                "run_id": run_id,
                "task_type": task_type.value,
                "tool": request.name,
                "arguments_hash": args_hash,
                "resource": self._audit_resource(request),
                "ok": result.ok,
                "started_at": started.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
            },
        )
        return result

    @staticmethod
    def _audit_resource(request: ToolRequest) -> dict[str, str] | None:
        if request.name not in AgentToolGateway.READONLY_TOOLS:
            return None
        return {
            key: str(request.arguments[key])
            for key in ("root", "path")
            if key in request.arguments
        }

    def _authorize(
        self,
        project_id: str,
        stage: int,
        task_type: TaskType,
        request: ToolRequest,
        run_id: str,
        allowed_tools: frozenset[str] | None,
    ) -> ToolArguments:
        if stage != Stage.CODE_DRAFT:
            raise AppError("TOOL_DENIED", "agent tools are available only in stage 5", stage=stage)
        record = self.storage.load_record(project_id)
        if record.current_stage != Stage.CODE_DRAFT:
            raise AppError("TOOL_DENIED", "project is not currently in stage 5", stage=stage)
        if allowed_tools is not None and request.name not in allowed_tools:
            raise AppError("TOOL_DENIED", "智能体只能使用只读文档工具。", stage=stage)
        schema = self.PERMISSIONS[task_type].get(request.name)
        if schema is None:
            raise AppError("TOOL_DENIED", "tool is not allowed for this task type", stage=stage)
        self._calls[run_id] += 1
        if self._calls[run_id] > self.settings.max_tool_calls_per_run:
            raise AppError("TOOL_DENIED", "tool call quota exceeded", stage=stage)
        arguments = schema.model_validate(request.arguments)
        if (
            isinstance(arguments, RevisionArguments)
            and arguments.revision_id != self.storage.current_revision(project_id)
        ):
            raise AppError(
                "TOOL_DENIED",
                "revision does not belong to the current draft",
                stage=stage,
            )
        return arguments

    async def _dispatch(
        self, project_id: str, name: str, arguments: ToolArguments
    ) -> dict[str, Any]:
        if name == "compile_generator":
            result = await self.sandbox.compile(project_id, "generator")
            return result.model_dump(mode="json")
        if name == "compile_validator":
            result = await self.sandbox.compile(project_id, "validator")
            return result.model_dump(mode="json")
        if name == "preview_generator":
            assert isinstance(arguments, PreviewArguments)
            compiled = await self.sandbox.compile(project_id, "generator")
            if not compiled.ok:
                return {"compile": compiled.model_dump(mode="json")}
            preview_id = uuid4().hex[:12]
            relative = f"preview/{preview_id}.in"
            generated = await self.sandbox.generate(
                project_id,
                arguments.subtask_id,
                arguments.seed,
                relative,
            )
            payload = {"preview_id": preview_id, "generation": generated.model_dump(mode="json")}
            if generated.ok:
                path = self.storage.project_dir(project_id) / relative
                payload["content"] = path.read_text(encoding="utf-8")[: self.settings.max_log_chars]
            return payload
        if name == "validate_preview":
            assert isinstance(arguments, ValidateArguments)
            preview = (
                self.storage.project_dir(project_id) / "preview" / f"{arguments.preview_id}.in"
            )
            if not preview.is_file():
                raise AppError("TOOL_DENIED", "preview id does not exist")
            compiled = await self.sandbox.compile(project_id, "validator")
            if not compiled.ok:
                return {"compile": compiled.model_dump(mode="json")}
            result = await self.sandbox.validate(project_id, f"preview/{arguments.preview_id}.in")
            return result.model_dump(mode="json")
        if name == "read_doc":
            assert isinstance(arguments, ReadDocArguments)
            path = self._resolve_readonly_path(arguments.root, arguments.path)
            return await asyncio.to_thread(self._read_doc, path, arguments.path)
        if name == "list_dir":
            assert isinstance(arguments, ListDirArguments)
            path = self._resolve_readonly_path(arguments.root, arguments.path)
            return await asyncio.to_thread(
                self._list_dir,
                path,
                arguments.path,
                arguments.pattern,
            )
        if name == "grep_doc":
            assert isinstance(arguments, GrepDocArguments)
            path = self._resolve_readonly_path(arguments.root, arguments.path)
            root = self._readonly_root(arguments.root)
            return await asyncio.to_thread(
                self._grep_doc,
                root,
                path,
                arguments.pattern,
                arguments.max_results,
            )
        raise AppError("TOOL_DENIED", "tool is not implemented")

    def clear_run(self, run_id: str) -> None:
        self._calls.pop(run_id, None)

    def _readonly_root(self, root_name: str) -> Path:
        configured = {
            "testlib": self.settings.testlib_root,
            "jngen": self.settings.jngen_root,
        }.get(root_name)
        if configured is None:
            raise AppError("TOOL_DENIED", "只允许读取 testlib 或 jngen 文档。")
        try:
            root = configured.resolve(strict=True)
        except OSError as exc:
            raise AppError("TOOL_NOT_FOUND", f"{root_name} 文档目录不存在。") from exc
        if not root.is_dir():
            raise AppError("TOOL_NOT_FOUND", f"{root_name} 文档路径不是目录。")
        return root

    def _resolve_readonly_path(self, root_name: str, relative: str) -> Path:
        root = self._readonly_root(root_name)
        requested = Path(relative)
        if requested.is_absolute() or ".." in requested.parts:
            raise AppError("TOOL_DENIED", "文档路径不得越过白名单目录。")

        candidate = root
        for part in requested.parts:
            if part in {"", "."}:
                continue
            candidate /= part
            if candidate.is_symlink():
                raise AppError("TOOL_DENIED", "文档路径不得包含符号链接。")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise AppError("TOOL_NOT_FOUND", f"文档路径不存在：{relative}") from exc
        if not resolved.is_relative_to(root):
            raise AppError("TOOL_DENIED", "文档路径不得越过白名单目录。")
        return resolved

    def _read_doc(self, path: Path, relative: str) -> dict[str, Any]:
        if not path.is_file():
            raise AppError("TOOL_DENIED", "read_doc 只能读取普通文件。")
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(self.MAX_READ_BYTES + 1)
        truncated = len(data) > self.MAX_READ_BYTES
        return {
            "path": relative,
            "content": data[: self.MAX_READ_BYTES].decode("utf-8", errors="replace"),
            "truncated": truncated,
            "size_bytes": size,
        }

    def _list_dir(
        self,
        path: Path,
        relative: str,
        pattern: str | None,
    ) -> dict[str, Any]:
        if not path.is_dir():
            raise AppError("TOOL_DENIED", "list_dir 只能列出目录。")
        entries = [
            child.name + ("/" if child.is_dir() and not child.is_symlink() else "")
            for child in path.iterdir()
        ]
        if pattern:
            entries = [entry for entry in entries if fnmatch.fnmatchcase(entry, pattern)]
        entries.sort()
        return {
            "path": relative,
            "entries": entries[: self.MAX_DIR_ENTRIES],
            "total_entries": len(entries),
            "truncated": len(entries) > self.MAX_DIR_ENTRIES,
        }

    def _grep_doc(
        self,
        root: Path,
        path: Path,
        pattern: str,
        max_results: int,
    ) -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        total_matches = 0
        scanned_files = 0
        scanned_bytes = 0
        scan_truncated = False
        for candidate in self._walk_regular_files(path):
            if scanned_files >= self.MAX_GREP_FILES:
                scan_truncated = True
                break
            remaining = self.MAX_GREP_BYTES - scanned_bytes
            if remaining <= 0:
                scan_truncated = True
                break
            scanned_files += 1
            with candidate.open("rb") as handle:
                data = handle.read(remaining + 1)
            if len(data) > remaining:
                data = data[:remaining]
                scan_truncated = True
            scanned_bytes += len(data)
            if b"\x00" in data:
                continue
            for line_number, line in enumerate(
                data.decode("utf-8", errors="replace").splitlines(),
                start=1,
            ):
                if pattern not in line:
                    continue
                total_matches += 1
                if len(matches) < max_results:
                    matches.append(
                        {
                            "path": candidate.relative_to(root).as_posix(),
                            "line": line_number,
                            "text": line[: self.MAX_MATCH_CHARS],
                        }
                    )
        return {
            "pattern": pattern,
            "matches": matches,
            "total_matches": total_matches,
            "truncated": total_matches > len(matches) or scan_truncated,
            "scanned_files": scanned_files,
            "scanned_bytes": scanned_bytes,
        }

    @staticmethod
    def _walk_regular_files(path: Path) -> Iterator[Path]:
        if path.is_file():
            yield path
            return
        if not path.is_dir():
            raise AppError("TOOL_DENIED", "grep_doc 路径必须是文件或目录。")
        for directory, directory_names, file_names in path.walk():
            directory_names[:] = sorted(
                name for name in directory_names if not (directory / name).is_symlink()
            )
            for name in sorted(file_names):
                candidate = directory / name
                if candidate.is_file() and not candidate.is_symlink():
                    yield candidate
