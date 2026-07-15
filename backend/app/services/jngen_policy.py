from __future__ import annotations

import re

JNGEN_API_PATTERN = re.compile(
    r"(?:\brnd\s*\.|\b(?:TArray|Array(?:64|f|p|2d)?|rnds|Tree|Graph|rndg|"
    r"Pointf?|Polygonf?)\b\s*(?:::|[({.]))"
)


def jngen_usage_issues(code: str, required_parameter_names: set[str] | None = None) -> list[str]:
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.DOTALL)
    issues: list[str] = []
    if not re.search(r"#\s*include\s*[<\"]jngen\.h[>\"]", source):
        issues.append("generator.cpp 必须包含 jngen.h。")
    if len(re.findall(r"\bregisterGen\s*\(\s*argc\s*,\s*argv\s*\)", source)) != 1:
        issues.append("generator.cpp 必须且只能调用一次 registerGen(argc, argv)。")
    if len(re.findall(r"\bparseArgs\s*\(\s*argc\s*,\s*argv\s*\)", source)) != 1:
        issues.append("generator.cpp 必须且只能调用一次 parseArgs(argc, argv)。")
    if not JNGEN_API_PATTERN.search(source):
        issues.append("generator.cpp 尚未调用从 jngen 文档中选择的数据生成接口。")
    for name in sorted(required_parameter_names or set()):
        if not re.search(rf'\bgetOpt\s*\(\s*"{re.escape(name)}"', source):
            issues.append(f"generator.cpp 必须用 getOpt 读取运行时参数 {name}。")
    return issues
