from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any

from app.models import (
    CodeDraft,
    ConstraintImplementation,
    ConstraintImplementationClaim,
    DocumentApiEvidence,
    ProofObligation,
    SubtaskPlanDraft,
)
from app.services.runtime_parameters import (
    runtime_parameter_issues,
    structure_tag_parameter_issues,
)
from app.services.structure_tag_catalog import StructureTagCatalog


class Agent4ContractPreflight:
    """Validates the stage-4 contract before any code-model call is allowed."""

    def __init__(self, tag_catalog: StructureTagCatalog) -> None:
        self.tag_catalog = tag_catalog

    def inspect(self, context: dict[str, Any]) -> tuple[list[ProofObligation], list[str]]:
        issues: list[str] = []
        try:
            plan = SubtaskPlanDraft.model_validate({"subtasks": context.get("subtasks", [])})
        except Exception as exc:
            return [], [f"阶段四契约无法解析：{exc}"]

        global_tags = [
            str(item["tag_id"])
            for item in context.get("confirmed_structure_tags", [])
            if isinstance(item, dict) and item.get("tag_id")
        ]
        if not global_tags:
            issues.append("阶段三没有已确认的结构标签。")
        issues.extend(runtime_parameter_issues(plan))
        issues.extend(structure_tag_parameter_issues(plan, global_tags, self.tag_catalog))
        issues.extend(_runtime_constraint_issues(plan))

        obligations: list[ProofObligation] = []
        for tag_id in global_tags:
            obligations.append(
                ProofObligation(
                    constraint_id=f"input:tag:{tag_id}",
                    scope="global_input",
                    severity="blocker",
                    verification_method="semantic",
                    requirement=f"生成器与校验器必须实现已确认结构标签 {tag_id}。",
                )
            )

        for subtask in plan.subtasks:
            obligations.append(
                ProofObligation(
                    constraint_id=f"subtask:{subtask.id}:constraints",
                    scope=f"subtask:{subtask.id}",
                    severity="blocker",
                    verification_method="semantic",
                    requirement=subtask.constraints,
                )
            )
            for index, special in enumerate(subtask.special_cases, start=1):
                obligations.append(
                    ProofObligation(
                        constraint_id=f"subtask:{subtask.id}:special:{index}",
                        scope=f"subtask:{subtask.id}",
                        severity="blocker",
                        verification_method="counterexample",
                        requirement=(
                            f"至少 {special.count} 个测试点必须明确构造特殊情况："
                            f"{special.description}"
                        ),
                    )
                )
            for profile in subtask.runtime_parameters:
                names = [parameter.name for parameter in profile.parameters]
                if len(names) != len(set(names)):
                    issues.append(f"子任务 {subtask.id} 测试点 {profile.case_id} 的参数名重复。")
                for parameter in profile.parameters:
                    obligations.append(
                        ProofObligation(
                            constraint_id=(
                                f"subtask:{subtask.id}:case:{profile.case_id}:"
                                f"parameter:{parameter.name}"
                            ),
                            scope=f"subtask:{subtask.id}:case:{profile.case_id}",
                            severity="blocker",
                            verification_method="static",
                            requirement=(
                                f"运行时参数 {parameter.name}={parameter.value} "
                                "必须被读取并实际影响构造。"
                            ),
                        )
                    )

        ids = [item.constraint_id for item in obligations]
        if len(ids) != len(set(ids)):
            issues.append("阶段三/四契约生成了重复的约束 ID。")
        return obligations, list(dict.fromkeys(issues))


def _runtime_constraint_issues(plan: SubtaskPlanDraft) -> list[str]:
    """Check arithmetic contract clauses against every concrete runtime profile."""

    issues: list[str] = []
    for subtask in plan.subtasks:
        clauses = [
            item.strip()
            for item in re.split(r"[,;，；]", subtask.constraints)
            if item.strip()
        ]
        for profile in subtask.runtime_parameters:
            parameters = {item.name: item.value for item in profile.parameters}
            for clause in clauses:
                satisfied = _evaluate_constraint_clause(clause, parameters)
                if satisfied is False:
                    issues.append(
                        f"子任务 {subtask.id} 测试点 {profile.case_id} 的运行参数"
                        f"不满足约束“{clause}”。"
                    )
    return issues


def _evaluate_constraint_clause(
    clause: str,
    parameters: dict[str, Any],
) -> bool | None:
    expression = clause.replace("≤", "<=").replace("≥", ">=")
    expression = re.sub(r"(?<![<>=!])=(?!=)", "==", expression)
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return None
    if not isinstance(tree.body, ast.Compare):
        return None
    referenced = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    if not referenced or not referenced.issubset(parameters):
        return None
    try:
        return bool(_evaluate_contract_expression(tree.body, parameters))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _evaluate_contract_expression(node: ast.AST, parameters: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (bool, int, float, str)
    ):
        return node.value
    if isinstance(node, ast.Name):
        return parameters[node.id]
    if isinstance(node, ast.UnaryOp):
        value = _evaluate_contract_expression(node.operand, parameters)
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.USub):
            return -value
        raise ValueError("unsupported unary operator")
    if isinstance(node, ast.BinOp):
        left = _evaluate_contract_expression(node.left, parameters)
        right = _evaluate_contract_expression(node.right, parameters)
        operations = {
            ast.Add: lambda: left + right,
            ast.Sub: lambda: left - right,
            ast.Mult: lambda: left * right,
            ast.Div: lambda: left / right,
            ast.FloorDiv: lambda: left // right,
            ast.Mod: lambda: left % right,
        }
        operation = operations.get(type(node.op))
        if operation is None:
            raise ValueError("unsupported binary operator")
        return operation()
    if isinstance(node, ast.Compare):
        left = _evaluate_contract_expression(node.left, parameters)
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            right = _evaluate_contract_expression(comparator, parameters)
            if not _compare_contract_values(operator, left, right):
                return False
            left = right
        return True
    raise ValueError("unsupported contract expression")


def _compare_contract_values(operator: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(operator, ast.Eq):
        return left == right
    if isinstance(operator, ast.NotEq):
        return left != right
    if isinstance(operator, ast.Lt):
        return left < right
    if isinstance(operator, ast.LtE):
        return left <= right
    if isinstance(operator, ast.Gt):
        return left > right
    if isinstance(operator, ast.GtE):
        return left >= right
    raise ValueError("unsupported comparison operator")


def candidate_revision(candidate: dict[str, Any] | CodeDraft) -> str:
    value = candidate.model_dump(mode="json") if isinstance(candidate, CodeDraft) else candidate
    payload = json.dumps(
        {
            "generator_code": value.get("generator_code", ""),
            "validator_code": value.get("validator_code", ""),
            "proof_obligations": value.get("proof_obligations", []),
            "implementation_mapping": value.get("implementation_mapping", []),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def resolve_implementation_mapping(
    claims: list[ConstraintImplementationClaim],
    documents: list[dict[str, Any]],
) -> list[ConstraintImplementation]:
    """Bind model claims to backend-owned document digests for this run."""

    document_index = {
        str(item.get("filename")): item
        for item in documents
        if isinstance(item, dict) and item.get("filename")
    }
    return [
        ConstraintImplementation(
            constraint_id=claim.constraint_id,
            locations=claim.locations,
            used_parameters=claim.used_parameters,
            document_evidence=[
                DocumentApiEvidence(
                    filename=evidence.filename,
                    digest=(document_index.get(evidence.filename) or {}).get("digest"),
                    symbol=evidence.symbol,
                )
                for evidence in claim.document_evidence
            ],
            test_strategy=claim.test_strategy,
        )
        for claim in claims
    ]


def implementation_mapping_issues(
    draft: CodeDraft,
    documents: dict[str, dict[str, Any]],
) -> list[str]:
    return [item["message"] for item in implementation_mapping_findings(draft, documents)]


def implementation_mapping_findings(
    draft: CodeDraft,
    documents: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    def add(constraint_id: str, target_file: str, error_code: str, message: str) -> None:
        findings.append(
            {
                "constraint_id": constraint_id,
                "target_file": target_file,
                "error_code": error_code,
                "message": message,
            }
        )

    obligations = {item.constraint_id: item for item in draft.proof_obligations}
    mappings = {item.constraint_id: item for item in draft.implementation_mapping}
    for missing in sorted(obligations.keys() - mappings.keys()):
        add(missing, "implementation_mapping", "MAPPING_MISSING", f"约束 {missing} 没有实现映射。")
    for extra in sorted(mappings.keys() - obligations.keys()):
        add(
            extra,
            "implementation_mapping",
            "UNKNOWN_CONSTRAINT",
            f"实现映射引用了不存在的约束 {extra}。",
        )

    sources = {
        "generator.cpp": draft.generator_code.splitlines(),
        "validator.cpp": draft.validator_code.splitlines(),
    }
    for constraint_id, mapping in mappings.items():
        if (
            any(item.target_file == "generator.cpp" for item in mapping.locations)
            and not mapping.document_evidence
        ):
            add(
                constraint_id,
                "generator.cpp",
                "DOCUMENT_EVIDENCE_MISSING",
                f"约束 {constraint_id} 的生成器实现缺少文档/API 证据。",
            )
        for location in mapping.locations:
            lines = sources[location.target_file]
            if location.line_end > len(lines):
                add(
                    constraint_id,
                    location.target_file,
                    "LOCATION_OUT_OF_RANGE",
                    f"约束 {constraint_id} 的实现位置超过 {location.target_file} 文件长度。",
                )
                continue
            source = "\n".join(lines)
            symbol_pattern = rf"\b{re.escape(location.symbol)}\b"
            if re.search(symbol_pattern, source) is None:
                add(
                    constraint_id,
                    location.target_file,
                    "SYMBOL_NOT_FOUND",
                    f"约束 {constraint_id} 声明的符号 {location.symbol} 不存在于目标文件。",
                )
        for evidence in mapping.document_evidence:
            document = documents.get(evidence.filename)
            if document is None:
                add(
                    constraint_id,
                    "implementation_mapping",
                    "DOCUMENT_NOT_READ",
                    f"约束 {constraint_id} 引用了未读取文档 {evidence.filename}。",
                )
                continue
            if evidence.digest != document.get("digest"):
                add(
                    constraint_id,
                    "implementation_mapping",
                    "DOCUMENT_DIGEST_MISMATCH",
                    f"约束 {constraint_id} 的文档摘要与已读取版本不一致。",
                )
            if evidence.symbol not in str(document.get("content", "")):
                add(
                    constraint_id,
                    "implementation_mapping",
                    "API_SYMBOL_NOT_DOCUMENTED",
                    f"约束 {constraint_id} 声明的 API {evidence.symbol} 不存在于已读取文档。",
                )

    parameter_obligations = {
        obligation.constraint_id: obligation.constraint_id.rsplit(":", 1)[-1]
        for obligation in draft.proof_obligations
        if ":parameter:" in obligation.constraint_id
    }
    for constraint_id, parameter in sorted(parameter_obligations.items()):
        mapping = mappings.get(constraint_id)
        if mapping is None:
            continue
        if parameter not in mapping.used_parameters:
            add(
                constraint_id,
                "generator.cpp",
                "PARAMETER_NOT_MAPPED",
                f"运行时参数 {parameter} 没有出现在约束 {constraint_id} 的实现映射中。",
            )
    for parameter in sorted(set(parameter_obligations.values())):
        if not _parameter_affects_code(draft.generator_code, parameter):
            for constraint_id, required in parameter_obligations.items():
                if required == parameter:
                    add(
                        constraint_id,
                        "generator.cpp",
                        "PARAMETER_NO_EFFECT",
                        f"运行时参数 {parameter} 被读取但没有可验证的实际用途。",
                    )
    return list(
        {
            (item["constraint_id"], item["target_file"], item["error_code"], item["message"]): item
            for item in findings
        }.values()
    )


def _parameter_affects_code(code: str, parameter: str) -> bool:
    quoted = re.escape(parameter)
    assignments = list(
        re.finditer(
            rf"\b(?P<name>[A-Za-z_]\w*)\s*=\s*"
            rf"(?:jngen::)?getOpt(?:<[^>]+>)?\(\s*\"{quoted}\"",
            code,
        )
    )
    for assignment in assignments:
        name = assignment.group("name")
        assigned_spans = {
            other.span("name")
            for other in assignments
            if other.group("name") == name
        }
        for usage in re.finditer(rf"\b{re.escape(name)}\b", code):
            if usage.span() in assigned_spans:
                continue
            if _inside_cpp_comment_or_string(code, usage.start()):
                continue
            if _is_cpp_declaration_name(code, usage.start()):
                continue
            return True
    return False


def _inside_cpp_comment_or_string(code: str, position: int) -> bool:
    token_pattern = re.compile(
        r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        re.DOTALL,
    )
    return any(match.start() <= position < match.end() for match in token_pattern.finditer(code))


def _is_cpp_declaration_name(code: str, position: int) -> bool:
    statement_start = max(
        code.rfind(";", 0, position),
        code.rfind("{", 0, position),
        code.rfind("}", 0, position),
        code.rfind("\n", 0, position),
    )
    prefix = code[statement_start + 1 : position]
    if "=" in prefix:
        return False
    return bool(
        re.match(
            r"\s*(?:(?:const|static|unsigned|signed)\s+)*"
            r"(?:auto|bool|int|long(?:\s+long)?|double|std::string|string)\b",
            prefix,
        )
    )
