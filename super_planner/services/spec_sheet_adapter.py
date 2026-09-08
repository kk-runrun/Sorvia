# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import sys
from pathlib import Path

from super_planner.core.config import OUTPUTS_DIR, WORKBENCH_ROOT
from super_planner.core.exceptions import WorkflowSystemError
from super_planner.schemas.project import SpecFile


def _short_error(error: Exception) -> str:
    message = str(error).strip() or "请检查上传文件或联系管理员。"
    return message.splitlines()[0].strip()[:220]


def _safe_stem(filename: str) -> str:
    stem = Path(filename or "规格书").stem
    return re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", stem).strip("_")[:80] or "规格书"


class WorkbenchSpecSheetAdapter:
    """Adapter around the mature workbench spec sheet arranger package."""

    def __init__(self, workbench_root: Path = WORKBENCH_ROOT):
        self.workbench_root = workbench_root

    def parse(self, project_id: str, spec_file: SpecFile):
        pipeline_cls = self._load_pipeline()
        source_path = Path(spec_file.storage_path)
        if not source_path.exists():
            raise WorkflowSystemError(f"规格书文件不存在：{source_path}")
        if source_path.stat().st_size <= 0:
            raise WorkflowSystemError("规格书文件为空，请重新上传。")

        output_dir = OUTPUTS_DIR / project_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{spec_file.upload_order:02d}_{_safe_stem(spec_file.filename)}_规格书参数整理结果.xlsx"

        try:
            pipeline = pipeline_cls(output_dir=output_dir)
            return pipeline.run(
                source_path,
                output_path=output_path,
                uploaded_filename=spec_file.filename,
                username="超级策划",
            )
        except WorkflowSystemError:
            raise
        except Exception as error:  # noqa: BLE001
            raise WorkflowSystemError(f"规格书解析异常：{_short_error(error)}") from error

    def _load_pipeline(self):
        if not self.workbench_root.exists():
            raise WorkflowSystemError(f"智能翻译工作台目录不存在：{self.workbench_root}")

        root_text = str(self.workbench_root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)

        try:
            from spec_sheet_arranger import SpecSheetArrangerPipeline
        except Exception as error:  # noqa: BLE001
            raise WorkflowSystemError(f"无法导入工作台规格书整理模块：{_short_error(error)}") from error

        return SpecSheetArrangerPipeline
