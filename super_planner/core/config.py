# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
DATA_DIR = PROJECT_ROOT / "data"
PROJECTS_DIR = DATA_DIR / "projects"
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"
WORKBENCH_ROOT = Path(os.getenv("SUPER_PLANNER_WORKBENCH_ROOT", r"D:\智能翻译工作台1.2"))

for path in (DATA_DIR, PROJECTS_DIR, UPLOADS_DIR, OUTPUTS_DIR):
    path.mkdir(parents=True, exist_ok=True)
