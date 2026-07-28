from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ENTRY_POINTS = {
    "chezmoi-preserve-templates": "hooks.preserve_templates:main",
    "chezmoi-preserve-lines": "hooks.preserve_lines:main",
    "chezmoi-enforce-portability": "hooks.enforce_portability:main",
}


def _venv_paths(venv: Path) -> tuple[Path, Path]:
    unix_python = venv / "bin" / "python"
    if unix_python.exists():
        return unix_python, unix_python.parent
    windows_python = venv / "Scripts" / "python.exe"
    return windows_python, windows_python.parent


def _run(cmd: list[str | Path], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(part) for part in cmd], cwd=cwd, check=check, capture_output=True, text=True)


def _manifest_blocks() -> list[str]:
    manifest = (PROJECT_ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
    return [f"- id:{block}" for block in re.split(r"(?m)^- id:", manifest) if block.strip()]


def _value_from_block(block: str, key: str) -> str:
    match = re.search(rf"(?m)^\s*(?:-\s*)?{re.escape(key)}:\s*(.+?)\s*$", block)
    assert match is not None, f"missing {key} in manifest block:\n{block}"
    return match.group(1).strip()


def test_distribution_install_exposes_expected_console_scripts_and_entry_points(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    _run([sys.executable, "-m", "venv", venv], cwd=PROJECT_ROOT)
    venv_python, venv_bin = _venv_paths(venv)

    _run([venv_python, "-m", "pip", "install", "."], cwd=PROJECT_ROOT)

    for script in EXPECTED_ENTRY_POINTS:
        script_path = venv_bin / script
        assert script_path.exists()
        assert script_path.is_file()

    metadata_script = (
        "import importlib.metadata as md\n"
        "dist = md.distribution('chezmoi-hooks')\n"
        "for entry_point in sorted(dist.entry_points, key=lambda ep: ep.name):\n"
        "    if entry_point.group == 'console_scripts' and entry_point.name.startswith('chezmoi-'):\n"
        "        print(f'{entry_point.name}={entry_point.value}')\n"
    )
    metadata_result = _run([venv_python, "-c", metadata_script], cwd=PROJECT_ROOT)
    installed_entry_points = dict(
        line.split("=", 1) for line in metadata_result.stdout.splitlines() if line.strip()
    )
    assert installed_entry_points == EXPECTED_ENTRY_POINTS

    preserve_lines_help = _run([venv_bin / "chezmoi-preserve-lines", "--help"], cwd=PROJECT_ROOT)
    assert "--preserve" in preserve_lines_help.stdout
    assert "chezmoi-preserve-lines" in preserve_lines_help.stdout

    portability_help = _run([venv_bin / "chezmoi-enforce-portability", "--help"], cwd=PROJECT_ROOT)
    assert "--ignore-expression" in portability_help.stdout
    assert "chezmoi-enforce-portability" in portability_help.stdout

    preserve_templates_run = _run(
        [venv_bin / "chezmoi-preserve-templates"],
        cwd=tmp_path,
        check=False,
    )
    assert preserve_templates_run.returncode == 2
    assert "chezmoi-preserve-templates:" in preserve_templates_run.stderr
    assert any(
        fragment in preserve_templates_run.stderr
        for fragment in ("git repository", "not a git repository", "rev-parse")
    )


def test_pre_commit_manifest_matches_distribution_entry_points() -> None:
    blocks_by_id = {_value_from_block(block, "id"): block for block in _manifest_blocks()}
    assert sorted(blocks_by_id) == sorted(EXPECTED_ENTRY_POINTS)

    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for hook_id, target in EXPECTED_ENTRY_POINTS.items():
        block = blocks_by_id[hook_id]
        assert _value_from_block(block, "entry") == hook_id
        assert _value_from_block(block, "language") == "python"
        assert _value_from_block(block, "pass_filenames") == "false"
        assert _value_from_block(block, "description")
        assert f'{hook_id} = "{target}"' in pyproject


def test_readme_documents_consumer_configuration_and_outcomes() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    for expected_text in [
        "chezmoi-preserve-templates",
        "chezmoi-preserve-lines",
        "chezmoi-enforce-portability",
        "prek",
        "pre-commit",
        "--ignore-expression",
        "--preserve",
        "review and restage",
        "pass_filenames: false",
        "Betterleaks",
        "Python 3.9+",
        "Git",
        "chezmoi",
    ]:
        assert expected_text in readme
