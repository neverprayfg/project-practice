from __future__ import annotations

import re

JNGEN_INCLUDE_PATTERN = re.compile(r'#\s*include\s*[<"]jngen\.h[>"]')
TESTLIB_INCLUDE_PATTERN = re.compile(r'#\s*include\s*[<"]testlib\.h[>"]')
JNGEN_API_PATTERN = re.compile(
    r"(?:\brnd\s*\.|\b(?:TArray|Array(?:64|f|p|2d)?|rnds|Tree|Graph|rndg|"
    r"Pointf?|Polygonf?)\b\s*(?:::|[({.]))"
)
GETOPT_ASSIGNMENT_PATTERN = re.compile(
    r'\b(?:auto|(?:std::)?string|bool|int|long\s+long|double)\s+'
    r'(?P<variable>[A-Za-z_]\w*)\s*=\s*'
    r'getOpt\s*\(\s*"(?P<parameter>[^"]+)"[^;]*;'
)
DISCARDED_RANDOM_PATTERN = re.compile(
    r'\b(?:auto|bool|int|long\s+long|double)\s+(?P<variable>[A-Za-z_]\w*)\s*=\s*'
    r'(?:rnd|rndm)\s*\.[^;]+;'
)


def generator_usage_issues(
    code: str,
    required_parameter_names: set[str] | None = None,
    *,
    require_constructive_output: bool = False,
) -> list[str]:
    """Validate either a testlib-only generator or an optional jngen generator."""
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.DOTALL)
    if JNGEN_INCLUDE_PATTERN.search(source):
        return _jngen_usage_issues(
            source,
            required_parameter_names or set(),
            require_constructive_output=require_constructive_output,
        )
    return _testlib_usage_issues(source, required_parameter_names or set())


def _jngen_usage_issues(
    source: str,
    required_parameter_names: set[str],
    *,
    require_constructive_output: bool,
) -> list[str]:
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
    assignments = {
        match.group("parameter"): match.group("variable")
        for match in GETOPT_ASSIGNMENT_PATTERN.finditer(source)
    }
    for name in sorted(required_parameter_names):
        variable = assignments.get(name)
        if variable is None:
            continue
        if _is_discarded(source, variable):
            issues.append(f"运行时参数 {name} 被读取后丢弃，没有实际参与数据构造。")
            continue
        declaration = next(
            match
            for match in GETOPT_ASSIGNMENT_PATTERN.finditer(source)
            if match.group("parameter") == name
        )
        remaining = source[: declaration.start()] + source[declaration.end() :]
        if not re.search(rf"\b{re.escape(variable)}\b", remaining):
            issues.append(f"运行时参数 {name} 被读取后没有实际参与数据构造。")
    for match in DISCARDED_RANDOM_PATTERN.finditer(source):
        if _is_discarded(source, match.group("variable")):
            issues.append("jngen 随机结果被直接丢弃，不能作为有效的数据生成调用。")
    if require_constructive_output and _only_echoes_runtime_parameters(
        source, set(assignments.values())
    ):
        issues.append(
            "存在非 fixed 构造模式，但 generator 只原样输出运行时参数，"
            "没有实现可验证的构造逻辑。"
        )
    return issues


def _is_discarded(source: str, variable: str) -> bool:
    return bool(
        re.search(
            rf"\(\s*void\s*\)\s*{re.escape(variable)}\b|"
            rf"\(\s*void\s*\)\s*\(\s*{re.escape(variable)}\s*\)",
            source,
        )
    )


def _only_echoes_runtime_parameters(source: str, variables: set[str]) -> bool:
    outputs = re.findall(r"\b(?:std::)?cout\s*((?:<<\s*[^;]+)+);", source)
    if not outputs:
        return False
    allowed = re.compile(
        r"^(?:\s*<<\s*(?:"
        + "|".join(re.escape(variable) for variable in sorted(variables))
        + r"|(?:std::)?endl|['\"][^'\"]*['\"]))*\s*$"
    )
    return bool(variables) and all(allowed.fullmatch(output) for output in outputs)


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
