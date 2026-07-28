from __future__ import annotations

import importlib.util
import json
import os
import subprocess
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


enforce_portability = _load_module(
    "hooks.enforce_portability", "hooks/enforce_portability.py"
)


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, env=env)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init"], cwd=repo)
    run(["git", "config", "user.name", "Test User"], cwd=repo)
    run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    return repo


def make_fake_chezmoi(
    tmp_path: Path,
    mapping: dict[str, str] | None = None,
    failures: dict[str, str] | None = None,
) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "chezmoi"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        f"mapping = {json.dumps(mapping or {'{{ .machine.token }}': 'sensitive-rendered-token'})}\n"
        f"failures = {json.dumps(failures or {})}\n"
        "template = sys.stdin.read().strip()\n"
        "if template in failures:\n"
        "    sys.stderr.write(failures[template])\n"
        "    sys.exit(1)\n"
        "rendered = mapping.get(template)\n"
        "if rendered is None:\n"
        "    sys.exit(1)\n"
        "sys.stdout.write(rendered)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


def assert_git_state_unchanged(repo: Path, before_diff: str, before_cached_diff: str) -> None:
    assert run(["git", "diff"], cwd=repo).stdout == before_diff
    assert run(["git", "diff", "--cached"], cwd=repo).stdout == before_cached_diff


def test_unchanged_expression_source_flags_changed_file_without_leaking_token(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text("token = {{ .machine.token }}\n", encoding="utf-8")
    run(["git", "add", "dot_config/source.tmpl"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    changed = repo / "dot_config" / "changed.txt"
    changed.write_text("first line\ntoken = sensitive-rendered-token\n", encoding="utf-8")
    run(["git", "add", "dot_config/changed.txt"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main([]) == 1
    captured = capsys.readouterr()
    assert "chezmoi-enforce-portability: dot_config/changed.txt:2: portable-expression-available:" in captured.err
    assert ".machine.token" in captured.err
    assert "sensitive-rendered-token" not in captured.err
    assert captured.out == ""


def test_existing_literal_on_unchanged_line_is_ignored(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text("token = {{ .machine.token }}\n", encoding="utf-8")
    changed = repo / "dot_config" / "changed.txt"
    changed.write_text("token = sensitive-rendered-token\nkeep = true\n", encoding="utf-8")
    run(["git", "add", "dot_config/source.tmpl", "dot_config/changed.txt"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    changed.write_text("token = sensitive-rendered-token\nkeep = false\n", encoding="utf-8")
    run(["git", "add", "dot_config/changed.txt"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_every_line_of_new_staged_text_file_is_checked(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text("token = {{ .machine.token }}\n", encoding="utf-8")
    run(["git", "add", "dot_config/source.tmpl"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    new_file = repo / "dot_config" / "new.txt"
    new_file.write_text("sensitive-rendered-token\nsecond sensitive-rendered-token\n", encoding="utf-8")
    run(["git", "add", "dot_config/new.txt"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main([]) == 1
    captured = capsys.readouterr()
    assert "dot_config/new.txt:1" in captured.err
    assert "dot_config/new.txt:2" in captured.err


def test_deleted_lines_are_ignored(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text("token = {{ .machine.token }}\n", encoding="utf-8")
    changed = repo / "dot_config" / "changed.txt"
    changed.write_text("token = sensitive-rendered-token\nkeep = true\n", encoding="utf-8")
    run(["git", "add", "dot_config/source.tmpl", "dot_config/changed.txt"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    changed.write_text("keep = true\n", encoding="utf-8")
    run(["git", "add", "dot_config/changed.txt"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main([]) == 0
    assert capsys.readouterr().err == ""


def test_duplicate_rendered_values_report_all_candidate_expressions_without_literal(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text(
        "alpha = {{ .a.token }}\n"
        "middle = {{ .m.token }}\n"
        "zeta = {{ .z.token }}\n",
        encoding="utf-8",
    )
    run(["git", "add", "dot_config/source.tmpl"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    changed = repo / "dot_config" / "changed.txt"
    changed.write_text("token = shared-rendered-value\n", encoding="utf-8")
    run(["git", "add", "dot_config/changed.txt"], cwd=repo)

    fake_bin = make_fake_chezmoi(
        tmp_path,
        mapping={
            "{{ .a.token }}": "shared-rendered-value",
            "{{ .m.token }}": "shared-rendered-value",
            "{{ .z.token }}": "shared-rendered-value",
        },
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main([]) == 1
    captured = capsys.readouterr()
    assert captured.err.index(".a.token") < captured.err.index(".m.token") < captured.err.index(".z.token")
    assert "dot_config/source.tmpl:1" in captured.err
    assert "dot_config/source.tmpl:2" in captured.err
    assert "dot_config/source.tmpl:3" in captured.err
    assert "shared-rendered-value" not in captured.err


def test_ignore_expression_accepts_equals_and_separate_forms(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text(
        "first = {{ .machine.token }}\nsecond = {{ .other.token }}\n",
        encoding="utf-8",
    )
    run(["git", "add", "dot_config/source.tmpl"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    changed = repo / "dot_config" / "changed.txt"
    changed.write_text("token = shared-rendered-value\n", encoding="utf-8")
    run(["git", "add", "dot_config/changed.txt"], cwd=repo)

    fake_bin = make_fake_chezmoi(
        tmp_path,
        mapping={
            "{{ .machine.token }}": "shared-rendered-value",
            "{{ .other.token }}": "shared-rendered-value",
        },
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main(["--ignore-expression=.machine.token", "--ignore-expression", ".other.token"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_ineligible_rendered_values_do_not_create_findings(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text(
        "empty = {{ .empty }}\n"
        "spaces = {{ .spaces }}\n"
        "multi = {{ .multi }}\n"
        "bool = {{ .bool }}\n"
        "num = {{ .num }}\n"
        "short = {{ .short }}\n",
        encoding="utf-8",
    )
    run(["git", "add", "dot_config/source.tmpl"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    changed = repo / "dot_config" / "changed.txt"
    changed.write_text("noise\nvalue\n", encoding="utf-8")
    run(["git", "add", "dot_config/changed.txt"], cwd=repo)

    fake_bin = make_fake_chezmoi(
        tmp_path,
        mapping={
            "{{ .empty }}": "",
            "{{ .spaces }}": "   ",
            "{{ .multi }}": "line1\nline2",
            "{{ .bool }}": "FALSE",
            "{{ .num }}": "3.14",
            "{{ .short }}": "a b",
        },
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_missing_chezmoi_fails_closed(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text("token = {{ .machine.token }}\n", encoding="utf-8")
    changed = repo / "dot_config" / "changed.txt"
    changed.write_text("token = sensitive-rendered-token\n", encoding="utf-8")
    run(["git", "add", "dot_config/source.tmpl", "dot_config/changed.txt"], cwd=repo)

    before_diff = run(["git", "diff"], cwd=repo).stdout
    before_cached_diff = run(["git", "diff", "--cached"], cwd=repo).stdout

    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{tmp_path / 'empty-bin'}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main([]) == 2
    captured = capsys.readouterr()
    assert "operational-error" in captured.err
    assert "fix chezmoi/git prerequisites" in captured.err
    assert "sensitive-rendered-token" not in captured.err
    assert_git_state_unchanged(repo, before_diff, before_cached_diff)


def test_render_failure_fails_closed_without_leaking_output(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text("token = {{ .machine.token }}\n", encoding="utf-8")
    changed = repo / "dot_config" / "changed.txt"
    changed.write_text("token = ignored\n", encoding="utf-8")
    run(["git", "add", "dot_config/source.tmpl", "dot_config/changed.txt"], cwd=repo)

    before_diff = run(["git", "diff"], cwd=repo).stdout
    before_cached_diff = run(["git", "diff", "--cached"], cwd=repo).stdout

    fake_bin = make_fake_chezmoi(
        tmp_path,
        failures={"{{ .machine.token }}": "rendered-secret-token-from-stderr"},
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main([]) == 2
    captured = capsys.readouterr()
    assert "operational-error" in captured.err
    assert "retry after fixing rendering/prerequisites" in captured.err
    assert "rendered-secret-token-from-stderr" not in captured.err
    assert_git_state_unchanged(repo, before_diff, before_cached_diff)


def test_invalid_ignore_expression_is_configuration_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text("token = {{ .machine.token }}\n", encoding="utf-8")
    run(["git", "add", "dot_config/source.tmpl"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main(["--ignore-expression=not-dotted"]) == 2
    captured = capsys.readouterr()
    assert "configuration-error" in captured.err
    assert "fix hook arguments and retry" in captured.err
    assert "not-dotted" not in captured.err


def test_binary_symlink_deleted_and_undecodable_entries_are_skipped_safely(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    dot_config = repo / "dot_config"
    dot_config.mkdir(parents=True)
    source = dot_config / "source.tmpl"
    source.write_text("token = {{ .machine.token }}\n", encoding="utf-8")
    deleted = dot_config / "deleted.txt"
    deleted.write_text("gone\n", encoding="utf-8")
    undecodable = dot_config / "undecodable.txt"
    undecodable.write_bytes(b"\xff\xfe\x00")
    symlink_path = dot_config / "link"
    symlink_path.symlink_to("source.tmpl")
    changed = dot_config / "changed.txt"
    changed.write_text("keep\n", encoding="utf-8")
    run(["git", "add", "dot_config/source.tmpl", "dot_config/deleted.txt", "dot_config/undecodable.txt", "dot_config/link", "dot_config/changed.txt"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    deleted.unlink()
    undecodable.write_bytes(b"\xff\xfe\x01")
    symlink_path.unlink()
    symlink_path.symlink_to("changed.txt")
    changed.write_text("keep\ntoken = sensitive-rendered-token\nextra\n", encoding="utf-8")
    run(["git", "add", "-A"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main([]) == 1
    captured = capsys.readouterr()
    assert "dot_config/changed.txt:2" in captured.err
    assert "deleted.txt" not in captured.err
    assert "undecodable.txt" not in captured.err
    assert "dot_config/link" not in captured.err


def test_renamed_file_added_lines_are_checked(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text("token = {{ .machine.token }}\n", encoding="utf-8")
    renamed = repo / "dot_config" / "old.txt"
    renamed.write_text("keep\n", encoding="utf-8")
    run(["git", "add", "dot_config/source.tmpl", "dot_config/old.txt"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    target = repo / "dot_config" / "renamed.txt"
    renamed.rename(target)
    target.write_text("keep\ntoken = sensitive-rendered-token\n", encoding="utf-8")
    run(["git", "add", "-A"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main([]) == 1
    captured = capsys.readouterr()
    assert "dot_config/renamed.txt:2" in captured.err


def test_hook_leaves_index_worktree_and_diff_state_unchanged_after_finding(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    source = repo / "dot_config" / "source.tmpl"
    source.parent.mkdir(parents=True)
    source.write_text("token = {{ .machine.token }}\n", encoding="utf-8")
    run(["git", "add", "dot_config/source.tmpl"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    changed = repo / "dot_config" / "changed.txt"
    changed.write_text("token = sensitive-rendered-token\n", encoding="utf-8")
    run(["git", "add", "dot_config/changed.txt"], cwd=repo)

    before_worktree = changed.read_text(encoding="utf-8")
    before_diff = run(["git", "diff"], cwd=repo).stdout
    before_cached_diff = run(["git", "diff", "--cached"], cwd=repo).stdout

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert enforce_portability.main([]) == 1
    _ = capsys.readouterr()

    assert changed.read_text(encoding="utf-8") == before_worktree
    assert_git_state_unchanged(repo, before_diff, before_cached_diff)
