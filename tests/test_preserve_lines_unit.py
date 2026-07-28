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
preserve_lines = _load_module("hooks.preserve_lines", "hooks/preserve_lines.py")

Diagnostic = diagnostics.Diagnostic
PlannedEdit = fix_plan.PlannedEdit
UnsafeEditError = fix_plan.UnsafeEditError
apply_worktree_edits = fix_plan.apply_worktree_edits
GitRepoSnapshot = git_index.GitRepoSnapshot
SnapshotText = git_index.SnapshotText
PreserveLineError = preserve_lines.PreserveLineError
main = preserve_lines.main
parse_preserve_selector = preserve_lines.parse_preserve_selector
plan_preserved_line_edits = preserve_lines.plan_preserved_line_edits


def _ok(path: str, snapshot: str, text: str) -> SnapshotText:
    return SnapshotText(path=path, snapshot=snapshot, text=text, category="text")


def test_parse_repeatable_preserve_selector_by_first_colon() -> None:
    selector = parse_preserve_selector("dot_config/app.conf:^key: value$")

    assert selector.path == "dot_config/app.conf"
    assert selector.pattern.pattern == "^key: value$"

    parsed = [
        parse_preserve_selector(value)
        for value in ["dot_config/app.conf:^volatile =", "dot_env:^FOO:BAR="]
    ]
    assert [item.path for item in parsed] == ["dot_config/app.conf", "dot_env"]
    assert [item.pattern.pattern for item in parsed] == ["^volatile =", "^FOO:BAR="]


def test_parse_rejects_absolute_selector_path() -> None:
    try:
        parse_preserve_selector("/etc/passwd:^root")
    except PreserveLineError as exc:
        assert "unsafe repository path" in str(exc)
    else:
        raise AssertionError("expected unsafe selector path failure")


def test_parse_rejects_traversal_selector_path() -> None:
    try:
        parse_preserve_selector("../secret:^token")
    except PreserveLineError as exc:
        assert "unsafe repository path" in str(exc)
    else:
        raise AssertionError("expected traversal selector path failure")


def test_parse_rejects_normalized_away_selector_path() -> None:
    for value in ["a/./b:^x", "a//b:^x", "folder/:^x"]:
        try:
            parse_preserve_selector(value)
        except PreserveLineError as exc:
            assert "unsafe repository path" in str(exc)
        else:
            raise AssertionError(f"expected normalized path failure for {value}")


def test_parse_reports_invalid_regex_without_losing_first_colon_path_split() -> None:
    try:
        parse_preserve_selector("dot_env:^(FOO:BAR")
    except PreserveLineError as exc:
        assert "invalid selector 'dot_env:^(FOO:BAR'" in str(exc)
    else:
        raise AssertionError("expected invalid regex failure")


def test_unique_selector_match_plans_full_head_line_restore() -> None:
    selector = parse_preserve_selector("dot_config/app.conf:^volatile =")

    edits = plan_preserved_line_edits(
        [selector],
        read_head=lambda path: "token = stable\nvolatile = OLD\n",
        read_index=lambda path: "token = stable\nvolatile = NEW\n",
    )

    assert edits == [
        PlannedEdit(
            path="dot_config/app.conf",
            line_number=2,
            expected_line="volatile = NEW",
            replacement_line="volatile = OLD",
        )
    ]


def test_equal_matched_full_line_is_noop_even_when_line_moved(tmp_path: Path, monkeypatch, capsys) -> None:
    selector = parse_preserve_selector("dot_config/app.conf:^volatile =")
    edits = plan_preserved_line_edits(
        [selector],
        read_head=lambda path: "volatile = SAME\nkeep = true\n",
        read_index=lambda path: "keep = true\nother = changed\nvolatile = SAME\n",
    )

    assert edits == []

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(GitRepoSnapshot, "discover", classmethod(lambda cls, start=None: GitRepoSnapshot(root=repo)))
    monkeypatch.setattr(GitRepoSnapshot, "read_head_entry", lambda self, path: _ok(path, "HEAD", "volatile = SAME\nkeep = true\n"))
    monkeypatch.setattr(
        GitRepoSnapshot,
        "read_index_entry",
        lambda self, path: _ok(path, "index", "keep = true\nother = changed\nvolatile = SAME\n"),
    )

    assert main(["--preserve=dot_config/app.conf:^volatile ="]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_multiple_independent_selectors_apply_in_one_run() -> None:
    selectors = [
        parse_preserve_selector("dot_config/app.conf:^volatile ="),
        parse_preserve_selector("dot_config/app.conf:^other ="),
    ]

    edits = plan_preserved_line_edits(
        selectors,
        read_head=lambda path: "volatile = OLD\nother = KEEP\nplain = same\n",
        read_index=lambda path: "volatile = NEW\nother = CHANGED\nplain = same\n",
    )

    assert edits == [
        PlannedEdit(
            path="dot_config/app.conf",
            line_number=1,
            expected_line="volatile = NEW",
            replacement_line="volatile = OLD",
        ),
        PlannedEdit(
            path="dot_config/app.conf",
            line_number=2,
            expected_line="other = CHANGED",
            replacement_line="other = KEEP",
        ),
    ]


def test_plan_reports_zero_head_matches_with_count_and_selector() -> None:
    selector = parse_preserve_selector("dot_config/app.conf:^volatile =")

    try:
        plan_preserved_line_edits(
            [selector],
            read_head=lambda path: _ok(path, "HEAD", "keep = true\n"),
            read_index=lambda path: _ok(path, "index", "volatile = NEW\n"),
        )
    except PreserveLineError as exc:
        assert "matched 0 lines in HEAD" in str(exc)
        assert selector.source in str(exc)
    else:
        raise AssertionError("expected zero-match failure")


def test_plan_reports_multiple_index_matches_with_count_and_selector() -> None:
    selector = parse_preserve_selector("dot_config/app.conf:^volatile =")

    try:
        plan_preserved_line_edits(
            [selector],
            read_head=lambda path: _ok(path, "HEAD", "volatile = OLD\n"),
            read_index=lambda path: _ok(path, "index", "volatile = ONE\nvolatile = TWO\n"),
        )
    except PreserveLineError as exc:
        assert "matched 2 lines in index" in str(exc)
        assert selector.source in str(exc)
    else:
        raise AssertionError("expected multiple-match failure")


def test_plan_reports_missing_head_path() -> None:
    selector = parse_preserve_selector("dot_config/app.conf:^volatile =")

    try:
        plan_preserved_line_edits(
            [selector],
            read_head=lambda path: SnapshotText(path=path, snapshot="HEAD", text=None, category="missing"),
            read_index=lambda path: _ok(path, "index", "volatile = NEW\n"),
        )
    except PreserveLineError as exc:
        assert "missing tracked file" in str(exc)
        assert "HEAD path dot_config/app.conf" in str(exc)
    else:
        raise AssertionError("expected missing HEAD path failure")


def test_plan_reports_missing_index_path() -> None:
    selector = parse_preserve_selector("dot_config/app.conf:^volatile =")

    try:
        plan_preserved_line_edits(
            [selector],
            read_head=lambda path: _ok(path, "HEAD", "volatile = OLD\n"),
            read_index=lambda path: SnapshotText(path=path, snapshot="index", text=None, category="missing"),
        )
    except PreserveLineError as exc:
        assert "missing tracked file" in str(exc)
        assert "index path dot_config/app.conf" in str(exc)
    else:
        raise AssertionError("expected missing index path failure")


def test_plan_reports_binary_or_undecodable_protected_blob() -> None:
    selector = parse_preserve_selector("dot_config/app.conf:^volatile =")

    for category in ["binary", "undecodable"]:
        try:
            plan_preserved_line_edits(
                [selector],
                read_head=lambda path, category=category: SnapshotText(path=path, snapshot="HEAD", text=None, category=category),
                read_index=lambda path: _ok(path, "index", "volatile = NEW\n"),
            )
        except PreserveLineError as exc:
            assert "unsuitable protected content" in str(exc)
        else:
            raise AssertionError(f"expected {category} failure")


def test_plan_rejects_duplicate_selectors_targeting_same_destination_line_even_when_same_replacement() -> None:
    selectors = [
        parse_preserve_selector("dot_config/app.conf:^volatile ="),
        parse_preserve_selector("dot_config/app.conf:volatile ="),
    ]

    try:
        plan_preserved_line_edits(
            selectors,
            read_head=lambda path: _ok(path, "HEAD", "volatile = OLD\n"),
            read_index=lambda path: _ok(path, "index", "volatile = NEW\n"),
        )
    except PreserveLineError as exc:
        assert "both target dot_config/app.conf:1" in str(exc)
    else:
        raise AssertionError("expected duplicate selector failure")


def test_plan_rejects_overlapping_selectors_targeting_same_destination_line_with_different_replacements() -> None:
    selectors = [
        parse_preserve_selector("dot_config/app.conf:^volatile ="),
        parse_preserve_selector("dot_config/app.conf:volatile ="),
    ]

    try:
        plan_preserved_line_edits(
            selectors,
            read_head=lambda path: _ok(path, "HEAD", "volatile = OLD\n"),
            read_index=lambda path: _ok(path, "index", "volatile = NEW\n"),
        )
    except PreserveLineError as exc:
        assert "both target dot_config/app.conf:1" in str(exc)
    else:
        raise AssertionError("expected overlapping selector failure")


def test_main_returns_two_for_invalid_selector(capsys) -> None:
    assert main(["--preserve=missing-colon"]) == 2
    captured = capsys.readouterr()
    assert "chezmoi-preserve-lines: invalid selector 'missing-colon'" in captured.err


def test_main_failure_diagnostic_redacts_line_contents(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(GitRepoSnapshot, "discover", classmethod(lambda cls, start=None: GitRepoSnapshot(root=repo)))
    monkeypatch.setattr(
        GitRepoSnapshot,
        "read_head_entry",
        lambda self, path: _ok(path, "HEAD", "keep = true\n"),
    )
    monkeypatch.setattr(
        GitRepoSnapshot,
        "read_index_entry",
        lambda self, path: _ok(path, "index", "SECRET_TOKEN_VALUE\n"),
    )

    assert main(["--preserve=dot_config/app.conf:^volatile ="]) == 2
    captured = capsys.readouterr()
    assert "matched 0 lines in HEAD" in captured.err
    assert "SECRET_TOKEN_VALUE" not in captured.err
    assert "keep = true" not in captured.err


def test_apply_worktree_edits_rejects_all_files_before_writing_when_one_file_mismatches(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    one = root / "one.txt"
    two = root / "two.txt"
    one.write_text("volatile = NEW\nkeep = true\n", encoding="utf-8")
    two.write_text("volatile = CHANGED\nkeep = true\n", encoding="utf-8")

    edits = [
        PlannedEdit(path="one.txt", line_number=1, expected_line="volatile = NEW", replacement_line="volatile = OLD"),
        PlannedEdit(path="two.txt", line_number=1, expected_line="volatile = NEW", replacement_line="volatile = OLD"),
    ]

    try:
        apply_worktree_edits(root, edits)
    except UnsafeEditError as exc:
        assert "unexpected content at two.txt:1" in str(exc)
    else:
        raise AssertionError("expected mismatch failure")

    assert one.read_text(encoding="utf-8") == "volatile = NEW\nkeep = true\n"
    assert two.read_text(encoding="utf-8") == "volatile = CHANGED\nkeep = true\n"


def test_apply_worktree_edits_rejects_unsafe_destination_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("volatile = NEW\n", encoding="utf-8")

    try:
        apply_worktree_edits(
            root,
            [PlannedEdit(path="../outside.txt", line_number=1, expected_line="volatile = NEW", replacement_line="volatile = OLD")],
        )
    except UnsafeEditError as exc:
        assert "unsafe path ../outside.txt" in str(exc)
    else:
        raise AssertionError("expected unsafe path failure")


def test_fix_diagnostic_tells_user_to_review_and_restage() -> None:
    diagnostic = Diagnostic(
        hook_id="chezmoi-preserve-lines",
        path="dot_config/app.conf",
        line=2,
        category="fixed",
        message="restored selected line from HEAD",
        action="review and restage, then retry",
    )

    assert (
        diagnostic.format()
        == "chezmoi-preserve-lines: dot_config/app.conf:2: fixed: restored selected line from HEAD; action: review and restage, then retry"
    )
