from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.models import Defect, DefectIdentity

VALIDATION_LEVELS = {
    "contract": 0,
    "static": 1,
    "compile": 2,
    "smoke": 3,
    "complete": 4,
    "semantic": 5,
}


def stable_defect_id(identity: DefectIdentity | dict[str, Any]) -> str:
    value = identity.model_dump(mode="json") if isinstance(identity, DefectIdentity) else identity
    canonical = [
        _token(value.get("category"), "unknown"),
        _token(value.get("target_file"), "candidate"),
        _token(value.get("constraint_id"), "system:unknown"),
        _token(value.get("subtask"), "all"),
        _token(value.get("test_point"), "all"),
        _token(value.get("error_code"), "UNKNOWN_ERROR"),
    ]
    digest = hashlib.sha256("\0".join(canonical).encode("utf-8")).hexdigest()[:20]
    return f"defect_{digest}"


def defects_from_execution(execution: dict[str, Any]) -> list[Defect]:
    if execution.get("ok"):
        return []
    category = str(execution.get("failure_category") or "unknown")
    level = str(execution.get("validation_level") or "static")
    if level not in VALIDATION_LEVELS:
        level = "static"
    defects: list[Defect] = []
    failed_checks = [
        item
        for item in execution.get("checks", [])
        if isinstance(item, dict) and (item.get("ok") is False or _nested_result_failed(item))
    ]
    if not failed_checks:
        failed_checks = [
            {
                "operation": category,
                "issues": [str(execution.get("message") or "确定性检查失败。")],
            }
        ]
    for check in failed_checks:
        operation = str(check.get("operation") or category)
        role = str(check.get("role") or "")
        target = str(check.get("target_file") or _target_file(operation, role))
        subtask = str(check.get("subtask_id") or "all")
        test_point = str(check.get("case_id") or "all")
        constraint_id = str(
            check.get("constraint_id")
            or (
                f"subtask:{subtask}:case:{test_point}"
                if subtask != "all" or test_point != "all"
                else f"system:{operation}"
            )
        )
        messages = [str(item) for item in check.get("issues", []) if str(item).strip()]
        diagnostics = check.get("diagnostics", [])
        if not messages and isinstance(diagnostics, list):
            messages = [
                str(item.get("message"))
                for item in diagnostics
                if isinstance(item, dict) and item.get("message")
            ]
        if not messages:
            messages = [str(execution.get("message") or f"{operation} 检查失败。")]
        error_code = str(check.get("error_code") or f"{operation}_failed").upper()
        error_code = re.sub(r"[^A-Z0-9_]+", "_", error_code).strip("_") or "UNKNOWN_ERROR"
        identity = DefectIdentity(
            category=category,
            target_file=target,
            constraint_id=constraint_id,
            subtask=subtask,
            test_point=test_point,
            error_code=error_code,
        )
        defects.append(
            Defect(
                defect_id=stable_defect_id(identity),
                identity=identity,
                severity="blocker",
                validation_level=level,
                message="；".join(messages),
                evidence={"check": check},
            )
        )
    return _deduplicate(defects)


def verification_summary(defects: list[Defect], validation_level: str) -> dict[str, Any]:
    blocker_ids = sorted(item.defect_id for item in defects if item.severity == "blocker")
    return {
        "open_blockers": len(blocker_ids),
        "blocker_ids": blocker_ids,
        "defect_ids": sorted(item.defect_id for item in defects),
        "validation_level": validation_level,
        "validation_rank": VALIDATION_LEVELS.get(validation_level, 0),
    }


def _deduplicate(defects: list[Defect]) -> list[Defect]:
    unique: dict[str, Defect] = {}
    for defect in defects:
        unique.setdefault(defect.defect_id, defect)
    return list(unique.values())


def _token(value: Any, default: str) -> str:
    text = str(value or default).strip().casefold()
    return re.sub(r"\s+", " ", text)


def _nested_result_failed(check: dict[str, Any]) -> bool:
    result = check.get("result")
    return isinstance(result, dict) and result.get("ok") is False


def _target_file(operation: str, role: str) -> str:
    if role in {"generator", "validator", "solution"}:
        return f"{role}.cpp"
    return {
        "generate": "generator.cpp",
        "jngen_usage": "generator.cpp",
        "validate": "validator.cpp",
        "testlib_usage": "validator.cpp",
        "solve": "solution.cpp",
        "implementation_mapping": "implementation_mapping",
        "runtime_parameters": "stage4_contract",
        "jngen_documentation": "document_index",
    }.get(operation, "candidate")


def ledger_digest(counterexample_ids: list[str]) -> str:
    payload = json.dumps(sorted(counterexample_ids), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
