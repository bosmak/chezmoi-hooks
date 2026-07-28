from __future__ import annotations

import importlib.util
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


preserve_templates = _load_module("hooks.preserve_templates", "hooks/preserve_templates.py")


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, env=env)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init"], cwd=repo)
    run(["git", "config", "user.name", "Test User"], cwd=repo)
    run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    return repo


def make_fake_chezmoi(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "chezmoi"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "line = sys.stdin.read()\n"
        "mapping = {\n"
        "    'home = {{ .chezmoi.homeDir }}': 'home = /home/alice',\n"
        "    'work = {{ .chezmoi.homeDir }}': 'work = /home/alice',\n"
        "}\n"
        "rendered = mapping.get(line)\n"
        "if rendered is None:\n"
        "    sys.exit(1)\n"
        "sys.stdout.write(rendered + '\\n')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


def test_hook_restores_prior_template_line_in_worktree_and_leaves_index_rendered(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "app.conf"
    path.parent.mkdir(parents=True)
    path.write_text("home = {{ .chezmoi.homeDir }}\nkeep = true\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    path.write_text("home = /home/alice\nkeep = true\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", env["PATH"])

    assert preserve_templates.main([]) == 1
    assert path.read_text(encoding="utf-8") == "home = {{ .chezmoi.homeDir }}\nkeep = true\n"
    assert run(["git", "show", ":dot_config/app.conf"], cwd=repo).stdout == "home = /home/alice\nkeep = true\n"
    assert "home = /home/alice" in run(["git", "diff", "--cached"], cwd=repo).stdout
    captured = capsys.readouterr()
    assert "chezmoi-preserve-templates: dot_config/app.conf:1: fixed:" in captured.err


def test_new_file_is_skipped_by_template_preservation(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "new.conf"
    path.parent.mkdir(parents=True)
    path.write_text("home = /home/alice\n", encoding="utf-8")
    run(["git", "add", "dot_config/new.conf"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert preserve_templates.main([]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""


def test_binary_staged_entry_is_skipped_safely(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "binary.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x00\x01initial\xff")
    run(["git", "add", "dot_config/binary.bin"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    path.write_bytes(b"\x00\x01changed\xff")
    run(["git", "add", "dot_config/binary.bin"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert preserve_templates.main([]) == 0
    assert capsys.readouterr().err == ""


def test_deleted_file_with_prior_template_requires_manual_review(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "app.conf"
    path.parent.mkdir(parents=True)
    path.write_text("home = {{ .chezmoi.homeDir }}\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    path.unlink()
    run(["git", "add", "-u", "dot_config/app.conf"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert preserve_templates.main([]) == 2
    assert not path.exists()
    assert "manual-review" in capsys.readouterr().err
    assert run(["git", "diff", "--cached", "--name-status"], cwd=repo).stdout.strip() == "D\tdot_config/app.conf"


def test_mixed_safe_repair_and_ambiguous_template_loss_writes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    safe_path = repo / "dot_config" / "safe.conf"
    unsafe_path = repo / "dot_config" / "unsafe.conf"
    safe_path.parent.mkdir(parents=True)
    safe_path.write_text("home = {{ .chezmoi.homeDir }}\n", encoding="utf-8")
    unsafe_path.write_text("work = {{ .chezmoi.homeDir }}\n", encoding="utf-8")
    run(["git", "add", "dot_config/safe.conf", "dot_config/unsafe.conf"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    safe_path.write_text("home = /home/alice\n", encoding="utf-8")
    unsafe_path.write_text("work = /srv/alice\n", encoding="utf-8")
    run(["git", "add", "dot_config/safe.conf", "dot_config/unsafe.conf"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert preserve_templates.main([]) == 2
    assert safe_path.read_text(encoding="utf-8") == "home = /home/alice\n"
    assert unsafe_path.read_text(encoding="utf-8") == "work = /srv/alice\n"
    assert run(["git", "show", ":dot_config/safe.conf"], cwd=repo).stdout == "home = /home/alice\n"
    assert "manual-review" in capsys.readouterr().err


def test_unsafe_worktree_divergence_returns_two_without_index_mutation(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "app.conf"
    path.parent.mkdir(parents=True)
    path.write_text("home = {{ .chezmoi.homeDir }}\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    path.write_text("home = /home/alice\n", encoding="utf-8")
    run(["git", "add", "dot_config/app.conf"], cwd=repo)
    path.write_text("home = /tmp/elsewhere\n", encoding="utf-8")

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert preserve_templates.main([]) == 2
    assert path.read_text(encoding="utf-8") == "home = /tmp/elsewhere\n"
    assert run(["git", "show", ":dot_config/app.conf"], cwd=repo).stdout == "home = /home/alice\n"
    assert "unexpected content" in capsys.readouterr().err


def test_deleted_file_without_template_is_skipped(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    path = repo / "dot_config" / "plain.conf"
    path.parent.mkdir(parents=True)
    path.write_text("home = /home/alice\n", encoding="utf-8")
    run(["git", "add", "dot_config/plain.conf"], cwd=repo)
    run(["git", "commit", "-m", "initial"], cwd=repo)

    path.unlink()
    run(["git", "add", "-u", "dot_config/plain.conf"], cwd=repo)

    fake_bin = make_fake_chezmoi(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    assert preserve_templates.main([]) == 0
    assert capsys.readouterr().err == ""


def test_pre_commit_manifest_exposes_repository_wide_hooks() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = (root / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
    assert "id: chezmoi-preserve-templates" in manifest
    assert "entry: chezmoi-preserve-templates" in manifest
    assert "id: chezmoi-enforce-portability" in manifest
    assert "entry: chezmoi-enforce-portability" in manifest
    assert "language: python" in manifest
    assert "pass_filenames: false" in manifest

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'chezmoi-preserve-templates = "hooks.preserve_templates:main"' in pyproject
    assert 'chezmoi-enforce-portability = "hooks.enforce_portability:main"' in pyproject
