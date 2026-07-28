from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module(name: str, relative_path: str):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(name, root / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


git_index = _load_module("hooks.git_index", "hooks/git_index.py")
chezmoi_render = _load_module("hooks.chezmoi_render", "hooks/chezmoi_render.py")
enforce_portability = _load_module(
    "hooks.enforce_portability", "hooks/enforce_portability.py"
)

GitRepoSnapshot = git_index.GitRepoSnapshot
AddedLine = git_index.AddedLine
StagedTextBlob = git_index.StagedTextBlob
ExpressionOrigin = enforce_portability.ExpressionOrigin
PortabilityFinding = enforce_portability.PortabilityFinding
ChezmoiRenderError = chezmoi_render.ChezmoiRenderError
build_rendered_dictionary = enforce_portability.build_rendered_dictionary
extract_direct_expressions = enforce_portability.extract_direct_expressions
find_portability_findings = enforce_portability.find_portability_findings
diagnostic_for_finding = enforce_portability.diagnostic_for_finding
normalize_expression = enforce_portability.normalize_expression
parse_added_lines = git_index.parse_added_lines


class FakeRenderer:
    def __init__(self, mapping: dict[str, str | None | Exception]):
        self.mapping = mapping
        self.calls: list[tuple[str, str]] = []

    def render_expression(self, path: str, expression: str) -> str | None:
        self.calls.append((path, expression))
        result = self.mapping.get(expression)
        if isinstance(result, Exception):
            raise result
        return result


def test_extracts_only_direct_dotted_expressions() -> None:
    origins = extract_direct_expressions(
        path="dot_config/app.tmpl",
        text="first = {{ .name }}\nsecond = {{   .group.name   }}\n",
    )

    assert origins == [
        ExpressionOrigin(path="dot_config/app.tmpl", line_number=1, expression=".name"),
        ExpressionOrigin(path="dot_config/app.tmpl", line_number=2, expression=".group.name"),
    ]


def test_rejects_non_direct_template_forms() -> None:
    origins = extract_direct_expressions(
        path="dot_config/app.tmpl",
        text=(
            '{{ .name | quote }}\n'
            '{{ env "HOME" }}\n'
            '{{ if .flag }}\n'
            '{{ secret "x" }}\n'
            '{{ .name "extra" }}\n'
            '{{ . }}\n'
        ),
    )

    assert origins == []


def test_normalize_expression_accepts_only_dotted_identifier_paths() -> None:
    assert normalize_expression(" .name ") == ".name"
    assert normalize_expression(".group.name") == ".group.name"
    assert normalize_expression(".") is None
    assert normalize_expression("name") is None
    assert normalize_expression(".group-name") is None
    assert normalize_expression(".1group") is None


def test_parse_added_lines_emits_only_added_hunk_lines() -> None:
    diff = (
        "diff --git a/file.txt b/file.txt\n"
        "--- a/file.txt\n"
        "+++ b/file.txt\n"
        "@@ -1,2 +1,3 @@\n"
        " keep\n"
        "+added one\n"
        "-removed\n"
        "+added two\n"
    )

    assert parse_added_lines(diff) == [
        AddedLine(path="file.txt", line_number=2, text="added one"),
        AddedLine(path="file.txt", line_number=3, text="added two"),
    ]


def test_parse_added_lines_handles_new_files_and_renames() -> None:
    diff = (
        "diff --git a/dev/null b/new.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+first\n"
        "+second\n"
        "diff --git a/old.txt b/renamed.txt\n"
        "similarity index 88%\n"
        "rename from old.txt\n"
        "rename to renamed.txt\n"
        "--- a/old.txt\n"
        "+++ b/renamed.txt\n"
        "@@ -2,0 +3 @@\n"
        "+renamed-addition\n"
    )

    assert parse_added_lines(diff) == [
        AddedLine(path="new.txt", line_number=1, text="first"),
        AddedLine(path="new.txt", line_number=2, text="second"),
        AddedLine(path="renamed.txt", line_number=3, text="renamed-addition"),
    ]


def test_staged_text_blobs_returns_utf8_staged_files_and_skips_binary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    snapshot = GitRepoSnapshot(root=repo)
    text_blob = "greeting = {{ .name }}\n"
    binary_blob = b"\xff\xfe\x00"
    original_run = git_index.subprocess.run

    def fake_run(argv, cwd, check=False, capture_output=False, text=False, input=None):
        class Result:
            def __init__(self, returncode, stdout=b"", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        if argv[:3] == ["git", "ls-files", "-s"]:
            return Result(
                0,
                stdout=(
                    b"100644 abc 0\tdot_config/app.tmpl\n"
                    b"100644 def 0\tbinary.bin\n"
                    b"120000 ghi 0\tlink\n"
                    b"100644 jkl 1\tunmerged.txt\n"
                ),
            )
        if argv[:2] == ["git", "show"] and argv[2] == ":dot_config/app.tmpl":
            return Result(0, stdout=text_blob.encode("utf-8"))
        if argv[:2] == ["git", "show"] and argv[2] == ":binary.bin":
            return Result(0, stdout=binary_blob)
        raise AssertionError(argv)

    git_index.subprocess.run = fake_run
    try:
        blobs = snapshot.staged_text_blobs()
    finally:
        git_index.subprocess.run = original_run

    assert blobs == [StagedTextBlob(path="dot_config/app.tmpl", text=text_blob)]


def test_build_rendered_dictionary_rejects_invalid_ignored_expression() -> None:
    blobs = [StagedTextBlob(path="dot_config/a.tmpl", text="token = {{ .machine.token }}\n")]
    renderer = FakeRenderer({".machine.token": "secret-value"})

    try:
        build_rendered_dictionary(blobs, renderer, ignored_expressions=["not-dotted"])
    except ValueError as exc:
        assert "invalid ignored expression" in str(exc)
    else:
        raise AssertionError("expected invalid ignored expression failure")


def test_repeated_ignored_expressions_remove_normalized_candidates() -> None:
    blobs = [
        StagedTextBlob(
            path="dot_config/a.tmpl",
            text=(
                "first = {{ .machine.token }}\n"
                "second = {{ .other.token }}\n"
            ),
        )
    ]
    renderer = FakeRenderer(
        {
            ".machine.token": "secret-value",
            ".other.token": "other-value",
        }
    )

    rendered = build_rendered_dictionary(
        blobs,
        renderer,
        ignored_expressions=[" .machine.token ", ".machine.token"],
    )

    assert renderer.calls == [("dot_config/a.tmpl", ".other.token")]
    assert rendered == {
        "other-value": (
            ExpressionOrigin(path="dot_config/a.tmpl", line_number=2, expression=".other.token"),
        )
    }


def test_build_rendered_dictionary_retains_duplicate_value_origins_in_deterministic_order() -> None:
    blobs = [
        StagedTextBlob(
            path="dot_config/b.tmpl",
            text=(
                "third = {{ .z.token }}\n"
                "first = {{ .a.token }}\n"
            ),
        ),
        StagedTextBlob(
            path="dot_config/a.tmpl",
            text=(
                "again = {{ .a.token }}\n"
                "second = {{ .m.token }}\n"
            ),
        ),
    ]
    renderer = FakeRenderer(
        {
            ".a.token": "secret-value",
            ".m.token": "secret-value",
            ".z.token": "secret-value",
        }
    )

    rendered = build_rendered_dictionary(blobs, renderer, ignored_expressions=[])

    assert renderer.calls == [
        ("dot_config/b.tmpl", ".a.token"),
        ("dot_config/a.tmpl", ".m.token"),
        ("dot_config/b.tmpl", ".z.token"),
    ]
    assert rendered == {
        "secret-value": (
            ExpressionOrigin(path="dot_config/b.tmpl", line_number=2, expression=".a.token"),
            ExpressionOrigin(path="dot_config/a.tmpl", line_number=1, expression=".a.token"),
            ExpressionOrigin(path="dot_config/a.tmpl", line_number=2, expression=".m.token"),
            ExpressionOrigin(path="dot_config/b.tmpl", line_number=1, expression=".z.token"),
        )
    }


def test_build_rendered_dictionary_skips_ineligible_values() -> None:
    blobs = [
        StagedTextBlob(
            path="dot_config/a.tmpl",
            text=(
                "empty = {{ .empty }}\n"
                "spaces = {{ .spaces }}\n"
                "multi = {{ .multi }}\n"
                "cr = {{ .cr }}\n"
                "bool1 = {{ .bool1 }}\n"
                "bool2 = {{ .bool2 }}\n"
                "num1 = {{ .num1 }}\n"
                "num2 = {{ .num2 }}\n"
                "num3 = {{ .num3 }}\n"
                "short = {{ .short }}\n"
            ),
        )
    ]
    renderer = FakeRenderer(
        {
            ".empty": "",
            ".spaces": "   ",
            ".multi": "line1\nline2",
            ".cr": "line1\rline2",
            ".bool1": "TRUE",
            ".bool2": " false ",
            ".num1": " 12345 ",
            ".num2": "-1",
            ".num3": "3.14",
            ".short": "a b",
        }
    )

    assert build_rendered_dictionary(blobs, renderer, ignored_expressions=[]) == {}


def test_build_rendered_dictionary_propagates_render_failures() -> None:
    blobs = [StagedTextBlob(path="dot_config/a.tmpl", text="token = {{ .machine.token }}\n")]
    renderer = FakeRenderer(
        {".machine.token": ChezmoiRenderError("failed to render expression for portability check")}
    )

    try:
        build_rendered_dictionary(blobs, renderer, ignored_expressions=[])
    except ChezmoiRenderError as exc:
        assert "failed to render expression" in str(exc)
    else:
        raise AssertionError("expected ChezmoiRenderError")


def test_find_portability_findings_and_diagnostic_redact_rendered_value() -> None:
    rendered_dictionary = {
        "secret-value": (
            ExpressionOrigin(path="dot_config/app.tmpl", line_number=1, expression=".machine.token"),
            ExpressionOrigin(path="dot_config/other.tmpl", line_number=3, expression=".other.token"),
        )
    }
    added_lines = [
        AddedLine(path="dot_config/changed.txt", line_number=7, text="token = secret-value"),
        AddedLine(path="dot_config/changed.txt", line_number=8, text="no match here"),
    ]

    findings = find_portability_findings(added_lines, rendered_dictionary)

    assert findings == [
        PortabilityFinding(
            added_line=AddedLine(path="dot_config/changed.txt", line_number=7, text="token = secret-value"),
            origins=(
                ExpressionOrigin(path="dot_config/app.tmpl", line_number=1, expression=".machine.token"),
                ExpressionOrigin(path="dot_config/other.tmpl", line_number=3, expression=".other.token"),
            ),
        )
    ]
    diagnostic = diagnostic_for_finding(findings[0])
    rendered_text = diagnostic.format()
    assert ".machine.token" in rendered_text
    assert ".other.token" in rendered_text
    assert "dot_config/app.tmpl:1" in rendered_text
    assert "dot_config/other.tmpl:3" in rendered_text
    assert "secret-value" not in rendered_text
    assert "token = secret-value" not in rendered_text
