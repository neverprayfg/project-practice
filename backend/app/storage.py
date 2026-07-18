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

from pydantic import ValidationError

from app.errors import AppError
from app.models import (
    CodeDraft,
    CodeRelease,
    GlobalInput,
    ProblemInput,
    ProjectRecord,
    SolutionInput,
    StageRecoverySummary,
    SubtaskPlanDraft,
    TestDataPlanDraft,
)
from app.services.code_candidate import candidate_revision

PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
STAGE_FILES = {
    3: "test_data_plan.json",
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

    def delete_project(self, project_id: str) -> None:
        """Delete a project and its private verification workspace."""
        project_dir = self.project_dir(project_id)
        if not (project_dir / "project.json").is_file():
            raise AppError("PROJECT_NOT_FOUND", "project does not exist", status_code=404)
        workspace_id = hashlib.sha256(f"agent4-verification:{project_id}".encode()).hexdigest()[:32]
        shutil.rmtree(project_dir)
        shutil.rmtree(self.project_dir(workspace_id), ignore_errors=True)

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
        return GlobalInput(
            problem=ProblemInput(
                description=record.problem_description,
                difficulty=record.difficulty,
            ),
            solution=SolutionInput(source=solution.read_text(encoding="utf-8")),
            project_name=record.project_name,
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

    def start_recovery_run(
        self,
        project_id: str,
        *,
        stage: int,
        agent_role: str,
    ) -> StageRecoverySummary:
        run_id = uuid4().hex
        summary = StageRecoverySummary(run_id=run_id, stage=stage)
        run_dir = self.project_dir(project_id) / "logs" / "agent-recovery" / run_id
        self.write_json(
            run_dir / "manifest.json",
            {
                **summary.model_dump(mode="json"),
                "agent_role": agent_role,
                "events": 0,
            },
        )
        record = self.load_record(project_id)
        record.recovery_summaries[stage] = summary
        record.updated_at = datetime.now(UTC)
        self.save_record(record)
        return summary

    def append_recovery_event(
        self,
        project_id: str,
        run_id: str,
        event: dict[str, Any],
    ) -> None:
        run_dir = self.project_dir(project_id) / "logs" / "agent-recovery" / run_id
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            raise AppError("RECOVERY_RUN_NOT_FOUND", "恢复运行记录不存在。", status_code=500)
        operation = str(event.get("operation") or "event")
        generation_round = int(event.get("generation_round") or 0)
        repair_attempt = int(event.get("repair_attempt") or 0)
        suffix = f"repair-{repair_attempt:02d}" if repair_attempt else "generate"
        filename = f"{operation}-{suffix}.json"
        event_path = run_dir / f"round-{generation_round:02d}" / filename
        if event_path.exists():
            event_path = event_path.with_name(f"{event_path.stem}-{uuid4().hex[:8]}.json")
        payload = {
            **event,
            "raw_output": self._bounded_recovery_text(event.get("raw_output")),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self.write_json(event_path, payload)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["events"] = int(manifest.get("events") or 0) + 1
        if operation == "repair":
            manifest["repair_calls"] = int(manifest.get("repair_calls") or 0) + 1
        self.write_json(manifest_path, manifest)
        record = self.load_record(project_id)
        current = record.recovery_summaries.get(int(event.get("stage") or 0))
        if current is not None and current.run_id == run_id:
            errors = event.get("validation_errors")
            last_error = ""
            if isinstance(errors, list):
                last_error = "；".join(
                    str(item.get("message") or "")
                    for item in errors
                    if isinstance(item, dict) and item.get("message")
                )[:1000]
            record.recovery_summaries[current.stage] = current.model_copy(
                update={
                    "generation_round": generation_round,
                    "repair_attempts": int(manifest.get("repair_calls") or 0),
                    "last_error_summary": last_error,
                }
            )
            record.updated_at = datetime.now(UTC)
            self.save_record(record)

    def finish_recovery_run(
        self,
        project_id: str,
        summary: StageRecoverySummary,
    ) -> StageRecoverySummary:
        finished = summary.model_copy(
            update={"finished_at": summary.finished_at or datetime.now(UTC)}
        )
        run_dir = self.project_dir(project_id) / "logs" / "agent-recovery" / finished.run_id
        manifest_path = run_dir / "manifest.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.is_file()
            else {}
        )
        self.write_json(manifest_path, {**manifest, **finished.model_dump(mode="json")})
        record = self.load_record(project_id)
        record.recovery_summaries[finished.stage] = finished
        record.updated_at = datetime.now(UTC)
        self.save_record(record)
        return finished

    def resume_recovery_run(
        self,
        project_id: str,
        summary: StageRecoverySummary,
        *,
        agent_role: str,
    ) -> StageRecoverySummary:
        resumed = summary.model_copy(
            update={"status": "running", "finished_at": None}
        )
        run_dir = self.project_dir(project_id) / "logs" / "agent-recovery" / resumed.run_id
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            return self.start_recovery_run(
                project_id,
                stage=resumed.stage,
                agent_role=agent_role,
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.write_json(manifest_path, {**manifest, **resumed.model_dump(mode="json")})
        record = self.load_record(project_id)
        record.recovery_summaries[resumed.stage] = resumed
        record.updated_at = datetime.now(UTC)
        self.save_record(record)
        return resumed

    def load_recovery_manifest(self, project_id: str, run_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise AppError("RECOVERY_RUN_NOT_FOUND", "恢复运行记录不存在。", status_code=404)
        path = self.project_dir(project_id) / "logs" / "agent-recovery" / run_id / "manifest.json"
        if not path.is_file():
            raise AppError("RECOVERY_RUN_NOT_FOUND", "恢复运行记录不存在。", status_code=404)
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def clear_working_draft(self, project_id: str, stage: int) -> None:
        project_dir = self.project_dir(project_id)
        if stage == 3:
            (project_dir / "state" / STAGE_FILES[3]).unlink(missing_ok=True)
            (project_dir / "state" / STAGE_FILES[4]).unlink(missing_ok=True)
            self._discard_working_template(project_dir)
        elif stage == 4:
            (project_dir / "state" / STAGE_FILES[4]).unlink(missing_ok=True)
            self._discard_working_template(project_dir)
        elif stage == 5:
            self._discard_working_template(project_dir)
        else:
            raise AppError("INVALID_STAGE", "只能清空阶段 3、4 或 5 的工作草稿。", stage=stage)
        self._ensure_code_projection(project_dir)

    @staticmethod
    def _bounded_recovery_text(value: Any) -> str:
        text = value if isinstance(value, str) else ""
        limit = 40_000
        if len(text) <= limit:
            return text
        return f"{text[:20_000]}\n...<truncated {len(text) - limit} chars>...\n{text[-20_000:]}"

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
            return TestDataPlanDraft.model_validate(value).model_dump(mode="json")
        if stage == 4:
            return SubtaskPlanDraft.model_validate(value).model_dump(mode="json")
        try:
            return CodeDraft.model_validate(value).model_dump(mode="json")
        except ValidationError as exc:
            raise _incompatible_agent4_state("code draft", exc) from exc

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

        if stage <= 5:
            if stage < 5:
                self._discard_working_template(project_dir)
                shutil.rmtree(project_dir / "state" / "code-revisions", ignore_errors=True)
                shutil.rmtree(project_dir / "state" / "released-code", ignore_errors=True)
            for filename in (
                "agent4-ledger.json",
                "agent4-cache.json",
                "agent4-last-valid-candidate.json",
            ):
                (project_dir / "state" / filename).unlink(missing_ok=True)
            for filename in ("agent4-decisions.jsonl", "agent4-timings.jsonl"):
                (project_dir / "logs" / filename).unlink(missing_ok=True)
            self._ensure_code_projection(project_dir)

        for filename in ("batch.json",):
            with suppress(FileNotFoundError):
                (project_dir / "state" / filename).unlink()

        for directory in ("preview", "data", "bin", "export"):
            self.clear_directory(project_id, directory)

    def save_code_draft(self, project_id: str, draft: CodeDraft) -> CodeDraft:
        digest = candidate_revision(draft)
        saved = draft.model_copy(update={"revision_id": digest})
        project_dir = self.project_dir(project_id)
        temporary = project_dir / "state" / f".working-code.{uuid4().hex}"
        self.write_text(temporary / "generated" / "generator.cpp", saved.generator_code)
        self.write_text(temporary / "generated" / "validator.cpp", saved.validator_code)
        self.write_json(temporary / STAGE_FILES[5], saved.model_dump(mode="json"))
        current = project_dir / "state" / "current-code"
        temporary_link = project_dir / "state" / f".current-code.{uuid4().hex}"
        previous = self._owned_projection_target(project_dir, current)
        try:
            temporary_link.symlink_to(temporary.name, target_is_directory=True)
            os.replace(temporary_link, current)
            self._set_code_projection(project_dir, Path("state/current-code/generated"))
        finally:
            with suppress(FileNotFoundError):
                temporary_link.unlink()
        if previous is not None and previous != temporary:
            shutil.rmtree(previous, ignore_errors=True)
        return saved

    def freeze_code_release(
        self,
        project_id: str,
        *,
        input_revision: int,
        subtasks_revision: int,
    ) -> CodeRelease:
        draft_raw = self.load_draft(project_id, 5)
        if draft_raw is None:
            raise AppError("PREREQUISITE_REQUIRED", "缺少阶段五工作模板。", stage=5)
        draft = CodeDraft.model_validate(draft_raw)
        if (
            draft.input_revision != input_revision
            or draft.subtasks_revision != subtasks_revision
            or draft.revision_id is None
        ):
            raise AppError(
                "STALE_CODE",
                "当前工作模板不是基于最新 INPUT 和 SUBTASKS 生成的。",
                stage=5,
                status_code=409,
            )
        generator_sha256 = hashlib.sha256(draft.generator_code.encode("utf-8")).hexdigest()
        validator_sha256 = hashlib.sha256(draft.validator_code.encode("utf-8")).hexdigest()
        content_sha256 = hashlib.sha256(
            draft.generator_code.encode("utf-8") + b"\0" + draft.validator_code.encode("utf-8")
        ).hexdigest()
        release = CodeRelease(
            format_contract_id=draft.format_contract_id,
            revision_id=draft.revision_id,
            input_revision=input_revision,
            subtasks_revision=subtasks_revision,
            generator_sha256=generator_sha256,
            validator_sha256=validator_sha256,
            content_sha256=content_sha256,
        )
        project_dir = self.project_dir(project_id)
        release_dir = project_dir / "state" / "released-code"
        existing = self.load_code_release(project_id)
        if existing is not None and existing.content_sha256 == release.content_sha256:
            self._set_code_projection(project_dir, Path("state/released-code/generated"))
            return existing
        temporary = project_dir / "state" / f".released-code.{uuid4().hex}"
        archived: Path | None = None
        try:
            self.write_text(
                temporary / "generated" / "generator.cpp",
                draft.generator_code,
            )
            self.write_text(
                temporary / "generated" / "validator.cpp",
                draft.validator_code,
            )
            self.write_json(temporary / STAGE_FILES[5], draft.model_dump(mode="json"))
            self.write_json(temporary / "release.json", release.model_dump(mode="json"))
            if existing is not None:
                history = project_dir / "state" / "released-code-history"
                history.mkdir(parents=True, exist_ok=True)
                archived = history / existing.revision_id
                if archived.exists():
                    archived = history / f"{existing.revision_id}-{uuid4().hex[:8]}"
                os.replace(release_dir, archived)
            os.replace(temporary, release_dir)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            if archived is not None and archived.exists() and not release_dir.exists():
                os.replace(archived, release_dir)
            raise
        self._set_code_projection(project_dir, Path("state/released-code/generated"))
        return release

    def load_code_release(self, project_id: str) -> CodeRelease | None:
        path = self.project_dir(project_id) / "state" / "released-code" / "release.json"
        if not path.is_file():
            return None
        try:
            return CodeRelease.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as exc:
            raise AppError(
                "CODE_RELEASE_INVALID",
                "冻结的阶段五发布快照元数据无效。",
                stage=5,
                status_code=409,
            ) from exc

    def verify_code_release(self, project_id: str) -> CodeRelease:
        release = self.load_code_release(project_id)
        if release is None:
            raise AppError("PREREQUISITE_REQUIRED", "缺少已确认的代码发布快照。", stage=5)
        root = self.project_dir(project_id) / "state" / "released-code" / "generated"
        generator = root / "generator.cpp"
        validator = root / "validator.cpp"
        if not generator.is_file() or not validator.is_file():
            raise AppError("CODE_RELEASE_INVALID", "冻结的代码发布快照不完整。", stage=5)
        generator_sha256 = hashlib.sha256(generator.read_bytes()).hexdigest()
        validator_sha256 = hashlib.sha256(validator.read_bytes()).hexdigest()
        content_sha256 = hashlib.sha256(
            generator.read_bytes() + b"\0" + validator.read_bytes()
        ).hexdigest()
        if (
            generator_sha256 != release.generator_sha256
            or validator_sha256 != release.validator_sha256
            or content_sha256 != release.content_sha256
        ):
            raise AppError(
                "CODE_RELEASE_HASH_MISMATCH",
                "冻结的代码发布快照内容哈希不匹配。",
                stage=5,
                status_code=409,
            )
        self._set_code_projection(
            self.project_dir(project_id),
            Path("state/released-code/generated"),
        )
        return release

    @staticmethod
    def _owned_projection_target(project_dir: Path, link: Path) -> Path | None:
        if not link.is_symlink():
            return None
        target = Path(os.readlink(link))
        if target.is_absolute() or target.parent != Path("."):
            return None
        return project_dir / "state" / target

    @classmethod
    def _discard_working_template(cls, project_dir: Path) -> None:
        current = project_dir / "state" / "current-code"
        target = cls._owned_projection_target(project_dir, current)
        with suppress(FileNotFoundError):
            current.unlink()
        if target is not None:
            shutil.rmtree(target, ignore_errors=True)
        for path in (project_dir / "state").glob(".working-code.*"):
            shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _ensure_code_projection(project_dir: Path) -> None:
        target = (
            Path("state/released-code/generated")
            if (project_dir / "state" / "released-code" / "release.json").is_file()
            else Path("state/current-code/generated")
        )
        ProjectStorage._set_code_projection(project_dir, target)

    @staticmethod
    def _set_code_projection(project_dir: Path, target: Path) -> None:
        """Atomically point the runner at the working template or frozen release."""

        generated = project_dir / "generated"
        if generated.is_symlink() and Path(os.readlink(generated)) == target:
            return
        if generated.is_symlink() or generated.is_file():
            generated.unlink()
        elif generated.exists():
            shutil.rmtree(generated)
        temporary = project_dir / f".generated.{uuid4().hex}"
        try:
            temporary.symlink_to(target, target_is_directory=True)
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
        workspace_id = hashlib.sha256(f"agent4-verification:{project_id}".encode()).hexdigest()[:32]
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
        release = self.load_code_release(project_id)
        return None if release is None else release.revision_id

    def load_batch_manifest(self, project_id: str) -> dict[str, Any] | None:
        path = self.project_dir(project_id) / "state" / "batch.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None

    def save_batch_manifest(self, project_id: str, value: dict[str, Any]) -> None:
        self.write_json(self.project_dir(project_id) / "state" / "batch.json", value)

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

    def save_generator_analysis(self, project_id: str, value: dict[str, Any]) -> Path:
        path = self.project_dir(project_id) / "state" / "generator-analysis.json"
        self.write_json(path, value)
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


def _incompatible_agent4_state(kind: str, exc: ValidationError) -> AppError:
    return AppError(
        "AGENT4_STATE_INCOMPATIBLE",
        f"阶段五 {kind} 不符合当前严格契约；旧阶段五状态不会被迁移或兼容。",
        stage=5,
        status_code=409,
        details={
            "validation_errors": [
                {
                    "location": ".".join(str(part) for part in error["loc"]),
                    "type": error["type"],
                    "message": error["msg"],
                }
                for error in exc.errors(include_url=False)
            ]
        },
    )
