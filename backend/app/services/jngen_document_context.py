from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from app.errors import AppError
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
            if path.is_file() and path.suffix == ".md" and not path.name.startswith(".")
        )
        if not filenames:
            raise AppError(
                "JNGEN_CONTEXT_MISSING",
                "jngen_doc_context 中没有可用的 Markdown 文档。",
                stage=5,
            )
        return filenames

    def route_documents(
        self,
        context: dict[str, Any],
        maximum_context_characters: int,
    ) -> dict[str, Any] | None:
        """Resolve docs from every confirmed tag that Agent4 must implement.

        Stage-three tags describe the global input shape while stage-four tags
        add per-subtask requirements.  Routing only from the former silently
        omits APIs needed by specialized subtasks (for example ``tree.md`` for
        a tree-only subtask), so the document set is the stable union of both.
        """
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
        global_tag_ids = list(
            dict.fromkeys(
                str(item["tag_id"])
                for item in structure_tags
                if isinstance(item, dict) and item.get("tag_id")
            )
        )
        subtask_tag_ids = list(
            dict.fromkeys(
                str(tag_id)
                for subtask in context.get("subtasks", [])
                if isinstance(subtask, dict)
                for tag_id in subtask.get("subtask_tags", [])
                if isinstance(tag_id, str) and tag_id
            )
        )
        tag_ids = list(dict.fromkeys([*global_tag_ids, *subtask_tag_ids]))
        route = self.tag_catalog.resolve_documents(
            tag_ids,
            maximum_context_characters,
            validate_conflicts=False,
        )
        return {
            **route,
            "global_tag_ids": global_tag_ids,
            "subtask_tag_ids": subtask_tag_ids,
        }

    def load_documents(self, filenames: list[str]) -> dict[str, Any]:
        """Load a deterministic, validated document set for Agent4."""
        available_filenames = self.available_filenames()
        available = set(available_filenames)
        selected = []
        for filename in filenames:
            if filename not in available:
                raise AppError(
                    "JNGEN_DOCUMENT_ROUTE_INVALID",
                    "结构标签路由包含不存在的 jngen 文档。",
                    stage=5,
                    details={"filename": filename},
                )
            content = (self.root / filename).read_text(encoding="utf-8").strip()
            selected.append(
                {
                    "filename": filename,
                    "digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "symbols": self._symbols(content),
                    "content": content,
                }
            )
        return {
            "format_version": 1,
            "selection_method": "confirmed_effective_structure_tags",
            "selected_documents": selected,
        }

    def repair_fragments(
        self,
        documentation: dict[str, Any],
        defect: dict[str, Any],
        maximum_characters: int,
        candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Select only indexed fragments related to one stable defect."""
        selected: list[dict[str, Any]] = []
        identity = defect.get("identity", {}) if isinstance(defect, dict) else {}
        evidence = defect.get("evidence", {}) if isinstance(defect, dict) else {}
        terms = {
            str(identity.get("constraint_id") or "").casefold(),
            str(identity.get("category") or "").casefold(),
            str(identity.get("error_code") or "").casefold(),
        }
        check = evidence.get("check", {}) if isinstance(evidence, dict) else {}
        terms.add(str(check.get("operation") or "").casefold())
        terms.update(
            str(item).casefold()
            for item in check.get("issues", [])
            if isinstance(item, str) and item.strip()
        )
        constraint_id = str(identity.get("constraint_id") or "")
        for obligation in (candidate or {}).get("proof_obligations", []):
            if (
                isinstance(obligation, dict)
                and obligation.get("constraint_id") == constraint_id
            ):
                terms.add(str(obligation.get("requirement") or "").casefold())
        mapped_files = {
            str(document_evidence.get("filename"))
            for mapping in (candidate or {}).get("implementation_mapping", [])
            if isinstance(mapping, dict) and mapping.get("constraint_id") == constraint_id
            for document_evidence in mapping.get("document_evidence", [])
            if isinstance(document_evidence, dict) and document_evidence.get("filename")
        }
        ranked_fragments: list[tuple[int, bool, dict[str, Any], dict[str, str]]] = []
        for document in documentation.get("selected_documents", []):
            if not isinstance(document, dict):
                continue
            content = str(document.get("content") or "")
            filename = str(document.get("filename") or "")
            mapped = filename in mapped_files
            for chunk in self._chunks(content):
                indexed_text = "\n".join(
                    (
                        filename,
                        " ".join(str(item) for item in document.get("symbols", [])),
                        chunk["heading"],
                        chunk["content"],
                    )
                )
                score = self._fragment_score(indexed_text, terms)
                ranked_fragments.append((score, mapped, document, chunk))
        ranked_fragments.sort(key=lambda item: (item[1], item[0]), reverse=True)
        for score, mapped, document, chunk in ranked_fragments:
            if len(selected) >= 6:
                break
            if score <= 0 and not mapped:
                continue
            current_size = sum(len(item["content"]) for item in selected)
            if current_size + len(chunk["content"]) > maximum_characters:
                continue
            selected.append(
                {
                    "filename": document.get("filename"),
                    "digest": document.get("digest"),
                    "heading": chunk["heading"],
                    "symbols": self._symbols(chunk["content"]),
                    "content": chunk["content"],
                }
            )
        if not selected:
            for _score, _mapped, document, chunk in ranked_fragments:
                if len(selected) >= 4:
                    break
                current_size = sum(len(item["content"]) for item in selected)
                if current_size + len(chunk["content"]) > maximum_characters:
                    continue
                selected.append(
                    {
                        "filename": document.get("filename"),
                        "digest": document.get("digest"),
                        "heading": chunk["heading"],
                        "symbols": self._symbols(chunk["content"]),
                        "content": chunk["content"],
                    }
                )
        return {
            "format_version": 1,
            "selection_method": "stable_defect_fragment_index",
            "target_defect_id": defect.get("defect_id"),
            "selected_fragments": selected,
        }

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

    @staticmethod
    def _chunks(content: str) -> list[dict[str, str]]:
        chunks: list[dict[str, str]] = []
        heading = "document"
        buffer: list[str] = []
        for line in content.splitlines():
            if line.startswith("#"):
                if buffer:
                    chunks.append({"heading": heading, "content": "\n".join(buffer).strip()})
                heading = line.lstrip("#").strip() or "document"
                buffer = [line]
            else:
                buffer.append(line)
        if buffer:
            chunks.append({"heading": heading, "content": "\n".join(buffer).strip()})
        return [item for item in chunks if item["content"]]

    @staticmethod
    def _fragment_score(content: str, terms: set[str]) -> int:
        lowered = content.casefold()
        tokens = {
            token
            for term in terms
            for token in re.findall(r"[a-z_][a-z0-9_]*", term)
            if len(token) > 2
        }
        return sum(lowered.count(token) for token in tokens)
