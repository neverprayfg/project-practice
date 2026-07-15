from __future__ import annotations

import hashlib
from typing import Any

from app.models import (
    Counterexample,
    CounterexampleLedger,
    CounterexampleRepair,
    Defect,
)
from app.services.candidate_verifier import AGENT4_VERIFIER_REVISION
from app.storage import ProjectStorage


class CounterexampleLedgerService:
    def __init__(self, storage: ProjectStorage) -> None:
        self.storage = storage

    def load(self, project_id: str) -> CounterexampleLedger:
        stored = self.storage.load_agent4_ledger(project_id)
        if stored.get("verifier_revision") != AGENT4_VERIFIER_REVISION:
            ledger = CounterexampleLedger(verifier_revision=AGENT4_VERIFIER_REVISION)
            self.storage.save_agent4_ledger(project_id, ledger.model_dump(mode="json"))
            self.storage.clear_agent4_last_valid_candidate(project_id)
            return ledger
        return CounterexampleLedger.model_validate(stored)

    def observe(
        self,
        project_id: str,
        ledger: CounterexampleLedger,
        defects: list[Defect],
        revision: str,
        *,
        closable_defect_ids: set[str],
    ) -> CounterexampleLedger:
        observed = {item.defect_id: item for item in defects}
        records = {item.defect.defect_id: item for item in ledger.counterexamples}
        for defect_id, defect in observed.items():
            existing = records.get(defect_id)
            if existing is None:
                records[defect_id] = Counterexample(
                    counterexample_id=_counterexample_id(defect_id),
                    defect=defect,
                    status="open",
                    reproduction=_reproduction(defect),
                    first_seen_revision=revision,
                    last_seen_revision=revision,
                )
            else:
                if existing.status == "closed":
                    existing.status = "regressed"
                existing.defect = defect
                existing.last_seen_revision = revision
        for defect_id, existing in records.items():
            if (
                defect_id in closable_defect_ids
                and defect_id not in observed
                and existing.status in {"open", "regressed"}
            ):
                existing.status = "closed"
                existing.last_seen_revision = revision
        ledger.counterexamples = sorted(records.values(), key=lambda item: item.counterexample_id)
        self.storage.save_agent4_ledger(project_id, ledger.model_dump(mode="json"))
        return ledger

    def record_repair(
        self,
        project_id: str,
        ledger: CounterexampleLedger,
        defect_id: str,
        revision: str,
        patch_scope: list[str],
        outcome: str,
        reason: str,
    ) -> CounterexampleLedger:
        for item in ledger.counterexamples:
            if item.defect.defect_id == defect_id:
                item.repair_history.append(
                    CounterexampleRepair(
                        candidate_revision=revision,
                        patch_scope=patch_scope,
                        outcome=outcome,
                        reason=reason,
                    )
                )
                break
        self.storage.save_agent4_ledger(project_id, ledger.model_dump(mode="json"))
        return ledger

    def rollback_repair(
        self,
        project_id: str,
        baseline: CounterexampleLedger,
        rejected: CounterexampleLedger,
        defect_id: str,
        revision: str,
        patch_scope: list[str],
        outcome: str,
        reason: str,
    ) -> CounterexampleLedger:
        """Restore accepted statuses while retaining rejected-patch evidence."""
        records = {item.defect.defect_id: item for item in baseline.counterexamples}
        for rejected_item in rejected.counterexamples:
            current = records.get(rejected_item.defect.defect_id)
            regressed = rejected_item.status == "regressed" or current is None
            if not regressed:
                continue
            if current is None:
                current = rejected_item.model_copy(deep=True)
                current.status = "closed"
                records[rejected_item.defect.defect_id] = current
            current.repair_history.append(
                CounterexampleRepair(
                    candidate_revision=revision,
                    patch_scope=patch_scope,
                    outcome="regressed",
                    reason="被拒绝补丁引入了该反例；回滚后保持关闭。",
                )
            )
        baseline.counterexamples = sorted(
            records.values(), key=lambda item: item.counterexample_id
        )
        return self.record_repair(
            project_id,
            baseline,
            defect_id,
            revision,
            patch_scope,
            outcome,
            reason,
        )


def _counterexample_id(defect_id: str) -> str:
    digest = hashlib.sha256(defect_id.encode("utf-8")).hexdigest()[:20]
    return f"case_{digest}"


def _reproduction(defect: Defect) -> dict[str, Any]:
    check = defect.evidence.get("check", {})
    return {
        "defect_id": defect.defect_id,
        "operation": check.get("operation"),
        "subtask_id": check.get("subtask_id"),
        "case_id": check.get("case_id"),
        "seed": check.get("seed"),
        "runtime_arguments": check.get("runtime_arguments", {}),
    }
