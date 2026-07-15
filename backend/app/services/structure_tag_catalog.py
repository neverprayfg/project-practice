from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.errors import AppError

TAG_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TAG_CATALOG_PATH = APP_ROOT / "structure_context" / "tag_catalog.json"
DEFAULT_JNGEN_DOCUMENT_ROOT = APP_ROOT / "generator_context" / "jngen_context" / "doc"


class StructureTagCatalog:
    def __init__(
        self,
        catalog_path: Path = DEFAULT_TAG_CATALOG_PATH,
        document_root: Path = DEFAULT_JNGEN_DOCUMENT_ROOT,
    ) -> None:
        self.path = catalog_path
        self.document_root = document_root
        self._raw = self._load_and_validate()
        self.version = int(self._raw["version"])
        self.base_documents = list(self._raw["base_documents"])
        self.entries = {str(item["id"]): item for item in self._raw["tags"]}

    def public_view(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tags": [
                {
                    key: item[key]
                    for key in (
                        "id",
                        "display_name",
                        "category",
                        "parent_tags",
                        "status",
                        "required_runtime_parameters",
                        "examples",
                    )
                }
                for item in self._raw["tags"]
            ],
        }

    def model_view(self) -> dict[str, Any]:
        return self.public_view()

    def validate_tag_ids(self, tag_ids: list[str]) -> list[str]:
        issues: list[str] = []
        if len(tag_ids) != len(set(tag_ids)):
            issues.append("结构标签不得重复。")
        unknown = sorted(set(tag_ids) - self.entries.keys())
        if unknown:
            issues.append(f"存在未知结构标签：{', '.join(unknown)}。")
            return issues
        non_supported = [
            tag_id for tag_id in tag_ids if self.entries[tag_id]["status"] != "supported"
        ]
        if non_supported:
            issues.append(
                "needs_tag_review：以下标签暂不支持自动 jngen 路由："
                + ", ".join(non_supported)
                + "。"
            )
        expanded = self.expand(tag_ids) if not unknown else set()
        conflicts = sorted(
            {
                tuple(sorted((tag_id, conflict)))
                for tag_id in expanded
                for conflict in self.entries[tag_id]["conflicts_with"]
                if conflict in expanded
            }
        )
        if conflicts:
            issues.append(
                "结构标签冲突："
                + "；".join(f"{left} / {right}" for left, right in conflicts)
                + "。"
            )
        return issues

    def validate_structure_tags(self, structure_tags: list[Any]) -> list[str]:
        normalized = [
            {
                "tag_id": str(item.get("tag_id") if isinstance(item, dict) else item.tag_id),
                "applies_to": str(
                    item.get("applies_to") if isinstance(item, dict) else item.applies_to
                ).strip(),
            }
            for item in structure_tags
        ]
        tag_ids = [item["tag_id"] for item in normalized]
        issues = [
            issue
            for issue in self.validate_tag_ids(list(dict.fromkeys(tag_ids)))
            if not issue.startswith("结构标签冲突")
        ]
        pairs = [(item["tag_id"], item["applies_to"].casefold()) for item in normalized]
        if len(pairs) != len(set(pairs)):
            issues.append("同一输入部分的结构标签不得重复。")
        if any(tag_id not in self.entries for tag_id in tag_ids):
            return list(dict.fromkeys(issues))
        for scope in {item["applies_to"].casefold() for item in normalized}:
            scoped = [
                item["tag_id"] for item in normalized if item["applies_to"].casefold() == scope
            ]
            expanded = self.expand(scoped)
            conflicts = sorted(
                {
                    tuple(sorted((tag_id, conflict)))
                    for tag_id in expanded
                    for conflict in self.entries[tag_id]["conflicts_with"]
                    if conflict in expanded
                }
            )
            if conflicts:
                issues.append(
                    f"输入部分“{scope}”的结构标签冲突："
                    + "；".join(f"{left} / {right}" for left, right in conflicts)
                    + "。"
                )
        return list(dict.fromkeys(issues))

    def expand(self, tag_ids: list[str]) -> set[str]:
        expanded: set[str] = set()

        def visit(tag_id: str) -> None:
            if tag_id in expanded:
                return
            if tag_id not in self.entries:
                raise AppError(
                    "UNKNOWN_STRUCTURE_TAG",
                    f"未知结构标签：{tag_id}。",
                    stage=3,
                )
            expanded.add(tag_id)
            item = self.entries[tag_id]
            for dependency in [*item["parent_tags"], *item["implies"]]:
                visit(dependency)

        for tag_id in tag_ids:
            visit(tag_id)
        return expanded

    def resolve_documents(
        self,
        tag_ids: list[str],
        maximum_context_characters: int,
        *,
        validate_conflicts: bool = True,
    ) -> dict[str, Any]:
        issues = (
            self.validate_tag_ids(tag_ids)
            if validate_conflicts
            else [
                issue
                for issue in self.validate_tag_ids(tag_ids)
                if not issue.startswith("结构标签冲突")
            ]
        )
        if issues:
            raise AppError(
                "STRUCTURE_TAG_REVIEW_REQUIRED",
                "阶段三结构标签需要复核。",
                stage=3,
                status_code=409,
                details={"issues": issues},
            )
        expanded = self.expand(tag_ids)
        filenames = list(
            dict.fromkeys(
                [
                    *self.base_documents,
                    *(
                        filename
                        for tag_id in sorted(expanded)
                        for filename in self.entries[tag_id]["jngen_documents"]
                    ),
                ]
            )
        )
        selected_characters = sum(
            len((self.document_root / filename).read_text(encoding="utf-8"))
            for filename in filenames
        )
        if selected_characters > maximum_context_characters:
            raise AppError(
                "STRUCTURE_TAG_DOCUMENT_BUDGET_EXCEEDED",
                "已确认标签所需 jngen 文档超过上下文预算。",
                stage=5,
                details={
                    "selected_characters": selected_characters,
                    "maximum_context_characters": maximum_context_characters,
                    "tag_ids": tag_ids,
                },
            )
        return {
            "route_method": "confirmed_effective_structure_tags",
            "catalog_version": self.version,
            "selected_tag_ids": list(tag_ids),
            "expanded_tag_ids": sorted(expanded),
            "selected_filenames": filenames,
            "selected_characters": selected_characters,
        }

    def required_runtime_parameters(self, tag_ids: list[str]) -> set[str]:
        return {
            str(name)
            for tag_id in self.expand(tag_ids)
            for name in self.entries[tag_id]["required_runtime_parameters"]
        }

    def _load_and_validate(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise AppError("TAG_CATALOG_MISSING", "结构标签目录不存在。", stage=3)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        tags = raw.get("tags")
        if not isinstance(tags, list) or not tags:
            raise AppError("TAG_CATALOG_INVALID", "结构标签目录为空。", stage=3)
        ids = [item.get("id") for item in tags if isinstance(item, dict)]
        if len(ids) != len(tags) or len(ids) != len(set(ids)):
            raise AppError("TAG_CATALOG_INVALID", "结构标签 ID 必须唯一。", stage=3)
        entries = {str(item["id"]): item for item in tags}
        available_documents = {
            path.name for path in self.document_root.glob("*.md") if path.is_file()
        }
        required_keys = {
            "id",
            "display_name",
            "category",
            "parent_tags",
            "implies",
            "conflicts_with",
            "jngen_documents",
            "required_runtime_parameters",
            "oi_wiki_topics",
            "status",
            "examples",
        }
        for tag_id, item in entries.items():
            if not TAG_ID_PATTERN.fullmatch(tag_id) or not required_keys.issubset(item):
                raise AppError("TAG_CATALOG_INVALID", f"标签 {tag_id} 格式无效。", stage=3)
            references = [
                *item["parent_tags"],
                *item["implies"],
                *item["conflicts_with"],
            ]
            if any(reference not in entries for reference in references):
                raise AppError("TAG_CATALOG_INVALID", f"标签 {tag_id} 引用未知标签。", stage=3)
            if item["status"] not in {"supported", "manual_only", "unsupported"}:
                raise AppError("TAG_CATALOG_INVALID", f"标签 {tag_id} 状态无效。", stage=3)
            if item["status"] == "supported" and not item["jngen_documents"]:
                raise AppError(
                    "TAG_CATALOG_INVALID", f"标签 {tag_id} 没有 jngen 文档映射。", stage=3
                )
            if set(item["jngen_documents"]) - available_documents:
                raise AppError(
                    "TAG_CATALOG_INVALID", f"标签 {tag_id} 映射了不存在的文档。", stage=3
                )
            if set(item["implies"]).intersection(item["conflicts_with"]):
                raise AppError(
                    "TAG_CATALOG_INVALID", f"标签 {tag_id} 的推导与冲突关系矛盾。", stage=3
                )
        temporary: set[str] = set()
        permanent: set[str] = set()

        def visit(tag_id: str) -> None:
            if tag_id in permanent:
                return
            if tag_id in temporary:
                raise AppError("TAG_CATALOG_INVALID", "结构标签依赖存在环。", stage=3)
            temporary.add(tag_id)
            for target in [*entries[tag_id]["parent_tags"], *entries[tag_id]["implies"]]:
                visit(target)
            temporary.remove(tag_id)
            permanent.add(tag_id)

        for tag_id in entries:
            visit(tag_id)
        for filename in raw.get("base_documents", []):
            if filename not in available_documents:
                raise AppError("TAG_CATALOG_INVALID", "基础 jngen 文档不存在。", stage=3)
        return raw
