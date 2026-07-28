from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class GitAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class StagedTextBlob:
    path: str
    text: str


@dataclass(frozen=True)
class AddedLine:
    path: str
    line_number: int
    text: str


_HUNK_PATTERN = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")


@dataclass(frozen=True)
class StagedChange:
    path: str
    status: str
    old_path: str | None = None


@dataclass(frozen=True)
class SnapshotText:
    path: str
    snapshot: Literal["HEAD", "index"]
    text: str | None
    category: Literal["text", "unborn-head", "missing", "unsupported-mode", "binary", "undecodable", "git-error"]
    detail: str | None = None
    mode: str | None = None


@dataclass(frozen=True)
class GitRepoSnapshot:
    root: Path
    git: str = "git"

    @classmethod
    def discover(cls, start: Path | None = None) -> "GitRepoSnapshot":
        cwd = start or Path.cwd()
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitAdapterError(result.stderr.strip() or "failed to discover git repository")
        return cls(root=Path(result.stdout.strip()))

    def staged_changes(self) -> list[StagedChange]:
        result = subprocess.run(
            [self.git, "diff", "--cached", "--name-status", "-z"],
            cwd=self.root,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitAdapterError(stderr or "failed to list staged changes")

        parts = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        if parts and parts[-1] == "":
            parts.pop()

        changes: list[StagedChange] = []
        index = 0
        while index < len(parts):
            status = parts[index]
            index += 1
            if not status:
                continue
            code = status[0]
            if code in {"R", "C"}:
                if index + 1 >= len(parts):
                    raise GitAdapterError("failed to parse staged rename/copy entry")
                old_path = parts[index]
                path = parts[index + 1]
                index += 2
                changes.append(StagedChange(path=path, status=status, old_path=old_path))
                continue
            if index >= len(parts):
                raise GitAdapterError("failed to parse staged entry")
            path = parts[index]
            index += 1
            changes.append(StagedChange(path=path, status=status, old_path=None))
        return changes

    def changed_paths(self) -> list[str]:
        return [change.path for change in self.staged_changes() if change.status.startswith("M")]

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(args, cwd=self.root, check=False, capture_output=True)

    def _head_exists(self) -> bool:
        return self._run([self.git, "rev-parse", "--verify", "HEAD"]).returncode == 0

    def _decode_blob(self, path: str, snapshot: Literal["HEAD", "index"], content: bytes, *, mode: str | None = None) -> SnapshotText:
        if b"\x00" in content:
            return SnapshotText(path=path, snapshot=snapshot, text=None, category="binary", mode=mode)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return SnapshotText(path=path, snapshot=snapshot, text=None, category="undecodable", mode=mode)
        return SnapshotText(path=path, snapshot=snapshot, text=text, category="text", mode=mode)

    def read_head_entry(self, path: str) -> SnapshotText:
        if not self._head_exists():
            return SnapshotText(path=path, snapshot="HEAD", text=None, category="unborn-head")

        entry = self._run([self.git, "ls-tree", "-z", "HEAD", "--", path])
        if entry.returncode != 0:
            return SnapshotText(path=path, snapshot="HEAD", text=None, category="git-error", detail=entry.stderr.decode("utf-8", errors="replace").strip() or "failed to inspect HEAD entry")
        if not entry.stdout:
            return SnapshotText(path=path, snapshot="HEAD", text=None, category="missing")

        header, _, _name = entry.stdout.partition(b"\t")
        mode = header.split(b" ", 1)[0].decode("ascii", errors="replace")
        if mode not in {"100644", "100755"}:
            return SnapshotText(path=path, snapshot="HEAD", text=None, category="unsupported-mode", mode=mode)

        content = self._run([self.git, "show", f"HEAD:{path}"])
        if content.returncode != 0:
            return SnapshotText(path=path, snapshot="HEAD", text=None, category="git-error", detail=content.stderr.decode("utf-8", errors="replace").strip() or "failed to read HEAD blob", mode=mode)
        return self._decode_blob(path, "HEAD", content.stdout, mode=mode)

    def read_index_entry(self, path: str) -> SnapshotText:
        entry = self._run([self.git, "ls-files", "-s", "-z", "--", path])
        if entry.returncode != 0:
            return SnapshotText(path=path, snapshot="index", text=None, category="git-error", detail=entry.stderr.decode("utf-8", errors="replace").strip() or "failed to inspect index entry")
        if not entry.stdout:
            return SnapshotText(path=path, snapshot="index", text=None, category="missing")

        record = entry.stdout.split(b"\x00", 1)[0]
        header, _, _name = record.partition(b"\t")
        mode = header.split(b" ", 1)[0].decode("ascii", errors="replace")
        if mode not in {"100644", "100755"}:
            return SnapshotText(path=path, snapshot="index", text=None, category="unsupported-mode", mode=mode)

        content = self._run([self.git, "show", f":{path}"])
        if content.returncode != 0:
            return SnapshotText(path=path, snapshot="index", text=None, category="git-error", detail=content.stderr.decode("utf-8", errors="replace").strip() or "failed to read index blob", mode=mode)
        return self._decode_blob(path, "index", content.stdout, mode=mode)

    def read_head_text(self, path: str) -> SnapshotText:
        return self.read_head_entry(path)

    def read_index_text(self, path: str) -> SnapshotText:
        return self.read_index_entry(path)

    def staged_text_blobs(self) -> list[StagedTextBlob]:
        result = subprocess.run(
            [self.git, "ls-files", "-s"],
            cwd=self.root,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise GitAdapterError(result.stderr.decode("utf-8", errors="ignore").strip() or "failed to list staged files")

        blobs: list[StagedTextBlob] = []
        for raw_line in result.stdout.splitlines():
            line = raw_line.decode("utf-8", errors="ignore") if isinstance(raw_line, bytes) else raw_line
            metadata, separator, path = line.partition("\t")
            if not separator:
                continue
            parts = metadata.split()
            if len(parts) != 3:
                continue
            mode, _object_id, stage = parts
            if stage != "0" or mode == "120000":
                continue
            snapshot = self.read_index_text(path)
            if snapshot.category != "text" or snapshot.text is None:
                continue
            blobs.append(StagedTextBlob(path=path, text=snapshot.text))
        return blobs

    def added_lines(self) -> list[AddedLine]:
        result = subprocess.run(
            [self.git, "diff", "--cached", "--unified=0", "--no-ext-diff"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitAdapterError(result.stderr.strip() or "failed to diff staged changes")
        return parse_added_lines(result.stdout)


def parse_added_lines(diff_text: str) -> list[AddedLine]:
    added_lines: list[AddedLine] = []
    current_path: str | None = None
    current_line_number: int | None = None
    in_hunk = False

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current_path = None
            current_line_number = None
            in_hunk = False
            continue
        if line.startswith("+++ b/"):
            current_path = line[6:]
            continue
        if line.startswith("@@ "):
            match = _HUNK_PATTERN.match(line)
            if match is None:
                current_line_number = None
                in_hunk = False
                continue
            try:
                current_line_number = int(match.group("new_start"))
            except (TypeError, ValueError):
                current_line_number = None
                in_hunk = False
                continue
            in_hunk = True
            continue
        if not in_hunk or current_path is None or current_line_number is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added_lines.append(AddedLine(path=current_path, line_number=current_line_number, text=line[1:]))
            current_line_number += 1
            continue
        if line.startswith(" "):
            current_line_number += 1
            continue
        if line.startswith("-"):
            continue
        if line.startswith("\\"):
            continue
    return added_lines
