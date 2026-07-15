from app.services.compiler_diagnostics import parse_compiler_diagnostics


def test_compiler_diagnostics_are_extracted_without_error_specific_rules() -> None:
    stderr = """\
/workspace/generator.cpp:54:40: error: 'class Random' has no member named 'anything'
/workspace/generator.cpp:55:7: warning: unused variable 'x' [-Wunused-variable]
/opt/library.h:10: note: declared here
"""

    diagnostics = parse_compiler_diagnostics(stderr)

    assert diagnostics == [
        {
            "file": "/workspace/generator.cpp",
            "line": 54,
            "column": 40,
            "severity": "error",
            "message": "'class Random' has no member named 'anything'",
        },
        {
            "file": "/workspace/generator.cpp",
            "line": 55,
            "column": 7,
            "severity": "warning",
            "message": "unused variable 'x' [-Wunused-variable]",
        },
        {
            "file": "/opt/library.h",
            "line": 10,
            "severity": "note",
            "message": "declared here",
        },
    ]
