from __future__ import annotations

import re

JNGEN_INCLUDE_PATTERN = re.compile(r'#\s*include\s*[<"]jngen\.h[>"]')
TESTLIB_INCLUDE_PATTERN = re.compile(r'#\s*include\s*[<"]testlib\.h[>"]')
JNGEN_API_PATTERN = re.compile(
    r"(?:\brnd\s*\.|\b(?:TArray|Array(?:64|f|p|2d)?|rnds|Tree|Graph|rndg|"
    r"Pointf?|Polygonf?)\b\s*(?:::|[({.]))"
)


def generator_usage_issues(
    code: str, required_parameter_names: set[str] | None = None
) -> list[str]:
    """Validate either a testlib-only generator or an optional jngen generator."""
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.DOTALL)
    if JNGEN_INCLUDE_PATTERN.search(source):
        return _jngen_usage_issues(source, required_parameter_names or set())
    return _testlib_usage_issues(source, required_parameter_names or set())


def _jngen_usage_issues(source: str, required_parameter_names: set[str]) -> list[str]:
    issues: list[str] = []
    if len(re.findall(r"\bregisterGen\s*\(\s*argc\s*,\s*argv\s*\)", source)) != 1:
        issues.append("jngen generator 必须且只能调用一次 registerGen(argc, argv)。")
    if len(re.findall(r"\bparseArgs\s*\(\s*argc\s*,\s*argv\s*\)", source)) != 1:
        issues.append("jngen generator 必须且只能调用一次 parseArgs(argc, argv)。")
    if not JNGEN_API_PATTERN.search(source):
        issues.append("generator.cpp 包含 jngen.h，但没有调用 jngen 数据生成接口。")
    for name in sorted(required_parameter_names):
        if not re.search(rf'\bgetOpt\s*\(\s*"{re.escape(name)}"', source):
            issues.append(f"jngen generator 必须用 getOpt 读取运行时参数 {name}。")
    return issues


def _testlib_usage_issues(source: str, required_parameter_names: set[str]) -> list[str]:
    issues: list[str] = []
    if not TESTLIB_INCLUDE_PATTERN.search(source):
        issues.append("generator.cpp 必须包含 testlib.h；复杂数据结构可改用 jngen.h。")
    if (
        len(
            re.findall(
                r"\bregisterGen\s*\(\s*argc\s*,\s*argv\s*,\s*1\s*\)",
                source,
            )
        )
        != 1
    ):
        issues.append("testlib generator 必须且只能调用一次 registerGen(argc, argv, 1)。")
    if re.search(
        r"\b(?:TArray|Array(?:64|f|p|2d)?|Tree|Graph|rndg|Pointf?|Polygonf?)\b\s*(?:::|[({.])",
        source,
    ):
        issues.append("generator.cpp 使用了 jngen API，但没有包含 jngen.h。")
    for name in sorted(required_parameter_names):
        if not re.search(
            rf'\bopt\s*<[^>]+>\s*\(\s*"{re.escape(name)}"', source
        ):
            issues.append(f"testlib generator 必须用 opt<T> 读取运行时参数 {name}。")
    return issues
