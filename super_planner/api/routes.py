# -*- coding: utf-8 -*-
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from super_planner.core.config import OUTPUTS_DIR, PROJECTS_DIR, UPLOADS_DIR
from super_planner.schemas.project import UserInput, create_project
from super_planner.storage.files import UploadedFileStorage
from super_planner.storage.repository import FileProjectRepository
from super_planner.workflow.engine import WorkflowEngine


router = APIRouter()
repository = FileProjectRepository(PROJECTS_DIR)
file_storage = UploadedFileStorage(UPLOADS_DIR)
engine = WorkflowEngine(repository)


class ConfirmationPayload(BaseModel):
    answers: Dict[str, str]


@router.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "service": "super-planner"}


@router.post("/projects")
async def create_planning_project(
    background_tasks: BackgroundTasks,
    keywords: str = Form(...),
    overview: str = Form(""),
    spec_files: Optional[List[UploadFile]] = File(None),
):
    keyword_value = keywords.strip()
    if not keyword_value:
        raise HTTPException(status_code=422, detail="产品关键词不能为空")

    project_id = uuid.uuid4().hex
    saved_files = await file_storage.save_many(project_id, spec_files or [])
    project = create_project(
        project_id=project_id,
        user_input=UserInput(keywords=keyword_value, overview=overview.strip()),
        spec_files=saved_files,
    )
    repository.save(project)
    background_tasks.add_task(engine.run, project_id)
    return repository.get(project_id)


@router.get("/projects/{project_id}")
async def get_planning_project(project_id: str):
    try:
        return repository.get(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="策划任务不存在") from exc


@router.get("/projects/{project_id}/download")
async def download_project_export(project_id: str):
    try:
        project = repository.get(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="�߻����񲻴���") from exc

    if project.export_result.status != "completed" or not project.export_result.artifact_path:
        raise HTTPException(status_code=404, detail="\u7b56\u5212\u7a3f\u5c1a\u672a\u751f\u6210")

    artifact_path = Path(project.export_result.artifact_path).resolve()
    output_root = OUTPUTS_DIR.resolve()
    if output_root not in artifact_path.parents or not artifact_path.exists():
        raise HTTPException(status_code=404, detail="\u7b56\u5212\u7a3f\u6587\u4ef6\u4e0d\u5b58\u5728")

    return FileResponse(
        artifact_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=project.export_result.artifact_filename or artifact_path.name,
    )


@router.get("/projects")
async def list_projects(limit: int = 20):
    return repository.list_recent(limit=limit)


@router.post("/projects/{project_id}/confirm")
async def confirm_and_resume(project_id: str, payload: ConfirmationPayload, background_tasks: BackgroundTasks):
    try:
        project = engine.resume_with_confirmation(project_id, payload.answers)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="策划任务不存在") from exc

    background_tasks.add_task(engine.run, project_id)
    return project
