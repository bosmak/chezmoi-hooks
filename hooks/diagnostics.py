from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Diagnostic:
    hook_id: str
    path: str
    line: int | None
    category: str
    message: str
    action: str

    def format(self) -> str:
        location = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{self.hook_id}: {location}: {self.category}: {self.message}; action: {self.action}"
