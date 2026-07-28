from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class PlannedEdit:
    path: str
    line_number: int
    expected_line: str
    replacement_line: str


class UnsafeEditError(RuntimeError):
    pass


def _split_physical_lines(text: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        eol = line[len(content) :]
        result.append((content, eol))
    if text and not text.endswith(("\n", "\r")):
        last = text.splitlines(keepends=True)[-1]
        if last not in {"\n", "\r", "\r\n"}:
            result[-1] = (last, "")
    return result


def _safe_destination(root: Path, path: str) -> Path:
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise UnsafeEditError(f"unsafe path {path}") from exc
    return target


def apply_worktree_edits(root: Path, edits: Sequence[PlannedEdit]) -> None:
    resolved_root = root.resolve()
    grouped: dict[str, dict[int, PlannedEdit]] = {}
    for edit in edits:
        by_line = grouped.setdefault(edit.path, {})
        if edit.line_number in by_line:
            existing = by_line[edit.line_number]
            if existing == edit:
                continue
            raise UnsafeEditError(f"conflicting edits for {edit.path}:{edit.line_number}")
        by_line[edit.line_number] = edit

    prepared: list[tuple[Path, str]] = []
    for path, by_line in grouped.items():
        file_path = _safe_destination(resolved_root, path)
        if not file_path.exists() or not file_path.is_file():
            raise UnsafeEditError(f"missing destination file {path}")
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            raise UnsafeEditError(f"missing destination file {path}") from exc
        if b"\x00" in raw:
            raise UnsafeEditError(f"unsupported destination content {path}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsafeEditError(f"unsupported destination content {path}") from exc
        lines = _split_physical_lines(text)
        for line_number, edit in by_line.items():
            index = line_number - 1
            if index < 0 or index >= len(lines):
                raise UnsafeEditError(f"missing target line {path}:{line_number}")
            current, _ = lines[index]
            if current != edit.expected_line:
                raise UnsafeEditError(f"unexpected content at {path}:{line_number}")
        for line_number, edit in sorted(by_line.items()):
            index = line_number - 1
            _, eol = lines[index]
            lines[index] = (edit.replacement_line, eol)
        prepared.append((file_path, "".join(content + eol for content, eol in lines)))

    for file_path, text in prepared:
        file_path.write_text(text, encoding="utf-8")
