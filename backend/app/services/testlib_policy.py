from __future__ import annotations

import re


def validator_usage_issues(code: str) -> list[str]:
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.DOTALL)
    issues: list[str] = []
    if not re.search(r'#\s*include\s*[<"]testlib\.h[>"]', source):
        issues.append("validator.cpp 必须包含 testlib.h。")
    if len(re.findall(r"\bregisterValidation\s*\(\s*argc\s*,\s*argv\s*\)", source)) != 1:
        issues.append("validator.cpp 必须且只能调用一次 registerValidation(argc, argv)。")
    if not re.search(r"\binf\s*\.\s*read[A-Za-z0-9_]*\s*\(", source):
        issues.append("validator.cpp 必须通过 testlib 的 inf.read* 接口读取输入。")
    if len(re.findall(r"\binf\s*\.\s*readEof\s*\(\s*\)", source)) != 1:
        issues.append("validator.cpp 必须且只能调用一次 inf.readEof()。")
    if re.search(r"\b(?:std::)?cin\b|\bscanf\s*\(", source):
        issues.append("validator.cpp 不得使用 cin 或 scanf 绕过 testlib 读入。")
    return issues
