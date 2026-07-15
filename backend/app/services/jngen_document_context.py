from __future__ import annotations

from pathlib import Path
from typing import Any

from app.errors import AppError
from app.models import JngenDocumentSelection


class JngenDocumentContext:
    """Builds the bounded, structured Jngen context used by Agent4."""

    def __init__(self, root: Path) -> None:
        self.root = root

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
