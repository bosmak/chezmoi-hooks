# chezmoi-hooks

Pre-commit hooks for keeping a chezmoi source repo portable and safe to stage.

## Hooks

| Hook | What it does |
| --- | --- |
| `chezmoi-preserve-templates` | Detects simple template lines that were accidentally rendered to local values. Restores the template line in the working tree and asks you to review/restage. Ambiguous cases fail for manual review. |
| `chezmoi-enforce-portability` | Checks newly added staged lines for local values that should use existing dotted chezmoi template expressions. Existing unchanged literals are ignored. |
| `chezmoi-preserve-lines` | Restores selected full lines from `HEAD` while keeping unrelated staged edits. Useful for local state fields you never want to commit. |

## Requirements

- Python 3.9+
- Git
- `chezmoi`

## Setup

Works with both [prek](https://github.com/j178/prek) and [pre-commit](https://pre-commit.com/):

```yaml
repos:
  - repo: https://github.com/bosmak/chezmoi-hooks
    rev: v0.1.0  # pin a tag or commit
    hooks:
      - id: chezmoi-preserve-templates
      - id: chezmoi-enforce-portability
        args:
          - --ignore-expression=.colorSchemeName
          - --ignore-expression=.keyboardLayout
      - id: chezmoi-preserve-lines
        args:
          - '--preserve=dot_config/Code/User/settings.json:^\s*"window.zoomLevel":'
          - '--preserve=dot_config/app/state.json:^\s*"lastOpened":'
```

## Configuration

There is no project config file. Use your hook runner's normal controls:

- hook `args`
- `exclude` patterns
- `SKIP=<hook-id>` or your runner's equivalent

Supported args:

- `chezmoi-enforce-portability`: repeat `--ignore-expression=.name`
- `chezmoi-preserve-lines`: repeat `--preserve=path:regex`

All hooks run repository-wide with `pass_filenames: false` and operate on the staged snapshot.

## Secret scanning

These hooks are not secret scanners. If you want secret scanning, add a separate pinned hook such as Betterleaks:

```yaml
repos:
  - repo: https://github.com/bosmak/chezmoi-hooks
    rev: v0.1.0
    hooks:
      - id: chezmoi-preserve-templates
  - repo: https://github.com/betterleaks/betterleaks
    rev: vX.Y.Z
    hooks:
      - id: betterleaks
```
