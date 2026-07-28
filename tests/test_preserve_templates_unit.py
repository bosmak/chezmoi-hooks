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


diagnostics = _load_module("hooks.diagnostics", "hooks/diagnostics.py")
fix_plan = _load_module("hooks.fix_plan", "hooks/fix_plan.py")
git_index = _load_module("hooks.git_index", "hooks/git_index.py")
preserve_templates = _load_module("hooks.preserve_templates", "hooks/preserve_templates.py")

Diagnostic = diagnostics.Diagnostic
PlannedEdit = fix_plan.PlannedEdit
GitRepoSnapshot = git_index.GitRepoSnapshot
StagedChange = git_index.StagedChange
TemplateViolation = preserve_templates.TemplateViolation
plan_template_preservation = preserve_templates.plan_template_preservation
find_restorable_template_edits = preserve_templates.find_restorable_template_edits
main = preserve_templates.main


class FakeRenderer:
    def __init__(self, mapping: dict[tuple[str, str], str | None]):
        self.mapping = mapping

    def render_line(self, path: str, line: str) -> str | None:
        return self.mapping.get((path, line))


def test_main_returns_success_when_no_candidate_paths(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(GitRepoSnapshot, "discover", classmethod(lambda cls, start=None: GitRepoSnapshot(root=repo)))
    monkeypatch.setattr(GitRepoSnapshot, "staged_changes", lambda self: [])

    assert main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_plan_reports_manual_review_when_template_line_changes_beyond_exact_render() -> None:
    renderer = FakeRenderer(
        {
            ("dot_config/app.conf", "home = {{ .chezmoi.homeDir }}"): "home = /home/alice",
        }
    )

    plan = plan_template_preservation(
        path="dot_config/app.conf",
        head_text="home = {{ .chezmoi.homeDir }}\n",
        staged_text="home = /srv/alice\n",
        renderer=renderer,
    )

    assert plan.edits == []
    assert plan.violations == [
        TemplateViolation(
            path="dot_config/app.conf",
            line=1,
            category="manual-review",
            message="template change is not an exact single-line rendered substitution",
        )
    ]


def test_plan_restores_only_exact_single_line_rendered_substitution() -> None:
    renderer = FakeRenderer(
        {
            ("dot_config/app.conf", "home = {{ .chezmoi.homeDir }}"): "home = /home/alice",
        }
    )

    plan = plan_template_preservation(
        path="dot_config/app.conf",
        head_text="home = {{ .chezmoi.homeDir }}\n",
        staged_text="home = /home/alice\n",
        renderer=renderer,
    )

    assert plan.violations == []
    assert len(plan.edits) == 1
    edit = plan.edits[0]
    assert isinstance(edit, PlannedEdit)
    assert edit.path == "dot_config/app.conf"
    assert edit.line_number == 1
    assert edit.expected_line == "home = /home/alice"
    assert edit.replacement_line == "home = {{ .chezmoi.homeDir }}"


def test_restorable_line_when_staged_line_equals_rendered_prior_line() -> None:
    renderer = FakeRenderer(
        {
            ("dot_config/app.conf", "home = {{ .chezmoi.homeDir }}"): "home = /home/alice",
        }
    )

    edits = find_restorable_template_edits(
        path="dot_config/app.conf",
        head_text="home = {{ .chezmoi.homeDir }}\n",
        staged_text="home = /home/alice\n",
        renderer=renderer,
    )

    assert len(edits) == 1
    edit = edits[0]
    assert isinstance(edit, PlannedEdit)
    assert edit.path == "dot_config/app.conf"
    assert edit.line_number == 1
    assert edit.expected_line == "home = /home/alice"
    assert edit.replacement_line == "home = {{ .chezmoi.homeDir }}"


def test_plan_reports_manual_review_for_ambiguous_template_loss() -> None:
    renderer = FakeRenderer({})

    deleted = plan_template_preservation(
        path="dot_config/app.conf",
        head_text="home = {{ .chezmoi.homeDir }}\nkeep = true\n",
        staged_text="keep = true\n",
        renderer=renderer,
    )
    assert deleted.edits == []
    assert deleted.violations == [
        TemplateViolation(
            path="dot_config/app.conf",
            line=1,
            category="manual-review",
            message="prior templated line was deleted",
        )
    ]

    many_to_one = plan_template_preservation(
        path="dot_config/app.conf",
        head_text="home = {{ .chezmoi.homeDir }}\nkeep = true\n",
        staged_text="home = /home/alice keep = true\n",
        renderer=renderer,
    )
    assert many_to_one.edits == []
    assert many_to_one.violations == [
        TemplateViolation(
            path="dot_config/app.conf",
            line=1,
            category="manual-review",
            message="template change is structurally ambiguous",
        )
    ]

    render_failed = plan_template_preservation(
        path="dot_config/app.conf",
        head_text="home = {{ .chezmoi.homeDir }}\n",
        staged_text="home = /home/alice\n",
        renderer=renderer,
    )
    assert render_failed.edits == []
    assert render_failed.violations == [
        TemplateViolation(
            path="dot_config/app.conf",
            line=1,
            category="manual-review",
            message="template rendering failed or produced multiple lines",
        )
    ]


def test_main_reports_violations_and_does_not_apply_safe_edits_when_any_ambiguity_exists(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    applied: list[tuple[Path, list[PlannedEdit]]] = []

    monkeypatch.setattr(GitRepoSnapshot, "discover", classmethod(lambda cls, start=None: GitRepoSnapshot(root=repo)))
    monkeypatch.setattr(
        GitRepoSnapshot,
        "staged_changes",
        lambda self: [
            StagedChange(path="safe.conf", status="M"),
            StagedChange(path="unsafe.conf", status="M"),
        ],
    )
    monkeypatch.setattr(
        GitRepoSnapshot,
        "read_head_text",
        lambda self, path: {
            "safe.conf": "home = {{ .chezmoi.homeDir }}\n",
            "unsafe.conf": "home = {{ .chezmoi.homeDir }}\n",
        }.get(path),
    )
    monkeypatch.setattr(
        GitRepoSnapshot,
        "read_index_text",
        lambda self, path: {
            "safe.conf": "home = /home/alice\n",
            "unsafe.conf": "home = /srv/alice\n",
        }.get(path),
    )
    monkeypatch.setattr(
        preserve_templates,
        "ChezmoiRenderer",
        lambda cwd=None: FakeRenderer(
            {
                ("safe.conf", "home = {{ .chezmoi.homeDir }}"): "home = /home/alice",
                ("unsafe.conf", "home = {{ .chezmoi.homeDir }}"): "home = /home/alice",
            }
        ),
    )
    monkeypatch.setattr(
        preserve_templates,
        "apply_worktree_edits",
        lambda root, edits: applied.append((root, list(edits))),
    )

    assert main([]) == 2
    assert applied == []
    assert "manual-review" in capsys.readouterr().err


def test_staged_changes_parses_name_status_with_renames_and_spaces(monkeypatch) -> None:
    class Result:
        returncode = 0
        stdout = b"M\0dot config/app.conf\0A\0new file\0D\0old file\0R100\0old name\0new name\0"
        stderr = b""

    monkeypatch.setattr(git_index.subprocess, "run", lambda *args, **kwargs: Result())

    repo = GitRepoSnapshot(root=Path("/tmp/repo"))
    assert repo.staged_changes() == [
        StagedChange(path="dot config/app.conf", status="M", old_path=None),
        StagedChange(path="new file", status="A", old_path=None),
        StagedChange(path="old file", status="D", old_path=None),
        StagedChange(path="new name", status="R100", old_path="old name"),
    ]


def test_fix_diagnostic_tells_user_to_review_and_restage() -> None:
    diagnostic = Diagnostic(
        hook_id="chezmoi-preserve-templates",
        path="dot_config/app.conf",
        line=1,
        category="fixed",
        message="restored templated source line",
        action="review and restage, then retry",
    )

    assert (
        diagnostic.format()
        == "chezmoi-preserve-templates: dot_config/app.conf:1: fixed: restored templated source line; action: review and restage, then retry"
    )
