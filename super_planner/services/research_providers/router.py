# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from typing import Any

from super_planner.core.exceptions import WorkflowSystemError
from super_planner.schemas.project import PlanningProject, ResearchResult
from super_planner.services.research_providers.base import ProgressCallback, ProviderHealth
from super_planner.services.research_providers.openai_provider import OpenAIResearchProvider
from super_planner.services.research_providers.sorftime_provider import SorftimeResearchProvider


class ResearchRouter:
    def __init__(self) -> None:
        self.mode = os.getenv("SUPER_PLANNER_RESEARCH_PROVIDER", "openai").strip().lower() or "openai"
        self.openai_provider = OpenAIResearchProvider()
        self.sorftime_provider = SorftimeResearchProvider()

    def run(self, project: PlanningProject, progress: ProgressCallback | None = None) -> ResearchResult:
        provider, health_map = self._select_provider()
        result = provider.run(project, progress=progress)
        result.raw_payload.setdefault("research_router", {})
        result.raw_payload["research_router"] = {
            "mode": self.mode,
            "selected_provider": provider.name,
            "provider_health": health_map,
        }
        if result.data_quality is None:
            result.data_quality = {}
        result.data_quality["router_mode"] = self.mode
        result.data_quality["selected_provider"] = provider.name
        return result

    def health(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "providers": {
                "openai": self.openai_provider.health_check().to_dict(),
                "sorftime": self.sorftime_provider.health_check().to_dict(),
            },
        }

    def _select_provider(self):
        openai_health = self.openai_provider.health_check()
        if self.mode in {"auto", "sorftime"}:
            sorftime_health = self.sorftime_provider.health_check()
        else:
            sorftime_health = ProviderHealth(
                name="sorftime",
                configured=False,
                available=False,
                reason="Sorftime health check skipped because router mode is openai.",
            )
        health_map = {
            "openai": openai_health.to_dict(),
            "sorftime": sorftime_health.to_dict(),
        }

        if self.mode == "openai":
            if not openai_health.available:
                raise self._provider_error(openai_health)
            return self.openai_provider, health_map

        if self.mode == "sorftime":
            if not sorftime_health.available:
                raise self._provider_error(sorftime_health)
            return self.sorftime_provider, health_map

        if self.mode == "auto":
            if sorftime_health.configured and sorftime_health.available:
                return self.sorftime_provider, health_map
            if openai_health.available:
                return self.openai_provider, health_map
            raise self._provider_error(openai_health)

        raise WorkflowSystemError(
            "Invalid SUPER_PLANNER_RESEARCH_PROVIDER. Use openai, auto, or sorftime. Please contact IT."
        )

    @staticmethod
    def _provider_error(health: ProviderHealth) -> WorkflowSystemError:
        return WorkflowSystemError(f"Research provider '{health.name}' is unavailable: {health.reason}. Please contact IT.")
