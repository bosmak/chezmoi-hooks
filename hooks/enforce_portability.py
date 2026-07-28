from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import Sequence

from hooks.chezmoi_render import ChezmoiRenderError, ChezmoiRenderer
from hooks.diagnostics import Diagnostic
from hooks.git_index import AddedLine, GitAdapterError, GitRepoSnapshot, StagedTextBlob

HOOK_ID = "chezmoi-enforce-portability"


@dataclass(frozen=True)
class ExpressionOrigin:
    path: str
    line_number: int
    expression: str


@dataclass(frozen=True)
class PortabilityFinding:
    added_line: AddedLine
    origins: tuple[ExpressionOrigin, ...]


_EXPRESSION_PATTERN = re.compile(r"^\.([A-Za-z_][A-Za-z0-9_]*)(\.([A-Za-z_][A-Za-z0-9_]*))*$")
_TEMPLATE_PATTERN = re.compile(r"{{(.*?)}}")
_NUMERIC_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")


def normalize_expression(raw: str) -> str | None:
    value = raw.strip()
    if not _EXPRESSION_PATTERN.fullmatch(value):
        return None
    if value == ".":
        return None
    return value


def extract_direct_expressions(path: str, text: str) -> list[ExpressionOrigin]:
    origins: list[ExpressionOrigin] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in _TEMPLATE_PATTERN.finditer(line):
            expression = normalize_expression(match.group(1))
            if expression is None:
                continue
            origins.append(ExpressionOrigin(path=path, line_number=line_number, expression=expression))
    return origins


def _is_eligible_rendered_value(value: str) -> bool:
    trimmed = value.strip()
    if not trimmed:
        return False
    if "\n" in value or "\r" in value:
        return False
    if trimmed.lower() in {"true", "false"}:
        return False
    if _NUMERIC_PATTERN.fullmatch(trimmed):
        return False
    visible_characters = [character for character in value if not character.isspace()]
    return len(visible_characters) >= 4


def build_rendered_dictionary(
    blobs: Sequence[StagedTextBlob],
    renderer: ChezmoiRenderer,
    ignored_expressions: Sequence[str],
) -> dict[str, tuple[ExpressionOrigin, ...]]:
    ignored: set[str] = set()
    for raw_expression in ignored_expressions:
        normalized = normalize_expression(raw_expression)
        if normalized is None:
            raise ValueError("invalid ignored expression")
        ignored.add(normalized)

    origins_by_expression: dict[str, list[ExpressionOrigin]] = {}
    first_origin_by_expression: dict[str, ExpressionOrigin] = {}
    for blob in blobs:
        for origin in extract_direct_expressions(blob.path, blob.text):
            if origin.expression in ignored:
                continue
            origins_by_expression.setdefault(origin.expression, []).append(origin)
            first_origin_by_expression.setdefault(origin.expression, origin)

    rendered_dictionary: dict[str, list[ExpressionOrigin]] = {}
    for expression in sorted(origins_by_expression):
        first_origin = first_origin_by_expression[expression]
        rendered = renderer.render_expression(first_origin.path, expression)
        if rendered is None or not _is_eligible_rendered_value(rendered):
            continue
        rendered_dictionary.setdefault(rendered, []).extend(origins_by_expression[expression])

    return {
        rendered: tuple(origins)
        for rendered, origins in sorted(rendered_dictionary.items(), key=lambda item: item[0])
    }


def find_portability_findings(
    added_lines: Sequence[AddedLine],
    rendered_dictionary: dict[str, tuple[ExpressionOrigin, ...]],
) -> list[PortabilityFinding]:
    findings: list[PortabilityFinding] = []
    for added_line in added_lines:
        matched_origins: set[ExpressionOrigin] = set()
        for rendered_value, origins in rendered_dictionary.items():
            if rendered_value in added_line.text:
                matched_origins.update(origins)
        if not matched_origins:
            continue
        findings.append(
            PortabilityFinding(
                added_line=added_line,
                origins=tuple(sorted(matched_origins, key=lambda origin: (origin.expression, origin.path, origin.line_number))),
            )
        )
    return sorted(findings, key=lambda finding: (finding.added_line.path, finding.added_line.line_number, tuple(origin.expression for origin in finding.origins)))


def diagnostic_for_finding(finding: PortabilityFinding) -> Diagnostic:
    origin_summary = ", ".join(
        f"{origin.expression} ({origin.path}:{origin.line_number})" for origin in finding.origins
    )
    return Diagnostic(
        hook_id=HOOK_ID,
        path=finding.added_line.path,
        line=finding.added_line.line_number,
        category="portable-expression-available",
        message=f"literal matches candidate expression(s): {origin_summary}",
        action="replace the literal with one of the candidate chezmoi expression(s) or configure --ignore-expression for an intentional exception",
    )


def _failure_diagnostic(category: str, action: str) -> Diagnostic:
    return Diagnostic(
        hook_id=HOOK_ID,
        path="repository",
        line=None,
        category=category,
        message="failed to evaluate portability candidates",
        action=action,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=HOOK_ID, add_help=True)
    parser.add_argument("--ignore-expression", action="append", default=[])
    parser.add_argument("filenames", nargs="*")
    try:
        args = parser.parse_args(argv)
        repo = GitRepoSnapshot.discover()
        renderer = ChezmoiRenderer(cwd=repo.root)
        rendered_dictionary = build_rendered_dictionary(
            repo.staged_text_blobs(),
            renderer,
            ignored_expressions=args.ignore_expression,
        )
        findings = find_portability_findings(repo.added_lines(), rendered_dictionary)
        for finding in findings:
            print(diagnostic_for_finding(finding).format(), file=sys.stderr)
        return 1 if findings else 0
    except ValueError:
        print(_failure_diagnostic("configuration-error", "fix hook arguments and retry").format(), file=sys.stderr)
        return 2
    except (GitAdapterError, ChezmoiRenderError, OSError):
        print(
            _failure_diagnostic(
                "operational-error",
                "fix chezmoi/git prerequisites or rendering configuration and retry after fixing rendering/prerequisites",
            ).format(),
            file=sys.stderr,
        )
        return 2
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
