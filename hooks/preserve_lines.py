from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Sequence

from .diagnostics import Diagnostic
from .fix_plan import PlannedEdit, UnsafeEditError, apply_worktree_edits
from .git_index import GitAdapterError, GitRepoSnapshot, SnapshotText

HOOK_ID = "chezmoi-preserve-lines"


@dataclass(frozen=True)
class PreserveSelector:
    path: str
    pattern: re.Pattern[str]
    source: str


class PreserveLineError(ValueError):
    pass


_SNAPSHOT_LABEL = {"HEAD": "HEAD", "index": "index"}


def _validate_selector_path(path: str, source: str) -> str:
    candidate = PurePosixPath(path)
    if not path or "\x00" in path:
        raise PreserveLineError(f"invalid selector '{source}': unsafe repository path; action: use a normalized repository-relative path")
    if path != candidate.as_posix() or path.endswith("/"):
        raise PreserveLineError(f"invalid selector '{source}': unsafe repository path; action: use a normalized repository-relative path")
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise PreserveLineError(f"invalid selector '{source}': unsafe repository path; action: use a normalized repository-relative path")
    return path


def parse_preserve_selector(value: str) -> PreserveSelector:
    path, separator, pattern = value.partition(":")
    if not separator or not path or not pattern:
        raise PreserveLineError(f"invalid selector '{value}'; action: use PATH:REGEX")
    safe_path = _validate_selector_path(path, value)
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise PreserveLineError(f"invalid selector '{value}': {exc.msg}; action: fix the regular expression") from exc
    return PreserveSelector(path=safe_path, pattern=compiled, source=value)


def _find_matches(text: str, selector: PreserveSelector) -> list[tuple[int, str]]:
    matches: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if selector.pattern.search(line):
            matches.append((line_number, line))
    return matches


def _coerce_snapshot_result(path: str, snapshot: Literal["HEAD", "index"], value: str | SnapshotText | None) -> SnapshotText:
    if isinstance(value, SnapshotText):
        return value
    if isinstance(value, str):
        return SnapshotText(path=path, snapshot=snapshot, text=value, category="text")
    return SnapshotText(path=path, snapshot=snapshot, text=None, category="missing")


def _snapshot_failure(selector: PreserveSelector, result: SnapshotText) -> PreserveLineError:
    snapshot = _SNAPSHOT_LABEL[result.snapshot]
    if result.category == "unborn-head":
        return PreserveLineError(
            f"{HOOK_ID}: selector '{selector.source}' cannot read HEAD for {selector.path}: unborn HEAD; action: create the first commit before preserving lines"
        )
    if result.category == "missing":
        return PreserveLineError(
            f"{HOOK_ID}: selector '{selector.source}' cannot read {snapshot} path {selector.path}: missing tracked file; action: choose a selector that matches an existing tracked file in {snapshot}"
        )
    if result.category == "unsupported-mode":
        return PreserveLineError(
            f"{HOOK_ID}: selector '{selector.source}' cannot read {snapshot} path {selector.path}: unsupported mode {result.mode or 'unknown'}; action: target a regular tracked text file"
        )
    if result.category in {"binary", "undecodable"}:
        return PreserveLineError(
            f"{HOOK_ID}: selector '{selector.source}' cannot read {snapshot} path {selector.path}: unsuitable protected content; action: target a UTF-8 tracked text file"
        )
    if result.category == "git-error":
        return PreserveLineError(
            f"{HOOK_ID}: selector '{selector.source}' cannot read {snapshot} path {selector.path}: git error; action: resolve repository errors and retry"
        )
    return PreserveLineError(
        f"{HOOK_ID}: selector '{selector.source}' cannot read {snapshot} path {selector.path}; action: review the selector and tracked file state"
    )


def _match_once(selector: PreserveSelector, result: SnapshotText) -> tuple[int, str]:
    matches = _find_matches(result.text or "", selector)
    if len(matches) != 1:
        raise PreserveLineError(
            f"{HOOK_ID}: selector '{selector.source}' matched {len(matches)} lines in {_SNAPSHOT_LABEL[result.snapshot]} for {selector.path}; action: choose a selector that matches exactly one tracked line"
        )
    return matches[0]


def plan_preserved_line_edits(
    selectors: Sequence[PreserveSelector],
    read_head,
    read_index,
) -> list[PlannedEdit]:
    edits: list[PlannedEdit] = []
    planned_targets: dict[tuple[str, int], PreserveSelector] = {}
    for selector in selectors:
        head_result = _coerce_snapshot_result(selector.path, "HEAD", read_head(selector.path))
        index_result = _coerce_snapshot_result(selector.path, "index", read_index(selector.path))
        if head_result.category != "text":
            raise _snapshot_failure(selector, head_result)
        if index_result.category != "text":
            raise _snapshot_failure(selector, index_result)

        _, head_line = _match_once(selector, head_result)
        staged_line_number, staged_line = _match_once(selector, index_result)
        if head_line == staged_line:
            continue

        target = (selector.path, staged_line_number)
        if target in planned_targets:
            prior = planned_targets[target]
            raise PreserveLineError(
                f"{HOOK_ID}: selectors '{prior.source}' and '{selector.source}' both target {selector.path}:{staged_line_number}; action: keep only one selector for that destination line"
            )
        planned_targets[target] = selector
        edits.append(
            PlannedEdit(
                path=selector.path,
                line_number=staged_line_number,
                expected_line=staged_line,
                replacement_line=head_line,
            )
        )
    return edits


def find_preserved_line_edits(selectors: Sequence[PreserveSelector], snapshot: GitRepoSnapshot) -> list[PlannedEdit]:
    return plan_preserved_line_edits(selectors, snapshot.read_head_entry, snapshot.read_index_entry)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=HOOK_ID)
    parser.add_argument("--preserve", action="append", required=True)
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        selectors = [parse_preserve_selector(value) for value in args.preserve]
        repo = GitRepoSnapshot.discover()
        edits = find_preserved_line_edits(selectors, repo)
        if not edits:
            return 0
        apply_worktree_edits(repo.root, edits)
        for edit in edits:
            print(
                Diagnostic(
                    hook_id=HOOK_ID,
                    path=edit.path,
                    line=edit.line_number,
                    category="fixed",
                    message="restored selected line from HEAD",
                    action="review and restage, then retry",
                ).format(),
                file=sys.stderr,
            )
        return 1
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    except (argparse.ArgumentError, PreserveLineError, GitAdapterError, UnsafeEditError) as exc:
        message = str(exc) if str(exc) else f"{HOOK_ID}: invalid arguments"
        if not message.startswith(f"{HOOK_ID}:"):
            message = f"{HOOK_ID}: {message}"
        print(message, file=sys.stderr)
        return 2
