from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.errors import AppError
from app.models import JngenDocumentSelection
from app.services.structure_tag_catalog import StructureTagCatalog


class JngenDocumentContext:
    """Builds the bounded, structured Jngen context used by Agent4."""

    def __init__(
        self,
        root: Path,
        tag_catalog: StructureTagCatalog | None = None,
    ) -> None:
        self.root = root
        self.tag_catalog = tag_catalog

    def available_filenames(self) -> list[str]:
        if not self.root.is_dir():
            raise AppError(
                "JNGEN_CONTEXT_MISSING",
                "jngen_doc_context 目录不存在。",
                stage=5,
            )
        filenames = sorted(
            path.name
            for path in self.root.iterdir()
            if path.is_file()
            and path.suffix == ".md"
            and not path.name.startswith(".")
        )
        if not filenames:
            raise AppError(
                "JNGEN_CONTEXT_MISSING",
                "jngen_doc_context 中没有可用的 Markdown 文档。",
                stage=5,
            )
        return filenames

    def document_catalog(self, available_filenames: list[str]) -> list[dict[str, Any]]:
        """Return only selection metadata; document bodies stay out of this step."""
        return [
            {
                "filename": filename,
                "content_chars": len(
                    (self.root / filename).read_text(encoding="utf-8").strip()
                ),
            }
            for filename in available_filenames
        ]

    def route_documents(
        self,
        context: dict[str, Any],
        maximum_context_characters: int,
    ) -> dict[str, Any] | None:
        """Resolve docs only from confirmed stage-three structure tags."""
        structure_tags = context.get("confirmed_structure_tags")
        if not isinstance(structure_tags, list) or not structure_tags:
            return None
        if self.tag_catalog is None:
            raise AppError(
                "TAG_CATALOG_MISSING",
                "结构标签目录未加载。",
                stage=3,
            )
        issues = self.tag_catalog.validate_structure_tags(structure_tags)
        if issues:
            raise AppError(
                "STRUCTURE_TAG_REVIEW_REQUIRED",
                "阶段三结构标签需要复核。",
                stage=3,
                status_code=409,
                details={"issues": issues},
            )
        tag_ids = list(
            dict.fromkeys(
                str(item["tag_id"])
                for item in structure_tags
                if isinstance(item, dict) and item.get("tag_id")
            )
        )
        return self.tag_catalog.resolve_documents(
            tag_ids,
            maximum_context_characters,
            validate_conflicts=False,
        )

    def legacy_route_documents(self, context: dict[str, Any]) -> dict[str, Any] | None:
        """Temporary keyword route retained only behind an explicit feature flag."""
        index_path = self.root / "index.json"
        if not index_path.is_file():
            return None
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        topics = index.get("topics")
        if not isinstance(topics, dict):
            return None
        searchable = json.dumps(
            {
                "input": context.get("input", {}),
                "subtasks": context.get("subtasks", []),
            },
            ensure_ascii=False,
        ).casefold()
        matched_topics: list[str] = []
        filenames: list[str] = []
        for topic, metadata in topics.items():
            if not isinstance(metadata, dict):
                continue
            keywords = metadata.get("keywords", [])
            if not any(
                isinstance(keyword, str) and keyword.casefold() in searchable
                for keyword in keywords
            ):
                continue
            matched_topics.append(str(topic))
            filenames.extend(metadata.get("documents", []))
            filenames.extend(metadata.get("dependencies", []))
        if not matched_topics:
            return None
        routed = [*index.get("base_documents", []), *filenames]
        available = set(self.available_filenames())
        selected_filenames = list(
            dict.fromkeys(
                filename
                for filename in routed
                if isinstance(filename, str) and filename in available
            )
        )
        if not selected_filenames:
            return None
        return {
            "route_method": "legacy_keyword_routing",
            "index_version": int(index.get("version", 1)),
            "matched_topics": matched_topics,
            "selected_filenames": selected_filenames,
        }

    def format_selected_documents(
        self,
        available_filenames: list[str],
        selection: JngenDocumentSelection,
    ) -> dict[str, Any]:
        available = set(available_filenames)
        selected = []
        for item in selection.selected_documents:
            if item.filename not in available:
                raise AppError(
                    "JNGEN_DOCUMENT_SELECTION_INVALID",
                    "模型选择了不在候选列表中的 jngen 文档。",
                    stage=5,
                    details={"filename": item.filename},
                )
            content = (self.root / item.filename).read_text(encoding="utf-8").strip()
            selected.append(
                {
                    "filename": item.filename,
                    "reason": item.reason,
                    "content": content,
                }
            )
        return {
            "format_version": 1,
            "selection_method": "multi_round_model_structured_selection",
            "available_filenames": available_filenames,
            "selected_documents": selected,
        }
