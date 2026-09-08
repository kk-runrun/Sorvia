# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from typing import Callable, Dict, Optional

from super_planner.core.exceptions import WorkflowBusinessInterruption, WorkflowSystemError
from super_planner.schemas.project import PlanningProject
from super_planner.schemas.workflow import StepStatus, WorkflowStatus, utc_now
from super_planner.services.content import run_content
from super_planner.services.export import run_export
from super_planner.services.product_context import parse_product_context
from super_planner.services.qa import run_qa
from super_planner.services.research import run_research
from super_planner.services.strategy import run_strategy
from super_planner.storage.repository import FileProjectRepository


StepRunner = Callable[[PlanningProject], None]


class WorkflowEngine:
    def __init__(self, repository: FileProjectRepository):
        self.repository = repository
        self._running: set[str] = set()

    async def run(self, project_id: str) -> None:
        if project_id in self._running:
            return
        self._running.add(project_id)
        try:
            await self._run_unlocked(project_id)
        finally:
            self._running.discard(project_id)

    async def _run_unlocked(self, project_id: str) -> None:
        while True:
            project = self.repository.get(project_id)
            if project.workflow_state.status in {WorkflowStatus.PAUSED, WorkflowStatus.COMPLETED, WorkflowStatus.FAILED}:
                return

            step = self._next_step(project)
            if step is None:
                project.workflow_state.status = WorkflowStatus.COMPLETED
                project.workflow_state.current_step_id = "export"
                self.repository.save(project)
                return

            step.status = StepStatus.RUNNING
            step.started_at = utc_now()
            step.error_message = ""
            step.error_type = ""
            step.summary = {
                "qa": "QA\u8d28\u68c0\u6267\u884c\u4e2d",
                "export": "\u7b56\u5212\u7a3f\u751f\u6210\u6267\u884c\u4e2d",
                "research": "市场研究执行中",
                "strategy": "策略策划执行中",
                "content": "文案生成执行中",
            }.get(step.id, "")
            project.workflow_state.status = WorkflowStatus.RUNNING
            project.workflow_state.current_step_id = step.id
            self.repository.save(project)

            await asyncio.sleep(0.9)

            project = self.repository.get(project_id)
            step = self._step_by_id(project, step.id)
            try:
                self._run_step(project, step.id, progress=self._progress_callback(project_id, step.id))
            except WorkflowBusinessInterruption as exc:
                step.status = StepStatus.WAITING_CONFIRMATION
                step.summary = "等待用户补充业务判断后继续。"
                project.workflow_state.status = WorkflowStatus.PAUSED
                project.workflow_state.confirmation_request = exc.confirmation_request
                self.repository.save(project)
                return
            except WorkflowSystemError as exc:
                step.status = StepStatus.FAILED
                step.error_type = "system"
                step.error_message = str(exc)
                project.workflow_state.status = WorkflowStatus.FAILED
                self.repository.save(project)
                return
            except Exception as exc:  # noqa: BLE001
                step.status = StepStatus.FAILED
                step.error_type = "system"
                step.error_message = f"未预期系统问题：{exc}"
                project.workflow_state.status = WorkflowStatus.FAILED
                self.repository.save(project)
                return

            step.status = StepStatus.COMPLETED
            step.completed_at = utc_now()
            step.summary = self._step_summary(project, step.id)
            self.repository.save(project)
            await asyncio.sleep(0.25)

    def resume_with_confirmation(self, project_id: str, answers: Dict[str, str]) -> PlanningProject:
        project = self.repository.get(project_id)
        request = project.workflow_state.confirmation_request
        if not request:
            return project

        for question in request.questions:
            question.answer = answers.get(question.id, "")
        request.resolved_at = utc_now()
        project.confirmation_history.append(request)
        project.workflow_state.confirmation_request = None
        project.workflow_state.status = WorkflowStatus.RUNNING
        step = self._step_by_id(project, request.step_id)
        step.status = StepStatus.PENDING
        step.summary = "已收到用户确认，等待继续执行。"

        if project.planning_context.human_notes is None:
            project.planning_context.human_notes = {}
        project.planning_context.human_notes.update({key: value for key, value in answers.items() if value.strip()})

        return self.repository.save(project)

    def _run_step(self, project: PlanningProject, step_id: str, progress: Optional[Callable[[str], None]] = None) -> None:
        if step_id == "product_context":
            answers = project.planning_context.human_notes or {}
            parse_product_context(project, answers)
            return

        if step_id == "research":
            run_research(project, progress=progress)
            return

        if step_id == "strategy":
            run_strategy(project, progress=progress)
            return

        if step_id == "content":
            run_content(project, progress=progress)
            return

        if step_id == "qa":
            run_qa(project, progress=progress)
            return

        if step_id == "export":
            run_export(project, progress=progress)
            return

        runners: Dict[str, StepRunner] = {
        }
        runner = runners.get(step_id)
        if runner:
            runner(project)

    def _progress_callback(self, project_id: str, step_id: str) -> Callable[[str], None]:
        def progress(summary: str) -> None:
            project = self.repository.get(project_id)
            step = self._step_by_id(project, step_id)
            if step.status != StepStatus.RUNNING:
                return
            step.summary = summary
            self.repository.save(project)

        return progress

    def _next_step(self, project: PlanningProject):
        for step in project.workflow_state.steps:
            if step.status in {StepStatus.PENDING, StepStatus.RUNNING}:
                return step
        return None

    def _step_by_id(self, project: PlanningProject, step_id: str):
        for step in project.workflow_state.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def _step_summary(self, project: PlanningProject, step_id: str) -> str:
        mapping = {
            "product_context": project.planning_context.summary,
            "research": project.research_result.summary,
            "strategy": project.strategy_result.summary,
            "content": project.content_result.summary,
            "qa": project.qa_result.summary,
            "export": project.export_result.summary,
        }
        return mapping.get(step_id, "")
