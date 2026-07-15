from __future__ import annotations

import hashlib
import json
from typing import Any

from app.models import CodeDraft


def candidate_revision(candidate: dict[str, Any] | CodeDraft) -> str:
    value = candidate.model_dump(mode="json") if isinstance(candidate, CodeDraft) else candidate
    payload = json.dumps(
        {
            "format_contract_id": value.get("format_contract_id", ""),
            "generator_code": value.get("generator_code", ""),
            "validator_code": value.get("validator_code", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
