# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import uuid

from super_planner.core.exceptions import WorkflowBusinessInterruption
from super_planner.schemas.project import PlanningProject
from super_planner.schemas.workflow import ConfirmationQuestion, ConfirmationRequest
from super_planner.services.research_providers import ResearchRouter
from super_planner.services.research_providers.base import ProgressCallback


def run_research(project: PlanningProject, progress: ProgressCallback | None = None) -> None:
    result = ResearchRouter().run(project, progress=progress)
    project.research_result = result

    workflow_control = result.raw_payload.get("workflow_control") or {}
    if workflow_control.get("requires_user_confirmation"):
        result.status = "waiting_confirmation"
        result.provider_status = "waiting_confirmation"
        result.summary = "市场研究检测到关键词或类目歧义，等待用户确认后继续。"
        raise WorkflowBusinessInterruption(
            ConfirmationRequest(
                id=uuid.uuid4().hex,
                step_id="research",
                title="市场研究需要确认类目",
                reason=str(workflow_control.get("reason") or "关键词或类目存在歧义，需要用户确认后继续。"),
                questions=[
                    ConfirmationQuestion(
                        id="research_category_confirmation",
                        prompt="请确认本次 Amazon US 市场研究应优先使用的类目或竞品方向。",
                        background=json.dumps(
                            workflow_control.get("category_options") or [],
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                ],
            )
        )
