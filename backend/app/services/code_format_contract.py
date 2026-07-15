from __future__ import annotations

import hashlib
import json
from typing import Any

from app.errors import AppError
from app.models import InputFormatContract


def build_input_format_contract(context: dict[str, Any]) -> InputFormatContract:
    """Freeze the stage-three input format before parallel code generation."""
    input_data = context.get("input", {})
    input_structure = (
        input_data.get("input_structure", {}) if isinstance(input_data, dict) else {}
    )
    template = str(input_structure.get("template") or "").strip()
    if not template:
        raise AppError(
            "INPUT_FORMAT_CONTRACT_MISSING",
            "阶段三没有可用于代码生成的已确认输入格式。",
            stage=3,
            status_code=409,
        )

    problem = input_data.get("problem", {}) if isinstance(input_data, dict) else {}
    sample_inputs = [
        str(item["input"])
        for item in problem.get("samples", [])
        if isinstance(item, dict) and isinstance(item.get("input"), str) and item["input"]
    ]
    payload = {
        "format_version": 1,
        "input_template": template,
        "reference_sample_inputs": sample_inputs,
        "testcase_cardinality": "one_testcase_per_process",
        "encoding": "utf-8",
        "layout_policy": "follow_input_template_exactly",
        "whitespace": {
            "token_separator": "single_ascii_space",
            "leading_space": "forbidden",
            "trailing_space": "forbidden",
            "tab_character": "forbidden",
            "blank_line": "forbidden_unless_template_requires",
            "line_ending": "lf",
            "final_newline": "required",
        },
        "generator_stdout_policy": "input_only_no_diagnostics",
        "validator_consumption_policy": "read_exact_template_then_eof",
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    return InputFormatContract.model_validate(
        {**payload, "format_contract_id": f"format_{digest}"}
    )
