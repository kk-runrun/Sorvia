# -*- coding: utf-8 -*-
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Iterable, List

from fastapi import UploadFile

from super_planner.schemas.project import SpecFile


class UploadedFileStorage:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def save_many(self, project_id: str, files: Iterable[UploadFile]) -> List[SpecFile]:
        project_dir = self.base_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        saved: List[SpecFile] = []

        for order, upload in enumerate(files, start=1):
            if not upload or not upload.filename:
                continue
            file_id = uuid.uuid4().hex
            safe_name = Path(upload.filename).name
            target = project_dir / f"{file_id}_{safe_name}"
            content = await upload.read()
            target.write_bytes(content)
            saved.append(
                SpecFile(
                    id=file_id,
                    filename=safe_name,
                    content_type=upload.content_type or "",
                    size_bytes=len(content),
                    storage_path=str(target),
                    upload_order=order,
                )
            )

        return saved
