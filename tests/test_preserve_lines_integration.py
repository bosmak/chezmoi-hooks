from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path


def _load_module(name: str, relative_path: str):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(name, root / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preserve_lines = _load_module("hooks.preserve_lines", "hooks/preserve_lines.py")


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def run_bytes(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init"], cwd=repo)
    run(["git", "config", "user.name", "Test User"], cwd=repo)
    run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    return repo


def test_hook_restores_selected_head_line_in_worktree_and_preserves_other_edits(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "app.conf"
    path.parent.mkdir(parents=True)
    path.write_text("volatile = OLD\nkeep = true\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    path.write_text("volatile = NEW\nkeep = changed\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)

    monkeypatch.chdir(repo)

    assert preserve_lines.main(["--preserve=dot_config/app.conf:^volatile ="]) == 1
    assert path.read_text(encoding="utf-8") == "volatile = OLD\nkeep = changed\n"
    assert run(["git", "show", ":dot_config/app.conf"], cwd=repo).stdout == "volatile = NEW\nkeep = changed\n"
    assert "volatile = NEW" in run(["git", "diff", "--cached"], cwd=repo).stdout
    captured = capsys.readouterr()
    assert "chezmoi-preserve-lines: dot_config/app.conf:1: fixed:" in captured.err
    assert "review and restage, then retry" in captured.err


def test_unborn_head_fails_without_writes(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "app.conf"
    path.parent.mkdir(parents=True)
    path.write_text("volatile = NEW\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)

    monkeypatch.chdir(repo)

    assert preserve_lines.main(["--preserve=dot_config/app.conf:^volatile ="]) == 2
    assert path.read_text(encoding="utf-8") == "volatile = NEW\n"
    captured = capsys.readouterr()
    assert "unborn HEAD" in captured.err


def test_missing_or_deleted_path_fails_without_writes(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "app.conf"
    path.parent.mkdir(parents=True)
    path.write_text("volatile = OLD\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    path.unlink()
    run(["git", "rm", "dot_config/app.conf"], cwd=repo)
    monkeypatch.chdir(repo)

    assert preserve_lines.main(["--preserve=dot_config/app.conf:^volatile ="]) == 2
    captured = capsys.readouterr()
    assert "missing tracked file" in captured.err
    assert run(["git", "diff", "--cached", "--name-status"], cwd=repo).stdout.startswith("D")


def test_binary_or_undecodable_protected_entry_fails_redacted(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "app.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"volatile = OLD\x00\n")
    run(["git", "add", "dot_config/app.bin"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    path.write_text("volatile = NEW\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.bin"], cwd=repo)

    monkeypatch.chdir(repo)

    assert preserve_lines.main(["--preserve=dot_config/app.bin:^volatile ="]) == 2
    captured = capsys.readouterr()
    assert "unsuitable protected content" in captured.err
    assert "volatile = OLD" not in captured.err


@unittest.skipIf(not hasattr(os, "symlink"), "symlinks unavailable")
def test_symlink_entry_fails_as_unsupported_mode(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    target = repo / "target.txt"
    target.write_text("volatile = OLD\n", encoding="utf-8")
    link = repo / "dot_link"
    os.symlink("target.txt", link)
    run(["git", "add", "target.txt", "dot_link"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    monkeypatch.chdir(repo)

    assert preserve_lines.main(["--preserve=dot_link:^volatile ="]) == 2
    captured = capsys.readouterr()
    assert "unsupported mode" in captured.err


def test_broad_selector_reports_match_count_before_writes(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "app.conf"
    path.parent.mkdir(parents=True)
    path.write_text("volatile = OLD\nvolatile = AGAIN\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    path.write_text("volatile = NEW\nkeep = true\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)
    monkeypatch.chdir(repo)

    before = path.read_text(encoding="utf-8")
    assert preserve_lines.main(["--preserve=dot_config/app.conf:^volatile ="]) == 2
    assert path.read_text(encoding="utf-8") == before
    captured = capsys.readouterr()
    assert "matched 2 lines in HEAD" in captured.err


def test_divergent_worktree_content_fails_without_mutating_file(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "app.conf"
    path.parent.mkdir(parents=True)
    path.write_text("volatile = OLD\nkeep = true\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    path.write_text("volatile = NEW\nkeep = true\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)
    path.write_text("volatile = DRIFTED\nkeep = true\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    assert preserve_lines.main(["--preserve=dot_config/app.conf:^volatile ="]) == 2
    assert path.read_text(encoding="utf-8") == "volatile = DRIFTED\nkeep = true\n"
    captured = capsys.readouterr()
    assert "unexpected content at dot_config/app.conf:1" in captured.err


def test_cross_file_atomicity_leaves_all_files_unchanged_on_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    one = repo / "one.conf"
    two = repo / "two.conf"
    one.write_text("volatile = OLD\nkeep = true\n", encoding="utf-8")
    two.write_text("volatile = OLD\nkeep = true\n", encoding="utf-8")
    run(["git", "add", "one.conf", "two.conf"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    one.write_text("volatile = NEW\nkeep = changed\n", encoding="utf-8")
    two.write_text("volatile = NEW\nkeep = changed\n", encoding="utf-8")
    run(["git", "add", "one.conf", "two.conf"], cwd=repo)
    two.write_text("volatile = DRIFTED\nkeep = changed\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    one_before = one.read_text(encoding="utf-8")
    two_before = two.read_text(encoding="utf-8")
    index_one_before = run(["git", "show", ":one.conf"], cwd=repo).stdout
    index_two_before = run(["git", "show", ":two.conf"], cwd=repo).stdout

    assert (
        preserve_lines.main([
            "--preserve=one.conf:^volatile =",
            "--preserve=two.conf:^volatile =",
        ])
        == 2
    )
    assert one.read_text(encoding="utf-8") == one_before
    assert two.read_text(encoding="utf-8") == two_before
    assert run(["git", "show", ":one.conf"], cwd=repo).stdout == index_one_before
    assert run(["git", "show", ":two.conf"], cwd=repo).stdout == index_two_before
    captured = capsys.readouterr()
    assert "unexpected content at two.conf:1" in captured.err


def test_crlf_and_no_final_newline_success_preserve_style(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "app.conf"
    path.write_bytes(b"volatile = OLD\r\nkeep = true")
    run(["git", "add", "app.conf"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    path.write_bytes(b"volatile = NEW\r\nkeep = changed")
    run(["git", "add", "app.conf"], cwd=repo)
    monkeypatch.chdir(repo)

    assert preserve_lines.main(["--preserve=app.conf:^volatile ="]) == 1
    assert path.read_bytes() == b"volatile = OLD\r\nkeep = changed"
    captured = capsys.readouterr()
    assert "app.conf:1: fixed" in captured.err


def test_pre_commit_manifest_exposes_repository_wide_preserve_lines_hook() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = (root / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
    assert "id: chezmoi-preserve-lines" in manifest
    assert "entry: chezmoi-preserve-lines" in manifest
    assert "language: python" in manifest
    assert "pass_filenames: false" in manifest

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'chezmoi-preserve-lines = "hooks.preserve_lines:main"' in pyproject
