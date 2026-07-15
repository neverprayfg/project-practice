from __future__ import annotations

import re

JNGEN_API_PATTERN = re.compile(
    r"(?:\brnd\s*\.|\b(?:TArray|Array(?:64|f|p|2d)?|rnds|Tree|Graph|rndg|"
    r"Pointf?|Polygonf?)\b\s*(?:::|[({.]))"
)


def jngen_usage_issues(code: str) -> list[str]:
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.DOTALL)
    issues: list[str] = []
    if not re.search(r'#\s*include\s*[<\"]jngen\.h[>\"]', source):
        issues.append("generator.cpp 必须包含 jngen.h。")
    if not re.search(r"\bregisterGen\s*\(\s*argc\s*,\s*argv\s*\)", source):
        issues.append("generator.cpp 必须按 jngen 规范调用 registerGen(argc, argv)。")
    if not re.search(r"\bparseArgs\s*\(\s*argc\s*,\s*argv\s*\)", source):
        issues.append("generator.cpp 必须调用 parseArgs(argc, argv) 读取生成参数。")
    if not JNGEN_API_PATTERN.search(source):
        issues.append("generator.cpp 尚未调用从 jngen 文档中选择的数据生成接口。")
    return issues
