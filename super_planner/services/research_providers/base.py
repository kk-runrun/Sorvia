# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from super_planner.schemas.project import PlanningProject, ResearchResult


ProgressCallback = Callable[[str], None]


@dataclass
class ProviderHealth:
    name: str
    configured: bool
    available: bool
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "available": self.available,
            "reason": self.reason,
            "metadata": self.metadata,
        }


class ResearchProvider(Protocol):
    name: str

    def health_check(self) -> ProviderHealth:
        ...

    def run(self, project: PlanningProject, progress: ProgressCallback | None = None) -> ResearchResult:
        ...


def emit_progress(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)
