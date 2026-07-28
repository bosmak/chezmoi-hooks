from __future__ import annotations

import subprocess
from pathlib import Path


class ChezmoiRenderError(RuntimeError):
    pass


class ChezmoiRenderer:
    def __init__(self, executable: str = "chezmoi", cwd: Path | None = None) -> None:
        self.executable = executable
        self.cwd = cwd

    def _normalize_output(self, output: str) -> str | None:
        if output.endswith("\r\n"):
            output = output[:-2]
        elif output.endswith("\n") or output.endswith("\r"):
            output = output[:-1]
        if "\n" in output or "\r" in output:
            return None
        return output

    def render_line(self, path: str, line: str) -> str | None:
        _ = path
        result = subprocess.run(
            [self.executable, "execute-template"],
            cwd=self.cwd,
            input=line,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return self._normalize_output(result.stdout)

    def render_expression(self, path: str, expression: str) -> str | None:
        result = subprocess.run(
            [self.executable, "execute-template"],
            cwd=self.cwd,
            input="{{ " + expression + " }}",
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ChezmoiRenderError(
                f"failed to render direct expression for portability check at {path}"
            )
        return self._normalize_output(result.stdout)
