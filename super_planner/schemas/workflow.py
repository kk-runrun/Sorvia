# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.utcnow()


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_CONFIRMATION = "waiting_confirmation"


class WorkflowStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowStep(BaseModel):
    id: str
    name: str
    layer: str
    status: StepStatus = StepStatus.PENDING
    summary: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: str = ""
    error_type: str = ""


class ConfirmationQuestion(BaseModel):
    id: str
    prompt: str
    background: str = ""
    answer: str = ""


class ConfirmationRequest(BaseModel):
    id: str
    step_id: str
    title: str
    reason: str
    questions: List[ConfirmationQuestion] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: Optional[datetime] = None


class WorkflowState(BaseModel):
    project_id: str
    status: WorkflowStatus = WorkflowStatus.DRAFT
    current_step_id: str = "product_context"
    steps: List[WorkflowStep] = Field(default_factory=list)
    confirmation_request: Optional[ConfirmationRequest] = None
    updated_at: datetime = Field(default_factory=utc_now)


def default_steps() -> List[WorkflowStep]:
    return [
        WorkflowStep(id="product_context", name="产品资料解析", layer="Product Context"),
        WorkflowStep(id="research", name="市场研究", layer="Research"),
        WorkflowStep(id="strategy", name="策略策划", layer="Strategy"),
        WorkflowStep(id="content", name="文案生成", layer="Content"),
        WorkflowStep(id="qa", name="QA质检", layer="QA"),
        WorkflowStep(id="export", name="策划稿生成", layer="Export"),
    ]


def create_workflow_state(project_id: str) -> WorkflowState:
    return WorkflowState(project_id=project_id, steps=default_steps())
