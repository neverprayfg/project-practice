from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.errors import AppError
from app.models import (
    CodeDraft,
    GlobalInput,
    InputStructureDraft,
    ProblemInput,
    ProjectRecord,
    SolutionInput,
    SubtaskPlanDraft,
)
from app.services.proof_obligations import candidate_revision

PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
STAGE_FILES = {
    3: "input_structure.json",
    4: "subtask_plan.json",
    5: "code_review.json",
}


class ProjectStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def project_dir(self, project_id: str) -> Path:
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise AppError("PROJECT_NOT_FOUND", "project does not exist", status_code=404)
        return self.root / project_id

    def project_ids(self) -> list[str]:
        return sorted(
            path.name
            for path in self.root.iterdir()
            if path.is_dir()
            and PROJECT_ID_PATTERN.fullmatch(path.name)
            and (path / "project.json").is_file()
        )

    def initialize(self, record: ProjectRecord, input_data: GlobalInput) -> None:
        project_dir = self.project_dir(record.project_id)
        if project_dir.exists():
            raise AppError("PROJECT_EXISTS", "project already exists", status_code=409)
        for directory in (
            "source",
            "state",
            "preview",
            "data",
            "logs",
            "export",
            "bin",
        ):
            (project_dir / directory).mkdir(parents=True, exist_ok=True)
        (project_dir / "state" / "code-revisions").mkdir(parents=True, exist_ok=True)
        self._ensure_code_projection(project_dir)
        self.write_text(project_dir / "source" / "solution.cpp", input_data.solution.source)
        self.save_input(record.project_id, input_data)
        self.save_record(record)

    def load_input(self, project_id: str) -> GlobalInput:
        path = self.project_dir(project_id) / "state" / "input.json"
        if path.is_file():
            return GlobalInput.model_validate_json(path.read_text(encoding="utf-8"))
        record = self.load_record(project_id)
        solution = self.project_dir(project_id) / "source" / "solution.cpp"
        structure = self.load_draft(project_id, 3)
        return GlobalInput(
            problem=ProblemInput(
                description=record.problem_description,
                difficulty=record.difficulty,
            ),
            solution=SolutionInput(source=solution.read_text(encoding="utf-8")),
            input_structure={
                "template": (structure or {}).get("template", ""),
                "status": "confirmed" if record.stages[3].status.value == "passed" else "draft",
                "revision": record.input_revision,
            },
            revision=record.input_revision,
        )

    def save_input(self, project_id: str, value: GlobalInput) -> None:
        path = self.project_dir(project_id) / "state" / "input.json"
        self.write_json(path, value.model_dump(mode="json"))

    def load_record(self, project_id: str) -> ProjectRecord:
        path = self.project_dir(project_id) / "project.json"
        if not path.is_file():
            raise AppError("PROJECT_NOT_FOUND", "project does not exist", status_code=404)
        return ProjectRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def save_record(self, record: ProjectRecord) -> None:
        path = self.project_dir(record.project_id) / "project.json"
        self.write_json(path, record.model_dump(mode="json"))

    def load_draft(self, project_id: str, stage: int) -> dict[str, Any] | None:
        filename = STAGE_FILES.get(stage)
        if filename is None:
            raise AppError("INVALID_STAGE", "only stages 3, 4 and 5 have drafts", stage=stage)
        project_dir = self.project_dir(project_id)
        path = (
            project_dir / "state" / "current-code" / filename
            if stage == 5
            else project_dir / "state" / filename
        )
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if stage == 3:
            return InputStructureDraft.model_validate(value).model_dump(mode="json")
        if stage == 4:
            return SubtaskPlanDraft.model_validate(value).model_dump(mode="json")
        return value

    def save_draft(self, project_id: str, stage: int, draft: dict[str, Any]) -> None:
        filename = STAGE_FILES.get(stage)
        if filename is None:
            raise AppError("INVALID_STAGE", "only stages 3, 4 and 5 have drafts", stage=stage)
        if stage == 5:
            raise AppError(
                "INVALID_CODE_PUBLISH",
                "阶段五代码必须作为候选 revision 整体发布。",
                stage=5,
            )
        self.write_json(self.project_dir(project_id) / "state" / filename, draft)

    def invalidate_downstream_artifacts(self, project_id: str, stage: int) -> None:
        """Remove active artifacts that must not leak into a restarted workflow."""
        project_dir = self.project_dir(project_id)
        for downstream in range(stage + 1, 6):
            filename = STAGE_FILES[downstream]
            with suppress(FileNotFoundError):
                (project_dir / "state" / filename).unlink()

        if stage < 5:
            with suppress(FileNotFoundError):
                (project_dir / "state" / "current-code").unlink()
            shutil.rmtree(project_dir / "state" / "code-revisions", ignore_errors=True)
            (project_dir / "state" / "code-revisions").mkdir(parents=True, exist_ok=True)
            self._ensure_code_projection(project_dir)

        for filename in (
            "agent4_feedback.json",
            "batch.json",
        ):
            with suppress(FileNotFoundError):
                (project_dir / "state" / filename).unlink()

        if stage < 5:
            for filename in (
                "agent4-ledger.json",
                "agent4-cache.json",
                "agent4-last-valid-candidate.json",
            ):
                with suppress(FileNotFoundError):
                    (project_dir / "state" / filename).unlink()

        for directory in ("preview", "data", "bin", "export"):
            self.clear_directory(project_id, directory)

    def save_code_draft(self, project_id: str, draft: CodeDraft) -> CodeDraft:
        previous_revision = self.current_revision(project_id)
        digest = candidate_revision(draft)
        saved = draft.model_copy(update={"revision_id": digest})
        project_dir = self.project_dir(project_id)
        revisions = project_dir / "state" / "code-revisions"
        revisions.mkdir(parents=True, exist_ok=True)
        revision_dir = revisions / digest
        if not revision_dir.exists():
            temporary = revisions / f".{digest}.{uuid4().hex}"
            try:
                self.write_text(
                    temporary / "generated" / "generator.cpp",
                    saved.generator_code,
                )
                self.write_text(
                    temporary / "generated" / "validator.cpp",
                    saved.validator_code,
                )
                self.write_json(
                    temporary / STAGE_FILES[5], saved.model_dump(mode="json")
                )
                os.replace(temporary, revision_dir)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        self._ensure_code_projection(project_dir)
        current = project_dir / "state" / "current-code"
        temporary_link = project_dir / "state" / f".current-code.{uuid4().hex}"
        try:
            temporary_link.symlink_to(
                Path("code-revisions") / digest,
                target_is_directory=True,
            )
            os.replace(temporary_link, current)
        finally:
            with suppress(FileNotFoundError):
                temporary_link.unlink()
        if previous_revision != digest:
            for filename in ("agent4_feedback.json",):
                with suppress(FileNotFoundError):
                    (project_dir / "state" / filename).unlink()
        return saved

    @staticmethod
    def _ensure_code_projection(project_dir: Path) -> None:
        """Expose one immutable code bundle through the stable generated path."""

        generated = project_dir / "generated"
        expected = Path("state") / "current-code" / "generated"
        if generated.is_symlink() and Path(os.readlink(generated)) == expected:
            return
        if generated.is_symlink() or generated.is_file():
            generated.unlink()
        elif generated.exists():
            shutil.rmtree(generated)
        temporary = project_dir / f".generated.{uuid4().hex}"
        try:
            temporary.symlink_to(expected, target_is_directory=True)
            os.replace(temporary, generated)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def prepare_agent4_verification_workspace(
        self,
        project_id: str,
        draft: CodeDraft,
    ) -> str:
        """Stage a candidate away from the active project for pure verification.

        The workspace id is stable per project so DockerSandbox can reuse compiled
        roles whose source digest did not change between candidate revisions.
        It intentionally has no project.json and is therefore never listed as a
        user project.
        """

        project_dir = self.project_dir(project_id)
        workspace_id = hashlib.sha256(
            f"agent4-verification:{project_id}".encode()
        ).hexdigest()[:32]
        workspace = self.project_dir(workspace_id)
        if (workspace / "project.json").exists():
            raise AppError(
                "VERIFICATION_WORKSPACE_CONFLICT",
                "Agent4 验证工作区与项目目录冲突。",
                stage=5,
            )
        for directory in ("source", "generated", "preview", "data", "bin"):
            (workspace / directory).mkdir(parents=True, exist_ok=True)
        solution = project_dir / "source" / "solution.cpp"
        if not solution.is_file():
            raise AppError("PREREQUISITE_REQUIRED", "缺少已编译标程源码。", stage=5)
        self.write_text(workspace / "source" / "solution.cpp", solution.read_text(encoding="utf-8"))
        self.write_text(workspace / "generated" / "generator.cpp", draft.generator_code)
        self.write_text(workspace / "generated" / "validator.cpp", draft.validator_code)
        self.clear_directory(workspace_id, "preview")
        return workspace_id

    def current_revision(self, project_id: str) -> str | None:
        draft = self.load_draft(project_id, 5)
        return None if draft is None else draft.get("revision_id")

    def load_agent4_feedback(self, project_id: str) -> list[dict[str, Any]]:
        path = self.project_dir(project_id) / "state" / "agent4_feedback.json"
        if not path.is_file():
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []

    def append_agent4_feedback(self, project_id: str, value: dict[str, Any]) -> None:
        feedback = self.load_agent4_feedback(project_id)
        feedback.append(value)
        self.write_json(
            self.project_dir(project_id) / "state" / "agent4_feedback.json",
            feedback[-10:],
        )

    def clear_agent4_feedback(self, project_id: str) -> None:
        path = self.project_dir(project_id) / "state" / "agent4_feedback.json"
        with suppress(FileNotFoundError):
            path.unlink()

    def load_agent4_ledger(self, project_id: str) -> dict[str, Any]:
        path = self.project_dir(project_id) / "state" / "agent4-ledger.json"
        if not path.is_file():
            return {"counterexamples": [], "last_valid_candidate_revision": None}
        value = json.loads(path.read_text(encoding="utf-8"))
        return (
            value
            if isinstance(value, dict)
            else {
                "counterexamples": [],
                "last_valid_candidate_revision": None,
            }
        )

    def save_agent4_ledger(self, project_id: str, value: dict[str, Any]) -> None:
        self.write_json(self.project_dir(project_id) / "state" / "agent4-ledger.json", value)

    def load_agent4_cache(self, project_id: str) -> dict[str, Any]:
        path = self.project_dir(project_id) / "state" / "agent4-cache.json"
        if not path.is_file():
            return {"candidates": {}, "documents": {}}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"candidates": {}, "documents": {}}

    def save_agent4_cache(self, project_id: str, value: dict[str, Any]) -> None:
        self.write_json(self.project_dir(project_id) / "state" / "agent4-cache.json", value)

    def save_agent4_last_valid_candidate(
        self, project_id: str, value: dict[str, Any]
    ) -> None:
        self.write_json(
            self.project_dir(project_id) / "state" / "agent4-last-valid-candidate.json",
            value,
        )

    def clear_agent4_last_valid_candidate(self, project_id: str) -> None:
        path = self.project_dir(project_id) / "state" / "agent4-last-valid-candidate.json"
        with suppress(FileNotFoundError):
            path.unlink()

    def load_agent4_last_valid_candidate(self, project_id: str) -> dict[str, Any] | None:
        path = self.project_dir(project_id) / "state" / "agent4-last-valid-candidate.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None

    def append_agent4_decision(self, project_id: str, entry: dict[str, Any]) -> None:
        path = self.project_dir(project_id) / "logs" / "agent4-decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    def load_agent4_decisions(self, project_id: str) -> list[dict[str, Any]]:
        path = self.project_dir(project_id) / "logs" / "agent4-decisions.jsonl"
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def load_batch_manifest(self, project_id: str) -> dict[str, Any] | None:
        path = self.project_dir(project_id) / "state" / "batch.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None

    def save_batch_manifest(self, project_id: str, value: dict[str, Any]) -> None:
        self.write_json(self.project_dir(project_id) / "state" / "batch.json", value)

    def append_agent4_document_selection(self, project_id: str, entry: dict[str, Any]) -> None:
        """Append a content-free trace of Agent4's document retrieval."""
        path = self.project_dir(project_id) / "logs" / "agent4-document-selection.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"timestamp": datetime.now(UTC).isoformat(), **entry}
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")

    def append_agent4_timing(self, project_id: str, entry: dict[str, Any]) -> None:
        """Append one stage-5 timing event without candidate or source content."""
        path = self.project_dir(project_id) / "logs" / "agent4-timings.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"timestamp": datetime.now(UTC).isoformat(), **entry}
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")

    def load_agent4_timings(self, project_id: str) -> list[dict[str, Any]]:
        path = self.project_dir(project_id) / "logs" / "agent4-timings.jsonl"
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def clear_directory(self, project_id: str, name: str) -> Path:
        if name not in {"preview", "data", "bin", "export"}:
            raise ValueError("unsupported managed directory")
        path = self.project_dir(project_id) / name
        path.mkdir(parents=True, exist_ok=True)
        for child in path.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink()
        return path

    def clear_subtask_data(self, project_id: str, subtask_ids: set[int]) -> Path:
        path = self.project_dir(project_id) / "data"
        path.mkdir(parents=True, exist_ok=True)
        prefixes = tuple(f"{subtask_id}_" for subtask_id in sorted(subtask_ids))
        for child in path.iterdir():
            if child.is_file() and child.name.startswith(prefixes):
                child.unlink()
        return path

    @staticmethod
    def write_json(path: Path, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        ProjectStorage.write_text(path, payload)

    @staticmethod
    def write_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(value.replace("\r\n", "\n").replace("\r", "\n"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        except Exception:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)
            raise
