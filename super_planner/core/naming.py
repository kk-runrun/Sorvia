# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from datetime import datetime


DEFAULT_PROJECT_NAME = "未命名策划项目"


def build_project_name(keywords: str) -> str:
    name = re.sub(r"\s+", " ", str(keywords or "")).strip(" \t\r\n-_/.")
    return name or DEFAULT_PROJECT_NAME


def build_export_filename(project_name: str, created_at: datetime | None = None) -> str:
    date_text = (created_at or datetime.now()).strftime("%Y%m%d")
    safe_name = _safe_filename_part(build_project_name(project_name))
    return f"超级策划_{safe_name}_{date_text}.xlsx"


def _safe_filename_part(value: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|]+", "_", value)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*_\s*", "_", text)
    text = re.sub(r"_+", "_", text).strip("._ ")
    return text[:90] or DEFAULT_PROJECT_NAME
