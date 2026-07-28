from __future__ import annotations

import difflib
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from .chezmoi_render import ChezmoiRenderer
from .diagnostics import Diagnostic
from .fix_plan import PlannedEdit, UnsafeEditError, apply_worktree_edits
from .git_index import GitAdapterError, GitRepoSnapshot, SnapshotText, StagedChange

HOOK_ID = "chezmoi-preserve-templates"
MANUAL_REVIEW_ACTION = "review manually or skip the hook for an intentional source-state change"


@dataclass(frozen=True)
class TemplateViolation:
    path: str
    line: int | None
    category: str
    message: str
    action: str = MANUAL_REVIEW_ACTION


@dataclass(frozen=True)
class TemplatePlan:
    edits: list[PlannedEdit]
    violations: list[TemplateViolation]


def _line_contents(text: str) -> list[str]:
    return text.splitlines()


def _is_template_line(line: str) -> bool:
    return "{{" in line and "}}" in line


def plan_template_preservation(
    path: str,
    head_text: str,
    staged_text: str,
    renderer: ChezmoiRenderer,
) -> TemplatePlan:
    edits: list[PlannedEdit] = []
    violations: list[TemplateViolation] = []
    head_lines = _line_contents(head_text)
    staged_lines = _line_contents(staged_text)
    matcher = difflib.SequenceMatcher(a=head_lines, b=staged_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        old_template_indexes = [index for index in range(i1, i2) if _is_template_line(head_lines[index])]
        if not old_template_indexes:
            continue

        if tag == "replace" and (i2 - i1) == 1 and (j2 - j1) == 1:
            old_line = head_lines[i1]
            new_line = staged_lines[j1]
            rendered = renderer.render_line(path, old_line)
            if rendered is None:
                violations.append(
                    TemplateViolation(
                        path=path,
                        line=j1 + 1,
                        category="manual-review",
                        message="template rendering failed or produced multiple lines",
                    )
                )
                continue
            if rendered == new_line:
                edits.append(
                    PlannedEdit(
                        path=path,
                        line_number=j1 + 1,
                        expected_line=new_line,
                        replacement_line=old_line,
                    )
                )
            else:
                violations.append(
                    TemplateViolation(
                        path=path,
                        line=j1 + 1,
                        category="manual-review",
                        message="template change is not an exact single-line rendered substitution",
                    )
                )
            continue

        message = "template change is structurally ambiguous" if tag == "insert" else "prior templated line was deleted"
        if tag == "replace":
            message = "template change is structurally ambiguous"
        for index in old_template_indexes:
            violations.append(
                TemplateViolation(
                    path=path,
                    line=index + 1,
                    category="manual-review",
                    message=message,
                )
            )
    return TemplatePlan(edits=edits, violations=violations)


def find_restorable_template_edits(
    path: str,
    head_text: str,
    staged_text: str,
    renderer: ChezmoiRenderer,
) -> list[PlannedEdit]:
    return plan_template_preservation(path, head_text, staged_text, renderer).edits


def _print_manual_review(violation: TemplateViolation) -> None:
    print(
        Diagnostic(
            hook_id=HOOK_ID,
            path=violation.path,
            line=violation.line,
            category=violation.category,
            message=violation.message,
            action=violation.action,
        ).format(),
        file=sys.stderr,
    )


def _plan_deleted_template_loss(path: str, head_text: str) -> list[TemplateViolation]:
    return [
        TemplateViolation(
            path=path,
            line=index + 1,
            category="manual-review",
            message="prior templated line was deleted",
        )
        for index, line in enumerate(_line_contents(head_text))
        if _is_template_line(line)
    ]


def _snapshot_text(value: str | SnapshotText | None) -> str | None:
    if isinstance(value, SnapshotText):
        return value.text if value.category == "text" else None
    return value


def _process_change(repo: GitRepoSnapshot, change: StagedChange, renderer: ChezmoiRenderer) -> TemplatePlan:
    status_code = change.status[0]
    if status_code == "A":
        return TemplatePlan(edits=[], violations=[])
    if status_code == "D":
        head_text = _snapshot_text(repo.read_head_text(change.path))
        if head_text is None:
            return TemplatePlan(edits=[], violations=[])
        return TemplatePlan(edits=[], violations=_plan_deleted_template_loss(change.path, head_text))

    head_path = change.old_path or change.path
    head_text = _snapshot_text(repo.read_head_text(head_path))
    staged_text = _snapshot_text(repo.read_index_text(change.path))
    if head_text is None or staged_text is None:
        return TemplatePlan(edits=[], violations=[])
    return plan_template_preservation(change.path, head_text, staged_text, renderer)


def main(argv: Sequence[str] | None = None) -> int:
    _ = argv
    try:
        repo = GitRepoSnapshot.discover()
        renderer = ChezmoiRenderer(cwd=repo.root)
        edits: list[PlannedEdit] = []
        violations: list[TemplateViolation] = []
        for change in repo.staged_changes():
            plan = _process_change(repo, change, renderer)
            edits.extend(plan.edits)
            violations.extend(plan.violations)
        if violations:
            for violation in violations:
                _print_manual_review(violation)
            return 2
        if not edits:
            return 0
        apply_worktree_edits(repo.root, edits)
        for edit in edits:
            diagnostic = Diagnostic(
                hook_id=HOOK_ID,
                path=edit.path,
                line=edit.line_number,
                category="fixed",
                message="restored templated source line",
                action="review and restage, then retry",
            )
            print(diagnostic.format(), file=sys.stderr)
        return 1
    except (GitAdapterError, UnsafeEditError) as exc:
        message = str(exc) if str(exc) else "failed to preserve templates"
        print(f"{HOOK_ID}: {message}", file=sys.stderr)
        return 2
