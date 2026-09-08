# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from typing import List

from super_planner.core.naming import build_project_name
from super_planner.schemas.project import PlanningProject
from super_planner.schemas.workflow import utc_now


def _model_to_dict(model):
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return json.loads(model.json())


def _project_from_dict(data) -> PlanningProject:
    if hasattr(PlanningProject, "model_validate"):
        return PlanningProject.model_validate(data)
    return PlanningProject.parse_obj(data)


class FileProjectRepository:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, project_id: str) -> Path:
        return self.base_dir / f"{project_id}.json"

    def save(self, project: PlanningProject) -> PlanningProject:
        if not project.project_name:
            project.project_name = build_project_name(project.user_input.keywords)
        project.updated_at = utc_now()
        project.workflow_state.updated_at = utc_now()
        path = self._path(project.project_id)
        path.write_text(json.dumps(_model_to_dict(project), ensure_ascii=False, indent=2), encoding="utf-8")
        return project

    def get(self, project_id: str) -> PlanningProject:
        path = self._path(project_id)
        if not path.exists():
            raise KeyError(project_id)
        return _project_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_recent(self, limit: int = 20) -> List[PlanningProject]:
        paths = sorted(self.base_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        return [self.get(path.stem) for path in paths[:limit]]
