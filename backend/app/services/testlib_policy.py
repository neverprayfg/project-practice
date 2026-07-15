from __future__ import annotations

import re

STRICT_MULTI_TOKEN_READ_PATTERN = re.compile(
    r"\binf\s*\.\s*read(?:Words|Tokens|Longs|UnsignedLongs|Integers|Ints|Reals|"
    r"Doubles|StrictReals|StrictDoubles)\s*\("
)


def validator_usage_issues(
    code: str, *, requires_ascii_space: bool = False
) -> list[str]:
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.DOTALL)
    issues: list[str] = []
    if not re.search(r'#\s*include\s*[<"]testlib\.h[>"]', source):
        issues.append("validator.cpp 必须包含 testlib.h。")
    if len(re.findall(r"\bregisterValidation\s*\(\s*argc\s*,\s*argv\s*\)", source)) != 1:
        issues.append("validator.cpp 必须且只能调用一次 registerValidation(argc, argv)。")
    if not re.search(r"\binf\s*\.\s*read[A-Za-z0-9_]*\s*\(", source):
        issues.append("validator.cpp 必须通过 testlib 的 inf.read* 接口读取输入。")
    if not re.search(r"\binf\s*\.\s*readEoln\s*\(\s*\)", source):
        issues.append("validator.cpp 必须用 inf.readEoln() 严格检查换行和行尾空格。")
    if requires_ascii_space and not (
        re.search(r"\binf\s*\.\s*readSpace\s*\(\s*\)", source)
        or STRICT_MULTI_TOKEN_READ_PATTERN.search(source)
    ):
        issues.append(
            "输入样例包含同行多 token，validator.cpp 必须用 inf.readSpace() 或 testlib "
            "批量读取接口严格检查单个 ASCII 空格。"
        )
    if len(re.findall(r"\binf\s*\.\s*readEof\s*\(\s*\)", source)) != 1:
        issues.append("validator.cpp 必须且只能调用一次 inf.readEof()。")
    if re.search(r"\binf\s*\.\s*(?:skipBlanks|seekEoln|seekEof)\s*\(", source):
        issues.append("validator.cpp 不得使用跳过空白的宽松接口。")
    if re.search(r"\b(?:std::)?cin\b|\bscanf\s*\(", source):
        issues.append("validator.cpp 不得使用 cin 或 scanf 绕过 testlib 读入。")
    return issues
