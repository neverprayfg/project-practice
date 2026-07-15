from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Literal

from app.errors import AppError

FILE_SEPARATOR = "\n\n<<<FILE_SEPARATOR>>>\n\n"
CONTEXT_FORMAT_VERSION = 5
CONTEXT_LOADING_METHOD = "strict_recursive_role_json"
ROLE_LIBRARIES = {
    "generator": ("jngen_context",),
    "validator": ("testlib_context",),
}


class Agent4DocumentContext:
    """Loads every role-owned context file without routing or truncation."""

    def __init__(self, generator_root: Path, validator_root: Path) -> None:
        self.roots = {
            "generator": generator_root,
            "validator": validator_root,
        }

    def load_all_documents(self) -> dict[str, Any]:
        documents: list[dict[str, Any]] = []
        role_filenames: dict[str, list[str]] = {}
        for role, root in self.roots.items():
            filenames = self._available_filenames(root, role)
            role_filenames[role] = [f"{role}/{filename}" for filename in filenames]
            for filename in filenames:
                content = (root / filename).read_text(encoding="utf-8").strip()
                documents.append(
                    {
                        "role": role,
                        "filename": f"{role}/{filename}",
                        "digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "symbols": self._symbols(content),
                        "content": content,
                    }
                )
        role_contexts = {
            role: self._role_json(role) for role in ("generator", "validator")
        }
        return {
            "format_version": CONTEXT_FORMAT_VERSION,
            "loading_method": CONTEXT_LOADING_METHOD,
            "roles": role_filenames,
            "role_contexts": role_contexts,
            "document_count": len(documents),
            "total_characters": sum(len(item["content"]) for item in documents),
            "documents": documents,
        }

    @staticmethod
    def for_role(
        documentation: dict[str, Any], role: Literal["generator", "validator"]
    ) -> dict[str, Any]:
        if (
            documentation.get("format_version") != CONTEXT_FORMAT_VERSION
            or documentation.get("loading_method") != CONTEXT_LOADING_METHOD
        ):
            raise AppError(
                "AGENT4_CONTEXT_CONTRACT_INVALID",
                "阶段五文档上下文不是当前严格递归 JSON 契约。",
                stage=5,
            )
        role_contexts = documentation.get("role_contexts")
        if not isinstance(role_contexts, dict) or not isinstance(
            role_contexts.get(role), dict
        ):
            raise AppError(
                "AGENT4_CONTEXT_CONTRACT_INVALID",
                f"阶段五缺少 {role} 的严格递归 JSON 上下文。",
                stage=5,
            )
        role_context = role_contexts[role]
        if set(role_context) != set(ROLE_LIBRARIES[role]) or any(
            not isinstance(library, dict) or set(library) != {"doc", "example"}
            for library in role_context.values()
        ):
            raise AppError(
                "AGENT4_CONTEXT_CONTRACT_INVALID",
                f"阶段五 {role} 上下文不符合当前库、doc、example 严格结构。",
                stage=5,
            )
        documents = [
            item
            for item in documentation.get("documents", [])
            if isinstance(item, dict) and item.get("role") == role
        ]
        return {
            "active_role": role,
            "library_context": role_context,
            "document_manifest": [
                {
                    key: item[key]
                    for key in ("filename", "digest", "symbols")
                }
                for item in documents
            ],
            "document_count": len(documents),
        }

    def _role_json(self, role: Literal["generator", "validator"]) -> dict[str, Any]:
        root = self.roots[role]
        direct_files = self._visible_files(root)
        directories = self._visible_directories(root)
        actual = {path.name for path in directories}
        expected = set(ROLE_LIBRARIES[role])
        if direct_files or actual != expected:
            raise AppError(
                "AGENT4_CONTEXT_LAYOUT_INVALID",
                f"{role} 上下文根目录必须且只能包含：{', '.join(sorted(expected))}。",
                stage=5,
                details={
                    "unexpected_files": [path.name for path in direct_files],
                    "missing_directories": sorted(expected - actual),
                    "unexpected_directories": sorted(actual - expected),
                },
            )
        return {
            library: self._library_json(root / library, role)
            for library in ROLE_LIBRARIES[role]
        }

    def _library_json(self, root: Path, role: str) -> dict[str, Any]:
        direct_files = self._visible_files(root)
        directories = self._visible_directories(root)
        actual = {path.name for path in directories}
        expected = {"doc", "example"}
        if direct_files or actual != expected:
            raise AppError(
                "AGENT4_CONTEXT_LAYOUT_INVALID",
                f"{root.name} 必须且只能包含 doc 与 example 两个目录。",
                stage=5,
                details={
                    "unexpected_files": [path.name for path in direct_files],
                    "missing_directories": sorted(expected - actual),
                    "unexpected_directories": sorted(actual - expected),
                },
            )
        result = {
            category: self._directory_json(root / category, role)
            for category in ("doc", "example")
        }
        if any(not self._has_text(value) for value in result.values()):
            raise AppError(
                "AGENT4_CONTEXT_EMPTY",
                f"{root.name} 的 doc 与 example 都必须包含上下文文件。",
                stage=5,
            )
        return result

    def _directory_json(self, root: Path, role: str) -> str | dict[str, Any]:
        if not root.is_dir():
            return ""
        files = self._visible_files(root)
        directories = self._visible_directories(root)
        combined = self._combine_files(files, role)
        if not directories:
            return combined
        result: dict[str, Any] = {}
        if combined:
            result["_files"] = combined
        for directory in directories:
            result[directory.name] = self._directory_json(directory, role)
        return result

    def _combine_files(self, files: list[Path], role: str) -> str:
        root = self.roots[role]
        return FILE_SEPARATOR.join(
            f"<<<FILE:{role}/{path.relative_to(root).as_posix()}>>>\n"
            + path.read_text(encoding="utf-8").strip()
            for path in files
        )

    @staticmethod
    def _visible_files(root: Path) -> list[Path]:
        return sorted(
            path for path in root.iterdir() if path.is_file() and not path.name.startswith(".")
        )

    @staticmethod
    def _visible_directories(root: Path) -> list[Path]:
        return sorted(
            path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")
        )

    @staticmethod
    def _has_text(value: str | dict[str, Any]) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        return any(Agent4DocumentContext._has_text(child) for child in value.values())

    @staticmethod
    def _available_filenames(root: Path, role: str) -> list[str]:
        if not root.is_dir():
            raise AppError(
                "AGENT4_CONTEXT_MISSING",
                f"{role} 上下文目录不存在。",
                stage=5,
            )
        filenames = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
        )
        if not filenames:
            raise AppError(
                "AGENT4_CONTEXT_MISSING",
                f"{role} 上下文目录中没有可用文档。",
                stage=5,
            )
        return filenames

    @staticmethod
    def _symbols(content: str) -> list[str]:
        patterns = (
            r"\b[A-Z][A-Za-z0-9_]*(?=::|\s*\()",
            r"\b[a-zA-Z_][A-Za-z0-9_]*(?=\s*\()",
        )
        return sorted(
            {
                match
                for pattern in patterns
                for match in re.findall(pattern, content)
                if len(match) > 2
            }
        )[:200]
